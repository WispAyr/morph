"""Capabilities, actions, adapters, and the effect executor.

Capabilities are the only path to side effects. A capability is a typed interface::

    capabilities:
      route_control:
        requires: [operator.capabilities]     # context paths that grant it ([] = ungated)
        inputs: {source: string, destination: string}
        outputs: {route_id: string}
        failures: [destination_locked, router_busy]
        idempotency: [source, destination]    # input fields that identify a repeat
        retries: 2

An action binds a decision's action name to a capability, with CEL expressions that
build the inputs from the context::

    actions:
      route_source:
        capability: route_control
        inputs:
          source: source.id
          destination: destination.id

Adapters are plain callables ``(inputs: dict) -> dict`` registered per capability. They
signal declared failure modes by raising :class:`EffectFailure`. The
:class:`EffectExecutor` is the only thing that calls them, and it records every attempt in
an :class:`EffectLog`.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from .expressions import EvaluationError, Expression, ExpressionError, compile_value
from .schema import FIELD_TYPES, Schema, infer_type, matches_type, validate_typed_fields
from .validators import CapabilityValidator

Adapter = Callable[[dict[str, Any]], Any]

CAPABILITY_KEYS = {"description", "requires", "inputs", "outputs", "failures", "idempotency", "retries", "affects"}
ACTION_KEYS = {"description", "capability", "inputs"}


class EffectFailure(Exception):
    """Raised by an adapter to report a failure mode of its capability."""

    def __init__(self, code: str, message: str = "", *, retryable: bool = False, details: dict[str, Any] | None = None):
        super().__init__(message or code)
        self.code = code
        self.message = message or code
        self.retryable = retryable
        self.details = details or {}


# --- specifications --------------------------------------------------------------------


def _typed_fields(raw: Any, label: str, errors: list[str]) -> dict[str, str]:
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        errors.append(f"{label} must be an object of field: type")
        return {}
    fields: dict[str, str] = {}
    for name, type_name in raw.items():
        if not isinstance(type_name, str) or type_name not in FIELD_TYPES:
            errors.append(f"{label}.{name} has unknown type '{type_name}' (known: {sorted(FIELD_TYPES)})")
        else:
            fields[str(name)] = type_name
    return fields


@dataclass
class CapabilitySpec:
    name: str
    inputs: dict[str, str] = field(default_factory=dict)
    outputs: dict[str, str] = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)
    idempotency: list[str] = field(default_factory=list)
    retries: int = 0
    requires: list[str] | None = None
    description: str = ""
    # Subjects the effect changes in the world. Read by impact analysis, not enforced.
    affects: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, name: str, data: Any) -> "CapabilitySpec":
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise ValueError(f"capability '{name}' must be an object")

        errors: list[str] = []
        unknown = set(data) - CAPABILITY_KEYS
        if unknown:
            errors.append(f"capability '{name}' has unsupported keys: {sorted(unknown)}")

        requires = data.get("requires")
        if isinstance(requires, str):
            requires = [requires]
        if requires is not None and not isinstance(requires, list):
            errors.append(f"capability '{name}'.requires must be a list of context paths")
            requires = None
        elif isinstance(requires, list) and not all(isinstance(path, str) and path for path in requires):
            errors.append(f"capability '{name}'.requires must contain non-empty context paths")
            requires = []
        elif isinstance(requires, list):
            requires = list(dict.fromkeys(requires))

        inputs = _typed_fields(data.get("inputs"), f"capability '{name}'.inputs", errors)
        outputs = _typed_fields(data.get("outputs"), f"capability '{name}'.outputs", errors)

        failures = data.get("failures") or []
        if not isinstance(failures, list) or not all(isinstance(code, str) for code in failures):
            errors.append(f"capability '{name}'.failures must be a list of failure codes")
            failures = []

        idempotency = data.get("idempotency") or []
        if not isinstance(idempotency, list):
            errors.append(f"capability '{name}'.idempotency must be a list of input names")
            idempotency = []
        else:
            for key in idempotency:
                if not isinstance(key, str) or not key:
                    errors.append(f"capability '{name}'.idempotency must contain non-empty input names")
                elif key not in inputs:
                    errors.append(f"capability '{name}'.idempotency names unknown input '{key}'")

        retries = data.get("retries", 0)
        if not isinstance(retries, int) or isinstance(retries, bool) or retries < 0:
            errors.append(f"capability '{name}'.retries must be a non-negative integer")
            retries = 0

        affects = data.get("affects") or []
        if not isinstance(affects, list) or not all(isinstance(item, str) and item for item in affects):
            errors.append(f"capability '{name}'.affects must be a list of non-empty subjects")
            affects = []

        if errors:
            raise ValueError("; ".join(errors))

        return cls(
            name=name,
            inputs=inputs,
            outputs=outputs,
            failures=list(failures),
            idempotency=list(idempotency),
            retries=retries,
            requires=list(requires) if requires is not None else None,
            description=str(data.get("description", "")),
            affects=list(affects),
        )

    def grant_definition(self) -> dict[str, Any] | None:
        """The shape CapabilityValidator expects. None means 'use the default grant path'."""
        return {"requires": self.requires} if self.requires is not None else None

    def validate_inputs(self, inputs: Any) -> list[str]:
        return validate_typed_fields(inputs, self.inputs, "inputs", require_all=True)

    def validate_outputs(self, outputs: Any) -> list[str]:
        return validate_typed_fields(outputs, self.outputs, "outputs", require_all=True)

    def idempotency_key(self, inputs: dict[str, Any]) -> str | None:
        if not self.idempotency:
            return None
        parts = {key: inputs.get(key) for key in self.idempotency}
        return f"{self.name}:{json.dumps(parts, sort_keys=True, default=str)}"

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "inputs": dict(self.inputs),
            "outputs": dict(self.outputs),
            "failures": list(self.failures),
            "idempotency": list(self.idempotency),
            "retries": self.retries,
        }
        if self.requires is not None:
            data["requires"] = list(self.requires)
        if self.description:
            data["description"] = self.description
        if self.affects:
            data["affects"] = list(self.affects)
        return data


@dataclass
class ActionSpec:
    name: str
    capability: str
    inputs: dict[str, Any] = field(default_factory=dict)
    description: str = ""
    _bindings: dict[str, Expression | tuple[str, Any]] = field(default_factory=dict, repr=False)

    @classmethod
    def from_dict(cls, name: str, data: Any, capabilities: dict[str, CapabilitySpec], schema: Schema) -> "ActionSpec":
        if not isinstance(data, dict):
            raise ValueError(f"action '{name}' must be an object")

        errors: list[str] = []
        unknown = set(data) - ACTION_KEYS
        if unknown:
            errors.append(f"action '{name}' has unsupported keys: {sorted(unknown)}")

        capability_name = data.get("capability")
        capability = capabilities.get(capability_name) if isinstance(capability_name, str) else None
        if capability is None:
            errors.append(f"action '{name}' binds unknown capability '{capability_name}' (known: {sorted(capabilities)})")
            raise ValueError("; ".join(errors))

        raw_inputs = data.get("inputs") or {}
        if not isinstance(raw_inputs, dict):
            errors.append(f"action '{name}'.inputs must be an object")
            raw_inputs = {}

        missing = set(capability.inputs) - set(raw_inputs)
        if missing:
            errors.append(f"action '{name}' does not bind inputs {sorted(missing)} of capability '{capability.name}'")
        extra = set(raw_inputs) - set(capability.inputs)
        if extra:
            errors.append(f"action '{name}' binds inputs {sorted(extra)} that capability '{capability.name}' does not declare")

        bindings: dict[str, Expression | tuple[str, Any]] = {}
        for input_name, raw in raw_inputs.items():
            if input_name not in capability.inputs:
                continue
            if isinstance(raw, str):
                try:
                    expression = compile_value(raw)
                except ExpressionError as exc:
                    errors.append(f"action '{name}'.inputs.{input_name}: {exc}")
                    continue
                for error in schema.validate_paths(expression.paths):
                    errors.append(f"action '{name}'.inputs.{input_name}: {error}")
                bindings[input_name] = expression
            else:
                expected = capability.inputs[input_name]
                if not matches_type(raw, expected):
                    errors.append(
                        f"action '{name}'.inputs.{input_name} literal must be {expected}, got {infer_type(raw)}"
                    )
                bindings[input_name] = ("literal", raw)

        if errors:
            raise ValueError("; ".join(errors))

        return cls(
            name=name,
            capability=capability.name,
            inputs=dict(raw_inputs),
            description=str(data.get("description", "")),
            _bindings=bindings,
        )

    @property
    def paths(self) -> set[str]:
        out: set[str] = set()
        for binding in self._bindings.values():
            if isinstance(binding, Expression):
                out.update(binding.paths)
        return out

    def evaluate_inputs(self, context: dict[str, Any]) -> dict[str, Any]:
        """Build the capability inputs from the context. Raises EvaluationError."""
        inputs: dict[str, Any] = {}
        for input_name, binding in self._bindings.items():
            if isinstance(binding, Expression):
                try:
                    inputs[input_name] = binding.value(context)
                except EvaluationError as exc:
                    raise EvaluationError(f"input '{input_name}': {exc}") from exc
            else:
                inputs[input_name] = binding[1]
        return inputs

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"capability": self.capability, "inputs": dict(self.inputs)}
        if self.description:
            data["description"] = self.description
        return data


def parse_capabilities(raw: Any) -> dict[str, CapabilitySpec]:
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("capabilities must be an object keyed by capability name")
    specs: dict[str, CapabilitySpec] = {}
    errors: list[str] = []
    for name, data in raw.items():
        try:
            specs[str(name)] = CapabilitySpec.from_dict(str(name), data)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("Invalid capabilities: " + "; ".join(errors))
    return specs


def parse_actions(raw: Any, capabilities: dict[str, CapabilitySpec], schema: Schema) -> dict[str, ActionSpec]:
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise ValueError("actions must be an object keyed by action name")
    specs: dict[str, ActionSpec] = {}
    errors: list[str] = []
    for name, data in raw.items():
        try:
            specs[str(name)] = ActionSpec.from_dict(str(name), data, capabilities, schema)
        except ValueError as exc:
            errors.append(str(exc))
    if errors:
        raise ValueError("Invalid actions: " + "; ".join(errors))
    return specs


# --- execution ------------------------------------------------------------------------


@dataclass
class EffectRecord:
    id: str
    at: str
    action: str | None
    capability: str | None
    status: str  # succeeded | failed | skipped | denied | unbound
    attempt: int
    inputs: dict[str, Any] | None
    idempotency_key: str | None
    outputs: dict[str, Any] | None
    error: dict[str, Any] | None
    duration_ms: float
    decision: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "at": self.at,
            "action": self.action,
            "capability": self.capability,
            "status": self.status,
            "attempt": self.attempt,
            "inputs": self.inputs,
            "idempotency_key": self.idempotency_key,
            "outputs": self.outputs,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "decision": self.decision,
        }


class EffectLog:
    """Append-only record of every effect attempt. Pass a sink to forward records elsewhere."""

    def __init__(self, sink: Callable[[EffectRecord], None] | None = None):
        self._records: list[EffectRecord] = []
        self._sink = sink

    def append(self, record: EffectRecord) -> None:
        self._records.append(record)
        if self._sink is not None:
            self._sink(record)

    def records(self) -> list[EffectRecord]:
        return list(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def to_list(self) -> list[dict[str, Any]]:
        return [record.to_dict() for record in self._records]


class AdapterRegistry:
    """Maps capability names to the callables that implement them."""

    def __init__(self, adapters: dict[str, Adapter] | None = None):
        self._adapters: dict[str, Adapter] = {}
        for name, adapter in (adapters or {}).items():
            self.register(name, adapter)

    def register(self, capability: str, adapter: Adapter) -> None:
        if not callable(adapter):
            raise TypeError(f"adapter for '{capability}' must be callable")
        self._adapters[capability] = adapter

    def get(self, capability: str) -> Adapter | None:
        return self._adapters.get(capability)

    def names(self) -> list[str]:
        return sorted(self._adapters)

    @classmethod
    def coerce(cls, adapters: Any) -> "AdapterRegistry":
        if isinstance(adapters, cls):
            return adapters
        if callable(adapters) and not isinstance(adapters, dict):
            adapters = adapters()
            if isinstance(adapters, cls):
                return adapters
        if isinstance(adapters, dict):
            return cls(adapters)
        raise TypeError("adapters must be an AdapterRegistry, a dict of callables, or a callable returning one")


@dataclass
class ExecutionResult:
    status: str  # executed | skipped | failed | denied | unbound
    decision: dict[str, Any]
    action: str | None
    capability: str | None
    inputs: dict[str, Any] | None
    outputs: dict[str, Any] | None
    attempts: int
    error: dict[str, Any] | None
    records: list[EffectRecord] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status in ("executed", "skipped")

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "decision": self.decision,
            "action": self.action,
            "capability": self.capability,
            "inputs": self.inputs,
            "outputs": self.outputs,
            "attempts": self.attempts,
            "error": self.error,
            "effects": [record.to_dict() for record in self.records],
        }


class EffectExecutor:
    """Runs the effect behind a decision through the registered adapters.

    The executor re-checks the capability grant against the context before calling an
    adapter, so a policy that forgot `requires` still cannot cause an ungranted effect.
    """

    def __init__(self, runtime: Any, adapters: Any, log: EffectLog | None = None, *, strict: bool = True):
        self.runtime = runtime
        self.adapters = AdapterRegistry.coerce(adapters)
        self.log = log if log is not None else EffectLog()
        self._completed: dict[str, dict[str, Any]] = {}

        if strict:
            missing = [name for name in sorted(runtime.capability_specs) if self.adapters.get(name) is None]
            if missing:
                raise ValueError(f"No adapter registered for capabilities: {missing}")

    def run(self, context: dict[str, Any]) -> ExecutionResult:
        """Evaluate the context and execute the resulting decision."""
        return self.execute(self.runtime.evaluate(context), context)

    def remember(self, idempotency_key: str, outputs: dict[str, Any]) -> None:
        """Mark an effect as already completed, e.g. when replaying a durable log."""
        self._completed[idempotency_key] = dict(outputs)

    def completed_keys(self) -> list[str]:
        return sorted(self._completed)

    def execute(self, decision: dict[str, Any], context: dict[str, Any]) -> ExecutionResult:
        action_name = decision.get("action")
        decision_status = decision.get("status")
        action = self.runtime.action_specs.get(action_name) if isinstance(action_name, str) else None

        if action is None:
            status = "unbound" if decision_status == "allow" else "denied"
            record = self._record(status, action_name, None, 0, None, None, None, None, 0.0, decision_status)
            return ExecutionResult(status, decision, action_name, None, None, None, 0, None, [record])

        capability: CapabilitySpec = self.runtime.capability_specs[action.capability]

        grant_errors = CapabilityValidator.validate(capability.name, context, capability.grant_definition())
        if grant_errors:
            error = {"code": "capability_not_granted", "message": "; ".join(grant_errors), "retryable": False}
            record = self._record("denied", action.name, capability.name, 0, None, None, None, error, 0.0, decision_status)
            return ExecutionResult("denied", decision, action.name, capability.name, None, None, 0, error, [record])

        try:
            inputs = action.evaluate_inputs(context)
        except EvaluationError as exc:
            error = {"code": "invalid_inputs", "message": str(exc), "retryable": False}
            record = self._record("failed", action.name, capability.name, 0, None, None, None, error, 0.0, decision_status)
            return ExecutionResult("failed", decision, action.name, capability.name, None, None, 0, error, [record])

        input_errors = capability.validate_inputs(inputs)
        if input_errors:
            error = {"code": "invalid_inputs", "message": "; ".join(input_errors), "retryable": False}
            record = self._record("failed", action.name, capability.name, 0, inputs, None, None, error, 0.0, decision_status)
            return ExecutionResult("failed", decision, action.name, capability.name, inputs, None, 0, error, [record])

        key = capability.idempotency_key(inputs)
        if key is not None and key in self._completed:
            outputs = dict(self._completed[key])
            record = self._record("skipped", action.name, capability.name, 0, inputs, key, outputs, None, 0.0, decision_status)
            return ExecutionResult("skipped", decision, action.name, capability.name, inputs, outputs, 0, None, [record])

        adapter = self.adapters.get(capability.name)
        if adapter is None:
            error = {"code": "adapter_missing", "message": f"no adapter registered for '{capability.name}'", "retryable": False}
            record = self._record("failed", action.name, capability.name, 0, inputs, key, None, error, 0.0, decision_status)
            return ExecutionResult("failed", decision, action.name, capability.name, inputs, None, 0, error, [record])

        records: list[EffectRecord] = []
        max_attempts = capability.retries + 1
        attempt = 0
        error: dict[str, Any] | None = None
        while attempt < max_attempts:
            attempt += 1
            started = time.perf_counter()
            try:
                raw = adapter(inputs)
                outputs = dict(raw) if isinstance(raw, dict) else ({} if raw is None else {"value": raw})
                output_errors = capability.validate_outputs(outputs)
                duration = (time.perf_counter() - started) * 1000
                if output_errors:
                    error = {"code": "invalid_outputs", "message": "; ".join(output_errors), "retryable": False}
                    records.append(self._record("failed", action.name, capability.name, attempt, inputs, key, outputs, error, duration, decision_status))
                    break
                records.append(self._record("succeeded", action.name, capability.name, attempt, inputs, key, outputs, None, duration, decision_status))
                if key is not None:
                    self._completed[key] = dict(outputs)
                return ExecutionResult("executed", decision, action.name, capability.name, inputs, outputs, attempt, None, records)
            except EffectFailure as exc:
                duration = (time.perf_counter() - started) * 1000
                error = {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": exc.retryable,
                    "declared": exc.code in capability.failures,
                }
                if exc.details:
                    error["details"] = exc.details
                records.append(self._record("failed", action.name, capability.name, attempt, inputs, key, None, error, duration, decision_status))
                if exc.retryable and attempt < max_attempts:
                    continue
                break
            except Exception as exc:  # an adapter bug is a failure, never a crash of the control plane
                duration = (time.perf_counter() - started) * 1000
                error = {"code": "adapter_error", "message": f"{type(exc).__name__}: {exc}", "retryable": False}
                records.append(self._record("failed", action.name, capability.name, attempt, inputs, key, None, error, duration, decision_status))
                break

        return ExecutionResult("failed", decision, action.name, capability.name, inputs, None, attempt, error, records)

    def _record(
        self,
        status: str,
        action: str | None,
        capability: str | None,
        attempt: int,
        inputs: dict[str, Any] | None,
        key: str | None,
        outputs: dict[str, Any] | None,
        error: dict[str, Any] | None,
        duration_ms: float,
        decision: str | None,
    ) -> EffectRecord:
        record = EffectRecord(
            id=uuid.uuid4().hex,
            at=datetime.now(timezone.utc).isoformat(),
            action=action,
            capability=capability,
            status=status,
            attempt=attempt,
            inputs=dict(inputs) if inputs is not None else None,
            idempotency_key=key,
            outputs=dict(outputs) if outputs is not None else None,
            error=error,
            duration_ms=round(duration_ms, 3),
            decision=decision,
        )
        self.log.append(record)
        return record


def load_adapters(reference: str) -> AdapterRegistry:
    """Resolve ``package.module:attribute`` to an AdapterRegistry.

    The attribute may be a registry, a dict of callables, or a zero-argument callable
    returning either.
    """
    import importlib

    module_name, separator, attribute = reference.partition(":")
    if not separator or not module_name or not attribute:
        raise ValueError(f"adapters reference '{reference}' must look like package.module:attribute")
    module = importlib.import_module(module_name)
    try:
        target = getattr(module, attribute)
    except AttributeError as exc:
        raise ValueError(f"module '{module_name}' has no attribute '{attribute}'") from exc
    return AdapterRegistry.coerce(target)

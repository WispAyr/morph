from __future__ import annotations

from typing import Any

from .expressions import Predicate, compile_when
from .schema import Schema
from .validators import CapabilityValidator, PolicyValidator


class _Missing:
    """Sentinel for a context field that is absent or null."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"


MISSING = _Missing()

DENY_ACTION = "raise_alert"


class MORPHRuntime:
    """A reusable engine for AI-native policy evaluation and operational decisions.

    Evaluation is deny-by-default: a policy only produces a decision when its condition
    holds and every capability it requires is granted. A condition that reads a missing
    field, compares incompatible types, or is not boolean never matches.

    When the definition declares entities, policies may only read declared fields (checked
    at construction) and contexts must carry the declared types (checked at evaluation).
    """

    def __init__(
        self,
        name: str,
        version: str,
        policies: list[dict[str, Any]],
        capabilities: dict[str, Any] | None = None,
        entities: Any = None,
    ):
        errors = PolicyValidator.validate_all(policies)
        if errors:
            raise ValueError("Invalid MORPH policies: " + "; ".join(errors))

        self.name = name
        self.version = version
        self.policies = policies
        self.capabilities = capabilities or {}
        self.schema = Schema.from_ir(entities)

        self._predicates: list[Predicate] = []
        schema_errors: list[str] = []
        for policy in policies:
            predicate = compile_when(policy.get("when"))
            self._predicates.append(predicate)
            for error in self.schema.validate_paths(predicate.paths):
                schema_errors.append(f"policy '{policy.get('name', 'unknown')}': {error}")
        if schema_errors:
            raise ValueError("Policies read fields outside the entity schema: " + "; ".join(schema_errors))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MORPHRuntime":
        if not data or "policies" not in data or not data["policies"]:
            raise ValueError("MORPHRuntime requires a non-empty 'policies' definition.")

        return cls(
            name=data.get("name", "morph"),
            version=data.get("version", "0.1.0"),
            policies=data["policies"],
            capabilities=data.get("capabilities"),
            entities=data.get("entities"),
        )

    def evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        context_errors = self.schema.validate_context(context)
        if context_errors:
            return {
                "name": self.name,
                "version": self.version,
                "status": "deny",
                "action": DENY_ACTION,
                "reason": "invalid_context",
                "errors": context_errors,
                "trace": [],
            }

        for policy, predicate in zip(self.policies, self._predicates):
            if self._capability_missing(policy, context):
                continue
            if predicate.evaluate(context):
                result = dict(policy.get("result", {"status": "deny", "action": DENY_ACTION}))
                policy_name = policy.get("name", "unknown")
                # Audit fields are written last so a policy result cannot spoof them.
                return {
                    **result,
                    "name": self.name,
                    "version": self.version,
                    "policy": policy_name,
                    "trace": [policy_name],
                }

        return {
            "name": self.name,
            "version": self.version,
            "status": "deny",
            "action": DENY_ACTION,
            "reason": "no_matching_policy",
            "trace": [],
        }

    def explain(self, context: dict[str, Any]) -> dict[str, Any]:
        """Evaluate and also report how every policy fared, for debugging a decision."""
        decision = self.evaluate(context)
        report = []
        for policy, predicate in zip(self.policies, self._predicates):
            report.append(
                {
                    "policy": policy.get("name", "unknown"),
                    "expression": predicate.source,
                    "capabilities_granted": not self._capability_missing(policy, context),
                    "condition_holds": predicate.evaluate(context),
                }
            )
        return {**decision, "policies": report}

    def _capability_missing(self, policy: dict[str, Any], context: dict[str, Any]) -> bool:
        required = policy.get("requires")
        if not required:
            return False

        if isinstance(required, str):
            required = [required]

        for capability in required:
            definition = self.capabilities.get(capability) if isinstance(self.capabilities, dict) else None
            if CapabilityValidator.validate(capability, context, definition):
                return True

        return False

    @staticmethod
    def matches(conditions: Any, context: dict[str, Any]) -> bool:
        """Return True when the condition block holds against the context."""
        return compile_when(conditions).evaluate(context)

    # Backwards-compatible alias for callers that used the private name.
    _matches = matches

    @staticmethod
    def resolve_field(context: dict[str, Any], field: str) -> Any:
        """Resolve a dotted path in the context, returning MISSING if absent or null."""
        current: Any = context
        for segment in field.split("."):
            if not isinstance(current, dict):
                return MISSING
            current = current.get(segment)
            if current is None:
                return MISSING
        return current

    _resolve_field = resolve_field

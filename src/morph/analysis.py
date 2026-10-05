"""Semantic checks on a MORPH definition: the things a loader cannot catch but an
operator would want to know before trusting a generated specification.

Static checks (exact):

* state machines: unreachable states, terminal states, shadowed transitions, transitions
  on unknown actions or outcomes;
* wiring: capabilities nothing uses, actions no policy selects, allow policies whose
  action needs a capability they do not require, declared fields nothing reads;
* policies: a catch-all that is not last, conditions that are constant.

Reachability checks (sampled): policy reachability is undecidable in general, so the
analyzer draws seeded random contexts from the entity schema and the literals the
policies mention, evaluates the policy list on each, and reports policies that never fire,
policies whose condition held but which an earlier policy always pre-empted, and deny
policies that an earlier allow pre-empts (an ordering hazard in a first-match engine).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from .effects import ActionSpec, CapabilitySpec
from .expressions import Predicate, compile_when
from .ir import MORPHIR
from .runtime import MORPHRuntime
from .schema import STATE_FIELD, EntityType, Schema
from .validators import CapabilityValidator

OUTCOMES = ("succeeded", "failed", "skipped", "denied")
SEVERITIES = ("error", "warning", "info")


@dataclass
class Finding:
    severity: str
    code: str
    message: str
    location: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"severity": self.severity, "code": self.code, "message": self.message, "location": self.location}

    def __str__(self) -> str:
        where = f" [{self.location}]" if self.location else ""
        return f"{self.severity}: {self.code}{where}: {self.message}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    samples: int = 0
    coverage: dict[str, dict[str, int]] = field(default_factory=dict)

    def add(self, severity: str, code: str, message: str, location: str = "") -> None:
        self.findings.append(Finding(severity, code, message, location))

    @property
    def errors(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "error"]

    @property
    def warnings(self) -> list[Finding]:
        return [finding for finding in self.findings if finding.severity == "warning"]

    def by_code(self, code: str) -> list[Finding]:
        return [finding for finding in self.findings if finding.code == code]

    def to_dict(self) -> dict[str, Any]:
        return {
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "samples": self.samples,
            "coverage": self.coverage,
            "findings": [finding.to_dict() for finding in self.findings],
        }


def analyze(definition: MORPHIR | dict[str, Any], *, samples: int = 500, seed: int = 0) -> Report:
    ir = definition if isinstance(definition, MORPHIR) else MORPHIR.from_dict(definition)
    runtime = MORPHRuntime(
        name=ir.name,
        version=ir.version,
        policies=ir.policies,
        capabilities=ir.capabilities,
        entities=ir.entities,
        actions=ir.actions,
    )
    report = Report()
    _check_state_machines(runtime, report)
    _check_wiring(runtime, report)
    _check_policy_shapes(runtime, report)
    if samples > 0 and runtime.policies:
        _check_reachability(runtime, report, samples=samples, seed=seed)
    return report


# --- static checks ----------------------------------------------------------------------


def _check_state_machines(runtime: MORPHRuntime, report: Report) -> None:
    for entity in runtime.schema.stateful_entities:
        where = f"entities.{entity.name}"
        targets = {transition.to for transition in entity.transitions}
        sources_any = any(transition.sources is None for transition in entity.transitions)
        for state in entity.states:
            if state != entity.initial and state not in targets:
                report.add("warning", "state-unreachable", f"state '{state}' has no transition leading to it", where)
            leaves = sources_any or any(transition.sources and state in transition.sources for transition in entity.transitions)
            if not leaves and len(entity.states) > 1:
                report.add("info", "state-terminal", f"state '{state}' has no transition leaving it", where)

        for index, transition in enumerate(entity.transitions):
            location = f"{where}.transitions[{index}]"
            kind = transition.on
            if kind != "observed":
                action_name, _, outcome = kind.rpartition(".")
                if not action_name or outcome not in OUTCOMES:
                    report.add("warning", "transition-unknown-event", f"'{kind}' is neither 'observed' nor '<action>.<outcome>' with outcome in {list(OUTCOMES)}", location)
                elif runtime.action_specs and action_name not in runtime.action_specs:
                    report.add("error", "transition-unknown-action", f"'{kind}' refers to action '{action_name}' which is not declared (known: {sorted(runtime.action_specs)})", location)

            for earlier_index in range(index):
                earlier = entity.transitions[earlier_index]
                if earlier.on != transition.on or earlier.when is not None:
                    continue
                overlap = earlier.sources is None or transition.sources is None or set(earlier.sources) & set(transition.sources)
                if overlap:
                    report.add(
                        "warning",
                        "transition-shadowed",
                        f"transition on '{transition.on}' to '{transition.to}' can never fire: transitions[{earlier_index}] matches the same event and states first with no condition",
                        location,
                    )
                    break


def _check_wiring(runtime: MORPHRuntime, report: Report) -> None:
    used_capabilities = {action.capability for action in runtime.action_specs.values()}
    required = set()
    selected_actions = set()
    for policy in runtime.policies:
        requires = policy.get("requires") or []
        required.update([requires] if isinstance(requires, str) else requires)
        selected_actions.add(policy.get("result", {}).get("action"))

    for name in runtime.capability_specs:
        if name not in used_capabilities and name not in required:
            report.add("warning", "capability-unused", f"capability '{name}' is neither bound by an action nor required by a policy", f"capabilities.{name}")

    for name in runtime.action_specs:
        if name not in selected_actions:
            report.add("warning", "action-unused", f"action '{name}' is never selected by a policy", f"actions.{name}")

    for policy in runtime.policies:
        result = policy.get("result", {})
        action = runtime.action_specs.get(result.get("action"))
        if action is None or result.get("status") != "allow":
            continue
        capability = runtime.capability_specs[action.capability]
        if capability.requires == []:
            continue  # ungated
        requires = policy.get("requires") or []
        requires = [requires] if isinstance(requires, str) else requires
        if capability.name not in requires:
            report.add(
                "warning",
                "policy-missing-requires",
                f"allows '{action.name}' which needs capability '{capability.name}', but does not require it; the executor will refuse ungranted contexts after the policy already matched",
                f"policies.{policy.get('name', 'unknown')}",
            )

    if not runtime.schema.empty:
        read: set[str] = set()
        for predicate in runtime._predicates:
            read.update(predicate.paths)
        for action in runtime.action_specs.values():
            read.update(action.paths)
        for capability in runtime.capability_specs.values():
            read.update(capability.requires or [])
        if any(policy.get("requires") for policy in runtime.policies) and not all(
            spec.requires is not None for spec in runtime.capability_specs.values()
        ):
            read.add(CapabilityValidator.DEFAULT_GRANT_PATH)
        for entity in runtime.schema.entities.values():
            for transition in entity.transitions:
                if transition.when is not None:
                    read.update(transition.when.paths)
                if transition.id_expr is not None:
                    read.update(transition.id_expr.paths)
        read_fields = {tuple(path.split(".")[:2]) for path in read}
        for entity in runtime.schema.entities.values():
            for field_name in entity.fields:
                if field_name in ("id", STATE_FIELD):
                    continue
                if (entity.name, field_name) not in read_fields:
                    report.add("info", "field-unread", f"field '{entity.name}.{field_name}' is declared but nothing reads it", f"entities.{entity.name}")


def _check_policy_shapes(runtime: MORPHRuntime, report: Report) -> None:
    count = len(runtime.policies)
    for index, (policy, predicate) in enumerate(zip(runtime.policies, runtime._predicates)):
        name = policy.get("name", "unknown")
        if predicate.source == "true" and not policy.get("requires") and index < count - 1:
            report.add("warning", "policy-catch-all-not-last", f"'{name}' matches every context, so the {count - index - 1} policies after it can never fire", f"policies.{name}")
        if predicate.source == "false":
            report.add("warning", "policy-never-fires", f"'{name}' has a constant false condition", f"policies.{name}")


# --- sampled reachability --------------------------------------------------------------


FOCUS_BIAS = 0.9  # chance a focused sample draws a path's value from the focus policy's literals
GLOBAL_BIAS = 0.6  # chance an unfocused sample draws from any literal compared with that path
OTHER_BIAS = 0.2  # chance a focused sample draws another policy's literal for a path its focus does not compare


def _grant_paths(runtime: MORPHRuntime, capability: str) -> list[str]:
    spec = runtime.capability_specs.get(capability)
    if spec is not None and spec.requires is not None:
        return list(spec.requires)
    return [CapabilityValidator.DEFAULT_GRANT_PATH]


def _policy_requires(policy: dict[str, Any]) -> list[str]:
    requires = policy.get("requires") or []
    return [requires] if isinstance(requires, str) else list(requires)


class _Sampler:
    """Draws contexts from the schema, biased toward the literals policies compare against."""

    def __init__(self, runtime: MORPHRuntime, seed: int):
        self.runtime = runtime
        self.random = random.Random(seed)
        self.schema = runtime.schema

        # path -> literals anything compares it with (policies, actions, transitions, grants)
        self.pools: dict[str, list[Any]] = {}
        # per policy: path -> literals that policy compares it with, plus the paths it reads
        self.focus_pools: list[dict[str, list[Any]]] = []
        self.focus_paths: list[set[str]] = []

        for policy, predicate in zip(runtime.policies, runtime._predicates):
            pool: dict[str, list[Any]] = {}
            self._merge(pool, predicate.comparisons)
            for capability in _policy_requires(policy):
                for path in _grant_paths(runtime, capability):
                    self._merge(pool, {path: (capability,)})
            self.focus_pools.append(pool)
            self.focus_paths.append(set(predicate.paths) | set(pool))
            self._merge(self.pools, pool)
        for action in runtime.action_specs.values():
            for binding in action._bindings.values():
                if hasattr(binding, "comparisons"):
                    self._merge(self.pools, binding.comparisons)
        for entity in self.schema.entities.values():
            for transition in entity.transitions:
                if transition.when is not None:
                    self._merge(self.pools, transition.when.comparisons)

        literals = [value for values in self.pools.values() for value in values]
        self.strings = sorted({value for value in literals if isinstance(value, str)} | set(runtime.capability_specs) | {"", "other"})
        ints = {value for value in literals if isinstance(value, int) and not isinstance(value, bool)}
        self.ints = sorted(ints | {value + 1 for value in ints} | {value - 1 for value in ints} | {0, 1, -1, 1000})
        doubles = {float(value) for value in literals if isinstance(value, (int, float)) and not isinstance(value, bool)}
        self.doubles = sorted(doubles | {value + 0.5 for value in doubles} | {value - 0.5 for value in doubles} | {0.0, 1.0})

        # Roots referenced when there is no schema to draw from.
        self.loose_paths: dict[str, set[str]] = {}
        if self.schema.empty:
            for predicate in runtime._predicates:
                for path in predicate.paths:
                    root, _, rest = path.partition(".")
                    self.loose_paths.setdefault(root, set()).add(rest)
            for path in self.pools:
                root, _, rest = path.partition(".")
                self.loose_paths.setdefault(root, set()).add(rest)

    @staticmethod
    def _merge(target: dict[str, list[Any]], source: dict[str, Any]) -> None:
        for path, values in source.items():
            bucket = target.setdefault(path, [])
            for value in values:
                if value not in bucket:
                    bucket.append(value)

    def _from_literal(self, literal: Any, type_name: str) -> Any:
        """Shape a compared literal into a value of the declared type, or None if it cannot."""
        choice = self.random.choice
        if isinstance(literal, bool):
            return literal if type_name in ("bool", "any") else None
        if isinstance(literal, int):
            if type_name == "int":
                return choice([literal - 1, literal, literal + 1, literal // 2, literal * 2])
            if type_name == "double":
                return float(choice([literal - 1, literal, literal + 1])) + choice([0.0, 0.5, -0.5])
            if type_name == "any":
                return literal
            return None
        if isinstance(literal, float):
            if type_name in ("double", "any"):
                return choice([literal - 0.5, literal, literal + 0.5])
            return None
        if isinstance(literal, str):
            if type_name in ("string", "any"):
                return literal
            if type_name == "list":
                extras = self.random.sample(self.strings, min(choice([0, 1, 2]), len(self.strings)))
                return [literal] + [value for value in extras if value != literal]
            return None
        return None

    def value(self, type_name: str, path: str, focus: dict[str, list[Any]] | None, entity: EntityType | None = None, field_name: str | None = None) -> Any:
        choice = self.random.choice
        global_bias = GLOBAL_BIAS if focus is None else OTHER_BIAS
        for pool, bias in ((focus, FOCUS_BIAS), (self.pools, global_bias)):
            literals = pool.get(path) if pool else None
            if literals and self.random.random() < bias:
                shaped = self._from_literal(choice(literals), type_name)
                if shaped is not None:
                    if entity is not None and field_name == STATE_FIELD and entity.stateful and shaped not in entity.states:
                        break
                    return shaped
        if entity is not None and field_name == STATE_FIELD and entity.stateful:
            return choice(entity.states)
        if type_name == "string":
            return choice(self.strings)
        if type_name == "int":
            return choice(self.ints)
        if type_name == "double":
            return choice(self.doubles)
        if type_name == "bool":
            return choice([True, False])
        if type_name == "list":
            size = choice([0, 1, 2, len(self.strings)])
            return self.random.sample(self.strings, min(size, len(self.strings)))
        if type_name == "map":
            return {}
        return choice([choice(self.strings), choice(self.ints), choice([True, False]), None])

    def context(self, focus_index: int | None) -> dict[str, Any]:
        focus = self.focus_pools[focus_index] if focus_index is not None else None
        wanted = self.focus_paths[focus_index] if focus_index is not None else set()
        wanted_roots = {path.split(".")[0] for path in wanted}

        def keep(path: str, root: str, probability: float) -> bool:
            if path in wanted or root in wanted_roots and path == root:
                return True
            return self.random.random() >= probability

        context: dict[str, Any] = {}
        if not self.schema.empty:
            for entity in self.schema.entities.values():
                if not keep(entity.name, entity.name, 0.05):
                    continue  # entity absent
                instance: dict[str, Any] = {}
                for field_name, type_name in entity.fields.items():
                    path = f"{entity.name}.{field_name}"
                    if field_name != "id" and not keep(path, entity.name, 0.1):
                        continue  # optional field absent
                    instance[field_name] = self.value(type_name, path, focus, entity, field_name)
                if "id" in entity.fields:
                    instance["id"] = f"{entity.name}-{self.random.randint(1, 3)}"
                context[entity.name] = instance
        else:
            for root, rests in self.loose_paths.items():
                if not keep(root, root, 0.05):
                    continue
                if any(rest == "" for rest in rests):
                    context[root] = self.value("any", root, focus)
                    continue
                instance = {}
                for rest in rests:
                    head = rest.split(".")[0]
                    path = f"{root}.{head}"
                    if not keep(path, root, 0.1):
                        continue
                    instance[head] = self.value("any", path, focus)
                context[root] = instance
        return context


def _check_reachability(runtime: MORPHRuntime, report: Report, *, samples: int, seed: int) -> None:
    sampler = _Sampler(runtime, seed)
    names = [policy.get("name", f"#{index}") for index, policy in enumerate(runtime.policies)]
    fired = [0] * len(names)
    matched = [0] * len(names)
    shadowed_by: list[dict[int, int]] = [dict() for _ in names]
    deny_after_allow: dict[tuple[int, int], int] = {}

    slots = len(names) + 1  # one focused slot per policy, plus an unfocused one
    for sample_index in range(samples):
        slot = sample_index % slots
        context = sampler.context(slot if slot < len(names) else None)
        if runtime.schema.validate_context(context):
            continue
        holders: list[int] = []
        for index, (policy, predicate) in enumerate(zip(runtime.policies, runtime._predicates)):
            if runtime._capability_missing(policy, context):
                continue
            if predicate.evaluate(context):
                holders.append(index)
        if not holders:
            continue
        winner = holders[0]
        fired[winner] += 1
        for index in holders:
            matched[index] += 1
            if index != winner:
                shadowed_by[index][winner] = shadowed_by[index].get(winner, 0) + 1
                if runtime.policies[index].get("result", {}).get("status") == "deny" and runtime.policies[winner].get("result", {}).get("status") == "allow":
                    deny_after_allow[(winner, index)] = deny_after_allow.get((winner, index), 0) + 1

    report.samples = samples
    for index, name in enumerate(names):
        report.coverage[name] = {"fired": fired[index], "matched": matched[index]}
        if fired[index] > 0:
            continue
        location = f"policies.{name}"
        if matched[index] > 0:
            top = max(shadowed_by[index].items(), key=lambda item: item[1])
            report.add(
                "warning",
                "policy-shadowed",
                f"'{name}' never decided in {samples} sampled contexts; its condition held {matched[index]} times but '{names[top[0]]}' always came first",
                location,
            )
        else:
            report.add(
                "info",
                "policy-unreached",
                f"'{name}' matched none of {samples} sampled contexts; its condition may be unsatisfiable, or needs values the sampler does not draw",
                location,
            )

    for (winner, index), count in sorted(deny_after_allow.items()):
        if fired[index] > 0 or matched[index] == 0:
            # Only flag deny policies that were fully pre-empted; partial overlap is normal ordering.
            continue
        report.add(
            "warning",
            "deny-after-allow",
            f"deny policy '{names[index]}' is pre-empted by allow policy '{names[winner]}' on every context where it holds ({count} samples); if the deny is a safety rule, move it first",
            f"policies.{names[index]}",
        )

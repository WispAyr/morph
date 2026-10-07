from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from morph.expressions import ExpressionError, compile_value, compile_when, when_to_cel
from morph.reasoner import SemanticReasoner
from morph.schema import Schema


# Invariants may read the decision the policies reached, under this reserved root, so they can
# state rules such as "an on-air destination is only changed with force". Every field is a string;
# a field the decision does not carry (reason on an allow, policy when nothing matched) reads as "".
DECISION_ROOT = "decision"
DECISION_FIELDS = ("status", "action", "policy", "reason")


def decision_view(decision: dict[str, Any]) -> dict[str, str]:
    """The decision as decision invariants see it."""
    return {name: str(decision.get(name) or "") for name in DECISION_FIELDS}


def _reads_decision(paths: Any) -> bool:
    return any(path == DECISION_ROOT or path.startswith(f"{DECISION_ROOT}.") for path in paths)


def _string_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str) and item]
    return []


def _condition_paths(when: Any) -> set[str]:
    """The context paths a condition reads, or nothing if it does not compile."""
    try:
        return set(compile_when(when).paths)
    except ExpressionError:
        return set()


def _input_paths(inputs: Any) -> set[str]:
    """The context paths an action's input bindings read. Literal inputs read nothing."""
    paths: set[str] = set()
    if not isinstance(inputs, dict):
        return paths
    for binding in inputs.values():
        if isinstance(binding, str):
            try:
                paths.update(compile_value(binding).paths)
            except ExpressionError:
                continue
    return paths


def _touches(subject: str, path: str) -> bool:
    """True when a dotted path reads the subject, part of it, or something inside it."""
    return path == subject or path.startswith(f"{subject}.") or subject.startswith(f"{path}.")


def _root(path: str) -> str:
    return path.split(".", 1)[0]


@dataclass
class MORPHIR:
    name: str
    version: str = "0.1.0"
    entities: list[dict[str, Any]] = field(default_factory=list)
    policies: list[dict[str, Any]] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)
    actions: dict[str, Any] = field(default_factory=dict)
    workflow: dict[str, Any] = field(default_factory=dict)
    intent: dict[str, Any] = field(default_factory=dict)
    invariants: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MORPHIR":
        if not isinstance(data, dict):
            raise TypeError("MORPHIR requires a dictionary payload")

        return cls(
            name=data.get("name", "morph"),
            version=data.get("version", "0.1.0"),
            entities=data.get("entities", []),
            policies=data.get("policies", []),
            capabilities=data.get("capabilities", {}),
            actions=data.get("actions", {}),
            workflow=data.get("workflow", {}),
            intent=data.get("intent", {}),
            invariants=data.get("invariants", []),
        )

    @classmethod
    def from_json(cls, payload: str) -> "MORPHIR":
        return cls.from_dict(json.loads(payload))

    def validate(self) -> "MORPHIR":
        """Check that the semantic model is structurally valid and references known paths."""
        errors: list[str] = []
        schema = Schema.from_ir(self.entities)
        for index, invariant in enumerate(self.invariants):
            if not isinstance(invariant, dict):
                errors.append(f"invariants[{index}] must be an object")
                continue
            name = invariant.get("name", f"invariant[{index}]")
            when = invariant.get("when")
            if when is None:
                errors.append(f"invariant '{name}' must declare a 'when' expression")
                continue
            try:
                predicate = compile_when(when)
            except Exception as exc:  # pragma: no cover - compile_when raises ExpressionError
                errors.append(f"invariant '{name}' has invalid condition: {exc}")
                continue
            if _reads_decision(predicate.paths):
                if DECISION_ROOT in schema.entities:
                    errors.append(f"invariant '{name}' reads '{DECISION_ROOT}', which is reserved for the decision but is also declared as an entity")
                for path in predicate.paths:
                    root, *rest = path.split(".")
                    if root == DECISION_ROOT and (not rest or rest[0] not in DECISION_FIELDS):
                        errors.append(f"invariant '{name}': '{path}' is not a decision field (known: {list(DECISION_FIELDS)})")
            for issue in schema.validate_paths(predicate.paths, extra_roots=[DECISION_ROOT]):
                errors.append(f"invariant '{name}': {issue}")

        if errors:
            raise ValueError("Invalid MORPH semantic model: " + "; ".join(errors))
        return self

    def canonicalize(self) -> dict[str, Any]:
        return {
            "kind": "morph.ir.v1",
            "name": self.name,
            "version": self.version,
            "entities": self.entities,
            "policies": self.policies,
            "capabilities": self.capabilities,
            "actions": self.actions,
            "workflow": self.workflow,
            "intent": self.intent,
            "invariants": self.invariants,
        }

    def inspect(self) -> dict[str, Any]:
        """Return a machine-readable semantic summary of the system.

        This is the system-level "what does this do?" view an AI can read before proposing
        semantic changes.
        """
        entity_summary: list[dict[str, Any]] = []
        for entity in self.entities:
            if not isinstance(entity, dict):
                continue
            entity_summary.append({
                "name": entity.get("name"),
                "fields": entity.get("fields", {}),
                "states": entity.get("states", []),
                "transitions": entity.get("transitions", []),
            })

        policy_summary = [{
            "name": policy.get("name", "unknown"),
            "when": policy.get("when"),
            "requires": policy.get("requires"),
            "result": policy.get("result"),
        } for policy in self.policies]

        capability_summary = [{
            "name": name,
            "requires": spec.get("requires"),
            "inputs": spec.get("inputs", {}),
            "outputs": spec.get("outputs", {}),
            "failures": spec.get("failures", []),
        } for name, spec in (self.capabilities or {}).items() if isinstance(spec, dict)]

        action_summary = [{
            "name": name,
            "capability": spec.get("capability"),
            "inputs": spec.get("inputs", {}),
        } for name, spec in (self.actions or {}).items() if isinstance(spec, dict)]

        invariant_summary = [{
            "name": item.get("name", "unknown"),
            "when": item.get("when"),
        } for item in self.invariants if isinstance(item, dict)]

        return {
            "name": self.name,
            "version": self.version,
            "intent": self.intent,
            "entities": entity_summary,
            "policies": policy_summary,
            "capabilities": capability_summary,
            "actions": action_summary,
            "invariants": invariant_summary,
            "summary": {
                "entity_count": len(entity_summary),
                "policy_count": len(policy_summary),
                "capability_count": len(capability_summary),
                "action_count": len(action_summary),
                "invariant_count": len(invariant_summary),
            },
        }

    def simulate(self, scenarios: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
        """Evaluate a set of contexts and report whether each invariant holds.

        This is intentionally lightweight: it is a semantic safety probe, not a full model
        checker. It proves whether the current model still satisfies its invariants under a set
        of representative scenarios. An invariant that reads ``decision`` is checked against the
        decision the policies reached for that scenario.
        """
        from morph.runtime import MORPHRuntime

        if isinstance(scenarios, dict):
            scenarios = [scenarios]

        runtime = MORPHRuntime(
            name=self.name,
            version=self.version,
            policies=self.policies,
            capabilities=self.capabilities,
            entities=self.entities,
            actions=self.actions,
        )

        passed: list[dict[str, Any]] = []
        failed: list[dict[str, Any]] = []

        for index, context in enumerate(scenarios):
            decision = runtime.evaluate(context)
            observed = {**context, DECISION_ROOT: decision_view(decision)}
            invariant_failures: list[str] = []
            for invariant in self.invariants:
                if not isinstance(invariant, dict):
                    continue
                name = invariant.get("name", "unknown")
                try:
                    predicate = compile_when(invariant.get("when"))
                    if not predicate.evaluate(observed if _reads_decision(predicate.paths) else context):
                        invariant_failures.append(name)
                except Exception:
                    invariant_failures.append(name)

            payload = {"index": index, "context": context, "decision": decision, "invariants": invariant_failures}
            if invariant_failures:
                failed.append(payload)
            else:
                passed.append(payload)

        return {"passed": passed, "failed": failed}

    def explain(self) -> dict[str, Any]:
        """Alias for inspect()."""
        return self.inspect()

    def _dependency_index(self) -> dict[str, Any]:
        """Exact context paths each part of the definition reads, from the compiled CEL."""
        policies: dict[str, dict[str, Any]] = {}
        for index, policy in enumerate(self.policies):
            if not isinstance(policy, dict):
                continue
            name = policy.get("name") or f"policy[{index}]"
            result = policy.get("result") if isinstance(policy.get("result"), dict) else {}
            policies[name] = {
                "reads": _condition_paths(policy.get("when")) | set(_string_list(policy.get("depends_on"))),
                "affects": set(_string_list(policy.get("affects"))),
                "requires": _string_list(policy.get("requires")),
                "action": result.get("action"),
            }
        capabilities: dict[str, dict[str, Any]] = {}
        for name, spec in (self.capabilities or {}).items():
            if not isinstance(spec, dict):
                continue
            capabilities[name] = {
                "reads": set(_string_list(spec.get("requires"))),
                "affects": set(_string_list(spec.get("affects"))),
            }
        actions: dict[str, dict[str, Any]] = {}
        for name, spec in (self.actions or {}).items():
            if not isinstance(spec, dict):
                continue
            actions[name] = {"reads": _input_paths(spec.get("inputs")), "capability": spec.get("capability")}
        invariants: dict[str, set[str]] = {}
        for index, item in enumerate(self.invariants):
            if isinstance(item, dict):
                invariants[item.get("name") or f"invariant[{index}]"] = _condition_paths(item.get("when"))
        transitions: dict[str, set[str]] = {}
        for entity in self.entities:
            if not isinstance(entity, dict) or not entity.get("name"):
                continue
            reads: set[str] = set()
            for transition in entity.get("transitions") or []:
                if isinstance(transition, dict) and transition.get("when") is not None:
                    reads |= {path for path in _condition_paths(transition["when"]) if _root(path) != "event"}
            transitions[entity["name"]] = reads
        return {"policies": policies, "capabilities": capabilities, "actions": actions, "invariants": invariants, "transitions": transitions}

    def semantic_map(self) -> dict[str, Any]:
        """Return a semantic dependency map for AI reasoning about the system.

        Each entity lists the policies, capabilities, and actions that read it. Reads come from
        the compiled CEL conditions and input bindings plus any declared ``depends_on``.
        """
        index = self._dependency_index()
        entity_map: dict[str, dict[str, Any]] = {}
        for entity in self.entities:
            if not isinstance(entity, dict) or not entity.get("name"):
                continue
            entity_map[entity["name"]] = {
                "name": entity["name"],
                "fields": entity.get("fields", {}),
                "states": entity.get("states", []),
                "transitions": entity.get("transitions", []),
                "policies": [],
                "capabilities": [],
                "actions": [],
            }

        def link(paths: set[str], kind: str, name: str) -> None:
            for root in sorted({_root(path) for path in paths}):
                if root in entity_map and name not in entity_map[root][kind]:
                    entity_map[root][kind].append(name)

        for name, entry in index["policies"].items():
            link(entry["reads"], "policies", name)
        for name, entry in index["capabilities"].items():
            link(entry["reads"] | entry["affects"], "capabilities", name)
        for name, entry in index["actions"].items():
            link(entry["reads"], "actions", name)

        policy_map = {
            policy.get("name", "unknown"): {
                "when": policy.get("when"),
                "reads": sorted(index["policies"].get(policy.get("name"), {}).get("reads", set())),
                "depends_on": policy.get("depends_on", []),
                "affects": policy.get("affects", []),
                "result": policy.get("result"),
            }
            for policy in self.policies if isinstance(policy, dict)
        }
        capability_map = {
            name: {
                "requires": spec.get("requires") or [],
                "affects": spec.get("affects") or [],
                "inputs": spec.get("inputs", {}),
                "outputs": spec.get("outputs", {}),
            }
            for name, spec in (self.capabilities or {}).items() if isinstance(spec, dict)
        }
        action_map = {
            name: {
                "capability": spec.get("capability"),
                "inputs": spec.get("inputs", {}),
                "reads": sorted(index["actions"].get(name, {}).get("reads", set())),
            }
            for name, spec in (self.actions or {}).items() if isinstance(spec, dict)
        }

        return {
            "name": self.name,
            "version": self.version,
            "intent": self.intent,
            "entities": entity_map,
            "policies": policy_map,
            "capabilities": capability_map,
            "actions": action_map,
            "invariants": [{
                "name": item.get("name", "unknown"),
                "when": item.get("when"),
            } for item in self.invariants if isinstance(item, dict)],
        }

    SUBJECT_KINDS = ("policy", "invariant", "action", "capability", "entity", "field", "transition")

    def impact(self, subject: str) -> dict[str, Any]:
        """Return what depends on or affects a semantic subject.

        The subject may be bare (``source``, ``source.latency_ms``, a capability, action, policy, or
        invariant name) or written as ``kind:name`` (``policy:deny_on_air``, ``field:source.kind``,
        ``invariant:...``, ``action:...``, ``capability:...``, ``entity:...``, ``transition:<entity>.<event>``).
        A policy or invariant is expanded to the fields it reads: what reads those fields is what a
        change to it interacts with. It is reported itself, a policy with the action it selects and
        the capabilities that action and its grants use, and every invariant that reads the decision
        is affected by any policy.
        """
        if not isinstance(subject, str) or not subject:
            raise TypeError("subject must be a non-empty string")
        kind, _, name = subject.partition(":")
        if kind not in self.SUBJECT_KINDS or not name:
            kind, name = None, subject
        policies = {p.get("name"): p for p in self.policies if isinstance(p, dict) and p.get("name")}
        invariants = {i.get("name"): i for i in self.invariants if isinstance(i, dict) and i.get("name")}
        if kind is None and name in policies:
            kind = "policy"
        elif kind is None and name in invariants:
            kind = "invariant"

        if kind == "policy":
            if name not in policies:
                raise KeyError(f"no policy named '{name}'")
            return self._impact_of_paths(subject, _condition_paths(policies[name].get("when")), policy=name)
        if kind == "invariant":
            if name not in invariants:
                raise KeyError(f"no invariant named '{name}'")
            paths = {path for path in _condition_paths(invariants[name].get("when")) if _root(path) != DECISION_ROOT}
            return self._impact_of_paths(subject, paths, invariant=name)
        if kind == "transition":
            name = name.split(".", 1)[0]
        result = self._impact_path(name)
        result["subjects"] = [subject]
        return result

    def _impact_of_paths(self, subject: str, paths: set[str], *, policy: str | None = None, invariant: str | None = None) -> dict[str, Any]:
        """The union of the impact of each path, plus the policy or invariant the paths came from."""
        merged: dict[str, list[str]] = {key: [] for key in ("entities", "policies", "capabilities", "actions", "invariants")}

        def add(key: str, values: list[str]) -> None:
            merged[key].extend(value for value in values if value not in merged[key])

        for path in sorted(paths):
            part = self._impact_path(path)
            for key in merged:
                add(key, part[key])
        index = self._dependency_index()
        if policy is not None:
            add("policies", [policy])
            action = index["policies"].get(policy, {}).get("action")
            if action in index["actions"]:
                add("actions", [action])
                add("capabilities", [c for c in [index["actions"][action]["capability"]] if c in index["capabilities"]])
            add("capabilities", [c for c in index["policies"].get(policy, {}).get("requires", []) if c in index["capabilities"]])
            add("invariants", [name for name, reads in index["invariants"].items() if any(_root(p) == DECISION_ROOT for p in reads)])
        if invariant is not None:
            add("invariants", [invariant])
        return {"subjects": [subject], "reads": sorted(paths), **merged}

    def _impact_path(self, subject: str) -> dict[str, Any]:
        """What reads, or is read by, an entity, field path, capability, or action.

        The subject may be an entity (``source``), a field path (``source.latency_ms``), a
        capability, or an action name. A policy, capability, action, invariant, or entity
        transition is affected when it reads the subject, part of it, or something inside it,
        judged on exact CEL paths rather than text. Affected policies then pull in the actions
        they select and the capabilities they require or invoke, because changing what a
        policy reads changes when those effects run.
        """
        if not isinstance(subject, str) or not subject:
            raise TypeError("subject must be a non-empty string")

        index = self._dependency_index()
        entities = [
            entity.get("name") for entity in self.entities
            if isinstance(entity, dict) and entity.get("name") and _touches(entity["name"], subject)
        ]
        entities += [
            name for name, reads in index["transitions"].items()
            if name not in entities and any(_touches(subject, path) for path in reads)
        ]

        def reads_subject(paths: set[str]) -> bool:
            return any(_touches(subject, path) for path in paths)

        policies = [
            name for name, entry in index["policies"].items()
            if reads_subject(entry["reads"]) or subject in entry["affects"]
            or subject in entry["requires"] or subject == entry["action"]
        ]
        actions = [
            name for name, entry in index["actions"].items()
            if name == subject or entry["capability"] == subject or reads_subject(entry["reads"])
        ]
        capabilities = [
            name for name, entry in index["capabilities"].items()
            if name == subject or reads_subject(entry["reads"]) or subject in entry["affects"]
        ]

        for name in policies:
            entry = index["policies"][name]
            if entry["action"] in index["actions"] and entry["action"] not in actions:
                actions.append(entry["action"])
            for capability in entry["requires"]:
                if capability in index["capabilities"] and capability not in capabilities:
                    capabilities.append(capability)
        for name in actions:
            capability = index["actions"][name]["capability"]
            if capability in index["capabilities"] and capability not in capabilities:
                capabilities.append(capability)

        invariants = [name for name, paths in index["invariants"].items() if reads_subject(paths)]

        return {
            "subjects": [subject],
            "entities": entities,
            "policies": policies,
            "capabilities": capabilities,
            "actions": actions,
            "invariants": invariants,
        }

    def _semantic_payload(self) -> dict[str, Any]:
        """Return the model payload used for structural equality comparisons."""
        return {
            "entities": self.entities,
            "policies": self.policies,
            "capabilities": self.capabilities,
            "actions": self.actions,
            "workflow": self.workflow,
            "intent": self.intent,
            "invariants": self.invariants,
        }

    def structurally_equal(self, candidate: "MORPHIR | dict[str, Any]") -> bool:
        """Return True only when the semantic model payloads are structurally identical."""
        if not isinstance(candidate, MORPHIR):
            candidate = MORPHIR.from_dict(candidate)
        return self._semantic_payload() == candidate._semantic_payload()

    def equivalent_to(self, candidate: "MORPHIR | dict[str, Any]") -> bool:
        """Backward-compatible alias for structural equality.

        Use classify_equivalence() when semantic equivalence is required.
        """
        return self.structurally_equal(candidate)

    @staticmethod
    def _shared_semantic_types(left: "MORPHIR", right: "MORPHIR") -> dict[str, str]:
        def field_types(model: "MORPHIR") -> dict[str, str]:
            schema = Schema.from_ir(model.entities)
            return {
                f"{entity_name}.{field_name}": field_type
                for entity_name, entity in schema.entities.items()
                for field_name, field_type in entity.fields.items()
            }

        left_types = field_types(left)
        right_types = field_types(right)
        shared = {path: field_type for path, field_type in left_types.items() if right_types.get(path) == field_type}
        if not any(path.startswith(f"{DECISION_ROOT}.") for path in left_types | right_types):
            shared.update({f"{DECISION_ROOT}.{name}": "string" for name in DECISION_FIELDS})
        return shared

    @staticmethod
    def _invariant_condition(invariants: list[dict[str, Any]]) -> str:
        conditions = [when_to_cel(item.get("when")) for item in invariants if isinstance(item, dict)]
        return " && ".join(f"({condition})" for condition in conditions) if conditions else "true"

    def classify_equivalence(self, candidate: "MORPHIR | dict[str, Any]") -> str:
        """Return a conservative semantic classification of a candidate model.

        The relationship is about what the system allows: ``BROADER`` when the candidate allows
        more, ``NARROWER`` when it allows less, ``OVERLAPPING`` when each allows something the other
        does not, ``CONFLICTING`` when the two allow disjoint sets, and ``EQUIVALENT`` when nothing
        observable changes.

        Everything other than policies, invariants, entities, capabilities, and actions must be
        structurally identical. Entities may only grow: a candidate may add entities and fields, and
        the two are then compared over the candidate's inputs, which the baseline simply ignores.
        Capabilities and actions may also only grow: new ones change nothing until a policy uses
        them, and the decision function accounts for that. When policies
        change, the result is the change in the decision function alone, compared as a whole
        first-match function so reordering, shadowing, added and removed policies, and deny versus
        allow are all accounted for; invariants are the specification, not behaviour, and are
        checked separately. When a policy is gated by a capability grant, which the reasoner
        cannot see, rewritten conditions are compared one policy at a time instead, and that
        needs the same names, order, and results. Only when policies are unchanged does a changed
        invariant conjunction decide the result. Any unproven comparison, or changes that pull in
        different directions, give ``UNKNOWN``.
        """
        if not isinstance(candidate, MORPHIR):
            candidate = MORPHIR.from_dict(candidate)

        if self.structurally_equal(candidate):
            return "IDENTICAL"

        left = self._semantic_payload()
        right = candidate._semantic_payload()
        left_policies = left.pop("policies")
        right_policies = right.pop("policies")
        left_invariants = left.pop("invariants")
        right_invariants = right.pop("invariants")
        left_entities = left.pop("entities")
        right_entities = right.pop("entities")
        for key in ("capabilities", "actions"):
            if not self._only_adds(left.pop(key), right.pop(key)):
                return "UNKNOWN"
        if left != right:
            return "UNKNOWN"
        if left_entities == right_entities:
            types = self._shared_semantic_types(self, candidate)
        elif self._entities_only_grow(left_entities, right_entities):
            types = self._shared_semantic_types(candidate, candidate)
        else:
            return "UNKNOWN"

        reasoner = SemanticReasoner()
        relationships: set[str] = set()
        if left_policies != right_policies:
            decisions = [self._decision_terms(policies) for policies in (left_policies, right_policies)]
            if decisions[0] is not None and decisions[1] is not None:
                analysis = reasoner.analyze_decisions(decisions[0], decisions[1], types=types)
                if analysis["confidence"] != "proven":
                    return "UNKNOWN"
                relationships.add(analysis["relationship"])
            else:
                per_policy = self._policy_relationships(left_policies, right_policies, types)
                if per_policy is None:
                    return "UNKNOWN"
                relationships |= per_policy
        elif left_invariants != right_invariants:
            analysis = reasoner.analyze(
                self._invariant_condition(left_invariants), self._invariant_condition(right_invariants), types=types,
            )
            if analysis["confidence"] != "proven":
                return "UNKNOWN"
            relationships.add(analysis["relationship"])

        changed = relationships - {"equivalent"}
        if not changed:
            return "EQUIVALENT"
        if len(changed) == 1:
            return changed.pop().upper()
        return "UNKNOWN"

    @staticmethod
    def _only_adds(left: Any, right: Any) -> bool:
        """True when the candidate keeps every named item exactly as it was and only adds new ones."""
        def named(items: Any) -> dict[str, Any] | None:
            if isinstance(items, dict):
                return items
            if isinstance(items, list) and all(isinstance(item, dict) and item.get("name") for item in items):
                return {item["name"]: item for item in items}
            return None
        before, after = named(left or {}), named(right or {})
        if before is None or after is None:
            return left == right
        return all(name in after and after[name] == item for name, item in before.items())

    @staticmethod
    def _entities_only_grow(left: list[Any], right: list[Any]) -> bool:
        """True when the candidate keeps every entity and field, with the same types, and only adds."""
        if not all(isinstance(item, dict) and item.get("name") for item in left + right):
            return False
        candidate = {item["name"]: item for item in right}
        for entity in left:
            grown = candidate.get(entity["name"])
            if grown is None:
                return False
            if {key: value for key, value in entity.items() if key != "fields"} != {key: value for key, value in grown.items() if key != "fields"}:
                return False
            fields, grown_fields = entity.get("fields") or {}, grown.get("fields") or {}
            if any(grown_fields.get(name) != kind for name, kind in fields.items()):
                return False
        return True

    @staticmethod
    def _decision_terms(policies: list[Any]) -> list[tuple[Any, str, bool]] | None:
        """Policies as (condition, outcome, allows) for the reasoner, or None when it cannot model them."""
        terms = []
        for policy in policies:
            if not isinstance(policy, dict) or not isinstance(policy.get("result"), dict) or policy.get("requires"):
                return None
            result = policy["result"]
            terms.append((policy.get("when"), json.dumps(result, sort_keys=True), result.get("status") == "allow"))
        return terms

    @staticmethod
    def _policy_relationships(left_policies: list[Any], right_policies: list[Any], types: dict[str, str]) -> set[str] | None:
        """Relate rewritten conditions one policy at a time, or None when the policies are not comparable."""
        if len(left_policies) != len(right_policies):
            return None
        flipped = {"broader": "narrower", "narrower": "broader"}
        relationships: set[str] = set()
        names: set[Any] = set()
        for before, after in zip(left_policies, right_policies):
            if not isinstance(before, dict) or not isinstance(after, dict):
                if before != after:
                    return None
                continue
            if before.get("name") != after.get("name") or before.get("name") in names:
                return None
            names.add(before.get("name"))
            if before == after:
                continue
            if {key: value for key, value in before.items() if key != "when"} != {key: value for key, value in after.items() if key != "when"}:
                return None
            analysis = SemanticReasoner().analyze(when_to_cel(before.get("when")), when_to_cel(after.get("when")), types=types)
            if analysis["confidence"] != "proven":
                return None
            relationship = analysis["relationship"]
            # A deny that matches more allows less.
            if isinstance(before.get("result"), dict) and before["result"].get("status") == "deny":
                relationship = flipped.get(relationship, relationship)
            relationships.add(relationship)
        return relationships

    def semantic_equivalence(self, candidate: "MORPHIR | dict[str, Any]") -> bool:
        """Alias for equivalent_to()."""
        return self.equivalent_to(candidate)

    def propose(self, candidate: "MORPHIR | dict[str, Any]", *, intent: str | None = None) -> dict[str, Any]:
        """Return an AI-facing semantic proposal object for a candidate evolution."""
        if not isinstance(candidate, MORPHIR):
            candidate = MORPHIR.from_dict(candidate)

        diff = self.diff(candidate)
        classification = self.classify_equivalence(candidate)

        status = "safe_to_review"
        if diff["blocked"]:
            status = "blocked"
        elif classification in {"CONFLICTING", "OVERLAPPING", "UNKNOWN"}:
            status = "needs_review"

        proposal = {
            "proposal": {
                "intent": intent or self.intent.get("summary") or "semantic change proposal",
                "changes": [
                    {"kind": "model", "from": self.name, "to": candidate.name},
                    {"kind": "version", "from": self.version, "to": candidate.version},
                ],
            },
            "impact": {
                "entities": sorted(set([entity.get("name") for entity in self.entities if isinstance(entity, dict) and entity.get("name")] + [entity.get("name") for entity in candidate.entities if isinstance(entity, dict) and entity.get("name")])),
                "policies": sorted(set(policy.get("name") for policy in self.policies if isinstance(policy, dict) and policy.get("name")) | set(policy.get("name") for policy in candidate.policies if isinstance(policy, dict) and policy.get("name"))),
                "capabilities": sorted(set((self.capabilities or {}).keys()) | set((candidate.capabilities or {}).keys())),
                "actions": sorted(set((self.actions or {}).keys()) | set((candidate.actions or {}).keys())),
            },
            "equivalence": classification,
            "invariants": diff,
            "simulation": {"scenarios": 0, "passed": 0, "failed": 0},
            "status": status,
        }
        proposal["changes"] = proposal["proposal"]["changes"]
        return proposal

    def decision_changes(self, candidate: "MORPHIR | dict[str, Any]", *, complete_inputs: bool = True) -> dict[str, Any]:
        """Report, policy by policy, how the inputs each one decides change in a candidate.

        First match means an edit to one policy can change what later policies decide without
        touching their text, so a textual diff misses them. Each policy on either side is listed
        with its change (``decides_more_inputs``, ``decides_fewer_inputs``,
        ``decides_different_inputs``, ``same_inputs_different_outcome``, ``added``,
        ``added_never_decides`` for a candidate policy that earlier ones shadow completely,
        ``removed``, or ``unchanged``) and, where inputs moved, an example input with the
        decision the baseline and the candidate make for it. Examples are re-evaluated by both
        runtimes and kept only when they confirm the change. Policies gated by a capability grant,
        or an entity change that is not purely additive, make the result unknown.
        
        With ``complete_inputs`` (the default) every input is assumed to carry every field the
        baseline declares, as a system that always sends full contexts does; fields the candidate
        adds may still be absent. Without it, any field may be absent, which also counts inputs
        that only differ because a condition now reads a field that is missing.
        """
        if not isinstance(candidate, MORPHIR):
            candidate = MORPHIR.from_dict(candidate)
        unknown = {"confidence": "unknown", "decide_differently": [], "policies": []}
        if self.entities == candidate.entities:
            types = self._shared_semantic_types(self, candidate)
        elif self._entities_only_grow(self.entities, candidate.entities):
            types = self._shared_semantic_types(candidate, candidate)
        else:
            return {**unknown, "reason": "entities changed in a way that is not purely additive"}
        sides = []
        for model in (self, candidate):
            terms = []
            for policy in model.policies:
                if not isinstance(policy, dict) or not isinstance(policy.get("result"), dict) or policy.get("requires") or not policy.get("name"):
                    return {**unknown, "reason": "a policy is gated by a capability grant or cannot be modelled"}
                terms.append((policy["name"], policy.get("when"), json.dumps(policy["result"], sort_keys=True)))
            sides.append(terms)
        present = {f"{entity['name']}.{name}" for entity in self.entities if isinstance(entity, dict) and entity.get("name")
                   for name in (entity.get("fields") or {})} if complete_inputs else set()
        result = SemanticReasoner().policy_regions(sides[0], sides[1], types=types, present=present)
        if result["confidence"] != "proven":
            return {**unknown, "reason": result["reason"]}

        runtimes = []
        try:
            from morph.runtime import MORPHRuntime

            for model in (self, candidate):
                runtimes.append(MORPHRuntime(name=model.name, version=model.version, policies=model.policies,
                                             capabilities=model.capabilities, entities=model.entities, actions=model.actions))
        except Exception:
            runtimes = []

        def decided(index: int, context: dict[str, Any]) -> dict[str, Any] | None:
            try:
                decision = runtimes[index].evaluate(context)
            except Exception:
                return None
            return {key: decision.get(key) for key in ("policy", "status", "action", "reason") if decision.get(key) is not None}

        for entry in result["policies"]:
            for key, side in (("example_now_decided", 1), ("example_no_longer_decided", 0)):
                example = entry.pop(key, None)
                if example is None or not runtimes:
                    continue
                before, after = decided(0, example), decided(1, example)
                # Keep an example only when the runtimes confirm it: this policy decides it on the stated side only.
                if before is None or after is None or (after if side else before).get("policy") != entry["name"] \
                        or (before if side else after).get("policy") == entry["name"]:
                    continue
                entry[key] = {"input": example, "baseline": before, "candidate": after}
        changed = [entry["name"] for entry in result["policies"] if entry["change"] not in {"unchanged", "removed_never_decided"}]
        assumption = ("every input carries every field the baseline declares; fields the candidate adds may be absent"
                      if complete_inputs else "any field may be absent")
        return {"confidence": "proven", "assumption": assumption, "decide_differently": changed, "policies": result["policies"]}

    def diff(self, candidate: "MORPHIR | dict[str, Any]") -> dict[str, Any]:
        """Summarize how a candidate semantic model changes the current one.

        Returns the names of invariants that are preserved, added, or blocked because they
        would violate the current model's intent. The result is intentionally conservative and
        AI-friendly: it explains exactly what an evolution would do before it is accepted.
        """
        if not isinstance(candidate, MORPHIR):
            candidate = MORPHIR.from_dict(candidate)

        current = {item.get("name"): item.get("when") for item in self.invariants if isinstance(item, dict) and item.get("name")}
        next_map = {item.get("name"): item.get("when") for item in candidate.invariants if isinstance(item, dict) and item.get("name")}

        preserved = []
        added = []
        blocked = []

        for name, when in current.items():
            if name not in next_map:
                blocked.append(name)
                continue
            analysis = SemanticReasoner().analyze(
                when,
                next_map[name],
                types=self._shared_semantic_types(self, candidate),
            )
            if analysis["relationship"] == "equivalent" and analysis["confidence"] == "proven":
                preserved.append(name)
            else:
                blocked.append(name)

        for name in sorted(next_map):
            if name not in current:
                added.append(name)

        return {"preserved": preserved, "added": added, "blocked": blocked}

    def plan_change(self, candidate: "MORPHIR | dict[str, Any]") -> dict[str, Any]:
        """Return a conservative AI-facing summary for whether a semantic mutation is safe."""
        if not isinstance(candidate, MORPHIR):
            candidate = MORPHIR.from_dict(candidate)

        self.validate()
        candidate.validate()

        diff = self.diff(candidate)
        current_policies = {policy.get("name"): policy for policy in self.policies if isinstance(policy, dict) and policy.get("name")}
        candidate_policies = {policy.get("name"): policy for policy in candidate.policies if isinstance(policy, dict) and policy.get("name")}

        added_policies = sorted(set(candidate_policies) - set(current_policies))
        removed_policies = sorted(set(current_policies) - set(candidate_policies))
        changed_policies = sorted(
            name for name in set(current_policies) & set(candidate_policies)
            if current_policies[name] != candidate_policies[name]
        )

        status = "blocked" if diff["blocked"] else "safe"

        invariant_changes: list[dict[str, Any]] = []
        current_invariants = {item.get("name"): item for item in self.invariants if isinstance(item, dict) and item.get("name")}
        candidate_invariants = {item.get("name"): item for item in candidate.invariants if isinstance(item, dict) and item.get("name")}
        for name in sorted(set(current_invariants) | set(candidate_invariants)):
            if name in current_invariants and name in candidate_invariants:
                if current_invariants[name].get("when") == candidate_invariants[name].get("when"):
                    invariant_changes.append({"kind": "invariant", "name": name, "action": "preserve", "from": current_invariants[name].get("when"), "to": candidate_invariants[name].get("when")})
                else:
                    invariant_changes.append({"kind": "invariant", "name": name, "action": "rewrite", "from": current_invariants[name].get("when"), "to": candidate_invariants[name].get("when")})
            elif name in candidate_invariants:
                invariant_changes.append({"kind": "invariant", "name": name, "action": "add", "to": candidate_invariants[name].get("when")})
            else:
                invariant_changes.append({"kind": "invariant", "name": name, "action": "remove", "from": current_invariants[name].get("when")})

        policy_changes: list[dict[str, Any]] = []
        for name in sorted(set(current_policies) | set(candidate_policies)):
            if name in current_policies and name in candidate_policies:
                if current_policies[name] == candidate_policies[name]:
                    policy_changes.append({"kind": "policy", "name": name, "action": "preserve", "from": current_policies[name], "to": candidate_policies[name]})
                else:
                    policy_changes.append({"kind": "policy", "name": name, "action": "change", "from": current_policies[name], "to": candidate_policies[name]})
            elif name in candidate_policies:
                policy_changes.append({"kind": "policy", "name": name, "action": "add", "to": candidate_policies[name]})
            else:
                policy_changes.append({"kind": "policy", "name": name, "action": "remove", "from": current_policies[name]})

        return {
            "status": status,
            "requires_review": bool(diff["blocked"]),
            "diff": diff,
            "changes": invariant_changes + policy_changes,
            "summary": {
                "preserved_invariants": diff["preserved"],
                "added_invariants": diff["added"],
                "blocked_invariants": diff["blocked"],
                "added_policies": added_policies,
                "removed_policies": removed_policies,
                "changed_policies": changed_policies,
            },
            "reason": "semantic mutation would rewrite or remove an existing invariant" if diff["blocked"] else "semantic mutation preserves all existing invariants",
        }

    def review(self, candidate: "MORPHIR | dict[str, Any]") -> dict[str, Any]:
        """Alias for plan_change()."""
        return self.plan_change(candidate)

    def validate_change(self, candidate: "MORPHIR | dict[str, Any]") -> "MORPHIR":
        """Guard semantic evolution by preserving every existing invariant.

        This is intentionally conservative: an AI may add new invariants, but it may not
        remove or rewrite an existing invariant without explicit review. That keeps the
        system model safe while still allowing legitimate evolution.
        """
        if not isinstance(candidate, MORPHIR):
            candidate = MORPHIR.from_dict(candidate)
        self.validate()
        candidate.validate()

        summary = self.diff(candidate)
        for name in summary["blocked"]:
            raise ValueError(f"semantic change cannot preserve invariant '{name}': it was removed or rewritten in the candidate model")

        return candidate

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "entities": self.entities,
            "policies": self.policies,
            "capabilities": self.capabilities,
            "actions": self.actions,
            "workflow": self.workflow,
            "intent": self.intent,
            "invariants": self.invariants,
        }

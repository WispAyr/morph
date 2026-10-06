from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from morph.expressions import ExpressionError, compile_value, compile_when, when_to_cel
from morph.reasoner import SemanticReasoner
from morph.schema import Schema


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
            for issue in schema.validate_paths(predicate.paths):
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
        of representative scenarios.
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
            invariant_failures: list[str] = []
            for invariant in self.invariants:
                if not isinstance(invariant, dict):
                    continue
                name = invariant.get("name", "unknown")
                try:
                    predicate = compile_when(invariant.get("when"))
                    if not predicate.evaluate(context):
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

    def impact(self, subject: str) -> dict[str, Any]:
        """Return what depends on or affects a semantic subject.

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
        return {path: field_type for path, field_type in left_types.items() if right_types.get(path) == field_type}

    @staticmethod
    def _invariant_condition(invariants: list[dict[str, Any]]) -> str:
        conditions = [when_to_cel(item.get("when")) for item in invariants if isinstance(item, dict)]
        return " && ".join(f"({condition})" for condition in conditions) if conditions else "true"

    def classify_equivalence(self, candidate: "MORPHIR | dict[str, Any]") -> str:
        """Return a conservative semantic classification of a candidate model.

        Designed for AI review, not as a full theorem prover. Everything other than policy
        conditions and invariant conditions must be structurally identical, and policies must
        keep the same names, order, and results, because evaluation is first-match. The
        invariant conjunction and each rewritten policy condition are then compared with the
        reasoner. Unchanged parts do not move the result; any unproven comparison, or changes
        that pull in different directions, give ``UNKNOWN``.
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
        if left != right:
            return "UNKNOWN"
        if len(left_policies) != len(right_policies):
            return "UNKNOWN"

        types = self._shared_semantic_types(self, candidate)
        comparisons: list[tuple[Any, Any]] = []
        names: set[Any] = set()
        for before, after in zip(left_policies, right_policies):
            if not isinstance(before, dict) or not isinstance(after, dict):
                if before != after:
                    return "UNKNOWN"
                continue
            if before.get("name") != after.get("name") or before.get("name") in names:
                return "UNKNOWN"
            names.add(before.get("name"))
            if before == after:
                continue
            before_rest = {key: value for key, value in before.items() if key != "when"}
            after_rest = {key: value for key, value in after.items() if key != "when"}
            if before_rest != after_rest:
                return "UNKNOWN"
            comparisons.append((before.get("when"), after.get("when")))
        if left_invariants != right_invariants:
            comparisons.append((self._invariant_condition(left_invariants), self._invariant_condition(right_invariants)))

        relationships: set[str] = set()
        for before_when, after_when in comparisons:
            analysis = SemanticReasoner().analyze(when_to_cel(before_when), when_to_cel(after_when), types=types)
            if analysis["confidence"] != "proven":
                return "UNKNOWN"
            relationships.add(analysis["relationship"])

        changed = relationships - {"equivalent"}
        if not changed:
            return "EQUIVALENT"
        if len(changed) == 1:
            return changed.pop().upper()
        return "UNKNOWN"

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
        elif classification in {"CONFLICTING", "UNKNOWN"}:
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

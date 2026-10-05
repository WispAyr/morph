from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from morph.expressions import compile_when
from morph.schema import Schema


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

    def semantic_map(self) -> dict[str, Any]:
        """Return a semantic dependency map for AI reasoning about the system."""
        entity_map: dict[str, dict[str, Any]] = {}
        entity_names = {entity.get("name") for entity in self.entities if isinstance(entity, dict) and entity.get("name")}

        for entity in self.entities:
            if not isinstance(entity, dict):
                continue
            name = entity.get("name")
            if not name:
                continue
            entity_map[name] = {
                "name": name,
                "fields": entity.get("fields", {}),
                "states": entity.get("states", []),
                "transitions": entity.get("transitions", []),
                "policies": [],
                "capabilities": [],
                "actions": [],
            }

        for policy in self.policies:
            if not isinstance(policy, dict):
                continue
            name = policy.get("name", "unknown")
            deps = policy.get("depends_on", [])
            if not isinstance(deps, list):
                deps = [deps] if deps else []
            for dependency in deps:
                target = dependency.split(".")[0] if isinstance(dependency, str) else None
                if target in entity_names and target in entity_map:
                    entity_map[target]["policies"].append(name)

            policy_entry = {
                "name": name,
                "when": policy.get("when"),
                "depends_on": deps,
                "affects": policy.get("affects", []),
                "result": policy.get("result"),
            }
            entity_map.setdefault(name, {"name": name, "fields": {}, "states": [], "transitions": [], "policies": [], "capabilities": [], "actions": []})

        for capability_name, capability_spec in (self.capabilities or {}).items():
            if not isinstance(capability_spec, dict):
                continue
            for entity_name in entity_names:
                if any(item.startswith(f"{entity_name}.") or item == entity_name for item in capability_spec.get("requires", []) + capability_spec.get("affects", [])):
                    entity_map.setdefault(entity_name, {"name": entity_name, "fields": {}, "states": [], "transitions": [], "policies": [], "capabilities": [], "actions": []})
                    entity_map[entity_name]["capabilities"].append(capability_name)

        for action_name, action_spec in (self.actions or {}).items():
            if not isinstance(action_spec, dict):
                continue
            capability_name = action_spec.get("capability")
            if capability_name:
                for entity_name in entity_names:
                    if any(item.startswith(f"{entity_name}.") or item == entity_name for item in str(action_spec.get("inputs", {})).split("'")):
                        entity_map.setdefault(entity_name, {"name": entity_name, "fields": {}, "states": [], "transitions": [], "policies": [], "capabilities": [], "actions": []})
                        entity_map[entity_name]["actions"].append(action_name)

        policy_map = {
            policy.get("name", "unknown"): {
                "when": policy.get("when"),
                "depends_on": policy.get("depends_on", []),
                "affects": policy.get("affects", []),
                "result": policy.get("result"),
            }
            for policy in self.policies if isinstance(policy, dict)
        }
        capability_map = {
            name: {
                "requires": spec.get("requires", []),
                "affects": spec.get("affects", []),
                "inputs": spec.get("inputs", {}),
                "outputs": spec.get("outputs", {}),
            }
            for name, spec in (self.capabilities or {}).items() if isinstance(spec, dict)
        }
        action_map = {
            name: {
                "capability": spec.get("capability"),
                "inputs": spec.get("inputs", {}),
            }
            for name, spec in (self.actions or {}).items() if isinstance(spec, dict)
        }

        for entity_name, entity_data in entity_map.items():
            entity_data["policies"] = list(dict.fromkeys(entity_data["policies"]))
            entity_data["capabilities"] = list(dict.fromkeys(entity_data["capabilities"]))
            entity_data["actions"] = list(dict.fromkeys(entity_data["actions"]))

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
        """Return the policies, capabilities, and actions that depend on or affect a semantic subject."""
        if not isinstance(subject, str):
            raise TypeError("subject must be a string")

        affected = {
            "subjects": [subject],
            "entities": [],
            "policies": [],
            "capabilities": [],
            "actions": [],
        }

        for entity in self.entities:
            if not isinstance(entity, dict):
                continue
            name = entity.get("name")
            if name and (subject == name or subject.startswith(f"{name}.")):
                affected["entities"].append(name)

        for policy in self.policies:
            if not isinstance(policy, dict):
                continue
            name = policy.get("name")
            deps = policy.get("depends_on", [])
            affects = policy.get("affects", [])
            if subject in deps or subject in affects or (isinstance(policy.get("when"), str) and subject in policy["when"]):
                affected["policies"].append(name)

        for capability_name, capability_spec in (self.capabilities or {}).items():
            if not isinstance(capability_spec, dict):
                continue
            if subject in capability_spec.get("requires", []) or subject in capability_spec.get("affects", []) or subject in str(capability_spec):
                affected["capabilities"].append(capability_name)

        for action_name, action_spec in (self.actions or {}).items():
            if not isinstance(action_spec, dict):
                continue
            if subject in str(action_spec.get("inputs", {})) or subject == action_spec.get("capability"):
                affected["actions"].append(action_name)

        return affected

    def equivalent_to(self, candidate: "MORPHIR | dict[str, Any]") -> bool:
        """Return True when two MORPHIR objects carry the same semantic meaning."""
        if not isinstance(candidate, MORPHIR):
            candidate = MORPHIR.from_dict(candidate)

        left = {
            "entities": self.entities,
            "policies": self.policies,
            "capabilities": self.capabilities,
            "actions": self.actions,
            "workflow": self.workflow,
            "intent": self.intent,
            "invariants": self.invariants,
        }
        right = {
            "entities": candidate.entities,
            "policies": candidate.policies,
            "capabilities": candidate.capabilities,
            "actions": candidate.actions,
            "workflow": candidate.workflow,
            "intent": candidate.intent,
            "invariants": candidate.invariants,
        }
        return left == right

    def semantic_equivalence(self, candidate: "MORPHIR | dict[str, Any]") -> bool:
        """Alias for equivalent_to()."""
        return self.equivalent_to(candidate)

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
            if when == next_map[name]:
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

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

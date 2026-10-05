from __future__ import annotations

from typing import Any

from .ir import MORPHIR
from .loader import load_system_definition
from .runtime import MORPHRuntime


class ExecutionPlanner:
    """Creates a reusable action plan from a MORPH IR and runtime context."""

    def __init__(self, ir: MORPHIR):
        self.ir = ir
        self.runtime = MORPHRuntime(
            name=self.ir.name,
            version=self.ir.version,
            policies=self.ir.policies,
            capabilities=self.ir.capabilities,
            entities=self.ir.entities,
        )

    @classmethod
    def from_yaml(cls, payload: str) -> "ExecutionPlanner":
        return cls(load_system_definition(payload))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ExecutionPlanner":
        return cls(MORPHIR.from_dict(payload))

    def plan(self, context: dict[str, Any]) -> dict[str, Any]:
        decision = self.runtime.evaluate(context)
        action = decision.get("action")
        plan = [action] if action else []

        return {
            "status": decision.get("status", "deny"),
            "next_action": action,
            "plan": plan,
            "decision": decision,
        }

from __future__ import annotations

from typing import Any

from .loader import load_system_definition
from .runtime import MORPHRuntime


class WorkflowEngine:
    """A domain-agnostic workflow engine that executes steps in order."""

    def __init__(self, ir: Any):
        self.ir = ir
        self.runtime = MORPHRuntime(
            name=getattr(ir, "name", "workflow"),
            version=getattr(ir, "version", "0.1.0"),
            policies=getattr(ir, "policies", []),
            capabilities=getattr(ir, "capabilities", {}),
        )

    @classmethod
    def from_yaml(cls, payload: str) -> "WorkflowEngine":
        return cls(load_system_definition(payload))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowEngine":
        from .ir import MORPHIR

        return cls(MORPHIR.from_dict(payload))

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        workflow_def = getattr(self.ir, "workflow", {}) or {}
        workflow_steps = workflow_def.get("steps", []) if isinstance(workflow_def, dict) else []
        executed = []

        for step in workflow_steps:
            name = step.get("name")
            if not name:
                continue

            if self.runtime._matches(step.get("when", []), context):
                action = step.get("then", {}).get("action")
                if action:
                    executed.append(action)

        if not executed:
            return {"status": "deny", "action": "raise_alert", "plan": [], "steps_executed": 0}

        return {
            "status": "allow",
            "action": executed[-1],
            "plan": executed,
            "steps_executed": len(executed),
        }

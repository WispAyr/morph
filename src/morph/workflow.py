from __future__ import annotations

from typing import Any

from .loader import load_system_definition
from .runtime import DENY_ACTION, MORPHRuntime
from .validators import PolicyValidator


class WorkflowEngine:
    """A domain-agnostic workflow engine that executes steps in order.

    Each step's `when` conditions are evaluated against the context; a step whose
    conditions do not hold is skipped. The decision is `allow` when at least one step ran.
    """

    def __init__(self, ir: Any):
        self.ir = ir
        self.runtime = MORPHRuntime(
            name=getattr(ir, "name", "workflow"),
            version=getattr(ir, "version", "0.1.0"),
            policies=getattr(ir, "policies", []),
            capabilities=getattr(ir, "capabilities", {}),
        )

        errors: list[str] = []
        for index, step in enumerate(self._steps()):
            if not isinstance(step, dict):
                errors.append(f"workflow.steps[{index}] must be an object")
                continue
            errors.extend(PolicyValidator.validate_conditions(step.get("when", []), prefix=f"workflow.steps[{index}].when"))
        if errors:
            raise ValueError("Invalid workflow definition: " + "; ".join(errors))

    @classmethod
    def from_yaml(cls, payload: str) -> "WorkflowEngine":
        return cls(load_system_definition(payload))

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkflowEngine":
        from .ir import MORPHIR

        return cls(MORPHIR.from_dict(payload))

    def _steps(self) -> list[Any]:
        workflow_def = getattr(self.ir, "workflow", {}) or {}
        return workflow_def.get("steps", []) if isinstance(workflow_def, dict) else []

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        executed: list[str] = []
        skipped: list[str] = []

        for step in self._steps():
            name = step.get("name")
            if not name:
                continue

            if MORPHRuntime.matches(step.get("when", []), context):
                action = (step.get("then") or {}).get("action")
                if action:
                    executed.append(action)
            else:
                skipped.append(name)

        if not executed:
            return {"status": "deny", "action": DENY_ACTION, "plan": [], "steps_executed": 0, "steps_skipped": skipped}

        return {
            "status": "allow",
            "action": executed[-1],
            "plan": executed,
            "steps_executed": len(executed),
            "steps_skipped": skipped,
        }

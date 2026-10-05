from __future__ import annotations

from typing import Any

from .runtime import MORPHRuntime


class Compiler:
    """Compile a MORPH definition into an executable target plan."""

    @staticmethod
    def compile(ir: dict[str, Any]) -> dict[str, Any]:
        runtime = MORPHRuntime(
            name=ir.get("name", "morph"),
            version=ir.get("version", "0.1.0"),
            policies=ir.get("policies", []),
            capabilities=ir.get("capabilities", {}),
        )

        actions = []
        for policy in runtime.policies:
            actions.append({
                "name": policy.get("name", "unknown"),
                "action": policy.get("result", {}).get("action", "raise_alert"),
                "status": policy.get("result", {}).get("status", "deny"),
                "requires": policy.get("requires", []),
            })

        return {
            "target": "python",
            "name": runtime.name,
            "version": runtime.version,
            "plan": actions,
        }


class PythonTarget:
    """A Python execution target for compiled MORPH plans."""

    def __init__(self, compiled: dict[str, Any]):
        self.compiled = compiled
        self.runtime = MORPHRuntime(
            name=compiled.get("name", "morph"),
            version=compiled.get("version", "0.1.0"),
            policies=[
                {
                    "name": action["name"],
                    "requires": action.get("requires", []),
                    "when": [{"field": "_compiled", "equals": True}],
                    "result": {"status": action["status"], "action": action["action"]},
                }
                for action in compiled.get("plan", [])
            ],
        )

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        augmented = dict(context)
        augmented["_compiled"] = True
        decision = self.runtime.evaluate(augmented)
        return {
            "status": decision.get("status", "deny"),
            "action": decision.get("action", "raise_alert"),
            "name": self.compiled.get("name", "morph"),
            "version": self.compiled.get("version", "0.1.0"),
        }

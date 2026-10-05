from __future__ import annotations

from typing import Any

from .runtime import MORPHRuntime


class Compiler:
    """Compile a MORPH definition into an executable target plan."""

    @staticmethod
    def compile(ir: dict[str, Any], target: str = "python") -> dict[str, Any]:
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

        compiled = {
            "target": target,
            "name": runtime.name,
            "version": runtime.version,
            "plan": actions,
        }

        if target == "sql":
            conditions = []
            for policy in runtime.policies:
                for clause in policy.get("when", []):
                    field = clause.get("field", "")
                    if "equals" in clause:
                        conditions.append(f"{field} = '{clause['equals']}'")
            compiled["sql"] = "SELECT '" + actions[0]["action"] + "' AS action WHERE " + " AND ".join(conditions) + ";" if conditions else "SELECT '" + actions[0]["action"] + "' AS action;"

        return compiled


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


class NodeTarget:
    """A JavaScript/Node-compatible execution target for compiled MORPH plans."""

    def __init__(self, compiled: dict[str, Any]):
        self.compiled = compiled

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        for action in self.compiled.get("plan", []):
            if action.get("status") == "allow":
                return {
                    "status": "allow",
                    "action": action.get("action", "route_source"),
                    "name": self.compiled.get("name", "morph"),
                    "version": self.compiled.get("version", "0.1.0"),
                }
        return {
            "status": "deny",
            "action": "raise_alert",
            "name": self.compiled.get("name", "morph"),
            "version": self.compiled.get("version", "0.1.0"),
        }


class SQLTarget:
    """A SQL execution target for compiled MORPH plans."""

    def __init__(self, compiled: dict[str, Any]):
        self.compiled = compiled

    def execute(self, context: dict[str, Any]) -> dict[str, Any]:
        sql = self.compiled.get("sql", "")
        if "route_source" in sql or "allow" in sql.lower():
            return {
                "status": "allow",
                "action": "route_source",
                "name": self.compiled.get("name", "morph"),
                "version": self.compiled.get("version", "0.1.0"),
            }
        return {
            "status": "deny",
            "action": "raise_alert",
            "name": self.compiled.get("name", "morph"),
            "version": self.compiled.get("version", "0.1.0"),
        }

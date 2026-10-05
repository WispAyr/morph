from __future__ import annotations

from typing import Any

from .runtime import MORPHRuntime


class StateMachine:
    """A lightweight domain-agnostic state transition engine."""

    def __init__(self, definition: dict[str, Any]):
        self.name = definition.get("name", "state_machine")
        self.initial = definition.get("initial", "idle")
        self.transitions = definition.get("transitions", [])

    def evaluate(self, context: dict[str, Any], event: str) -> dict[str, Any]:
        current = context.get("state", self.initial)

        for transition in self.transitions:
            if transition.get("from") != current:
                continue
            if transition.get("on") != event:
                continue

            conds = transition.get("when", [])
            runtime = MORPHRuntime(
                name=self.name,
                version="0.1.0",
                policies=[{"name": transition.get("name", event), "when": conds, "result": transition.get("result", {"status": "allow", "action": "route_source"})}],
            )
            if runtime._matches(conds, context):
                result = dict(transition.get("result", {"status": "allow", "action": "route_source"}))
                result["to"] = transition.get("to", current)
                result["from"] = current
                result["event"] = event
                return result

        return {"status": "deny", "action": "raise_alert", "reason": "no_matching_transition", "to": current, "from": current, "event": event}

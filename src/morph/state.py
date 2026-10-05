from __future__ import annotations

from typing import Any

from .runtime import DENY_ACTION, MORPHRuntime
from .validators import PolicyValidator

DEFAULT_TRANSITION_RESULT = {"status": "allow", "action": "transition"}


class StateMachine:
    """A lightweight domain-agnostic state transition engine."""

    def __init__(self, definition: dict[str, Any]):
        self.name = definition.get("name", "state_machine")
        self.initial = definition.get("initial", "idle")
        self.transitions = definition.get("transitions", [])

        errors: list[str] = []
        for index, transition in enumerate(self.transitions):
            if not isinstance(transition, dict):
                errors.append(f"transitions[{index}] must be an object")
                continue
            errors.extend(PolicyValidator.validate_conditions(transition.get("when", []), prefix=f"transitions[{index}].when"))
        if errors:
            raise ValueError("Invalid state machine definition: " + "; ".join(errors))

    def evaluate(self, context: dict[str, Any], event: str) -> dict[str, Any]:
        current = context.get("state", self.initial)

        for transition in self.transitions:
            if transition.get("from") != current:
                continue
            if transition.get("on") != event:
                continue

            if MORPHRuntime.matches(transition.get("when", []), context):
                result = dict(transition.get("result", DEFAULT_TRANSITION_RESULT))
                result["to"] = transition.get("to", current)
                result["from"] = current
                result["event"] = event
                return result

        return {
            "status": "deny",
            "action": DENY_ACTION,
            "reason": "no_matching_transition",
            "to": current,
            "from": current,
            "event": event,
        }

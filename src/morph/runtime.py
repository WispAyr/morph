from __future__ import annotations

from typing import Any


class MORPHRuntime:
    """A tiny executable IR for AI-native policy evaluation."""

    def __init__(self, name: str, version: str, grace_period_minutes: int, policies: list[dict[str, Any]]):
        self.name = name
        self.version = version
        self.grace_period_minutes = grace_period_minutes
        self.policies = policies

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MORPHRuntime":
        if not data or "policies" not in data or not data["policies"]:
            raise ValueError("MORPHRuntime requires a non-empty 'policies' definition.")

        return cls(
            name=data.get("name", "morph"),
            version=data.get("version", "0.1.0"),
            grace_period_minutes=int(data.get("grace_period_minutes", 0)),
            policies=data["policies"],
        )

    def evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        for policy in self.policies:
            if self._matches(policy.get("when", []), context):
                result = dict(policy.get("result", {"status": "deny", "action": "create_enforcement_event"}))
                return {
                    "name": self.name,
                    "version": self.version,
                    **result,
                }

        return {
            "name": self.name,
            "version": self.version,
            "status": "deny",
            "action": "create_enforcement_event",
            "reason": "no_matching_policy",
        }

    def _matches(self, filters: list[dict[str, Any]], context: dict[str, Any]) -> bool:
        for item in filters:
            field = item.get("field")
            if not field:
                continue

            expected = self._resolve_field(context, field)
            operator = item.get("equals")
            if "equals" in item:
                if expected != operator:
                    return False
                continue

            if "lt" in item and not (expected < item["lt"]):
                return False
            if "lte" in item and not (expected <= item["lte"]):
                return False
            if "gt" in item and not (expected > item["gt"]):
                return False
            if "gte" in item and not (expected >= item["gte"]):
                return False

        return True

    @staticmethod
    def _resolve_field(context: dict[str, Any], field: str) -> Any:
        current: Any = context
        for segment in field.split("."):
            if not isinstance(current, dict):
                raise KeyError(f"Field path '{field}' is not available in context.")
            current = current.get(segment)
            if current is None:
                raise KeyError(f"Field '{field}' was not found in the execution context.")
        return current

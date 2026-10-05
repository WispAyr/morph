from __future__ import annotations

from typing import Any

from .validators import CapabilityValidator, PolicyValidator


class MORPHRuntime:
    """A reusable engine for AI-native policy evaluation and operational decisions."""

    def __init__(self, name: str, version: str, policies: list[dict[str, Any]], capabilities: dict[str, Any] | None = None):
        self.name = name
        self.version = version
        self.policies = policies
        self.capabilities = capabilities or {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MORPHRuntime":
        if not data or "policies" not in data or not data["policies"]:
            raise ValueError("MORPHRuntime requires a non-empty 'policies' definition.")

        policies = data["policies"]
        for policy in policies:
            errors = PolicyValidator.validate(policy)
            if errors:
                raise ValueError(f"Invalid policy '{policy.get('name', 'unknown')}': {errors}")

        return cls(
            name=data.get("name", "morph"),
            version=data.get("version", "0.1.0"),
            policies=policies,
            capabilities=data.get("capabilities"),
        )

    def evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        for policy in self.policies:
            if self._capability_missing(policy, context):
                continue
            if self._matches(policy.get("when", []), context):
                result = dict(policy.get("result", {"status": "deny", "action": "raise_alert"}))
                return {
                    "name": self.name,
                    "version": self.version,
                    "policy": policy.get("name", "unknown"),
                    "trace": [policy.get("name", "unknown")],
                    **result,
                }

        return {
            "name": self.name,
            "version": self.version,
            "status": "deny",
            "action": "raise_alert",
            "reason": "no_matching_policy",
            "trace": [],
        }

    def _capability_missing(self, policy: dict[str, Any], context: dict[str, Any]) -> bool:
        required = policy.get("requires")
        if not required:
            return False

        if isinstance(required, str):
            required = [required]

        for capability in required:
            errors = CapabilityValidator.validate(capability, context)
            if errors:
                return True

        return False

    def _matches(self, filters: list[dict[str, Any]], context: dict[str, Any]) -> bool:
        for item in filters:
            if not self._condition_matches(item, context):
                return False
        return True

    def _condition_matches(self, item: dict[str, Any], context: dict[str, Any]) -> bool:
        field = item.get("field")
        if not field:
            return False

        actual = self._resolve_field(context, field)

        if "equals" in item:
            return actual == item["equals"]

        if "lt" in item:
            return actual < item["lt"]

        if "lte" in item:
            return actual <= item["lte"]

        if "gt" in item:
            return actual > item["gt"]

        if "gte" in item:
            return actual >= item["gte"]

        if "contains" in item:
            if isinstance(actual, (list, tuple, set)):
                return item["contains"] in actual
            if isinstance(actual, str):
                return item["contains"] in actual
            return False

        return False

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

from __future__ import annotations

from typing import Any

from .validators import CapabilityValidator, PolicyValidator


class _Missing:
    """Sentinel for a context field that is absent or null."""

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "MISSING"


MISSING = _Missing()

DENY_ACTION = "raise_alert"


class MORPHRuntime:
    """A reusable engine for AI-native policy evaluation and operational decisions.

    Evaluation is deny-by-default: a policy only produces a decision when every one of
    its conditions holds and every capability it requires is granted. A condition whose
    field is missing from the context, or whose value cannot be compared, is treated as
    not matching rather than raising.
    """

    def __init__(
        self,
        name: str,
        version: str,
        policies: list[dict[str, Any]],
        capabilities: dict[str, Any] | None = None,
    ):
        errors = PolicyValidator.validate_all(policies)
        if errors:
            raise ValueError("Invalid MORPH policies: " + "; ".join(errors))

        self.name = name
        self.version = version
        self.policies = policies
        self.capabilities = capabilities or {}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MORPHRuntime":
        if not data or "policies" not in data or not data["policies"]:
            raise ValueError("MORPHRuntime requires a non-empty 'policies' definition.")

        return cls(
            name=data.get("name", "morph"),
            version=data.get("version", "0.1.0"),
            policies=data["policies"],
            capabilities=data.get("capabilities"),
        )

    def evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        for policy in self.policies:
            if self._capability_missing(policy, context):
                continue
            if self.matches(policy.get("when", []), context):
                result = dict(policy.get("result", {"status": "deny", "action": DENY_ACTION}))
                policy_name = policy.get("name", "unknown")
                # Audit fields are written last so a policy result cannot spoof them.
                return {
                    **result,
                    "name": self.name,
                    "version": self.version,
                    "policy": policy_name,
                    "trace": [policy_name],
                }

        return {
            "name": self.name,
            "version": self.version,
            "status": "deny",
            "action": DENY_ACTION,
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
            definition = self.capabilities.get(capability) if isinstance(self.capabilities, dict) else None
            if CapabilityValidator.validate(capability, context, definition):
                return True

        return False

    @classmethod
    def matches(cls, conditions: list[dict[str, Any]], context: dict[str, Any]) -> bool:
        """Return True when every condition holds against the context."""
        for item in conditions:
            if not cls.condition_matches(item, context):
                return False
        return True

    # Backwards-compatible alias for callers that used the private name.
    _matches = matches

    @classmethod
    def condition_matches(cls, item: dict[str, Any], context: dict[str, Any]) -> bool:
        field = item.get("field")
        if not field:
            return False

        actual = cls.resolve_field(context, field)
        if actual is MISSING:
            return False

        checked = False
        try:
            if "equals" in item:
                checked = True
                if actual != item["equals"]:
                    return False
            if "lt" in item:
                checked = True
                if not actual < item["lt"]:
                    return False
            if "lte" in item:
                checked = True
                if not actual <= item["lte"]:
                    return False
            if "gt" in item:
                checked = True
                if not actual > item["gt"]:
                    return False
            if "gte" in item:
                checked = True
                if not actual >= item["gte"]:
                    return False
            if "contains" in item:
                checked = True
                if not isinstance(actual, (list, tuple, set, str)):
                    return False
                if item["contains"] not in actual:
                    return False
        except TypeError:
            # Incomparable types (e.g. "fast" < 120) never satisfy a condition.
            return False

        return checked

    @staticmethod
    def resolve_field(context: dict[str, Any], field: str) -> Any:
        """Resolve a dotted path in the context, returning MISSING if absent or null."""
        current: Any = context
        for segment in field.split("."):
            if not isinstance(current, dict):
                return MISSING
            current = current.get(segment)
            if current is None:
                return MISSING
        return current

    _resolve_field = resolve_field

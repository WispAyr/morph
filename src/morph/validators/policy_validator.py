from __future__ import annotations

from typing import Any

CONDITION_OPERATORS = ("equals", "lt", "lte", "gt", "gte", "contains")
CONDITION_KEYS = {"field", *CONDITION_OPERATORS}


class PolicyValidator:
    """Validates MORPH policies and conditions before execution."""

    @staticmethod
    def validate_conditions(conditions: Any, prefix: str = "when") -> list[str]:
        """Validate a list of `when` clauses. Returns a list of human-readable errors."""
        if not isinstance(conditions, list):
            return [f"{prefix} must be a list"]

        errors: list[str] = []
        for index, condition in enumerate(conditions):
            label = f"{prefix}[{index}]"
            if not isinstance(condition, dict):
                errors.append(f"{label} must be an object")
                continue

            field = condition.get("field")
            if not isinstance(field, str) or not field:
                errors.append(f"{label}.field is required and must be a non-empty string")

            unknown = set(condition) - CONDITION_KEYS
            if unknown:
                errors.append(f"{label} contains unsupported keys: {sorted(unknown)}")

            if not any(operator in condition for operator in CONDITION_OPERATORS):
                errors.append(f"{label} must declare at least one operator: {list(CONDITION_OPERATORS)}")

        return errors

    @staticmethod
    def validate(policy: dict[str, Any]) -> list[str]:
        errors: list[str] = []

        if not isinstance(policy, dict):
            return ["policy must be an object"]

        if "name" not in policy or not policy["name"]:
            errors.append("policy.name is required")

        if "when" not in policy:
            errors.append("policy.when is required")
        else:
            errors.extend(PolicyValidator.validate_conditions(policy["when"], prefix="policy.when"))

        if "result" not in policy:
            errors.append("policy.result is required")
        elif not isinstance(policy["result"], dict):
            errors.append("policy.result must be an object")
        else:
            if "status" not in policy["result"]:
                errors.append("policy.result.status is required")
            if "action" not in policy["result"]:
                errors.append("policy.result.action is required")

        requires = policy.get("requires")
        if requires is not None and not isinstance(requires, (str, list)):
            errors.append("policy.requires must be a string or a list of capability names")

        return errors

    @staticmethod
    def validate_all(policies: Any) -> list[str]:
        """Validate a list of policies, prefixing each error with the policy name."""
        if not isinstance(policies, list):
            return ["policies must be a list"]

        errors: list[str] = []
        for index, policy in enumerate(policies):
            name = policy.get("name", f"#{index}") if isinstance(policy, dict) else f"#{index}"
            for error in PolicyValidator.validate(policy):
                errors.append(f"policy '{name}': {error}")
        return errors

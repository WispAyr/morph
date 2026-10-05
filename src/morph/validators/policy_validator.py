from __future__ import annotations

from typing import Any


class PolicyValidator:
    """Validates MORPH policies before execution."""

    @staticmethod
    def validate(policy: dict[str, Any]) -> list[str]:
        errors: list[str] = []

        if not isinstance(policy, dict):
            return ["policy must be an object"]

        if "name" not in policy or not policy["name"]:
            errors.append("policy.name is required")

        if "when" not in policy:
            errors.append("policy.when is required")

        if "result" not in policy:
            errors.append("policy.result is required")

        if "when" in policy and not isinstance(policy["when"], list):
            errors.append("policy.when must be a list")

        if "result" in policy and not isinstance(policy["result"], dict):
            errors.append("policy.result must be an object")

        if "result" in policy and isinstance(policy["result"], dict):
            if "status" not in policy["result"]:
                errors.append("policy.result.status is required")
            if "action" not in policy["result"]:
                errors.append("policy.result.action is required")

        if "when" in policy and isinstance(policy["when"], list):
            for index, condition in enumerate(policy["when"]):
                if not isinstance(condition, dict):
                    errors.append(f"policy.when[{index}] must be an object")
                    continue

                if "field" not in condition:
                    errors.append(f"policy.when[{index}].field is required")

                valid_keys = {"field", "equals", "lt", "lte", "gt", "gte", "contains"}
                unknown = set(condition) - valid_keys
                if unknown:
                    errors.append(f"policy.when[{index}] contains unsupported keys: {sorted(unknown)}")

        return errors

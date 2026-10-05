from __future__ import annotations

from typing import Any


class CapabilityValidator:
    """Validate whether a required capability exists in the runtime context."""

    @staticmethod
    def validate(capability_name: str, context: dict[str, Any]) -> list[str]:
        if not capability_name:
            return []

        operator = context.get("operator", {})
        capabilities = operator.get("capabilities", [])
        if isinstance(capabilities, str):
            capabilities = [capabilities]

        if capability_name not in capabilities:
            return [f"missing capability: {capability_name}"]

        return []

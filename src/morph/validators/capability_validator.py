from __future__ import annotations

from typing import Any


class CapabilityValidator:
    """Validate whether a required capability is granted in the runtime context.

    A capability is resolved in one of two ways:

    * If the system definition declares the capability under ``capabilities`` with a
      ``requires`` list, every entry is a dotted context path that must grant it. A path
      that resolves to a list, tuple, set or string grants the capability when it
      contains the capability name; any other value grants it when it is truthy.
    * Otherwise the capability must appear in ``operator.capabilities``.
    """

    DEFAULT_GRANT_PATH = "operator.capabilities"

    @staticmethod
    def validate(
        capability_name: str,
        context: dict[str, Any],
        definition: dict[str, Any] | None = None,
    ) -> list[str]:
        if not capability_name:
            return []

        paths: list[str] = []
        if isinstance(definition, dict):
            declared = definition.get("requires", [])
            if isinstance(declared, str):
                declared = [declared]
            if isinstance(declared, list):
                paths = [path for path in declared if isinstance(path, str) and path]

        if not paths:
            paths = [CapabilityValidator.DEFAULT_GRANT_PATH]

        for path in paths:
            if not CapabilityValidator._grants(capability_name, context, path):
                return [f"missing capability: {capability_name}"]

        return []

    @staticmethod
    def _grants(capability_name: str, context: dict[str, Any], path: str) -> bool:
        current: Any = context
        for segment in path.split("."):
            if not isinstance(current, dict):
                return False
            current = current.get(segment)
            if current is None:
                return False

        if isinstance(current, (list, tuple, set, str)):
            return capability_name in current
        return bool(current)

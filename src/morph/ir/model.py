from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MORPHIR:
    name: str
    version: str = "0.1.0"
    entities: list[dict[str, Any]] = field(default_factory=list)
    policies: list[dict[str, Any]] = field(default_factory=list)
    capabilities: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "MORPHIR":
        if not isinstance(data, dict):
            raise TypeError("MORPHIR requires a dictionary payload")

        return cls(
            name=data.get("name", "morph"),
            version=data.get("version", "0.1.0"),
            entities=data.get("entities", []),
            policies=data.get("policies", []),
            capabilities=data.get("capabilities", {}),
        )

    @classmethod
    def from_json(cls, payload: str) -> "MORPHIR":
        return cls.from_dict(json.loads(payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "entities": self.entities,
            "policies": self.policies,
            "capabilities": self.capabilities,
        }

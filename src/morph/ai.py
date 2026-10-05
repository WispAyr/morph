from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .ir import MORPHIR


@dataclass
class SemanticAI:
    """AI-native interface for semantic software reasoning over a MORPH model."""

    definition: MORPHIR | dict[str, Any]
    approved: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.definition, MORPHIR):
            self.definition = MORPHIR.from_dict(self.definition)

    def inspect(self) -> dict[str, Any]:
        return self.definition.inspect()

    def query(self, subject: str) -> dict[str, Any]:
        return self.definition.impact(subject)

    def impact(self, subject: str) -> dict[str, Any]:
        return self.definition.impact(subject)

    def propose(self, candidate: MORPHIR | dict[str, Any], *, intent: str | None = None) -> dict[str, Any]:
        return self.definition.propose(candidate, intent=intent)

    def diff(self, candidate: MORPHIR | dict[str, Any]) -> dict[str, Any]:
        return self.definition.diff(candidate)

    def simulate(self, scenarios: list[dict[str, Any]] | dict[str, Any]) -> dict[str, Any]:
        return self.definition.simulate(scenarios)

    def validate(self) -> dict[str, Any]:
        self.definition.validate()
        return {"status": "valid", "name": self.definition.name, "version": self.definition.version}

    def approve(self, *, approved: bool = True, by: str | None = None) -> dict[str, Any]:
        self.approved = bool(approved)
        return {"approved": self.approved, "by": by}

    def explain(self, candidate: MORPHIR | dict[str, Any] | None = None) -> dict[str, Any]:
        if candidate is None:
            return self.definition.inspect()
        return self.definition.propose(candidate)

    def apply(self, candidate: MORPHIR | dict[str, Any]) -> MORPHIR:
        if not isinstance(candidate, MORPHIR):
            candidate = MORPHIR.from_dict(candidate)
        self.definition.validate_change(candidate)
        return candidate

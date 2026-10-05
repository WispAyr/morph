from __future__ import annotations

from typing import Any

from morph.runtime import MORPHRuntime

from .model import MORPHIR


class ExecutionGraph:
    """A reusable execution graph that turns a generic MORPH IR into executable rules."""

    def __init__(self, ir: MORPHIR | dict[str, Any]):
        self.ir = ir if isinstance(ir, MORPHIR) else MORPHIR.from_dict(ir)
        self.runtime = MORPHRuntime(
            name=self.ir.name,
            version=self.ir.version,
            policies=self.ir.policies,
            capabilities=self.ir.capabilities,
            entities=self.ir.entities,
            actions=self.ir.actions,
        )

    @classmethod
    def from_ir(cls, data: MORPHIR | dict[str, Any]) -> "ExecutionGraph":
        return cls(data)

    def evaluate(self, context: dict[str, Any]) -> dict[str, Any]:
        return self.runtime.evaluate(context)

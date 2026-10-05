"""Crosspoint example: a fake studio router and notifier behind the route_control and
notify capabilities declared in ``crosspoint.yaml``.

The router is deliberately stateful and failure-prone so the effect executor's retry,
idempotency, and failure handling can be exercised end to end::

    from morph.examples.crosspoint import build
    executor, router, notifier = build()
    result = executor.run(context)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..effects import AdapterRegistry, EffectExecutor, EffectFailure, EffectLog
from ..loader import load_system_definition
from ..runtime import MORPHRuntime

DEFINITION_PATH = Path(__file__).with_name("crosspoint.yaml")


class FakeRouter:
    """An in-memory stand-in for a video/audio matrix router."""

    def __init__(self, busy_for: int = 0):
        self.routes: dict[str, str] = {}
        self.locks: dict[str, str] = {}
        self.calls: list[dict[str, Any]] = []
        self._busy_for = busy_for

    def lock(self, destination: str, operator: str) -> None:
        self.locks[destination] = operator

    def route(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(dict(inputs))
        if self._busy_for > 0:
            self._busy_for -= 1
            raise EffectFailure("router_busy", "router is applying another change", retryable=True)

        destination = inputs["destination"]
        owner = self.locks.get(destination)
        if owner is not None and owner != inputs["operator"]:
            raise EffectFailure(
                "destination_locked",
                f"{destination} is locked by {owner}",
                retryable=False,
                details={"locked_by": owner},
            )

        previous = self.routes.get(destination, "")
        self.routes[destination] = inputs["source"]
        return {"route_id": f"{inputs['source']}->{destination}", "previous_source": previous}


class FakeNotifier:
    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def notify(self, inputs: dict[str, Any]) -> dict[str, Any]:
        self.messages.append(dict(inputs))
        return {}


def load_runtime() -> MORPHRuntime:
    definition = load_system_definition(DEFINITION_PATH)
    return MORPHRuntime(
        name=definition.name,
        version=definition.version,
        policies=definition.policies,
        capabilities=definition.capabilities,
        entities=definition.entities,
        actions=definition.actions,
    )


def registry(router: FakeRouter | None = None, notifier: FakeNotifier | None = None) -> AdapterRegistry:
    router = router or FakeRouter()
    notifier = notifier or FakeNotifier()
    return AdapterRegistry({"route_control": router.route, "notify": notifier.notify})


def build(
    router: FakeRouter | None = None,
    notifier: FakeNotifier | None = None,
    log: EffectLog | None = None,
) -> tuple[EffectExecutor, FakeRouter, FakeNotifier]:
    router = router or FakeRouter()
    notifier = notifier or FakeNotifier()
    executor = EffectExecutor(load_runtime(), registry(router, notifier), log)
    return executor, router, notifier


def adapters() -> AdapterRegistry:
    """Factory for ``morph run --adapters morph.examples.crosspoint:adapters``."""
    return registry()

"""MORPHSystem: a running system with durable, event-sourced state.

It ties the pieces together:

* ``observe`` records what the world reports about an entity instance;
* ``context`` assembles the evaluation context from the current projection;
* ``act`` decides and executes, recording the decision and every effect attempt;
* entity state machines advance on ``observed`` and ``<action>.<outcome>`` events;
* everything is an event in the :class:`~morph.store.EventStore`, so a new instance
  pointed at the same log rebuilds the projection and the idempotency memory without
  re-running any effect.
"""

from __future__ import annotations

from typing import Any

from .effects import AdapterRegistry, EffectExecutor, EffectLog, EffectRecord, ExecutionResult
from .expressions import EvaluationError
from .ir import MORPHIR
from .runtime import MORPHRuntime
from .schema import STATE_FIELD, EntityType
from .store import SYSTEM_STREAM, Event, EventStore, stream_for

OBSERVED = "observed"
DECIDED = "decided"
EFFECT = "effect"
TRANSITIONED = "transitioned"


class MORPHSystem:
    def __init__(
        self,
        definition: MORPHIR | dict[str, Any],
        adapters: Any = None,
        store: EventStore | None = None,
        *,
        strict: bool = True,
    ):
        self.ir = definition if isinstance(definition, MORPHIR) else MORPHIR.from_dict(definition)
        self.runtime = MORPHRuntime(
            name=self.ir.name,
            version=self.ir.version,
            policies=self.ir.policies,
            capabilities=self.ir.capabilities,
            entities=self.ir.entities,
            actions=self.ir.actions,
        )
        self.schema = self.runtime.schema
        self.store = store if store is not None else EventStore()
        self.executor: EffectExecutor | None = None
        if adapters is not None:
            self.executor = EffectExecutor(self.runtime, AdapterRegistry.coerce(adapters), EffectLog(sink=self._record_effect), strict=strict)

        # projection: entity name -> instance id -> fields (including machine state)
        self.projection: dict[str, dict[str, dict[str, Any]]] = {}
        self._replay()

    # --- replay ---------------------------------------------------------------------

    def _replay(self) -> None:
        for event in self.store:
            self._apply(event)

    def _apply(self, event: Event) -> None:
        """Fold one event into the projection. Must stay deterministic and effect-free."""
        if event.kind == OBSERVED:
            instance = self._instance(event.data["entity"], event.data["id"], create=True)
            instance.update(event.data.get("fields") or {})
        elif event.kind == TRANSITIONED:
            instance = self._instance(event.data["entity"], event.data["id"], create=True)
            instance[STATE_FIELD] = event.data["to"]
        elif event.kind == EFFECT and self.executor is not None:
            data = event.data
            if data.get("status") == "succeeded" and data.get("idempotency_key"):
                self.executor.remember(data["idempotency_key"], data.get("outputs") or {})

    def _instance(self, entity: str, entity_id: str, *, create: bool) -> dict[str, Any]:
        instances = self.projection.setdefault(entity, {})
        instance = instances.get(entity_id)
        if instance is None:
            if not create:
                return {"id": entity_id}
            instance = {"id": entity_id}
            entity_type = self.schema.entities.get(entity)
            if entity_type is not None and entity_type.stateful:
                instance[STATE_FIELD] = entity_type.initial
            instances[entity_id] = instance
        return instance

    # --- observation ----------------------------------------------------------------

    def observe(self, entity: str, entity_id: str, fields: dict[str, Any] | None = None, **more: Any) -> Event:
        """Record what the world reports about one entity instance."""
        if not self.schema.empty and entity not in self.schema.entities:
            raise ValueError(f"unknown entity '{entity}' (known: {sorted(self.schema.entities)})")
        payload = {**(fields or {}), **more}
        if STATE_FIELD in payload and self.schema.entities.get(entity, EntityType(entity)).stateful:
            raise ValueError(f"'{STATE_FIELD}' is owned by the state machine of '{entity}'; it cannot be observed")
        errors = self.schema.validate_context({entity: {**payload, "id": entity_id}})
        if errors:
            raise ValueError("Invalid observation: " + "; ".join(errors))

        event = self.store.append(stream_for(entity, entity_id), OBSERVED, {"entity": entity, "id": entity_id, "fields": payload})
        self._apply(event)
        instance = self._instance(entity, entity_id, create=True)
        self._transition(OBSERVED, {entity: dict(instance)}, {"kind": OBSERVED, "entity": entity, "id": entity_id, "fields": payload})
        return event

    # --- context and decisions -------------------------------------------------------

    def state(self, entity: str, entity_id: str) -> dict[str, Any]:
        return dict(self._instance(entity, entity_id, create=False))

    def context(self, bindings: dict[str, str] | None = None, extra: dict[str, Any] | None = None, **more: str) -> dict[str, Any]:
        """Build an evaluation context from entity instances in the projection.

        ``system.context(source="cam1", destination="wall")`` yields the current fields of
        those instances under their entity names, with ``id`` set.
        """
        selected = {**(bindings or {}), **more}
        context: dict[str, Any] = {}
        for entity, entity_id in selected.items():
            if not self.schema.empty and entity not in self.schema.entities:
                raise ValueError(f"unknown entity '{entity}' (known: {sorted(self.schema.entities)})")
            context[entity] = dict(self._instance(entity, str(entity_id), create=False))
        for key, value in (extra or {}).items():
            if isinstance(value, dict) and isinstance(context.get(key), dict):
                context[key] = {**context[key], **value}
            else:
                context[key] = value
        return context

    def decide(self, context: dict[str, Any]) -> dict[str, Any]:
        decision = self.runtime.evaluate(context)
        self.store.append(SYSTEM_STREAM, DECIDED, {"context": context, "decision": decision})
        return decision

    def act(self, context: dict[str, Any] | None = None, **bindings: str) -> ExecutionResult:
        """Decide, execute the effect, and advance entity state machines on the outcome."""
        if self.executor is None:
            raise RuntimeError("MORPHSystem was created without adapters; pass adapters to execute effects")
        if context is None:
            context = self.context(**bindings)
        decision = self.decide(context)
        result = self.executor.execute(decision, context)
        if result.action is not None:
            outcome = {"executed": "succeeded", "skipped": "skipped", "failed": "failed", "denied": "denied"}[result.status]
            event_payload = {
                "kind": f"{result.action}.{outcome}",
                "action": result.action,
                "capability": result.capability,
                "inputs": result.inputs,
                "outputs": result.outputs,
                "error": result.error,
            }
            self._transition(event_payload["kind"], context, event_payload)
        return result

    # --- transitions -----------------------------------------------------------------

    def _record_effect(self, record: EffectRecord) -> None:
        self.store.append(SYSTEM_STREAM, EFFECT, record.to_dict())

    def _transition(self, kind: str, context: dict[str, Any], event_payload: dict[str, Any]) -> list[Event]:
        fired: list[Event] = []
        for entity_type in self.schema.stateful_entities:
            for transition in entity_type.transitions_for(kind):
                evaluation_context = {**context, "event": event_payload}
                entity_id = self._target_id(entity_type, transition, evaluation_context)
                if entity_id is None:
                    continue
                instance = self._instance(entity_type.name, entity_id, create=True)
                current = instance.get(STATE_FIELD) or entity_type.initial
                if not transition.accepts(current):
                    continue
                if transition.when is not None:
                    scoped = {**evaluation_context, entity_type.name: dict(instance)}
                    if not transition.when.evaluate(scoped):
                        continue
                if current == transition.to:
                    continue
                event = self.store.append(
                    stream_for(entity_type.name, entity_id),
                    TRANSITIONED,
                    {"entity": entity_type.name, "id": entity_id, "from": current, "to": transition.to, "on": kind},
                )
                self._apply(event)
                fired.append(event)
                break  # one transition per entity instance per event
        return fired

    @staticmethod
    def _target_id(entity_type: EntityType, transition: Any, context: dict[str, Any]) -> str | None:
        if transition.id_expr is not None:
            try:
                value = transition.id_expr.value(context)
            except EvaluationError:
                return None
            return str(value) if value is not None else None
        instance = context.get(entity_type.name)
        if isinstance(instance, dict) and instance.get("id") is not None:
            return str(instance["id"])
        event = context.get("event") or {}
        if event.get("entity") == entity_type.name and event.get("id") is not None:
            return str(event["id"])
        return None

    # --- introspection ----------------------------------------------------------------

    def history(self, stream: str | None = None, kinds: list[str] | None = None) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.store.events(stream=stream, kinds=kinds)]

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.ir.name,
            "version": self.ir.version,
            "events": len(self.store),
            "entities": {entity: {entity_id: dict(fields) for entity_id, fields in instances.items()} for entity, instances in self.projection.items()},
        }

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
        if "id" in payload and payload["id"] != entity_id:
            raise ValueError(
                f"observation id '{payload['id']}' does not match entity instance id '{entity_id}'"
            )
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
        decision, _ = self._record_decision(context)
        return decision

    def _record_decision(self, context: dict[str, Any]) -> tuple[dict[str, Any], Event]:
        decision = self.runtime.evaluate(context)
        event = self.store.append(SYSTEM_STREAM, DECIDED, {"context": context, "decision": decision})
        return decision, event

    def act(self, context: dict[str, Any] | None = None, **bindings: str) -> ExecutionResult:
        """Decide, execute the effect, and advance entity state machines on the outcome."""
        if self.executor is None:
            raise RuntimeError("MORPHSystem was created without adapters; pass adapters to execute effects")
        if context is None:
            context = self.context(**bindings)
        decision, decision_event = self._record_decision(context)
        result = self.executor.execute(decision, context)
        # An unbound decision named an action with no binding, so nothing ran and there is
        # no outcome for a state machine to react to. The effect log still records it.
        if result.action is not None and result.status != "unbound":
            outcome = {"executed": "succeeded", "skipped": "skipped", "failed": "failed", "denied": "denied"}[result.status]
            event_payload = {
                "kind": f"{result.action}.{outcome}",
                "action": result.action,
                "capability": result.capability,
                "inputs": result.inputs,
                "outputs": result.outputs,
                "error": result.error,
            }
            caused_by = {
                "decision_seq": decision_event.seq,
                "effect_ids": [record.id for record in result.records],
            }
            self._transition(event_payload["kind"], context, event_payload, caused_by=caused_by)
        return result

    # --- transitions -----------------------------------------------------------------

    def _record_effect(self, record: EffectRecord) -> None:
        self.store.append(SYSTEM_STREAM, EFFECT, record.to_dict())

    def _transition(
        self,
        kind: str,
        context: dict[str, Any],
        event_payload: dict[str, Any],
        *,
        caused_by: dict[str, Any] | None = None,
    ) -> list[Event]:
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
                transition_data = {"entity": entity_type.name, "id": entity_id, "from": current, "to": transition.to, "on": kind}
                if caused_by is not None:
                    transition_data["caused_by"] = caused_by
                event = self.store.append(stream_for(entity_type.name, entity_id), TRANSITIONED, transition_data)
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

    def replay(self, entity: str | None = None, entity_id: str | None = None, *, after: int = 0, kinds: list[str] | None = None) -> list[dict[str, Any]]:
        """Return the event history for a system or a specific entity stream.

        This is the causal-history view AI systems need for time-travel style reasoning.
        """
        if entity is None and entity_id is not None:
            raise ValueError("entity_id requires entity")
        if entity is None:
            return [event.to_dict() for event in self.store.events(after=after, kinds=kinds)]
        if entity_id is None:
            prefix = f"{entity}/"
            events = self.store.events(after=after, kinds=kinds)
            return [event.to_dict() for event in events if event.stream.startswith(prefix)]
        stream = stream_for(entity, entity_id)
        return [event.to_dict() for event in self.store.events(stream=stream, after=after, kinds=kinds)]

    def provenance(self, entity: str, entity_id: str) -> dict[str, Any]:
        """Return a causal summary describing the current state of an entity instance."""
        events = self.replay(entity, entity_id)
        current = self.state(entity, entity_id)
        return {
            "entity": entity,
            "id": entity_id,
            "current_state": current,
            "events": events,
            "summary": {
                "event_count": len(events),
                "last_kind": events[-1]["kind"] if events else None,
            },
        }

    def explain(self, entity: str, entity_id: str) -> dict[str, Any]:
        """Return a human-readable causal explanation for the current state of an entity."""
        provenance = self.provenance(entity, entity_id)
        events = provenance["events"]
        current = provenance["current_state"]
        narrative_parts: list[str] = [f"{entity} {entity_id} currently sits in state '{current.get('state', 'unknown')}'."]
        causal: dict[str, Any] = {"transition": None, "decision": None, "effects": []}

        if not events:
            narrative_parts.append("There are no recorded events for this entity instance.")
            narrative = " ".join(narrative_parts)
            return {"entity": entity, "id": entity_id, "current_state": current, "summary": narrative, "narrative": narrative, "events": events, "causal": causal}

        transition = next((event for event in reversed(events) if event["kind"] == "transitioned"), None)
        if transition is not None:
            data = transition["data"]
            causal["transition"] = transition
            narrative_parts.append(
                f"The state transitioned from '{data.get('from')}' to '{data.get('to')}' on '{data.get('on')}', "
                f"which is the most recent causal change in this event chain."
            )
            cause_refs = data.get("caused_by") or {}
            decision_seq = cause_refs.get("decision_seq")
            if decision_seq is not None:
                decision_event = next((event.to_dict() for event in self.store if event.seq == decision_seq), None)
                causal["decision"] = decision_event
            effect_ids = set(cause_refs.get("effect_ids") or [])
            if effect_ids:
                causal["effects"] = [
                    event.to_dict()
                    for event in self.store.events(stream=SYSTEM_STREAM, kinds=[EFFECT])
                    if event.data.get("id") in effect_ids
                ]

            decision_data = (causal["decision"] or {}).get("data", {}).get("decision", {})
            if decision_data:
                narrative_parts.append(
                    f"Policy '{decision_data.get('policy', 'unknown')}' selected "
                    f"'{decision_data.get('status', 'unknown')}' action '{decision_data.get('action', 'unknown')}'."
                )
            for effect_event in causal["effects"]:
                effect_data = effect_event["data"]
                narrative_parts.append(
                    f"Effect '{effect_data.get('action', 'unknown')}' "
                    f"{effect_data.get('status', 'unknown')} through capability '{effect_data.get('capability', 'unknown')}'."
                )
        else:
            narrative_parts.append("No transitioned state was observed, so the current state is driven by observation history alone.")

        narrative_parts.append(f"The causal chain includes {len(events)} recorded events, beginning with '{events[0]['kind']}' and ending with '{events[-1]['kind']}'.")
        narrative = " ".join(narrative_parts)
        return {"entity": entity, "id": entity_id, "current_state": current, "summary": narrative, "narrative": narrative, "events": events, "causal": causal}

    def explain_diff(self, entity: str, entity_id: str, *, omit_kinds: set[str] | None = None, omit_seq: int | None = None) -> dict[str, Any]:
        """Compare the actual entity state against the counterfactual path without selected event kinds."""
        actual = self.state(entity, entity_id)
        alternative = self.counterfactual(entity, entity_id, omit_kinds=omit_kinds, omit_seq=omit_seq)
        before = alternative["current_state"]
        after = actual
        delta = {key: {"before": before.get(key), "after": after.get(key)} for key in sorted(set(before) | set(after)) if before.get(key) != after.get(key)}
        narrative = (
            f"{entity} {entity_id} would have remained in state '{before.get('state', 'unknown')}' "
            f"without {', '.join(sorted(omit_kinds or set())) or 'the observed causal change'}, "
            f"but the actual history moved it to '{after.get('state', 'unknown')}' through the observed transition path."
        )
        return {
            "entity": entity,
            "id": entity_id,
            "before": before,
            "after": after,
            "delta": delta,
            "narrative": narrative,
            "summary": narrative,
            "omitted": sorted(omit_kinds or set()),
        }

    def review(self, entity: str, entity_id: str, *, approver: str | None = None, approved: bool | None = None) -> dict[str, Any]:
        """Return the semantic review object for one entity instance.

        This bundles the causal provenance, policy lineage, and approval metadata in one
        machine-readable artifact for AI review workflows.
        """
        provenance = self.provenance(entity, entity_id)
        policy_lineage = []
        for policy in self.ir.policies:
            if not isinstance(policy, dict):
                continue
            result = policy.get("result") or {}
            policy_lineage.append({
                "name": policy.get("name", "unknown"),
                "when": policy.get("when"),
                "status": result.get("status"),
                "action": result.get("action"),
            })

        invariant_lineage = [{
            "name": item.get("name", "unknown"),
            "when": item.get("when"),
        } for item in (self.ir.invariants or []) if isinstance(item, dict)]

        approval = {
            "approved": bool(approved) if approved is not None else False,
            "by": approver,
            "status": "approved" if approved else "pending",
        }

        summary = (
            f"{entity} {entity_id} is in state '{provenance['current_state'].get('state', 'unknown')}', "
            f"with {len(provenance['events'])} causal events, {len(policy_lineage)} policy entries, "
            f"and {len(invariant_lineage)} invariants in scope."
        )
        return {
            "entity": entity,
            "id": entity_id,
            "current_state": provenance["current_state"],
            "provenance": provenance,
            "policy_lineage": policy_lineage,
            "invariant_lineage": invariant_lineage,
            "approval": approval,
            "summary": summary,
            "narrative": summary,
        }

    def counterfactual(self, entity: str, entity_id: str, *, omit_kinds: set[str] | None = None, omit_seq: int | None = None) -> dict[str, Any]:
        """Rebuild an entity stream without selected events to estimate alternate behaviour.

        This is the semantic answer to: “what would have happened if this decision/event had
        not occurred?”
        """
        stream = stream_for(entity, entity_id)
        filtered = [
            event
            for event in self.store.events(stream=stream)
            if (omit_seq is None or event.seq != omit_seq)
            and (omit_kinds is None or event.kind not in omit_kinds)
        ]

        projected_store = EventStore()
        for event in filtered:
            projected_store.append(event.stream, event.kind, event.data)

        counter = MORPHSystem(self.ir, store=projected_store)
        return {
            "entity": entity,
            "id": entity_id,
            "current_state": counter.state(entity, entity_id),
            "events": [event.to_dict() for event in filtered],
            "omitted": sorted({event.kind for event in self.store.events(stream=stream) if omit_kinds is not None and event.kind in omit_kinds}),
            "summary": {"event_count": len(filtered)},
        }

    def history(self, stream: str | None = None, kinds: list[str] | None = None) -> list[dict[str, Any]]:
        return [event.to_dict() for event in self.store.events(stream=stream, kinds=kinds)]

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.ir.name,
            "version": self.ir.version,
            "events": len(self.store),
            "entities": {entity: {entity_id: dict(fields) for entity_id, fields in instances.items()} for entity, instances in self.projection.items()},
        }

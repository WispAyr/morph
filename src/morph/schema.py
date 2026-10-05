"""Typed entity schema for MORPH definitions.

Entities declare the shape of the execution context. Once a definition declares any
entities, every field path a policy reads must exist in the schema, and a context whose
declared fields carry the wrong type is rejected before evaluation.

Entities may be declared with explicit field types::

    entities:
      - name: source
        fields:
          status: string
          latency_ms: int

or with example state, from which types are inferred::

    entities:
      - name: source
        state:
          status: live
          latency_ms: 42

An entity may also carry a state machine. Its current state is exposed to policies as the
``state`` field, and transitions fire on events the system records::

    entities:
      - name: destination
        fields: {id: string, status: string}
        states: [idle, routed]
        initial: idle
        transitions:
          - {on: route_source.succeeded, from: "*", to: routed}
          - {on: observed, from: "*", to: idle, when: 'event.fields.status != "ready"'}
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

from .expressions import Expression, ExpressionError, Predicate, compile_value, compile_when

FIELD_TYPES: dict[str, tuple[type, ...] | None] = {
    "string": (str,),
    "int": (int,),
    "double": (int, float),
    "bool": (bool,),
    "list": (list, tuple),
    "map": (dict,),
    "any": None,
}
# Types whose values may be traversed further with a dotted path.
NESTABLE_TYPES = {"map", "any", "list"}
STATE_FIELD = "state"
ENTITY_KEYS = {"name", "description", "fields", "state", "states", "initial", "transitions"}
TRANSITION_KEYS = {"on", "from", "to", "when", "id", "description"}
# Roots that transition conditions may read besides declared entities.
TRANSITION_ROOTS = {"event"}


def infer_type(value: Any) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "double"
    if isinstance(value, str):
        return "string"
    if isinstance(value, (list, tuple)):
        return "list"
    if isinstance(value, dict):
        return "map"
    return "any"


def matches_type(value: Any, type_name: str) -> bool:
    allowed = FIELD_TYPES[type_name]
    if allowed is None:
        return True
    if type_name in ("int", "double") and isinstance(value, bool):
        return False
    return isinstance(value, allowed)


_matches_type = matches_type


def validate_typed_fields(values: Any, declared: dict[str, str], label: str, *, require_all: bool) -> list[str]:
    """Check a dict of values against declared field types. Shared by schema and capabilities."""
    if not isinstance(values, dict):
        return [f"{label} must be an object"]
    errors: list[str] = []
    for name, type_name in declared.items():
        if name not in values or values[name] is None:
            if require_all:
                errors.append(f"{label}.{name} is required ({type_name})")
            continue
        if not matches_type(values[name], type_name):
            errors.append(f"{label}.{name} must be {type_name}, got {infer_type(values[name])}")
    unknown = set(values) - set(declared)
    if unknown and require_all:
        errors.append(f"{label} has undeclared fields: {sorted(unknown)}")
    return errors


@dataclass
class Transition:
    on: str
    to: str
    sources: list[str] | None = None  # None means any state
    when: Predicate | None = None
    id_expr: Expression | None = None
    description: str = ""

    def accepts(self, current: str | None) -> bool:
        return self.sources is None or current in self.sources

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"on": self.on, "from": "*" if self.sources is None else list(self.sources), "to": self.to}
        if self.when is not None:
            data["when"] = self.when.source
        if self.id_expr is not None:
            data["id"] = self.id_expr.source
        if self.description:
            data["description"] = self.description
        return data


@dataclass
class EntityType:
    name: str
    fields: dict[str, str] = field(default_factory=dict)
    states: list[str] = field(default_factory=list)
    initial: str | None = None
    transitions: list[Transition] = field(default_factory=list)
    description: str = ""

    @property
    def stateful(self) -> bool:
        return bool(self.states)

    def transitions_for(self, kind: str) -> list[Transition]:
        return [transition for transition in self.transitions if transition.on == kind]

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "fields": dict(self.fields)}
        if self.states:
            data["states"] = list(self.states)
            data["initial"] = self.initial
            data["transitions"] = [transition.to_dict() for transition in self.transitions]
        if self.description:
            data["description"] = self.description
        return data


class Schema:
    """The set of entity types a definition declares."""

    def __init__(self, entities: dict[str, EntityType] | None = None):
        self.entities: dict[str, EntityType] = dict(entities or {})

    @property
    def empty(self) -> bool:
        return not self.entities

    @property
    def stateful_entities(self) -> list[EntityType]:
        return [entity for entity in self.entities.values() if entity.stateful]

    @classmethod
    def from_ir(cls, entities: Any) -> "Schema":
        if not entities:
            return cls()

        if isinstance(entities, dict):
            entities = [{"name": name, **(spec if isinstance(spec, dict) else {})} for name, spec in entities.items()]
        if not isinstance(entities, list):
            raise ValueError("entities must be a list of entity declarations")

        types: dict[str, EntityType] = {}
        errors: list[str] = []
        pending_transitions: list[tuple[EntityType, list[Any]]] = []
        for index, declaration in enumerate(entities):
            if not isinstance(declaration, dict):
                errors.append(f"entities[{index}] must be an object")
                continue
            name = declaration.get("name")
            if not isinstance(name, str) or not name:
                errors.append(f"entities[{index}].name is required")
                continue
            if name in types:
                errors.append(f"entity '{name}' is declared more than once")
                continue
            unknown = set(declaration) - ENTITY_KEYS
            if unknown:
                errors.append(f"entity '{name}' has unsupported keys: {sorted(unknown)}")

            fields: dict[str, str] = {}
            state = declaration.get("state")
            if isinstance(state, dict):
                for key, example in state.items():
                    fields[str(key)] = infer_type(example)
            declared = declaration.get("fields")
            if declared is not None:
                if not isinstance(declared, dict):
                    errors.append(f"entity '{name}'.fields must be an object")
                else:
                    for key, type_name in declared.items():
                        if type_name not in FIELD_TYPES:
                            errors.append(
                                f"entity '{name}'.{key} has unknown type '{type_name}' (known: {sorted(FIELD_TYPES)})"
                            )
                        else:
                            fields[str(key)] = type_name

            states = declaration.get("states") or []
            initial = declaration.get("initial")
            if states:
                if not isinstance(states, list) or not all(isinstance(item, str) and item for item in states):
                    errors.append(f"entity '{name}'.states must be a list of state names")
                    states = []
                else:
                    if initial is None:
                        initial = states[0]
                    elif initial not in states:
                        errors.append(f"entity '{name}'.initial '{initial}' is not one of its states {states}")
                    if STATE_FIELD in fields and fields[STATE_FIELD] != "string":
                        errors.append(f"entity '{name}'.{STATE_FIELD} is reserved for the machine state and must be string")
                    fields[STATE_FIELD] = "string"
            elif declaration.get("transitions") or initial is not None:
                errors.append(f"entity '{name}' declares transitions or initial without states")

            entity = EntityType(
                name=name,
                fields=fields,
                states=list(states),
                initial=initial if states else None,
                description=str(declaration.get("description", "")),
            )
            types[name] = entity
            if states:
                pending_transitions.append((entity, declaration.get("transitions") or []))

        schema = cls(types)
        for entity, raw_transitions in pending_transitions:
            errors.extend(schema._parse_transitions(entity, raw_transitions))

        if errors:
            raise ValueError("Invalid entity schema: " + "; ".join(errors))
        return schema

    def _parse_transitions(self, entity: EntityType, raw: Any) -> list[str]:
        errors: list[str] = []
        if not isinstance(raw, list):
            return [f"entity '{entity.name}'.transitions must be a list"]

        for index, item in enumerate(raw):
            label = f"entity '{entity.name}'.transitions[{index}]"
            if not isinstance(item, dict):
                errors.append(f"{label} must be an object")
                continue
            # YAML 1.1 parses a bare `on:` key as boolean True; accept it as "on".
            item = {("on" if key is True else key): value for key, value in item.items()}
            unknown = set(item) - TRANSITION_KEYS
            if unknown:
                errors.append(f"{label} has unsupported keys: {sorted(unknown)}")

            on = item.get("on")
            if not isinstance(on, str) or not on:
                errors.append(f"{label}.on is required (an event kind such as 'observed' or '<action>.succeeded')")
                continue

            to = item.get("to")
            if to not in entity.states:
                errors.append(f"{label}.to '{to}' is not one of {entity.states}")
                continue

            sources_raw = item.get("from", "*")
            sources: list[str] | None
            if sources_raw == "*" or sources_raw is None:
                sources = None
            else:
                if isinstance(sources_raw, str):
                    sources_raw = [sources_raw]
                if not isinstance(sources_raw, list):
                    errors.append(f"{label}.from must be a state, a list of states, or '*'")
                    continue
                bad = [state for state in sources_raw if state not in entity.states]
                if bad:
                    errors.append(f"{label}.from names unknown states {bad}")
                    continue
                sources = list(sources_raw)

            when = None
            if item.get("when") is not None:
                try:
                    when = compile_when(item["when"])
                except ExpressionError as exc:
                    errors.append(f"{label}.when: {exc}")
                    continue
                errors.extend(f"{label}.when: {error}" for error in self.validate_paths(when.paths, extra_roots=TRANSITION_ROOTS))

            id_expr = None
            if item.get("id") is not None:
                if not isinstance(item["id"], str):
                    errors.append(f"{label}.id must be a CEL expression string")
                    continue
                try:
                    id_expr = compile_value(item["id"])
                except ExpressionError as exc:
                    errors.append(f"{label}.id: {exc}")
                    continue
                errors.extend(f"{label}.id: {error}" for error in self.validate_paths(id_expr.paths, extra_roots=TRANSITION_ROOTS))

            entity.transitions.append(
                Transition(on=on, to=to, sources=sources, when=when, id_expr=id_expr, description=str(item.get("description", "")))
            )
        return errors

    def validate_paths(self, paths: Iterable[str], extra_roots: Iterable[str] = ()) -> list[str]:
        """Check that every dotted path reads a declared entity and field."""
        if self.empty:
            return []

        allowed_roots = set(extra_roots)
        errors: list[str] = []
        for path in sorted(set(paths)):
            root, *rest = path.split(".")
            if root in allowed_roots:
                continue
            entity = self.entities.get(root)
            if entity is None:
                errors.append(f"unknown entity '{root}' in '{path}' (known: {sorted(self.entities)})")
                continue
            if not rest:
                continue
            field_name = rest[0]
            field_type = entity.fields.get(field_name)
            if field_type is None:
                errors.append(f"unknown field '{root}.{field_name}' in '{path}' (known: {sorted(entity.fields)})")
                continue
            if len(rest) > 1 and field_type not in NESTABLE_TYPES:
                errors.append(f"field '{root}.{field_name}' is {field_type} and has no member '{rest[1]}' in '{path}'")
        return errors

    def validate_context(self, context: Any) -> list[str]:
        """Check that declared fields present in the context carry their declared type."""
        if self.empty:
            return []
        if not isinstance(context, dict):
            return ["context must be an object"]

        errors: list[str] = []
        for name, entity in self.entities.items():
            if name not in context:
                continue
            value = context[name]
            if not isinstance(value, dict):
                errors.append(f"context.{name} must be an object")
                continue
            for field_name, type_name in entity.fields.items():
                if field_name not in value or value[field_name] is None:
                    continue
                if not matches_type(value[field_name], type_name):
                    errors.append(
                        f"context.{name}.{field_name} must be {type_name}, got {infer_type(value[field_name])}"
                    )
            if entity.stateful and value.get(STATE_FIELD) is not None and value[STATE_FIELD] not in entity.states:
                errors.append(f"context.{name}.{STATE_FIELD} '{value[STATE_FIELD]}' is not one of {entity.states}")
        return errors

    def to_dict(self) -> list[dict[str, Any]]:
        return [entity.to_dict() for entity in self.entities.values()]

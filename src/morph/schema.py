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
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

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


def _matches_type(value: Any, type_name: str) -> bool:
    allowed = FIELD_TYPES[type_name]
    if allowed is None:
        return True
    if type_name in ("int", "double") and isinstance(value, bool):
        return False
    return isinstance(value, allowed)


@dataclass
class EntityType:
    name: str
    fields: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "fields": dict(self.fields)}


class Schema:
    """The set of entity types a definition declares."""

    def __init__(self, entities: dict[str, EntityType] | None = None):
        self.entities: dict[str, EntityType] = dict(entities or {})

    @property
    def empty(self) -> bool:
        return not self.entities

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
            types[name] = EntityType(name=name, fields=fields)

        if errors:
            raise ValueError("Invalid entity schema: " + "; ".join(errors))
        return cls(types)

    def validate_paths(self, paths: Iterable[str]) -> list[str]:
        """Check that every dotted path reads a declared entity and field."""
        if self.empty:
            return []

        errors: list[str] = []
        for path in sorted(set(paths)):
            root, *rest = path.split(".")
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
                if not _matches_type(value[field_name], type_name):
                    errors.append(
                        f"context.{name}.{field_name} must be {type_name}, got {infer_type(value[field_name])}"
                    )
        return errors

    def to_dict(self) -> list[dict[str, Any]]:
        return [entity.to_dict() for entity in self.entities.values()]

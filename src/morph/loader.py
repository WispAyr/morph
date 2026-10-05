from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.nodes import MappingNode
from yaml.resolver import BaseResolver

from .ir import MORPHIR


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects ambiguous mappings."""


def _construct_unique_mapping(loader: _UniqueKeySafeLoader, node: MappingNode, deep: bool = False) -> dict[Any, Any]:
    explicit_keys: set[Any] = set()
    for key_node, _ in node.value:
        # YAML merge keys intentionally inherit values that may be overridden locally.
        if key_node.tag == "tag:yaml.org,2002:merge":
            continue
        key = loader.construct_object(key_node, deep=deep)
        try:
            duplicate = key in explicit_keys
            explicit_keys.add(key)
        except TypeError as exc:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "found an unhashable mapping key",
                key_node.start_mark,
            ) from exc
        if duplicate:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"found duplicate key {key!r}",
                key_node.start_mark,
            )

    loader.flatten_mapping(node)
    result: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


_UniqueKeySafeLoader.add_constructor(BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping)


def load_system_definition(source: str | Path) -> MORPHIR:
    """Load a MORPH IR definition from a YAML string or file path."""
    if isinstance(source, Path):
        text = source.read_text(encoding="utf-8")
    else:
        text = source

    data = yaml.load(text, Loader=_UniqueKeySafeLoader)
    if not isinstance(data, dict):
        raise ValueError("MORPH IR definition must be a YAML object.")

    return MORPHIR.from_dict(data)

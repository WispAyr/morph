from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .ir import MORPHIR


def load_system_definition(source: str | Path) -> MORPHIR:
    """Load a MORPH IR definition from a YAML string or file path."""
    if isinstance(source, Path):
        text = source.read_text(encoding="utf-8")
    else:
        text = source

    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("MORPH IR definition must be a YAML object.")

    return MORPHIR.from_dict(data)

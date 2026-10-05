from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class MORPHPolicy:
    name: str
    when: list[dict[str, Any]] = field(default_factory=list)
    result: dict[str, Any] = field(default_factory=dict)


@dataclass
class MORPHDecision:
    name: str
    version: str
    status: str
    action: str
    policy: str | None = None
    reason: str | None = None
    trace: list[str] = field(default_factory=list)

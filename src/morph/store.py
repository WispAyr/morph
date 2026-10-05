"""Append-only event store: the source of truth for a MORPH system.

Every observation, decision, effect attempt and state transition is an event. The current
state of any entity is a fold over its events, so a system can be rebuilt from the log
alone. The in-memory store is the default; give it a path to persist as JSON Lines.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

SYSTEM_STREAM = "system"


def stream_for(entity: str, entity_id: str) -> str:
    return f"{entity}/{entity_id}"


@dataclass(frozen=True)
class Event:
    seq: int
    at: str
    stream: str
    kind: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "at": self.at, "stream": self.stream, "kind": self.kind, "data": self.data}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Event":
        return cls(seq=int(data["seq"]), at=str(data["at"]), stream=str(data["stream"]), kind=str(data["kind"]), data=dict(data.get("data") or {}))


class EventStore:
    """An ordered, append-only sequence of events with optional JSONL persistence."""

    def __init__(self, path: str | Path | None = None):
        self.path = Path(path) if path is not None else None
        self._events: list[Event] = []
        if self.path is not None and self.path.exists():
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        with self.path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = Event.from_dict(json.loads(line))
                except (ValueError, KeyError, TypeError) as exc:
                    raise ValueError(f"{self.path}:{line_number}: malformed event: {exc}") from exc
                expected = len(self._events) + 1
                if event.seq != expected:
                    raise ValueError(f"{self.path}:{line_number}: expected seq {expected}, found {event.seq}")
                self._events.append(event)

    def append(self, stream: str, kind: str, data: dict[str, Any] | None = None) -> Event:
        event = Event(
            seq=len(self._events) + 1,
            at=datetime.now(timezone.utc).isoformat(),
            stream=stream,
            kind=kind,
            data=json.loads(json.dumps(data or {}, default=str)),  # guarantee the event is serialisable
        )
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(event.to_dict(), sort_keys=True) + "\n")
                handle.flush()
        self._events.append(event)
        return event

    def events(self, stream: str | None = None, kinds: Iterable[str] | None = None, after: int = 0) -> list[Event]:
        wanted = set(kinds) if kinds is not None else None
        return [
            event
            for event in self._events
            if event.seq > after
            and (stream is None or event.stream == stream)
            and (wanted is None or event.kind in wanted)
        ]

    def last(self) -> Event | None:
        return self._events[-1] if self._events else None

    def streams(self) -> list[str]:
        return sorted({event.stream for event in self._events})

    def __len__(self) -> int:
        return len(self._events)

    def __iter__(self):
        return iter(list(self._events))

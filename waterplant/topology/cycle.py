"""Cycle records persisted alongside the topology.

Every cycle record pins the exact topology version the cycle started on, so a
mid-life topology change can never retroactively change what a running or
finished cycle meant. Records are an append-only command list; a running
record is filled in when the cycle finishes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from waterplant.store.commands import load_commands
from waterplant.store.store import Store

CYCLES_KEY = "topology:cycles"

STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"


@dataclass(frozen=True)
class CycleRecord:
    """One cycle bound to one topology version."""

    cycle_id: str
    topology_version: int
    topology_name: str
    status: str
    plan: tuple[str, ...]
    completed: tuple[str, ...] = ()
    result: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    started_at: str = ""
    finished_at: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "cycle_id": self.cycle_id,
            "topology_version": self.topology_version,
            "topology_name": self.topology_name,
            "status": self.status,
            "plan": list(self.plan),
            "completed": list(self.completed),
            "result": self.result,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _record_from_payload(payload: dict[str, Any]) -> CycleRecord:
    return CycleRecord(
        cycle_id=str(payload.get("cycle_id", "")),
        topology_version=int(payload.get("topology_version", 0)),
        topology_name=str(payload.get("topology_name", "")),
        status=str(payload.get("status", STATUS_RUNNING)),
        plan=tuple(str(item) for item in payload.get("plan", [])),
        completed=tuple(str(item) for item in payload.get("completed", [])),
        result=dict(payload.get("result", {})),
        error=str(payload.get("error", "")),
        started_at=str(payload.get("started_at", "")),
        finished_at=str(payload.get("finished_at", "")),
    )


def list_cycles(store: Store) -> list[CycleRecord]:
    """Return every well formed cycle record, in execution order."""

    records: list[CycleRecord] = []
    for item in load_commands(store, CYCLES_KEY):
        try:
            payload = json.loads(item)
        except ValueError:
            continue
        if isinstance(payload, dict):
            records.append(_record_from_payload(payload))
    return records


def append_cycle(store: Store, record: CycleRecord) -> None:
    """Append one running/finished record to the cycle history."""

    raw = json.dumps(record.as_dict(), ensure_ascii=False)
    items = load_commands(store, CYCLES_KEY)
    items.append(raw)
    store.put(CYCLES_KEY, json.dumps(items, ensure_ascii=False))


def update_last_cycle(store: Store, record: CycleRecord) -> None:
    """Rewrite the trailing record when a running cycle finishes.

    The list stays append-only as a history; the last element is the record
    opened by the current cycle and is filled in exactly once.
    """

    items = load_commands(store, CYCLES_KEY)
    if not items:
        append_cycle(store, record)
        return
    items[-1] = json.dumps(record.as_dict(), ensure_ascii=False)
    store.put(CYCLES_KEY, json.dumps(items, ensure_ascii=False))

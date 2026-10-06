"""Versioned topology registry with cycle-boundary activation and fallback."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass

from waterplant.audit import Auditor
from waterplant.store.commands import append_command, load_commands
from waterplant.store.store import Store

from .model import Topology, TopologyError, default_topology, parse_topology
from .report import TopologyState

DOCUMENT_KEY = "topology:document"
LAST_GOOD_KEY = "topology:last-good"
CHANGES_KEY = "topology:changes"
CYCLES_KEY = "topology:cycles"
CYCLE_SEQ_KEY = "topology:cycle-seq"

NEXT_CYCLE = "next_cycle"
IMMEDIATE = "immediate"

AUDIT_KIND = "topology"


class ChangeConflict(Exception):
    """Raised when a change id is reused with a different document."""


@dataclass(frozen=True)
class TopologyView:
    """The topology pinned for reading or for one running cycle."""

    topology: Topology
    version: int
    degraded: bool = False
    degraded_reason: str = ""

    def as_dict(self) -> dict[str, object]:
        payload = self.topology.as_dict()
        payload["version"] = self.version
        payload["degraded"] = self.degraded
        payload["degraded_reason"] = self.degraded_reason
        return payload


@dataclass(frozen=True)
class ChangeResult:
    """Outcome of submitting a topology change."""

    change_id: str
    version: int
    status: str
    activation: str
    replay: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "change_id": self.change_id,
            "version": self.version,
            "status": self.status,
            "activation": self.activation,
            "replay": self.replay,
        }


@dataclass(frozen=True)
class CycleRecord:
    """One finished cycle pinned to the topology version it ran under."""

    cycle: int
    topology_version: int
    at: str
    stages: list[str]
    executed: list[str]
    bypassed: list[str]
    degraded: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "cycle": self.cycle,
            "topology_version": self.topology_version,
            "at": self.at,
            "stages": list(self.stages),
            "executed": list(self.executed),
            "bypassed": list(self.bypassed),
            "degraded": self.degraded,
        }


class TopologyRegistry:
    """Keeps the active topology, pending changes and cycle records."""

    def __init__(self, store: Store, auditor: Auditor | None = None) -> None:
        self._store = store
        self._auditor = auditor
        self._last_good: TopologyView | None = None
        self._lock = threading.RLock()

    def active(self) -> TopologyView:
        """Return the effective topology, falling back when the stored one breaks."""

        with self._lock:
            raw, present = self._store.get(DOCUMENT_KEY)
            if present:
                try:
                    view = self._decode_view(raw)
                except (ValueError, KeyError, TypeError) as exc:
                    return self._fallback(f"stored topology unreadable: {exc}")
                self._last_good = view
                return view
            persisted = self._persisted_last_good()
            if persisted is not None:
                return self._fallback("active topology document is missing", persisted)
            return TopologyView(topology=default_topology(), version=0)

    def pending(self) -> dict[str, object] | None:
        with self._lock:
            record = self._pending_record()
            return None if record is None else _public_record(record)

    def changes(self) -> list[dict[str, object]]:
        with self._lock:
            return [_public_record(record) for record in self._change_records()]

    def cycles(self) -> list[dict[str, object]]:
        with self._lock:
            records: list[dict[str, object]] = []
            for item in load_commands(self._store, CYCLES_KEY):
                try:
                    payload = json.loads(item)
                except ValueError:
                    continue
                if isinstance(payload, dict):
                    records.append(payload)
            return records

    def last_cycle(self) -> dict[str, object] | None:
        records = self.cycles()
        return records[-1] if records else None

    def submit_change(self, document: object, activation: str, change_id: str) -> ChangeResult:
        """Validate and register a topology change, idempotent on ``change_id``."""

        with self._lock:
            topology = parse_topology(document)
            digest = _document_digest(document)
            existing = self._find_change(change_id)
            if existing is not None:
                if existing.get("hash") != digest:
                    raise ChangeConflict(
                        f"change {change_id} was already submitted with a different document"
                    )
                return ChangeResult(
                    change_id=change_id,
                    version=int(existing["version"]),
                    status=str(existing["status"]),
                    activation=str(existing["activation"]),
                    replay=True,
                )
            current = self.active()
            already_active = activation == IMMEDIATE and self._active_hash() == digest
            record: dict[str, object] = {
                "change_id": change_id,
                "activation": activation,
                "hash": digest,
                "document": document,
                "submitted_at": _now(),
                "activated_at": "",
                "summary": f"nodes={len(topology.nodes)} edges={len(topology.edges)}",
            }
            if activation == IMMEDIATE:
                record["version"] = current.version if already_active else current.version + 1
                record["status"] = "active"
                record["activated_at"] = _now()
                if not already_active:
                    self._apply(record, topology, int(record["version"]))
                self._append_record(record)
                self._audit("activated", record)
            else:
                record["version"] = current.version + 1
                record["status"] = "pending"
                self._supersede_pending()
                self._append_record(record)
                self._audit("submitted", record)
            return ChangeResult(
                change_id=change_id,
                version=int(record["version"]),
                status=str(record["status"]),
                activation=activation,
                replay=False,
            )

    def begin_cycle(self) -> TopologyView:
        """Activate a pending change at the cycle boundary, then pin the topology."""

        with self._lock:
            record = self._pending_record()
            if record is not None:
                change_id = str(record["change_id"])
                try:
                    topology = parse_topology(record.get("document"))
                except TopologyError as exc:
                    self._update_record(change_id, status="discarded")
                    self._audit_event("discarded", change_id, 0, str(exc))
                    return self.active()
                current = self.active()
                if self._active_hash() == record.get("hash"):
                    # Already in effect (a retry after a crash); close the record.
                    self._update_record(
                        change_id,
                        status="active",
                        version=current.version,
                        activated_at=_now(),
                    )
                else:
                    version = max(int(record["version"]), current.version + 1)
                    self._apply(record, topology, version)
                    self._update_record(
                        change_id, status="active", version=version, activated_at=_now()
                    )
                    self._audit_event("activated", change_id, version, "")
            return self.active()

    def record_cycle(self, view: TopologyView, executed: list[str]) -> CycleRecord:
        """Persist a cycle record pinned to the topology version that ran it."""

        with self._lock:
            sequence = self._cycle_sequence() + 1
            self._store.put(CYCLE_SEQ_KEY, str(sequence))
            record = CycleRecord(
                cycle=sequence,
                topology_version=view.version,
                at=_now(),
                stages=[node.stage.value for node in view.topology.recorded_nodes()],
                executed=list(executed),
                bypassed=[
                    view.topology.node(node_id).stage.value
                    for node_id in view.topology.bypassed_node_ids()
                ],
                degraded=view.degraded,
            )
            append_command(
                self._store, CYCLES_KEY, json.dumps(record.as_dict(), ensure_ascii=False)
            )
            return record

    def state(self) -> TopologyState:
        view = self.active()
        pending = self._pending_record()
        return TopologyState(
            version=view.version,
            name=view.topology.name,
            degraded=view.degraded,
            degraded_reason=view.degraded_reason,
            main_chain=[
                view.topology.node(node_id).stage.value for node_id in view.topology.main_order
            ],
            recorded_stages=[node.stage.value for node in view.topology.recorded_nodes()],
            bypassed_stages=[
                view.topology.node(node_id).stage.value
                for node_id in view.topology.bypassed_node_ids()
            ],
            pending_change="" if pending is None else str(pending.get("change_id", "")),
            cycles_recorded=len(self.cycles()),
        )

    def describe(self) -> str:
        state = self.state()
        pending = state.pending_change or "none"
        return (
            f"topology version={state.version} stages={len(state.main_chain)} "
            f"degraded={str(state.degraded).lower()} pending={pending}"
        )

    def _apply(self, record: dict[str, object], topology: Topology, version: int) -> None:
        envelope = json.dumps(
            {"version": version, "hash": record["hash"], "document": record["document"]},
            ensure_ascii=False,
            sort_keys=True,
        )
        self._store.put(DOCUMENT_KEY, envelope)
        self._store.put(LAST_GOOD_KEY, envelope)
        self._last_good = TopologyView(topology=topology, version=version)

    def _fallback(self, reason: str, view: TopologyView | None = None) -> TopologyView:
        cached = view or self._last_good or self._persisted_last_good()
        if cached is None:
            cached = TopologyView(topology=default_topology(), version=0)
        return TopologyView(
            topology=cached.topology,
            version=cached.version,
            degraded=True,
            degraded_reason=reason,
        )

    def _decode_view(self, raw: str) -> TopologyView:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TopologyError("stored topology envelope must be an object")
        version = int(payload["version"])
        topology = parse_topology(payload.get("document"))
        return TopologyView(topology=topology, version=version)

    def _persisted_last_good(self) -> TopologyView | None:
        raw, present = self._store.get(LAST_GOOD_KEY)
        if not present:
            return None
        try:
            return self._decode_view(raw)
        except (ValueError, KeyError, TypeError):
            return None

    def _active_hash(self) -> str:
        raw, present = self._store.get(DOCUMENT_KEY)
        if not present:
            return ""
        try:
            payload = json.loads(raw)
        except ValueError:
            return ""
        if not isinstance(payload, dict):
            return ""
        return str(payload.get("hash", ""))

    def _cycle_sequence(self) -> int:
        raw, present = self._store.get(CYCLE_SEQ_KEY)
        if not present:
            return 0
        try:
            return int(raw)
        except ValueError:
            return 0

    def _change_records(self) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        for item in load_commands(self._store, CHANGES_KEY):
            try:
                payload = json.loads(item)
            except ValueError:
                continue
            if isinstance(payload, dict):
                records.append(payload)
        return records

    def _write_records(self, records: list[dict[str, object]]) -> None:
        encoded = [json.dumps(record, ensure_ascii=False) for record in records]
        self._store.put(CHANGES_KEY, json.dumps(encoded, ensure_ascii=False))

    def _append_record(self, record: dict[str, object]) -> None:
        self._append_record_raw(json.dumps(record, ensure_ascii=False))

    def _append_record_raw(self, encoded: str) -> None:
        append_command(self._store, CHANGES_KEY, encoded)

    def _update_record(self, change_id: str, **fields: object) -> None:
        records = self._change_records()
        for record in records:
            if record.get("change_id") == change_id:
                record.update(fields)
        self._write_records(records)

    def _find_change(self, change_id: str) -> dict[str, object] | None:
        for record in self._change_records():
            if record.get("change_id") == change_id:
                return record
        return None

    def _pending_record(self) -> dict[str, object] | None:
        for record in self._change_records():
            if record.get("status") == "pending":
                return record
        return None

    def _supersede_pending(self) -> None:
        pending = self._pending_record()
        if pending is None:
            return
        change_id = str(pending["change_id"])
        self._update_record(change_id, status="superseded")
        self._audit_event("superseded", change_id, int(pending["version"]), "")

    def _audit(self, event: str, record: dict[str, object]) -> None:
        self._audit_event(event, str(record["change_id"]), int(record["version"]), "")

    def _audit_event(self, event: str, change_id: str, version: int, note: str) -> None:
        if self._auditor is None:
            return
        detail: dict[str, object] = {
            "event": event,
            "change_id": change_id,
            "version": version,
        }
        if note:
            detail["note"] = note
        self._auditor.record(
            AUDIT_KIND, json.dumps(detail, ensure_ascii=False, sort_keys=True)
        )


def _public_record(record: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in record.items() if key != "document"}


def _document_digest(document: object) -> str:
    canonical = json.dumps(document, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

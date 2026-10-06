"""Owns topology versions, staged changes and the cycle/topology timing.

Timing model
------------
A submitted change is either staged for the *next* cycle or applied
immediately; the effect is part of the running arrangement and travels with
the change request. A cycle pins the topology version it starts on: the plan,
the gate and the cycle record all keep that snapshot, so a change that lands
while a cycle is running can never disturb it.

Read failures
-------------
Reloading the topology is a *partial* failure. The last known-good topology
keeps running, the service is marked degraded (and the console shows why), and
an audit entry is written. Once reads succeed again the degraded mark is
cleared with one recovery entry; repeated refresh triggers are idempotent.
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable

from waterplant.audit import Auditor

from .cycle import (
    CYCLES_KEY,
    STATUS_COMPLETE,
    STATUS_FAILED,
    STATUS_RUNNING,
    CycleRecord,
    append_cycle,
    list_cycles,
    update_last_cycle,
)
from .model import (
    DEFAULT_POLICY,
    EFFECTS,
    EFFECT_IMMEDIATE,
    EFFECT_NEXT_CYCLE,
    Topology,
    topology_from_dict,
    topology_to_json,
)
from .order import StageGate

ACTIVE_KEY = "topology:active"
STAGED_KEY = "topology:staged"
STATUS_KEY = "topology:status"

AUDIT_CHANGE = "topology-change"
AUDIT_STAGED = "topology-staged"
AUDIT_ACTIVATED = "topology-activated"
AUDIT_DEGRADED = "topology-degraded"
AUDIT_RECOVERED = "topology-recovered"
AUDIT_CYCLE = "topology-cycle"


class CycleInProgress(RuntimeError):
    """Raised when a cycle is opened while another is still running."""


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


@dataclass(frozen=True)
class SubmitOutcome:
    """Result of a topology submission."""

    applied: bool
    idempotent: bool
    effect: str
    active_version: int
    staged: bool
    deferred: bool
    topology: Topology

    def as_dict(self) -> dict[str, object]:
        return {
            "applied": self.applied,
            "idempotent": self.idempotent,
            "effect": self.effect,
            "active_version": self.active_version,
            "staged": self.staged,
            "deferred": self.deferred,
            "topology": self.topology.as_dict(),
        }


@dataclass
class CycleContext:
    """Everything a running cycle is pinned to for its whole lifetime."""

    cycle_id: str
    topology: Topology
    version: int
    gate: StageGate
    record: CycleRecord
    finished: bool = False

    def note_finished(
        self,
        status: str,
        result: dict[str, object],
        error: str,
        completed: list[str],
        at: str,
    ) -> CycleRecord:
        return CycleRecord(
            cycle_id=self.record.cycle_id,
            topology_version=self.record.topology_version,
            topology_name=self.record.topology_name,
            status=status,
            plan=self.record.plan,
            completed=tuple(completed),
            result=dict(result),
            error=error,
            started_at=self.record.started_at,
            finished_at=at,
        )


Reader = Callable[[], str | None]
"""Returns the raw active-topology document, or None when nothing is stored."""


class TopologyManager:
    """Loads, versions and activates topologies and opens/closes cycles."""

    def __init__(
        self,
        store,
        auditor: Auditor | None = None,
        reader: Reader | None = None,
        default_effect: str = DEFAULT_POLICY,
    ) -> None:
        self._store = store
        self._auditor = auditor
        self._reader: Reader = reader or self._read_from_store
        if default_effect not in EFFECTS:
            raise ValueError(f"unknown topology effect {default_effect!r}")
        self._default_effect = default_effect

        self._active: Topology | None = None
        self._active_version = 0
        self._staged: Topology | None = None
        self._staged_effect = EFFECT_NEXT_CYCLE
        self._staged_by = ""
        self._degraded = False
        self._degraded_reason = ""
        self._degraded_since = ""
        self._running: CycleContext | None = None
        self._lock = threading.RLock()

        self._bootstrap()

    # ------------------------------------------------------------- loading
    def _read_from_store(self) -> str | None:
        raw, present = self._store.get(ACTIVE_KEY)
        return raw if present else None

    def _bootstrap(self) -> None:
        """Load the persisted topology once, falling back to the built-in."""

        try:
            raw = self._reader()
        except Exception as exc:  # noqa: BLE001 - any read fault is a partial failure
            raw = None
            boot_failure = str(exc)
        else:
            boot_failure = ""

        if raw is not None:
            topology, version = self._parse_envelope(raw)
            if topology is not None:
                self._adopt(topology, version)
                self._load_staged()
                self._recover_interrupted_cycles()
                return
            boot_failure = boot_failure or "stored topology document is unreadable"

        # First boot, or an unreadable document: seed/keep the built-in line.
        from .model import default_topology

        fallback = default_topology()
        if boot_failure:
            self._active = fallback
            self._active_version = 1
            self._mark_degraded(boot_failure)
        else:
            self._active = fallback
            self._active_version = 1
            self._persist_active(fallback, 1, effect=EFFECT_NEXT_CYCLE, by="bootstrap")
        self._recover_interrupted_cycles()

    def _recover_interrupted_cycles(self) -> None:
        """Mark cycles left ``running`` by a crashed process as interrupted."""

        records = list_cycles(self._store)
        changed = False
        for index, record in enumerate(records):
            if record.status != STATUS_RUNNING:
                continue
            records[index] = CycleRecord(
                cycle_id=record.cycle_id,
                topology_version=record.topology_version,
                topology_name=record.topology_name,
                status=STATUS_FAILED,
                plan=record.plan,
                completed=record.completed,
                result=record.result,
                error="interrupted by restart",
                started_at=record.started_at,
                finished_at=utc_now(),
            )
            changed = True
        if changed:
            raw = json.dumps(
                [json.dumps(record.as_dict(), ensure_ascii=False) for record in records],
                ensure_ascii=False,
            )
            self._store.put(CYCLES_KEY, raw)

    def _parse_envelope(self, raw: str) -> tuple[Topology | None, int]:
        try:
            envelope = json.loads(raw)
        except ValueError:
            return None, 0
        if not isinstance(envelope, dict) or not isinstance(envelope.get("document"), dict):
            return None, 0
        try:
            topology = topology_from_dict(envelope["document"])
        except ValueError:
            return None, 0
        try:
            version = int(envelope.get("version", 0))
        except (TypeError, ValueError):
            version = 0
        return topology, version

    def _adopt(self, topology: Topology, version: int) -> None:
        self._active = topology
        self._active_version = max(version, 1)

    def _load_staged(self) -> None:
        raw, present = self._store.get(STAGED_KEY)
        if not present:
            return
        try:
            envelope = json.loads(raw)
        except ValueError:
            return
        if not isinstance(envelope, dict) or not isinstance(envelope.get("document"), dict):
            return
        try:
            self._staged = topology_from_dict(envelope["document"])
        except ValueError:
            return
        effect = str(envelope.get("effect", EFFECT_NEXT_CYCLE))
        self._staged_effect = effect if effect in EFFECTS else EFFECT_NEXT_CYCLE
        self._staged_by = str(envelope.get("requested_by", ""))

    # ------------------------------------------------------------- readings
    def current(self) -> Topology:
        assert self._active is not None  # bootstrapped in __init__
        return self._active

    def version(self) -> int:
        return self._active_version

    def staged(self) -> Topology | None:
        return self._staged

    def default_effect(self) -> str:
        return self._default_effect

    def is_degraded(self) -> bool:
        return self._degraded

    def degraded_reason(self) -> str:
        return self._degraded_reason

    def running_cycle(self) -> CycleContext | None:
        return self._running

    def status(self) -> dict[str, object]:
        return {
            "active_version": self._active_version,
            "active_name": self.current().name,
            "staged": self._staged is not None,
            "staged_effect": self._staged_effect if self._staged is not None else "",
            "running_cycle": "" if self._running is None else self._running.cycle_id,
            "degraded": self._degraded,
            "degraded_reason": self._degraded_reason,
            "degraded_since": self._degraded_since,
            "plan": [node.as_dict() for node in self.current().plan()],
        }

    def cycles(self) -> list[CycleRecord]:
        return list_cycles(self._store)

    # ------------------------------------------------------------ auditing
    def _audit(self, kind: str, detail: dict[str, object]) -> None:
        if self._auditor is None:
            return
        self._auditor.record(kind, json.dumps(detail, ensure_ascii=False, sort_keys=True))

    def _mark_degraded(self, reason: str) -> None:
        if self._degraded:
            # Repeated failures keep the original annotation; no audit noise.
            return
        self._degraded = True
        self._degraded_reason = reason
        self._degraded_since = utc_now()
        self._persist_status()
        self._audit(
            AUDIT_DEGRADED,
            {"reason": reason, "retained_version": self._active_version},
        )

    def _clear_degraded(self) -> bool:
        if not self._degraded:
            return False
        self._degraded = False
        self._degraded_reason = ""
        self._degraded_since = ""
        self._persist_status()
        self._audit(AUDIT_RECOVERED, {"active_version": self._active_version})
        return True

    def _persist_status(self) -> None:
        payload = {
            "degraded": self._degraded,
            "reason": self._degraded_reason,
            "since": self._degraded_since,
        }
        self._store.put(STATUS_KEY, json.dumps(payload, ensure_ascii=False))

    # ----------------------------------------------------------- reloading
    def refresh(self) -> dict[str, object]:
        """Reload the active topology through the read boundary.

        Idempotent: repeated failures and repeated healthy reads leave the
        version and the audit trail untouched.
        """

        with self._lock:
            try:
                raw = self._reader()
            except Exception as exc:  # noqa: BLE001 - read boundary keeps last valid
                self._mark_degraded(f"topology read failed: {exc}")
                return self.status()

            if raw is None:
                # Source has no document; the in-memory last-good topology stands.
                self._mark_degraded("topology source returned no document")
                return self.status()

            topology, version = self._parse_envelope(raw)
            if topology is None:
                self._mark_degraded("topology document is invalid")
                return self.status()

            fingerprint = topology_to_json(topology)
            active_fingerprint = topology_to_json(self.current())
            recovered = self._clear_degraded()
            if fingerprint != active_fingerprint or version != self._active_version:
                self._adopt(topology, version)
                if not recovered:
                    self._audit(
                        AUDIT_CHANGE,
                        {"detected": "external", "active_version": self._active_version},
                    )
            return self.status()

    # ----------------------------------------------------------- mutation
    def _persist_active(self, topology: Topology, version: int, effect: str, by: str) -> None:
        envelope = {
            "version": version,
            "effect": effect,
            "activated_by": by,
            "activated_at": utc_now(),
            "document": topology.as_dict(),
        }
        self._store.put(ACTIVE_KEY, json.dumps(envelope, ensure_ascii=False, sort_keys=True))

    def _persist_staged(self, topology: Topology, effect: str, by: str) -> None:
        envelope = {
            "effect": effect,
            "requested_by": by,
            "created_at": utc_now(),
            "document": topology.as_dict(),
        }
        self._store.put(STAGED_KEY, json.dumps(envelope, ensure_ascii=False, sort_keys=True))

    def _promote(self, topology: Topology, effect: str, by: str) -> int:
        self._active_version += 1
        self._active = topology
        self._persist_active(topology, self._active_version, effect, by)
        self._store.delete(STAGED_KEY)
        self._staged = None
        self._staged_effect = EFFECT_NEXT_CYCLE
        self._staged_by = ""
        return self._active_version

    def submit(
        self,
        document: dict[str, object],
        *,
        effect: str | None = None,
        requested_by: str = "",
    ) -> SubmitOutcome:
        """Validate and stage or activate a topology change.

        Re-submitting the identical already-active or already-staged document
        is an idempotent no-op.
        """

        with self._lock:
            return self._submit_locked(document, effect=effect, requested_by=requested_by)

    def _submit_locked(
        self,
        document: dict[str, object],
        *,
        effect: str | None,
        requested_by: str,
    ) -> SubmitOutcome:

        topology = topology_from_dict(document)  # raises ValueError
        chosen = effect or self._default_effect
        if chosen not in EFFECTS:
            raise ValueError(f"effect must be one of {', '.join(EFFECTS)}")

        fingerprint = topology_to_json(topology)
        if fingerprint == topology_to_json(self.current()):
            # Resubmitting the active document never disturbs a pending change.
            return SubmitOutcome(
                applied=False,
                idempotent=True,
                effect=chosen,
                active_version=self._active_version,
                staged=False,
                deferred=False,
                topology=topology,
            )
        if self._staged is not None and fingerprint == topology_to_json(self._staged):
            return SubmitOutcome(
                applied=False,
                idempotent=True,
                effect=self._staged_effect,
                active_version=self._active_version,
                staged=True,
                deferred=self._running is not None,
                topology=topology,
            )

        if chosen == EFFECT_IMMEDIATE and self._running is None:
            previous = self._active_version
            version = self._promote(topology, EFFECT_IMMEDIATE, requested_by)
            self._audit(
                AUDIT_CHANGE,
                {"from_version": previous, "to_version": version, "effect": EFFECT_IMMEDIATE},
            )
            return SubmitOutcome(
                applied=True,
                idempotent=False,
                effect=EFFECT_IMMEDIATE,
                active_version=version,
                staged=False,
                deferred=False,
                topology=topology,
            )

        # Either the arrangement waits for a cycle boundary, or an immediate
        # request landed while a cycle is running: hold it until that boundary
        # so the running cycle keeps its pinned version.
        deferred = chosen == EFFECT_IMMEDIATE and self._running is not None
        held_effect = EFFECT_IMMEDIATE if deferred else EFFECT_NEXT_CYCLE
        self._staged = topology
        self._staged_effect = held_effect
        self._staged_by = requested_by
        self._persist_staged(topology, held_effect, requested_by)
        self._audit(
            AUDIT_STAGED,
            {
                "active_version": self._active_version,
                "next_version": self._active_version + 1,
                "effect": held_effect,
                "deferred": deferred,
            },
        )
        return SubmitOutcome(
            applied=True,
            idempotent=False,
            effect=held_effect,
            active_version=self._active_version,
            staged=True,
            deferred=deferred,
            topology=topology,
        )

    # ------------------------------------------------------------ cycles
    def begin_cycle(self) -> CycleContext:
        """Open a cycle on the freshest valid topology.

        A staged change is promoted first, so it takes effect exactly at the
        next cycle boundary. Overlapping cycles are rejected.
        """

        with self._lock:
            return self._begin_cycle_locked()

    def _begin_cycle_locked(self) -> CycleContext:
        if self._running is not None:
            raise CycleInProgress(f"cycle {self._running.cycle_id} is still running")

        self.refresh()

        if self._staged is not None:
            staged = self._staged
            by = self._staged_by
            held_effect = self._staged_effect
            previous = self._active_version
            version = self._promote(staged, held_effect, by or "cycle-boundary")
            self._audit(
                AUDIT_ACTIVATED,
                {"from_version": previous, "to_version": version, "effect": held_effect},
            )

        topology = self.current()
        records = list_cycles(self._store)
        sequence = 1
        for previous in records:
            prefix = "cycle-"
            if previous.cycle_id.startswith(prefix):
                head = previous.cycle_id.split("-", 2)[1]
                if head.isdigit():
                    sequence = max(sequence, int(head) + 1)
        cycle_id = f"cycle-{sequence}-{uuid.uuid4().hex[:8]}"
        plan_ids = tuple(node.node_id for node in topology.plan())
        record = CycleRecord(
            cycle_id=cycle_id,
            topology_version=self._active_version,
            topology_name=topology.name,
            status=STATUS_RUNNING,
            plan=plan_ids,
            started_at=utc_now(),
        )
        append_cycle(self._store, record)
        context = CycleContext(
            cycle_id=cycle_id,
            topology=topology,
            version=self._active_version,
            gate=StageGate(topology),
            record=record,
        )
        self._running = context
        return context

    def finish_cycle(
        self,
        context: CycleContext,
        *,
        ok: bool,
        result: dict[str, object] | None = None,
        error: str = "",
    ) -> CycleRecord:
        """Close the cycle record under the version it actually ran on.

        Idempotent: finishing an already finished context returns the stored
        record and never writes a second history entry.
        """

        with self._lock:
            return self._finish_cycle_locked(context, ok=ok, result=result, error=error)

    def _finish_cycle_locked(
        self,
        context: CycleContext,
        *,
        ok: bool,
        result: dict[str, object] | None,
        error: str,
    ) -> CycleRecord:
        if context is not self._running:
            if context.finished:
                return context.record
            raise RuntimeError("cycle context is not the active running cycle")
        if context.finished:
            return context.record

        status = STATUS_COMPLETE if ok else STATUS_FAILED
        completed = context.gate.completed()
        record = context.note_finished(status, result or {}, error, completed, utc_now())
        update_last_cycle(self._store, record)
        context.record = record
        context.finished = True
        self._running = None
        self._audit(
            AUDIT_CYCLE,
            {
                "cycle_id": record.cycle_id,
                "topology_version": record.topology_version,
                "status": status,
            },
        )
        return record

"""Topology projections for the console."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TopologyState:
    """Active topology summary plus pending change and cycle counters."""

    version: int
    name: str
    degraded: bool
    degraded_reason: str
    main_chain: list[str]
    recorded_stages: list[str]
    bypassed_stages: list[str]
    pending_change: str
    cycles_recorded: int

    def as_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "name": self.name,
            "degraded": self.degraded,
            "degraded_reason": self.degraded_reason,
            "main_chain": list(self.main_chain),
            "recorded_stages": list(self.recorded_stages),
            "bypassed_stages": list(self.bypassed_stages),
            "pending_change": self.pending_change,
            "cycles_recorded": self.cycles_recorded,
        }

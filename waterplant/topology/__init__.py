"""Configurable treatment line topology with versioned activation."""

from .model import (
    Edge,
    Node,
    NodeKind,
    Topology,
    TopologyError,
    default_document,
    default_topology,
    parse_topology,
)
from .registry import (
    IMMEDIATE,
    NEXT_CYCLE,
    ChangeConflict,
    ChangeResult,
    CycleRecord,
    TopologyRegistry,
    TopologyView,
)
from .report import TopologyState

__all__ = [
    "IMMEDIATE",
    "NEXT_CYCLE",
    "ChangeConflict",
    "ChangeResult",
    "CycleRecord",
    "Edge",
    "Node",
    "NodeKind",
    "Topology",
    "TopologyError",
    "TopologyRegistry",
    "TopologyState",
    "TopologyView",
    "default_document",
    "default_topology",
    "parse_topology",
]

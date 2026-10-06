"""Configurable treatment line topology.

The line is described as data (an ordered main line plus optional branches and
bypasses) instead of being hard coded into the control cycle. The package also
owns topology validation, runtime order enforcement, versioned activation and
the cycle records that bind a running cycle to one concrete topology version.
"""

from .cycle import CYCLES_KEY, CycleRecord, append_cycle, list_cycles, update_last_cycle
from .manager import CycleInProgress, TopologyManager
from .model import (
    BYPASS_PREFIX,
    DEFAULT_POLICY,
    EFFECT_IMMEDIATE,
    EFFECT_NEXT_CYCLE,
    Branch,
    Bypass,
    OrderRule,
    PlanNode,
    Topology,
    default_topology,
    topology_from_dict,
    topology_to_json,
)
from .order import OrderError, StageGate

__all__ = [
    "BYPASS_PREFIX",
    "DEFAULT_POLICY",
    "EFFECT_IMMEDIATE",
    "EFFECT_NEXT_CYCLE",
    "Branch",
    "Bypass",
    "CYCLES_KEY",
    "CycleInProgress",
    "CycleRecord",
    "OrderError",
    "OrderRule",
    "PlanNode",
    "StageGate",
    "Topology",
    "TopologyManager",
    "append_cycle",
    "default_topology",
    "list_cycles",
    "topology_from_dict",
    "topology_to_json",
    "update_last_cycle",
]

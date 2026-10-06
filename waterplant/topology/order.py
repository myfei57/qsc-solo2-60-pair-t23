"""Runtime enforcement of the planned stage order.

Validation at submission time proves a topology is internally consistent; the
gate proves the running cycle actually performs the walk in plan order. A
stage reported before every node that precedes it has finished is rejected up
front instead of corrupting factory verdicts and record order.
"""

from __future__ import annotations

from .model import PlanNode, Topology


class OrderError(RuntimeError):
    """Raised when a cycle reports a node out of the planned order."""


class StageGate:
    """Tracks which plan nodes have completed and rejects out of order work."""

    def __init__(self, topology: Topology) -> None:
        self._plan = topology.plan()
        self._completed: set[str] = set()
        self._main_plan = [node for node in self._plan if node.kind == "main"]

    def nodes(self) -> list[PlanNode]:
        return list(self._plan)

    def node_ids(self) -> list[str]:
        return [node.node_id for node in self._plan]

    def node(self, node_id: str) -> PlanNode | None:
        for candidate in self._plan:
            if candidate.node_id == node_id:
                return candidate
        return None

    def completed(self) -> list[str]:
        return [node.node_id for node in self._plan if node.node_id in self._completed]

    def check(self, node_id: str) -> PlanNode:
        """Validate that ``node_id`` may run now; return its plan node.

        Unknown nodes, already completed nodes and nodes whose predecessors
        have not finished are all reported as order violations.
        """

        node = self.node(node_id)
        if node is None:
            raise OrderError(f"node {node_id} is not part of the current topology")
        if node_id in self._completed:
            raise OrderError(f"node {node_id} already completed in this cycle")
        for candidate in self._plan[: node.index]:
            if candidate.node_id not in self._completed:
                raise OrderError(
                    f"node {node_id} reported before {candidate.node_id}; "
                    "stage order violated"
                )
        return node

    def mark(self, node_id: str) -> PlanNode:
        node = self.check(node_id)
        self._completed.add(node_id)
        return node

    def remaining(self) -> list[PlanNode]:
        return [node for node in self._plan if node.node_id not in self._completed]

    def pending_main(self) -> list[PlanNode]:
        """Main line stages not completed (a bypassed stage is absent entirely)."""

        return [node for node in self._main_plan if node.node_id not in self._completed]

    def assert_main_prefix(self, stages: list[str]) -> None:
        """Assert the completed main stages match the start of the main walk."""

        planned = [node.node_id for node in self._main_plan]
        if stages != planned[: len(stages)]:
            raise OrderError("reported main stages are not a prefix of the planned line")

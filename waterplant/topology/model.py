"""Configurable treatment line topology: main chain, branches and bypasses."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from waterplant.ns import Stage, treatment_line


class TopologyError(ValueError):
    """Raised when a topology document fails validation."""


class NodeKind(str, Enum):
    """How a node takes part in the line."""

    MAIN = "main"
    BRANCH = "branch"
    BYPASS = "bypass"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class Node:
    """One stage occurrence on the line."""

    id: str
    stage: Stage
    kind: NodeKind
    counts_as_stage: bool
    enabled: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "stage": self.stage.value,
            "kind": self.kind.value,
            "counts_as_stage": self.counts_as_stage,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class Edge:
    """A directed flow link between two nodes."""

    upstream: str
    downstream: str

    def as_dict(self) -> dict[str, str]:
        return {"from": self.upstream, "to": self.downstream}


@dataclass(frozen=True)
class Topology:
    """A validated line layout: one main chain plus branch and bypass nodes."""

    name: str
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    main_order: tuple[str, ...]
    bypass_spans: dict[str, tuple[str, str]]

    def node(self, node_id: str) -> Node:
        for candidate in self.nodes:
            if candidate.id == node_id:
                return candidate
        raise KeyError(node_id)

    def main_chain(self) -> list[Node]:
        return [self.node(node_id) for node_id in self.main_order]

    def bypasses(self) -> list[Node]:
        return [node for node in self.nodes if node.kind is NodeKind.BYPASS]

    def bypassed_node_ids(self) -> dict[str, str]:
        """Map each bypassed main node id to the enabled bypass that skips it."""

        positions = {node_id: index for index, node_id in enumerate(self.main_order)}
        skipped: dict[str, str] = {}
        for bypass_id, (entry, exit_) in self.bypass_spans.items():
            if not self.node(bypass_id).enabled:
                continue
            for node_id in self.main_order[positions[entry] + 1 : positions[exit_]]:
                skipped.setdefault(node_id, bypass_id)
        return skipped

    def execution_nodes(self) -> list[Node]:
        """Nodes the cycle walks, in order, with bypassed main stages removed."""

        skipped = self.bypassed_node_ids()
        order: list[Node] = []
        for node in self.main_chain():
            if node.id in skipped:
                continue
            order.append(node)
            order.extend(self._branches_after(node.id))
        return order

    def recorded_nodes(self) -> list[Node]:
        """Nodes that count as stages for the cycle record, in process order."""

        skipped = self.bypassed_node_ids()
        emitted: set[str] = set()
        order: list[Node] = []
        for node in self.main_chain():
            bypass_id = skipped.get(node.id)
            if bypass_id is not None:
                bypass = self.node(bypass_id)
                if bypass.counts_as_stage and bypass_id not in emitted:
                    order.append(bypass)
                    emitted.add(bypass_id)
                continue
            if node.counts_as_stage:
                order.append(node)
            for branch in self._branches_after(node.id):
                if branch.counts_as_stage:
                    order.append(branch)
        return order

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "nodes": [node.as_dict() for node in self.nodes],
            "edges": [edge.as_dict() for edge in self.edges],
            "main_chain": list(self.main_order),
            "execution_stages": [node.stage.value for node in self.execution_nodes()],
            "recorded_stages": [node.stage.value for node in self.recorded_nodes()],
            "bypasses": [self._bypass_dict(bypass) for bypass in self.bypasses()],
        }

    def _branches_after(self, main_id: str) -> list[Node]:
        attached: list[Node] = []
        for edge in self.edges:
            if edge.upstream != main_id:
                continue
            candidate = self.node(edge.downstream)
            if candidate.kind is NodeKind.BRANCH and candidate.enabled:
                attached.append(candidate)
        return sorted(attached, key=lambda node: node.id)

    def _bypass_dict(self, bypass: Node) -> dict[str, object]:
        entry, exit_ = self.bypass_spans.get(bypass.id, ("", ""))
        positions = {node_id: index for index, node_id in enumerate(self.main_order)}
        skips: list[str] = []
        if entry and exit_:
            skips = [
                self.node(node_id).stage.value
                for node_id in self.main_order[positions[entry] + 1 : positions[exit_]]
            ]
        return {
            "id": bypass.id,
            "enabled": bypass.enabled,
            "counts_as_stage": bypass.counts_as_stage,
            "entry": entry,
            "exit": exit_,
            "skips": skips,
        }


DEFAULT_MAIN_STAGES: tuple[Stage, ...] = (
    Stage.INTAKE,
    Stage.COAG,
    Stage.TURBIDITY,
    Stage.CHLOR,
    Stage.FILTER,
    Stage.CLEARWELL,
    Stage.BACKWASH,
    Stage.QUOTA,
    Stage.AUDIT,
)


def default_document() -> dict[str, object]:
    """The built-in topology that mirrors the historical fixed line."""

    nodes = [
        {"id": stage.value, "stage": stage.value, "kind": NodeKind.MAIN.value}
        for stage in DEFAULT_MAIN_STAGES
    ]
    edges = [
        {"from": earlier.value, "to": later.value}
        for earlier, later in zip(DEFAULT_MAIN_STAGES, DEFAULT_MAIN_STAGES[1:])
    ]
    return {"name": "treatment", "nodes": nodes, "edges": edges}


def default_topology() -> Topology:
    return parse_topology(default_document())


def parse_topology(document: object) -> Topology:
    """Validate a topology document and return the derived topology."""

    if not isinstance(document, dict):
        raise TopologyError("topology document must be an object")
    name = document.get("name", "treatment")
    if not isinstance(name, str) or not name.strip():
        raise TopologyError("topology name must be a non-empty string")
    raw_nodes = document.get("nodes")
    if not isinstance(raw_nodes, list) or not raw_nodes:
        raise TopologyError("topology document needs a non-empty nodes list")
    nodes = [_parse_node(item) for item in raw_nodes]
    by_id: dict[str, Node] = {}
    for node in nodes:
        if node.id in by_id:
            raise TopologyError(f"duplicate node id: {node.id}")
        by_id[node.id] = node
    raw_edges = document.get("edges", [])
    if not isinstance(raw_edges, list):
        raise TopologyError("topology edges must be a list")
    edges = _parse_edges(raw_edges, by_id)
    main_order = _main_chain(by_id, edges)
    _check_required_stages(by_id, main_order)
    bypass_spans = _check_attachments(by_id, edges, main_order)
    _check_acyclic(by_id, edges)
    return Topology(
        name=name.strip(),
        nodes=tuple(nodes),
        edges=tuple(edges),
        main_order=tuple(main_order),
        bypass_spans=bypass_spans,
    )


def _parse_node(item: object) -> Node:
    if not isinstance(item, dict):
        raise TopologyError("each node must be an object")
    stage_raw = item.get("stage")
    try:
        stage = Stage(str(stage_raw))
    except ValueError as exc:
        raise TopologyError(f"unknown stage: {stage_raw}") from exc
    node_id = str(item.get("id") or stage.value)
    kind_raw = str(item.get("kind", NodeKind.MAIN.value))
    try:
        kind = NodeKind(kind_raw)
    except ValueError as exc:
        raise TopologyError(f"node {node_id} has unknown kind: {kind_raw}") from exc
    counts_as_stage = _flag(item, "counts_as_stage", kind is not NodeKind.BYPASS, node_id)
    enabled = _flag(item, "enabled", True, node_id)
    if kind is NodeKind.MAIN and not enabled:
        raise TopologyError(
            f"main node {node_id} cannot be disabled; add a bypass to route around it"
        )
    return Node(
        id=node_id,
        stage=stage,
        kind=kind,
        counts_as_stage=counts_as_stage,
        enabled=enabled,
    )


def _flag(item: dict[str, object], key: str, default: bool, node_id: str) -> bool:
    value = item.get(key, default)
    if not isinstance(value, bool):
        raise TopologyError(f"node {node_id} field {key} must be a boolean")
    return value


def _parse_edges(raw_edges: list[object], by_id: dict[str, Node]) -> list[Edge]:
    edges: list[Edge] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_edges:
        if not isinstance(item, dict):
            raise TopologyError("each edge must be an object")
        upstream = str(item.get("from", ""))
        downstream = str(item.get("to", ""))
        for endpoint in (upstream, downstream):
            if endpoint not in by_id:
                raise TopologyError(f"edge references unknown node: {endpoint}")
        if upstream == downstream:
            raise TopologyError(f"edge loops back to node {upstream}")
        if (upstream, downstream) in seen:
            raise TopologyError(f"duplicate edge from {upstream} to {downstream}")
        seen.add((upstream, downstream))
        edges.append(Edge(upstream=upstream, downstream=downstream))
    return edges


def _main_chain(by_id: dict[str, Node], edges: list[Edge]) -> list[str]:
    main_ids = [node.id for node in by_id.values() if node.kind is NodeKind.MAIN]
    if not main_ids:
        raise TopologyError("topology needs at least one main node")
    main_set = set(main_ids)
    predecessors: dict[str, list[str]] = {node_id: [] for node_id in main_ids}
    successors: dict[str, list[str]] = {node_id: [] for node_id in main_ids}
    for edge in edges:
        if edge.upstream in main_set and edge.downstream in main_set:
            successors[edge.upstream].append(edge.downstream)
            predecessors[edge.downstream].append(edge.upstream)
    sources = [node_id for node_id in main_ids if not predecessors[node_id]]
    sinks = [node_id for node_id in main_ids if not successors[node_id]]
    if len(sources) != 1 or len(sinks) != 1:
        raise TopologyError("main nodes must form a single chain with one start and one end")
    for node_id in main_ids:
        if node_id != sources[0] and len(predecessors[node_id]) != 1:
            raise TopologyError(f"main node {node_id} must have exactly one main predecessor")
        if node_id != sinks[0] and len(successors[node_id]) != 1:
            raise TopologyError(f"main node {node_id} must have exactly one main successor")
    order: list[str] = []
    current = sources[0]
    while True:
        order.append(current)
        following = successors[current]
        if not following:
            break
        current = following[0]
        if current in order:
            raise TopologyError("main chain loops back on itself")
    if len(order) != len(main_ids):
        raise TopologyError("main nodes must form a single connected chain")
    return order


def _check_required_stages(by_id: dict[str, Node], main_order: list[str]) -> None:
    required = list(treatment_line().stages)
    main_stages = [by_id[node_id].stage for node_id in main_order]
    missing = [stage.value for stage in required if stage not in main_stages]
    if missing:
        raise TopologyError("missing required stages: " + ", ".join(missing))
    for stage in required:
        if main_stages.count(stage) > 1:
            raise TopologyError(f"stage {stage.value} appears more than once on the main chain")
    for earlier, later in zip(required, required[1:]):
        if main_stages.index(later) < main_stages.index(earlier):
            raise TopologyError(
                f"stage {later.value} must come after {earlier.value} on the main chain"
            )
    for node_id in main_order:
        node = by_id[node_id]
        if node.stage in required and not node.counts_as_stage:
            raise TopologyError(f"required stage {node.stage.value} must count as a stage")


def _check_attachments(
    by_id: dict[str, Node], edges: list[Edge], main_order: list[str]
) -> dict[str, tuple[str, str]]:
    main_set = set(main_order)
    positions = {node_id: index for index, node_id in enumerate(main_order)}
    incoming: dict[str, list[str]] = {}
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        incoming.setdefault(edge.downstream, []).append(edge.upstream)
        outgoing.setdefault(edge.upstream, []).append(edge.downstream)
    spans: dict[str, tuple[str, str]] = {}
    for node_id, node in by_id.items():
        if node.kind is NodeKind.MAIN:
            continue
        ins = incoming.get(node_id, [])
        outs = outgoing.get(node_id, [])
        if node.kind is NodeKind.BRANCH:
            if len(ins) != 1 or ins[0] not in main_set:
                raise TopologyError(
                    f"branch {node_id} must take off from exactly one main stage"
                )
            if len(outs) > 1 or (outs and outs[0] not in main_set):
                raise TopologyError(f"branch {node_id} may only rejoin a main stage")
            if outs and positions[outs[0]] <= positions[ins[0]]:
                raise TopologyError(
                    f"branch {node_id} must rejoin downstream of its take-off"
                )
        else:
            main_ins = [source for source in ins if source in main_set]
            main_outs = [target for target in outs if target in main_set]
            if len(main_ins) != 1 or len(ins) != 1 or len(main_outs) != 1 or len(outs) != 1:
                raise TopologyError(
                    f"bypass {node_id} needs exactly one entry and one exit edge to main stages"
                )
            entry, exit_ = main_ins[0], main_outs[0]
            if positions[exit_] <= positions[entry]:
                raise TopologyError(f"bypass {node_id} must exit downstream of its entry")
            if positions[exit_] - positions[entry] < 2:
                raise TopologyError(f"bypass {node_id} does not skip any main stage")
            spans[node_id] = (entry, exit_)
    return spans


def _check_acyclic(by_id: dict[str, Node], edges: list[Edge]) -> None:
    indegree = {node_id: 0 for node_id in by_id}
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        indegree[edge.downstream] += 1
        outgoing.setdefault(edge.upstream, []).append(edge.downstream)
    queue = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited = 0
    while queue:
        current = queue.pop()
        visited += 1
        for nxt in outgoing.get(current, []):
            indegree[nxt] -= 1
            if indegree[nxt] == 0:
                queue.append(nxt)
    if visited != len(by_id):
        raise TopologyError("topology graph contains a cycle")

"""Topology data model: an ordered main line with branches and bypasses.

A topology is a plain, JSON-serialisable description of the process line:

* ``stages``  - the ordered main line. A canonical treatment stage runs on the
  main line unless a bypass explicitly diverts flow around it.
* ``branches`` - named side lines attached before or after a main line stage.
  A branch performs a real stage in addition to the main line work.
* ``bypasses`` - temporary or permanent side lines that divert flow around a
  contiguous interval of the main line. By process convention a bypass is a
  stage on the walk itself; set ``counts_as_stage`` false on a bypass whose
  process definition treats it as a pure jump with no operation of its own.
* ``required`` - stages that the process definition demands somewhere on the
  effective walk. Missing one is a configuration error.
* ``ordering`` - explicit before/after pairs used to reject a submitted order
  that contradicts a hard process constraint before it is ever activated.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from waterplant.ns import Stage
from waterplant.ns.pipeline import STAGE_ACTIONS

BYPASS_PREFIX = "bypass:"
BRANCH_PREFIX = "branch:"

EFFECT_NEXT_CYCLE = "next_cycle"
EFFECT_IMMEDIATE = "immediate"
DEFAULT_POLICY = EFFECT_NEXT_CYCLE
EFFECTS = (EFFECT_NEXT_CYCLE, EFFECT_IMMEDIATE)

DEFAULT_REQUIRED: tuple[Stage, ...] = (
    Stage.INTAKE,
    Stage.COAG,
    Stage.CHLOR,
    Stage.FILTER,
    Stage.CLEARWELL,
    Stage.QUOTA,
    Stage.AUDIT,
)


@dataclass(frozen=True)
class Branch:
    """A named side line attached at one main line stage."""

    name: str
    stage: Stage
    after: Stage | None = None
    before: Stage | None = None

    def node_id(self) -> str:
        return f"{BRANCH_PREFIX}{self.name}"

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"name": self.name, "stage": self.stage.value}
        if self.after is not None:
            payload["after"] = self.after.value
        if self.before is not None:
            payload["before"] = self.before.value
        return payload


@dataclass(frozen=True)
class Bypass:
    """A diversion around a contiguous ``start..end`` interval.

    ``start`` is the last main line stage reached before diverting and ``end``
    is the first main line stage reached after the bypass rejoins. Either
    boundary may be omitted for a bypass that diverges before the line begins
    or rejoins after it ends.
    """

    name: str
    start: Stage | None = None
    end: Stage | None = None
    counts_as_stage: bool = True

    def node_id(self) -> str:
        return f"{BYPASS_PREFIX}{self.name}"

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "name": self.name,
            "counts_as_stage": self.counts_as_stage,
        }
        if self.start is not None:
            payload["start"] = self.start.value
        if self.end is not None:
            payload["end"] = self.end.value
        return payload


@dataclass(frozen=True)
class OrderRule:
    """A hard process constraint: ``first`` must be reached before ``second``."""

    first: Stage
    second: Stage

    def as_dict(self) -> dict[str, str]:
        return {"before": self.first.value, "after": self.second.value}


@dataclass(frozen=True)
class PlanNode:
    """One node on the effective walk a cycle performs.

    ``node_id`` is unique across the whole walk (branch and bypass names live
    in their own prefixes). A bypass that counts as a stage has no real stage.
    """

    index: int
    node_id: str
    stage: Stage | None
    kind: str
    label: str
    action: str

    def as_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "node_id": self.node_id,
            "stage": None if self.stage is None else self.stage.value,
            "kind": self.kind,
            "label": self.label,
            "action": self.action,
        }


@dataclass(frozen=True)
class Topology:
    """A validated, versionable description of the whole line."""

    name: str
    stages: tuple[Stage, ...]
    branches: tuple[Branch, ...] = ()
    bypasses: tuple[Bypass, ...] = ()
    required: tuple[Stage, ...] = DEFAULT_REQUIRED
    order_rules: tuple[OrderRule, ...] = field(default_factory=tuple)
    bypass_as_stage: bool = True

    # ------------------------------------------------------------------ views
    def index_of(self, stage: Stage) -> int:
        for index, candidate in enumerate(self.stages):
            if candidate == stage:
                return index
        return -1

    def contains(self, stage: Stage) -> bool:
        return self.index_of(stage) >= 0

    def count(self) -> int:
        return len(self.stages)

    def last(self) -> Stage | None:
        return self.stages[-1] if self.stages else None

    def stage_names(self) -> list[str]:
        return [stage.value for stage in self.stages]

    def bypass(self, name: str) -> Bypass | None:
        for candidate in self.bypasses:
            if candidate.name == name:
                return candidate
        return None

    def branch(self, name: str) -> Branch | None:
        for candidate in self.branches:
            if candidate.name == name:
                return candidate
        return None

    def skipped_stages(self, bypass: Bypass) -> list[Stage]:
        """Main line stages diverted around by ``bypass``."""

        if bypass.start is not None and not self.contains(bypass.start):
            return []
        if bypass.end is not None and not self.contains(bypass.end):
            return []
        start_index = -1 if bypass.start is None else self.index_of(bypass.start)
        end_index = len(self.stages) if bypass.end is None else self.index_of(bypass.end)
        return list(self.stages[start_index + 1 : end_index])

    def _skip_set(self) -> set[Stage]:
        skipped: set[Stage] = set()
        for bypass in self.bypasses:
            skipped.update(self.skipped_stages(bypass))
        return skipped

    def counts_as_stage(self, bypass: Bypass) -> bool:
        return self.bypass_as_stage and bypass.counts_as_stage

    # ------------------------------------------------------------- the walk
    def plan(self) -> list[PlanNode]:
        """Expand the description into the ordered walk executed by a cycle."""

        skipped = self._skip_set()
        nodes: list[PlanNode] = []

        def add(node_id: str, stage: Stage | None, kind: str, label: str) -> None:
            action = "" if stage is None else STAGE_ACTIONS.get(stage, "")
            nodes.append(
                PlanNode(
                    index=len(nodes),
                    node_id=node_id,
                    stage=stage,
                    kind=kind,
                    label=label,
                    action=action,
                )
            )

        def bypass_label(bypass: Bypass) -> str:
            names = [stage.value for stage in self.skipped_stages(bypass)]
            text = f"{bypass.name} bypass"
            if names:
                text += f" skips {','.join(names)}"
            return text

        for stage in self.stages:
            for branch in self.branches:
                if branch.before == stage:
                    add(branch.node_id(), branch.stage, "branch", f"{branch.name} before {stage.value}")
            for bypass in self.bypasses:
                if bypass.start is None and bypass.end == stage and self.counts_as_stage(bypass):
                    add(bypass.node_id(), None, "bypass", bypass_label(bypass))
            if stage not in skipped:
                add(stage.value, stage, "main", stage.value)
            for branch in self.branches:
                if branch.after == stage:
                    add(branch.node_id(), branch.stage, "branch", f"{branch.name} after {stage.value}")
            for bypass in self.bypasses:
                if bypass.start == stage and self.counts_as_stage(bypass):
                    add(bypass.node_id(), None, "bypass", bypass_label(bypass))

        for bypass in self.bypasses:
            if bypass.start is None and bypass.end is None and self.counts_as_stage(bypass):
                add(bypass.node_id(), None, "bypass", bypass_label(bypass))
        for branch in self.branches:
            if branch.after is None and branch.before is None:
                add(branch.node_id(), branch.stage, "branch", f"{branch.name} at line end")
        return nodes

    def node_ids(self) -> list[str]:
        return [node.node_id for node in self.plan()]

    def effective_main_stages(self) -> list[Stage]:
        """Main line stages actually reached once bypasses divert flow."""

        return [stage for stage in self.stages if stage not in self._skip_set()]

    def missing_required(self) -> list[Stage]:
        reached = set(self.effective_main_stages())
        return [stage for stage in self.required if stage not in reached]

    def violated_order_rules(self) -> list[OrderRule]:
        """Return hard ordering rules that the effective walk does not satisfy."""

        effective = self.effective_main_stages()
        position = {stage: index for index, stage in enumerate(effective)}
        broken: list[OrderRule] = []
        for rule in self.order_rules:
            if rule.first not in position or rule.second not in position:
                broken.append(rule)
                continue
            if position[rule.first] >= position[rule.second]:
                broken.append(rule)
        return broken

    # ------------------------------------------------------------- summaries
    def describe(self) -> str:
        return (
            f"topology {self.name} stages={len(self.stages)} "
            f"branches={len(self.branches)} bypasses={len(self.bypasses)}"
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "stages": [stage.value for stage in self.stages],
            "branches": [branch.as_dict() for branch in self.branches],
            "bypasses": [bypass.as_dict() for bypass in self.bypasses],
            "required": [stage.value for stage in self.required],
            "ordering": [rule.as_dict() for rule in self.order_rules],
            "bypass_as_stage": self.bypass_as_stage,
        }


# --------------------------------------------------------------------- parsing
def _stage_value(payload: dict[str, object], field_name: str) -> Stage:
    raw = payload.get(field_name)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"topology {field_name} must be a non-empty stage name")
    try:
        return Stage(raw)
    except ValueError as exc:
        raise ValueError(f"unknown stage {raw!r} in topology {field_name}") from exc


def _optional_stage(payload: dict[str, object], field_name: str) -> Stage | None:
    raw = payload.get(field_name)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"topology {field_name} must be a stage name")
    try:
        return Stage(raw)
    except ValueError as exc:
        raise ValueError(f"unknown stage {raw!r} in topology {field_name}") from exc


def _parse_stages(raw: object) -> tuple[Stage, ...]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("topology stages must be a non-empty list")
    stages: list[Stage] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("topology stages must be stage names")
        try:
            stage = Stage(item)
        except ValueError as exc:
            raise ValueError(f"unknown stage {item!r} in topology stages") from exc
        if stage in stages:
            raise ValueError(f"stage {stage.value} appears more than once on the main line")
        stages.append(stage)
    return tuple(stages)


def _parse_branches(raw: object, main: tuple[Stage, ...]) -> tuple[Branch, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("topology branches must be a list")
    branches: list[Branch] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each topology branch must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("each topology branch needs a non-empty name")
        stage = _stage_value(item, "stage")
        after = _optional_stage(item, "after")
        before = _optional_stage(item, "before")
        if after is not None and before is not None:
            raise ValueError(f"branch {name} cannot attach both after and before a stage")
        for anchor in (after, before):
            if anchor is not None and anchor not in main:
                raise ValueError(f"branch {name} attaches at {anchor.value}, which is not on the line")
        branches.append(Branch(name=name, stage=stage, after=after, before=before))
    return tuple(branches)


def _parse_bypasses(raw: object, main: tuple[Stage, ...]) -> tuple[Bypass, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("topology bypasses must be a list")
    bypasses: list[Bypass] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each topology bypass must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("each topology bypass needs a non-empty name")
        start = _optional_stage(item, "start")
        end = _optional_stage(item, "end")
        counts = item.get("counts_as_stage", True)
        if not isinstance(counts, bool):
            raise ValueError(f"bypass {name} counts_as_stage must be true or false")
        if start is not None and start not in main:
            raise ValueError(f"bypass {name} starts at {start.value}, which is not on the line")
        if end is not None and end not in main:
            raise ValueError(f"bypass {name} ends at {end.value}, which is not on the line")
        if start is not None and end is not None:
            indices = [stage for stage in main]
            if indices.index(start) >= indices.index(end):
                raise ValueError(f"bypass {name} start must come before its end")
        bypasses.append(Bypass(name=name, start=start, end=end, counts_as_stage=counts))
    return tuple(bypasses)


def _parse_required(raw: object) -> tuple[Stage, ...]:
    if raw is None:
        return DEFAULT_REQUIRED
    if not isinstance(raw, list):
        raise ValueError("topology required must be a list")
    required: list[Stage] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("topology required must be stage names")
        try:
            stage = Stage(item)
        except ValueError as exc:
            raise ValueError(f"unknown stage {item!r} in topology required") from exc
        if stage not in required:
            required.append(stage)
    return tuple(required)


def _parse_ordering(raw: object) -> tuple[OrderRule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError("topology ordering must be a list")
    rules: list[OrderRule] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each topology ordering rule must be an object")
        first = _stage_value(item, "before")
        second = _stage_value(item, "after")
        if first == second:
            raise ValueError(f"ordering rule cannot compare {first.value} to itself")
        rules.append(OrderRule(first=first, second=second))
    return tuple(rules)


def _cross_validate(topology: Topology) -> None:
    names: set[str] = set()
    for branch in topology.branches:
        if branch.name in names:
            raise ValueError(f"duplicate branch or bypass name {branch.name}")
        names.add(branch.name)
    for bypass in topology.bypasses:
        if bypass.name in names:
            raise ValueError(f"duplicate branch or bypass name {bypass.name}")
        names.add(bypass.name)

    skipped = topology._skip_set()
    for branch in topology.branches:
        anchor = branch.after if branch.after is not None else branch.before
        if anchor is not None and anchor in skipped:
            raise ValueError(
                f"branch {branch.name} attaches at {anchor.value}, which a bypass skips"
            )

    intervals: list[tuple[int, int, str]] = []
    last = len(topology.stages)
    for bypass in topology.bypasses:
        start = -1 if bypass.start is None else topology.index_of(bypass.start)
        end = last if bypass.end is None else topology.index_of(bypass.end)
        for other_start, other_end, other_name in intervals:
            if start < other_end and other_start < end:
                raise ValueError(f"bypass {bypass.name} overlaps bypass {other_name}")
        intervals.append((start, end, bypass.name))

    missing = topology.missing_required()
    if missing:
        raise ValueError(
            "topology is missing required stages: " + ", ".join(stage.value for stage in missing)
        )
    broken = topology.violated_order_rules()
    if broken:
        rule = broken[0]
        raise ValueError(
            f"ordering violated: {rule.first.value} must run before {rule.second.value}"
        )


def topology_from_dict(payload: dict[str, object]) -> Topology:
    """Validate a topology document and build the model.

    Raises ``ValueError`` with an operator readable message for any structural
    problem, unknown stage, missing required stage or violated ordering rule.
    """

    if not isinstance(payload, dict):
        raise ValueError("topology document must be an object")
    name = payload.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("topology name must be a non-empty string")
    main = _parse_stages(payload.get("stages"))
    branches = _parse_branches(payload.get("branches"), main)
    bypasses = _parse_bypasses(payload.get("bypasses"), main)
    required = _parse_required(payload.get("required"))
    ordering = _parse_ordering(payload.get("ordering"))
    bypass_as_stage = payload.get("bypass_as_stage", True)
    if not isinstance(bypass_as_stage, bool):
        raise ValueError("topology bypass_as_stage must be true or false")
    topology = Topology(
        name=name,
        stages=main,
        branches=branches,
        bypasses=bypasses,
        required=required,
        order_rules=ordering,
        bypass_as_stage=bypass_as_stage,
    )
    _cross_validate(topology)
    return topology


def topology_to_json(topology: Topology) -> str:
    """Stable text form used both for persistence and idempotency hashing."""

    return json.dumps(topology.as_dict(), ensure_ascii=False, sort_keys=True)


def default_topology() -> Topology:
    """The canonical line, expressed through the configurable model.

    The seven canonical stages form the main line. Turbidity judging is a
    process side line attached right after coagulation; the pH verdict is a
    precondition of chlorination and runs inside the chlorine stage.
    """

    return Topology(
        name="treatment",
        stages=(
            Stage.INTAKE,
            Stage.COAG,
            Stage.CHLOR,
            Stage.FILTER,
            Stage.CLEARWELL,
            Stage.QUOTA,
            Stage.AUDIT,
        ),
        branches=(Branch(name="turbidity", stage=Stage.TURBIDITY, after=Stage.COAG),),
        order_rules=(OrderRule(first=Stage.INTAKE, second=Stage.AUDIT),),
    )

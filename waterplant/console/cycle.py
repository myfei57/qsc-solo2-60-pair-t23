"""One full treatment control cycle, executed along the active topology.

The cycle no longer hard codes the line. It asks the topology manager for a
cycle context pinned to one version, walks the planned nodes in order and lets
the :class:`~waterplant.topology.StageGate` reject out of order work. The
cycle record stores the same version, so verdicts and records can never
disagree about which line produced them.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from waterplant.clearwell import validate_level
from waterplant.filter import validate_zone
from waterplant.flow import validate_factor
from waterplant.intake import Sensor, mix, validate_flow
from waterplant.ns import Stage
from waterplant.quota import validate_amount
from waterplant.topology import OrderError

from .http import Request, Response, json_response
from .runtime import Runtime


@dataclass
class CycleState:
    """Outputs accumulated as the walk progresses."""

    coag_dose: float = 0.0
    turb_dose: float = 0.0
    chlor_dose: float = 0.0
    min_level: float = 0.0
    level: float = 0.0
    quota_value: float = 0.0
    rotation: list[str] = field(default_factory=list)
    duty: str = ""
    backwash_bed: str = ""
    drains: int = 0
    ph_stable: bool = True
    ph_adjustment: float = 0.0
    completed: list[str] = field(default_factory=list)


def _validate_inputs(rt: Runtime, request: Request) -> None:
    flow = request.float_field("flow")
    request.float_list("samples")
    level = request.float_field("level")
    zone = request.int_field("zone")
    amount = request.float_field("amount")

    validate_flow(flow)
    validate_factor(rt.calibration.current())
    validate_zone(zone)
    validate_amount(amount)
    validate_level(level)


def _perform(rt: Runtime, request: Request, stage: Stage, state: CycleState) -> None:
    """Execute one real stage. The order of these branches is the walk order."""

    flow = request.float_field("flow")
    samples = request.float_list("samples")
    demand = request.float_field("demand")
    level = request.float_field("level")
    bed_id = request.str_field("bed_id")
    amount = request.float_field("amount")

    if stage is Stage.INTAKE:
        rt.flow_repository.record(Sensor(flow=flow, turbidity=mix(samples)))
        rt.trend.record(flow)
    elif stage is Stage.COAG:
        state.coag_dose = rt.coag_doser.update_flow_and_dose(flow)
    elif stage is Stage.TURBIDITY:
        state.turb_dose = rt.sampler.judge(samples)
    elif stage is Stage.CHLOR:
        rt.well.update_residual_demand(demand)
        verdict = rt.stabilizer.stabilize()
        state.ph_stable = verdict.stable
        state.ph_adjustment = verdict.adjustment
        state.chlor_dose = rt.chlor_doser.apply_residual() if verdict.stable else 0.0
    elif stage is Stage.CLEARWELL:
        state.min_level = rt.well.adjust_level(level, rt.inlet, rt.outlet)
        state.level = rt.well.level()
    elif stage is Stage.FILTER:
        state.rotation = rt.backwash.order_rotation()
        state.duty = rt.bank.on_duty() or ""
        if bed_id:
            rt.backwash.start(bed_id)
            state.backwash_bed = bed_id
        state.drains = rt.backwash.drain_count()
    elif stage is Stage.QUOTA:
        state.quota_value = rt.accumulator.add(amount)
    # AUDIT and any future stage have no side effect of their own here.


def run_cycle(rt: Runtime, request: Request) -> Response:
    """Walk the active topology once, in its planned order."""

    _validate_inputs(rt, request)
    context = rt.topologies.begin_cycle()
    state = CycleState()
    result: dict[str, object] = {}

    try:
        for node in context.gate.nodes():
            # The gate blocks an out of order node before any side effect.
            context.gate.check(node.node_id)
            if node.stage is not None:
                _perform(rt, request, node.stage, state)
            context.gate.mark(node.node_id)
            state.completed.append(node.node_id)

        pending = context.gate.pending_main()
        if pending:
            missing = ", ".join(node.node_id for node in pending)
            raise OrderError(f"cycle ended before required stages completed: {missing}")

        result = {
            "coag_dose": state.coag_dose,
            "turb_dose": state.turb_dose,
            "chlor_dose": state.chlor_dose,
            "min_level": state.min_level,
            "level": state.level,
            "quota": state.quota_value,
            "rotation": state.rotation,
            "on_duty": state.duty,
            "backwash": state.backwash_bed,
            "drains": state.drains,
            "audit_count": rt.auditor.count(),
            "ph_stable": state.ph_stable,
            "ph_adjustment": state.ph_adjustment,
            "topology_version": context.version,
            "topology_name": context.topology.name,
            "cycle_id": context.cycle_id,
            "completed": state.completed,
        }
        rt.topologies.finish_cycle(context, ok=True, result=result)
    except (ValueError, OrderError) as exc:
        # Bad input the stage validators did not see early, or a violated
        # order: record the failed cycle so the version history stays truthful.
        rt.topologies.finish_cycle(context, ok=False, error=str(exc))
        raise

    return json_response(result)

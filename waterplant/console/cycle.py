"""One full treatment control cycle."""

from __future__ import annotations

from dataclasses import dataclass, field

from waterplant.clearwell import validate_level
from waterplant.filter import validate_zone
from waterplant.flow import validate_factor
from waterplant.intake import Sensor, mix, validate_flow
from waterplant.ns import Stage
from waterplant.quota import validate_amount

from .http import Request, Response, json_response
from .runtime import Runtime


@dataclass
class CycleContext:
    """Inputs and collected outputs of one cycle walk."""

    flow: float
    samples: list[float]
    demand: float
    level: float
    bed_id: str
    amount: float
    results: dict[str, object] = field(default_factory=dict)


def _run_intake(rt: Runtime, ctx: CycleContext) -> None:
    rt.flow_repository.record(Sensor(flow=ctx.flow, turbidity=mix(ctx.samples)))
    rt.trend.record(ctx.flow)


def _run_coag(rt: Runtime, ctx: CycleContext) -> None:
    ctx.results["coag_dose"] = rt.coag_doser.update_flow_and_dose(ctx.flow)


def _run_turbidity(rt: Runtime, ctx: CycleContext) -> None:
    ctx.results["turb_dose"] = rt.sampler.judge(ctx.samples)


def _run_chlor(rt: Runtime, ctx: CycleContext) -> None:
    verdict = rt.stabilizer.stabilize()
    ctx.results["ph_stable"] = verdict.stable
    ctx.results["ph_adjustment"] = verdict.adjustment
    rt.well.update_residual_demand(ctx.demand)
    ctx.results["chlor_dose"] = rt.chlor_doser.apply_residual() if verdict.stable else 0.0


def _run_clearwell(rt: Runtime, ctx: CycleContext) -> None:
    ctx.results["min_level"] = rt.well.adjust_level(ctx.level, rt.inlet, rt.outlet)


def _run_backwash(rt: Runtime, ctx: CycleContext) -> None:
    ctx.results["rotation"] = rt.backwash.order_rotation()
    ctx.results["on_duty"] = rt.bank.on_duty() or ""
    if ctx.bed_id:
        rt.backwash.start(ctx.bed_id)


def _run_quota(rt: Runtime, ctx: CycleContext) -> None:
    ctx.results["quota"] = rt.accumulator.add(ctx.amount)


_STAGE_EXECUTORS = {
    Stage.INTAKE: _run_intake,
    Stage.COAG: _run_coag,
    Stage.TURBIDITY: _run_turbidity,
    Stage.CHLOR: _run_chlor,
    Stage.CLEARWELL: _run_clearwell,
    Stage.BACKWASH: _run_backwash,
    Stage.QUOTA: _run_quota,
}


def run_cycle(rt: Runtime, request: Request) -> Response:
    """Walk the line once, in the order of the topology pinned for this cycle."""

    flow = request.float_field("flow")
    samples = request.float_list("samples")
    demand = request.float_field("demand")
    level = request.float_field("level")
    bed_id = request.str_field("bed_id")
    zone = request.int_field("zone")
    amount = request.float_field("amount")

    validate_flow(flow)
    validate_factor(rt.calibration.current())
    validate_zone(zone)
    validate_amount(amount)
    validate_level(level)

    view = rt.topology.begin_cycle()
    ctx = CycleContext(
        flow=flow,
        samples=samples,
        demand=demand,
        level=level,
        bed_id=bed_id,
        amount=amount,
    )
    walked = view.topology.execution_nodes()
    for node in walked:
        executor = _STAGE_EXECUTORS.get(node.stage)
        if executor is not None:
            executor(rt, ctx)
    record = rt.topology.record_cycle(view, [node.stage.value for node in walked])
    results = ctx.results

    return json_response(
        {
            "coag_dose": results.get("coag_dose", 0.0),
            "turb_dose": results.get("turb_dose", 0.0),
            "chlor_dose": results.get("chlor_dose", 0.0),
            "min_level": results.get("min_level", 0.0),
            "level": rt.well.level(),
            "quota": results.get("quota", rt.accumulator.value()),
            "rotation": results.get("rotation", []),
            "on_duty": results.get("on_duty", ""),
            "backwash": bed_id,
            "drains": rt.backwash.drain_count(),
            "audit_count": rt.auditor.count(),
            "ph_stable": results.get("ph_stable", True),
            "ph_adjustment": results.get("ph_adjustment", 0.0),
            "cycle": record.cycle,
            "topology_version": record.topology_version,
            "stages": record.stages,
            "bypassed": record.bypassed,
            "topology_degraded": view.degraded,
        }
    )

"""Console endpoints for the configurable topology."""

from __future__ import annotations

import json

from waterplant.topology import topology_to_json

from .http import Request, RequestError, Response, json_response
from .runtime import Runtime

EFFECTS = ("next_cycle", "immediate")


def _json_field(request: Request, name: str) -> dict[str, object]:
    value = request.payload.get(name)
    if value is None:
        raise RequestError(400, f"field {name} is required")
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError as exc:
            raise RequestError(400, f"field {name} must be valid JSON") from exc
        value = parsed
    if not isinstance(value, dict):
        raise RequestError(400, f"field {name} must be an object")
    return value


def current(rt: Runtime, request: Request) -> Response:
    manager = rt.topologies
    topology = manager.current()
    staged = manager.staged()
    return json_response(
        {
            "version": manager.version(),
            "effect_default": manager.default_effect(),
            "degraded": manager.is_degraded(),
            "degraded_reason": manager.degraded_reason(),
            "topology": topology.as_dict(),
            "plan": [node.as_dict() for node in topology.plan()],
            "staged": None if staged is None else staged.as_dict(),
            "status": manager.status(),
        }
    )


def submit(rt: Runtime, request: Request) -> Response:
    document = _json_field(request, "topology")
    effect = request.str_field("effect") or None
    if effect is not None and effect not in EFFECTS:
        raise RequestError(400, f"effect must be one of {', '.join(EFFECTS)}")
    requested_by = request.str_field("requested_by")
    outcome = rt.topologies.submit(document, effect=effect, requested_by=requested_by)
    return json_response(outcome.as_dict())


def refresh(rt: Runtime, request: Request) -> Response:
    status = rt.topologies.refresh()
    return json_response({"refreshed": True, "status": status})


def cycles(rt: Runtime, request: Request) -> Response:
    records = rt.topologies.cycles()
    return json_response(
        {
            "cycles": [record.as_dict() for record in records],
            "count": len(records),
        }
    )


def order_preview(rt: Runtime, request: Request) -> Response:
    """Validate a document without persisting it, returning its walk."""

    from waterplant.topology import topology_from_dict

    document = _json_field(request, "topology")
    topology = topology_from_dict(document)
    return json_response(
        {
            "valid": True,
            "plan": [node.as_dict() for node in topology.plan()],
            "missing_required": [stage.value for stage in topology.missing_required()],
            "fingerprint": topology_to_json(topology),
        }
    )


def audit_trail(rt: Runtime, request: Request) -> Response:
    kinds = {
        "topology-change",
        "topology-staged",
        "topology-activated",
        "topology-degraded",
        "topology-recovered",
        "topology-cycle",
    }
    entries = [
        entry.as_dict()
        for entry in rt.auditor.entries()
        if entry.kind in kinds
    ]
    return json_response({"entries": entries, "count": len(entries)})

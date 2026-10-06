"""Tests for configurable topology, versioning and cycle binding."""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.audit import Auditor
from waterplant.console.seed import seed_defaults
from waterplant.console.server import Server
from waterplant.ns import Stage
from waterplant.store import Store
from waterplant.topology import (
    BYPASS_PREFIX,
    EFFECT_IMMEDIATE,
    EFFECT_NEXT_CYCLE,
    OrderError,
    StageGate,
    TopologyManager,
    default_topology,
    topology_from_dict,
)


def document(stages, branches=None, bypasses=None, ordering=None, required=None, **extra):
    payload = {"name": "line", "stages": stages}
    if branches is not None:
        payload["branches"] = branches
    if bypasses is not None:
        payload["bypasses"] = bypasses
    if ordering is not None:
        payload["ordering"] = ordering
    if required is not None:
        payload["required"] = required
    payload.update(extra)
    return payload


LEGACY = ["intake", "coag", "chlor", "filter", "clearwell", "quota", "audit"]
NO_CHLOR = ["intake", "coag", "filter", "clearwell", "quota", "audit"]


class ModelCase(unittest.TestCase):
    def test_default_walk_has_branch_and_keeps_main_line(self) -> None:
        topology = default_topology()
        node_ids = topology.node_ids()
        self.assertEqual(node_ids[0], "intake")
        self.assertIn("branch:turbidity", node_ids)
        self.assertEqual(
            [node.node_id for node in topology.plan() if node.kind == "main"],
            LEGACY,
        )
        self.assertEqual([node.stage.value for node in topology.plan() if node.kind == "branch"],
                         ["turbidity"])

    def test_bypass_counts_as_stage_by_process_convention(self) -> None:
        doc = document(
            LEGACY,
            bypasses=[{"name": "temp", "start": "coag", "end": "filter"}],
            required=NO_CHLOR,
        )
        topology = topology_from_dict(doc)
        node_ids = topology.node_ids()
        self.assertIn(f"{BYPASS_PREFIX}temp", node_ids)
        self.assertNotIn("chlor", node_ids)
        bypass_node = [n for n in topology.plan() if n.kind == "bypass"][0]
        self.assertIsNone(bypass_node.stage)
        self.assertIn("chlor", bypass_node.label)

    def test_bypass_can_be_a_pure_jump_when_process_says_so(self) -> None:
        doc = document(
            LEGACY,
            bypasses=[{"name": "jumper", "start": "coag", "end": "filter",
                       "counts_as_stage": False}],
            required=NO_CHLOR,
        )
        topology = topology_from_dict(doc)
        self.assertNotIn(f"{BYPASS_PREFIX}jumper", topology.node_ids())
        self.assertNotIn("chlor", topology.node_ids())

    def test_top_level_bypass_convention_flag(self) -> None:
        doc = document(
            LEGACY,
            bypasses=[{"name": "temp", "start": "coag", "end": "filter"}],
            bypass_as_stage=False,
            required=NO_CHLOR,
        )
        topology = topology_from_dict(doc)
        self.assertNotIn(f"{BYPASS_PREFIX}temp", topology.node_ids())

    def test_branch_runs_alongside_the_main_line(self) -> None:
        doc = document(
            LEGACY,
            branches=[{"name": "side-dose", "stage": "backwash", "before": "filter"}],
        )
        topology = topology_from_dict(doc)
        node_ids = topology.node_ids()
        self.assertLess(node_ids.index("branch:side-dose"), node_ids.index("filter"))
        self.assertIn("filter", node_ids)

    def test_missing_required_stage_is_rejected_up_front(self) -> None:
        doc = document(
            LEGACY,
            bypasses=[{"name": "wide", "start": "coag", "end": "audit"}],
        )
        # filter, clearwell, quota skipped but required -> error naming them
        with self.assertRaises(ValueError) as ctx:
            topology_from_dict(doc)
        message = str(ctx.exception)
        self.assertIn("missing required stages", message)
        self.assertIn("filter", message)

    def test_relaxed_required_list_allows_bypass(self) -> None:
        doc = document(
            LEGACY,
            bypasses=[{"name": "wide", "start": "coag", "end": "clearwell"}],
            required=["intake", "coag", "audit"],
        )
        topology = topology_from_dict(doc)
        self.assertEqual(topology.missing_required(), [])

    def test_unknown_stage_and_bad_shape_rejected(self) -> None:
        with self.assertRaises(ValueError):
            topology_from_dict({"name": "x", "stages": ["intake", "nope"]})
        with self.assertRaises(ValueError):
            topology_from_dict({"name": "x", "stages": []})
        with self.assertRaises(ValueError):
            topology_from_dict({"name": "x", "stages": ["intake", "intake"]})
        with self.assertRaises(ValueError):
            topology_from_dict(document(LEGACY, branches=[{"name": "b", "stage": "??"}]))
        with self.assertRaises(ValueError):
            topology_from_dict(
                document(LEGACY, bypasses=[{"name": "p", "start": "filter", "end": "coag"}])
            )

    def test_overlapping_bypasses_and_duplicate_names_rejected(self) -> None:
        with self.assertRaises(ValueError):
            topology_from_dict(
                document(
                    LEGACY,
                    bypasses=[
                        {"name": "a", "start": "intake", "end": "filter"},
                        {"name": "a", "start": "coag", "end": "clearwell"},
                    ],
                )
            )
        with self.assertRaises(ValueError):
            topology_from_dict(
                document(
                    LEGACY,
                    bypasses=[
                        {"name": "a", "start": "intake", "end": "filter"},
                        {"name": "b", "start": "coag", "end": "clearwell"},
                    ],
                )
            )

    def test_ordering_rule_blocks_contradictory_order(self) -> None:
        reversed_line = list(reversed(LEGACY))
        doc = document(
            reversed_line,
            ordering=[{"before": "intake", "after": "coag"}],
        )
        with self.assertRaises(ValueError) as ctx:
            topology_from_dict(doc)
        self.assertIn("ordering violated", str(ctx.exception))

    def test_ordering_rule_skipped_by_bypass_is_violation(self) -> None:
        doc = document(
            LEGACY,
            bypasses=[{"name": "skip-coag", "start": "intake", "end": "chlor"}],
            ordering=[{"before": "coag", "after": "chlor"}],
            required=["intake", "chlor", "filter", "clearwell", "quota", "audit"],
        )
        with self.assertRaises(ValueError):
            topology_from_dict(doc)

    def test_branch_anchor_inside_bypass_rejected(self) -> None:
        doc = document(
            LEGACY,
            branches=[{"name": "b", "stage": "backwash", "after": "chlor"}],
            bypasses=[{"name": "p", "start": "coag", "end": "filter"}],
        )
        with self.assertRaises(ValueError):
            topology_from_dict(doc)


class GateCase(unittest.TestCase):
    def test_gate_blocks_out_of_order_node(self) -> None:
        topology = default_topology()
        gate = StageGate(topology)
        with self.assertRaises(OrderError):
            gate.check("coag")
        gate.mark("intake")
        node = gate.mark("coag")
        self.assertEqual(node.stage, Stage.COAG)
        with self.assertRaises(OrderError):
            gate.mark("intake")

    def test_gate_rejects_unknown_and_detects_pending(self) -> None:
        gate = StageGate(default_topology())
        with self.assertRaises(OrderError):
            gate.mark("ghost")
        self.assertTrue(len(gate.pending_main()) > 0)


def new_server() -> tuple[Server, Store, "tempfile.TemporaryDirectory[str]"]:
    tmp = tempfile.TemporaryDirectory()
    store = Store.open(f"{tmp.name}/state.json")
    seed_defaults(store)
    return Server(store), store, tmp


class ConsoleTopologyCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server, self.store, self.tmp = new_server()

    def tearDown(self) -> None:
        self.store.close()
        self.tmp.cleanup()

    def call(self, method, path, payload=None):
        response = self.server.dispatch(method, path, payload)
        return response.status, json.loads(response.body.decode("utf-8"))

    def cycle_payload(self, **overrides):
        payload = {
            "flow": 800.0,
            "samples": [2.0, 4.0],
            "demand": 0.5,
            "level": 10.0,
            "bed_id": "",
            "zone": 1,
            "amount": 12.0,
        }
        payload.update(overrides)
        return payload

    def test_active_topology_endpoint(self) -> None:
        status, body = self.call("GET", "/topology")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 1)
        self.assertFalse(body["degraded"])
        self.assertEqual(body["effect_default"], EFFECT_NEXT_CYCLE)
        kinds = [n["kind"] for n in body["plan"]]
        self.assertIn("main", kinds)
        self.assertIn("branch", kinds)

    def test_next_cycle_activation_is_honoured(self) -> None:
        new_doc = document(
            LEGACY,
            branches=[{"name": "extra", "stage": "backwash", "after": "filter"}],
        )
        status, body = self.call(
            "POST", "/topology/submit", {"topology": new_doc, "effect": "next_cycle"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(body["staged"])
        self.assertEqual(body["active_version"], 1)

        # The running line does not change before a cycle boundary.
        _, before = self.call("GET", "/topology")
        self.assertEqual(before["version"], 1)
        self.assertTrue(before["staged"])

        status, body = self.call("POST", "/cycle", self.cycle_payload())
        self.assertEqual(status, 200)
        self.assertEqual(body["topology_version"], 2)
        self.assertIn("branch:extra", body["completed"])

        _, after = self.call("GET", "/topology")
        self.assertEqual(after["version"], 2)
        self.assertFalse(after["staged"])

    def test_immediate_activation_ignores_cycle_boundary(self) -> None:
        new_doc = document(LEGACY, bypass_as_stage=True)
        new_doc["bypasses"] = []
        status, body = self.call(
            "POST", "/topology/submit", {"topology": new_doc, "effect": "immediate"}
        )
        self.assertEqual(status, 200)
        self.assertFalse(body["staged"])
        self.assertEqual(body["active_version"], 2)
        _, current = self.call("GET", "/topology")
        self.assertEqual(current["version"], 2)

    def test_invalid_document_rejected_and_not_stored(self) -> None:
        bad = document(["intake", "audit"])
        status, body = self.call("POST", "/topology/submit", {"topology": bad})
        self.assertEqual(status, 400)
        self.assertIn("missing required", body["error"])
        _, current = self.call("GET", "/topology")
        self.assertEqual(current["version"], 1)
        self.assertFalse(current["staged"])

    def test_submit_is_idempotent(self) -> None:
        new_doc = document(LEGACY, branches=[
            {"name": "extra", "stage": "backwash", "after": "filter"}
        ])
        first_status, first = self.call(
            "POST", "/topology/submit", {"topology": new_doc, "effect": "next_cycle"}
        )
        self.assertTrue(first["applied"])
        second_status, second = self.call(
            "POST", "/topology/submit", {"topology": new_doc, "effect": "next_cycle"}
        )
        self.assertEqual(second_status, 200)
        self.assertTrue(second["idempotent"])
        self.assertFalse(second["applied"])
        # Active still unchanged; no duplicate staged version created.
        self.assertEqual(second["active_version"], 1)

        # Re-submitting the currently active document is a no-op too.
        _, active = self.call("GET", "/topology")
        _, third = self.call(
            "POST",
            "/topology/submit",
            {"topology": active["topology"], "effect": "immediate"},
        )
        self.assertTrue(third["idempotent"])
        self.assertFalse(third["staged"])
        self.assertEqual(third["active_version"], 1)

    def test_cycle_record_pins_the_topology_version(self) -> None:
        # Stage a change so cycle 2 runs on v2 while cycle 1 stays v1.
        new_doc = document(LEGACY, branches=[
            {"name": "x", "stage": "backwash", "before": "audit"}
        ])
        self.call("POST", "/topology/submit", {"topology": new_doc})

        status, cycle_two = self.call("POST", "/cycle", self.cycle_payload())
        self.assertEqual(status, 200)
        self.assertEqual(cycle_two["topology_version"], 2)

        status, body = self.call("GET", "/topology/cycles")
        self.assertEqual(status, 200)
        self.assertEqual(body["count"], 1)
        record = body["cycles"][0]
        self.assertEqual(record["topology_version"], 2)
        self.assertEqual(record["status"], "complete")
        self.assertEqual(record["cycle_id"], cycle_two["cycle_id"])
        self.assertEqual(record["completed"], cycle_two["completed"])
        self.assertIn("branch:x", record["plan"])

    def test_bad_request_is_rejected_before_opening_a_cycle(self) -> None:
        status, body = self.call(
            "POST", "/cycle", self.cycle_payload(flow=-1)
        )
        self.assertEqual(status, 400)
        _, records = self.call("GET", "/topology/cycles")
        self.assertEqual(records["count"], 0)
        # Staged change must not be promoted by the rejected request.
        new_doc = document(LEGACY, branches=[
            {"name": "x", "stage": "backwash", "before": "audit"}
        ])
        self.call("POST", "/topology/submit", {"topology": new_doc})
        status, _ = self.call("POST", "/cycle", self.cycle_payload(flow=-1))
        self.assertEqual(status, 400)
        _, current = self.call("GET", "/topology")
        self.assertEqual(current["version"], 1)
        self.assertTrue(current["staged"])

    def test_failed_in_cycle_records_failure_and_frees_the_line(self) -> None:
        context = self.server.runtime.topologies.begin_cycle()
        # Simulate the cycle reporting a stage out of plan order.
        with self.assertRaises(OrderError):
            context.gate.mark("filter")
        record = self.server.runtime.topologies.finish_cycle(
            context, ok=False, error="forced order failure"
        )
        self.assertEqual(record.status, "failed")
        self.assertEqual(record.topology_version, 1)
        # The line is free again and a fresh cycle completes.
        status, body = self.call("POST", "/cycle", self.cycle_payload())
        self.assertEqual(status, 200)
        self.assertEqual(body["topology_version"], 1)
        _, records = self.call("GET", "/topology/cycles")
        statuses = [c["status"] for c in records["cycles"]]
        self.assertEqual(statuses, ["failed", "complete"])

    def test_validate_endpoint_previews_walk_without_saving(self) -> None:
        doc = document(
            LEGACY,
            bypasses=[{"name": "temp", "start": "coag", "end": "filter"}],
            required=["intake", "filter", "clearwell", "quota", "audit"],
        )
        status, body = self.call("POST", "/topology/validate", {"topology": doc})
        self.assertEqual(status, 200)
        self.assertTrue(body["valid"])
        node_ids = [n["node_id"] for n in body["plan"]]
        self.assertIn("bypass:temp", node_ids)
        _, current = self.call("GET", "/topology")
        self.assertEqual(current["version"], 1)

    def test_ordering_violation_rejected_over_http(self) -> None:
        bad = document(
            ["coag", "intake", "chlor", "filter", "clearwell", "quota", "audit"],
            ordering=[{"before": "intake", "after": "coag"}],
        )
        status, body = self.call("POST", "/topology/submit", {"topology": bad})
        self.assertEqual(status, 400)
        self.assertIn("ordering violated", body["error"])
        status, body = self.call("POST", "/topology/validate", {"topology": bad})
        self.assertEqual(status, 400)

    def test_overlapping_cycle_rejected_with_conflict(self) -> None:
        manager = self.server.runtime.topologies
        manager.begin_cycle()
        status, body = self.call("POST", "/cycle", self.cycle_payload())
        self.assertEqual(status, 409)
        self.assertIn("still running", body["error"])

    def test_change_audit_is_visible(self) -> None:
        new_doc = document(LEGACY, branches=[
            {"name": "x", "stage": "backwash", "after": "filter"}
        ])
        self.call("POST", "/topology/submit", {"topology": new_doc, "requested_by": "ops"})
        status, body = self.call("GET", "/topology/audit")
        self.assertEqual(status, 200)
        kinds = {entry["kind"] for entry in body["entries"]}
        self.assertIn("topology-staged", kinds)
        self.assertNotIn("topology-activated", kinds)
        self.call("POST", "/cycle", self.cycle_payload())
        _, body = self.call("GET", "/topology/audit")
        kinds = {entry["kind"] for entry in body["entries"]}
        self.assertIn("topology-activated", kinds)
        self.assertIn("topology-cycle", kinds)

    def test_health_check_reflects_topology(self) -> None:
        status, body = self.call("GET", "/health/checks")
        topology_check = [c for c in body["checks"] if c["name"] == "topology"][0]
        self.assertEqual(topology_check["status"], "ok")
        new_doc = document(LEGACY, branches=[
            {"name": "x", "stage": "backwash", "after": "filter"}
        ])
        self.call("POST", "/topology/submit", {"topology": new_doc})
        _, body = self.call("GET", "/health/checks")
        topology_check = [c for c in body["checks"] if c["name"] == "topology"][0]
        self.assertIn("pending", topology_check["detail"])

    def test_partial_read_failure_keeps_line_running_and_then_recovers(self) -> None:
        manager = self.server.runtime.topologies

        def fail():
            raise OSError("config store unreachable")

        manager._reader = fail
        status, body = self.call("POST", "/topology/refresh", {})
        self.assertEqual(status, 200)
        self.assertTrue(body["status"]["degraded"])

        # The line still runs on the last good topology.
        status, cycle = self.call("POST", "/cycle", self.cycle_payload())
        self.assertEqual(status, 200)
        self.assertEqual(cycle["topology_version"], 1)

        status, checks = self.call("GET", "/health/checks")
        topology_check = [c for c in checks["checks"] if c["name"] == "topology"][0]
        self.assertEqual(topology_check["status"], "warn")
        self.assertIn("retained", topology_check["detail"])

        # Recovery clears the annotation; repeated refresh is idempotent.
        manager._reader = manager._read_from_store
        self.call("POST", "/topology/refresh", {})
        self.call("POST", "/topology/refresh", {})
        _, body = self.call("GET", "/topology")
        self.assertFalse(body["degraded"])

        _, audits = self.call("GET", "/topology/audit")
        kinds = [e["kind"] for e in audits["entries"]]
        self.assertEqual(kinds.count("topology-degraded"), 1)
        self.assertEqual(kinds.count("topology-recovered"), 1)


class ManagerCase(unittest.TestCase):
    def _manager(self, **kwargs):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self.tmp.name}/state.json")
        auditor = Auditor(self.store)
        return TopologyManager(self.store, auditor=auditor, **kwargs), auditor

    def tearDown(self) -> None:
        if hasattr(self, "store"):
            self.store.close()
        if hasattr(self, "tmp"):
            self.tmp.cleanup()

    def test_seeded_default_survives_restart(self) -> None:
        manager, _ = self._manager()
        self.assertEqual(manager.version(), 1)
        reopened = TopologyManager(Store.open(self.store.path))
        self.assertEqual(reopened.version(), 1)
        self.assertEqual(reopened.current().stage_names(), manager.current().stage_names())

    def test_read_failure_keeps_last_good_and_marks_degraded(self) -> None:
        manager, auditor = self._manager()
        good = manager.current()

        def failing_reader():
            raise OSError("source unreachable")

        manager._reader = failing_reader
        status = manager.refresh()
        self.assertTrue(status["degraded"])
        self.assertIn("source unreachable", status["degraded_reason"])
        self.assertIs(manager.current(), good)

        kinds = [e.kind for e in auditor.entries()]
        self.assertEqual(kinds.count("topology-degraded"), 1)
        # Repeated failures are idempotent.
        manager.refresh()
        self.assertEqual([e.kind for e in auditor.entries()].count("topology-degraded"), 1)

    def test_recovery_is_idempotent(self) -> None:
        manager, auditor = self._manager()

        def failing_reader():
            raise OSError("boom")

        manager._reader = failing_reader
        manager.refresh()
        manager._reader = manager._read_from_store
        manager.refresh()
        manager.refresh()
        kinds = [e.kind for e in auditor.entries()]
        self.assertEqual(kinds.count("topology-recovered"), 1)
        self.assertFalse(manager.is_degraded())

    def test_corrupt_document_is_partial_failure(self) -> None:
        manager, _ = self._manager()

        def corrupt_reader():
            return "{not json"

        manager._reader = corrupt_reader
        status = manager.refresh()
        self.assertTrue(status["degraded"])
        self.assertEqual(manager.version(), 1)
        self.assertEqual(manager.current().name, "treatment")

    def test_boot_with_corrupt_store_falls_back_to_built_in(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        store = Store.open(f"{tmp.name}/state.json")
        store.put("topology:active", "garbage")
        manager = TopologyManager(store)
        self.assertTrue(manager.is_degraded())
        self.assertEqual(manager.current().name, "treatment")
        self.addCleanup(tmp.cleanup)

    def test_overlapping_cycles_rejected(self) -> None:
        manager, _ = self._manager()
        manager.begin_cycle()
        with self.assertRaises(RuntimeError):
            manager.begin_cycle()

    def test_finish_cycle_is_idempotent(self) -> None:
        manager, auditor = self._manager()
        context = manager.begin_cycle()
        for node in context.gate.nodes():
            context.gate.mark(node.node_id)
        record = manager.finish_cycle(context, ok=True, result={"x": 1})
        again = manager.finish_cycle(context, ok=True, result={"x": 1})
        self.assertEqual(record.cycle_id, again.cycle_id)
        self.assertEqual(len(manager.cycles()), 1)

    def test_default_effect_policy_can_be_immediate(self) -> None:
        manager, _ = self._manager(default_effect=EFFECT_IMMEDIATE)
        self.assertEqual(manager.default_effect(), EFFECT_IMMEDIATE)
        doc = document(LEGACY, branches=[
            {"name": "x", "stage": "backwash", "after": "filter"}
        ])
        outcome = manager.submit(doc)
        self.assertEqual(outcome.effect, EFFECT_IMMEDIATE)
        self.assertEqual(manager.version(), 2)

    def test_staged_change_persists_across_restart_and_activates_next_cycle(self) -> None:
        manager, _ = self._manager()
        doc = document(LEGACY, branches=[
            {"name": "x", "stage": "backwash", "after": "filter"}
        ])
        manager.submit(doc, effect=EFFECT_NEXT_CYCLE)
        reopened = TopologyManager(self.store)
        self.assertIsNotNone(reopened.staged())
        context = reopened.begin_cycle()
        self.assertEqual(context.version, 2)
        self.assertIn("branch:x", [node.node_id for node in context.gate.nodes()])

    def test_external_change_picked_up_on_refresh(self) -> None:
        manager, auditor = self._manager()
        doc = document(LEGACY, branches=[
            {"name": "y", "stage": "backwash", "before": "audit"}
        ])
        manager.submit(doc, effect=EFFECT_IMMEDIATE)
        external = TopologyManager(Store.open(self.store.path))
        self.assertEqual(external.version(), 2)
        self.assertIn("branch:y", external.current().node_ids())
        self.assertFalse(external.is_degraded())

    def test_immediate_change_while_cycle_runs_is_deferred_to_boundary(self) -> None:
        manager, _ = self._manager()
        running = manager.begin_cycle()
        doc = document(LEGACY, branches=[
            {"name": "z", "stage": "backwash", "after": "filter"}
        ])
        outcome = manager.submit(doc, effect=EFFECT_IMMEDIATE)
        # The request is held, not applied, so the open cycle keeps v1.
        self.assertTrue(outcome.deferred)
        self.assertTrue(outcome.staged)
        self.assertEqual(manager.version(), 1)
        self.assertEqual(running.version, 1)
        self.assertNotIn("branch:z", running.gate.node_ids())
        for node in running.gate.nodes():
            running.gate.mark(node.node_id)
        record = manager.finish_cycle(running, ok=True)
        self.assertEqual(record.topology_version, 1)
        # The next cycle promotes the deferred change.
        next_cycle = manager.begin_cycle()
        self.assertEqual(next_cycle.version, 2)
        self.assertIn("branch:z", next_cycle.gate.node_ids())

    def test_interrupted_running_cycle_is_marked_failed_on_restart(self) -> None:
        manager, _ = self._manager()
        context = manager.begin_cycle()
        context.gate.mark("intake")
        # Simulate a crash: the record stays "running" with no finish written.
        reopened = TopologyManager(self.store)
        records = reopened.cycles()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].status, "failed")
        self.assertEqual(records[0].error, "interrupted by restart")
        # A fresh cycle is allowed and sequence numbers do not collide.
        fresh = reopened.begin_cycle()
        self.assertNotEqual(fresh.cycle_id, context.cycle_id)
        self.assertEqual(fresh.record.topology_version, records[0].topology_version)


if __name__ == "__main__":
    unittest.main()

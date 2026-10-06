"""Behavioural tests for the configurable treatment line topology."""

from __future__ import annotations

import json
import tempfile
import unittest

from waterplant.audit import Auditor
from waterplant.console.seed import seed_defaults
from waterplant.console.server import Server
from waterplant.store import Store
from waterplant.topology import (
    IMMEDIATE,
    NEXT_CYCLE,
    ChangeConflict,
    TopologyError,
    TopologyRegistry,
    default_document,
    default_topology,
    parse_topology,
)
from waterplant.topology import registry as registry_module


def fresh_document() -> dict:
    return json.loads(json.dumps(default_document()))


def bypass_document(
    bypass_id: str = "filter-bypass",
    entry: str = "chlor",
    exit_: str = "clearwell",
    enabled: bool = True,
    counts_as_stage: bool = False,
) -> dict:
    document = fresh_document()
    document["nodes"].append(
        {
            "id": bypass_id,
            "stage": "filter",
            "kind": "bypass",
            "enabled": enabled,
            "counts_as_stage": counts_as_stage,
        }
    )
    document["edges"].append({"from": entry, "to": bypass_id})
    document["edges"].append({"from": bypass_id, "to": exit_})
    return document


def branch_document(counts_as_stage: bool = True) -> dict:
    document = fresh_document()
    document["nodes"].append(
        {
            "id": "side-turb",
            "stage": "turbidity",
            "kind": "branch",
            "counts_as_stage": counts_as_stage,
        }
    )
    document["edges"].append({"from": "clearwell", "to": "side-turb"})
    return document


class ValidationCase(unittest.TestCase):
    def test_default_document_is_valid(self) -> None:
        topology = default_topology()
        self.assertEqual(len(topology.main_order), 9)
        self.assertEqual(topology.main_order[0], "intake")
        self.assertEqual(topology.main_order[-1], "audit")
        self.assertEqual(topology.bypassed_node_ids(), {})

    def test_missing_required_stage_is_blocked(self) -> None:
        document = fresh_document()
        document["nodes"] = [node for node in document["nodes"] if node["id"] != "quota"]
        document["edges"] = [
            edge for edge in document["edges"] if "quota" not in (edge["from"], edge["to"])
        ]
        document["edges"].append({"from": "backwash", "to": "audit"})
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(document)
        self.assertIn("missing required stages: quota", str(ctx.exception))

    def test_out_of_order_stages_are_blocked(self) -> None:
        document = fresh_document()
        document["edges"] = [
            {"from": "intake", "to": "chlor"},
            {"from": "chlor", "to": "coag"},
            {"from": "coag", "to": "turbidity"},
            {"from": "turbidity", "to": "filter"},
            {"from": "filter", "to": "clearwell"},
            {"from": "clearwell", "to": "backwash"},
            {"from": "backwash", "to": "quota"},
            {"from": "quota", "to": "audit"},
        ]
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(document)
        self.assertIn("must come after", str(ctx.exception))

    def test_duplicate_node_id_is_blocked(self) -> None:
        document = fresh_document()
        document["nodes"].append({"id": "coag", "stage": "turbidity", "kind": "branch"})
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(document)
        self.assertIn("duplicate node id: coag", str(ctx.exception))

    def test_unknown_stage_is_blocked(self) -> None:
        document = fresh_document()
        document["nodes"][0]["stage"] = "magic"
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(document)
        self.assertIn("unknown stage: magic", str(ctx.exception))

    def test_unknown_edge_endpoint_is_blocked(self) -> None:
        document = fresh_document()
        document["edges"].append({"from": "intake", "to": "ghost"})
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(document)
        self.assertIn("unknown node: ghost", str(ctx.exception))

    def test_duplicate_and_self_edges_are_blocked(self) -> None:
        document = fresh_document()
        document["edges"].append({"from": "intake", "to": "coag"})
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(document)
        self.assertIn("duplicate edge", str(ctx.exception))
        document = fresh_document()
        document["edges"].append({"from": "coag", "to": "coag"})
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(document)
        self.assertIn("loops back", str(ctx.exception))

    def test_main_ring_is_blocked(self) -> None:
        document = fresh_document()
        document["edges"] = [
            edge
            for edge in document["edges"]
            if not (edge["from"] == "quota" and edge["to"] == "audit")
        ]
        document["edges"].append({"from": "quota", "to": "intake"})
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(document)
        self.assertIn("single", str(ctx.exception))

    def test_disabled_main_node_is_blocked(self) -> None:
        document = fresh_document()
        for node in document["nodes"]:
            if node["id"] == "filter":
                node["enabled"] = False
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(document)
        self.assertIn("cannot be disabled", str(ctx.exception))

    def test_required_stage_must_count(self) -> None:
        document = fresh_document()
        for node in document["nodes"]:
            if node["id"] == "filter":
                node["counts_as_stage"] = False
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(document)
        self.assertIn("must count as a stage", str(ctx.exception))

    def test_branch_attachment_rules(self) -> None:
        document = branch_document()
        document["edges"] = [
            edge for edge in document["edges"] if edge["to"] != "side-turb"
        ]
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(document)
        self.assertIn("take off from exactly one main stage", str(ctx.exception))

        document = branch_document()
        document["edges"].append({"from": "side-turb", "to": "coag"})
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(document)
        self.assertIn("rejoin downstream", str(ctx.exception))

    def test_bypass_attachment_rules(self) -> None:
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(bypass_document(entry="chlor", exit_="filter"))
        self.assertIn("does not skip any main stage", str(ctx.exception))

        with self.assertRaises(TopologyError) as ctx:
            parse_topology(bypass_document(entry="clearwell", exit_="chlor"))
        self.assertIn("exit downstream", str(ctx.exception))

        document = bypass_document()
        document["edges"] = [
            edge for edge in document["edges"] if edge["from"] != "filter-bypass"
        ]
        with self.assertRaises(TopologyError) as ctx:
            parse_topology(document)
        self.assertIn("exactly one entry and one exit", str(ctx.exception))


class DerivationCase(unittest.TestCase):
    def test_enabled_bypass_skips_main_stage(self) -> None:
        topology = parse_topology(bypass_document())
        self.assertEqual(topology.bypassed_node_ids(), {"filter": "filter-bypass"})
        executed = [node.id for node in topology.execution_nodes()]
        self.assertNotIn("filter", executed)
        self.assertIn("clearwell", executed)
        recorded = [node.id for node in topology.recorded_nodes()]
        self.assertNotIn("filter", recorded)
        self.assertNotIn("filter-bypass", recorded)

    def test_counted_bypass_takes_the_stage_slot(self) -> None:
        topology = parse_topology(bypass_document(counts_as_stage=True))
        recorded = [node.id for node in topology.recorded_nodes()]
        self.assertNotIn("filter", recorded)
        self.assertIn("filter-bypass", recorded)
        self.assertEqual(recorded.index("filter-bypass"), recorded.index("chlor") + 1)

    def test_disabled_bypass_changes_nothing(self) -> None:
        topology = parse_topology(bypass_document(enabled=False))
        self.assertEqual(topology.bypassed_node_ids(), {})
        executed = [node.id for node in topology.execution_nodes()]
        self.assertIn("filter", executed)

    def test_branch_runs_after_takeoff(self) -> None:
        topology = parse_topology(branch_document())
        executed = [node.id for node in topology.execution_nodes()]
        self.assertEqual(executed.index("side-turb"), executed.index("clearwell") + 1)
        recorded = [node.id for node in topology.recorded_nodes()]
        self.assertIn("side-turb", recorded)

    def test_uncounted_branch_executes_without_record(self) -> None:
        topology = parse_topology(branch_document(counts_as_stage=False))
        executed = [node.id for node in topology.execution_nodes()]
        self.assertIn("side-turb", executed)
        recorded = [node.id for node in topology.recorded_nodes()]
        self.assertNotIn("side-turb", recorded)


class RegistryCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        self.auditor = Auditor(self.store)
        self.registry = TopologyRegistry(self.store, self.auditor)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def topology_events(self) -> list[dict]:
        events = []
        for entry in self.auditor.filter("topology"):
            events.append(json.loads(entry.detail))
        return events

    def test_default_view_is_clean(self) -> None:
        view = self.registry.active()
        self.assertEqual(view.version, 0)
        self.assertFalse(view.degraded)
        self.assertEqual(len(view.topology.main_order), 9)

    def test_next_cycle_change_waits_for_the_boundary(self) -> None:
        result = self.registry.submit_change(bypass_document(), NEXT_CYCLE, "chg-1")
        self.assertEqual(result.status, "pending")
        self.assertFalse(result.replay)
        self.assertEqual(self.registry.active().version, 0)
        self.assertEqual(self.registry.pending()["change_id"], "chg-1")

        view = self.registry.begin_cycle()
        self.assertEqual(view.version, 1)
        self.assertEqual(view.topology.bypassed_node_ids(), {"filter": "filter-bypass"})
        self.assertIsNone(self.registry.pending())
        changes = {change["change_id"]: change for change in self.registry.changes()}
        self.assertEqual(changes["chg-1"]["status"], "active")
        events = [event["event"] for event in self.topology_events()]
        self.assertEqual(events, ["submitted", "activated"])

    def test_immediate_change_applies_at_once(self) -> None:
        result = self.registry.submit_change(bypass_document(), IMMEDIATE, "chg-now")
        self.assertEqual(result.status, "active")
        view = self.registry.active()
        self.assertEqual(view.version, 1)
        self.assertIn("filter", view.topology.bypassed_node_ids())

    def test_repeated_submission_is_idempotent(self) -> None:
        document = bypass_document()
        first = self.registry.submit_change(document, IMMEDIATE, "chg-1")
        second = self.registry.submit_change(document, IMMEDIATE, "chg-1")
        self.assertFalse(first.replay)
        self.assertTrue(second.replay)
        self.assertEqual(first.version, second.version)
        self.assertEqual(len(self.registry.changes()), 1)
        self.assertEqual(len(self.topology_events()), 1)
        self.assertEqual(self.registry.active().version, 1)

    def test_conflicting_change_id_is_rejected(self) -> None:
        self.registry.submit_change(bypass_document(), IMMEDIATE, "chg-1")
        with self.assertRaises(ChangeConflict):
            self.registry.submit_change(branch_document(), IMMEDIATE, "chg-1")

    def test_new_pending_change_supersedes_the_old_one(self) -> None:
        self.registry.submit_change(bypass_document(), NEXT_CYCLE, "chg-a")
        self.registry.submit_change(branch_document(), NEXT_CYCLE, "chg-b")
        changes = {change["change_id"]: change for change in self.registry.changes()}
        self.assertEqual(changes["chg-a"]["status"], "superseded")
        self.assertEqual(changes["chg-b"]["status"], "pending")
        view = self.registry.begin_cycle()
        self.assertEqual(view.version, 1)
        self.assertEqual(view.topology.bypassed_node_ids(), {})
        events = [event["event"] for event in self.topology_events()]
        self.assertEqual(events, ["submitted", "superseded", "submitted", "activated"])

    def test_begin_cycle_is_idempotent(self) -> None:
        self.registry.submit_change(bypass_document(), NEXT_CYCLE, "chg-1")
        first = self.registry.begin_cycle()
        second = self.registry.begin_cycle()
        self.assertEqual(first.version, second.version)
        self.assertEqual(len(self.registry.changes()), 1)
        self.assertEqual(len(self.topology_events()), 2)

    def test_corrupt_document_falls_back_to_last_valid(self) -> None:
        self.registry.submit_change(bypass_document(), IMMEDIATE, "chg-1")
        self.assertEqual(self.registry.active().version, 1)
        self.store.put(registry_module.DOCUMENT_KEY, "{not json")

        view = self.registry.active()
        self.assertTrue(view.degraded)
        self.assertIn("unreadable", view.degraded_reason)
        self.assertEqual(view.version, 1)
        self.assertIn("filter", view.topology.bypassed_node_ids())

        fresh = TopologyRegistry(self.store, self.auditor)
        recovered = fresh.active()
        self.assertTrue(recovered.degraded)
        self.assertEqual(recovered.version, 1)
        self.assertIn("filter", recovered.topology.bypassed_node_ids())

    def test_missing_document_falls_back_to_persisted_copy(self) -> None:
        self.registry.submit_change(bypass_document(), IMMEDIATE, "chg-1")
        self.store.delete(registry_module.DOCUMENT_KEY)
        fresh = TopologyRegistry(self.store, self.auditor)
        view = fresh.active()
        self.assertTrue(view.degraded)
        self.assertIn("missing", view.degraded_reason)
        self.assertEqual(view.version, 1)

    def test_recovery_clears_the_degraded_mark(self) -> None:
        self.registry.submit_change(bypass_document(), IMMEDIATE, "chg-1")
        self.store.put(registry_module.DOCUMENT_KEY, "garbage")
        self.assertTrue(self.registry.active().degraded)
        self.registry.submit_change(fresh_document(), IMMEDIATE, "chg-2")
        view = self.registry.active()
        self.assertFalse(view.degraded)
        self.assertEqual(view.version, 2)

    def test_cycle_records_stay_consistent_with_versions(self) -> None:
        view = self.registry.begin_cycle()
        first = self.registry.record_cycle(view, ["intake"])
        self.registry.submit_change(bypass_document(), NEXT_CYCLE, "chg-1")
        view = self.registry.begin_cycle()
        second = self.registry.record_cycle(view, ["intake"])
        self.assertEqual(first.topology_version, 0)
        self.assertEqual(second.topology_version, 1)
        self.assertEqual(second.cycle, first.cycle + 1)
        self.assertEqual(second.bypassed, ["filter"])
        cycles = self.registry.cycles()
        self.assertEqual([cycle["topology_version"] for cycle in cycles], [0, 1])


class ConsoleTopologyCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = Store.open(f"{self._tmp.name}/state.json")
        seed_defaults(self.store)
        self.server = Server(self.store)

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def call(self, method: str, path: str, payload: dict | None = None):
        response = self.server.dispatch(method, path, payload)
        body = response.body.decode("utf-8")
        parsed = json.loads(body) if response.content_type.startswith("application/json") else body
        return response.status, parsed

    def cycle_payload(self) -> dict:
        return {
            "flow": 800.0,
            "samples": [2.0, 4.0],
            "demand": 0.5,
            "level": 10.0,
            "zone": 1,
            "amount": 12.0,
        }

    def test_topology_state_endpoint(self) -> None:
        status, body = self.call("GET", "/topology")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 0)
        self.assertFalse(body["degraded"])
        self.assertEqual(body["main_chain"][0], "intake")
        self.assertEqual(body["main_chain"][-1], "audit")
        self.assertIsNone(body["pending"])
        self.assertIsNone(body["last_cycle"])

    def test_change_validation_is_blocked_early(self) -> None:
        status, body = self.call("POST", "/topology/change", {"document": fresh_document()})
        self.assertEqual(status, 400)
        self.assertIn("change_id", body["error"])

        status, body = self.call(
            "POST",
            "/topology/change",
            {"change_id": "c1", "activation": "whenever", "document": fresh_document()},
        )
        self.assertEqual(status, 400)
        self.assertIn("activation", body["error"])

        status, body = self.call("POST", "/topology/change", {"change_id": "c1"})
        self.assertEqual(status, 400)
        self.assertIn("document", body["error"])

        document = fresh_document()
        document["nodes"] = [node for node in document["nodes"] if node["id"] != "quota"]
        document["edges"] = [
            edge for edge in document["edges"] if "quota" not in (edge["from"], edge["to"])
        ]
        document["edges"].append({"from": "backwash", "to": "audit"})
        status, body = self.call(
            "POST", "/topology/change", {"change_id": "c1", "document": document}
        )
        self.assertEqual(status, 400)
        self.assertIn("missing required stages: quota", body["error"])
        self.assertEqual(self.server.runtime.topology.changes(), [])

    def test_next_cycle_flow_and_cycle_records(self) -> None:
        status, body = self.call(
            "POST",
            "/topology/change",
            {"change_id": "chg-1", "document": bypass_document()},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "pending")
        self.assertEqual(body["version"], 1)

        status, body = self.call("GET", "/topology")
        self.assertEqual(body["pending"]["change_id"], "chg-1")
        self.assertEqual(body["version"], 0)

        status, body = self.call("POST", "/cycle", self.cycle_payload())
        self.assertEqual(status, 200)
        self.assertEqual(body["topology_version"], 1)
        self.assertEqual(body["cycle"], 1)
        self.assertEqual(body["bypassed"], ["filter"])
        self.assertNotIn("filter", body["stages"])
        self.assertEqual(body["stages"][-1], "audit")
        self.assertFalse(body["topology_degraded"])

        status, body = self.call("GET", "/topology/cycles")
        self.assertEqual(body["count"], 1)
        record = body["cycles"][0]
        self.assertEqual(record["topology_version"], 1)
        self.assertEqual(record["bypassed"], ["filter"])
        self.assertIn("intake", record["executed"])

        status, body = self.call("GET", "/topology/changes")
        change = body["changes"][0]
        self.assertEqual(change["change_id"], "chg-1")
        self.assertEqual(change["status"], "active")
        self.assertNotIn("document", change)

        status, body = self.call("GET", "/topology")
        self.assertIsNone(body["pending"])
        self.assertEqual(body["last_cycle"]["topology_version"], 1)

    def test_replay_and_conflict_over_http(self) -> None:
        payload = {
            "change_id": "chg-1",
            "activation": "immediate",
            "document": bypass_document(),
        }
        status, body = self.call("POST", "/topology/change", payload)
        self.assertEqual(status, 200)
        self.assertFalse(body["replay"])
        status, body = self.call("POST", "/topology/change", payload)
        self.assertEqual(status, 200)
        self.assertTrue(body["replay"])
        self.assertEqual(body["version"], 1)

        payload = dict(payload, document=branch_document())
        status, body = self.call("POST", "/topology/change", payload)
        self.assertEqual(status, 409)
        self.assertIn("different document", body["error"])

    def test_bypassed_stage_drops_out_of_the_cycle(self) -> None:
        document = bypass_document(
            bypass_id="chlor-bypass", entry="turbidity", exit_="filter"
        )
        document["nodes"][-1]["stage"] = "chlor"
        status, _ = self.call(
            "POST",
            "/topology/change",
            {"change_id": "chg-bypass", "activation": "immediate", "document": document},
        )
        self.assertEqual(status, 200)
        status, body = self.call("POST", "/cycle", self.cycle_payload())
        self.assertEqual(status, 200)
        self.assertEqual(body["chlor_dose"], 0.0)
        self.assertEqual(body["bypassed"], ["chlor"])
        self.assertNotIn("chlor", body["stages"])
        self.assertEqual(body["coag_dose"], 800.0)

    def test_pipeline_and_snapshot_follow_the_active_topology(self) -> None:
        self.call(
            "POST",
            "/topology/change",
            {"change_id": "chg-1", "activation": "immediate", "document": bypass_document()},
        )
        status, body = self.call("GET", "/pipeline")
        self.assertEqual(status, 200)
        self.assertEqual(body["version"], 1)
        self.assertTrue(body["ordered"])
        self.assertEqual(body["last_stage"], "audit")
        step_stages = [step["stage"] for step in body["steps"]]
        self.assertNotIn("filter", step_stages)

        status, body = self.call("GET", "/snapshot")
        self.assertEqual(body["topology"]["version"], 1)
        self.assertEqual(body["topology"]["bypassed_stages"], ["filter"])

        status, body = self.call("GET", "/health/checks")
        topology_check = [c for c in body["checks"] if c["name"] == "topology"][0]
        self.assertEqual(topology_check["status"], "ok")

    def test_degraded_topology_is_marked_everywhere(self) -> None:
        self.call(
            "POST",
            "/topology/change",
            {"change_id": "chg-1", "activation": "immediate", "document": bypass_document()},
        )
        self.store.put(registry_module.DOCUMENT_KEY, "{broken")

        status, body = self.call("GET", "/topology")
        self.assertEqual(status, 200)
        self.assertTrue(body["degraded"])
        self.assertIn("unreadable", body["degraded_reason"])
        self.assertEqual(body["version"], 1)

        status, body = self.call("POST", "/cycle", self.cycle_payload())
        self.assertEqual(status, 200)
        self.assertTrue(body["topology_degraded"])
        self.assertEqual(body["topology_version"], 1)
        self.assertEqual(body["bypassed"], ["filter"])

        status, body = self.call("GET", "/health/checks")
        topology_check = [c for c in body["checks"] if c["name"] == "topology"][0]
        self.assertEqual(topology_check["status"], "warn")

        status, body = self.call("GET", "/topology/cycles")
        self.assertTrue(body["cycles"][0]["degraded"])

    def test_audit_trail_records_topology_changes(self) -> None:
        self.call(
            "POST",
            "/topology/change",
            {"change_id": "chg-1", "document": bypass_document()},
        )
        self.call("POST", "/cycle", self.cycle_payload())
        status, body = self.call("POST", "/audit/filter", {"kind": "topology"})
        self.assertEqual(status, 200)
        events = [json.loads(entry["detail"])["event"] for entry in body["entries"]]
        self.assertEqual(events, ["submitted", "activated"])


if __name__ == "__main__":
    unittest.main()

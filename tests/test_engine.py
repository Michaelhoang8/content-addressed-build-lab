import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from buildlab import operations
from buildlab.engine import Engine, Trace, action_key, atomic_replace, validate_graph
from scripts.fixtures import edit_first_amount, generate


class BuildTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "workspace"
        self.cache = self.root / "cache"
        self.output = self.root / "output"
        self.spec = generate(self.workspace, count=40)
        self.engine = Engine(self.workspace, self.cache, self.output)

    def statuses(self, result, status):
        return {
            name
            for name, action in result["actions"].items()
            if action["status"] == status
        }

    def test_clean_warm_and_selective_dependency_closure(self):
        clean = self.engine.build(self.spec)
        self.assertEqual(clean["status"], "SUCCESS")
        self.assertEqual(len(self.statuses(clean, "BUILT")), 7)
        warm = self.engine.build(self.spec)
        self.assertEqual(len(self.statuses(warm, "HIT")), 7)
        self.assertEqual(
            clean["pointer"]["artifact_digest"], warm["pointer"]["artifact_digest"]
        )
        edit_first_amount(self.workspace)
        edited = self.engine.build(self.spec)
        self.assertEqual(
            self.statuses(edited, "BUILT"),
            {"orders", "validated", "revenue", "report", "bundle"},
        )
        self.assertEqual(self.statuses(edited, "HIT"), {"customers", "schema_source"})
        self.assertNotEqual(
            edited["pointer"]["artifact_digest"], warm["pointer"]["artifact_digest"]
        )

    def test_byte_change_with_same_output_stops_invalidation(self):
        clean = self.engine.build(self.spec)
        with (self.workspace / "orders.csv").open(
            "a", encoding="utf-8", newline="\n"
        ) as stream:
            stream.write("\n")
        result = self.engine.build(self.spec)
        self.assertEqual(self.statuses(result, "BUILT"), {"orders"})
        self.assertEqual(len(self.statuses(result, "HIT")), 6)
        self.assertEqual(
            result["pointer"]["artifact_digest"], clean["pointer"]["artifact_digest"]
        )

    def test_corrupt_cache_is_quarantined_and_rebuilt(self):
        clean = self.engine.build(self.spec)
        key = clean["actions"]["customers"]["key"]
        path = self.cache / "objects" / key / "files/customers.json"
        path.write_bytes(path.read_bytes() + b"corrupt")
        repaired = self.engine.build(self.spec)
        self.assertEqual(self.statuses(repaired, "BUILT"), {"customers"})
        self.assertEqual(
            len([e for e in repaired["events"] if e["event"] == "CACHE_REJECTED"]), 1
        )
        self.assertEqual(len(list((self.cache / "quarantine").iterdir())), 1)
        self.assertEqual(
            repaired["pointer"]["artifact_digest"], clean["pointer"]["artifact_digest"]
        )

    def test_failed_action_does_not_select_partial_release(self):
        self.engine.build(self.spec)
        before = (self.output / "current.json").read_bytes()
        broken = copy.deepcopy(self.spec)
        report = next(
            action for action in broken["actions"] if action["id"] == "report"
        )
        report.update(operation="fail_after_write", argv=[], config={})
        result = self.engine.build(broken)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual((self.output / "current.json").read_bytes(), before)
        failure = next(e for e in result["events"] if e["event"] == "ACTION_FAILED")
        self.assertEqual(failure["staged_files"], ["report.md"])
        self.assertFalse((self.cache / "objects" / failure["key"]).exists())
        self.assertEqual(list((self.cache / "staging").iterdir()), [])
        self.assertEqual(set(result["not_completed"]), {"report", "bundle"})
        self.assertEqual(len(list((self.output / "releases").iterdir())), 1)

    def test_concurrent_ready_actions_and_dependency_gates(self):
        barrier = threading.Barrier(3)
        original = operations.transform

        def synchronized(action, inputs, dependencies, stage):
            if action["id"] in {"orders", "customers", "schema_source"}:
                barrier.wait(timeout=3)
            return original(action, inputs, dependencies, stage)

        with patch.object(operations, "transform", synchronized):
            result = self.engine.build(self.spec)
        self.assertEqual(result["status"], "SUCCESS")
        starts = {
            e["action"]: e for e in result["events"] if e["event"] == "ACTION_STARTED"
        }
        finishes = {
            e["action"]: e for e in result["events"] if e["event"] == "ACTION_BUILT"
        }
        self.assertEqual(
            len(
                {
                    starts[name]["thread"]
                    for name in ["orders", "customers", "schema_source"]
                }
            ),
            3,
        )
        for action in self.spec["actions"]:
            for dependency in action["deps"]:
                self.assertGreater(
                    starts[action["id"]]["event_id"], finishes[dependency]["event_id"]
                )

    def test_graph_errors_and_paths_precede_cache_mutation(self):
        for change in ["unknown", "cycle", "path", "non_json_key"]:
            spec = copy.deepcopy(self.spec)
            if change == "unknown":
                spec["actions"][0]["deps"] = ["missing"]
            elif change == "cycle":
                spec["actions"][0]["deps"] = ["bundle"]
            elif change == "path":
                spec["actions"][0]["outputs"] = ["../escape.json"]
            else:
                spec["actions"][0]["config"] = {1: "non-JSON key"}
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.engine.build(spec)
        self.assertFalse(self.cache.exists())

    def test_cache_lock_is_exclusive(self):
        self.cache.mkdir()
        (self.cache / "build.lock").write_text("external owner")
        with self.assertRaises(RuntimeError):
            self.engine.build(self.spec)
        self.assertEqual((self.cache / "build.lock").read_text(), "external owner")

    def test_windows_replace_retries_are_bounded_and_recorded(self):
        sharing_error = PermissionError("injected Windows sharing failure")
        sharing_error.winerror = 32
        trace = Trace()
        with patch(
            "buildlab.engine.os.replace", side_effect=[sharing_error, None]
        ) as replace, patch("buildlab.engine.time.sleep") as sleep:
            atomic_replace("source", "target", trace, "test")
        self.assertEqual(replace.call_count, 2)
        sleep.assert_called_once_with(0.01)
        self.assertEqual(trace.events[0]["event"], "RENAME_RETRY")
        with patch(
            "buildlab.engine.os.replace", side_effect=sharing_error
        ) as replace, patch("buildlab.engine.time.sleep") as sleep:
            with self.assertRaises(PermissionError):
                atomic_replace("source", "target", Trace(), "test")
        self.assertEqual(replace.call_count, 6)
        self.assertEqual(sleep.call_count, 5)
        with patch(
            "buildlab.engine.os.replace", side_effect=PermissionError("permanent")
        ) as replace, patch("buildlab.engine.time.sleep") as sleep:
            with self.assertRaises(PermissionError):
                atomic_replace("source", "target", Trace(), "test")
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()

    def test_key_and_column_order_semantics(self):
        action = self.spec["actions"][0]
        data = {"orders.csv": (self.workspace / "orders.csv").read_bytes()}
        tool = {"version": "one"}
        baseline = action_key(action, data, {}, tool)
        reordered = copy.deepcopy(action)
        reordered["config"]["columns"] = dict(
            reversed(list(action["config"]["columns"].items()))
        )
        self.assertEqual(baseline, action_key(reordered, data, {}, tool))
        for field, value in [
            ("argv", ["--sort-by", "amount_cents"]),
            ("config", {"columns": {"order_id": "string"}}),
            ("outputs", ["other.json"]),
        ]:
            changed = copy.deepcopy(action)
            changed[field] = value
            self.assertNotEqual(baseline, action_key(changed, data, {}, tool))
        self.assertNotEqual(
            baseline,
            action_key(action, {"orders.csv": data["orders.csv"] + b"\n"}, {}, tool),
        )
        self.assertNotEqual(baseline, action_key(action, data, {}, {"version": "two"}))
        first_dependency = {"upstream": {"file.json": {"sha256": "a" * 64, "bytes": 1}}}
        changed_dependency = {
            "upstream": {"file.json": {"sha256": "b" * 64, "bytes": 1}}
        }
        self.assertNotEqual(
            action_key(action, data, first_dependency, tool),
            action_key(action, data, changed_dependency, tool),
        )
        clean = self.engine.build(self.spec)
        changed_spec = copy.deepcopy(self.spec)
        changed_spec["actions"][0] = reordered
        self.assertEqual(len(self.statuses(self.engine.build(changed_spec), "HIT")), 7)


if __name__ == "__main__":
    unittest.main()

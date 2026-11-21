"""Independent release-selection failure and key-equivalence checks."""

import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from buildlab.engine import Engine


def specification():
    return {
        "format": 1,
        "actions": [
            {
                "id": "parse",
                "operation": "csv_to_json",
                "deps": [],
                "inputs": ["items.csv"],
                "outputs": ["items.json"],
                "argv": ["--sort-by", "id"],
                "config": {"columns": {"id": "integer", "name": "string"}},
            }
        ],
    }


class ReviewTests(unittest.TestCase):
    def test_failed_pointer_replace_preserves_selected_release_and_reuses_valid_cache(
        self,
    ):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace, cache, output = root / "src", root / "cache", root / "out"
            workspace.mkdir()
            source = workspace / "items.csv"
            source.write_text("id,name\n1,first\n", encoding="utf-8")
            engine = Engine(workspace, cache, output)
            initial = engine.build(specification())
            self.assertEqual(initial["status"], "SUCCESS")
            pointer = output / "current.json"
            old_pointer = pointer.read_bytes()
            selected = output / "releases" / initial["pointer"]["release"]
            old_content = (selected / "actions/parse/items.json").read_bytes()
            source.write_text("id,name\n1,second\n", encoding="utf-8")
            replace = os.replace

            def reject_selection(source_path, destination):
                if Path(destination) == pointer:
                    raise OSError("Injected release-pointer replacement failure")
                return replace(source_path, destination)

            with patch("buildlab.engine.os.replace", side_effect=reject_selection):
                failed = engine.build(specification())
            self.assertEqual(failed["status"], "FAILED")
            self.assertIsNone(failed["pointer"])
            self.assertEqual(pointer.read_bytes(), old_pointer)
            self.assertEqual(
                (selected / "actions/parse/items.json").read_bytes(), old_content
            )
            self.assertEqual(list(output.glob(".current-*")), [])
            self.assertFalse((cache / "build.lock").exists())

            recovered = engine.build(specification())
            self.assertEqual(recovered["status"], "SUCCESS")
            self.assertEqual(recovered["actions"]["parse"]["status"], "HIT")
            self.assertNotEqual(
                recovered["pointer"]["artifact_digest"],
                initial["pointer"]["artifact_digest"],
            )
            current = (
                output
                / "releases"
                / recovered["pointer"]["release"]
                / "actions/parse/items.json"
            )
            self.assertEqual(
                json.loads(current.read_bytes()), [{"id": 1, "name": "second"}]
            )

    def test_column_mapping_order_is_equivalent_in_both_key_and_behavior(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "src"
            workspace.mkdir()
            (workspace / "items.csv").write_text("id,name\n7,seven\n", encoding="utf-8")
            first = specification()
            second = copy.deepcopy(first)
            second["actions"][0]["config"]["columns"] = {
                "name": "string",
                "id": "integer",
            }
            engine = Engine(workspace, root / "cache", root / "out")
            built = engine.build(first)
            reused = engine.build(second)
            self.assertEqual(built["status"], "SUCCESS")
            self.assertEqual(reused["status"], "SUCCESS")
            self.assertEqual(reused["actions"]["parse"]["status"], "HIT")
            self.assertEqual(
                reused["actions"]["parse"]["key"], built["actions"]["parse"]["key"]
            )
            fresh = Engine(workspace, root / "fresh-cache", root / "fresh-out").build(
                second
            )
            self.assertEqual(fresh["status"], "SUCCESS")
            self.assertEqual(
                fresh["pointer"]["artifact_digest"], built["pointer"]["artifact_digest"]
            )

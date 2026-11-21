import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.fixtures import generate

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_missing_workspace_and_invalid_graph_are_concise_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            command = [
                sys.executable,
                "-m",
                "buildlab",
                str(workspace),
                "--cache",
                str(root / "cache"),
                "--output",
                str(root / "output"),
            ]
            missing = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, timeout=5
            )
            self.assertEqual(missing.returncode, 2)
            self.assertIn("buildlab:", missing.stderr)
            self.assertNotIn("Traceback", missing.stderr)
            workspace.mkdir()
            (workspace / "build.json").write_text('{"format": 1, "actions": []}')
            invalid = subprocess.run(
                command, cwd=ROOT, capture_output=True, text=True, timeout=5
            )
            self.assertEqual(invalid.returncode, 2)
            self.assertIn("1..32 actions", invalid.stderr)
            self.assertNotIn("Traceback", invalid.stderr)

    def test_action_failure_has_distinct_exit_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "workspace"
            spec = generate(workspace, count=10)
            spec["actions"][0]["operation"] = "fail_after_write"
            (workspace / "build.json").write_text(json.dumps(spec))
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "buildlab",
                    str(workspace),
                    "--cache",
                    str(root / "cache"),
                    "--output",
                    str(root / "output"),
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertEqual(result.returncode, 1)
            self.assertEqual(json.loads(result.stdout)["status"], "FAILED")
            self.assertNotIn("Traceback", result.stderr)

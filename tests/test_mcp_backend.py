from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import mcp_backend


class McpBackendTests(unittest.TestCase):
    def test_task_contract_requires_scope_acceptance_and_verification(self):
        base = {
            "task": "Fix the focused behavior",
            "scope": ["src/app.py"],
            "constraints": [],
            "acceptance": ["Relevant test passes"],
            "verify_commands": ["python -m unittest"],
        }
        spec = mcp_backend.build_task_spec(**base)
        self.assertEqual(spec["maxRounds"], 2)
        for field in ("scope", "acceptance", "verify_commands"):
            invalid = dict(base)
            invalid[field] = []
            with self.subTest(field=field), self.assertRaises(mcp_backend.BackendError):
                mcp_backend.build_task_spec(**invalid)

    def test_mcp_limits_rounds_and_verification_count(self):
        base = {
            "task": "Fix it",
            "scope": ["src/app.py"],
            "constraints": [],
            "acceptance": ["Done"],
            "verify_commands": ["true"],
        }
        with self.assertRaises(mcp_backend.BackendError):
            mcp_backend.build_task_spec(**base, max_rounds=3)
        with self.assertRaises(mcp_backend.BackendError):
            mcp_backend.build_task_spec(
                **{**base, "verify_commands": ["true"] * 5}
            )

    def test_get_run_sanitizes_session_and_worker_message(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run_id = "20260821-120000-abcdef12"
            directory = root / run_id
            directory.mkdir()
            (directory / "summary.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "runId": run_id,
                        "project": "/repo",
                        "openCodeSessionId": "secret-session",
                        "rounds": [
                            {
                                "round": 1,
                                "exitCode": 0,
                                "sessionId": "secret-session",
                                "workerMessage": "raw model output",
                                "tools": [{"tool": "edit", "status": "completed"}],
                            }
                        ],
                        "verification": [{"command": "test", "exitCode": 0}],
                    }
                ),
                encoding="utf-8",
            )
            result = mcp_backend.get_run(run_id, root=root)
            serialized = json.dumps(result)
            self.assertTrue(result["ok"])
            self.assertNotIn("secret-session", serialized)
            self.assertNotIn("raw model output", serialized)

    def test_get_run_rejects_path_traversal(self):
        with self.assertRaises(mcp_backend.BackendError):
            mcp_backend.get_run("../summary")

    def test_get_run_reports_corrupt_summary_as_safe_error(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run_id = "20260821-120002-abcdef14"
            directory = root / run_id
            directory.mkdir()
            (directory / "summary.json").write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(mcp_backend.BackendError, "unreadable"):
                mcp_backend.get_run(run_id, root=root)

    def test_list_runs_reports_dry_run(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            run_id = "20260821-120001-abcdef13"
            directory = root / run_id
            directory.mkdir()
            (directory / "metadata.json").write_text(
                json.dumps({"project": "/repo"}), encoding="utf-8"
            )
            result = mcp_backend.list_runs(root=root)
            self.assertEqual(result["runs"][0]["status"], "dry-run")


if __name__ == "__main__":
    unittest.main()

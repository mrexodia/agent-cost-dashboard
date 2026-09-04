import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import cost_dashboard


class StandardSessionMetadataTests(unittest.TestCase):
    def test_discovers_sessions_for_each_pi_profile(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            profile_sessions = Path(temp_dir) / ".pi" / "custom-profile" / "sessions"
            profile_sessions.mkdir(parents=True)
            with patch("cost_dashboard.Path.home", return_value=Path(temp_dir)):
                discovered = cost_dashboard.get_sessions_dirs()

            self.assertIn(
                (profile_sessions, "pi", "standard"),
                discovered,
            )

    def write_session(self, directory: Path, records: list[dict]) -> Path:
        path = directory / "session.jsonl"
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    def test_omp_title_record_before_session_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            path = self.write_session(
                project_dir,
                [
                    {
                        "type": "title",
                        "v": 1,
                        "title": "Example session",
                        "updatedAt": "2026-08-09T10:00:00.000Z",
                        "pad": " ",
                    },
                    {
                        "type": "session",
                        "version": 3,
                        "id": "omp-session-id",
                        "timestamp": "2026-08-09T10:00:00.000Z",
                        "cwd": "/workspace/example",
                    },
                    {
                        "type": "message",
                        "timestamp": "2026-08-09T10:00:01.000Z",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "Hello"}],
                        },
                    },
                    {
                        "type": "message",
                        "timestamp": "2026-08-09T10:00:02.000Z",
                        "message": {
                            "role": "assistant",
                            "model": "test-model",
                            "content": [{"type": "text", "text": "Hi"}],
                            "usage": {
                                "input": 10,
                                "output": 2,
                                "cacheRead": 0,
                                "cacheWrite": 0,
                                "totalTokens": 12,
                                "cost": {"total": 0.01},
                            },
                        },
                    },
                ],
            )

            self.assertEqual(
                cost_dashboard.get_session_id_from_file(str(path)),
                "omp-session-id",
            )
            self.assertEqual(
                cost_dashboard.get_project_path_from_jsonl(project_dir),
                "/workspace/example",
            )
            stats = cost_dashboard.analyze_jsonl_file(path)
            self.assertEqual(stats["cwd"], "/workspace/example")
            self.assertEqual(stats["messages"], 1)
            self.assertEqual(stats["total_tokens"], 12)

    def test_zeroed_local_usage_gets_estimated_tokens_and_tps(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_session(
                Path(temp_dir),
                [
                    {"type": "session", "id": "local-session", "cwd": "/workspace/local"},
                    {
                        "type": "message",
                        "timestamp": "2026-08-09T10:00:01.000Z",
                        "message": {
                            "role": "user",
                            "content": [{"type": "text", "text": "Please help"}],
                        },
                    },
                    {
                        "type": "message",
                        "timestamp": "2026-08-09T10:00:03.000Z",
                        "message": {
                            "role": "assistant",
                            "model": "local-model",
                            "content": [{"type": "text", "text": "Here is a useful answer."}],
                            "usage": {"input": 0, "output": 0, "totalTokens": 0},
                        },
                    },
                ],
            )

            stats = cost_dashboard.analyze_jsonl_file(path)
            self.assertTrue(stats["tokens_estimated"])
            self.assertGreater(stats["input_tokens"], 0)
            self.assertGreater(stats["output_tokens"], 0)
            self.assertGreater(stats["total_tokens"], 0)
            self.assertEqual(stats["cost_total"], 0.0)
            self.assertTrue(stats["tps_samples"])
            self.assertGreater(cost_dashboard.calc_avg_tokens_per_sec(stats["tps_samples"]), 0)

    def test_pi_usage_reasoning_and_numeric_timestamp_are_normalized(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = self.write_session(
                Path(temp_dir),
                [
                    {"type": "session", "id": "numeric-session", "cwd": "/workspace/pi"},
                    {
                        "type": "message",
                        "message": {
                            "role": "user",
                            "timestamp": 1786269601000,
                            "content": "Hello",
                        },
                    },
                    {
                        "type": "message",
                        "message": {
                            "role": "assistant",
                            "timestamp": 1786269603000,
                            "model": "test-model",
                            "content": "Hi",
                            "usage": {
                                "inputTokens": 10,
                                "outputTokens": 2,
                                "reasoningTokens": 3,
                                "totalTokens": 15,
                                "cost": {"total": 0.01},
                            },
                        },
                    },
                ],
            )

            stats = cost_dashboard.analyze_jsonl_file(path)
            self.assertEqual(stats["input_tokens"], 10)
            self.assertEqual(stats["output_tokens"], 2)
            self.assertEqual(stats["reasoning_tokens"], 3)
            self.assertEqual(stats["total_tokens"], 15)
            self.assertFalse(stats["tokens_estimated"])
            self.assertEqual(stats["tps_samples"], [(2, 2.0, "test-model")])

    def test_pi_session_metadata_on_first_line_still_works(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            project_dir = Path(temp_dir)
            path = self.write_session(
                project_dir,
                [
                    {
                        "type": "session",
                        "id": "pi-session-id",
                        "timestamp": "2026-08-09T10:00:00.000Z",
                        "cwd": "/workspace/pi",
                    }
                ],
            )

            self.assertEqual(
                cost_dashboard.get_session_id_from_file(str(path)),
                "pi-session-id",
            )
            self.assertEqual(
                cost_dashboard.get_project_path_from_jsonl(project_dir),
                "/workspace/pi",
            )
            self.assertEqual(
                cost_dashboard.analyze_jsonl_file(path)["cwd"],
                "/workspace/pi",
            )


if __name__ == "__main__":
    unittest.main()

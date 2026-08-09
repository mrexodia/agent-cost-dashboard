import json
import tempfile
import unittest
from pathlib import Path

import cost_dashboard


class StandardSessionMetadataTests(unittest.TestCase):
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

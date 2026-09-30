import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import cost_dashboard


def varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def field_varint(num: int, value: int) -> bytes:
    return varint((num << 3) | 0) + varint(value)


def field_bytes(num: int, value: bytes) -> bytes:
    return varint((num << 3) | 2) + varint(len(value)) + value


def field_text(num: int, value: str) -> bytes:
    return field_bytes(num, value.encode("utf-8"))


def timestamp(seconds: int) -> bytes:
    return field_varint(1, seconds) + field_varint(2, 0)


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


class AntigravitySessionTests(unittest.TestCase):
    """Lock in how an Antigravity conversation database is decoded.

    The step payloads are protobuf blobs, so these build them by hand rather
    than shipping a binary fixture.
    """

    def build_db(self, path: Path, steps: list[tuple]) -> None:
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE steps (idx integer, step_type integer, status integer,"
            " metadata blob, step_payload blob)"
        )
        for idx, (step_type, status, metadata, payload) in enumerate(steps):
            conn.execute(
                "INSERT INTO steps VALUES (?,?,?,?,?)",
                (idx, step_type, status, metadata, payload),
            )
        conn.commit()
        conn.close()

    def usage_step(self, model_id, input_tok, output_tok, cached, reasoning, start, end):
        usage = (
            field_varint(1, model_id)
            + field_varint(2, input_tok)
            + field_varint(3, output_tok)
            + field_varint(5, cached)
            + field_varint(9, reasoning)
            + field_varint(10, output_tok - reasoning)
        )
        metadata = (
            field_bytes(1, timestamp(start))
            + field_bytes(8, timestamp(end))
            + field_bytes(9, usage)
        )
        return (15, 3, metadata, b"")

    def tool_step(self, name, start, end, status=3):
        call = field_text(1, "toolu_1") + field_text(2, name)
        metadata = (
            field_bytes(1, timestamp(start))
            + field_bytes(4, call)
            + field_bytes(8, timestamp(end))
        )
        return (132, status, metadata, b"")

    def test_tokens_are_summed_not_differenced(self):
        # Antigravity reports uncached input and cache reads as disjoint
        # buckets (cached far exceeds input here), so they must be added
        # together rather than subtracted from one another.
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "conv.db"
            self.build_db(
                path,
                [self.usage_step(1020, 1000, 300, 16000, 100, 100, 105)],
            )

            stats = cost_dashboard.analyze_antigravity_db_file(path)

            self.assertEqual(stats["messages"], 1)
            self.assertEqual(stats["input_tokens"], 1000)
            self.assertEqual(stats["cache_read_tokens"], 16000)
            # Reasoning is a slice of the output count, so the itemised
            # output excludes it and the total matches their sum exactly.
            self.assertEqual(stats["output_tokens"], 200)
            self.assertEqual(stats["reasoning_tokens"], 100)
            self.assertEqual(stats["total_tokens"], 1000 + 200 + 16000)

    def test_cost_prices_the_full_output_count(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "conv.db"
            # 1000 uncached input, 300 generated (100 of it reasoning),
            # 16000 cache reads on Gemini 3.8 Flash ($0.50/$3.00/$0.05).
            self.build_db(
                path,
                [self.usage_step(1322, 1000, 300, 16000, 100, 100, 105)],
            )

            stats = cost_dashboard.analyze_antigravity_db_file(path)

            expected = (
                1000 * 0.50 + 300 * 3.00 + 16000 * 0.05
            ) / 1_000_000
            self.assertAlmostEqual(stats["cost_total"], expected, places=9)
            self.assertIn("Gemini 3.8 Flash", stats["models"])

    def test_tool_calls_are_timed_and_errors_counted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "conv.db"
            self.build_db(
                path,
                [
                    self.tool_step("run_command", 100, 110),
                    self.tool_step("view_file", 120, 121),
                    self.tool_step("run_command", 130, 140, status=7),
                ],
            )

            stats = cost_dashboard.analyze_antigravity_db_file(path)

            self.assertEqual(stats["tool_time"], 21.0)
            self.assertEqual(stats["tools"]["run_command"]["calls"], 2)
            self.assertEqual(stats["tools"]["run_command"]["errors"], 1)
            self.assertEqual(stats["tools"]["view_file"]["calls"], 1)
            self.assertEqual(stats["tools"]["view_file"]["errors"], 0)

    def test_workspace_is_read_from_step_payload(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "conv.db"
            workspace = field_bytes(28, field_text(2, "/workspace/project"))
            self.build_db(
                path,
                [(15, 3, b"", workspace), self.usage_step(1020, 1, 1, 0, 0, 100, 101)],
            )

            stats = cost_dashboard.analyze_antigravity_db_file(path)

            self.assertEqual(stats["cwd"], "/workspace/project")

    def test_session_id_comes_from_the_database_filename(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "conv.db"
            self.build_db(path, [self.usage_step(1020, 1, 1, 0, 0, 100, 101)])
            self.assertEqual(
                cost_dashboard.get_session_id_from_file(str(path), "antigravity"),
                "conv",
            )


if __name__ == "__main__":
    unittest.main()

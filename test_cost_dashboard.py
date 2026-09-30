import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import cost_dashboard
import opencode_export


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


class OpenCodeSessionTests(unittest.TestCase):
    """OpenCode stores every session in one shared database.

    These build a miniature database with the same tables and JSON payload
    shape the real one uses, including the millisecond timestamps and the fact
    that neither the message nor the part payload carries its own session or
    message id.
    """

    BASE_MS = 1_700_000_000_000

    def build_db(self, path: Path, sessions, messages, parts) -> None:
        conn = sqlite3.connect(path)
        conn.execute(
            "CREATE TABLE session (id text, directory text, time_created integer,"
            " time_updated integer)"
        )
        conn.execute(
            "CREATE TABLE message (id text, session_id text, time_created integer,"
            " data text)"
        )
        conn.execute(
            "CREATE TABLE part (id text, message_id text, session_id text,"
            " time_created integer, data text)"
        )
        conn.executemany(
            "INSERT INTO session VALUES (?,?,?,?)",
            [(s["id"], s["directory"], s["created"], s["updated"]) for s in sessions],
        )
        conn.executemany(
            "INSERT INTO message VALUES (?,?,?,?)",
            [
                (m["id"], m["session_id"], m["created"], json.dumps(m["payload"]))
                for m in messages
            ],
        )
        conn.executemany(
            "INSERT INTO part VALUES (?,?,?,?,?)",
            [
                (
                    f"prt_{i}",
                    p["message_id"],
                    p["session_id"],
                    p["created"],
                    json.dumps(p["payload"]),
                )
                for i, p in enumerate(parts)
            ],
        )
        conn.commit()
        conn.close()

    def assistant_message(self, session_id, message_id, tokens, offset_ms=0):
        return {
            "id": message_id,
            "session_id": session_id,
            "created": self.BASE_MS + offset_ms,
            "payload": {
                "role": "assistant",
                "modelID": "vendor/paid-model",
                "providerID": "openrouter",
                "tokens": tokens,
                "time": {
                    "created": self.BASE_MS + offset_ms,
                    "completed": self.BASE_MS + offset_ms + 2000,
                },
            },
        }

    def tool_part(self, session_id, message_id, tool, offset_ms=0, status="completed"):
        return {
            "message_id": message_id,
            "session_id": session_id,
            "created": self.BASE_MS + offset_ms,
            "payload": {
                "type": "tool",
                "tool": tool,
                "state": {
                    "status": status,
                    "time": {
                        "start": self.BASE_MS + offset_ms,
                        "end": self.BASE_MS + offset_ms + 500,
                    },
                },
            },
        }

    def tokens(self, input_, output, reasoning, cache_read, cache_write=0):
        return {
            "total": input_ + output + reasoning + cache_read + cache_write,
            "input": input_,
            "output": output,
            "reasoning": reasoning,
            "cache": {"read": cache_read, "write": cache_write},
        }

    def build_single_session_db(self, temp_dir, tokens, parts):
        session_id = "ses_test"
        path = Path(temp_dir) / "opencode.db"
        self.build_db(
            path,
            [
                {
                    "id": session_id,
                    "directory": "/workspace/oc",
                    "created": self.BASE_MS,
                    "updated": self.BASE_MS + 10_000,
                }
            ],
            [self.assistant_message(session_id, "msg_1", tokens)],
            parts,
        )
        return path, session_id

    def test_token_buckets_are_summed_and_reasoning_excluded_from_total(self):
        # OpenCode's counters are disjoint, so cache reads must be added to
        # input rather than subtracted, and the itemised rows still have to add
        # up to the reported total.
        with tempfile.TemporaryDirectory() as temp_dir:
            path, session_id = self.build_single_session_db(
                temp_dir,
                self.tokens(1000, 300, 100, 16000),
                [],
            )

            projects = cost_dashboard.analyze_opencode_db(path, "opencode")
            self.assertEqual(len(projects), 1)
            self.assertEqual(projects[0]["name"], "/workspace/oc")

            stats = projects[0]["sessions"][0]
            self.assertEqual(stats["uid"], session_id)
            self.assertEqual(stats["messages"], 1)
            self.assertEqual(stats["input_tokens"], 1000)
            self.assertEqual(stats["output_tokens"], 300)
            self.assertEqual(stats["reasoning_tokens"], 100)
            self.assertEqual(stats["cache_read_tokens"], 16000)
            self.assertEqual(stats["tokens"], 1000 + 300 + 16000)

    def test_components_win_over_an_inconsistent_total(self):
        # An aborted stream can report a total that disagrees with the
        # counters; the counters are the ones that get summed.
        with tempfile.TemporaryDirectory() as temp_dir:
            tokens = self.tokens(1000, 0, 500, 0)
            tokens["total"] = 900
            path, _ = self.build_single_session_db(temp_dir, tokens, [])

            projects = cost_dashboard.analyze_opencode_db(path, "opencode")
            self.assertEqual(projects[0]["sessions"][0]["tokens"], 1000)

    def test_cost_is_derived_because_opencode_reports_zero(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # $5.00/M input, $25.00/M output on claude-opus-4-6.
            path = Path(temp_dir) / "opencode.db"
            session_id = "ses_paid"
            message = self.assistant_message(
                session_id, "msg_1", self.tokens(1_000_000, 1_000_000, 0, 0)
            )
            message["payload"]["modelID"] = "claude-opus-4-6"
            self.build_db(
                path,
                [
                    {
                        "id": session_id,
                        "directory": "/workspace/paid",
                        "created": self.BASE_MS,
                        "updated": self.BASE_MS + 5_000,
                    }
                ],
                [message],
                [],
            )

            projects = cost_dashboard.analyze_opencode_db(path, "opencode")
            session = projects[0]["sessions"][0]
            # The stored cost is zero; the dashboard prices the tokens itself.
            self.assertAlmostEqual(session["cost"], 5.00 + 25.00, places=6)

    def test_unknown_local_models_cost_nothing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "opencode.db"
            session_id = "ses_local"
            message = self.assistant_message(
                session_id, "msg_1", self.tokens(500, 50, 0, 900_000)
            )
            message["payload"]["modelID"] = "Qwen3.8-27B-GGUF-UD-Q2_K_XL"
            self.build_db(
                path,
                [
                    {
                        "id": session_id,
                        "directory": "/workspace/local",
                        "created": self.BASE_MS,
                        "updated": self.BASE_MS + 5_000,
                    }
                ],
                [message],
                [],
            )

            projects = cost_dashboard.analyze_opencode_db(path, "opencode")
            self.assertEqual(projects[0]["sessions"][0]["cost"], 0.0)

    def test_tools_are_timed_and_errors_counted(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            session_id = "ses_test"
            path = Path(temp_dir) / "opencode.db"
            self.build_db(
                path,
                [
                    {
                        "id": session_id,
                        "directory": "/workspace/oc",
                        "created": self.BASE_MS,
                        "updated": self.BASE_MS + 10_000,
                    }
                ],
                [
                    self.assistant_message(
                        session_id, "msg_1", self.tokens(10, 10, 0, 0)
                    )
                ],
                [
                    self.tool_part(session_id, "msg_1", "bash", 1000),
                    self.tool_part(session_id, "msg_1", "bash", 2000, status="error"),
                    self.tool_part(session_id, "msg_1", "read", 3000),
                ],
            )

            stats = cost_dashboard.analyze_opencode_db(path, "opencode")[0]["sessions"][0]
            self.assertEqual(stats["tools"]["bash"]["calls"], 2)
            self.assertEqual(stats["tools"]["bash"]["errors"], 1)
            self.assertEqual(stats["tools"]["read"]["calls"], 1)
            self.assertEqual(stats["tools"]["read"]["errors"], 0)
            # Three calls at 500ms each.
            self.assertEqual(stats["tool_time"], 1.5)
            # LLM time comes from the message's created -> completed window.
            self.assertEqual(stats["llm_time"], 2.0)

    def test_unparsable_tool_input_is_attributed_to_the_real_tool(self):
        # OpenCode files input it could not parse under a synthetic "invalid"
        # tool and keeps the real name in the input.
        with tempfile.TemporaryDirectory() as temp_dir:
            session_id = "ses_test"
            path = Path(temp_dir) / "opencode.db"
            bad = self.tool_part(session_id, "msg_1", "invalid", 1000)
            bad["payload"]["state"]["input"] = {
                "tool": "bash",
                "error": "Invalid input for tool bash",
            }
            self.build_db(
                path,
                [
                    {
                        "id": session_id,
                        "directory": "/workspace/oc",
                        "created": self.BASE_MS,
                        "updated": self.BASE_MS + 10_000,
                    }
                ],
                [
                    self.assistant_message(
                        session_id, "msg_1", self.tokens(10, 10, 0, 0)
                    )
                ],
                [bad],
            )

            stats = cost_dashboard.analyze_opencode_db(path, "opencode")[0]["sessions"][0]
            self.assertEqual(stats["tools"]["bash"]["calls"], 1)
            self.assertNotIn("invalid", stats["tools"])

    def test_sessions_are_grouped_by_directory(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "opencode.db"
            sessions, messages, parts = [], [], []
            for index, directory in enumerate(
                ["/workspace/a", "/workspace/a", "/workspace/b"]
            ):
                session_id = f"ses_{index}"
                message_id = f"msg_{index}"
                sessions.append(
                    {
                        "id": session_id,
                        "directory": directory,
                        "created": self.BASE_MS + index * 1000,
                        "updated": self.BASE_MS + index * 1000 + 5_000,
                    }
                )
                messages.append(
                    self.assistant_message(
                        session_id, message_id, self.tokens(10, 5, 0, 100)
                    )
                )
            self.build_db(path, sessions, messages, parts)

            projects = cost_dashboard.analyze_opencode_db(path, "opencode")
            by_name = {p["name"]: len(p["sessions"]) for p in projects}
            self.assertEqual(by_name, {"/workspace/a": 2, "/workspace/b": 1})

    def test_sessions_without_assistant_messages_are_skipped(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "opencode.db"
            self.build_db(
                path,
                [
                    {
                        "id": "ses_empty",
                        "directory": "/workspace/empty",
                        "created": self.BASE_MS,
                        "updated": self.BASE_MS,
                    }
                ],
                [
                    {
                        "id": "msg_u",
                        "session_id": "ses_empty",
                        "created": self.BASE_MS,
                        "payload": {"role": "user"},
                    }
                ],
                [],
            )

            self.assertEqual(cost_dashboard.analyze_opencode_db(path, "opencode"), [])

    def test_timestamps_are_epoch_milliseconds(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path, _ = self.build_single_session_db(
                temp_dir, self.tokens(1, 1, 0, 0), []
            )
            stats = cost_dashboard.analyze_opencode_db(path, "opencode")[0][
                "sessions"
            ][0]
            # Costs are attributed to the moment the call completed, which the
            # fixture sets two seconds after the window opened.
            self.assertEqual(stats["start"].isoformat(), "2023-11-14T22:13:22+00:00")
            self.assertEqual(stats["end"].isoformat(), "2023-11-14T22:13:22+00:00")


class OpenCodeRenderTests(unittest.TestCase):
    """The transcript is assembled from message and tool text that the model
    produced, so markup in it has to stay inert."""

    def test_tags_inside_inline_code_are_escaped(self):
        rendered = opencode_export.render_text("use `agy --conversation <id>` here")
        self.assertIn("&lt;id&gt;", rendered)
        self.assertNotIn("<id>", rendered)

    def test_script_tags_are_escaped(self):
        rendered = opencode_export.render_text("<script>alert(1)</script>")
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;script&gt;", rendered)

    def test_inline_formatting_does_not_rewrite_code_blocks(self):
        # A backtick inside a fenced block must stay a literal backtick rather
        # than turning into an inline-code tag and breaking the nesting.
        rendered = opencode_export.render_text("intro\n```py\nx = `f({1})`\n```\nout")
        self.assertIn("<pre><code>x = `f({1})`", rendered)
        self.assertEqual(rendered.count("inline-code"), 0)

    def test_tool_output_is_truncated(self):
        part = {
            "type": "tool",
            "tool": "bash",
            "state": {
                "status": "completed",
                "input": {"command": "cat big"},
                "output": "y" * (opencode_export.MAX_OUTPUT_CHARS * 3),
            },
        }
        rendered = opencode_export.render_tool(part)
        self.assertIn("truncated", rendered)
        self.assertLess(len(rendered), opencode_export.MAX_OUTPUT_CHARS * 2)

    def test_transcript_html_is_balanced(self):
        rendered = opencode_export.render_text("a <b> c `d` **e**")
        self.assertEqual(rendered.count("<strong>"), rendered.count("</strong>"))
        self.assertEqual(
            rendered.count('<code class="inline-code">'),
            rendered.count("</code>"),
        )


if __name__ == "__main__":
    unittest.main()

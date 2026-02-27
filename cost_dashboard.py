#!/usr/bin/env python3
"""Serve a dynamic HTML dashboard with cost statistics for all pi-agent sessions."""

import json
import subprocess
import tempfile
import urllib.parse
import uuid
from pathlib import Path
from collections import defaultdict
from datetime import datetime
import html
import http.server
import socketserver
import argparse
import shlex
import sys
from typing import TypedDict, DefaultDict


# Type definitions
class ModelStats(TypedDict):
    messages: int
    tokens: int
    cost: float
    llm_time: float
    output_tokens: int


class ToolStats(TypedDict):
    calls: int
    time: float
    errors: int


class DailyStats(TypedDict):
    messages: int
    tokens: int
    cost: float


class SessionStats(TypedDict):
    messages: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    total_tokens: int
    cost_total: float
    models: DefaultDict[str, ModelStats]
    timestamps: list[datetime]
    start: datetime | None
    end: datetime | None
    llm_time: float
    tool_time: float
    tools: DefaultDict[str, ToolStats]
    tps_samples: list[tuple[int, float, str]]
    cwd: str


class ProjectStats(TypedDict):
    name: str
    agent_cmd: str
    sessions: list["Session"]
    total_messages: int
    total_tokens: int
    total_output_tokens: int
    total_cost: float
    total_llm_time: float
    total_tool_time: float
    models: DefaultDict[str, ModelStats]
    tools: DefaultDict[str, ToolStats]
    daily_stats: DefaultDict[str, DailyStats]
    first_activity: datetime | None
    last_activity: datetime | None
    tps_samples: list[tuple[int, float, str]]


class GlobalStats(TypedDict):
    total_cost: float
    total_tokens: int
    total_output_tokens: int
    total_messages: int
    total_sessions: int
    total_projects: int
    total_llm_time: float
    total_tool_time: float
    models: DefaultDict[str, ModelStats]
    tools: DefaultDict[str, ToolStats]
    daily_stats: DefaultDict[str, DailyStats]
    tps_samples: list[tuple[int, float, str]]


class Session(TypedDict):
    """Session data for a single agent session."""

    file: str
    path: str
    uid: str
    relative_path: str
    cwd: str
    agent_cmd: str
    messages: int
    tokens: int
    output_tokens: int
    cost: float
    start: datetime | None
    end: datetime | None
    duration: float
    llm_time: float
    tool_time: float
    tools: dict[str, ToolStats]
    avg_tps: float
    subagent_sessions: list["Session"]


# Helper functions to create properly-typed defaultdicts
def create_model_stats() -> ModelStats:
    return {
        "messages": 0,
        "tokens": 0,
        "cost": 0.0,
        "llm_time": 0.0,
        "output_tokens": 0,
    }


def create_tool_stats() -> ToolStats:
    return {"calls": 0, "time": 0.0, "errors": 0}


def create_daily_stats() -> DailyStats:
    return {"messages": 0, "tokens": 0, "cost": 0.0}


# Session directories for different agents: (path, agent_command, source_type)
# source_type: "standard" (pi/omp), "claude" (~/.claude/projects), "codex" (~/.codex/sessions)
SESSIONS_DIRS = [
    (Path.home() / ".pi" / "agent" / "sessions", "pi", "standard"),
    (Path.home() / ".omp" / "agent" / "sessions", "omp", "standard"),
    (Path.home() / ".claude" / "projects", "claude", "claude"),
    (Path.home() / ".codex" / "sessions", "codex", "codex"),
]
TEMP_DIR = Path(tempfile.gettempdir()) / "pi-dashboard"

# Registry mapping session UUIDs to session data
# This keeps sensitive path/command info server-side only
SESSION_REGISTRY: dict[str, Session] = {}


def clear_session_registry() -> None:
    """Clear all sessions from the registry."""
    SESSION_REGISTRY.clear()


def get_session_id_from_file(
    filepath: str, source_type: str = "standard"
) -> str | None:
    """Extract session ID from a JSONL file.

    For standard (pi/omp): first line {"type":"session","id":"..."}
    For claude: use the filename stem (UUID)
    For codex: read session_meta.payload.id
    """
    if source_type == "claude":
        return Path(filepath).stem

    try:
        with open(filepath, "r") as f:
            first_line = f.readline().strip()
            if first_line:
                data = json.loads(first_line)
                if source_type == "codex":
                    if data.get("type") == "session_meta":
                        return data.get("payload", {}).get("id")
                else:
                    if data.get("type") == "session" and "id" in data:
                        return data["id"]
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return None


# Manual pricing for models that report zero cost (price per million tokens)
# Format: model_pattern -> {"input": price_per_M, "output": price_per_M, "cache_read": price_per_M}
MANUAL_PRICING = {
    # Gemini models via Google Cloud Code Assist (free tier, but estimate value)
    # Pricing based on public Gemini API pricing as of Dec 2024
    "gemini-2.5-pro": {
        "input": 1.25,  # $1.25 per 1M input tokens
        "output": 10.00,  # $10.00 per 1M output tokens (with thinking)
        "cache_read": 0.31,  # $0.3125 per 1M cached tokens
    },
    "gemini-2.5-flash": {
        "input": 0.15,  # $0.15 per 1M input tokens
        "output": 0.60,  # $0.60 per 1M output tokens
        "cache_read": 0.0375,
    },
    "gemini-2.0-flash": {
        "input": 0.10,
        "output": 0.40,
        "cache_read": 0.025,
    },
    "gemini-3-pro-preview": {
        # Use same pricing as gemini-2.5-pro as estimate
        "input": 1.25,
        "output": 10.00,
        "cache_read": 0.31,
    },
    # Claude models (per 1M tokens) - from Anthropic API pricing
    "claude-opus-4": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
    },
    "claude-sonnet-4": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write": 3.75,
    },
    "claude-haiku-4": {
        "input": 1.0,
        "output": 5.0,
        "cache_read": 0.1,
        "cache_write": 1.25,
    },
    # GLM models
    "glm-4.7": {
        "input": 0.4,
        "output": 1.0,
        "cache_read": 0.0,
        "cache_write": 0.0,
    },
    "glm-4.5-air": {
        "input": 0.2,
        "output": 1.1,
        "cache_read": 0.03,
        "cache_write": 0.0,
    },
    # OpenAI/Codex models (estimated pricing)
    # Put codex-specific patterns before generic gpt-* patterns.
    # Codex cache pricing is lower than normal input pricing.
    "gpt-5.3-codex": {
        "input": 1.75,
        "output": 14.0,
        "cache_read": 0.175,
    },
    "gpt-5.2-codex": {
        "input": 1.5,
        "output": 10.0,
        "cache_read": 0.15,
    },
    "gpt-5.1-codex": {
        "input": 1.5,
        "output": 10.0,
        "cache_read": 0.15,
    },
    "gpt-5-codex": {
        "input": 1.5,
        "output": 10.0,
        "cache_read": 0.15,
    },
    "gpt-5": {
        "input": 2.0,
        "output": 8.0,
        "cache_read": 0.5,
    },
    "gpt-4": {
        "input": 2.5,
        "output": 10.0,
        "cache_read": 1.25,
    },
    "o3": {
        "input": 2.0,
        "output": 8.0,
        "cache_read": 0.5,
    },
    "o4-mini": {
        "input": 1.1,
        "output": 4.4,
        "cache_read": 0.275,
    },
}


def get_manual_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int = 0,
) -> float:
    """Calculate cost using manual pricing if available."""
    for pattern, pricing in MANUAL_PRICING.items():
        if pattern in model.lower():
            input_cost = (input_tokens / 1_000_000) * pricing["input"]
            output_cost = (output_tokens / 1_000_000) * pricing["output"]
            cache_read_cost = (cache_read_tokens / 1_000_000) * pricing.get(
                "cache_read", 0
            )
            cache_write_cost = (cache_write_tokens / 1_000_000) * pricing.get(
                "cache_write", 0
            )
            return input_cost + output_cost + cache_read_cost + cache_write_cost
    return 0.0


def parse_timestamp(ts):
    """Parse ISO timestamp string to datetime."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None


def format_duration(seconds):
    """Format seconds into human-readable duration like 1h23m45s."""
    if seconds < 60:
        return f"{seconds:.0f}s"
    elif seconds < 3600:
        mins = int(seconds // 60)
        secs = int(seconds % 60)
        return f"{mins}m{secs:02d}s" if secs else f"{mins}m"
    else:
        hours = int(seconds // 3600)
        mins = int((seconds % 3600) // 60)
        return f"{hours}h{mins:02d}m" if mins else f"{hours}h"


def calc_avg_tokens_per_sec(tps_samples):
    """Calculate average tokens/second from samples.

    Each sample is (output_tokens, llm_seconds, model).
    Returns average tokens/second, or 0 if no valid samples.
    """
    if not tps_samples:
        return 0.0

    # Calculate tokens/sec for each sample and average them
    tps_values = [tokens / secs for tokens, secs, _ in tps_samples if secs > 0]
    if not tps_values:
        return 0.0

    return sum(tps_values) / len(tps_values)


def get_project_path_from_jsonl(project_dir, source_type: str = "standard"):
    """Get the actual project path from the first session file's cwd field."""
    jsonl_files = sorted(project_dir.glob("*.jsonl"))
    for filepath in jsonl_files:
        try:
            with open(filepath, "r") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if source_type == "claude":
                        # Skip records that don't carry cwd
                        if data.get("type") in (
                            "queue-operation",
                            "progress",
                            "file-history-snapshot",
                            "summary",
                        ):
                            continue
                        if data.get("cwd"):
                            return data["cwd"]
                    elif source_type == "codex":
                        if data.get("type") == "session_meta":
                            cwd = data.get("payload", {}).get("cwd")
                            if cwd:
                                return cwd
                    else:
                        if data.get("type") == "session" and "cwd" in data:
                            return data["cwd"]
                    break  # Only check first relevant line
        except (OSError, json.JSONDecodeError, KeyError, TypeError):
            continue
    return project_dir.name


def analyze_jsonl_file(filepath: Path) -> SessionStats:
    """Analyze a single JSONL file and return stats."""
    stats: SessionStats = {
        "messages": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "cost_total": 0.0,
        "models": defaultdict(create_model_stats),
        "timestamps": [],
        "start": None,
        "end": None,
        "llm_time": 0.0,  # Total LLM working time in seconds
        "tool_time": 0.0,  # Total tool execution time in seconds
        "tools": defaultdict(create_tool_stats),  # Per-tool stats
        "tps_samples": [],  # List of (output_tokens, llm_seconds) per call for tokens/sec calculation
        "cwd": "",
    }

    last_request_ts = None  # Timestamp of last user message or toolResult
    pending_tool_calls = {}  # tool_call_id -> {"name": str, "timestamp": datetime}
    cwd = ""

    try:
        with open(filepath, "r") as f:
            # First, try to read cwd from the session line
            first_line = f.readline().strip()
            if first_line:
                try:
                    session_data = json.loads(first_line)
                    if session_data.get("type") == "session":
                        cwd = session_data.get("cwd", "")
                except (json.JSONDecodeError, TypeError):
                    pass

            # Now process the rest of the file
            for line in f:
                try:
                    data = json.loads(line.strip())
                    if data.get("type") != "message" or "message" not in data:
                        continue

                    msg = data["message"]
                    ts = parse_timestamp(data.get("timestamp"))
                    role = msg.get("role")

                    # Process assistant messages (with or without usage)
                    if role == "assistant":
                        # Calculate LLM time for this call
                        llm_delta = 0
                        if ts and last_request_ts:
                            llm_delta = (ts - last_request_ts).total_seconds()
                            if 0 < llm_delta < 300:  # Cap at 5 min to filter outliers
                                stats["llm_time"] += llm_delta
                            else:
                                llm_delta = 0  # Invalid, don't use for tokens/sec
                            last_request_ts = None

                        # Process usage data if present
                        if "usage" in msg:
                            usage = msg["usage"]
                            cost = usage.get("cost", {})
                            model = msg.get("model", "unknown")

                            input_tok = usage.get("input", 0)
                            output_tok = usage.get("output", 0)
                            cache_read_tok = usage.get("cacheRead", 0)
                            cache_write_tok = usage.get("cacheWrite", 0)
                            total_tok = usage.get("totalTokens", 0)
                            reported_cost = cost.get("total", 0)

                            if reported_cost == 0:
                                reported_cost = get_manual_cost(
                                    model,
                                    input_tok,
                                    output_tok,
                                    cache_read_tok,
                                    cache_write_tok,
                                )

                            stats["messages"] += 1
                            stats["input_tokens"] += input_tok
                            stats["output_tokens"] += output_tok
                            stats["cache_read_tokens"] += cache_read_tok
                            stats["cache_write_tokens"] += cache_write_tok
                            stats["total_tokens"] += total_tok
                            stats["cost_total"] += reported_cost

                            stats["models"][model]["messages"] += 1
                            stats["models"][model]["tokens"] += total_tok
                            stats["models"][model]["cost"] += reported_cost
                            stats["models"][model]["output_tokens"] += output_tok

                            # Track tokens/second sample if we have valid timing
                            if llm_delta > 0 and output_tok > 0:
                                stats["tps_samples"].append(
                                    (output_tok, llm_delta, model)
                                )
                                stats["models"][model]["llm_time"] += llm_delta

                            if ts:
                                stats["timestamps"].append(ts)
                                if stats["start"] is None or ts < stats["start"]:
                                    stats["start"] = ts
                                if stats["end"] is None or ts > stats["end"]:
                                    stats["end"] = ts

                        # Track tool calls from assistant messages
                        if ts:
                            content = msg.get("content", [])
                            if isinstance(content, list):
                                for item in content:
                                    if (
                                        isinstance(item, dict)
                                        and item.get("type") == "toolCall"
                                    ):
                                        tool_id = item.get("id")
                                        tool_name = item.get("name", "unknown")
                                        if tool_id:
                                            pending_tool_calls[tool_id] = {
                                                "name": tool_name,
                                                "timestamp": ts,
                                            }

                    elif role == "user":
                        if ts:
                            last_request_ts = ts

                    elif role == "toolResult":
                        if ts:
                            last_request_ts = ts
                            # Match tool result with pending call
                            tool_call_id = msg.get("toolCallId")
                            tool_name = msg.get("toolName", "unknown")
                            is_error = msg.get("isError", False)

                            if tool_call_id and tool_call_id in pending_tool_calls:
                                call_info = pending_tool_calls.pop(tool_call_id)
                                tool_delta = (
                                    ts - call_info["timestamp"]
                                ).total_seconds()
                                if (
                                    0 < tool_delta < 600
                                ):  # Cap at 10 min to filter outliers
                                    stats["tool_time"] += tool_delta
                                    stats["tools"][tool_name]["calls"] += 1
                                    stats["tools"][tool_name]["time"] += tool_delta
                                    if is_error:
                                        stats["tools"][tool_name]["errors"] += 1

                except json.JSONDecodeError:
                    continue
    except Exception as e:
        print(f"Error reading {filepath}: {e}")

    stats["cwd"] = cwd
    return stats


def analyze_claude_jsonl_file(filepath: Path) -> SessionStats:
    """Analyze a Claude Code JSONL session file and return stats.

    Claude Code format: each line is a JSON record with top-level 'type' field.
    Types include: user, assistant, progress, file-history-snapshot, summary.
    Usage is in message.usage with input_tokens, output_tokens, cache_read_input_tokens,
    cache_creation_input_tokens. No embedded cost - compute via get_manual_cost().
    """
    stats: SessionStats = {
        "messages": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "cost_total": 0.0,
        "models": defaultdict(create_model_stats),
        "timestamps": [],
        "start": None,
        "end": None,
        "llm_time": 0.0,
        "tool_time": 0.0,
        "tools": defaultdict(create_tool_stats),
        "tps_samples": [],
        "cwd": "",
    }

    last_request_ts = None
    pending_tool_calls = {}  # tool_use id -> {"name": str, "timestamp": datetime}
    cwd = ""

    try:
        with open(filepath, "r") as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                record_type = data.get("type")

                # Skip progress records (subagent data - avoid double-counting)
                # Skip file-history-snapshot and summary records
                if record_type in ("progress", "file-history-snapshot", "summary"):
                    continue

                # Extract cwd from first record that has it
                if not cwd and data.get("cwd"):
                    cwd = data["cwd"]

                ts = parse_timestamp(data.get("timestamp"))

                if record_type == "user":
                    if ts:
                        last_request_ts = ts
                    # Check for tool_result in user message content
                    msg = data.get("message", {})
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for item in content:
                            if not isinstance(item, dict):
                                continue
                            if item.get("type") == "tool_result":
                                tool_use_id = item.get("tool_use_id")
                                is_error = item.get("is_error", False)
                                if (
                                    ts
                                    and tool_use_id
                                    and tool_use_id in pending_tool_calls
                                ):
                                    call_info = pending_tool_calls.pop(tool_use_id)
                                    tool_delta = (
                                        ts - call_info["timestamp"]
                                    ).total_seconds()
                                    if 0 < tool_delta < 600:
                                        stats["tool_time"] += tool_delta
                                        tool_name = call_info["name"]
                                        stats["tools"][tool_name]["calls"] += 1
                                        stats["tools"][tool_name][
                                            "time"
                                        ] += tool_delta
                                        if is_error:
                                            stats["tools"][tool_name]["errors"] += 1

                elif record_type == "assistant":
                    msg = data.get("message", {})
                    usage = msg.get("usage", {})
                    model = msg.get("model", "")

                    # Skip synthetic records
                    if model == "<synthetic>":
                        continue

                    # Calculate LLM time
                    llm_delta = 0
                    if ts and last_request_ts:
                        llm_delta = (ts - last_request_ts).total_seconds()
                        if 0 < llm_delta < 300:
                            stats["llm_time"] += llm_delta
                        else:
                            llm_delta = 0
                        last_request_ts = None

                    # Process usage data if present
                    if usage and model:
                        input_tok = usage.get("input_tokens", 0)
                        output_tok = usage.get("output_tokens", 0)
                        cache_read_tok = usage.get("cache_read_input_tokens", 0)
                        cache_write_tok = usage.get(
                            "cache_creation_input_tokens", 0
                        )
                        total_tok = input_tok + output_tok + cache_read_tok + cache_write_tok

                        cost = get_manual_cost(
                            model,
                            input_tok,
                            output_tok,
                            cache_read_tok,
                            cache_write_tok,
                        )

                        stats["messages"] += 1
                        stats["input_tokens"] += input_tok
                        stats["output_tokens"] += output_tok
                        stats["cache_read_tokens"] += cache_read_tok
                        stats["cache_write_tokens"] += cache_write_tok
                        stats["total_tokens"] += total_tok
                        stats["cost_total"] += cost

                        stats["models"][model]["messages"] += 1
                        stats["models"][model]["tokens"] += total_tok
                        stats["models"][model]["cost"] += cost
                        stats["models"][model]["output_tokens"] += output_tok

                        if llm_delta > 0 and output_tok > 0:
                            stats["tps_samples"].append(
                                (output_tok, llm_delta, model)
                            )
                            stats["models"][model]["llm_time"] += llm_delta

                        if ts:
                            stats["timestamps"].append(ts)
                            if stats["start"] is None or ts < stats["start"]:
                                stats["start"] = ts
                            if stats["end"] is None or ts > stats["end"]:
                                stats["end"] = ts

                    # Track tool_use calls from assistant content
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for item in content:
                            if (
                                isinstance(item, dict)
                                and item.get("type") == "tool_use"
                            ):
                                tool_id = item.get("id")
                                tool_name = item.get("name", "unknown")
                                if tool_id and ts:
                                    pending_tool_calls[tool_id] = {
                                        "name": tool_name,
                                        "timestamp": ts,
                                    }

    except Exception as e:
        print(f"Error reading Claude session {filepath}: {e}")

    stats["cwd"] = cwd
    return stats


def analyze_codex_jsonl_file(filepath: Path) -> SessionStats:
    """Analyze a Codex CLI JSONL session file and return stats.

    Codex format uses record types: session_meta, turn_context, event_msg, response_item.
    Usage is in event_msg records where payload.type == "token_count".
    We prefer last_token_usage deltas when available, otherwise derive deltas from
    total_token_usage using running totals.
    """

    def to_nonneg_int(value) -> int:
        try:
            if value is None:
                return 0
            return max(0, int(value))
        except (TypeError, ValueError):
            try:
                return max(0, int(float(value)))
            except (TypeError, ValueError):
                return 0

    def parse_usage(usage_obj: dict | None) -> dict | None:
        if not isinstance(usage_obj, dict):
            return None

        raw_input = to_nonneg_int(usage_obj.get("input_tokens", 0))
        cache_read = to_nonneg_int(usage_obj.get("cached_input_tokens", 0))
        output = to_nonneg_int(usage_obj.get("output_tokens", 0))
        reasoning = to_nonneg_int(usage_obj.get("reasoning_output_tokens", 0))

        # Codex input_tokens includes cached_input_tokens. Store net input to avoid
        # double counting input + cache read in totals and manual pricing.
        input_net = max(0, raw_input - cache_read)

        # Match ccusage semantics: billable total excludes reasoning breakdown
        # and avoids relying on provider-specific total_tokens behavior.
        total = input_net + output + cache_read

        return {
            "input_tokens": input_net,
            "output_tokens": output,
            "reasoning_tokens": reasoning,
            "cache_read_tokens": cache_read,
            "total_tokens": total,
        }

    def subtract_usage(current: dict, previous: dict) -> dict:
        return {
            "input_tokens": max(
                0, current["input_tokens"] - previous["input_tokens"]
            ),
            "output_tokens": max(
                0, current["output_tokens"] - previous["output_tokens"]
            ),
            "reasoning_tokens": max(
                0, current["reasoning_tokens"] - previous["reasoning_tokens"]
            ),
            "cache_read_tokens": max(
                0,
                current["cache_read_tokens"] - previous["cache_read_tokens"],
            ),
            "total_tokens": max(
                0, current["total_tokens"] - previous["total_tokens"]
            ),
        }

    def add_usage(left: dict, right: dict) -> dict:
        return {
            "input_tokens": left["input_tokens"] + right["input_tokens"],
            "output_tokens": left["output_tokens"] + right["output_tokens"],
            "reasoning_tokens": left["reasoning_tokens"]
            + right["reasoning_tokens"],
            "cache_read_tokens": left["cache_read_tokens"]
            + right["cache_read_tokens"],
            "total_tokens": left["total_tokens"] + right["total_tokens"],
        }

    stats: SessionStats = {
        "messages": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "cost_total": 0.0,
        "models": defaultdict(create_model_stats),
        "timestamps": [],
        "start": None,
        "end": None,
        "llm_time": 0.0,
        "tool_time": 0.0,
        "tools": defaultdict(create_tool_stats),
        "tps_samples": [],
        "cwd": "",
    }

    cwd = ""
    model = ""
    pending_tool_calls = {}  # call_id -> {"name": str, "timestamp": datetime}
    previous_total_usage = None

    try:
        with open(filepath, "r") as f:
            for line in f:
                try:
                    data = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue

                record_type = data.get("type")
                payload = data.get("payload", {})
                if not isinstance(payload, dict):
                    payload = {}
                ts = parse_timestamp(data.get("timestamp"))

                if record_type == "session_meta":
                    if not cwd:
                        cwd = payload.get("cwd", "")
                    if ts:
                        if stats["start"] is None or ts < stats["start"]:
                            stats["start"] = ts

                elif record_type == "turn_context":
                    if payload.get("model"):
                        model = payload["model"]

                elif record_type == "event_msg":
                    if payload.get("type") != "token_count":
                        continue

                    info = payload.get("info")
                    if not isinstance(info, dict):
                        continue

                    last_usage = parse_usage(info.get("last_token_usage"))
                    total_usage = parse_usage(info.get("total_token_usage"))

                    delta_usage = None
                    latest_total_usage = None

                    if last_usage:
                        delta_usage = last_usage
                        latest_total_usage = total_usage
                    elif total_usage:
                        delta_usage = (
                            subtract_usage(total_usage, previous_total_usage)
                            if previous_total_usage
                            else total_usage
                        )
                        latest_total_usage = total_usage

                    if not delta_usage:
                        continue

                    has_usage_signal = (
                        delta_usage["input_tokens"] > 0
                        or delta_usage["output_tokens"] > 0
                        or delta_usage["reasoning_tokens"] > 0
                        or delta_usage["cache_read_tokens"] > 0
                        or delta_usage["total_tokens"] > 0
                    )
                    if not has_usage_signal:
                        if latest_total_usage:
                            previous_total_usage = latest_total_usage
                        continue

                    input_tok = delta_usage["input_tokens"]
                    output_tok = delta_usage["output_tokens"]
                    cache_read_tok = delta_usage["cache_read_tokens"]
                    reasoning_tok = delta_usage["reasoning_tokens"]
                    total_tok = delta_usage["total_tokens"]

                    cost = get_manual_cost(
                        model, input_tok, output_tok, cache_read_tok
                    )

                    stats["messages"] += 1
                    stats["input_tokens"] += input_tok
                    stats["output_tokens"] += output_tok + reasoning_tok
                    stats["cache_read_tokens"] += cache_read_tok
                    stats["total_tokens"] += total_tok
                    stats["cost_total"] += cost

                    current_model = model or "unknown"
                    stats["models"][current_model]["messages"] += 1
                    stats["models"][current_model]["tokens"] += total_tok
                    stats["models"][current_model]["cost"] += cost
                    stats["models"][current_model]["output_tokens"] += (
                        output_tok + reasoning_tok
                    )

                    if ts:
                        stats["timestamps"].append(ts)
                        if stats["start"] is None or ts < stats["start"]:
                            stats["start"] = ts
                        if stats["end"] is None or ts > stats["end"]:
                            stats["end"] = ts

                    if latest_total_usage:
                        previous_total_usage = latest_total_usage
                    elif previous_total_usage:
                        previous_total_usage = add_usage(
                            previous_total_usage, delta_usage
                        )
                    else:
                        previous_total_usage = delta_usage

                elif record_type == "response_item":
                    payload_type = payload.get("type")

                    if payload_type == "function_call":
                        call_id = payload.get("call_id")
                        tool_name = payload.get("name", "unknown")
                        if call_id and ts:
                            pending_tool_calls[call_id] = {
                                "name": tool_name,
                                "timestamp": ts,
                            }

                    elif payload_type == "function_call_output":
                        call_id = payload.get("call_id")
                        if ts and call_id and call_id in pending_tool_calls:
                            call_info = pending_tool_calls.pop(call_id)
                            tool_delta = (
                                ts - call_info["timestamp"]
                            ).total_seconds()
                            if 0 < tool_delta < 600:
                                stats["tool_time"] += tool_delta
                                tool_name = call_info["name"]
                                stats["tools"][tool_name]["calls"] += 1
                                stats["tools"][tool_name]["time"] += tool_delta

                    if ts:
                        if stats["end"] is None or ts > stats["end"]:
                            stats["end"] = ts

    except Exception as e:
        print(f"Error reading Codex session {filepath}: {e}")

    stats["cwd"] = cwd
    return stats


def analyze_session_file(filepath: Path, source_type: str) -> SessionStats:
    """Dispatch to the correct parser based on source type."""
    if source_type == "claude":
        return analyze_claude_jsonl_file(filepath)
    elif source_type == "codex":
        return analyze_codex_jsonl_file(filepath)
    else:
        return analyze_jsonl_file(filepath)


def analyze_project(project_dir: Path, agent_cmd: str, source_type: str = "standard") -> ProjectStats | None:
    """Analyze all sessions in a project directory."""
    project_stats: ProjectStats = {
        "name": get_project_path_from_jsonl(project_dir, source_type),
        "agent_cmd": agent_cmd,
        "sessions": [],
        "total_messages": 0,
        "total_tokens": 0,
        "total_output_tokens": 0,
        "total_cost": 0.0,
        "total_llm_time": 0.0,
        "total_tool_time": 0.0,
        "models": defaultdict(create_model_stats),
        "tools": defaultdict(create_tool_stats),
        "daily_stats": defaultdict(create_daily_stats),
        "first_activity": None,
        "last_activity": None,
        "tps_samples": [],  # Aggregated tokens/sec samples
    }

    # Only get top-level JSONL files (not in subdirectories)
    jsonl_files = list(project_dir.glob("*.jsonl"))
    if not jsonl_files:
        return None

    for filepath in sorted(jsonl_files):
        stats = analyze_session_file(filepath, source_type)
        if stats["messages"] == 0:
            continue

        duration = (
            (stats["end"] - stats["start"]).total_seconds()
            if stats["start"] and stats["end"]
            else 0
        )

        # Look for subagent sessions in a matching subdirectory
        # e.g., "session.jsonl" -> "session/" directory
        session_name = filepath.stem  # filename without .jsonl extension
        subagent_dir = filepath.parent / session_name

        subagent_sessions = []
        if subagent_dir.exists() and subagent_dir.is_dir():
            # Find all JSONL files in the subagent directory
            for sub_jsonl in sorted(subagent_dir.rglob("*.jsonl")):
                sub_stats = analyze_session_file(sub_jsonl, source_type)
                if sub_stats["messages"] > 0:
                    sub_duration = (
                        (sub_stats["end"] - sub_stats["start"]).total_seconds()
                        if sub_stats["start"] and sub_stats["end"]
                        else 0
                    )
                    try:
                        sub_relative = sub_jsonl.relative_to(project_dir)
                    except ValueError:
                        sub_relative = sub_jsonl

                    # Get UID from file or generate random one
                    sub_uid = get_session_id_from_file(
                        str(sub_jsonl), source_type
                    ) or str(uuid.uuid4())

                    sub_session = Session(
                        file=sub_jsonl.name,
                        path=str(sub_jsonl),
                        uid=sub_uid,
                        relative_path=str(sub_relative),
                        cwd=sub_stats["cwd"],
                        agent_cmd=agent_cmd,
                        messages=sub_stats["messages"],
                        tokens=sub_stats["total_tokens"],
                        output_tokens=sub_stats["output_tokens"],
                        cost=sub_stats["cost_total"],
                        start=sub_stats["start"],
                        end=sub_stats["end"],
                        duration=sub_duration,
                        llm_time=sub_stats["llm_time"],
                        tool_time=sub_stats["tool_time"],
                        tools=dict(sub_stats["tools"]),
                        avg_tps=calc_avg_tokens_per_sec(sub_stats["tps_samples"]),
                        subagent_sessions=[],
                    )
                    SESSION_REGISTRY[sub_uid] = sub_session
                    subagent_sessions.append(sub_session)

                    # Include subagent stats in project totals
                    project_stats["total_messages"] += sub_stats["messages"]
                    project_stats["total_tokens"] += sub_stats["total_tokens"]
                    project_stats["total_output_tokens"] += sub_stats["output_tokens"]
                    project_stats["total_cost"] += sub_stats["cost_total"]
                    project_stats["total_llm_time"] += sub_stats["llm_time"]
                    project_stats["total_tool_time"] += sub_stats["tool_time"]
                    project_stats["tps_samples"].extend(sub_stats["tps_samples"])

                    # Track subagent model usage
                    for model, mstats in sub_stats["models"].items():
                        project_stats["models"][model]["messages"] += mstats["messages"]
                        project_stats["models"][model]["tokens"] += mstats["tokens"]
                        project_stats["models"][model]["cost"] += mstats["cost"]
                        project_stats["models"][model]["llm_time"] += mstats.get(
                            "llm_time", 0
                        )
                        project_stats["models"][model]["output_tokens"] += mstats.get(
                            "output_tokens", 0
                        )

                    # Track subagent tool usage
                    for tool_name, tstats in sub_stats["tools"].items():
                        project_stats["tools"][tool_name]["calls"] += tstats["calls"]
                        project_stats["tools"][tool_name]["time"] += tstats["time"]
                        project_stats["tools"][tool_name]["errors"] += tstats["errors"]

                    # Track subagent daily stats
                    for ts in sub_stats["timestamps"]:
                        day_key = ts.strftime("%Y-%m-%d")
                        project_stats["daily_stats"][day_key]["messages"] += 1
                        project_stats["daily_stats"][day_key]["cost"] += sub_stats[
                            "cost_total"
                        ] / max(len(sub_stats["timestamps"]), 1)

        # Get UID from file or generate random one
        session_uid = get_session_id_from_file(str(filepath), source_type) or str(
            uuid.uuid4()
        )

        session = Session(
            file=filepath.name,
            path=str(filepath),
            uid=session_uid,
            relative_path=filepath.name,  # Top-level, just the filename
            cwd=stats["cwd"],
            agent_cmd=agent_cmd,
            messages=stats["messages"],
            tokens=stats["total_tokens"],
            output_tokens=stats["output_tokens"],
            cost=stats["cost_total"],
            start=stats["start"],
            end=stats["end"],
            duration=duration,
            llm_time=stats["llm_time"],
            tool_time=stats["tool_time"],
            tools=dict(stats["tools"]),
            avg_tps=calc_avg_tokens_per_sec(stats["tps_samples"]),
            subagent_sessions=subagent_sessions,
        )
        SESSION_REGISTRY[session_uid] = session
        project_stats["sessions"].append(session)

        project_stats["total_messages"] += stats["messages"]
        project_stats["total_tokens"] += stats["total_tokens"]
        project_stats["total_output_tokens"] += stats["output_tokens"]
        project_stats["total_cost"] += stats["cost_total"]
        project_stats["total_llm_time"] += stats["llm_time"]
        project_stats["total_tool_time"] += stats["tool_time"]
        project_stats["tps_samples"].extend(stats["tps_samples"])

        for model, mstats in stats["models"].items():
            project_stats["models"][model]["messages"] += mstats["messages"]
            project_stats["models"][model]["tokens"] += mstats["tokens"]
            project_stats["models"][model]["cost"] += mstats["cost"]
            project_stats["models"][model]["llm_time"] += mstats.get("llm_time", 0)
            project_stats["models"][model]["output_tokens"] += mstats.get(
                "output_tokens", 0
            )

        for tool_name, tstats in stats["tools"].items():
            project_stats["tools"][tool_name]["calls"] += tstats["calls"]
            project_stats["tools"][tool_name]["time"] += tstats["time"]
            project_stats["tools"][tool_name]["errors"] += tstats["errors"]

        for ts in stats["timestamps"]:
            day_key = ts.strftime("%Y-%m-%d")
            project_stats["daily_stats"][day_key]["messages"] += 1
            project_stats["daily_stats"][day_key]["cost"] += stats["cost_total"] / max(
                len(stats["timestamps"]), 1
            )

        if stats["start"]:
            if (
                project_stats["first_activity"] is None
                or stats["start"] < project_stats["first_activity"]
            ):
                project_stats["first_activity"] = stats["start"]
        if stats["end"]:
            if (
                project_stats["last_activity"] is None
                or stats["end"] > project_stats["last_activity"]
            ):
                project_stats["last_activity"] = stats["end"]

    return project_stats if project_stats["sessions"] else None


def export_session_to_html(session_path: str, agent_cmd: str) -> str:
    """Export a session file to HTML.

    For pi/omp: use agent_cmd --export.
    For claude/codex: use standalone export scripts.
    """
    TEMP_DIR.mkdir(parents=True, exist_ok=True)

    # Create a unique output filename based on the session path
    session_hash = hash(session_path) & 0xFFFFFFFF
    output_file = TEMP_DIR / f"session_{session_hash}.html"

    try:
        try:
            base_cmd = shlex.split(agent_cmd)
        except ValueError:
            base_cmd = agent_cmd.split()

        agent_name = Path(base_cmd[0]).name.lower() if base_cmd else ""

        if agent_name.startswith("claude"):
            script = Path(__file__).parent / "claude_export.py"
            cmd = [sys.executable or "python3", str(script), session_path, str(output_file)]
        elif agent_name.startswith("codex"):
            script = Path(__file__).parent / "codex_export.py"
            cmd = [sys.executable or "python3", str(script), session_path, str(output_file)]
        else:
            cmd = [*base_cmd, "--export", session_path, str(output_file)]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and output_file.exists():
            return output_file.read_text()
    except Exception as e:
        return f"<html><body><h1>Error exporting session</h1><pre>{html.escape(str(e))}</pre></body></html>"

    error_text = result.stderr or result.stdout or "Unknown export error"
    return f"<html><body><h1>Error exporting session</h1><pre>{html.escape(error_text)}</pre></body></html>"


def get_session_cwd(session_path: str, source_type: str = "standard") -> str:
    """Get the working directory from a session file."""
    try:
        with open(session_path, "r") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                data = json.loads(line)
                if source_type == "claude":
                    if data.get("type") in ("file-history-snapshot", "summary"):
                        continue
                    if data.get("cwd"):
                        return data["cwd"]
                elif source_type == "codex":
                    if data.get("type") == "session_meta":
                        return data.get("payload", {}).get("cwd", "")
                else:
                    if data.get("type") == "session" and "cwd" in data:
                        return data["cwd"]
                break
    except (OSError, json.JSONDecodeError, KeyError, TypeError):
        pass
    return ""


def _build_codex_project_stats(
    project_cwd: str, files: list[Path], agent_cmd: str
) -> ProjectStats | None:
    """Build a ProjectStats from a list of Codex session files grouped by cwd."""
    project_stats: ProjectStats = {
        "name": project_cwd,
        "agent_cmd": agent_cmd,
        "sessions": [],
        "total_messages": 0,
        "total_tokens": 0,
        "total_output_tokens": 0,
        "total_cost": 0.0,
        "total_llm_time": 0.0,
        "total_tool_time": 0.0,
        "models": defaultdict(create_model_stats),
        "tools": defaultdict(create_tool_stats),
        "daily_stats": defaultdict(create_daily_stats),
        "first_activity": None,
        "last_activity": None,
        "tps_samples": [],
    }

    for filepath in sorted(files):
        stats = analyze_codex_jsonl_file(filepath)
        if stats["messages"] == 0:
            continue

        duration = (
            (stats["end"] - stats["start"]).total_seconds()
            if stats["start"] and stats["end"]
            else 0
        )

        session_uid = get_session_id_from_file(str(filepath), "codex") or str(
            uuid.uuid4()
        )

        session = Session(
            file=filepath.name,
            path=str(filepath),
            uid=session_uid,
            relative_path=filepath.name,
            cwd=stats["cwd"],
            agent_cmd=agent_cmd,
            messages=stats["messages"],
            tokens=stats["total_tokens"],
            output_tokens=stats["output_tokens"],
            cost=stats["cost_total"],
            start=stats["start"],
            end=stats["end"],
            duration=duration,
            llm_time=stats["llm_time"],
            tool_time=stats["tool_time"],
            tools=dict(stats["tools"]),
            avg_tps=calc_avg_tokens_per_sec(stats["tps_samples"]),
            subagent_sessions=[],
        )
        SESSION_REGISTRY[session_uid] = session
        project_stats["sessions"].append(session)

        project_stats["total_messages"] += stats["messages"]
        project_stats["total_tokens"] += stats["total_tokens"]
        project_stats["total_output_tokens"] += stats["output_tokens"]
        project_stats["total_cost"] += stats["cost_total"]
        project_stats["total_llm_time"] += stats["llm_time"]
        project_stats["total_tool_time"] += stats["tool_time"]
        project_stats["tps_samples"].extend(stats["tps_samples"])

        for model, mstats in stats["models"].items():
            project_stats["models"][model]["messages"] += mstats["messages"]
            project_stats["models"][model]["tokens"] += mstats["tokens"]
            project_stats["models"][model]["cost"] += mstats["cost"]
            project_stats["models"][model]["llm_time"] += mstats.get("llm_time", 0)
            project_stats["models"][model]["output_tokens"] += mstats.get(
                "output_tokens", 0
            )

        for tool_name, tstats in stats["tools"].items():
            project_stats["tools"][tool_name]["calls"] += tstats["calls"]
            project_stats["tools"][tool_name]["time"] += tstats["time"]
            project_stats["tools"][tool_name]["errors"] += tstats["errors"]

        for ts in stats["timestamps"]:
            day_key = ts.strftime("%Y-%m-%d")
            project_stats["daily_stats"][day_key]["messages"] += 1
            project_stats["daily_stats"][day_key]["cost"] += stats[
                "cost_total"
            ] / max(len(stats["timestamps"]), 1)

        if stats["start"]:
            if (
                project_stats["first_activity"] is None
                or stats["start"] < project_stats["first_activity"]
            ):
                project_stats["first_activity"] = stats["start"]
        if stats["end"]:
            if (
                project_stats["last_activity"] is None
                or stats["end"] > project_stats["last_activity"]
            ):
                project_stats["last_activity"] = stats["end"]

    return project_stats if project_stats["sessions"] else None


def _accumulate_global_stats(
    global_stats: GlobalStats, project_stats: ProjectStats
) -> None:
    """Accumulate project stats into global stats."""
    global_stats["total_cost"] += project_stats["total_cost"]
    global_stats["total_tokens"] += project_stats["total_tokens"]
    global_stats["total_output_tokens"] += project_stats["total_output_tokens"]
    global_stats["total_messages"] += project_stats["total_messages"]
    global_stats["total_sessions"] += len(project_stats["sessions"])
    global_stats["total_projects"] += 1
    global_stats["total_llm_time"] += project_stats["total_llm_time"]
    global_stats["total_tool_time"] += project_stats["total_tool_time"]
    global_stats["tps_samples"].extend(project_stats["tps_samples"])

    for model, mstats in project_stats["models"].items():
        global_stats["models"][model]["messages"] += mstats["messages"]
        global_stats["models"][model]["tokens"] += mstats["tokens"]
        global_stats["models"][model]["cost"] += mstats["cost"]
        global_stats["models"][model]["llm_time"] += mstats.get("llm_time", 0)
        global_stats["models"][model]["output_tokens"] += mstats.get(
            "output_tokens", 0
        )

    for tool_name, tstats in project_stats["tools"].items():
        global_stats["tools"][tool_name]["calls"] += tstats["calls"]
        global_stats["tools"][tool_name]["time"] += tstats["time"]
        global_stats["tools"][tool_name]["errors"] += tstats["errors"]

    for day, dstats in project_stats["daily_stats"].items():
        global_stats["daily_stats"][day]["messages"] += dstats["messages"]
        global_stats["daily_stats"][day]["cost"] += dstats["cost"]


def collect_all_stats() -> tuple[list[ProjectStats], GlobalStats]:
    """Collect statistics from all projects."""
    # Clear the session registry to avoid stale entries on reload
    clear_session_registry()

    all_projects: list[ProjectStats] = []
    global_stats: GlobalStats = {
        "total_cost": 0.0,
        "total_tokens": 0,
        "total_output_tokens": 0,
        "total_messages": 0,
        "total_sessions": 0,
        "total_projects": 0,
        "total_llm_time": 0.0,
        "total_tool_time": 0.0,
        "models": defaultdict(create_model_stats),
        "tools": defaultdict(create_tool_stats),
        "daily_stats": defaultdict(create_daily_stats),
        "tps_samples": [],
    }

    for sessions_dir, agent_cmd, source_type in SESSIONS_DIRS:
        if not sessions_dir.exists():
            continue

        if source_type == "codex":
            # Codex: date-based hierarchy (YYYY/MM/DD/file.jsonl)
            # Group sessions by cwd to create virtual "projects"
            codex_projects: dict[str, list[Path]] = defaultdict(list)
            for jsonl_file in sessions_dir.rglob("*.jsonl"):
                cwd = get_session_cwd(str(jsonl_file), "codex")
                key = cwd if cwd else "unknown"
                codex_projects[key].append(jsonl_file)

            for project_cwd, files in codex_projects.items():
                # Create a temporary directory-like structure for analyze
                # by building ProjectStats directly
                project_stats = _build_codex_project_stats(
                    project_cwd, files, agent_cmd
                )
                if project_stats and project_stats["sessions"]:
                    all_projects.append(project_stats)
                    _accumulate_global_stats(global_stats, project_stats)
            continue

        # Standard and Claude: iterate per-project subdirectories
        for project_dir in sessions_dir.iterdir():
            if not project_dir.is_dir() or project_dir.name.startswith("."):
                continue

            project_stats = analyze_project(project_dir, agent_cmd, source_type)
            if project_stats:
                all_projects.append(project_stats)
                _accumulate_global_stats(global_stats, project_stats)

    return all_projects, global_stats


def generate_html():
    """Generate HTML dashboard."""
    all_projects, global_stats = collect_all_stats()

    # Sort projects by cost for initial display
    all_projects.sort(key=lambda p: -p["total_cost"])

    # Build projects JSON for client-side sorting
    projects_json = []
    for p in all_projects:
        sessions_json = []
        for s in p["sessions"]:
            duration_secs = s["duration"] if s["duration"] else 0
            llm_secs = s["llm_time"] if s["llm_time"] else 0

            # Include subagent sessions in JSON
            sub_sessions_json = []
            for sub in s.get("subagent_sessions", []):
                sub_duration = sub["duration"] if sub["duration"] else 0
                sub_llm = sub["llm_time"] if sub["llm_time"] else 0
                sub_tool = sub.get("tool_time", 0) if sub.get("tool_time") else 0
                sub_tps = sub.get("avg_tps", 0)
                sub_sessions_json.append(
                    {
                        "file": sub["file"],
                        "uid": sub["uid"],
                        "path": sub[
                            "path"
                        ],  # Keep path for resume command (local use only)
                        "relative_path": sub["relative_path"],
                        "cwd": sub["cwd"],
                        "messages": sub["messages"],
                        "tokens": sub["tokens"],
                        "cost": sub["cost"],
                        "start": sub["start"].isoformat() if sub["start"] else "",
                        "start_display": sub["start"].strftime("%Y-%m-%d %H:%M")
                        if sub["start"]
                        else "N/A",
                        "end": sub["end"].isoformat() if sub["end"] else "",
                        "duration": sub_duration,
                        "duration_display": format_duration(sub_duration),
                        "llm_time": sub_llm,
                        "llm_time_display": format_duration(sub_llm),
                        "tool_time": sub_tool,
                        "tool_time_display": format_duration(sub_tool),
                        "avg_tps": sub_tps,
                    }
                )

            tool_secs = s.get("tool_time", 0) if s.get("tool_time") else 0
            session_tps = s.get("avg_tps", 0)
            sessions_json.append(
                {
                    "file": s["file"],
                    "uid": s["uid"],
                    "path": s["path"],  # Keep path for resume command (local use only)
                    "relative_path": s.get("relative_path", s["file"]),
                    "cwd": s["cwd"],
                    "messages": s["messages"],
                    "tokens": s["tokens"],
                    "cost": s["cost"],
                    "start": s["start"].isoformat() if s["start"] else "",
                    "start_display": s["start"].strftime("%Y-%m-%d %H:%M")
                    if s["start"]
                    else "N/A",
                    "end": s["end"].isoformat() if s["end"] else "",
                    "duration": duration_secs,
                    "duration_display": format_duration(duration_secs),
                    "llm_time": llm_secs,
                    "llm_time_display": format_duration(llm_secs),
                    "tool_time": tool_secs,
                    "tool_time_display": format_duration(tool_secs),
                    "avg_tps": session_tps,
                    "subagent_sessions": sub_sessions_json,
                }
            )
        # Build model breakdown for this project
        models_list = []
        for model_name, mstats in sorted(
            p["models"].items(), key=lambda x: -x[1]["cost"]
        ):
            model_tps = (
                mstats.get("output_tokens", 0) / mstats.get("llm_time", 1)
                if mstats.get("llm_time", 0) > 0
                else 0
            )
            models_list.append(
                {
                    "name": model_name,
                    "messages": mstats["messages"],
                    "tokens": mstats["tokens"],
                    "cost": mstats["cost"],
                    "avg_tps": model_tps,
                }
            )

        # Build tool breakdown for this project
        tools_list = []
        for tool_name, tstats in sorted(
            p["tools"].items(), key=lambda x: -x[1]["time"]
        ):
            tools_list.append(
                {
                    "name": tool_name,
                    "calls": tstats["calls"],
                    "time": tstats["time"],
                    "time_display": format_duration(tstats["time"]),
                    "errors": tstats["errors"],
                    "avg_time": tstats["time"] / tstats["calls"]
                    if tstats["calls"] > 0
                    else 0,
                    "avg_time_display": format_duration(
                        tstats["time"] / tstats["calls"]
                    )
                    if tstats["calls"] > 0
                    else "0s",
                }
            )

        project_avg_tps = calc_avg_tokens_per_sec(p["tps_samples"])
        projects_json.append(
            {
                "name": p["name"],
                "agent_cmd": p["agent_cmd"],  # Needed for resume command
                "sessions": len(p["sessions"]),
                "sessions_list": sessions_json,
                "messages": p["total_messages"],
                "tokens": p["total_tokens"],
                "cost": p["total_cost"],
                "llm_time": p["total_llm_time"],
                "llm_time_display": format_duration(p["total_llm_time"]),
                "tool_time": p["total_tool_time"],
                "tool_time_display": format_duration(p["total_tool_time"]),
                "avg_tps": project_avg_tps,
                "last_activity": p["last_activity"].isoformat()
                if p["last_activity"]
                else "",
                "last_activity_display": p["last_activity"].strftime("%Y-%m-%d %H:%M")
                if p["last_activity"]
                else "N/A",
                "models": models_list,
                "tools": tools_list,
            }
        )

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Agent Cost Dashboard</title>
    <style>
        :root {{
            --bg-primary: #0d1117;
            --bg-secondary: #161b22;
            --bg-tertiary: #21262d;
            --border-color: #30363d;
            --text-primary: #e6edf3;
            --text-secondary: #8b949e;
            --accent-blue: #58a6ff;
            --accent-green: #3fb950;
            --accent-yellow: #d29922;
            --accent-red: #f85149;
            --accent-purple: #a371f7;
        }}
        
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.5;
            padding: 20px;
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}
        
        h1 {{
            font-size: 28px;
            margin-bottom: 8px;
        }}
        
        .subtitle {{
            color: var(--text-secondary);
            margin-bottom: 24px;
        }}
        
        .stats-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 16px;
            margin-bottom: 32px;
        }}
        
        .stat-card {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 16px;
        }}
        
        .stat-card .label {{
            color: var(--text-secondary);
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        
        .stat-card .value {{
            font-size: 28px;
            font-weight: 600;
            margin-top: 4px;
        }}
        
        .stat-card .value.cost {{
            color: var(--accent-green);
        }}
        
        .section {{
            background: var(--bg-secondary);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            margin-bottom: 24px;
            overflow: hidden;
        }}
        
        .section-header {{
            padding: 16px;
            border-bottom: 1px solid var(--border-color);
            font-weight: 600;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        
        .section-header .badge {{
            background: var(--bg-tertiary);
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 12px;
            color: var(--text-secondary);
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        
        th, td {{
            padding: 12px 16px;
            text-align: left;
            border-bottom: 1px solid var(--border-color);
        }}
        
        th {{
            background: var(--bg-tertiary);
            font-weight: 600;
            font-size: 12px;
            text-transform: uppercase;
            color: var(--text-secondary);
            cursor: pointer;
            user-select: none;
            white-space: nowrap;
        }}
        
        th:hover {{
            color: var(--text-primary);
        }}
        
        th .sort-icon {{
            margin-left: 4px;
            opacity: 0.3;
        }}
        
        th.sorted .sort-icon {{
            opacity: 1;
        }}
        
        tr:hover {{
            background: var(--bg-tertiary);
        }}
        
        .project-name {{
            font-family: monospace;
            color: var(--accent-blue);
        }}
        
        .cost {{
            color: var(--accent-green);
            font-weight: 500;
        }}
        
        .tokens {{
            color: var(--text-secondary);
        }}
        
        .model-tag {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            margin-right: 4px;
            margin-bottom: 4px;
        }}
        
        .model-claude {{
            background: rgba(167, 113, 247, 0.2);
            color: var(--accent-purple);
        }}
        
        .model-other {{
            background: rgba(88, 166, 255, 0.2);
            color: var(--accent-blue);
        }}
        
        .bar-container {{
            width: 100%;
            height: 8px;
            background: var(--bg-tertiary);
            border-radius: 4px;
            overflow: hidden;
        }}
        
        .bar {{
            height: 100%;
            background: var(--accent-green);
            border-radius: 4px;
        }}
        
        .daily-chart {{
            padding: 16px;
        }}
        
        .daily-bar {{
            display: flex;
            align-items: center;
            margin-bottom: 8px;
        }}
        
        .daily-bar .date {{
            width: 100px;
            font-size: 13px;
            color: var(--text-secondary);
        }}
        
        .daily-bar .bar-wrapper {{
            flex: 1;
            margin: 0 12px;
        }}
        
        .daily-bar .amount {{
            width: 80px;
            text-align: right;
            font-size: 13px;
            color: var(--accent-green);
        }}
        
        .refresh-note {{
            color: var(--text-secondary);
            font-size: 12px;
        }}
        
        .session-link {{
            color: var(--accent-blue);
            text-decoration: none;
            cursor: pointer;
        }}
        
        .session-link:hover {{
            text-decoration: underline;
        }}
        
        .expand-btn {{
            background: none;
            border: none;
            color: var(--text-secondary);
            cursor: pointer;
            padding: 4px 8px;
            font-size: 12px;
        }}
        
        .expand-btn:hover {{
            color: var(--text-primary);
        }}
        
        .sessions-dropdown {{
            display: none;
            background: var(--bg-tertiary);
            padding: 8px 16px;
            margin-top: 4px;
            border-radius: 4px;
        }}
        
        .sessions-dropdown.show {{
            display: block;
        }}
        
        .session-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 6px 0;
            border-bottom: 1px solid var(--border-color);
            font-size: 13px;
        }}
        
        .session-item:last-child {{
            border-bottom: none;
        }}
        
        .session-info {{
            display: flex;
            gap: 16px;
            color: var(--text-secondary);
        }}
        
        .back-link {{
            color: var(--accent-blue);
            text-decoration: none;
            margin-bottom: 16px;
            display: inline-block;
        }}
        
        .back-link:hover {{
            text-decoration: underline;
        }}
        
        .expandable-row {{
            cursor: pointer;
        }}
        
        .expandable-row:hover {{
            background: var(--bg-tertiary);
        }}
        
        .expand-icon {{
            display: inline-block;
            width: 16px;
            color: var(--text-secondary);
            transition: transform 0.2s;
        }}
        
        .expandable-row.expanded .expand-icon {{
            transform: rotate(90deg);
        }}
        
        .model-breakdown {{
            display: none;
        }}
        
        .model-breakdown.show {{
            display: table-row;
        }}
        
        .model-breakdown td {{
            padding: 0;
            background: var(--bg-tertiary);
        }}
        
        .model-tree {{
            padding: 8px 16px 8px 32px;
        }}
        
        .model-item {{
            display: flex;
            align-items: center;
            padding: 6px 0;
            font-size: 13px;
            border-bottom: 1px solid var(--border-color);
        }}
        
        .model-item:last-child {{
            border-bottom: none;
        }}
        
        .model-name {{
            flex: 1;
            color: var(--accent-purple);
        }}
        
        .model-stat {{
            margin-left: 16px;
            color: var(--text-secondary);
            min-width: 80px;
            text-align: right;
        }}
        
        .model-stat.cost {{
            color: var(--accent-green);
        }}
        
        .copy-btn {{
            background: var(--bg-tertiary);
            border: 1px solid var(--border-color);
            color: var(--text-primary);
            padding: 4px 10px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 12px;
            margin-right: 4px;
            transition: background 0.2s;
        }}
        
        .copy-btn:hover {{
            background: var(--accent-blue);
            border-color: var(--accent-blue);
        }}
        
        .icon-btn {{
            background: transparent;
            border: none;
            color: var(--text-secondary);
            cursor: pointer;
            font-size: 14px;
            padding: 4px;
            margin-right: 4px;
            transition: color 0.2s;
        }}
        
        .icon-btn:hover {{
            color: var(--accent-blue);
        }}
        
        .session-link {{
            color: var(--text-secondary);
            text-decoration: none;
            font-size: 14px;
            padding: 4px;
            transition: color 0.2s;
        }}
        
        .session-link:hover {{
            color: var(--accent-blue);
        }}
        
        footer {{
            text-align: center;
            padding: 24px;
            color: var(--text-secondary);
            font-size: 13px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>🤖 Agent Cost Dashboard</h1>
        <p class="subtitle">Generated on {datetime.now().strftime("%Y-%m-%d %H:%M:%S")} <span class="refresh-note">(Refresh page for updated stats)</span></p>
        
        <div class="stats-grid">
            <div class="stat-card">
                <div class="label">Total Cost</div>
                <div class="value cost">${global_stats["total_cost"]:.2f}</div>
            </div>
            <div class="stat-card">
                <div class="label">Projects</div>
                <div class="value">{global_stats["total_projects"]}</div>
            </div>
            <div class="stat-card">
                <div class="label">Sessions</div>
                <div class="value">{global_stats["total_sessions"]}</div>
            </div>
            <div class="stat-card">
                <div class="label">API Calls</div>
                <div class="value">{global_stats["total_messages"]:,}</div>
            </div>
            <div class="stat-card">
                <div class="label">Total Tokens</div>
                <div class="value">{global_stats["total_tokens"] / 1_000_000:.1f}M</div>
            </div>
            <div class="stat-card">
                <div class="label">LLM Time</div>
                <div class="value" style="color: var(--accent-purple)">{format_duration(global_stats["total_llm_time"])}</div>
            </div>
            <div class="stat-card">
                <div class="label">Tool Time</div>
                <div class="value" style="color: var(--accent-yellow)">{format_duration(global_stats["total_tool_time"])}</div>
            </div>
            <div class="stat-card">
                <div class="label">Avg Tokens/s</div>
                <div class="value" style="color: var(--accent-blue)">{calc_avg_tokens_per_sec(global_stats["tps_samples"]):.1f}</div>
            </div>
        </div>
        
        <div class="section">
            <div class="section-header">
                <span>📊 Daily Spending</span>
            </div>
            <div class="daily-chart">
"""

    # Daily chart
    if global_stats["daily_stats"]:
        max_daily = max(d["cost"] for d in global_stats["daily_stats"].values())
        for day in sorted(global_stats["daily_stats"].keys())[-14:]:
            stats = global_stats["daily_stats"][day]
            pct = (stats["cost"] / max_daily * 100) if max_daily > 0 else 0
            html_content += f"""
                <div class="daily-bar">
                    <span class="date">{day}</span>
                    <div class="bar-wrapper">
                        <div class="bar-container">
                            <div class="bar" style="width: {pct}%"></div>
                        </div>
                    </div>
                    <span class="amount">${stats["cost"]:.2f}</span>
                </div>
"""

    html_content += """
            </div>
        </div>
        
        <div class="section">
            <div class="section-header">
                <span>🤖 Models Used</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Model</th>
                        <th>Messages</th>
                        <th>Tokens</th>
                        <th>Avg Tokens/s</th>
                        <th>Cost</th>
                        <th>% of Total</th>
                    </tr>
                </thead>
                <tbody>
"""

    for model, mstats in sorted(
        global_stats["models"].items(), key=lambda x: -x[1]["cost"]
    ):
        pct = (
            (mstats["cost"] / global_stats["total_cost"] * 100)
            if global_stats["total_cost"] > 0
            else 0
        )
        model_class = "model-claude" if "claude" in model.lower() else "model-other"
        # Calculate tokens/second for this model
        model_tps = (
            mstats["output_tokens"] / mstats["llm_time"]
            if mstats.get("llm_time", 0) > 0
            else 0
        )
        html_content += f"""
                    <tr>
                        <td><span class="model-tag {model_class}">{html.escape(model)}</span></td>
                        <td>{mstats["messages"]:,}</td>
                        <td class="tokens">{mstats["tokens"]:,}</td>
                        <td style="color: var(--accent-blue)">{model_tps:.1f}</td>
                        <td class="cost">${mstats["cost"]:.2f}</td>
                        <td>
                            <div class="bar-container" style="width: 100px; display: inline-block; vertical-align: middle;">
                                <div class="bar" style="width: {pct}%"></div>
                            </div>
                            {pct:.1f}%
                        </td>
                    </tr>
"""

    html_content += """
                </tbody>
            </table>
        </div>
        
        <div class="section">
            <div class="section-header">
                <span>🔧 Tools Used</span>
            </div>
            <table>
                <thead>
                    <tr>
                        <th>Tool</th>
                        <th>Calls</th>
                        <th>Total Time</th>
                        <th>Avg Time</th>
                        <th>Errors</th>
                        <th>% of Time</th>
                    </tr>
                </thead>
                <tbody>
"""

    total_tool_time = global_stats["total_tool_time"]
    for tool_name, tstats in sorted(
        global_stats["tools"].items(), key=lambda x: -x[1]["time"]
    ):
        pct = (tstats["time"] / total_tool_time * 100) if total_tool_time > 0 else 0
        avg_time = tstats["time"] / tstats["calls"] if tstats["calls"] > 0 else 0
        error_style = (
            "color: var(--accent-red)"
            if tstats["errors"] > 0
            else "color: var(--text-secondary)"
        )
        html_content += f'''
                    <tr>
                        <td><span class="model-tag model-other">{html.escape(tool_name)}</span></td>
                        <td>{tstats["calls"]:,}</td>
                        <td style="color: var(--accent-yellow)">{format_duration(tstats["time"])}</td>
                        <td style="color: var(--text-secondary)">{format_duration(avg_time)}</td>
                        <td style="{error_style}">{tstats["errors"]}</td>
                        <td>
                            <div class="bar-container" style="width: 100px; display: inline-block; vertical-align: middle;">
                                <div class="bar" style="width: {pct}%; background: var(--accent-yellow)"></div>
                            </div>
                            {pct:.1f}%
                        </td>
                    </tr>
'''

    html_content += (
        """
                </tbody>
            </table>
        </div>
        
        <div class="section">
            <div class="section-header">
                <span>📁 Projects</span>
                <span class="badge">"""
        + str(len(all_projects))
        + """ projects</span>
            </div>
            <table id="projects-table">
                <thead>
                    <tr>
                        <th data-sort="name">Project <span class="sort-icon">▼</span></th>
                        <th data-sort="sessions">Sessions <span class="sort-icon">▼</span></th>
                        <th data-sort="messages">Messages <span class="sort-icon">▼</span></th>
                        <th data-sort="tokens">Tokens <span class="sort-icon">▼</span></th>
                        <th data-sort="llm_time">LLM Time <span class="sort-icon">▼</span></th>
                        <th data-sort="tool_time">Tool Time <span class="sort-icon">▼</span></th>
                        <th data-sort="avg_tps">Tok/s <span class="sort-icon">▼</span></th>
                        <th data-sort="cost">Cost <span class="sort-icon">▼</span></th>
                        <th data-sort="last_activity" class="sorted">Last Activity <span class="sort-icon">▼</span></th>
                    </tr>
                </thead>
                <tbody id="projects-tbody">
                </tbody>
            </table>
        </div>
        
        <div class="section">
            <div class="section-header">
                <span>📜 All Sessions</span>
                <span class="badge" id="sessions-count"></span>
            </div>
            <table id="sessions-table">
                <thead>
                    <tr>
                        <th data-sort="project">Project / Session <span class="sort-icon">▼</span></th>
                        <th data-sort="start">Date <span class="sort-icon">▼</span></th>
                        <th data-sort="duration">Duration <span class="sort-icon">▼</span></th>
                        <th data-sort="llm_time">LLM Time <span class="sort-icon">▼</span></th>
                        <th data-sort="tool_time">Tool Time <span class="sort-icon">▼</span></th>
                        <th data-sort="avg_tps">Tok/s <span class="sort-icon">▼</span></th>
                        <th data-sort="messages">Messages <span class="sort-icon">▼</span></th>
                        <th data-sort="tokens">Tokens <span class="sort-icon">▼</span></th>
                        <th data-sort="cost">Cost <span class="sort-icon">▼</span></th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody id="sessions-tbody">
                </tbody>
            </table>
        </div>
        
        <footer>
            Agent Cost Dashboard • Data from ~/.pi, ~/.omp, ~/.claude, and ~/.codex
        </footer>
    </div>
    
    <script>
        const projects = """
        + json.dumps(projects_json)
        + """;

        function buildResumeCmd(agentCmd, cwd, sessionPath, sessionUid) {
            if (agentCmd === 'claude') {
                return 'cd "' + cwd + '" && claude --resume "' + sessionUid + '"';
            } else if (agentCmd === 'codex') {
                return 'cd "' + cwd + '" && codex --resume "' + sessionUid + '"';
            } else {
                return 'cd "' + cwd + '" && ' + agentCmd + ' --session "' + sessionPath + '"';
            }
        }

        function formatDuration(seconds) {
            if (seconds < 60) {
                return Math.round(seconds) + 's';
            } else if (seconds < 3600) {
                const mins = Math.floor(seconds / 60);
                const secs = Math.round(seconds % 60);
                return mins + 'm' + secs.toString().padStart(2, '0') + 's';
            } else {
                const hours = Math.floor(seconds / 3600);
                const mins = Math.round((seconds % 3600) / 60);
                return hours + 'h' + mins.toString().padStart(2, '0') + 'm';
            }
        }

        // Flatten all sessions for the sessions table
        const allSessions = [];
        projects.forEach(p => {
            p.sessions_list.forEach(s => {
                allSessions.push({
                    project: p.name,
                    ...s
                });
            });
        });

        // Group sessions by project for expandable display
        const sessionsByProject = {};
        allSessions.forEach(s => {
            if (!sessionsByProject[s.project]) {
                sessionsByProject[s.project] = [];
            }
            sessionsByProject[s.project].push(s);
        });

        let projectSort = { field: 'last_activity', asc: false };
        let sessionSort = { field: 'end', asc: false };  // Sort by last activity (most recent first)
        let sessionsSort = { field: 'end', asc: false };
        
        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }
        
        function sortData(data, sort) {
            return [...data].sort((a, b) => {
                let aVal = a[sort.field];
                let bVal = b[sort.field];
                
                if (typeof aVal === 'string') {
                    aVal = aVal.toLowerCase();
                    bVal = bVal.toLowerCase();
                }
                
                if (aVal < bVal) return sort.asc ? -1 : 1;
                if (aVal > bVal) return sort.asc ? 1 : -1;
                return 0;
            });
        }
        
        function renderProjects() {
            const tbody = document.getElementById('projects-tbody');
            const sorted = sortData(projects, projectSort);
            const maxCost = Math.max(...projects.map(p => p.cost));
            
            tbody.innerHTML = sorted.map((p, idx) => {
                const shortName = p.name.length > 50 ? '...' + p.name.slice(-47) : p.name;
                const rowId = 'project-' + idx;
                
                // Build model breakdown HTML
                const modelRows = p.models.map(m => `
                    <div class="model-item">
                        <span class="model-name">${escapeHtml(m.name)}</span>
                        <span class="model-stat">${m.messages} msgs</span>
                        <span class="model-stat">${m.tokens.toLocaleString()} tok</span>
                        <span class="model-stat" style="color: var(--accent-blue)">${(m.avg_tps || 0).toFixed(1)} tok/s</span>
                        <span class="model-stat cost">$${m.cost.toFixed(2)}</span>
                    </div>
                `).join('');
                
                // Build tool breakdown HTML
                const toolRows = (p.tools || []).map(t => `
                    <div class="model-item">
                        <span class="model-name" style="color: var(--accent-yellow)">${escapeHtml(t.name)}</span>
                        <span class="model-stat">${t.calls} calls</span>
                        <span class="model-stat" style="color: var(--accent-yellow)">${t.time_display}</span>
                        <span class="model-stat">avg ${t.avg_time_display}</span>
                        ${t.errors > 0 ? `<span class="model-stat" style="color: var(--accent-red)">${t.errors} errors</span>` : ''}
                    </div>
                `).join('');
                
                return `
                    <tr class="expandable-row" data-target="${rowId}" onclick="toggleProjectRow('${rowId}')">
                        <td class="project-name" title="${escapeHtml(p.name)}"><span class="expand-icon">▶</span> ${escapeHtml(shortName)}</td>
                        <td>${p.sessions}</td>
                        <td>${p.messages.toLocaleString()}</td>
                        <td class="tokens">${p.tokens.toLocaleString()}</td>
                        <td style="color: var(--accent-purple)">${p.llm_time_display}</td>
                        <td style="color: var(--accent-yellow)">${p.tool_time_display}</td>
                        <td style="color: var(--accent-blue)">${(p.avg_tps || 0).toFixed(1)}</td>
                        <td class="cost">$${p.cost.toFixed(2)}</td>
                        <td style="color: var(--text-secondary)">${p.last_activity_display}</td>
                    </tr>
                    <tr class="model-breakdown" id="${rowId}">
                        <td colspan="9">
                            <div class="model-tree">
                                <div style="font-weight: 600; margin-bottom: 8px; color: var(--text-secondary)">Models:</div>
                                ${modelRows || '<div style="color: var(--text-secondary)">No model data</div>'}
                                ${toolRows ? `<div style="font-weight: 600; margin: 12px 0 8px 0; color: var(--text-secondary)">Tools:</div>${toolRows}` : ''}
                            </div>
                        </td>
                    </tr>
                `;
            }).join('');
        }
        
        function toggleProjectRow(rowId) {
            const row = document.getElementById(rowId);
            const parentRow = document.querySelector('[data-target="' + rowId + '"]');
            row.classList.toggle('show');
            parentRow.classList.toggle('expanded');
        }
        
        function renderSessions() {
            const tbody = document.getElementById('sessions-tbody');

            // Flatten sessions with subagent info
            const allSessionsWithSubs = [];
            projects.forEach(p => {
                p.sessions_list.forEach(s => {
                    // Add agent_cmd from parent project for resume command
                    allSessionsWithSubs.push({...s, agent_cmd: p.agent_cmd});
                });
            });

            // Helper to get aggregated value for a session (including subagents)
            function getAggregatedValue(s, field) {
                const subs = s.subagent_sessions || [];
                const all = [s, ...subs];
                
                switch(field) {
                    case 'cost':
                        return all.reduce((sum, session) => sum + session.cost, 0);
                    case 'tokens':
                        return all.reduce((sum, session) => sum + session.tokens, 0);
                    case 'messages':
                        return all.reduce((sum, session) => sum + session.messages, 0);
                    case 'llm_time':
                        return all.reduce((sum, session) => sum + (session.llm_time || 0), 0);
                    case 'tool_time':
                        return all.reduce((sum, session) => sum + (session.tool_time || 0), 0);
                    case 'avg_tps':
                        const tpsValues = all.map(session => session.avg_tps || 0).filter(v => v > 0);
                        return tpsValues.length > 0 ? tpsValues.reduce((a, b) => a + b, 0) / tpsValues.length : 0;
                    case 'duration':
                        const starts = all.map(session => session.start).filter(Boolean);
                        const ends = all.map(session => session.end).filter(Boolean);
                        if (!starts.length || !ends.length) return 0;
                        const earliest = Math.min(...starts.map(d => new Date(d)));
                        const latest = Math.max(...ends.map(d => new Date(d)));
                        return (latest - earliest) / 1000;
                    case 'start':
                        return s.start ? new Date(s.start).getTime() : 0;
                    case 'project':
                        return s.cwd.toLowerCase();
                    default:
                        return s[field] || 0;
                }
            }
            
            // Sort sessions using current sort state
            const sortedSessions = [...allSessionsWithSubs].sort((a, b) => {
                const aVal = getAggregatedValue(a, sessionsSort.field);
                const bVal = getAggregatedValue(b, sessionsSort.field);
                
                if (aVal < bVal) return sessionsSort.asc ? -1 : 1;
                if (aVal > bVal) return sessionsSort.asc ? 1 : -1;
                return 0;
            });

            const totalSessions = allSessionsWithSubs.reduce((sum, s) => sum + 1 + (s.subagent_sessions || []).length, 0);
            document.getElementById('sessions-count').textContent = totalSessions + ' sessions';

            let html = '';
            let rowIdx = 0;

            sortedSessions.forEach(s => {
                const subs = s.subagent_sessions || [];
                const hasSubs = subs.length > 0;

                // If no subagent sessions, just show the main session as a regular row
                if (!hasSubs) {
                    const sessionUrl = '/session?uid=' + encodeURIComponent(s.uid);
                    const resumePath = s.path.replace(/\\\\/g, '/');
                    const resumeCmd = buildResumeCmd(s.agent_cmd, s.cwd, resumePath, s.uid);
                    const encodedCmd = encodeURIComponent(resumeCmd);
                    const shortProject = s.cwd.length > 40 ? '...' + s.cwd.slice(-37) : s.cwd;

                    html += `
                        <tr>
                            <td class="project-name" title="${escapeHtml(s.cwd)}">${escapeHtml(shortProject)}</td>
                            <td style="color: var(--text-secondary)">${s.start_display}</td>
                            <td style="color: var(--text-secondary)">${s.duration_display}</td>
                            <td style="color: var(--accent-purple)">${s.llm_time_display}</td>
                            <td style="color: var(--accent-yellow)">${s.tool_time_display || '0s'}</td>
                            <td style="color: var(--accent-blue)">${(s.avg_tps || 0).toFixed(1)}</td>
                            <td>${s.messages.toLocaleString()}</td>
                            <td class="tokens">${s.tokens.toLocaleString()}</td>
                            <td class="cost">$${s.cost.toFixed(2)}</td>
                            <td>
                                <button onclick="copyResumeCommand(event, decodeURIComponent('${encodedCmd}'))" class="icon-btn" title="Resume session">📋</button>
                                <a href="${sessionUrl}" class="session-link" target="_blank" title="View full session">Open →</a>
                            </td>
                        </tr>
                    `;
                    return;
                }

                // Has subagent sessions - show expandable summary
                const allSessionsInGroup = [s, ...subs];
                const projectId = 'session-group-' + rowIdx;
                rowIdx++;

                // Calculate aggregated totals
                const aggCost = allSessionsInGroup.reduce((sum, session) => sum + session.cost, 0);
                const aggTokens = allSessionsInGroup.reduce((sum, session) => sum + session.tokens, 0);
                const aggMessages = allSessionsInGroup.reduce((sum, session) => sum + session.messages, 0);
                const aggLlmTime = allSessionsInGroup.reduce((sum, session) => sum + (session.llm_time || 0), 0);
                const aggToolTime = allSessionsInGroup.reduce((sum, session) => sum + (session.tool_time || 0), 0);

                // Get earliest start and latest end
                const starts = allSessionsInGroup.map(session => session.start).filter(Boolean);
                const ends = allSessionsInGroup.map(session => session.end).filter(Boolean);
                const earliestStart = starts.length ? new Date(Math.min(...starts.map(d => new Date(d)))) : null;
                const latestEnd = ends.length ? new Date(Math.max(...ends.map(d => new Date(d)))) : null;
                const totalDuration = earliestStart && latestEnd ? (latestEnd - earliestStart) / 1000 : 0;

                const shortProject = s.cwd.length > 40 ? '...' + s.cwd.slice(-37) : s.cwd;

                // Format date to match other sessions (YYYY-MM-DD HH:MM)
                const dateDisplay = s.start_display;

                // Summary row with resume/open buttons
                const sessionUrl = '/session?uid=' + encodeURIComponent(s.uid);
                const resumePath = s.path.replace(/\\\\/g, '/');
                const resumeCmd = buildResumeCmd(s.agent_cmd, s.cwd, resumePath, s.uid);
                const encodedCmd = encodeURIComponent(resumeCmd);

                // Calculate average tokens/sec for aggregated sessions
                const tpsValues = allSessionsInGroup.map(session => session.avg_tps || 0).filter(v => v > 0);
                const aggAvgTps = tpsValues.length > 0 ? tpsValues.reduce((a, b) => a + b, 0) / tpsValues.length : 0;

                html += `
                    <tr class="expandable-row" data-target="${projectId}" onclick="toggleProjectRow('${projectId}')">
                        <td class="project-name" title="${escapeHtml(s.cwd)}">
                            <span class="expand-icon">▶</span>
                            ${escapeHtml(shortProject)}
                        </td>
                        <td style="color: var(--text-secondary)">${dateDisplay}</td>
                        <td style="color: var(--text-secondary)">${formatDuration(totalDuration)}</td>
                        <td style="color: var(--accent-purple)">${formatDuration(aggLlmTime)}</td>
                        <td style="color: var(--accent-yellow)">${formatDuration(aggToolTime)}</td>
                        <td style="color: var(--accent-blue)">${aggAvgTps.toFixed(1)}</td>
                        <td>${aggMessages.toLocaleString()}</td>
                        <td class="tokens">${aggTokens.toLocaleString()}</td>
                        <td class="cost">$${aggCost.toFixed(2)}</td>
                        <td>
                            <button onclick="event.stopPropagation(); copyResumeCommand(event, decodeURIComponent('${encodedCmd}'))" class="icon-btn" title="Resume session">📋</button>
                            <a href="${sessionUrl}" class="session-link" target="_blank" title="View full session" onclick="event.stopPropagation()">Open →</a>
                        </td>
                    </tr>
                    <tr class="model-breakdown" id="${projectId}">
                        <td colspan="10" style="padding: 0">
                            <div class="model-tree">
                `;

                // Main session with buttons
                html += `
                    <div class="model-item">
                        <span class="model-name" title="${escapeHtml(s.file)}">
                            <strong>📁 Main Session:</strong> ${escapeHtml(s.file)}
                        </span>
                        <span class="model-stat">${s.start_display}</span>
                        <span class="model-stat">${s.duration_display}</span>
                        <span class="model-stat" style="color: var(--accent-purple)">${s.llm_time_display}</span>
                        <span class="model-stat" style="color: var(--accent-yellow)">${s.tool_time_display || '0s'}</span>
                        <span class="model-stat" style="color: var(--accent-blue)">${(s.avg_tps || 0).toFixed(1)} tok/s</span>
                        <span class="model-stat">${s.messages} msgs</span>
                        <span class="model-stat">${s.tokens.toLocaleString()} tok</span>
                        <span class="model-stat cost">$${s.cost.toFixed(2)}</span>
                        <span style="margin-left: 8px">
                            <button onclick="copyResumeCommand(event, decodeURIComponent('${encodedCmd}'))" class="icon-btn" title="Resume session">📋</button>
                            <a href="${sessionUrl}" class="session-link" target="_blank" title="View full session">Open →</a>
                        </span>
                    </div>
                `;

                // Subagent sessions with buttons
                subs.forEach(sub => {
                    const subSessionUrl = '/session?uid=' + encodeURIComponent(sub.uid);
                    const subResumePath = sub.path.replace(/\\\\/g, '/');
                    // Use parent session's agent_cmd for subagent resume command
                    const subResumeCmd = buildResumeCmd(s.agent_cmd, sub.cwd, subResumePath, sub.uid);
                    const subEncodedCmd = encodeURIComponent(subResumeCmd);

                    // Just show the filename, not the full relative path
                    const fileName = sub.file;

                    html += `
                        <div class="model-item">
                            <span class="model-name" title="${escapeHtml(sub.relative_path)}">
                                ${escapeHtml(fileName)}
                            </span>
                            <span class="model-stat">${sub.start_display}</span>
                            <span class="model-stat">${sub.duration_display}</span>
                            <span class="model-stat" style="color: var(--accent-purple)">${sub.llm_time_display}</span>
                            <span class="model-stat" style="color: var(--accent-yellow)">${sub.tool_time_display || '0s'}</span>
                            <span class="model-stat" style="color: var(--accent-blue)">${(sub.avg_tps || 0).toFixed(1)} tok/s</span>
                            <span class="model-stat">${sub.messages} msgs</span>
                            <span class="model-stat">${sub.tokens.toLocaleString()} tok</span>
                            <span class="model-stat cost">$${sub.cost.toFixed(2)}</span>
                            <span style="margin-left: 8px">
                                <button onclick="copyResumeCommand(event, decodeURIComponent('${subEncodedCmd}'))" class="icon-btn" title="Resume session">📋</button>
                                <a href="${subSessionUrl}" class="session-link" target="_blank" title="View full session">Open →</a>
                            </span>
                        </div>
                    `;
                });

                html += `
                            </div>
                        </td>
                    </tr>
                `;
            });

            tbody.innerHTML = html;
        }
        
        function copyResumeCommand(event, cmd) {
            const btn = event.target;
            
            function showSuccess() {
                const originalText = btn.textContent;
                btn.textContent = '✓';
                btn.style.color = 'var(--accent-green)';
                setTimeout(() => {
                    btn.textContent = originalText;
                    btn.style.color = '';
                }, 1500);
            }
            
            // Use clipboard API if available (HTTPS or localhost)
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(cmd).then(showSuccess).catch(err => {
                    console.error('Failed to copy:', err);
                });
            } else {
                // Fallback for HTTP contexts
                const textArea = document.createElement('textarea');
                textArea.value = cmd;
                textArea.style.position = 'fixed';
                textArea.style.left = '-9999px';
                textArea.setAttribute('readonly', '');
                document.body.appendChild(textArea);
                textArea.select();
                try {
                    document.execCommand('copy');
                    showSuccess();
                } catch (err) {
                    console.error('Fallback copy failed:', err);
                }
                document.body.removeChild(textArea);
            }
        }
        
        function setupSorting(tableId, sortState, renderFn) {
            document.querySelectorAll(`#${tableId} th[data-sort]`).forEach(th => {
                th.addEventListener('click', () => {
                    const field = th.dataset.sort;
                    if (sortState.field === field) {
                        sortState.asc = !sortState.asc;
                    } else {
                        sortState.field = field;
                        sortState.asc = field === 'name' || field === 'project' || field === 'start';
                    }
                    updateSortIcons(tableId, sortState);
                    renderFn();
                });
            });
        }
        
        function updateSortIcons(tableId, sortState) {
            document.querySelectorAll(`#${tableId} th`).forEach(th => {
                const field = th.dataset.sort;
                const icon = th.querySelector('.sort-icon');
                if (!icon) return;
                if (field === sortState.field) {
                    th.classList.add('sorted');
                    icon.textContent = sortState.asc ? '▲' : '▼';
                } else {
                    th.classList.remove('sorted');
                    icon.textContent = '▼';
                }
            });
        }
        
        // Setup
        setupSorting('projects-table', projectSort, renderProjects);
        setupSorting('sessions-table', sessionsSort, renderSessions);

        // Initial render
        renderProjects();
        renderSessions();
        updateSortIcons('projects-table', projectSort);
        updateSortIcons('sessions-table', sessionsSort);
    </script>
</body>
</html>
"""
    )

    return html_content


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    """HTTP request handler for the dashboard."""

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        query = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/" or parsed.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-type", "text/html; charset=utf-8")
            self.end_headers()
            html_content = generate_html()
            self.wfile.write(html_content.encode("utf-8"))

        elif parsed.path == "/session":
            uid = query.get("uid", [""])[0]
            session_info = SESSION_REGISTRY.get(uid)

            if session_info:
                session_path = session_info["path"]
                agent_cmd = session_info["agent_cmd"]
                if Path(session_path).exists():
                    self.send_response(200)
                    self.send_header("Content-type", "text/html; charset=utf-8")
                    self.end_headers()
                    html_content = export_session_to_html(session_path, agent_cmd)
                    self.wfile.write(html_content.encode("utf-8"))
                else:
                    self.send_response(404)
                    self.send_header("Content-type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(
                        b"<html><body><h1>Session file not found</h1></body></html>"
                    )
            else:
                self.send_response(404)
                self.send_header("Content-type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(
                    b"<html><body><h1>Invalid session ID</h1></body></html>"
                )

        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {args[0]}")


def main():
    parser = argparse.ArgumentParser(description="Agent Cost Dashboard Server")
    parser.add_argument(
        "-H",
        "--host",
        type=str,
        default="localhost",
        help="Host to bind to (default: localhost)",
    )
    parser.add_argument(
        "-p", "--port", type=int, default=8753, help="Port to serve on (default: 8753)"
    )
    args = parser.parse_args()

    # Check if any sessions directory exists
    any_exists = any(sessions_dir.exists() for sessions_dir, _, _ in SESSIONS_DIRS)
    if not any_exists:
        print("⚠️  No sessions directories found. No data to display yet.")

    # Start server
    class DashboardServer(socketserver.TCPServer):
        def server_bind(self):
            # Allow port reuse to avoid "Address already in use" on quick restart
            self.allow_reuse_address = True
            socketserver.TCPServer.server_bind(self)

    httpd = DashboardServer((args.host, args.port), DashboardHandler)
    print("🚀 Agent Cost Dashboard (pi, omp, claude, codex)")
    print(f"   Serving on: http://{args.host}:{args.port}")
    print("   Data from:")
    for sessions_dir, agent_cmd, source_type in SESSIONS_DIRS:
        exists = "✓" if sessions_dir.exists() else "✗"
        print(f"     {exists} {sessions_dir} ({agent_cmd})")
    print("\n   Press Ctrl+C to stop\n")

    # Set a timeout on the socket so we can check for shutdown periodically
    httpd.timeout = 0.5

    try:
        while True:
            httpd.handle_request()
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()

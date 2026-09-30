#!/usr/bin/env python3
"""Antigravity CLI / IDE session cost and token calculator.

Calculates token usage (input, output, cached, reasoning) and estimated API costs
for Antigravity agent sessions (.db SQLite files or directories).

Usage:
    python3 antigravity_cost.py <path/to/conversations or single .db file>
"""

import os
import sys
import glob
import sqlite3
import argparse
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse


COST_MAP = {
    "gemini-3.8-flash": {
        "input": 0.50,
        "output": 3.00,
        "cache_read": 0.05,
        "cache_write": 0.083,
    },
    "gemini-3.7-flash": {
        "input": 0.50,
        "output": 3.00,
        "cache_read": 0.05,
        "cache_write": 0.083,
    },
    "gemini-3.6-flash": {
        "input": 0.50,
        "output": 3.00,
        "cache_read": 0.05,
        "cache_write": 0.083,
    },
    "gemini-3.5-flash": {
        "input": 0.50,
        "output": 3.00,
        "cache_read": 0.05,
        "cache_write": 0.083,
    },
    "gemini-3.1-pro": {
        "input": 2.00,
        "output": 12.00,
        "cache_read": 0.20,
        "cache_write": 0.375,
    },
    "gemini-3.1-flash-lite": {
        "input": 0.25,
        "output": 1.50,
        "cache_read": 0.025,
        "cache_write": 0.083,
    },
    "gemini-3.1-flash": {
        "input": 0.50,
        "output": 3.00,
        "cache_read": 0.05,
        "cache_write": 0.083,
    },
    "gemini-2.5-pro": {
        "input": 1.25,
        "output": 10.00,
        "cache_read": 0.125,
        "cache_write": 0.375,
    },
    "gemini-2.5-flash": {
        "input": 0.30,
        "output": 2.50,
        "cache_read": 0.03,
        "cache_write": 0.083,
    },
    "claude-opus-4.6": {
        "input": 5.0,
        "output": 25.0,
        "cache_read": 0.5,
        "cache_write": 6.25,
    },
    "claude-sonnet-4.6": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write": 3.75,
    },
    "claude-sonnet-4": {
        "input": 3.0,
        "output": 15.0,
        "cache_read": 0.3,
        "cache_write": 3.75,
    },
    "gpt-oss-120b": {
        "input": 0.15,
        "output": 0.60,
        "cache_read": 0.015,
        "cache_write": 0.0,
    },
}

# Kept in sync with MANUAL_PRICING in cost_dashboard.py — update both when
# adding or changing a model's rates.

ANTIGRAVITY_MODEL_ID_MAP = {
    342: "GPT-OSS 120B (Medium)",
    1016: "Gemini 3.1 Pro (High)",
    1020: "Gemini 3.5 Flash (Medium)",
    1021: "Gemini 3.1 Flash Image",
    1026: "Claude Opus 4.6 (Thinking)",
    1035: "Claude Sonnet 4.6 (Thinking)",
    1036: "Gemini 3.1 Pro (Low)",
    1037: "Gemini 3.1 Pro (High)",
    1050: "Gemini 3.1 Flash Lite",
    1071: "Gemini 3.6 Flash (High)",
    1072: "Gemini 3.6 Flash (Medium)",
    1073: "Gemini 3.6 Flash (Low)",
    1196: "Gemini 3.6 Flash",
    1266: "Gemini 3.6 Flash",
    1298: "Gemini 3.7 Flash (High)",
    1299: "Gemini 3.7 Flash (Medium)",
    1300: "Gemini 3.7 Flash (Low)",
    1318: "Gemini 3.8 Flash (High)",
    1319: "Gemini 3.8 Flash (Medium)",
    1320: "Gemini 3.8 Flash (Low)",
    1322: "Gemini 3.8 Flash",
}


def decode_protobuf(data: bytes) -> list[tuple[int, int, any]]:
    """Decode raw protobuf bytes into a list of (field_num, wire_type, value).

    Pure Python implementation with no external dependencies.
    """
    i = 0
    res = []
    n = len(data)
    while i < n:
        key = 0
        shift = 0
        while True:
            if i >= n:
                return res
            b = data[i]
            i += 1
            key |= (b & 0x7F) << shift
            if not (b & 0x80):
                break
            shift += 7
        field_num = key >> 3
        wire_type = key & 7
        if wire_type == 0:  # varint
            val = 0
            shift = 0
            while True:
                if i >= n:
                    break
                b = data[i]
                i += 1
                val |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            res.append((field_num, 0, val))
        elif wire_type == 2:  # length-delimited
            length = 0
            shift = 0
            while True:
                if i >= n:
                    break
                b = data[i]
                i += 1
                length |= (b & 0x7F) << shift
                if not (b & 0x80):
                    break
                shift += 7
            val_bytes = data[i : i + length]
            i += length
            res.append((field_num, 2, val_bytes))
        elif wire_type == 1:  # 64-bit
            i += 8
        elif wire_type == 5:  # 32-bit
            i += 4
        else:
            return res
    return res


def get_cost_rates(model: str) -> dict[str, float] | None:
    """Find pricing rates per million tokens for a model name."""
    norm = model.lower().replace(" ", "-").replace("_", "-")
    best_pattern = None
    for pattern, pricing in COST_MAP.items():
        if pattern in norm:
            if best_pattern is None or len(pattern) > len(best_pattern):
                best_pattern = pattern
    return COST_MAP.get(best_pattern) if best_pattern else None


def session_cost(path: str) -> tuple[int, float]:
    """Calculate total tokens and cost for a single Antigravity .db session file."""
    stats = session_token_stats(path)
    return stats["total_tokens"], stats["total_cost"]


def session_token_stats(path: str) -> dict:
    """Extract detailed token and cost breakdown for an Antigravity .db session file."""
    res = {
        "messages": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "total_cost": 0.0,
        "model": "Unknown",
    }
    if not os.path.exists(path):
        return res

    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(
            "SELECT step_type, metadata FROM steps WHERE step_type IN (15, 23)"
        )
        rows = cur.fetchall()
        conn.close()
    except Exception as e:
        return res

    for st, meta in rows:
        if not meta:
            continue
        fields = decode_protobuf(meta)
        for f in fields:
            if f[0] == 9 and f[1] == 2:
                sf = decode_protobuf(f[2])
                d = {x[0]: x[2] for x in sf if x[1] == 0}
                mid = d.get(1, 0)
                input_tok = d.get(2, 0)
                output_tok = d.get(3, 0)
                cached_tok = d.get(5, 0)
                reasoning_tok = d.get(9, 0)
                total_tok = input_tok + output_tok + cached_tok

                model_name = ANTIGRAVITY_MODEL_ID_MAP.get(
                    mid, f"Antigravity Model {mid}" if mid else "Gemini Flash"
                )
                res["model"] = model_name

                rates = get_cost_rates(model_name)
                turn_cost = 0.0
                if rates:
                    turn_cost = (
                        input_tok * rates.get("input", 0.0)
                        + output_tok * rates.get("output", 0.0)
                        + cached_tok * rates.get("cache_read", 0.0)
                    ) / 1e6

                res["messages"] += 1
                res["input_tokens"] += input_tok
                res["output_tokens"] += output_tok
                res["cache_read_tokens"] += cached_tok
                res["reasoning_tokens"] += reasoning_tok
                res["total_tokens"] += total_tok
                res["total_cost"] += turn_cost

    return res


def main():
    parser = argparse.ArgumentParser("Antigravity token usage and cost calculator")
    parser.add_argument(
        "path", help="Path to Antigravity directory, conversations folder, or single .db file"
    )
    args = parser.parse_args()
    path = args.path

    files = []
    if os.path.isdir(path):
        # Check if user pointed to antigravity-cli root or conversations/
        conv_dir = os.path.join(path, "conversations")
        if os.path.isdir(conv_dir):
            files = list(glob.glob(os.path.join(conv_dir, "*.db")))
        else:
            files = list(glob.glob(os.path.join(path, "**/*.db"), recursive=True))
        files.sort()
        if not files:
            print("No Antigravity session databases found")
            return
    elif os.path.isfile(path):
        files = [path]
    else:
        print(f"Invalid path: {path}")
        return

    acc_tokens = 0
    acc_in = 0
    acc_out = 0
    acc_cached = 0
    acc_reasoning = 0
    acc_cost = 0.0
    valid_sessions = 0

    for file in files:
        stats = session_token_stats(file)
        if stats["messages"] == 0:
            continue
        valid_sessions += 1
        print(f"{file}")
        print(f"  Model: {stats['model']}")
        print(
            f"  Tokens: {stats['total_tokens']:,} "
            f"(In: {stats['input_tokens']:,}, Out: {stats['output_tokens']:,}, "
            f"Cached: {stats['cache_read_tokens']:,}, Reasoning: {stats['reasoning_tokens']:,})"
        )
        print(f"    Cost: ${stats['total_cost']:.4f}")

        acc_tokens += stats["total_tokens"]
        acc_in += stats["input_tokens"]
        acc_out += stats["output_tokens"]
        acc_cached += stats["cache_read_tokens"]
        acc_reasoning += stats["reasoning_tokens"]
        acc_cost += stats["total_cost"]

    if len(files) > 1:
        print("\n===\n")
        print(f"Total sessions: {valid_sessions}")
        print(
            f"Total tokens: {acc_tokens:,} "
            f"(In: {acc_in:,}, Out: {acc_out:,}, "
            f"Cached: {acc_cached:,}, Reasoning: {acc_reasoning:,})"
        )
        print(f"  Total cost: ${acc_cost:.2f}")


if __name__ == "__main__":
    main()

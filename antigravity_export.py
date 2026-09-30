#!/usr/bin/env python3
"""Export an Antigravity CLI / IDE session to a styled HTML transcript.

Usage:
    python3 antigravity_export.py <input.db or input.jsonl> <output.html>
"""

import os
import sys
import json
import html
import re
import sqlite3
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import unquote, urlparse

from antigravity_cost import (
    decode_protobuf,
    get_cost_rates,
    ANTIGRAVITY_MODEL_ID_MAP,
)


def escape(text: str) -> str:
    return html.escape(text or "")


def render_text(text: str) -> str:
    """Render text with basic markdown formatting."""
    if not text:
        return ""

    def replace_code_block(m):
        lang = escape(m.group(1) or "")
        code = escape(m.group(2))
        label = f'<span class="code-lang">{lang}</span>' if lang else ""
        return f'<div class="code-block">{label}<pre><code>{code}</code></pre></div>'

    text = re.sub(
        r"```(\w*)\n(.*?)```", replace_code_block, text, flags=re.DOTALL
    )
    text = re.sub(r"`([^`]+)`", r'<code class="inline-code">\1</code>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    parts = re.split(r'(<div class="code-block">.*?</div>)', text, flags=re.DOTALL)
    result = []
    for part in parts:
        if part.startswith('<div class="code-block">'):
            result.append(part)
        else:
            result.append(part.replace("\n", "<br>\n"))
    return "".join(result)


CSS = """\
:root {
    --bg-primary: #0d1117;
    --bg-secondary: #161b22;
    --bg-tertiary: #21262d;
    --border-color: #30363d;
    --text-primary: #e6edf3;
    --text-secondary: #8b949e;
    --accent-blue: #58a6ff;
    --accent-green: #3fb950;
    --accent-purple: #a371f7;
    --accent-yellow: #d29922;
    --accent-red: #f85149;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: var(--bg-primary);
    color: var(--text-primary);
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, Arial, sans-serif;
    line-height: 1.6;
    padding: 20px;
    max-width: 960px;
    margin: 0 auto;
}
.header {
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 20px;
}
.header h1 { font-size: 1.3em; margin-bottom: 8px; color: var(--accent-blue); }
.header .meta {
    display: flex;
    flex-wrap: wrap;
    gap: 16px;
    color: var(--text-secondary);
    font-size: 0.85em;
    margin-top: 8px;
}
.header .meta span strong { color: var(--text-primary); }
.message {
    margin-bottom: 14px;
    border-radius: 8px;
    border: 1px solid var(--border-color);
    overflow: hidden;
}
.message .role-label {
    padding: 6px 12px;
    font-size: 0.75em;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.05em;
}
.message .content {
    padding: 12px 16px;
    font-size: 0.9em;
}
.msg-user .role-label { background: rgba(88, 166, 255, 0.15); color: var(--accent-blue); }
.msg-user .content { background: var(--bg-secondary); }
.msg-assistant .role-label { background: rgba(63, 185, 80, 0.15); color: var(--accent-green); }
.msg-assistant .content { background: var(--bg-secondary); }
.msg-generic .role-label { background: rgba(210, 153, 34, 0.15); color: var(--accent-yellow); }
.msg-generic .content { background: var(--bg-secondary); }
.tool-call {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    margin: 10px 0;
    overflow: hidden;
}
.tool-call .tool-header {
    padding: 6px 12px;
    font-size: 0.8em;
    font-weight: 600;
    color: var(--accent-purple);
    background: rgba(163, 113, 247, 0.1);
    border-bottom: 1px solid var(--border-color);
}
.tool-call .tool-body {
    padding: 8px 12px;
    font-size: 0.82em;
    font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
    white-space: pre-wrap;
    word-break: break-all;
    color: var(--text-secondary);
}
.tool-result {
    padding: 8px 12px;
    background: rgba(0,0,0,0.25);
    border-top: 1px solid var(--border-color);
}
.tool-result .res-header {
    font-size: 0.75em;
    font-weight: 600;
    margin-bottom: 4px;
    color: var(--accent-yellow);
}
.tool-result .res-body {
    font-size: 0.82em;
    font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
    max-height: 400px;
    overflow-y: auto;
    white-space: pre-wrap;
}
.code-block { background: #000; border-radius: 6px; margin: 12px 0; border: 1px solid var(--border-color); }
.code-lang { display: block; padding: 4px 12px; font-size: 0.7em; color: var(--text-secondary); border-bottom: 1px solid var(--border-color); }
.code-block pre { padding: 12px; overflow-x: auto; font-family: 'SF Mono', Monaco, monospace; font-size: 0.85em; }
.inline-code { background: var(--bg-tertiary); padding: 2px 4px; border-radius: 4px; font-family: monospace; font-size: 0.9em; }
details.thoughts {
    margin: 8px 0;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    overflow: hidden;
}
details.thoughts summary {
    padding: 6px 12px;
    font-size: 0.8em;
    font-weight: 600;
    color: var(--accent-purple);
    background: var(--bg-tertiary);
    cursor: pointer;
}
details.thoughts .thought-body {
    padding: 10px 14px;
    border-top: 1px solid var(--border-color);
    font-size: 0.85em;
    color: var(--text-secondary);
    white-space: pre-wrap;
    max-height: 500px;
    overflow-y: auto;
}
.token-footer {
    font-size: 0.75em;
    color: var(--text-secondary);
    margin-top: 12px;
    border-top: 1px solid var(--border-color);
    padding-top: 6px;
}
"""


def extract_step_token_map(db_path: Path) -> dict[int, dict]:
    """Read turn-level tokens and models from the Antigravity SQLite database."""
    token_map = {}
    if not db_path.exists():
        return token_map

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        cur = conn.cursor()
        cur.execute(
            "SELECT idx, step_type, metadata FROM steps WHERE step_type IN (15, 23)"
        )
        for idx, st, meta in cur.fetchall():
            if not meta:
                continue
            fields = decode_protobuf(meta)
            for f in fields:
                if f[0] == 9 and f[1] == 2:
                    sf = decode_protobuf(f[2])
                    d = {x[0]: x[2] for x in sf if x[1] == 0}
                    mid = d.get(1, 0)
                    in_tok = d.get(2, 0)
                    out_tok = d.get(3, 0)
                    cached_tok = d.get(5, 0)
                    reason_tok = d.get(9, 0)
                    model_name = ANTIGRAVITY_MODEL_ID_MAP.get(
                        mid, f"Antigravity Model {mid}" if mid else "Gemini Flash"
                    )
                    rates = get_cost_rates(model_name)
                    cost = 0.0
                    if rates:
                        cost = (
                            in_tok * rates.get("input", 0.0)
                            + out_tok * rates.get("output", 0.0)
                            + cached_tok * rates.get("cache_read", 0.0)
                        ) / 1e6

                    token_map[idx] = {
                        "model": model_name,
                        "input": in_tok,
                        "output": out_tok,
                        "cached": cached_tok,
                        "reasoning": reason_tok,
                        "total": in_tok + out_tok + cached_tok,
                        "cost": cost,
                    }
        conn.close()
    except Exception:
        pass
    return token_map


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 antigravity_export.py <input.db or input.jsonl> <output.html>")
        sys.exit(1)

    input_path = Path(sys.argv[1]).resolve()
    output_path = Path(sys.argv[2]).resolve()

    # Determine paths for db, transcript, and summaries
    if input_path.suffix == ".jsonl":
        transcript_path = input_path
        cid = input_path.parent.parent.parent.name
        antigravity_dir = input_path.parent.parent.parent.parent.parent
        db_path = antigravity_dir / "conversations" / f"{cid}.db"
        summaries_path = antigravity_dir / "conversation_summaries.db"
    else:
        db_path = input_path
        cid = input_path.stem
        antigravity_dir = input_path.parent.parent
        summaries_path = antigravity_dir / "conversation_summaries.db"
        brain_base = antigravity_dir / "brain" / cid / ".system_generated" / "logs"
        transcript_path = brain_base / "transcript_full.jsonl"
        if not transcript_path.exists():
            transcript_path = brain_base / "transcript.jsonl"

    session_title = ""
    workspace = ""
    start_time = ""

    if summaries_path.exists():
        try:
            conn = sqlite3.connect(f"file:{summaries_path}?mode=ro", uri=True)
            cur = conn.cursor()
            cur.execute(
                "SELECT title, preview, workspace_uris, last_modified_time FROM conversation_summaries WHERE conversation_id = ?",
                (cid,),
            )
            row = cur.fetchone()
            conn.close()
            if row:
                session_title = row[0] or row[1]
                start_time = row[3]
                try:
                    uris = json.loads(row[2])
                    for u in uris:
                        if u.startswith("file://"):
                            workspace = unquote(urlparse(u).path)
                            break
                except Exception:
                    pass
        except Exception:
            pass

    # Read token breakdown per turn from DB
    step_tokens = extract_step_token_map(db_path)

    # Read transcript entries
    steps = []
    if transcript_path.exists():
        with open(transcript_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    steps.append(data)
                except Exception:
                    continue

    tot_in = sum(s.get("input", 0) for s in step_tokens.values())
    tot_out = sum(s.get("output", 0) for s in step_tokens.values())
    tot_cached = sum(s.get("cached", 0) for s in step_tokens.values())
    tot_reason = sum(s.get("reasoning", 0) for s in step_tokens.values())
    tot_tokens = tot_in + tot_out + tot_cached
    tot_cost = sum(s.get("cost", 0.0) for s in step_tokens.values())

    html_parts = [
        "<!DOCTYPE html>",
        "<html lang=\"en\">",
        "<head>",
        '<meta charset="utf-8">',
        f"<title>Antigravity Session - {escape(session_title or cid)}</title>",
        f"<style>{CSS}</style>",
        "</head>",
        "<body>",
        '<div class="header">',
        f"<h1>Antigravity Session: {escape(session_title or cid)}</h1>",
        '<div class="meta">',
        f"<span><strong>Conversation ID:</strong> {escape(cid)}</span>",
        f"<span><strong>Workspace:</strong> {escape(workspace or 'unknown')}</span>",
        f"<span><strong>Tokens:</strong> {tot_tokens:,} (In: {tot_in:,}, Out: {tot_out:,}, Cached: {tot_cached:,}, Reasoning: {tot_reason:,})</span>",
        f"<span><strong>Cost:</strong> ${tot_cost:.4f}</span>",
        f"<span><strong>Date:</strong> {escape(start_time or 'N/A')}</span>",
        "</div>",
        "</div>",
    ]

    for step in steps:
        step_idx = step.get("step_index", 0)
        source = step.get("source", "")
        stype = step.get("type", "")
        ts = step.get("created_at", "")
        content = step.get("content", "")
        thinking = step.get("thinking", "")
        tool_calls = step.get("tool_calls", [])

        if stype == "USER_INPUT":
            html_parts.append('<div class="message msg-user">')
            html_parts.append(
                f'<div class="role-label">User <span style="float:right; font-weight:normal; opacity:0.6;">{escape(ts)}</span></div>'
            )
            html_parts.append('<div class="content">')
            html_parts.append(render_text(content))
            html_parts.append("</div></div>")

        elif stype == "PLANNER_RESPONSE":
            html_parts.append('<div class="message msg-assistant">')
            html_parts.append(
                f'<div class="role-label">Antigravity (Step {step_idx}) <span style="float:right; font-weight:normal; opacity:0.6;">{escape(ts)}</span></div>'
            )
            html_parts.append('<div class="content">')

            if thinking:
                html_parts.append('<details class="thoughts"><summary>Thinking...</summary>')
                html_parts.append(f'<div class="thought-body">{escape(thinking)}</div>')
                html_parts.append("</details>")

            if content:
                html_parts.append(render_text(content))

            if tool_calls:
                for tc in tool_calls:
                    tc_name = tc.get("name", "tool")
                    tc_args = tc.get("args", {})
                    html_parts.append('<div class="tool-call">')
                    html_parts.append(f'<div class="tool-header">Tool Call: {escape(tc_name)}</div>')
                    html_parts.append(
                        f'<div class="tool-body">{escape(json.dumps(tc_args, indent=2))}</div>'
                    )
                    html_parts.append("</div>")

            # Append turn token breakdown if available
            tok = step_tokens.get(step_idx)
            if tok:
                html_parts.append(
                    f'<div class="token-footer">'
                    f'Model: {escape(tok["model"])} | '
                    f'Tokens: {tok["total"]:,} (In: {tok["input"]:,}, Out: {tok["output"]:,}, Cached: {tok["cached"]:,}, Reasoning: {tok["reasoning"]:,}) | '
                    f'Cost: ${tok["cost"]:.4f}'
                    f'</div>'
                )

            html_parts.append("</div></div>")

        elif stype == "GENERIC" and content:
            # Generic tool execution result
            html_parts.append('<div class="message msg-generic">')
            html_parts.append(
                f'<div class="role-label">Tool Execution Output (Step {step_idx}) <span style="float:right; font-weight:normal; opacity:0.6;">{escape(ts)}</span></div>'
            )
            html_parts.append('<div class="content">')
            preview_content = content
            if len(preview_content) > 10000:
                preview_content = preview_content[:10000] + f"\n... ({len(content) - 10000} chars truncated)"
            html_parts.append(f'<pre style="font-family: monospace; font-size: 0.85em; overflow-x: auto; white-space: pre-wrap;"><code>{escape(preview_content)}</code></pre>')
            html_parts.append("</div></div>")

    html_parts.append("</body></html>")

    output_path.write_text("\n".join(html_parts), encoding="utf-8")
    print(f"Exported to {output_path}")


if __name__ == "__main__":
    main()

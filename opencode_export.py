#!/usr/bin/env python3
"""Export an OpenCode session from its database to a styled HTML transcript.

Usage: python3 opencode_export.py <session_id> <output.html>

OpenCode keeps every session in one SQLite database, so the session is looked
up by id rather than read from a per-session file.
"""

import html
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = Path.home() / ".local" / "share" / "opencode" / "opencode.db"

# Tool output and file bodies can run to megabytes; keep the transcript readable.
MAX_OUTPUT_CHARS = 4000


def escape(text) -> str:
    return html.escape("" if text is None else str(text))


def format_time(value) -> str:
    """Render an epoch-millisecond timestamp as a readable UTC string."""
    if not value:
        return ""
    try:
        return datetime.fromtimestamp(
            int(value) / 1000, tz=timezone.utc
        ).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def render_text(text: str) -> str:
    """Render text with basic markdown-ish formatting.

    The text is escaped once up front so that every insertion below can splice
    in capture groups verbatim — escaping each group separately would leave
    the surrounding markup double-escaped, and leaving them raw would let a
    message that mentions a tag inject markup into the transcript.
    """
    if not text:
        return ""

    escaped = escape(text)

    # Fenced blocks are pulled out before any inline formatting runs, otherwise
    # a backtick inside a code block would be rewritten into an inline-code tag
    # and break the nesting.
    rendered = []
    for part in re.split(r"(```\w*\n.*?```)", escaped, flags=re.DOTALL):
        fenced = re.fullmatch(r"```(\w*)\n(.*?)```", part, flags=re.DOTALL)
        if fenced:
            lang, body = fenced.group(1), fenced.group(2)
            label = f'<span class="code-lang">{lang}</span>' if lang else ""
            rendered.append(
                f'<div class="code-block">{label}<pre><code>{body}</code></pre></div>'
            )
            continue

        part = re.sub(r"`([^`]+)`", r'<code class="inline-code">\1</code>', part)
        part = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", part)
        rendered.append(part.replace("\n", "<br>\n"))

    return "".join(rendered)


def truncate(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return text[:MAX_OUTPUT_CHARS] + f"\n\n... [truncated, {len(text):,} chars total]"


def tool_display_name(part: dict) -> str:
    """The tool a part belongs to.

    OpenCode files input it could not parse under a synthetic "invalid" tool and
    keeps the real name next to the parse error, so show the real one.
    """
    name = part.get("tool")
    if name and name != "invalid":
        return name
    original = ((part.get("state") or {}).get("input") or {}).get("tool")
    return original or name or "unknown"


def render_tool(part: dict) -> str:
    """Render one tool part as a call with its input, output and error."""
    name = tool_display_name(part)
    state = part.get("state") or {}
    status = state.get("status") or "unknown"

    body = [f'<div class="tool-call">']
    label = escape(name)
    if name != (part.get("tool") or name):
        label += ' <span class="tool-status">(invalid input)</span>'
    elif status not in ("completed", "unknown"):
        label += f' <span class="tool-status">{escape(status)}</span>'
    body.append(f'<div class="tool-header">{label}</div>')

    tool_input = state.get("input")
    if tool_input:
        if isinstance(tool_input, str):
            rendered_input = escape(tool_input)
        else:
            rendered_input = escape(
                truncate(json.dumps(tool_input, indent=2, ensure_ascii=False))
            )
        body.append(f'<div class="tool-body">{rendered_input}</div>')

    output = state.get("output")
    error = state.get("error")
    if output or error:
        body.append('<div class="tool-result">')
        body.append('<div class="res-header">Result</div>')
        if error:
            body.append(f'<div class="res-body error">{escape(truncate(str(error)))}</div>')
        else:
            body.append(
                f'<div class="res-body">{escape(truncate(str(output)))}</div>'
            )
        body.append("</div>")

    body.append("</div>")
    return "".join(body)


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
    padding: 20px;
    margin-bottom: 20px;
}
.header h1 { font-size: 1.3em; margin-bottom: 10px; }
.header .meta { color: var(--text-secondary); font-size: 0.85em; }
.header .meta span { margin-right: 16px; display: inline-block; }
.message {
    border: 1px solid var(--border-color);
    border-radius: 8px;
    margin-bottom: 16px;
    overflow: hidden;
}
.message .role-label {
    display: inline-block;
    padding: 4px 12px;
    font-size: 0.8em;
    font-weight: 600;
    border-bottom: 1px solid var(--border-color);
}
.message .content { padding: 12px 16px; font-size: 0.9em; }
.msg-user .role-label { background: rgba(88, 166, 255, 0.15); color: var(--accent-blue); }
.msg-assistant .role-label { background: rgba(63, 185, 80, 0.15); color: var(--accent-green); }
.msg-user .content, .msg-assistant .content { background: var(--bg-secondary); }
.tool-call {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    margin: 8px 0;
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
.tool-status { color: var(--accent-red); font-weight: 500; }
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
    background: rgba(0,0,0,0.2);
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
    word-break: break-all;
}
.res-body.error { color: var(--accent-red); }
.code-block { background: #000; border-radius: 6px; margin: 12px 0; border: 1px solid var(--border-color); }
.code-lang { display: block; padding: 4px 12px; font-size: 0.7em; color: var(--text-secondary); border-bottom: 1px solid var(--border-color); }
.code-block pre { padding: 12px; overflow-x: auto; }
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
    color: var(--text-secondary);
    background: var(--bg-tertiary);
    cursor: pointer;
}
details.thoughts .thought-item {
    padding: 8px 12px;
    border-top: 1px solid var(--border-color);
    font-size: 0.85em;
    white-space: pre-wrap;
}
"""


def load_session(session_id: str, db_path: Path = DB_PATH) -> tuple[dict, list]:
    """Load one session's metadata, messages and tool parts from the database."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        row = conn.execute(
            "SELECT directory, title, agent, model, time_created, time_updated"
            " FROM session WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise SystemExit(f"No OpenCode session found with id {session_id}")

        info = {
            "directory": row[0],
            "title": row[1],
            "agent": row[2],
            "model": row[3],
            "created": row[4],
            "updated": row[5],
        }

        messages = []
        for message_id, created, data in conn.execute(
            "SELECT id, time_created, data FROM message WHERE session_id = ?"
            " ORDER BY time_created",
            (session_id,),
        ):
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                # The payload carries no id of its own; the column does.
                payload["_id"] = message_id
                payload["_created"] = created
                messages.append(payload)

        # Parts arrive keyed by message, and their ordering is not guaranteed,
        # so sort each message's parts by creation time before rendering.
        parts_by_message: dict[str, list[dict]] = {}
        for message_id, created, data in conn.execute(
            "SELECT message_id, time_created, data FROM part WHERE session_id = ?"
            " ORDER BY time_created",
            (session_id,),
        ):
            try:
                payload = json.loads(data)
            except (json.JSONDecodeError, TypeError):
                continue
            if isinstance(payload, dict):
                parts_by_message.setdefault(message_id, []).append(payload)

        for message in messages:
            message["_parts"] = parts_by_message.get(message.get("_id"), [])
        return info, messages
    finally:
        conn.close()


def render_message(message: dict) -> str:
    """Render one message with its text, reasoning and tool calls."""
    role = message.get("role") or "unknown"
    if role not in ("user", "assistant"):
        return ""

    reasoning, text, tools = [], [], []
    for part in message.get("_parts") or []:
        part_type = part.get("type")
        if part_type == "text":
            body = (part.get("text") or "").strip()
            if body:
                text.append(body)
        elif part_type == "reasoning":
            body = (part.get("text") or "").strip()
            if body:
                reasoning.append(body)
        elif part_type == "tool":
            tools.append(render_tool(part))

    label = "User" if role == "user" else "Assistant"
    body = []
    if reasoning:
        items = "".join(
            f'<div class="thought-item">{render_text(item)}</div>'
            for item in reasoning
        )
        body.append(
            f'<details class="thoughts"><summary>Reasoning '
            f"({len(reasoning)})</summary>{items}</details>"
        )
    if text:
        body.append(render_text("\n\n".join(text)))
    body.extend(tools)

    if not body:
        return ""
    content = "".join(body)
    return (
        f'<div class="message msg-{role}">'
        f'<span class="role-label">{label}</span>'
        f'<div class="content">{content}</div></div>'
    )


def build_html(info: dict, messages: list) -> str:
    title = info.get("title") or "OpenCode Session"
    model = info.get("model")
    try:
        model_name = (json.loads(model) or {}).get("id", "") if model else ""
    except (json.JSONDecodeError, TypeError):
        model_name = str(model or "")

    body = "".join(filter(None, (render_message(m) for m in messages)))

    meta = [
        f"<span><strong>Session ID:</strong> {escape(info.get('id'))}</span>",
        f"<span><strong>Workspace:</strong> {escape(info.get('directory'))}</span>",
    ]
    if info.get("agent"):
        meta.append(f"<span><strong>Agent:</strong> {escape(info['agent'])}</span>")
    if model_name:
        meta.append(f"<span><strong>Model:</strong> {escape(model_name)}</span>")
    meta.append(
        f"<span><strong>Created:</strong> {escape(format_time(info.get('created')))}</span>"
    )

    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>OpenCode Session - {escape(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="header">
<h1>OpenCode Session: {escape(title)}</h1>
<div class="meta">{"".join(meta)}</div>
</div>
{body}
</body>
</html>
"""


def main():
    if len(sys.argv) < 3:
        print("Usage: python3 opencode_export.py <session_id> <output.html>")
        sys.exit(1)

    session_id = sys.argv[1]
    output_path = Path(sys.argv[2])

    info, messages = load_session(session_id)
    info["id"] = session_id
    output_path.write_text(build_html(info, messages), encoding="utf-8")


if __name__ == "__main__":
    main()
#!/usr/bin/env python3
"""Export a Codex CLI JSONL session file to a styled HTML transcript.

Usage: python3 codex_export.py <input.jsonl> <output.html>
"""

import html
import json
import re
import sys
from pathlib import Path


def escape(text: str) -> str:
    return html.escape(text)


def render_text(text: str) -> str:
    """Render text with basic markdown-ish formatting."""
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


def truncate_text(text: str, max_len: int = 2000) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"\n... ({len(text) - max_len} chars truncated)"


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
a { color: var(--accent-blue); text-decoration: none; }
a:hover { text-decoration: underline; }
.header {
    background: var(--bg-secondary);
    border: 1px solid var(--border-color);
    border-radius: 8px;
    padding: 16px 20px;
    margin-bottom: 20px;
}
.header h1 { font-size: 1.2em; margin-bottom: 8px; }
.header .meta { color: var(--text-secondary); font-size: 0.85em; }
.header .meta span { margin-right: 16px; }
.message {
    margin-bottom: 12px;
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
.tool-call .tool-body {
    padding: 8px 12px;
    font-size: 0.82em;
    font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
    white-space: pre-wrap;
    word-break: break-all;
    color: var(--text-secondary);
    max-height: 300px;
    overflow-y: auto;
}
.tool-result {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-color);
    border-radius: 6px;
    margin: 8px 0;
    overflow: hidden;
}
.tool-result .tool-header {
    padding: 6px 12px;
    font-size: 0.8em;
    font-weight: 600;
    color: var(--accent-yellow);
    background: rgba(210, 153, 34, 0.1);
    border-bottom: 1px solid var(--border-color);
}
.tool-result .tool-body {
    padding: 8px 12px;
    font-size: 0.82em;
    font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
    white-space: pre-wrap;
    word-break: break-all;
    color: var(--text-secondary);
    max-height: 300px;
    overflow-y: auto;
}
details.reasoning {
    margin: 8px 0;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    overflow: hidden;
}
details.reasoning summary {
    padding: 6px 12px;
    font-size: 0.8em;
    font-weight: 600;
    color: var(--text-secondary);
    background: var(--bg-tertiary);
    cursor: pointer;
}
details.reasoning .reasoning-body {
    padding: 8px 12px;
    font-size: 0.82em;
    font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--text-secondary);
    background: var(--bg-secondary);
    max-height: 400px;
    overflow-y: auto;
}
.code-block {
    background: var(--bg-tertiary);
    border: 1px solid var(--border-color);
    border-radius: 4px;
    margin: 8px 0;
    overflow-x: auto;
}
.code-block .code-lang {
    display: block;
    padding: 4px 10px;
    font-size: 0.75em;
    color: var(--text-secondary);
    border-bottom: 1px solid var(--border-color);
}
.code-block pre {
    padding: 10px;
    margin: 0;
    font-size: 0.85em;
    font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
    white-space: pre-wrap;
    word-break: break-word;
}
code.inline-code {
    background: var(--bg-tertiary);
    padding: 2px 5px;
    border-radius: 3px;
    font-size: 0.9em;
    font-family: 'SF Mono', Monaco, 'Cascadia Code', monospace;
}
.back-link {
    margin-top: 20px;
    padding: 10px 0;
    text-align: center;
    color: var(--text-secondary);
    font-size: 0.85em;
}
"""


def export_session(input_path: str) -> str:
    """Parse a Codex JSONL session and return HTML."""
    records = []
    session_id = ""
    cwd = ""
    model = ""
    cli_version = ""
    model_provider = ""
    first_ts = ""
    last_ts = ""
    git_info = {}

    with open(input_path, "r") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue

            rtype = data.get("type", "")
            ts = data.get("timestamp", "")
            if ts:
                if not first_ts:
                    first_ts = ts
                last_ts = ts

            payload = data.get("payload", {})

            if rtype == "session_meta":
                session_id = payload.get("id", "")
                cwd = payload.get("cwd", "")
                cli_version = payload.get("cli_version", "")
                model_provider = payload.get("model_provider", "")
                git_info = payload.get("git", {})
                continue

            if rtype == "turn_context":
                if not model and payload.get("model"):
                    model = payload["model"]
                continue

            # Skip token_count events (not conversation content)
            if rtype == "event_msg" and payload.get("type") == "token_count":
                continue

            records.append(data)

    # Build HTML
    parts = []
    parts.append("<!DOCTYPE html>\n<html lang='en'>\n<head>")
    parts.append("<meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    title = f"Codex Session {session_id[:8]}..." if session_id else "Codex Session"
    parts.append(f"<title>{escape(title)}</title>")
    parts.append(f"<style>{CSS}</style>")
    parts.append("</head>\n<body>")

    # Header
    parts.append('<div class="header">')
    parts.append("<h1>Codex CLI Session</h1>")
    parts.append('<div class="meta">')
    if session_id:
        parts.append(f"<span>Session: {escape(session_id)}</span>")
    if cwd:
        parts.append(f"<span>CWD: {escape(cwd)}</span>")
    if model:
        parts.append(f"<span>Model: {escape(model)}</span>")
    if model_provider:
        parts.append(f"<span>Provider: {escape(model_provider)}</span>")
    if cli_version:
        parts.append(f"<span>CLI: {escape(cli_version)}</span>")
    parts.append("</div>")
    meta2 = []
    if first_ts:
        meta2.append(f"<span>Started: {escape(first_ts)}</span>")
    if last_ts:
        meta2.append(f"<span>Last: {escape(last_ts)}</span>")
    if git_info.get("branch"):
        meta2.append(f"<span>Branch: {escape(git_info['branch'])}</span>")
    if meta2:
        parts.append('<div class="meta">')
        parts.extend(meta2)
        parts.append("</div>")
    parts.append("</div>")

    # Track which event_msg agent_messages we've already shown via response_item
    # to avoid duplicates — event_msg and response_item can both carry the same text
    seen_agent_messages = set()

    # Messages
    for rec in records:
        rtype = rec.get("type", "")
        payload = rec.get("payload", {})

        if rtype == "response_item":
            ptype = payload.get("type", "")
            role = payload.get("role", "")

            if ptype == "message" and role == "user":
                # User message — extract input_text blocks, skip developer/system
                content_blocks = payload.get("content", [])
                for block in content_blocks:
                    if block.get("type") == "input_text":
                        text = block.get("text", "")
                        # Skip environment context / system blocks
                        if text.strip().startswith("<environment_context>"):
                            continue
                        if text.strip():
                            parts.append('<div class="message msg-user">')
                            parts.append('<div class="role-label">User</div>')
                            parts.append(
                                f'<div class="content">{render_text(escape(text))}</div>'
                            )
                            parts.append("</div>")

            elif ptype == "message" and role == "assistant":
                # Assistant message
                content_blocks = payload.get("content", [])
                text_parts = []
                for block in content_blocks:
                    if block.get("type") == "output_text":
                        text_parts.append(block.get("text", ""))
                full_text = "\n".join(text_parts)
                if full_text.strip():
                    seen_agent_messages.add(full_text.strip())
                    parts.append('<div class="message msg-assistant">')
                    parts.append('<div class="role-label">Assistant</div>')
                    parts.append(
                        f'<div class="content">{render_text(escape(full_text))}</div>'
                    )
                    parts.append("</div>")

            elif ptype == "reasoning":
                # Reasoning block — show summary as collapsible
                summary_blocks = payload.get("summary", [])
                summary_text = ""
                for sb in summary_blocks:
                    if sb.get("type") == "summary_text":
                        summary_text += sb.get("text", "")
                if summary_text.strip():
                    preview = summary_text[:80].replace("\n", " ")
                    parts.append('<details class="reasoning">')
                    parts.append(f"<summary>Reasoning: {escape(preview)}...</summary>")
                    parts.append(
                        f'<div class="reasoning-body">{render_text(escape(summary_text))}</div>'
                    )
                    parts.append("</details>")

            elif ptype == "function_call":
                # Tool call
                name = payload.get("name", "unknown")
                args = payload.get("arguments", "")
                if isinstance(args, str) and len(args) > 300:
                    args = args[:300] + "..."
                parts.append('<div class="tool-call">')
                parts.append(f'<div class="tool-header">Tool: {escape(name)}</div>')
                parts.append(f'<div class="tool-body">{escape(args)}</div>')
                parts.append("</div>")

            elif ptype == "function_call_output":
                # Tool result
                output = payload.get("output", "")
                output = truncate_text(str(output))
                parts.append('<div class="tool-result">')
                parts.append('<div class="tool-header">Tool Result</div>')
                parts.append(f'<div class="tool-body">{escape(output)}</div>')
                parts.append("</div>")

        elif rtype == "event_msg":
            ptype = payload.get("type", "")

            if ptype == "agent_message":
                msg_text = payload.get("message", "")
                # Avoid duplicates if we already showed this via response_item
                if msg_text.strip() and msg_text.strip() not in seen_agent_messages:
                    seen_agent_messages.add(msg_text.strip())
                    parts.append('<div class="message msg-assistant">')
                    parts.append('<div class="role-label">Assistant</div>')
                    parts.append(
                        f'<div class="content">{render_text(escape(msg_text))}</div>'
                    )
                    parts.append("</div>")

            elif ptype == "agent_reasoning":
                reasoning_text = payload.get("text", "")
                if reasoning_text.strip():
                    preview = reasoning_text[:80].replace("\n", " ")
                    parts.append('<details class="reasoning">')
                    parts.append(f"<summary>Reasoning: {escape(preview)}...</summary>")
                    parts.append(
                        f'<div class="reasoning-body">{render_text(escape(reasoning_text))}</div>'
                    )
                    parts.append("</details>")

            elif ptype == "user_message":
                # User message from event_msg
                msg_text = payload.get("message", "")
                if msg_text.strip():
                    parts.append('<div class="message msg-user">')
                    parts.append('<div class="role-label">User</div>')
                    parts.append(
                        f'<div class="content">{render_text(escape(msg_text))}</div>'
                    )
                    parts.append("</div>")

    # Footer
    parts.append('<div class="back-link">')
    parts.append('<a href="/">Back to Dashboard</a>')
    parts.append("</div>")

    parts.append("</body>\n</html>")
    return "\n".join(parts)


def main():
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.jsonl> <output.html>", file=sys.stderr)
        sys.exit(1)

    input_path = sys.argv[1]
    output_path = sys.argv[2]

    if not Path(input_path).exists():
        print(f"Error: input file not found: {input_path}", file=sys.stderr)
        sys.exit(1)

    html_content = export_session(input_path)
    Path(output_path).write_text(html_content)


if __name__ == "__main__":
    main()

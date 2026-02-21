#!/usr/bin/env python3
"""Export a Claude Code JSONL session file to a styled HTML transcript.

Usage: python3 claude_export.py <input.jsonl> <output.html>
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
    # Code blocks: ```lang\n...\n```
    def replace_code_block(m):
        lang = escape(m.group(1) or "")
        code = escape(m.group(2))
        label = f'<span class="code-lang">{lang}</span>' if lang else ""
        return f'<div class="code-block">{label}<pre><code>{code}</code></pre></div>'

    text = re.sub(
        r"```(\w*)\n(.*?)```", replace_code_block, text, flags=re.DOTALL
    )
    # Inline code
    text = re.sub(r"`([^`]+)`", r'<code class="inline-code">\1</code>', text)
    # Bold
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    # Convert newlines to <br> outside of code blocks
    parts = re.split(r'(<div class="code-block">.*?</div>)', text, flags=re.DOTALL)
    result = []
    for part in parts:
        if part.startswith('<div class="code-block">'):
            result.append(part)
        else:
            result.append(part.replace("\n", "<br>\n"))
    return "".join(result)


def summarize_tool_input(name: str, inp: dict) -> str:
    """Create a short summary of tool input."""
    if name == "Bash":
        cmd = inp.get("command", "")
        if len(cmd) > 200:
            cmd = cmd[:200] + "..."
        return escape(cmd)
    if name == "Read":
        return escape(inp.get("file_path", str(inp)))
    if name in ("Write", "Edit"):
        fp = inp.get("file_path", "")
        return escape(fp)
    if name in ("Glob", "Grep"):
        pattern = inp.get("pattern", "")
        path = inp.get("path", "")
        s = escape(pattern)
        if path:
            s += f" in {escape(path)}"
        return s
    if name == "Task":
        return escape(inp.get("description", str(inp)[:200]))
    if name in ("WebFetch", "WebSearch"):
        return escape(inp.get("url", inp.get("query", str(inp)[:200])))
    # Generic fallback
    s = json.dumps(inp, ensure_ascii=False)
    if len(s) > 300:
        s = s[:300] + "..."
    return escape(s)


def truncate_tool_result(text: str, max_len: int = 2000) -> str:
    """Truncate long tool results."""
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
.tool-result.error .tool-header { color: var(--accent-red); background: rgba(248, 81, 73, 0.1); }
details.thinking {
    margin: 8px 0;
    border: 1px solid var(--border-color);
    border-radius: 6px;
    overflow: hidden;
}
details.thinking summary {
    padding: 6px 12px;
    font-size: 0.8em;
    font-weight: 600;
    color: var(--text-secondary);
    background: var(--bg-tertiary);
    cursor: pointer;
}
details.thinking .thinking-body {
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
    """Parse a Claude JSONL session and return HTML."""
    records = []
    session_id = ""
    cwd = ""
    model = ""
    first_ts = ""
    last_ts = ""
    version = ""

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

            # Skip non-conversation records
            if rtype in ("progress", "file-history-snapshot", "summary"):
                continue

            # Extract metadata from first relevant record
            if not session_id and data.get("sessionId"):
                session_id = data["sessionId"]
            if not cwd and data.get("cwd"):
                cwd = data["cwd"]
            if not version and data.get("version"):
                version = data["version"]

            ts = data.get("timestamp", "")
            if ts:
                if not first_ts:
                    first_ts = ts
                last_ts = ts

            msg = data.get("message", {})

            if rtype == "assistant":
                # Skip synthetic messages
                if msg.get("model") == "<synthetic>":
                    continue
                if not model and msg.get("model"):
                    model = msg["model"]

            records.append(data)

    # Build HTML
    parts = []
    parts.append("<!DOCTYPE html>\n<html lang='en'>\n<head>")
    parts.append("<meta charset='utf-8'>")
    parts.append("<meta name='viewport' content='width=device-width, initial-scale=1'>")
    title = f"Claude Session {session_id[:8]}..." if session_id else "Claude Session"
    parts.append(f"<title>{escape(title)}</title>")
    parts.append(f"<style>{CSS}</style>")
    parts.append("</head>\n<body>")

    # Header
    parts.append('<div class="header">')
    parts.append(f"<h1>Claude Code Session</h1>")
    parts.append('<div class="meta">')
    if session_id:
        parts.append(f"<span>Session: {escape(session_id)}</span>")
    if cwd:
        parts.append(f"<span>CWD: {escape(cwd)}</span>")
    if model:
        parts.append(f"<span>Model: {escape(model)}</span>")
    if version:
        parts.append(f"<span>Version: {escape(version)}</span>")
    parts.append("</div>")
    if first_ts or last_ts:
        parts.append('<div class="meta">')
        if first_ts:
            parts.append(f"<span>Started: {escape(first_ts)}</span>")
        if last_ts:
            parts.append(f"<span>Last: {escape(last_ts)}</span>")
        parts.append("</div>")
    parts.append("</div>")

    # Messages
    for rec in records:
        rtype = rec.get("type", "")
        msg = rec.get("message", {})

        if rtype == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                # Simple user message
                parts.append('<div class="message msg-user">')
                parts.append('<div class="role-label">User</div>')
                parts.append(f'<div class="content">{render_text(escape(content))}</div>')
                parts.append("</div>")
            elif isinstance(content, list):
                # Tool results
                for item in content:
                    if item.get("type") == "tool_result":
                        tool_id = item.get("tool_use_id", "")
                        is_error = item.get("is_error", False)
                        result_content = item.get("content", "")
                        if isinstance(result_content, list):
                            # Content can be a list of text blocks
                            text_parts = []
                            for block in result_content:
                                if isinstance(block, dict) and block.get("type") == "text":
                                    text_parts.append(block.get("text", ""))
                                elif isinstance(block, str):
                                    text_parts.append(block)
                            result_content = "\n".join(text_parts)
                        result_content = truncate_tool_result(str(result_content))
                        err_class = " error" if is_error else ""
                        parts.append(f'<div class="tool-result{err_class}">')
                        label = "Tool Error" if is_error else "Tool Result"
                        parts.append(f'<div class="tool-header">{label}</div>')
                        parts.append(f'<div class="tool-body">{escape(result_content)}</div>')
                        parts.append("</div>")

        elif rtype == "assistant":
            if msg.get("model") == "<synthetic>":
                continue
            content = msg.get("content", [])
            if not isinstance(content, list):
                continue

            has_text = any(
                c.get("type") in ("text", "tool_use", "thinking") for c in content
            )
            if not has_text:
                continue

            parts.append('<div class="message msg-assistant">')
            parts.append('<div class="role-label">Assistant</div>')
            parts.append('<div class="content">')

            for block in content:
                btype = block.get("type", "")
                if btype == "text":
                    text = block.get("text", "")
                    if text.strip():
                        parts.append(f"<div>{render_text(escape(text))}</div>")
                elif btype == "thinking":
                    thinking = block.get("thinking", "")
                    if thinking.strip():
                        preview = thinking[:80].replace("\n", " ")
                        parts.append('<details class="thinking">')
                        parts.append(
                            f"<summary>Thinking: {escape(preview)}...</summary>"
                        )
                        parts.append(
                            f'<div class="thinking-body">{escape(thinking)}</div>'
                        )
                        parts.append("</details>")
                elif btype == "tool_use":
                    name = block.get("name", "unknown")
                    inp = block.get("input", {})
                    summary = summarize_tool_input(name, inp)
                    parts.append('<div class="tool-call">')
                    parts.append(
                        f'<div class="tool-header">Tool: {escape(name)}</div>'
                    )
                    parts.append(f'<div class="tool-body">{summary}</div>')
                    parts.append("</div>")

            parts.append("</div>")
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

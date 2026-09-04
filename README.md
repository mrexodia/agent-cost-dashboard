# Agent Cost Dashboard

Web dashboard to monitor API costs for [Pi](https://github.com/mariozechner/pi-coding-agent), [Oh My Pi](https://github.com/can1357/oh-my-pi), [Claude Code](https://github.com/anthropics/claude-code), [Codex CLI](https://github.com/openai/codex), and [Gemini CLI](https://github.com/google-gemini/gemini-cli) coding agents.

No external dependencies — pure Python stdlib. The dashboard reads the local session logs directly and recalculates filtered views in the browser without a database or external service.

![Main dashboard showing global stats, and daily spending](screenshots/dashboard-overview.png)

## Features

### Global Statistics

Track spending and activity across all projects, profiles, and sessions:

- Total cost, projects, sessions, and LLM calls
- Total tokens broken down by input, output, cache-read, cache-write, and reasoning counts
- Detailed token usage across all models where source data exposes it
- LLM time versus tool execution time
- Weighted average tokens per second across all API calls
- Automatic duplicate-session protection when the same session exists in multiple Pi profiles

### Date Filtering

Filter the complete dashboard by a custom date/time range or quick presets:

- **All time**
- **1h**, **2h**, **4h**, **8h**, **12h**, **24h**, and **7d**
- Custom **From** and **To** date/time values

The selected range updates the summary cards, daily spending chart, projects, models, tools, and session browser. Sessions that overlap the selected range are included. Use **All time** or **Clear** to reset the filter.

### Pi Profile Discovery

All Pi profiles under `~/.pi/<profile>/sessions` are discovered automatically, including custom profiles such as `agent-local`, `agent-ollama`, or `agent-harness`. No configuration change is needed when a profile is added. The default `~/.pi/agent/sessions` location remains supported.

### Daily Spending Chart

Timeline of API costs over time.

### Model Breakdown

Costs broken down by AI model (Claude, Gemini, GPT-5, O3, O4, GLM, and local models):

- Messages, input/output/cache-read/cache-write/reasoning token usage, and cost per model
- Average tokens per second
- Filter-aware totals that recalculate when a date range is selected

![Model Stats](screenshots/model-stats.png)

### Tool Usage

Track which tools your agent uses most:

- Call counts and execution time per tool
- Error rates

![Tool Stats](screenshots/tool-stats.png)

### Project View

All projects with expandable details:

- Per-project cost, model usage, tool usage, and session history
- Sortable by cost, tokens, LLM time, or date

![Projects](screenshots/projects.png)

### Session Browser

Browse every session with full details:

- Copy command to resume session to the clipboard
- Full transcript export (Pi via `pi --export`, Claude and Codex via built-in exporters)
- Session duration, LLM time, tool time, token usage, and tokens/s
- Subagent session support with expandable grouping
- Sortable by date, duration, cost, tokens, and more
- Date-range filtering for quickly isolating recent activity

![Sessions](screenshots/sessions.png)

## Installation

Requires **Python 3.12+**.

```bash
git clone https://github.com/user/pi-cost-dashboard
cd pi-cost-dashboard
```

## Usage

```bash
# Start the dashboard (defaults to localhost:8753)
./cost_dashboard.py

# Use a custom port
./cost_dashboard.py --port 3000

# Bind to all interfaces (accessible from network)
./cost_dashboard.py --host 0.0.0.0

# Custom host and port
./cost_dashboard.py -H 0.0.0.0 -p 3000
```

On Windows, you can also double-click `start.bat`.

Then open <http://localhost:8753> in your browser.

## Session Directories

The dashboard automatically reads session data from the following locations. Pi is scanned dynamically, so every profile with a `sessions` directory is included:

| Agent | Directory |
|---|---|
| Pi | `~/.pi/<profile>/sessions` (all profiles, including `agent`) |
| Oh My Pi | `~/.omp/agent/sessions` |
| Claude Code | `~/.claude/projects` |
| Codex CLI | `~/.codex/sessions` |
| Gemini CLI | `~/.gemini/tmp` |

## CLI Utilities

### claude_cost.py / gemini_cost.py

Calculate API costs for agent sessions:

```bash
python claude_cost.py /path/to/sessions
python gemini_cost.py ~/.gemini/tmp/project/chats
```

### claude_export.py / codex_export.py / gemini_export.py

Export a session JSONL file to a styled HTML transcript:

```bash
python claude_export.py input.jsonl output.html
python codex_export.py input.jsonl output.html
python gemini_export.py input.jsonl output.html
```

## Pricing and Usage Accuracy

Costs are calculated using pricing reported by the agent. For models that don't report costs (e.g., Gemini via Google Cloud), estimated pricing is applied based on public API rates. Supported model families: Claude, Gemini, GPT-5, O3/O4, GLM.

The parser accepts Pi and common API usage field formats, including camelCase, snake_case, OpenAI-style, and Gemini-style counters. It also understands Pi's numeric millisecond timestamps.

Some local Pi providers persist a zeroed usage object because their backend does not expose token counts. For those sessions the dashboard estimates tokens from the saved transcript and derives tokens/s from estimated output tokens and response time. Estimated values are marked with `~`, are included in filtered aggregates, and are never mixed into a session that already contains provider-reported usage. Estimated values are not used to invent a cost.

## Credits

- **[Mario Zechner](https://github.com/mariozechner)** - For Pi and its session export feature
- **[can1357](https://github.com/can1357)** - For Oh My Pi

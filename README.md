<div align="center">

<img src="logo.png" alt="Capybara" width="320" />

# Capybara

**A coding agent as calm and reliable as a capybara.**

[English](README.md) · [简体中文](README.zh-CN.md)

![Python ≥ 3.11](https://img.shields.io/badge/Python-≥3.11-4A90E2?style=flat-square)
![License MIT](https://img.shields.io/badge/License-MIT-4A90E2?style=flat-square)
![Status active](https://img.shields.io/badge/Status-active-4A90E2?style=flat-square)

</div>

---

## What is Capybara?

Capybara is a Python coding agent. Give it a task and it reads code, edits files, runs verification, and reports back — through a ReAct loop, with 14 curated tools and a working memory of conversation history and tool traces. Built to be as steady as its namesake: no panic-drift, no overconfident edits, no hidden side effects.

If you've used Claude Code or Aider, you have the right mental model. The difference is Capybara is a library + CLI you can read in one sitting — `core/` + `tools/` + `agents/` totals around three thousand lines.

---

## What it can do

The short version:

- Reads & edits code — list/read/search, single + multi-patch, line-range replace, full-file write
- Runs verification — set `verify_command` and it loops fix-verify until tests pass (up to `max_fix_iters` rounds)
- Navigates the codebase — ctags definitions, FTS5 search, tree-sitter references + import graph + repo map
- Plugs into external tools — MCP client (stdio, zero-dep) consumes any Model Context Protocol server
- Self-checks — retries with backoff, circuit-breaks on repeated failures, honours a per-task token budget
- Asks before risky writes — confirm-or-auto prompt before every file edit and shell command
- Streams — async event stream for UIs; ships an Ink (React) terminal UI that mimics Claude Code
- Stays observable — optional JSONL trace + HTML report, session save/restore with schema-drift warnings
- Stays in budget — auto-compresses long histories, compacts tool runs, trims tool outputs

The five code-intelligence tools (`find_definition` / `search_codebase` / `find_references` / `repo_map` / `get_file_relations`) are **on by default** — no extras to install for tree-sitter–backed ones.

---

## Install

### Prerequisites

| | Needed for | Install |
|---|---|---|
| **Python ≥ 3.11** | everything | python.org or `pyenv install 3.11` |
| **Node.js ≥ 18** | interactive TUI **only** | nodejs.org LTS / `nvm install 18` |
| **Universal Ctags** | `find_definition` **only** | `brew install universal-ctags` / `apt install universal-ctags` / `choco install universal-ctags` |

If you skip Node or ctags, Capybara still runs and tells you exactly what's missing (see the startup banner section below).

### Option A — pipx (recommended)

`pipx` keeps Capybara in its own isolated environment. Once installed, `capybara` works from **any** directory and **any** shell — no `conda activate`, no venv juggling.

```bash
python -m pip install --user pipx
python -m pipx ensurepath                          # adds pipx bin to PATH; open a new shell after

# In the Capybara repo root
pipx install --editable .

# Anywhere on your machine
capybara "add a CLI entry point and run tests to verify" --workspace ./myproj
```

Editable mode: edit code in place, changes take effect immediately. Re-run `pipx install --editable . --force` only when `pyproject.toml` dependencies change.

### Option B — install into an existing environment

```bash
conda run -n <your-env> pip install -e .
# or:  pip install -e .
```

`capybara` is only on `PATH` while that env is active.

---

## Required environment (two small pieces)

### 1. LLM credentials — `.env` in your working directory

```bash
LLM_API_KEY="sk-..."                        # any OpenAI-compatible provider
LLM_BASE_URL="https://api.deepseek.com"     # DeepSeek / OpenAI / Qwen / Kimi / etc.
LLM_MODEL_ID="deepseek-chat"                # the actual model you want
LLM_TIMEOUT=60                              # optional, in seconds
```

Missing any one → `AgentsLLM` raises at startup. No mid-conversation surprises, no wasted tokens.

### 2. Project config — `./myproj/.capybara.toml` (optional)

A single TOML where any `core/config.py::Config` field is an override. CLI auto-discovers it at the workspace root; pass `--config <path>` to point elsewhere. The repo ships a fully-commented `.capybara.toml` template you can copy and edit.

Minimal example:

```toml
# Where to find ctags — leave empty to use the system PATH
ctags_path = '/usr/local/bin/ctags'

# Tailor the agent
trace_enabled = true
context_window = 64000

# Connect to external MCP servers — no Python glue needed
[[mcp_servers]]
name = "example"
command = "python"
args = ["path/to/your_mcp_server.py"]
```

Unknown fields are silently ignored by pydantic, so a stray key never breaks startup.

---

## What ships with it (the dep table)

Five pip packages, all with upper bounds to prevent surprise upgrades:

| Package | What it does |
|---|---|
| `openai>=2,<3` | LLM client (works for any OpenAI-compatible API: DeepSeek, Qwen, Kimi, OpenAI, Together, …) |
| `python-dotenv>=1,<2` | Loads `.env` automatically at import time |
| `pydantic>=2,<3` | `Config` + `Message` data models |
| `tree-sitter>=0.25,<0.26` | AST parsing for `find_references` / `repo_map` / `get_file_relations` |
| `tree-sitter-language-pack>=1.10,<2` | 150+ language grammars (Python, JS, TS, Go, Rust, Java, C, C++, …) |

Plus one **external binary** (intentionally not a pip dep):

- **Universal Ctags** — for `find_definition`. Single config source is `.capybara.toml`'s `ctags_path` field, or put `ctags` on `PATH`.

### Startup banner

If either code-intel piece is missing, Capybara prints a single Chinese banner to **stderr** at startup listing exactly what's gone and how to fix it:

```
⚠ 代码智能可用度不足（不影响基础读写/搜索/运行，仅以下高级检索降级）：
  · ctags 未找到 → find_definition 不可用。
      装 Universal Ctags：Windows `choco install universal-ctags` / `scoop install universal-ctags`；
      或在 .capybara.toml 设 ctags_path 指向 ctags 可执行文件。
```

Capybara does **not** abort — base read/search/run tools keep working, only the specific code-intel tool that needs the missing piece degrades. When nothing is missing, the banner is silent and stdout stays clean.

---

## Usage

### One-shot, from the CLI

```bash
# Minimal
capybara "fix the typo in foo.py and run compileall"

# In a different workspace, with auto-confirm
capybara "refactor the cache" --workspace ./myproj --auto-approve

# Full production stack + verify-fix loop
capybara "wire up the CLI" --production --verify "python -m unittest"
```

Flags:

- `--production` — opens trace, auto-save, project-context detection, edit tracking, sub-agent, budget
- `--verify CMD` — runs `CMD` after the agent's last edit; failure loops back as a user message until pass or `max_fix_iters`
- `--auto-approve` — skip per-edit confirmations for the whole run
- `--max-steps N` — ReAct step cap (default 20)
- `--config PATH` — use a specific `.capybara.toml`
- `--verbose` — print step-by-step trace to stdout

### Interactive TUI (Claude Code style)

Without a task argument, `capybara` starts an Ink (React) terminal UI: Sixel logo on launch, model line, prompt box, multi-turn conversation, tool calls flash by (spinner + current step, collapses on complete) and leave a compact log line in the persistent transcript, file writes / shell commands show a confirmation dialog (`y` approve / `n` reject).

```bash
cd ui && npm install     # one-time
capybara                  # anywhere
```

Needs Node ≥ 18. The one-shot `capybara "task"` mode never touches Node.

### Library (Python)

```python
from core.llm import AgentsLLM
from agents import CodingAgent

agent = CodingAgent(name="coder", llm=AgentsLLM(), workspace_root=".")
print(agent.run("fix the foo.py typo and verify with compileall"))
```

Or async + streaming:

```python
async for ev in agent.arun_stream("refactor the cache module"):
    print(ev.type, ev.data)
```

For the full stack (trace + auto-save + project bootstrap + confirm/auto + edit tracking + sub-agent + budget):

```python
agent = CodingAgent.production(
    name="coder", llm=AgentsLLM(), workspace_root=".",
    verify_command="python -m unittest",
)
```

---

## Tests

298 stdlib `unittest` cases, no `pytest` needed, no real network calls. Code-intel tests auto-skip on machines missing ctags / tree-sitter so a fresh clone still runs:

```bash
python -m unittest discover -s tests
```

Runnable demos (some need an LLM, some don't):

```bash
python demo/coding_agent_resilience_demo.py     # retry / circuit-breaker — no LLM
python demo/coding_agent_orchestration_demo.py  # sub-agent delegation — no LLM
python demo/coding_agent_mcp_demo.py            # MCP client — first part runs offline
python demo/coding_agent_pro_demo.py            # full production stack — needs LLM
```

---

## Where to read more

| Doc | What's in it |
|---|---|
| `PROJECT.md` | architecture, package layout, full changelog |
| `docs/api-reference.md` | public API by package, import paths verified |
| `docs/api-handbook.md` | internals + extensions + troubleshooting recipes |
| `docs/code-intelligence.md` | design of the code-intel subsystem |
| `docs/mcp-guide.md` | MCP primer + how to write your own server |
| `docs/enhancement-roadmap.md` | live list of what's still TODO |

---

## Status & contributing

Active. All roadmap stages 0–8 are shipped; the four subsequent hardening rounds (P0–P3, CI-1–4, packaging/MCP/encoding) are also done. Real outstanding items live in `docs/enhancement-roadmap.md`.

Issues and discussion are open. Open an issue before sending a PR — `docs/agent-structure-guide.md` lays out the conventions.

---

## License

MIT — see [LICENSE](LICENSE).

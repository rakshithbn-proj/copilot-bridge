# Copilot Bridge Server

[![VS Code Marketplace](https://vsmarketplacebadges.dev/version/rakshithbn.copilot-bridge-server.svg)](https://marketplace.visualstudio.com/items?itemName=rakshithbn.copilot-bridge-server)
[![PyPI](https://img.shields.io/pypi/v/copilot-bridge)](https://pypi.org/project/copilot-bridge/)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://github.com/rakshithbn-proj/copilot-bridge/blob/main/LICENSE)

A local HTTP server that exposes VS Code and GitHub Copilot Chat to any external process — Python scripts, agents, CLIs, or other tools.

```
External process
    ↕  HTTP (localhost:5150)
copilot-bridge-extension   ← this extension (the server)
    ↕  VS Code API
GitHub Copilot Chat
```

## What it does

Once installed, this extension starts an HTTP server on `localhost:5150` when VS Code opens. Any local process can then call Copilot Chat, read/write files, run terminal commands, query diagnostics, and more — all over plain HTTP.

## Quickstart

### 1 — Install the Python client

```bash
pip install copilot-bridge
```

### 2 — Use it

```python
from copilot_bridge import CopilotBridge, CopilotAgent

# One-shot question
client = CopilotBridge()
print(client.ask("Explain this function in one sentence"))

# Stateful chat
client.set_system_prompt("You are a terse code reviewer.")
reply = client.chat("Review my PR diff")

# Agentic loop — reads/writes files, runs commands, etc.
agent = CopilotAgent()
agent.run("Refactor src/auth.py to use async/await")
```

**CLI:**

```bash
python -m copilot_bridge              # interactive chat
python -m copilot_bridge agent        # interactive agent
python -m copilot_bridge ask "Hello"  # one-shot
python -m copilot_bridge status       # connection check
```

## API overview

`CopilotBridge` exposes the full VS Code surface over HTTP:

| Category | Methods |
|----------|---------|
| **Chat** | `ask`, `chat`, `chat_stream`, `chat_with_image` |
| **Files** | `read_file`, `write_file`, `edit_file`, `multi_edit`, `delete_file`, `rename_file`, `copy_file`, `list_directory` |
| **Search** | `search_text`, `find_files`, `semantic_search` |
| **Code intelligence** | `search_symbols`, `find_definition`, `find_usages`, `hover`, `rename_symbol`, `call_hierarchy` |
| **Git** | `git_status`, `git_diff`, `git_log`, `git_add`, `git_commit`, `git_push`, `git_pull` |
| **Diagnostics** | `get_diagnostics`, `get_errors` |
| **Terminal** | `create_terminal`, `send_to_terminal`, `get_terminal_output` |
| **Workspace** | `get_workspace`, `get_workspace_index`, `semantic_search`, `reindex` |

## Performance

Measured on `v5.2.0` (localhost, 20 LLM rounds, 5 concurrent workers, 100 requests):

| Layer | Latency |
|---|---|
| HTTP stack | ~7 ms |
| + Auth + extension host | +0.7 ms |
| + Copilot LLM | +1941 ms |

**The bridge adds < 1 ms overhead.** The LLM is the bottleneck — not the bridge.

| Benchmark | Mean | p95 | p99 | Errors |
|-----------|-----:|----:|----:|-------:|
| Health (HTTP only) | 7 ms | 17 ms | 18 ms | 0% |
| Echo (ext host, no LLM) | 8 ms | 19 ms | 24 ms | 0% |
| Sequential ask() | 1949 ms | 2138 ms | 2651 ms | 0% |
| Streaming TTFT | 1958 ms | 2829 ms | 2831 ms | 0% |
| Concurrent (5 workers, 100 req) | 2018 ms | 2732 ms | 3054 ms | 0% |

**Throughput:** 2.45 req/s · 0 errors on 100 concurrent requests

## Authentication

The extension generates an API key on first start, stored at `~/.copilot-bridge/config.json`. The Python client loads it automatically. All endpoints except `/health` require a `Bearer` token.

To rotate: delete `~/.copilot-bridge/config.json` and reload VS Code.

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `copilotBridge.port` | `5150` | HTTP server port (auto-increments if in use) |
| `copilotBridge.autoStart` | `true` | Start server when VS Code opens |

## Security

- Binds to `127.0.0.1` only — never exposed to the network
- API key protected — no unauthenticated request reaches Copilot Chat
- No telemetry — no usage data collected, no outbound connections

## Links

- [GitHub](https://github.com/rakshithbn-proj/copilot-bridge)
- [PyPI — Python client](https://pypi.org/project/copilot-bridge/)
- [Benchmark results](https://github.com/rakshithbn-proj/copilot-bridge/tree/main/benchmarks)
- [Full API reference](https://github.com/rakshithbn-proj/copilot-bridge/blob/main/copilot-bridge-dist/copilot_bridge.pyi)

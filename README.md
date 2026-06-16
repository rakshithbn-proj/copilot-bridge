# Copilot Bridge

A local HTTP bridge that exposes VS Code and GitHub Copilot Chat to any external process — Python scripts, agents, CLIs, or other tools.

```
External process
    ↕  HTTP (localhost:5150)
copilot-bridge-extension   ← VS Code extension (the server)
    ↕  VS Code API
GitHub Copilot Chat
```

## Components

| Path | Purpose |
|------|---------|
| `copilot-bridge-extension/` | VS Code extension — starts the HTTP server inside VS Code |
| `copilot-bridge-dist/` | Python client library (`CopilotBridge`, `CopilotAgent`) |

## Quickstart

### 1 — Install the VS Code extension

```bash
cd copilot-bridge-extension
npm install
npm run package          # produces copilot-bridge-x.x.x.vsix
code --install-extension copilot-bridge-*.vsix
```

Or run `install_bridge.bat` on Windows.

The extension auto-starts an HTTP server on `localhost:5150` when VS Code opens.

### 2 — Install the Python client

```bash
cd copilot-bridge-dist
pip install .
```

Or build and install the wheel:

```bash
pip install build
python -m build
pip install dist/copilot_bridge-*.whl
```

### 3 — Use it

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
| **Search** | `search_text`, `search_files`, `find_files`, `semantic_search` |
| **Code intelligence** | `search_symbols`, `find_definition`, `find_usages`, `hover`, `document_symbols`, `rename_symbol`, `call_hierarchy` |
| **Git** | `git_status`, `git_diff`, `git_log`, `git_branches`, `git_add`, `git_commit`, `git_push`, `git_pull`, `git_merge`, `git_checkout`, `git_stash` |
| **Diagnostics** | `get_diagnostics`, `get_errors` |
| **Editor** | `get_editor`, `open_file`, `insert_text`, `get_selection` |
| **Terminal** | `create_terminal`, `send_to_terminal`, `get_terminal_output`, `dispose_terminal` |
| **UI** | `notify_info`, `notify_warn`, `notify_error`, `prompt_input`, `prompt_quickpick` |
| **Workspace** | `get_workspace`, `get_workspace_index`, `get_workspace_files`, `get_related_files`, `get_import_graph`, `reindex` |

See `copilot-bridge-dist/copilot_bridge.pyi` for the full typed interface.

## Configuration

In VS Code settings (`Ctrl+,`, search "Copilot Bridge"):

| Setting | Default | Description |
|---------|---------|-------------|
| `copilotBridge.port` | `5150` | HTTP server port (auto-increments if busy) |
| `copilotBridge.autoStart` | `true` | Start server when VS Code opens |

## Requirements

- VS Code 1.90+
- GitHub Copilot Chat extension
- Python ≥ 3.10 (client library)
- Node.js ≥ 18 (to build the extension from source)

## License

MIT — see `copilot-bridge-extension/LICENSE`.

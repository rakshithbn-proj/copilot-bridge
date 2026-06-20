"""
Copilot Bridge v5 - Unified Python Interface to VS Code

Single entry point for ALL Copilot Bridge functionality:
  - CopilotBridge: Client class (chat, files, git, search, diagnostics, etc.)
  - CopilotAgent:  Agentic assistant with tool-calling loop
  - CLI:           chat, agent, ask, status, test modes

Usage (as library):
    from copilot_bridge import CopilotBridge, CopilotAgent

    client = CopilotBridge()
    print(client.ask("Hello!"))

    agent = CopilotAgent()
    agent.run("Create a hello world script")

Usage (from command line):
    python copilot_bridge.py                    # interactive chat
    python copilot_bridge.py chat               # interactive chat
    python copilot_bridge.py agent              # interactive agent
    python copilot_bridge.py agent "fix bug"    # one-shot agent task
    python copilot_bridge.py ask "question"     # one-shot question
    python copilot_bridge.py status             # show connection status
"""

import json
import re
import sys
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Dict, Optional, Any, Union, Generator

import requests


# ╔══════════════════════════════════════════════════════════════════╗
# ║                     COPILOT BRIDGE CLIENT                       ║
# ╚══════════════════════════════════════════════════════════════════╝

class CopilotBridge:
    """Full-feature client for the Copilot Bridge VS Code extension."""

    VERSION = "5.2.0"

    CONFIG_FILE = os.path.join(os.path.expanduser("~"), ".copilot-bridge", "config.json")

    def __init__(self, host: str = "127.0.0.1", port: int = 5150,
                 auto_discover: bool = True, api_key: Optional[str] = None):
        self._host = host
        self._port = port
        self.base_url = f"http://{host}:{port}"
        self.conversation_history: List[Dict[str, str]] = []
        self.system_prompt: Optional[str] = None
        self.preferred_model: Optional[str] = None
        self.temperature: Optional[float] = None
        self.top_p: Optional[float] = None
        self.max_tokens: Optional[int] = None
        self.timeout = 120

        # Load API key: explicit arg > config file > no auth (backwards compat)
        self._api_key: Optional[str] = api_key or self._load_api_key()

        if auto_discover and not self.is_available():
            self._discover_port()

    @classmethod
    def _load_api_key(cls) -> Optional[str]:
        """Read the API key from ~/.copilot-bridge/config.json if it exists."""
        try:
            with open(cls.CONFIG_FILE) as f:
                cfg = json.load(f)
                return cfg.get("apiKey") or None
        except (FileNotFoundError, json.JSONDecodeError, PermissionError):
            return None

    def _auth_headers(self) -> Dict[str, str]:
        if self._api_key:
            return {"Authorization": f"Bearer {self._api_key}"}
        return {}

    def _discover_port(self, start: int = 5150, max_attempts: int = 10):
        """Try consecutive ports to find the running bridge server."""
        for i in range(max_attempts):
            port = start + i
            try:
                # /health is unauthenticated — safe to probe without a key
                r = requests.get(f"http://{self._host}:{port}/health", timeout=1)
                if r.status_code == 200:
                    self._port = port
                    self.base_url = f"http://{self._host}:{port}"
                    return
            except requests.RequestException:
                continue

    # ── HTTP helpers ──────────────────────────────────────────────

    def _get(self, endpoint: str, timeout: int = None) -> Dict[str, Any]:
        r = requests.get(f"{self.base_url}{endpoint}",
                         headers=self._auth_headers(),
                         timeout=timeout or self.timeout)
        return r.json()

    def _post(self, endpoint: str, data: Dict[str, Any] = None, timeout: int = None) -> Dict[str, Any]:
        r = requests.post(f"{self.base_url}{endpoint}",
                          json=data or {},
                          headers=self._auth_headers(),
                          timeout=timeout or self.timeout)
        return r.json()

    # ── Connection ────────────────────────────────────────────────

    def is_available(self) -> bool:
        """Check if the Copilot Bridge server is running."""
        try:
            # /health is unauthenticated — safe to call without a key
            return requests.get(f"{self.base_url}/health", timeout=2).status_code == 200
        except requests.RequestException:
            return False

    def get_health(self) -> Dict[str, Any]:
        return self._get("/health", timeout=5)

    def echo(self) -> Dict[str, Any]:
        """No-op round-trip through the extension host. Used to isolate
        extension-host overhead from LLM latency in benchmarks."""
        return self._get("/echo", timeout=5)

    def get_models(self) -> List[Dict[str, Any]]:
        return self._get("/models", timeout=10).get("models", [])

    def get_workspace(self) -> Dict[str, Any]:
        return self._get("/workspace", timeout=5)

    # ── Chat / LLM ───────────────────────────────────────────────

    def set_system_prompt(self, prompt: str) -> None:
        self.system_prompt = prompt

    def set_model(self, model: str) -> None:
        self.preferred_model = model

    def set_temperature(self, temperature: float) -> None:
        """Set LLM temperature (0.0 = deterministic, 1.0+ = creative)."""
        self.temperature = temperature

    def set_top_p(self, top_p: float) -> None:
        """Set top-p (nucleus) sampling parameter."""
        self.top_p = top_p

    def set_max_tokens(self, max_tokens: int) -> None:
        """Set maximum tokens in the response."""
        self.max_tokens = max_tokens

    def clear_history(self) -> None:
        self.conversation_history = []

    def chat(self, message: str, keep_history: bool = True) -> str:
        """Send a chat message and get a response."""
        if not keep_history:
            messages = [{"role": "user", "content": message}]
        else:
            self.conversation_history.append({"role": "user", "content": message})
            messages = self.conversation_history.copy()

        req = {"messages": messages}
        if self.system_prompt:
            req["systemPrompt"] = self.system_prompt
        if self.preferred_model:
            req["model"] = self.preferred_model
        if self.temperature is not None:
            req["temperature"] = self.temperature
        if self.top_p is not None:
            req["topP"] = self.top_p
        if self.max_tokens is not None:
            req["maxTokens"] = self.max_tokens

        data = self._post("/chat", req)
        if not data.get("success"):
            raise RuntimeError(f"Chat failed: {data.get('error')}")

        response = data.get("content", "")
        if keep_history:
            self.conversation_history.append({"role": "assistant", "content": response})
        return response

    def ask(self, message: str) -> str:
        """Single question, no history."""
        return self.chat(message, keep_history=False)

    def chat_stream(self, message: str, keep_history: bool = True):
        """Send a chat message and yield response chunks (SSE streaming)."""
        if not keep_history:
            messages = [{"role": "user", "content": message}]
        else:
            self.conversation_history.append({"role": "user", "content": message})
            messages = self.conversation_history.copy()

        req: Dict[str, Any] = {"messages": messages}
        if self.system_prompt:
            req["systemPrompt"] = self.system_prompt
        if self.preferred_model:
            req["model"] = self.preferred_model
        if self.temperature is not None:
            req["temperature"] = self.temperature
        if self.top_p is not None:
            req["topP"] = self.top_p
        if self.max_tokens is not None:
            req["maxTokens"] = self.max_tokens

        full_response = ""
        with requests.post(f"{self.base_url}/chat/stream", json=req,
                           headers=self._auth_headers(),
                           stream=True, timeout=self.timeout) as r:
            for line in r.iter_lines(decode_unicode=True):
                if not line or not line.startswith("data: "):
                    continue
                data = json.loads(line[6:])
                if data.get("error"):
                    raise RuntimeError(f"Stream error: {data['error']}")
                if data.get("chunk"):
                    full_response += data["chunk"]
                    yield data["chunk"]
                if data.get("done"):
                    break

        if keep_history:
            self.conversation_history.append({"role": "assistant", "content": full_response})

    # ── File operations ──────────────────────────────────────────

    def read_file(self, path: str, start_line: int = None, end_line: int = None) -> str:
        req = {"path": path}
        if start_line: req["startLine"] = start_line
        if end_line:   req["endLine"] = end_line
        data = self._post("/file/read", req)
        if not data.get("success"):
            raise RuntimeError(f"Read failed: {data.get('error')}")
        return data.get("content", "")

    def write_file(self, path: str, content: str) -> str:
        data = self._post("/file/write", {"path": path, "content": content})
        if not data.get("success"):
            raise RuntimeError(f"Write failed: {data.get('error')}")
        return data.get("path", path)

    def edit_file(self, path: str, old_string: str, new_string: str) -> bool:
        data = self._post("/file/edit", {"path": path, "oldString": old_string, "newString": new_string})
        if not data.get("success"):
            raise RuntimeError(f"Edit failed: {data.get('error')}")
        return True

    def multi_edit(self, edits: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        return self._post("/file/multi-edit", {"edits": edits}).get("results", [])

    def delete_file(self, path: str) -> bool:
        return self._post("/file/delete", {"path": path}).get("success", False)

    def rename_file(self, old_path: str, new_path: str) -> bool:
        return self._post("/file/rename", {"oldPath": old_path, "newPath": new_path}).get("success", False)

    def copy_file(self, source: str, destination: str) -> bool:
        return self._post("/file/copy", {"source": source, "destination": destination}).get("success", False)

    def list_directory(self, path: str = ".") -> List[Dict[str, Any]]:
        data = self._post("/file/list", {"path": path})
        if not data.get("success"):
            raise RuntimeError(f"List failed: {data.get('error')}")
        return data.get("items", [])

    # ── Search ───────────────────────────────────────────────────

    def search_text(self, pattern: str, directory: str = ".",
                    file_pattern: str = None, max_results: int = 100) -> List[Dict[str, Any]]:
        req = {"pattern": pattern, "directory": directory, "maxResults": max_results}
        if file_pattern: req["filePattern"] = file_pattern
        return self._post("/search/text", req).get("results", [])

    def search_files(self, pattern: str, directory: str = ".",
                     file_pattern: str = None) -> List[Dict[str, Any]]:
        """Alias for search_text (backward compat)."""
        return self.search_text(pattern, directory, file_pattern)

    def find_files(self, pattern: str, max_results: int = 100) -> List[str]:
        return self._post("/search/files", {"pattern": pattern, "maxResults": max_results}).get("files", [])

    def search_symbols(self, query: str) -> List[Dict[str, Any]]:
        return self._post("/search/symbols", {"query": query}).get("symbols", [])

    def find_usages(self, path: str, line: int, character: int) -> List[Dict[str, Any]]:
        return self._post("/search/usages", {"path": path, "line": line, "character": character}).get("usages", [])

    def find_definition(self, path: str, line: int, character: int) -> List[Dict[str, Any]]:
        return self._post("/search/definition", {"path": path, "line": line, "character": character}).get("definitions", [])

    def hover(self, path: str, line: int, character: int) -> List[Dict[str, Any]]:
        """Get hover information at a position."""
        return self._post("/search/hover", {"path": path, "line": line, "character": character}).get("hovers", [])

    def code_actions(self, path: str, line: int, start_character: int = 0,
                     end_line: int = None, end_character: int = None) -> List[Dict[str, Any]]:
        """Get available code actions / quick fixes at a position or range."""
        req: Dict[str, Any] = {"path": path, "line": line, "startCharacter": start_character}
        if end_line: req["endLine"] = end_line
        if end_character is not None: req["endCharacter"] = end_character
        return self._post("/search/codeActions", req).get("codeActions", [])

    def document_symbols(self, path: str) -> List[Dict[str, Any]]:
        """Get document outline / symbols for a file."""
        return self._post("/search/documentSymbols", {"path": path}).get("symbols", [])

    def rename_symbol(self, path: str, line: int, character: int, new_name: str) -> Dict[str, Any]:
        """Rename a symbol across the workspace."""
        return self._post("/search/rename", {"path": path, "line": line, "character": character, "newName": new_name})

    def call_hierarchy(self, path: str, line: int, character: int,
                       direction: str = "both") -> Dict[str, Any]:
        """Get call hierarchy (incoming/outgoing callers) at a position."""
        return self._post("/search/callHierarchy", {
            "path": path, "line": line, "character": character, "direction": direction
        })

    # ── Commands ─────────────────────────────────────────────────

    def run_command(self, command: str, cwd: str = None, timeout: int = 30000) -> Dict[str, Any]:
        req = {"command": command, "timeout": timeout}
        if cwd: req["cwd"] = cwd
        return self._post("/command/run", req, timeout=timeout // 1000 + 10)

    def vscode_command(self, command: str, *args) -> Any:
        data = self._post("/vscode/command", {"command": command, "args": list(args)})
        if not data.get("success"):
            raise RuntimeError(f"Command failed: {data.get('error')}")
        return data.get("result")

    # ── Git ───────────────────────────────────────────────────────

    def git_status(self) -> List[Dict[str, str]]:
        return self._post("/git/status", {}).get("files", [])

    def git_diff(self, staged: bool = False, file: str = None) -> str:
        return self._post("/git/diff", {"staged": staged, "file": file}).get("diff", "")

    def git_changed_files(self, staged: bool = False, include_untracked: bool = False) -> List[str]:
        return self._post("/git/changed", {"staged": staged, "includeUntracked": include_untracked}).get("files", [])

    def git_log(self, limit: int = 20) -> List[Dict[str, str]]:
        return self._post("/git/log", {"limit": limit}).get("commits", [])

    def git_branches(self) -> List[Dict[str, Any]]:
        return self._post("/git/branches", {}).get("branches", [])

    # ── Diagnostics ──────────────────────────────────────────────

    def get_diagnostics(self, path: str = None) -> List[Dict[str, Any]]:
        if path:
            return self._post("/diagnostics/file", {"path": path}).get("diagnostics", [])
        return self._get("/diagnostics").get("diagnostics", [])

    def get_errors(self, path: str = None) -> List[Dict[str, Any]]:
        return self.get_diagnostics(path)

    # ── Editor ───────────────────────────────────────────────────

    def get_editor(self) -> Dict[str, Any]:
        return self._get("/editor")

    def open_file(self, path: str, line: int = None, character: int = None) -> bool:
        req = {"path": path}
        if line:      req["line"] = line
        if character: req["character"] = character
        return self._post("/editor/open", req).get("success", False)

    def insert_text(self, text: str, position: str = "cursor",
                    line: int = None, character: int = None) -> bool:
        req = {"text": text, "position": position}
        if line:      req["line"] = line
        if character: req["character"] = character
        return self._post("/editor/insert", req).get("success", False)

    def get_selection(self) -> Dict[str, Any]:
        return self._post("/editor/selection", {})

    # ── Notifications ────────────────────────────────────────────

    def notify_info(self, message: str) -> None:
        self._post("/notify/info", {"message": message})

    def notify_warn(self, message: str) -> None:
        self._post("/notify/warn", {"message": message})

    def notify_error(self, message: str) -> None:
        self._post("/notify/error", {"message": message})

    def prompt_input(self, prompt: str, placeholder: str = None,
                     default_value: str = None) -> Optional[str]:
        req = {"prompt": prompt}
        if placeholder:    req["placeholder"] = placeholder
        if default_value:  req["defaultValue"] = default_value
        return self._post("/notify/input", req).get("value")

    def prompt_quickpick(self, items: List[str], placeholder: str = None,
                         multi_select: bool = False) -> Union[str, List[str], None]:
        return self._post("/notify/quickpick", {
            "items": items, "placeholder": placeholder, "multiSelect": multi_select
        }).get("value")

    # ── Network ──────────────────────────────────────────────────

    def fetch(self, url: str, method: str = "GET",
              headers: Dict[str, str] = None, body: str = None) -> Dict[str, Any]:
        req = {"url": url, "method": method}
        if headers: req["headers"] = headers
        if body:    req["body"] = body
        return self._post("/fetch", req)

    # ── Info ─────────────────────────────────────────────────────

    def get_terminals(self) -> List[Dict[str, Any]]:
        return self._get("/terminals").get("terminals", [])

    def get_extensions(self) -> List[Dict[str, Any]]:
        return self._get("/extensions").get("extensions", [])

    # ── Terminal management ──────────────────────────────────────

    def create_terminal(self, name: str = "Bridge Terminal", cwd: str = None,
                        show: bool = True) -> str:
        """Create a new VS Code terminal and return its name."""
        req: Dict[str, Any] = {"name": name, "show": show}
        if cwd: req["cwd"] = cwd
        data = self._post("/terminal/create", req)
        if not data.get("success"):
            raise RuntimeError(f"Terminal create failed: {data.get('error')}")
        return data.get("name", name)

    def send_to_terminal(self, name: str, text: str, add_newline: bool = True) -> bool:
        """Send text to a named terminal."""
        return self._post("/terminal/send", {
            "name": name, "text": text, "addNewline": add_newline
        }).get("success", False)

    def dispose_terminal(self, name: str) -> bool:
        """Close a named terminal."""
        return self._post("/terminal/dispose", {"name": name}).get("success", False)

    # ── Git extended ─────────────────────────────────────────────

    def git_commit(self, message: str, all: bool = False, amend: bool = False) -> Dict[str, Any]:
        """Commit staged changes (or all with all=True)."""
        return self._post("/git/commit", {"message": message, "all": all, "amend": amend})

    def git_stash(self, action: str = "push", message: str = None,
                  include_untracked: bool = False) -> Dict[str, Any]:
        """Stash changes. action: push, pop, list, drop, apply."""
        req: Dict[str, Any] = {"action": action}
        if message: req["message"] = message
        if include_untracked: req["includeUntracked"] = include_untracked
        return self._post("/git/stash", req)

    def git_checkout(self, branch: str, create: bool = False) -> Dict[str, Any]:
        """Checkout a branch. Set create=True to create a new branch."""
        return self._post("/git/checkout", {"branch": branch, "create": create})

    # ── Terminal output ──────────────────────────────────────────

    def get_terminal_output(self, name: str, last_lines: int = None,
                            clear: bool = False) -> str:
        """Get captured output from a named terminal."""
        req: Dict[str, Any] = {"name": name, "clear": clear}
        if last_lines: req["lastLines"] = last_lines
        return self._post("/terminal/output", req).get("output", "")

    # ── Cancellation ─────────────────────────────────────────────

    def cancel(self, request_id: str = None) -> Dict[str, Any]:
        """Cancel an active operation by ID, or all if no ID."""
        req: Dict[str, Any] = {}
        if request_id: req["id"] = request_id
        return self._post("/cancel", req)

    # ── Token counting ───────────────────────────────────────────

    def count_tokens(self, text: str, model: str = None) -> Dict[str, Any]:
        """Count tokens in text using the model's tokenizer."""
        req: Dict[str, Any] = {"text": text}
        if model: req["model"] = model
        return self._post("/tokens/count", req)

    # ── Semantic search + workspace index ────────────────────────

    def semantic_search(self, query: str, max_results: int = 20,
                        use_llm: bool = True) -> Dict[str, Any]:
        """Semantic search using TF-IDF + LLM query expansion + relevance ranking."""
        return self._post("/search/semantic", {
            "query": query, "maxResults": max_results, "useLLM": use_llm
        })

    def get_workspace_index(self) -> Dict[str, Any]:
        """Get workspace index statistics."""
        return self._get("/workspace/index")

    def get_workspace_files(self) -> List[Dict[str, Any]]:
        """Get all indexed files with metadata."""
        return self._get("/workspace/index/files").get("files", [])

    def get_related_files(self, path: str, max_results: int = 10) -> Dict[str, Any]:
        """Find files related to the given file via imports, symbols, etc."""
        return self._post("/workspace/related", {"path": path, "maxResults": max_results})

    def get_import_graph(self, path: str = None) -> Dict[str, Any]:
        """Get import graph for a file, or the full workspace import graph."""
        req: Dict[str, Any] = {}
        if path: req["path"] = path
        return self._post("/workspace/imports", req)

    def reindex(self) -> Dict[str, Any]:
        """Trigger workspace re-indexing."""
        return self._post("/workspace/reindex", {})

    # ── Git add / push / pull / merge ────────────────────────────

    def git_add(self, files: Union[str, List[str]]) -> Dict[str, Any]:
        """Stage files. Pass '.' or '-A' to stage all, or a list of paths."""
        return self._post("/git/add", {"files": files})

    def git_push(self, remote: str = "origin", branch: str = None,
                 set_upstream: bool = False, force: bool = False,
                 tags: bool = False) -> Dict[str, Any]:
        """Push commits to remote. force uses --force-with-lease (safe force)."""
        req: Dict[str, Any] = {"remote": remote}
        if branch:       req["branch"] = branch
        if set_upstream: req["setUpstream"] = True
        if force:        req["force"] = True
        if tags:         req["tags"] = True
        return self._post("/git/push", req, timeout=90)

    def git_pull(self, remote: str = None, branch: str = None,
                 rebase: bool = False) -> Dict[str, Any]:
        """Pull from remote."""
        req: Dict[str, Any] = {}
        if remote: req["remote"] = remote
        if branch: req["branch"] = branch
        if rebase: req["rebase"] = True
        return self._post("/git/pull", req, timeout=90)

    def git_merge(self, branch: str, no_ff: bool = False,
                  squash: bool = False, message: str = None) -> Dict[str, Any]:
        """Merge a branch into the current branch."""
        req: Dict[str, Any] = {"branch": branch}
        if no_ff:   req["noFf"] = True
        if squash:  req["squash"] = True
        if message: req["message"] = message
        return self._post("/git/merge", req)

    # ── LM tools / workspace trust / custom instructions ─────────

    def get_lm_tools(self) -> List[Dict[str, Any]]:
        """List all Language Model tools registered in VS Code (requires VS Code 1.99+)."""
        return self._get("/lm/tools", timeout=10).get("tools", [])

    def get_workspace_trust(self) -> Dict[str, Any]:
        """Get workspace trust level ('full' or 'restricted')."""
        return self._get("/workspace/trust", timeout=5)

    def get_custom_instructions(self) -> str:
        """Read .github/copilot-instructions.md from the workspace root (returns '' if absent)."""
        return self._get("/copilot/instructions", timeout=5).get("content", "")

    # ── Vision / image chat ──────────────────────────────────────

    def chat_with_image(self, message: str, image_path: str,
                        keep_history: bool = False,
                        system_prompt: str = None,
                        model: str = None,
                        temperature: float = None,
                        max_tokens: int = None) -> str:
        """Send a message + image to the LLM (requires VS Code 1.97+ and a vision-capable model).

        image_path: absolute or workspace-relative path to PNG/JPG/GIF/WEBP.
        """
        req: Dict[str, Any] = {"imagePath": image_path, "message": message}
        if system_prompt: req["systemPrompt"] = system_prompt
        if model:         req["model"] = model
        if temperature is not None: req["temperature"] = temperature
        if max_tokens is not None:  req["maxTokens"] = max_tokens

        if keep_history and self.conversation_history:
            req["messages"] = self.conversation_history.copy()

        data = self._post("/chat/image", req)
        if not data.get("success"):
            raise RuntimeError(f"Image chat failed: {data.get('error')}")

        response = data.get("content", "")
        if keep_history:
            self.conversation_history.append({"role": "user", "content": f"[image: {image_path}] {message}"})
            self.conversation_history.append({"role": "assistant", "content": response})
        return response


# ╔══════════════════════════════════════════════════════════════════╗
# ║                      COPILOT AGENT                              ║
# ╚══════════════════════════════════════════════════════════════════╝

_AGENT_SYSTEM_PROMPT = """You are an expert coding agent with full VS Code access. You think step-by-step and use tools precisely.

TOOL CALLS: Output JSON inside <tool_call> blocks. You can make MULTIPLE calls in one response — independent tools run in parallel.

== FILE ==
<tool_call>{"tool": "read_file", "path": "file.py", "start_line": 1, "end_line": 50}</tool_call>
<tool_call>{"tool": "write_file", "path": "file.py", "content": "# content"}</tool_call>
<tool_call>{"tool": "edit_file", "path": "file.py", "old_string": "old text with 2-3 lines context", "new_string": "new text"}</tool_call>
<tool_call>{"tool": "multi_edit", "edits": [{"path": "a.py", "old_string": "x", "new_string": "y"}]}</tool_call>
<tool_call>{"tool": "delete_file", "path": "file.py"}</tool_call>
<tool_call>{"tool": "list_directory", "path": "."}</tool_call>

== SEARCH ==
<tool_call>{"tool": "search_text", "pattern": "def main", "file_pattern": ".py"}</tool_call>
<tool_call>{"tool": "find_files", "pattern": "**/*.py"}</tool_call>
<tool_call>{"tool": "search_symbols", "query": "ClassName"}</tool_call>
<tool_call>{"tool": "find_definition", "path": "f.py", "line": 10, "character": 5}</tool_call>
<tool_call>{"tool": "find_usages", "path": "f.py", "line": 10, "character": 5}</tool_call>

== SEMANTIC SEARCH (natural language, TF-IDF + LLM) ==
<tool_call>{"tool": "semantic_search", "query": "authentication logic", "max_results": 10}</tool_call>
<tool_call>{"tool": "semantic_search", "query": "error handling", "use_llm": false}</tool_call>

== WORKSPACE INDEX ==
<tool_call>{"tool": "workspace_index"}</tool_call>
<tool_call>{"tool": "related_files", "path": "src/auth.py"}</tool_call>
<tool_call>{"tool": "import_graph", "path": "src/auth.py"}</tool_call>

== CODE INTELLIGENCE ==
<tool_call>{"tool": "hover", "path": "f.py", "line": 10, "character": 5}</tool_call>
<tool_call>{"tool": "code_actions", "path": "f.py", "line": 10}</tool_call>
<tool_call>{"tool": "document_symbols", "path": "f.py"}</tool_call>
<tool_call>{"tool": "rename_symbol", "path": "f.py", "line": 10, "character": 5, "new_name": "newName"}</tool_call>
<tool_call>{"tool": "call_hierarchy", "path": "f.py", "line": 10, "character": 5, "direction": "both"}</tool_call>

== COMMANDS ==
<tool_call>{"tool": "run_command", "command": "python script.py", "cwd": "."}</tool_call>
<tool_call>{"tool": "vscode_command", "command": "editor.action.formatDocument"}</tool_call>

== GIT ==
<tool_call>{"tool": "git_status"}</tool_call>
<tool_call>{"tool": "git_diff", "staged": false}</tool_call>
<tool_call>{"tool": "git_log", "limit": 10}</tool_call>
<tool_call>{"tool": "git_branches"}</tool_call>
<tool_call>{"tool": "git_commit", "message": "fix: bug", "all": true}</tool_call>
<tool_call>{"tool": "git_stash", "action": "push", "message": "wip"}</tool_call>
<tool_call>{"tool": "git_checkout", "branch": "feature", "create": true}</tool_call>
<tool_call>{"tool": "git_add", "files": ["src/main.py", "README.md"]}</tool_call>
<tool_call>{"tool": "git_add", "files": "."}</tool_call>
<tool_call>{"tool": "git_push", "remote": "origin", "branch": "main", "set_upstream": false}</tool_call>
<tool_call>{"tool": "git_pull", "remote": "origin", "branch": "main", "rebase": false}</tool_call>
<tool_call>{"tool": "git_merge", "branch": "feature/my-feature", "no_ff": false}</tool_call>

== WORKSPACE & COPILOT ==
<tool_call>{"tool": "get_lm_tools"}</tool_call>
<tool_call>{"tool": "workspace_trust"}</tool_call>
<tool_call>{"tool": "custom_instructions"}</tool_call>

== VISION (image chat, requires VS Code 1.97+) ==
<tool_call>{"tool": "chat_with_image", "message": "Describe this UI screenshot", "image_path": "screenshots/ui.png"}</tool_call>

== DIAGNOSTICS ==
<tool_call>{"tool": "get_errors", "path": "file.py"}</tool_call>

== TERMINAL ==
<tool_call>{"tool": "create_terminal", "name": "Build"}</tool_call>
<tool_call>{"tool": "send_to_terminal", "name": "Build", "text": "npm run build"}</tool_call>
<tool_call>{"tool": "get_terminal_output", "name": "Build"}</tool_call>

== EDITOR ==
<tool_call>{"tool": "open_file", "path": "f.py", "line": 10}</tool_call>
<tool_call>{"tool": "insert_text", "text": "hello"}</tool_call>
<tool_call>{"tool": "get_selection"}</tool_call>

== NETWORK ==
<tool_call>{"tool": "fetch", "url": "https://api.example.com", "method": "GET"}</tool_call>

== NOTIFY ==
<tool_call>{"tool": "notify", "message": "Done!", "level": "info"}</tool_call>

== USER INTERACTION ==
<tool_call>{"tool": "ask_user", "question": "What should I name the file?"}</tool_call>

RULES:
1. Read before writing. Understand context before making changes.
2. For edit_file, include 2-3 lines of UNIQUE surrounding context in old_string.
3. Multiple independent tool calls = PARALLEL execution. Use this for speed.
4. CONFIRM before: deleting files, git push/commit, destructive actions.
5. After edits, verify with get_errors. Fix what you break.
6. When done, say "TASK COMPLETE" with a summary of what was done.
7. If stuck, explain what's blocking and ask the user.
"""

# Read-only tools that can run in parallel safely
_READ_ONLY_TOOLS = frozenset({
    "read_file", "list_directory", "search_text", "search_files",
    "find_files", "search_symbols", "find_definition", "find_usages",
    "hover", "code_actions", "document_symbols", "call_hierarchy",
    "git_status", "git_diff", "git_log", "git_branches",
    "get_errors", "get_selection", "get_terminal_output", "fetch",
    "open_file", "semantic_search", "workspace_index",
    "related_files", "import_graph",
    "get_lm_tools", "workspace_trust", "custom_instructions",
})

# Destructive tools requiring confirmation
_DESTRUCTIVE_TOOLS = frozenset({
    "delete_file", "git_commit", "git_stash", "git_checkout",
    "git_push", "git_merge",
})


class CopilotAgent:
    """Agentic assistant with parallel tools, context management, and safety guardrails."""

    VERSION = "5.1.5"

    def __init__(self, model: str = None, max_iterations: int = 25):
        self.client = CopilotBridge()
        if model:
            self.client.set_model(model)
        self.client.set_system_prompt(_AGENT_SYSTEM_PROMPT)
        self.max_iterations = max_iterations
        self.verbose = True
        self.confirm_destructive = True
        self._max_context_tokens = 100000  # Will be updated from model info
        self._token_budget_fraction = 0.75  # Use 75% of context for history
        self._retry_limit = 2

    # ── Context window management ─────────────────────────────────

    def _estimate_tokens(self, text: str) -> int:
        """Rough estimate: ~4 chars/token. Use server count for precision."""
        return len(text) // 4

    def _get_history_tokens(self) -> int:
        total = 0
        for msg in self.client.conversation_history:
            total += self._estimate_tokens(msg.get("content", ""))
        return total

    def _summarize_old_messages(self):
        """When context gets too large, summarize older messages to free space."""
        history = self.client.conversation_history
        if len(history) < 6:
            return  # Too few messages to summarize

        budget = int(self._max_context_tokens * self._token_budget_fraction)
        current_tokens = self._get_history_tokens()

        if current_tokens < budget:
            return

        # Keep the first message (task description) and last 4 messages
        # Summarize everything in between
        first_msg = history[0]
        recent = history[-4:]
        middle = history[1:-4]

        if not middle:
            return

        # Build a summary of what happened
        summary_parts = []
        for msg in middle:
            role = msg["role"]
            content = msg["content"]
            if role == "assistant":
                # Extract just the reasoning, skip tool calls
                text = re.sub(r'<tool_call>.*?</tool_call>', '[tool call]', content, flags=re.DOTALL)
                if len(text) > 200:
                    text = text[:200] + "..."
                summary_parts.append(f"Assistant: {text}")
            else:
                if len(content) > 300:
                    content = content[:300] + "..."
                summary_parts.append(f"Tool results: {content}")

        summary = "[CONVERSATION SUMMARY - older messages compressed]\n" + "\n".join(summary_parts)

        self.client.conversation_history = [
            first_msg,
            {"role": "user", "content": summary},
            {"role": "assistant", "content": "Understood. I have context from the summary. Continuing..."},
            *recent
        ]

        if self.verbose:
            new_tokens = self._get_history_tokens()
            print(f"  [Context compressed: {current_tokens} → {new_tokens} est. tokens]")

    # ── Tool extraction ───────────────────────────────────────────

    def _extract_tool_calls(self, response: str) -> List[Dict[str, Any]]:
        tool_calls = []
        for match in re.findall(r'<tool_call>(.*?)</tool_call>', response, re.DOTALL):
            text = match.strip()
            try:
                tool_calls.append(json.loads(text))
            except json.JSONDecodeError:
                # Try to fix common JSON issues: trailing commas, single quotes
                try:
                    fixed = re.sub(r',\s*}', '}', text)
                    fixed = re.sub(r',\s*]', ']', fixed)
                    tool_calls.append(json.loads(fixed))
                except json.JSONDecodeError as e:
                    if self.verbose:
                        print(f"  [Warning] Invalid tool JSON: {e}")
        return tool_calls

    # ── Safety guardrails ─────────────────────────────────────────

    def _check_safety(self, tc: Dict[str, Any]) -> Optional[str]:
        """Return a reason string if the action should be blocked, else None."""
        tool = tc.get("tool", "")

        if tool in _DESTRUCTIVE_TOOLS and self.confirm_destructive:
            if tool == "delete_file":
                return f"Delete file: {tc.get('path', '?')}"
            if tool == "git_commit":
                return f"Git commit: {tc.get('message', '?')}"
            if tool == "git_checkout":
                return f"Git checkout: {tc.get('branch', '?')}"
            if tool == "git_stash":
                return f"Git stash {tc.get('action', 'push')}"
            if tool == "git_push":
                return f"Git push {tc.get('remote', 'origin')} {tc.get('branch', '')}"
            if tool == "git_merge":
                return f"Git merge: {tc.get('branch', '?')}"

        return None

    def _confirm_action(self, reason: str) -> bool:
        """Ask user for confirmation. Returns True if approved."""
        try:
            answer = input(f"\n  ⚠ Confirm: {reason}? [y/N] ").strip().lower()
            return answer in ("y", "yes")
        except (EOFError, KeyboardInterrupt):
            return False

    # ── Tool execution (with retry) ───────────────────────────────

    def _execute_tool(self, tc: Dict[str, Any]) -> str:
        for attempt in range(1, self._retry_limit + 1):
            try:
                return self._execute_tool_inner(tc)
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt < self._retry_limit:
                    if self.verbose:
                        print(f"  [Retry {attempt}/{self._retry_limit}: {e}]")
                    time.sleep(0.5 * attempt)
                else:
                    return f"[Error in {tc.get('tool', '?')} after {self._retry_limit} attempts: {e}]"
            except Exception as e:
                return f"[Error in {tc.get('tool', '?')}: {e}]"
        return "[Unexpected retry exhaustion]"

    def _execute_tool_inner(self, tc: Dict[str, Any]) -> str:
        tool = tc.get("tool")
        c = self.client

        # FILE
        if tool == "read_file":
            content = c.read_file(tc["path"], tc.get("start_line"), tc.get("end_line"))
            lines = content.count(chr(10)) + 1
            return f"[{tc['path']} ({lines} lines)]\n{content}"
        if tool == "write_file":
            return f"[Wrote {c.write_file(tc['path'], tc['content'])}]"
        if tool == "edit_file":
            c.edit_file(tc["path"], tc["old_string"], tc["new_string"])
            return f"[Edited {tc['path']}]"
        if tool == "multi_edit":
            results = c.multi_edit(tc["edits"])
            ok = sum(1 for r in results if r.get("success"))
            return f"[Multi-edit: {ok}/{len(results)} succeeded]"
        if tool == "delete_file":
            c.delete_file(tc["path"])
            return f"[Deleted {tc['path']}]"
        if tool == "list_directory":
            items = c.list_directory(tc.get("path", "."))
            listing = "\n".join(f"{'D' if i.get('isDirectory') else 'F'} {i['name']}" for i in items[:50])
            return f"[{tc.get('path', '.')}]\n{listing}"

        # SEARCH
        if tool in ("search_text", "search_files"):
            results = c.search_text(tc["pattern"], tc.get("directory", "."), tc.get("file_pattern"))
            if not results:
                return "[No matches]"
            lines = "\n".join(f"{r['file']}:{r['line']}: {r['content'][:120]}" for r in results[:30])
            return f"[{len(results)} matches]\n{lines}"
        if tool == "find_files":
            files = c.find_files(tc["pattern"])
            return f"[{len(files)} files]\n" + "\n".join(files[:50])
        if tool == "search_symbols":
            syms = c.search_symbols(tc["query"])
            if not syms:
                return "[No symbols found]"
            lines = "\n".join(f"{s.get('kind','?')} {s['name']} @ {s.get('file','?')}:{s.get('line','?')}" for s in syms[:20])
            return f"[{len(syms)} symbols]\n{lines}"
        if tool == "find_definition":
            defs = c.find_definition(tc["path"], tc["line"], tc["character"])
            if not defs:
                return "[No definition found]"
            return "\n".join(f"Def: {d['file']}:{d['line']}:{d.get('character',0)}" for d in defs)
        if tool == "find_usages":
            usages = c.find_usages(tc["path"], tc["line"], tc["character"])
            if not usages:
                return "[No usages found]"
            lines = "\n".join(f"{u['file']}:{u['line']}:{u.get('character',0)}" for u in usages[:30])
            return f"[{len(usages)} usages]\n{lines}"

        # SEMANTIC SEARCH + WORKSPACE INDEX
        if tool == "semantic_search":
            result = c.semantic_search(tc["query"], tc.get("max_results", 20), tc.get("use_llm", True))
            if not result.get("success"):
                return f"[Semantic search error: {result.get('error', 'unknown')}]"
            results = result.get("results", [])
            if not results:
                return "[No semantic matches]"
            lines = "\n".join(
                f"  {r['path']} (score: {r['score']}) {r.get('reason', '')}" for r in results[:15]
            )
            meta = result.get("meta", {})
            return f"[Semantic: {len(results)} results from {meta.get('indexedFiles', '?')} indexed files]\n{lines}"
        if tool == "workspace_index":
            info = c.get_workspace_index()
            return (f"[Workspace index: {info.get('fileCount', 0)} files, "
                    f"{info.get('symbolCount', 0)} symbols, "
                    f"{info.get('importEdges', 0)} imports, "
                    f"status={info.get('status')}, "
                    f"built in {info.get('buildTimeMs', '?')}ms]")
        if tool == "related_files":
            result = c.get_related_files(tc["path"], tc.get("max_results", 10))
            if not result.get("success"):
                return f"[Related files error: {result.get('error', 'unknown')}]"
            related = result.get("related", [])
            if not related:
                return f"[No files related to {tc['path']}]"
            lines = "\n".join(f"  {r['path']} (score: {r['score']}) - {r['reason']}" for r in related)
            return f"[Related to {tc['path']}]\n{lines}"
        if tool == "import_graph":
            result = c.get_import_graph(tc.get("path"))
            if not result.get("success"):
                return f"[Import graph error: {result.get('error', 'unknown')}]"
            if tc.get("path"):
                imports = result.get("imports", [])
                imported_by = result.get("importedBy", [])
                symbols = result.get("symbols", [])
                return (f"[{tc['path']}]\n"
                        f"  Imports: {', '.join(imports[:20])}\n"
                        f"  Imported by: {', '.join(imported_by[:20])}\n"
                        f"  Symbols: {', '.join(symbols[:20])}")
            else:
                edges = result.get("edges", [])
                return f"[Import graph: {len(edges)} edges across {result.get('fileCount', 0)} files]"

        # WORKSPACE & COPILOT
        if tool == "get_lm_tools":
            tools = c.get_lm_tools()
            if not tools:
                return "[No LM tools registered (requires VS Code 1.99+)]"
            lines = "\n".join(f"  {t['name']}: {t.get('description','')[:80]}" for t in tools[:30])
            return f"[{len(tools)} LM tools]\n{lines}"
        if tool == "workspace_trust":
            info = c.get_workspace_trust()
            return f"[Workspace trust: {info.get('trustLevel', '?')} (trusted={info.get('trusted')})]"
        if tool == "custom_instructions":
            content = c.get_custom_instructions()
            if not content:
                return "[No .github/copilot-instructions.md found]"
            return f"[Custom instructions ({len(content)} chars)]\n{content[:3000]}"

        # VISION
        if tool == "chat_with_image":
            response = c.chat_with_image(
                message=tc.get("message", ""),
                image_path=tc["image_path"],
                model=tc.get("model"),
                temperature=tc.get("temperature"),
                max_tokens=tc.get("max_tokens"),
            )
            return f"[Image chat response]\n{response}"

        # CODE INTELLIGENCE
        if tool == "hover":
            hovers = c.hover(tc["path"], tc["line"], tc["character"])
            if not hovers:
                return "[No hover info]"
            contents = []
            for h in hovers:
                contents.extend(h.get("contents", []))
            return "[Hover]\n" + "\n---\n".join(contents[:5])
        if tool == "code_actions":
            actions = c.code_actions(tc["path"], tc["line"], tc.get("start_character", 0))
            if not actions:
                return "[No code actions]"
            return "[Code actions]\n" + "\n".join(f"- {a['title']} ({a.get('kind','')})" for a in actions[:20])
        if tool == "document_symbols":
            syms = c.document_symbols(tc["path"])
            if not syms:
                return "[No symbols]"
            return "[Document symbols]\n" + "\n".join(
                f"{s.get('kind','?')} {s['qualifiedName']} L{s['range']['start']['line']}" for s in syms[:40])
        if tool == "rename_symbol":
            result = c.rename_symbol(tc["path"], tc["line"], tc["character"], tc["new_name"])
            if result.get("success"):
                return f"[Renamed. Files changed: {', '.join(result.get('filesChanged', []))}]"
            return f"[Rename failed: {result.get('error', 'unknown')}]"
        if tool == "call_hierarchy":
            result = c.call_hierarchy(tc["path"], tc["line"], tc["character"], tc.get("direction", "both"))
            item = result.get("item")
            if not item:
                return "[No call hierarchy at this position]"
            parts = [f"[Call hierarchy for {item['name']}]"]
            for ic in result.get("incomingCalls", []):
                parts.append(f"  <- {ic['name']} ({ic['file']}:{ic['line']})")
            for oc in result.get("outgoingCalls", []):
                parts.append(f"  -> {oc['name']} ({oc['file']}:{oc['line']})")
            return "\n".join(parts)

        # COMMANDS
        if tool == "run_command":
            r = c.run_command(tc["command"], tc.get("cwd"), tc.get("timeout", 30000))
            out = r.get("stdout", "") + ("\n[STDERR]\n" + r.get("stderr", "") if r.get("stderr") else "")
            return f"[Exit {r.get('exitCode', 0)}]\n{out[:5000]}"
        if tool == "vscode_command":
            return f"[Result: {c.vscode_command(tc['command'], *tc.get('args', []))}]"

        # GIT
        if tool == "git_status":
            files = c.git_status()
            if not files:
                return "[Clean working tree]"
            return "[Git status]\n" + "\n".join(f"{f['status']} {f['file']}" for f in files[:30])
        if tool == "git_diff":
            diff = c.git_diff(tc.get("staged", False), tc.get("file"))
            return f"[Git diff]\n{diff[:10000]}" if diff else "[No changes]"
        if tool == "git_log":
            commits = c.git_log(tc.get("limit", 10))
            return "[Git log]\n" + "\n".join(f"{x['hash'][:8]} {x['message'][:60]}" for x in commits)
        if tool == "git_branches":
            branches = c.git_branches()
            return "[Branches]\n" + "\n".join(f"{'*' if b.get('current') else ' '} {b['name']}" for b in branches)
        if tool == "git_commit":
            result = c.git_commit(tc["message"], tc.get("all", False), tc.get("amend", False))
            return f"[Git commit {'OK' if result.get('success') else 'FAILED'}] {result.get('output', result.get('error', ''))}"
        if tool == "git_stash":
            result = c.git_stash(tc.get("action", "push"), tc.get("message"), tc.get("include_untracked", False))
            return f"[Git stash] {result.get('output', result.get('error', ''))}"
        if tool == "git_checkout":
            result = c.git_checkout(tc["branch"], tc.get("create", False))
            return f"[Git checkout {'OK' if result.get('success') else 'FAILED'}] {result.get('output', result.get('error', ''))}"
        if tool == "git_add":
            result = c.git_add(tc["files"])
            return f"[Git add {'OK' if result.get('success') else 'FAILED'}] {result.get('output', result.get('error', ''))}"
        if tool == "git_push":
            result = c.git_push(
                tc.get("remote", "origin"), tc.get("branch"),
                tc.get("set_upstream", False), tc.get("force", False),
                tc.get("tags", False),
            )
            return f"[Git push {'OK' if result.get('success') else 'FAILED'}] {result.get('output', result.get('error', ''))}"
        if tool == "git_pull":
            result = c.git_pull(tc.get("remote"), tc.get("branch"), tc.get("rebase", False))
            return f"[Git pull {'OK' if result.get('success') else 'FAILED'}] {result.get('output', result.get('error', ''))}"
        if tool == "git_merge":
            result = c.git_merge(
                tc["branch"], tc.get("no_ff", False),
                tc.get("squash", False), tc.get("message"),
            )
            return f"[Git merge {'OK' if result.get('success') else 'FAILED'}] {result.get('output', result.get('error', ''))}"

        # DIAGNOSTICS
        if tool == "get_errors":
            diags = c.get_diagnostics(tc.get("path"))
            if not diags:
                return "[No errors/warnings]"
            lines = "\n".join(f"{d.get('severity','Err')} {d.get('file','?')}:{d.get('line','?')}: {d['message']}" for d in diags[:30])
            return f"[{len(diags)} diagnostics]\n{lines}"

        # EDITOR
        if tool == "open_file":
            c.open_file(tc["path"], tc.get("line"), tc.get("character"))
            return f"[Opened {tc['path']}]"
        if tool == "insert_text":
            c.insert_text(tc["text"], tc.get("position", "cursor"), tc.get("line"), tc.get("character"))
            return "[Text inserted]"
        if tool == "get_selection":
            sel = c.get_selection()
            text = sel.get("selection", {}).get("text", "")
            return f"[Selection: {len(text)} chars]\n{text[:1000]}"

        # TERMINAL
        if tool == "create_terminal":
            name = c.create_terminal(tc.get("name", "Agent"), tc.get("cwd"))
            return f"[Created terminal: {name}]"
        if tool == "send_to_terminal":
            c.send_to_terminal(tc["name"], tc["text"])
            return f"[Sent to terminal '{tc['name']}']"
        if tool == "get_terminal_output":
            output = c.get_terminal_output(tc["name"], tc.get("last_lines"), tc.get("clear", False))
            return f"[Terminal output]\n{output[:5000]}" if output else "[No output captured]"

        # NETWORK
        if tool == "fetch":
            r = c.fetch(tc["url"], tc.get("method", "GET"), tc.get("headers"), tc.get("body"))
            return f"[HTTP {r.get('status', '?')}]\n{r.get('body', '')[:5000]}"

        # NOTIFICATIONS
        if tool == "notify":
            msg = tc["message"]
            {"warn": c.notify_warn, "error": c.notify_error}.get(tc.get("level", "info"), c.notify_info)(msg)
            return f"[Notified: {msg}]"

        # ASK USER
        if tool == "ask_user":
            print(f"\n  [Agent asks]: {tc['question']}")
            try:
                return f"[User answered]: {input('  Your answer: ').strip()}"
            except (EOFError, KeyboardInterrupt):
                return "[User cancelled]"

        return f"[Unknown tool: {tool}]"

    # ── Parallel tool execution ───────────────────────────────────

    def _execute_tools_parallel(self, tool_calls: List[Dict[str, Any]]) -> List[str]:
        """Execute independent (read-only) tools in parallel, mutating tools sequentially."""
        # Separate into parallel-safe and sequential
        parallel_batch: List[Dict[str, Any]] = []
        sequential_batch: List[Dict[str, Any]] = []

        for tc in tool_calls:
            tool = tc.get("tool", "")
            if tool in _READ_ONLY_TOOLS:
                parallel_batch.append(tc)
            else:
                sequential_batch.append(tc)

        results: List[str] = []

        # Run read-only tools in parallel
        if parallel_batch:
            with ThreadPoolExecutor(max_workers=min(len(parallel_batch), 8)) as pool:
                futures = {pool.submit(self._execute_tool, tc): i for i, tc in enumerate(parallel_batch)}
                indexed_results: Dict[int, str] = {}
                for future in as_completed(futures):
                    idx = futures[future]
                    try:
                        indexed_results[idx] = future.result()
                    except Exception as e:
                        indexed_results[idx] = f"[Parallel error: {e}]"
                for i in range(len(parallel_batch)):
                    result = indexed_results.get(i, "[Missing result]")
                    if self.verbose:
                        tool_name = parallel_batch[i].get("tool", "?")
                        print(f"  > {tool_name} (parallel)...\n{result}\n")
                    results.append(result)

        # Run mutating tools sequentially
        for tc in sequential_batch:
            tool_name = tc.get("tool", "?")

            # Safety check
            reason = self._check_safety(tc)
            if reason:
                if not self._confirm_action(reason):
                    results.append(f"[BLOCKED by user: {reason}]")
                    if self.verbose:
                        print(f"  > {tool_name} BLOCKED")
                    continue

            if self.verbose:
                print(f"  > {tool_name}...")
            result = self._execute_tool(tc)
            if self.verbose:
                print(f"{result}\n")
            results.append(result)

        return results

    # ── Agent loop ────────────────────────────────────────────────

    def run(self, task: str) -> str:
        """Run the agent to complete a task. Returns final response."""
        if not self.client.is_available():
            raise ConnectionError("Copilot Bridge not running. Reload VS Code.")

        # Get workspace info and model limits
        workspace = self.client.get_workspace()
        try:
            models = self.client.get_models()
            if models:
                max_input = models[0].get("maxInputTokens", 100000)
                self._max_context_tokens = max_input
        except Exception:
            pass

        full_task = (
            f"Workspace root: {workspace.get('root', '?')}\n"
            f"Folders: {', '.join(f.get('name', '?') for f in workspace.get('folders', []))}\n\n"
            f"Task: {task}"
        )

        if self.verbose:
            print(f"\n{'='*60}")
            print(f"Agent v{self.VERSION} | Max iterations: {self.max_iterations}")
            print(f"Task: {task}")
            print(f"{'='*60}\n")

        self.client.clear_history()

        for iteration in range(1, self.max_iterations + 1):
            if self.verbose:
                print(f"--- Iteration {iteration}/{self.max_iterations} ---")

            # Context management: summarize if too large
            self._summarize_old_messages()

            # Send task on first iteration, empty "Continue." on subsequent
            prompt = full_task if iteration == 1 else "Continue with the task."
            response = self.client.chat(prompt)

            if self.verbose:
                clean_response = re.sub(r'<tool_call>.*?</tool_call>', '', response, flags=re.DOTALL).strip()
                if clean_response:
                    print("\nAssistant:\n" + clean_response + "\n")

            # Check for completion
            if "TASK COMPLETE" in response:
                if self.verbose:
                    print("\n✓ Task completed")
                return re.sub(r'<tool_call>.*?</tool_call>', '', response, flags=re.DOTALL).strip()

            # Extract and execute tools
            tool_calls = self._extract_tool_calls(response)
            if not tool_calls:
                if self.verbose:
                    print("[No tools called — ending]")
                return re.sub(r'<tool_call>.*?</tool_call>', '', response, flags=re.DOTALL).strip()

            if self.verbose:
                print(f"  [{len(tool_calls)} tool(s) to execute]")

            tool_results = self._execute_tools_parallel(tool_calls)

            # Feed results back
            results_text = "\n".join(tool_results)
            # Truncate if tool results are massive
            if len(results_text) > 50000:
                results_text = results_text[:50000] + "\n\n[Output truncated — too large]"

            self.client.conversation_history.append({
                "role": "user",
                "content": f"Tool results:\n{results_text}\n\nContinue."
            })

        if self.verbose:
            print(f"\nMax iterations ({self.max_iterations}) reached")
        return "Max iterations reached. Task may be incomplete."


# ╔══════════════════════════════════════════════════════════════════╗
# ║                    CONVENIENCE FUNCTIONS                        ║
# ╚══════════════════════════════════════════════════════════════════╝

def ask_copilot(message: str, system_prompt: Optional[str] = None, model: str = None) -> str:
    """One-liner: ask Copilot a question and get a string back."""
    client = CopilotBridge()
    if system_prompt: client.set_system_prompt(system_prompt)
    if model:         client.set_model(model)
    return client.ask(message)


# ╔══════════════════════════════════════════════════════════════════╗
# ║                         CLI MODES                               ║
# ╚══════════════════════════════════════════════════════════════════╝

def _cli_chat():
    """Interactive chat mode."""
    client = CopilotBridge()

    print("=" * 60)
    print("Copilot Bridge v5 - Interactive Chat")
    print("=" * 60)

    if not client.is_available():
        print("\nERROR: Bridge not running. Reload VS Code.")
        return

    print("\nConnected!")
    try:
        models = client.get_models()
        print(f"Models: {len(models)}")
        for m in models[:5]:
            print(f"  - {m.get('name', m.get('id'))}")
    except Exception:
        pass

    print("\nCommands: quit | clear | models | status")
    print("-" * 60)

    while True:
        try:
            user_input = input("\nYou: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!"); break

        if not user_input: continue
        cmd = user_input.lower()
        if cmd == "quit":
            print("Goodbye!"); break
        if cmd == "clear":
            client.clear_history(); print("[History cleared]"); continue
        if cmd == "models":
            for m in client.get_models(): print(f"  - {m.get('id')}: {m.get('name')}")
            continue
        if cmd == "status":
            print(f"Health: {client.get_health()}")
            print(f"Workspace: {client.get_workspace()}")
            continue

        try:
            print("\nCopilot: ", end="", flush=True)
            print(client.chat(user_input))
        except Exception as e:
            print(f"\nError: {e}")


def _cli_agent(task: str = None):
    """Interactive or one-shot agent mode."""
    agent = CopilotAgent()

    if not agent.client.is_available():
        print("ERROR: Bridge not running. Reload VS Code.")
        sys.exit(1)

    if task:
        result = agent.run(task)
        print(f"\n{'='*60}\nRESULT:\n{'='*60}\n{result}")
        return

    print("=" * 60)
    print("Copilot Agent v5 - Interactive Mode")
    print("=" * 60)
    print("Enter tasks. Type 'quit' to exit.\n")

    while True:
        try:
            task = input("Task: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!"); break

        if not task: continue
        if task.lower() == "quit":
            print("Goodbye!"); break

        try:
            agent.client.clear_history()
            agent.plan = []
            agent.run(task)
        except Exception as e:
            print(f"\nError: {e}")


def _cli_status():
    """Show connection status."""
    client = CopilotBridge()
    print(f"Available: {client.is_available()}")
    if client.is_available():
        health = client.get_health()
        ws = client.get_workspace()
        models = client.get_models()
        print(f"Version:   {health.get('version', '?')}")
        print(f"Workspace: {ws.get('root', '?')}")
        print(f"Models:    {len(models)}")
        for m in models[:5]:
            print(f"  - {m.get('id')}: {m.get('name')}")


def _print_usage():
    print("""
Copilot Bridge v5 - Unified CLI
================================
Usage:
  python copilot_bridge.py                     Interactive chat
  python copilot_bridge.py chat                Interactive chat
  python copilot_bridge.py agent               Interactive agent
  python copilot_bridge.py agent "do X"        One-shot agent task
  python copilot_bridge.py ask "question"      One-shot question
  python copilot_bridge.py status              Show connection status

As library:
  from copilot_bridge import CopilotBridge, CopilotAgent, ask_copilot
""")


def main():
    """Single entry point - routes to the right mode."""
    args = sys.argv[1:]

    if not args:
        _cli_chat()
        return

    mode = args[0].lower()

    if mode in ("chat", "c"):
        _cli_chat()
    elif mode in ("agent", "a"):
        task = " ".join(args[1:]) if len(args) > 1 else None
        _cli_agent(task)
    elif mode in ("ask", "q"):
        if len(args) < 2:
            print("Usage: python copilot_bridge.py ask \"your question\"")
            sys.exit(1)
        question = " ".join(args[1:])
        print(ask_copilot(question))
    elif mode in ("status", "s"):
        _cli_status()
    elif mode in ("help", "-h", "--help"):
        _print_usage()
    else:
        # Treat entire args as a question
        print(ask_copilot(" ".join(args)))


if __name__ == "__main__":
    main()

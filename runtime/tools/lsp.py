from __future__ import annotations

import hashlib
import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import ClassVar
from urllib.parse import unquote, urlparse

from runtime.tools.fs import (
    SKIP_NAMES,
    WorkspacePathError,
    relative_posix,
    resolve_in_workspace,
)

INDEX_FILE_CAP = 500
LOCATION_MAX = 20
REFERENCES_MAX = 50
SYMBOL_MAX = 200


class LSPTimeoutError(RuntimeError):
    pass


class LSPClient:
    def __init__(self, cmd: list[str], cwd: str):
        self.proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if (
            self.proc.stdin is None
            or self.proc.stdout is None
            or self.proc.stderr is None
        ):
            self.proc.kill()
            raise RuntimeError("LSP server pipes failed to open")
        self._stdin = self.proc.stdin
        self._stdout = self.proc.stdout
        self._stderr = self.proc.stderr
        self._pending: dict[int, queue.Queue[dict]] = {}
        self._notifications: queue.Queue[dict] = queue.Queue()
        self._next_id = 1
        self._id_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._alive = True
        threading.Thread(
            target=self._reader_loop, daemon=True, name=f"lsp-reader-{cmd[0]}"
        ).start()
        threading.Thread(
            target=self._stderr_drain, daemon=True, name=f"lsp-stderr-{cmd[0]}"
        ).start()

    @staticmethod
    def _read_message(stream) -> dict | None:
        headers = {}
        while True:
            line = stream.readline()
            if not line:
                return None
            if line in (b"\r\n", b"\n", b""):
                break
            if b":" in line:
                key, _, value = line.decode("ascii", "replace").partition(":")
                headers[key.strip().lower()] = value.strip()
        length = int(headers.get("content-length", 0))
        body = b""
        while len(body) < length:
            chunk = stream.read(length - len(body))
            if not chunk:
                return None
            body += chunk
        return json.loads(body.decode("utf-8"))

    def _write_message(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        try:
            with self._write_lock:
                self._stdin.write(header + body)
                self._stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise RuntimeError(f"LSP server process died: {exc}") from exc

    def _reader_loop(self) -> None:
        while self._alive:
            try:
                msg = self._read_message(self._stdout)
            except (OSError, ValueError, json.JSONDecodeError):
                break
            if msg is None:
                break
            if "id" in msg and ("result" in msg or "error" in msg):
                pending = self._pending.pop(msg["id"], None)
                if pending is not None:
                    pending.put(msg)
            elif "method" in msg:
                self._notifications.put(msg)
        self._alive = False

    def _stderr_drain(self) -> None:
        for _ in iter(self._stderr.readline, b""):
            pass

    def request(self, method: str, params: dict, timeout: float = 15.0):
        if not self._alive:
            raise RuntimeError("LSP server is not running")
        with self._id_lock:
            msg_id = self._next_id
            self._next_id += 1
        pending: queue.Queue[dict] = queue.Queue()
        self._pending[msg_id] = pending
        self._write_message(
            {"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}
        )
        try:
            response = pending.get(timeout=timeout)
        except queue.Empty:
            self._pending.pop(msg_id, None)
            raise LSPTimeoutError(f"{method} timed out after {timeout}s")
        if "error" in response:
            raise RuntimeError(f"{method} error: {response['error']}")
        return response.get("result")

    def notify(self, method: str, params: dict) -> None:
        self._write_message({"jsonrpc": "2.0", "method": method, "params": params})

    def reply(self, msg_id, result=None) -> None:
        self._write_message({"jsonrpc": "2.0", "id": msg_id, "result": result})

    def get_notification(self, timeout: float = 0.0) -> dict | None:
        try:
            return self._notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def shutdown(self) -> None:
        if not self._alive:
            return
        try:
            self.request("shutdown", {}, timeout=5.0)
            self.notify("exit", {})
        except (LSPTimeoutError, RuntimeError, OSError, BrokenPipeError):
            pass
        self._alive = False
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5.0)
        except (OSError, subprocess.TimeoutExpired):
            try:
                self.proc.kill()
            except OSError:
                pass


class LSPManager:
    @dataclass
    class ServerConfig:
        language: str
        cmd: list[str]
        language_id: str
        extensions: tuple[str, ...]
        init_options: dict = field(default_factory=dict)
        settings: dict = field(default_factory=dict)

    SERVER_CONFIGS: ClassVar[dict[str, ServerConfig]] = {
        "python": ServerConfig(
            language="python",
            cmd=["npx", "-y", "-p", "pyright", "pyright-langserver", "--stdio"],
            language_id="python",
            extensions=(".py", ".pyi"),
            settings={
                "python": {
                    "analysis": {
                        "diagnosticMode": "workspace",
                        "autoSearchPaths": True,
                        "useLibraryCodeForTypes": True,
                    }
                }
            },
        ),
        "typescript": ServerConfig(
            language="typescript",
            cmd=["npx", "-y", "typescript-language-server", "--stdio"],
            language_id="typescript",
            extensions=(".ts", ".tsx", ".mts", ".cts"),
        ),
        "javascript": ServerConfig(
            language="javascript",
            cmd=["npx", "-y", "typescript-language-server", "--stdio"],
            language_id="javascript",
            extensions=(".js", ".jsx", ".mjs", ".cjs"),
        ),
        "go": ServerConfig(
            language="go",
            cmd=["gopls", "serve"],
            language_id="go",
            extensions=(".go",),
        ),
    }

    VALID_ACTIONS: ClassVar[dict[str, str]] = {
        "references": "textDocument/references",
        "definition": "textDocument/definition",
        "hover": "textDocument/hover",
    }

    def __init__(self, workspace_root: str | Path):
        self.root = str(Path(workspace_root).resolve())
        self.root_uri = self._path_to_uri(self.root)
        self._clients: dict[tuple[str, ...], LSPClient] = {}
        self._opened_files: set[str] = set()
        self._diagnostics: dict[str, list[dict]] = {}
        self._versions: dict[str, int] = {}
        self._diag_seq: dict[str, int] = {}
        # sha256 of the text last handed to the server, so an out-of-band
        # change on disk is detectable without trusting mtime.
        self._sent_sha: dict[str, str] = {}
        self._lock = threading.Lock()
        self._closed = False

    @staticmethod
    def _path_to_uri(path: str) -> str:
        return Path(path).resolve().as_uri()

    @classmethod
    def _config_for_extension(cls, ext: str) -> ServerConfig | None:
        for cfg in cls.SERVER_CONFIGS.values():
            if ext in cfg.extensions:
                return cfg
        return None

    @staticmethod
    def _language_id(cfg: ServerConfig, ext: str) -> str:
        if ext == ".tsx":
            return "typescriptreact"
        if ext == ".jsx":
            return "javascriptreact"
        return cfg.language_id

    def _client_for(self, cfg: ServerConfig) -> LSPClient:
        if self._closed:
            raise RuntimeError("LSP manager is shut down")
        key = tuple(cfg.cmd)
        with self._lock:
            client = self._clients.get(key)
            if client is not None:
                return client

        client = LSPClient(cfg.cmd, cwd=self.root)
        client.request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": self.root_uri,
                "rootPath": self.root,
                "workspaceFolders": [
                    {"uri": self.root_uri, "name": Path(self.root).name}
                ],
                "capabilities": {
                    "workspace": {
                        "workspaceFolders": True,
                        "configuration": True,
                        "didChangeConfiguration": {"dynamicRegistration": False},
                    },
                    "window": {"workDoneProgress": True},
                    "textDocument": {
                        "synchronization": {"didSave": True, "didChange": True},
                        "publishDiagnostics": {"relatedInformation": True},
                        "definition": {},
                        "references": {},
                        "hover": {},
                        "rename": {"prepareSupport": True},
                        "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                    },
                },
                "initializationOptions": cfg.init_options,
            },
            timeout=30.0,
        )
        threading.Thread(
            target=self._notification_listener,
            args=(client, cfg),
            daemon=True,
            name=f"lsp-notify-{cfg.language}",
        ).start()
        client.notify("initialized", {})
        if cfg.settings:
            client.notify(
                "workspace/didChangeConfiguration", {"settings": cfg.settings}
            )
        with self._lock:
            if self._closed:
                client.shutdown()
                raise RuntimeError("LSP manager is shut down")
            existing = self._clients.get(key)
            if existing is not None:
                client.shutdown()
                return existing
            self._clients[key] = client
        return client

    def _notification_listener(self, client: LSPClient, cfg: ServerConfig) -> None:
        while client._alive and not self._closed:
            note = client.get_notification(timeout=0.5)
            if note is None:
                continue
            if "id" in note and "method" in note:
                self._handle_server_request(client, cfg, note)
                continue
            if note.get("method") == "textDocument/publishDiagnostics":
                params = note.get("params", {})
                uri = params.get("uri", "<unknown>")
                with self._lock:
                    self._diagnostics[uri] = params.get("diagnostics", [])
                    self._diag_seq[uri] = self._diag_seq.get(uri, 0) + 1

    @staticmethod
    def _settings_section(settings: dict, section: str | None):
        if not section:
            return settings
        node = settings
        for part in section.split("."):
            if not isinstance(node, dict) or part not in node:
                return None
            node = node[part]
        return node

    def _handle_server_request(self, client: LSPClient, cfg: ServerConfig, note: dict) -> None:
        method = note.get("method")
        msg_id = note["id"]
        params = note.get("params") or {}
        if method == "workspace/configuration":
            items = params.get("items") or [{}]
            result = [
                self._settings_section(cfg.settings, item.get("section"))
                for item in items
            ]
            client.reply(msg_id, result)
        elif method == "workspace/workspaceFolders":
            client.reply(
                msg_id,
                [{"uri": self.root_uri, "name": Path(self.root).name}],
            )
        else:
            client.reply(msg_id, None)

    def shutdown_all(self) -> None:
        self._closed = True
        clients = []
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
            self._opened_files.clear()
            self._sent_sha.clear()
        for client in clients:
            client.shutdown()

    @staticmethod
    def _warm_languages(language: str) -> list[str]:
        if language in ("javascript", "typescript"):
            return ["javascript", "typescript"]
        if language in LSPManager.SERVER_CONFIGS:
            return [language]
        return []

    def warm_start(self, language: str) -> int | None:
        names = self._warm_languages(language)
        if not names:
            return None
        opened = 0
        for name in names:
            cfg = self.SERVER_CONFIGS[name]
            self._client_for(cfg)
            opened += self.index_workspace(name)
        self._wait_for_diagnostics(opened)
        return opened

    def _wait_for_diagnostics(self, opened: int, timeout: float = 20.0) -> None:
        if opened == 0:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._closed:
            with self._lock:
                count = len(self._diagnostics)
            if count >= opened:
                break
            time.sleep(0.2)

    def _iter_source_files(self, extensions: tuple[str, ...], max_files: int):
        count = 0
        for root, dirs, files in os.walk(self.root):
            dirs[:] = [
                name
                for name in dirs
                if name not in SKIP_NAMES and not name.startswith(".")
            ]
            for fname in files:
                if Path(fname).suffix not in extensions:
                    continue
                full = os.path.join(root, fname)
                rel = os.path.relpath(full, self.root)
                if os.sep != "/":
                    rel = rel.replace(os.sep, "/")
                yield rel
                count += 1
                if count >= max_files:
                    return

    def _did_open(self, rel_path: str, cfg: ServerConfig | None = None) -> str | None:
        full_path = str(Path(self.root, rel_path).resolve())
        uri = self._path_to_uri(full_path)
        with self._lock:
            if full_path in self._opened_files:
                return uri
        ext = Path(full_path).suffix
        cfg = cfg or self._config_for_extension(ext)
        if cfg is None:
            return None
        client = self._client_for(cfg)
        text = Path(full_path).read_text(errors="replace")
        client.notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": self._language_id(cfg, ext),
                    "version": 1,
                    "text": text,
                }
            },
        )
        with self._lock:
            self._opened_files.add(full_path)
            self._versions.setdefault(full_path, 1)
            self._sent_sha[full_path] = self._text_sha(text)
        return uri

    def index_workspace(self, language: str, max_files: int = INDEX_FILE_CAP) -> int:
        cfg = self.SERVER_CONFIGS.get(language)
        if cfg is None:
            return 0
        opened = 0
        for rel in self._iter_source_files(cfg.extensions, max_files):
            try:
                if self._did_open(rel, cfg) is not None:
                    opened += 1
            except OSError:
                continue
        return opened

    def open_file_and_get_diagnostics(
        self, rel_path: str, wait_timeout: float = 5.0
    ) -> list:
        full_path = str(Path(self.root, rel_path).resolve())
        uri = self._path_to_uri(full_path)
        with self._lock:
            already_open = full_path in self._opened_files
        if already_open:
            # The file may have changed outside the edit funnel (the user's
            # editor, a git checkout, a build step). Returning the cached
            # entry there would report diagnostics for content the server no
            # longer has, with nothing marking them stale.
            stale = self._stale_disk_text(full_path)
            if stale is not None:
                return self.diagnostics_after_change(
                    rel_path, stale, timeout=wait_timeout
                )
            with self._lock:
                return list(self._diagnostics.get(uri, []))
        ext = Path(full_path).suffix
        cfg = self._config_for_extension(ext)
        if cfg is None:
            raise ValueError(f"No LSP server configured for extension '{ext}'")
        self._did_open(rel_path, cfg)
        deadline = time.monotonic() + wait_timeout
        while time.monotonic() < deadline:
            if uri in self._diagnostics:
                return self._diagnostics[uri]
            time.sleep(0.1)
        self._diagnostics.setdefault(uri, [])
        return self._diagnostics[uri]

    def ask_lsp(self, rel_path: str, line: int, character: int, action: str):
        if action not in self.VALID_ACTIONS:
            raise ValueError(f"action must be one of {list(self.VALID_ACTIONS)}")
        full_path = str(Path(self.root, rel_path).resolve())
        if full_path not in self._opened_files:
            self.open_file_and_get_diagnostics(rel_path)
        else:
            self.sync_if_stale(rel_path)
        ext = Path(full_path).suffix
        cfg = self._config_for_extension(ext)
        if cfg is None:
            raise ValueError(f"No LSP server configured for extension '{ext}'")
        client = self._client_for(cfg)
        uri = self._path_to_uri(full_path)
        params = {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        }
        if action == "references":
            params["context"] = {"includeDeclaration": True}
        return client.request(self.VALID_ACTIONS[action], params)

    def ask_document_symbols(self, rel_path: str):
        full_path = str(Path(self.root, rel_path).resolve())
        if full_path not in self._opened_files:
            self.open_file_and_get_diagnostics(rel_path)
        else:
            self.sync_if_stale(rel_path)
        ext = Path(full_path).suffix
        cfg = self._config_for_extension(ext)
        if cfg is None:
            raise ValueError(f"No LSP server configured for extension '{ext}'")
        client = self._client_for(cfg)
        uri = self._path_to_uri(full_path)
        return client.request(
            "textDocument/documentSymbol",
            {"textDocument": {"uri": uri}},
        )

    def cached_diagnostics(self, rel_path: str) -> list:
        full_path = str(Path(self.root, rel_path).resolve())
        uri = self._path_to_uri(full_path)
        with self._lock:
            return list(self._diagnostics.get(uri, []))

    @staticmethod
    def _text_sha(text: str) -> str:
        return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()

    @staticmethod
    def _disk_text(full_path: str) -> str | None:
        """Read with the same newline normalization used when opening."""
        try:
            return Path(full_path).read_text(errors="replace")
        except OSError:
            return None

    def _stale_disk_text(self, full_path: str) -> str | None:
        """Disk text, when it differs from what the server was last sent."""
        with self._lock:
            if full_path not in self._opened_files:
                return None
            sent = self._sent_sha.get(full_path)
        text = self._disk_text(full_path)
        if text is None:
            return None
        if sent is not None and self._text_sha(text) == sent:
            return None
        return text

    def sync_if_stale(self, rel_path: str) -> bool:
        """Push disk content when the server's view is out of date.

        Notification only, with no diagnostics wait: the server processes
        messages in order, so a request sent after this returns is answered
        against the updated content.
        """
        full_path = str(Path(self.root, rel_path).resolve())
        text = self._stale_disk_text(full_path)
        if text is None:
            return False
        return self.did_change(rel_path, text) is not None

    def did_change(self, rel_path: str, new_text: str) -> str | None:
        full_path = str(Path(self.root, rel_path).resolve())
        ext = Path(full_path).suffix
        cfg = self._config_for_extension(ext)
        if cfg is None:
            return None
        uri = self._path_to_uri(full_path)
        with self._lock:
            already_open = full_path in self._opened_files
        if not already_open:
            client = self._client_for(cfg)
            client.notify(
                "textDocument/didOpen",
                {
                    "textDocument": {
                        "uri": uri,
                        "languageId": self._language_id(cfg, ext),
                        "version": 1,
                        "text": new_text,
                    }
                },
            )
            with self._lock:
                self._opened_files.add(full_path)
                self._versions[full_path] = 1
                self._sent_sha[full_path] = self._text_sha(new_text)
                self._diagnostics.pop(uri, None)
            return uri
        client = self._client_for(cfg)
        with self._lock:
            version = self._versions.get(full_path, 1) + 1
            self._versions[full_path] = version
            self._sent_sha[full_path] = self._text_sha(new_text)
            self._diagnostics.pop(uri, None)
        client.notify(
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": new_text}],
            },
        )
        return uri

    def diagnostics_after_change(
        self, rel_path: str, new_text: str, timeout: float = 5.0
    ) -> list:
        full_path = str(Path(self.root, rel_path).resolve())
        uri = self._path_to_uri(full_path)
        with self._lock:
            seq_before = self._diag_seq.get(uri, 0)
        if self.did_change(rel_path, new_text) is None:
            return []
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not self._closed:
            with self._lock:
                if self._diag_seq.get(uri, 0) > seq_before:
                    return list(self._diagnostics.get(uri, []))
            time.sleep(0.1)
        with self._lock:
            return list(self._diagnostics.get(uri, []))

    def ask_rename(self, rel_path: str, line: int, character: int, new_name: str):
        full_path = str(Path(self.root, rel_path).resolve())
        if full_path not in self._opened_files:
            self.open_file_and_get_diagnostics(rel_path)
        ext = Path(full_path).suffix
        cfg = self._config_for_extension(ext)
        if cfg is None:
            raise ValueError(f"No LSP server configured for extension '{ext}'")
        client = self._client_for(cfg)
        uri = self._path_to_uri(full_path)
        self.sync_if_stale(rel_path)
        return client.request(
            "textDocument/rename",
            {
                "textDocument": {"uri": uri},
                "position": {"line": line, "character": character},
                "newName": new_name,
            },
        )


SYMBOL_KINDS = {
    1: "File",
    2: "Module",
    3: "Namespace",
    4: "Package",
    5: "Class",
    6: "Method",
    7: "Property",
    8: "Field",
    9: "Constructor",
    10: "Enum",
    11: "Interface",
    12: "Function",
    13: "Variable",
    14: "Constant",
    15: "String",
    16: "Number",
    17: "Boolean",
    18: "Array",
    19: "Object",
    20: "Key",
    21: "Null",
    22: "EnumMember",
    23: "Struct",
    24: "Event",
    25: "Operator",
    26: "TypeParameter",
}


def _uri_to_path(uri: str) -> str:
    return unquote(urlparse(uri).path)


def _rel_from_uri(workspace: Path, uri: str) -> str:
    full = Path(_uri_to_path(uri))
    try:
        return relative_posix(workspace, full)
    except (ValueError, WorkspacePathError):
        return str(full)


def _normalize_locations(result) -> list[tuple[str, dict]]:
    if not result:
        return []
    items = [result] if isinstance(result, dict) else result
    locs = []
    for item in items:
        if "targetUri" in item:
            uri = item["targetUri"]
            rng = item.get("targetSelectionRange") or item["targetRange"]
        else:
            uri = item["uri"]
            rng = item["range"]
        locs.append((uri, rng))
    return locs


def _format_locations(
    workspace: Path, locs: list[tuple[str, dict]], max_results: int
) -> str:
    if not locs:
        return "No results."
    out = []
    for uri, rng in locs[:max_results]:
        rel = _rel_from_uri(workspace, uri)
        start = rng.get("start", {})
        line_no = start.get("line", -1) + 1
        col_no = start.get("character", -1) + 1
        snippet = ""
        try:
            full = _uri_to_path(uri)
            with open(full, encoding="utf-8", errors="replace") as handle:
                file_lines = handle.readlines()
            if 0 <= start.get("line", -1) < len(file_lines):
                snippet = file_lines[start["line"]].strip()
        except OSError:
            pass
        entry = f"{rel}:{line_no}:{col_no}"
        if snippet:
            entry += f"  {snippet}"
        out.append(entry)
    if len(locs) > max_results:
        out.append(f"... and {len(locs) - max_results} more")
    return "\n".join(out)


def _extract_hover_text(contents) -> str:
    if contents is None:
        return ""
    if isinstance(contents, str):
        return contents
    if isinstance(contents, dict):
        return contents.get("value", "")
    if isinstance(contents, list):
        parts = [
            item if isinstance(item, str) else item.get("value", "")
            for item in contents
        ]
        return "\n\n".join(part for part in parts if part)
    return ""


def _resolve(workspace: Path, path: str) -> str | None:
    try:
        resolve_in_workspace(workspace, path)
    except WorkspacePathError as exc:
        return f"error: {exc}"
    return None


def goto_definition(workspace: Path, lsp: LSPManager, path: str, line: int, character: int) -> str:
    err = _resolve(workspace, path)
    if err:
        return err
    try:
        result = lsp.ask_lsp(path, line - 1, character - 1, "definition")
        locs = _normalize_locations(result)
        if not locs:
            return f"No definition found at {path}:{line}:{character}"
        return _format_locations(workspace, locs, LOCATION_MAX)
    except (LSPTimeoutError, RuntimeError, ValueError, OSError) as exc:
        return f"error: finding definition in '{path}' at {line}:{character}: {exc}"


def find_references(workspace: Path, lsp: LSPManager, path: str, line: int, character: int) -> str:
    err = _resolve(workspace, path)
    if err:
        return err
    try:
        result = lsp.ask_lsp(path, line - 1, character - 1, "references")
        locs = _normalize_locations(result)
        if not locs:
            return f"No references found at {path}:{line}:{character}"
        return _format_locations(workspace, locs, REFERENCES_MAX)
    except (LSPTimeoutError, RuntimeError, ValueError, OSError) as exc:
        return f"error: finding references in '{path}' at {line}:{character}: {exc}"


def hover(workspace: Path, lsp: LSPManager, path: str, line: int, character: int) -> str:
    err = _resolve(workspace, path)
    if err:
        return err
    try:
        result = lsp.ask_lsp(path, line - 1, character - 1, "hover")
        text = _extract_hover_text((result or {}).get("contents"))
        if text and text.strip():
            return text.strip()
        return f"No hover information at {path}:{line}:{character}"
    except (LSPTimeoutError, RuntimeError, ValueError, OSError) as exc:
        return f"error: getting hover info in '{path}' at {line}:{character}: {exc}"


def get_diagnostics(workspace: Path, lsp: LSPManager, path: str) -> str:
    err = _resolve(workspace, path)
    if err:
        return err
    try:
        diags = lsp.open_file_and_get_diagnostics(path)
        if not diags:
            return f"No diagnostics for '{path}' (clean)."
        names = {1: "Error", 2: "Warning", 3: "Info", 4: "Hint"}
        out = []
        for item in diags:
            start = item.get("range", {}).get("start", {})
            line_no = start.get("line", -1) + 1
            col_no = start.get("character", -1) + 1
            sev = names.get(item.get("severity"), "Diagnostic")
            msg = (item.get("message") or "").strip()
            source = item.get("source")
            prefix = f"[{source}] " if source else ""
            out.append(f"{path}:{line_no}:{col_no} {sev}: {prefix}{msg}")
        return "\n".join(out)
    except (LSPTimeoutError, RuntimeError, ValueError, OSError) as exc:
        return f"error: getting diagnostics for '{path}': {exc}"


def _format_document_symbols(items, depth: int, lines: list[str]) -> None:
    if not items or len(lines) >= SYMBOL_MAX:
        return
    for item in items:
        if len(lines) >= SYMBOL_MAX:
            return
        kind = SYMBOL_KINDS.get(item.get("kind"), "Symbol")
        name = item.get("name") or ""
        if "location" in item:
            start = item["location"].get("range", {}).get("start", {})
        else:
            rng = item.get("selectionRange") or item.get("range") or {}
            start = rng.get("start", {})
        line_no = start.get("line", -1) + 1
        col_no = start.get("character", -1) + 1
        detail = item.get("detail")
        extra = f"  {detail}" if detail else ""
        lines.append(f"{'  ' * depth}{kind} {name}  {line_no}:{col_no}{extra}")
        children = item.get("children") or []
        _format_document_symbols(children, depth + 1, lines)


def document_symbols(workspace: Path, lsp: LSPManager, path: str) -> str:
    err = _resolve(workspace, path)
    if err:
        return err
    try:
        result = lsp.ask_document_symbols(path)
        if not result:
            return f"No document symbols for '{path}'"
        lines: list[str] = []
        _format_document_symbols(result, 0, lines)
        footer = ""
        if len(lines) >= SYMBOL_MAX:
            footer = f"\n... (capped at {SYMBOL_MAX})"
        return f"{len(lines)} symbol(s) in {path}\n" + "\n".join(lines) + footer
    except (LSPTimeoutError, RuntimeError, ValueError, OSError) as exc:
        return f"error: getting document symbols for '{path}': {exc}"


def rename_symbol(
    workspace: Path,
    lsp: LSPManager,
    path: str,
    line: int,
    character: int,
    new_name: str,
):
    err = _resolve(workspace, path)
    if err:
        return err
    if not new_name:
        return "error: new_name is required"
    try:
        result = lsp.ask_rename(path, line - 1, character - 1, new_name)
        if not result:
            return f"error: language server returned no edits for rename at {path}:{line}:{character}"
        return result
    except (LSPTimeoutError, RuntimeError, ValueError, OSError) as exc:
        return f"error: renaming symbol in '{path}' at {line}:{character}: {exc}"

"""Minimal stdio MCP (Model Context Protocol) client.

Implements just enough of the spec to spawn a server, run the initialize
handshake, list tools, call tools, and shut down cleanly. The protocol on
stdio is newline-delimited JSON-RPC 2.0; each subprocess gets a reader
thread that demultiplexes responses by request id and silently absorbs
server-initiated notifications (we don't need them yet).

Synchronous-by-design: every public method blocks the caller until the
subprocess answers (or a timeout fires). The pywebview JS bridge calls
each Python method on its own worker thread, so blocking is safe and
keeps the wire surface tiny.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any


PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO = {"name": "mimi-chat", "version": "1.0"}
DEFAULT_TIMEOUT = 30.0
INIT_TIMEOUT = 15.0


def namespaced_tool_name(server_id: str, tool_name: str) -> str:
    """Wire name used to expose an MCP tool to the model — matches Claude Code."""
    return f"mcp__{server_id}__{tool_name}"


def parse_namespaced_tool(name: str) -> tuple[str, str] | None:
    """Reverse of ``namespaced_tool_name``. Returns ``(server_id, tool_name)`` or None."""
    if not isinstance(name, str) or not name.startswith("mcp__"):
        return None
    rest = name[len("mcp__"):]
    sep = rest.find("__")
    if sep <= 0 or sep == len(rest) - 2:
        return None
    return rest[:sep], rest[sep + 2:]


@dataclass
class _Server:
    id: str
    name: str
    command: str
    args: list[str]
    env: dict[str, str]
    cwd: str
    process: subprocess.Popen | None = None
    reader: threading.Thread | None = None
    pending: dict[int, queue.Queue] = field(default_factory=dict)
    next_id: int = 1
    lock: threading.Lock = field(default_factory=threading.Lock)
    state: str = "stopped"  # stopped | starting | ready | error
    last_error: str = ""
    server_info: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    started_at: float = 0.0
    stderr_tail: list[str] = field(default_factory=list)
    stderr_thread: threading.Thread | None = None


class McpManager:
    """Owns a small pool of MCP server subprocesses keyed by server id."""

    def __init__(self) -> None:
        self._servers: dict[str, _Server] = {}
        self._lock = threading.Lock()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self, sid: str, name: str, command: str, args: list[str],
              env: dict[str, str] | None = None, cwd: str = "") -> dict[str, Any]:
        """Start (or re-start) the server identified by ``sid``."""
        self.stop(sid)
        srv = _Server(
            id=sid,
            name=name or sid,
            command=str(command or "").strip(),
            args=[str(a) for a in (args or []) if str(a).strip() != ""],
            env={str(k): str(v) for k, v in (env or {}).items() if str(k).strip()},
            cwd=str(cwd or "").strip(),
        )
        if not srv.command:
            srv.state = "error"
            srv.last_error = "Empty command."
            with self._lock:
                self._servers[sid] = srv
            return self.status(sid)

        full_env = os.environ.copy()
        full_env.update(srv.env)

        try:
            popen_cwd = srv.cwd if srv.cwd and os.path.isdir(srv.cwd) else None
            srv.process = subprocess.Popen(
                [srv.command, *srv.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=full_env,
                cwd=popen_cwd,
                bufsize=0,
                text=False,
            )
        except (FileNotFoundError, OSError) as e:
            srv.state = "error"
            srv.last_error = f"Failed to spawn: {e}"
            with self._lock:
                self._servers[sid] = srv
            return self.status(sid)

        srv.state = "starting"
        srv.started_at = time.time()
        srv.reader = threading.Thread(target=self._read_loop, args=(srv,), daemon=True)
        srv.reader.start()
        srv.stderr_thread = threading.Thread(target=self._stderr_loop, args=(srv,), daemon=True)
        srv.stderr_thread.start()

        with self._lock:
            self._servers[sid] = srv

        try:
            self._handshake(srv)
            tools = self._list_tools(srv)
            srv.tools = tools
            srv.state = "ready"
        except Exception as e:
            srv.last_error = f"{e}"
            srv.state = "error"
            self._kill_process(srv)

        return self.status(sid)

    def stop(self, sid: str) -> dict[str, Any]:
        """Stop and forget the named server. Idempotent."""
        with self._lock:
            srv = self._servers.pop(sid, None)
        if srv is None:
            return {"id": sid, "state": "stopped", "tools": [], "error": ""}
        self._kill_process(srv)
        srv.state = "stopped"
        return {"id": sid, "state": "stopped", "tools": [], "error": ""}

    def stop_all(self) -> None:
        with self._lock:
            ids = list(self._servers.keys())
        for sid in ids:
            self.stop(sid)

    # ── status / introspection ──────────────────────────────────────────────

    def status(self, sid: str) -> dict[str, Any]:
        with self._lock:
            srv = self._servers.get(sid)
        if not srv:
            return {
                "id": sid, "state": "stopped", "tools": [],
                "error": "", "serverInfo": {}, "stderrTail": [],
            }
        return {
            "id": srv.id,
            "state": srv.state,
            "tools": list(srv.tools),
            "error": srv.last_error,
            "serverInfo": dict(srv.server_info),
            "stderrTail": list(srv.stderr_tail[-40:]),
            "uptimeMs": int((time.time() - srv.started_at) * 1000) if srv.started_at else 0,
        }

    def status_all(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            ids = list(self._servers.keys())
        return {sid: self.status(sid) for sid in ids}

    def list_tools(self, sid: str) -> list[dict[str, Any]]:
        with self._lock:
            srv = self._servers.get(sid)
        if not srv or srv.state != "ready":
            return []
        return list(srv.tools)

    # ── tools/call ──────────────────────────────────────────────────────────

    def call_tool(self, sid: str, tool_name: str, arguments: dict[str, Any],
                  timeout: float = DEFAULT_TIMEOUT) -> dict[str, Any]:
        """Invoke ``tool_name`` on the named server. Returns the raw MCP result."""
        with self._lock:
            srv = self._servers.get(sid)
        if not srv:
            return {"ok": False, "error": f"MCP server '{sid}' is not running."}
        if srv.state != "ready":
            return {"ok": False, "error": f"MCP server '{sid}' is {srv.state}: {srv.last_error or 'not ready'}"}
        try:
            result = self._request(srv, "tools/call", {
                "name": tool_name,
                "arguments": arguments or {},
            }, timeout=timeout)
        except _RpcError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": f"MCP call failed: {e}"}
        return {"ok": True, "result": result}

    # ── internals ────────────────────────────────────────────────────────────

    def _handshake(self, srv: _Server) -> None:
        result = self._request(srv, "initialize", {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "clientInfo": CLIENT_INFO,
        }, timeout=INIT_TIMEOUT)
        srv.server_info = (result or {}).get("serverInfo", {}) if isinstance(result, dict) else {}
        # The "initialized" notification has no response — protocol just expects it.
        self._send_notification(srv, "notifications/initialized", {})

    def _list_tools(self, srv: _Server) -> list[dict[str, Any]]:
        try:
            result = self._request(srv, "tools/list", {}, timeout=DEFAULT_TIMEOUT)
        except _RpcError:
            return []
        if not isinstance(result, dict):
            return []
        items = result.get("tools")
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for t in items:
            if not isinstance(t, dict):
                continue
            tn = t.get("name")
            if not isinstance(tn, str) or not tn.strip():
                continue
            out.append({
                "name": tn,
                "description": str(t.get("description") or ""),
                "inputSchema": t.get("inputSchema") if isinstance(t.get("inputSchema"), dict) else {
                    "type": "object", "properties": {}, "additionalProperties": True,
                },
            })
        return out

    def _request(self, srv: _Server, method: str, params: dict[str, Any],
                 timeout: float) -> Any:
        with srv.lock:
            req_id = srv.next_id
            srv.next_id += 1
            q: queue.Queue = queue.Queue(maxsize=1)
            srv.pending[req_id] = q
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}}
        try:
            self._write(srv, msg)
        except Exception as e:
            with srv.lock:
                srv.pending.pop(req_id, None)
            raise _RpcError(f"Failed to write request: {e}") from e

        try:
            resp = q.get(timeout=timeout)
        except queue.Empty:
            with srv.lock:
                srv.pending.pop(req_id, None)
            raise _RpcError(f"Timed out waiting for {method} response after {timeout}s")
        finally:
            with srv.lock:
                srv.pending.pop(req_id, None)

        if isinstance(resp, dict) and "error" in resp and resp["error"]:
            err = resp["error"]
            msg_txt = err.get("message") if isinstance(err, dict) else str(err)
            raise _RpcError(f"{method}: {msg_txt}")
        return resp.get("result") if isinstance(resp, dict) else None

    def _send_notification(self, srv: _Server, method: str, params: dict[str, Any]) -> None:
        msg = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        try:
            self._write(srv, msg)
        except Exception:
            # Notifications are best-effort.
            pass

    def _write(self, srv: _Server, payload: dict[str, Any]) -> None:
        proc = srv.process
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise _RpcError("Process is not running.")
        line = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            proc.stdin.write(line)
            proc.stdin.flush()
        except (BrokenPipeError, OSError) as e:
            raise _RpcError(f"Pipe broke: {e}") from e

    def _read_loop(self, srv: _Server) -> None:
        proc = srv.process
        if proc is None or proc.stdout is None:
            return
        while True:
            try:
                raw = proc.stdout.readline()
            except Exception:
                break
            if not raw:
                break
            try:
                msg = json.loads(raw.decode("utf-8", errors="replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(msg, dict):
                continue
            mid = msg.get("id")
            if mid is not None and "method" not in msg:
                # Response to one of our requests.
                with srv.lock:
                    q = srv.pending.get(mid)
                if q is not None:
                    try:
                        q.put_nowait(msg)
                    except queue.Full:
                        pass
            # else: notification or server-side request — ignore for now.

    def _stderr_loop(self, srv: _Server) -> None:
        proc = srv.process
        if proc is None or proc.stderr is None:
            return
        while True:
            try:
                raw = proc.stderr.readline()
            except Exception:
                break
            if not raw:
                break
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if line:
                srv.stderr_tail.append(line)
                # Cap memory; only the recent tail is exposed to the UI.
                if len(srv.stderr_tail) > 200:
                    del srv.stderr_tail[:100]

    def _kill_process(self, srv: _Server) -> None:
        proc = srv.process
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                try:
                    proc.stdin.close()
                except Exception:
                    pass
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        pass
        except Exception:
            pass
        srv.process = None
        # Wake any pending requests so callers don't hang past server exit.
        with srv.lock:
            for q in srv.pending.values():
                try:
                    q.put_nowait({"jsonrpc": "2.0", "error": {"message": "Server stopped."}})
                except queue.Full:
                    pass
            srv.pending.clear()


class _RpcError(Exception):
    pass


# Module-level singleton — pywebview's JS bridge calls happen on worker threads.
_manager: McpManager | None = None
_manager_lock = threading.Lock()


def manager() -> McpManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = McpManager()
        return _manager


def shutdown_all() -> None:
    global _manager
    with _manager_lock:
        m = _manager
    if m is not None:
        m.stop_all()

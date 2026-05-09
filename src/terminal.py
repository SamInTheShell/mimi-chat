"""PTY-backed terminal sessions exposed to the frontend.

Linux/macOS only. The frontend opens a session, writes user keystrokes into
it, and polls drain() for fresh output. xterm.js renders the bytes; we don't
interpret escape sequences here.

Bytes on the wire are base64-encoded in both directions so binary output
(escape sequences, partial UTF-8 across read boundaries) round-trips
unchanged.
"""

from __future__ import annotations

import atexit
import base64
import errno
import fcntl
import os
import pty
import select
import signal
import struct
import sys
import termios
import threading
import time
import uuid


class TerminalSession:
    def __init__(self, cols: int = 80, rows: int = 24, cwd: str | None = None, shell: str | None = None) -> None:
        self.id = uuid.uuid4().hex[:12]
        self.cols = max(1, int(cols or 80))
        self.rows = max(1, int(rows or 24))

        env = os.environ.copy()
        env.setdefault("TERM", "xterm-256color")
        env.setdefault("COLORTERM", "truecolor")
        sh = shell or env.get("SHELL") or "/bin/bash"

        pid, fd = pty.fork()
        if pid == 0:
            try:
                if cwd:
                    try:
                        os.chdir(cwd)
                    except OSError:
                        pass
                for k, v in env.items():
                    os.environ[k] = v
                os.execvp(sh, [sh, "-i"])
            except Exception:
                os._exit(127)

        self.pid = pid
        self.fd = fd
        self._buf = bytearray()
        self._lock = threading.Lock()
        self._closed = False
        self._set_winsize(self.rows, self.cols)
        self._reader = threading.Thread(target=self._read_loop, name=f"pty-reader-{self.id}", daemon=True)
        self._reader.start()

    def _set_winsize(self, rows: int, cols: int) -> None:
        try:
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        except OSError:
            pass

    def _read_loop(self) -> None:
        # Tight-ish select loop. 100ms poll keeps the thread responsive to
        # shutdown without burning CPU.
        while not self._closed:
            try:
                r, _, _ = select.select([self.fd], [], [], 0.1)
            except (OSError, ValueError):
                break
            if not r:
                continue
            try:
                data = os.read(self.fd, 16384)
            except OSError as e:
                if e.errno in (errno.EIO, errno.EBADF):
                    break
                continue
            if not data:
                break
            with self._lock:
                self._buf.extend(data)
        self._closed = True
        # Reap the child so it doesn't linger as a zombie.
        try:
            os.waitpid(self.pid, os.WNOHANG)
        except OSError:
            pass

    def write(self, data: bytes) -> None:
        if self._closed or not data:
            return
        try:
            os.write(self.fd, data)
        except OSError:
            self._closed = True

    def drain(self) -> bytes:
        with self._lock:
            data = bytes(self._buf)
            self._buf.clear()
        return data

    def get_cwd(self) -> str | None:
        """Best-effort current working directory of the shell process.

        Linux exposes this via /proc/<pid>/cwd. macOS doesn't have /proc, so
        we fall back to libproc via subprocess; if that fails we return None
        and the frontend just hides the badge.
        """
        if self._closed:
            return None
        try:
            return os.readlink(f"/proc/{self.pid}/cwd")
        except OSError:
            pass
        if sys.platform == "darwin":
            try:
                import subprocess
                out = subprocess.check_output(
                    ["lsof", "-p", str(self.pid), "-a", "-d", "cwd", "-Fn"],
                    stderr=subprocess.DEVNULL, timeout=0.5,
                )
                for line in out.decode("utf-8", "replace").splitlines():
                    if line.startswith("n"):
                        return line[1:]
            except Exception:
                return None
        return None

    def resize(self, cols: int, rows: int) -> None:
        self.cols = max(1, int(cols or 80))
        self.rows = max(1, int(rows or 24))
        self._set_winsize(self.rows, self.cols)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            os.kill(self.pid, signal.SIGHUP)
        except OSError:
            pass
        try:
            os.close(self.fd)
        except OSError:
            pass
        # Escalate if the shell ignores SIGHUP — bash with `huponexit` off, vim
        # in alt-screen, etc. Poll waitpid briefly, then SIGKILL. The reader
        # thread is daemonic so we don't wait on it.
        deadline = time.monotonic() + 0.6
        killed = False
        while time.monotonic() < deadline:
            try:
                wpid, _ = os.waitpid(self.pid, os.WNOHANG)
                if wpid != 0:
                    return
            except OSError:
                return
            time.sleep(0.05)
        try:
            os.kill(self.pid, signal.SIGKILL)
            killed = True
        except OSError:
            pass
        if killed:
            try:
                os.waitpid(self.pid, 0)
            except OSError:
                pass


_sessions: dict[str, TerminalSession] = {}
_lock = threading.Lock()


def start(cols: int = 80, rows: int = 24, cwd: str | None = None) -> str:
    s = TerminalSession(cols=cols, rows=rows, cwd=cwd)
    with _lock:
        _sessions[s.id] = s
    return s.id


def write(session_id: str, data_b64: str) -> bool:
    with _lock:
        s = _sessions.get(session_id)
    if s is None:
        return False
    try:
        data = base64.b64decode(data_b64 or "")
    except Exception:
        return False
    s.write(data)
    return True


def drain(session_id: str) -> dict:
    with _lock:
        s = _sessions.get(session_id)
    if s is None:
        return {"ok": False, "closed": True, "data": "", "cwd": None}
    chunk = s.drain()
    return {
        "ok": True,
        "closed": s._closed,
        "data": base64.b64encode(chunk).decode("ascii"),
        "cwd": s.get_cwd(),
    }


def resize(session_id: str, cols: int, rows: int) -> bool:
    with _lock:
        s = _sessions.get(session_id)
    if s is None:
        return False
    s.resize(cols, rows)
    return True


def kill(session_id: str) -> bool:
    with _lock:
        s = _sessions.pop(session_id, None)
    if s is not None:
        s.close()
    return True


def shutdown_all() -> None:
    with _lock:
        ss = list(_sessions.values())
        _sessions.clear()
    for s in ss:
        s.close()


# Final safety net: pywebview's `closing` event already calls shutdown_all on
# graceful exit. atexit covers cases where the GUI thread crashes before that
# fires, leaving PTYs leaked under the user's session.
atexit.register(shutdown_all)

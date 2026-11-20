"""Local AF_UNIX IPC for MiniPC teleop CLI (架构 §6)."""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Any, Callable

MAX_BYTES = 8192

Handler = Callable[[dict[str, Any]], dict[str, Any]]


def default_socket_path(robot_id: str) -> Path:
    rid = str(robot_id or "unknown").strip() or "unknown"
    xdg = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if xdg:
        base = Path(xdg) / "yam"
    else:
        uid = os.getuid() if hasattr(os, "getuid") else os.getpid()
        base = Path(f"/tmp/yam-{uid}")
    return base / f"arena-agent-{rid}.sock"


def _prepare_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    if path.exists():
        path.unlink()


class TeleopIpcServer:
    def __init__(self, path: Path, handler: Handler) -> None:
        self.path = path
        self.handler = handler
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        if not hasattr(socket, "AF_UNIX"):
            return
        _prepare_dir(self.path)
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.bind(str(self.path))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        sock.listen(4)
        sock.settimeout(0.5)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="teleop-ipc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        if self.path.exists():
            try:
                self.path.unlink()
            except OSError:
                pass

    def _loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._stop.is_set():
            try:
                conn, _addr = sock.accept()
            except TimeoutError:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            with conn:
                self._handle_conn(conn)

    def _handle_conn(self, conn: socket.socket) -> None:
        raw = b""
        while b"\n" not in raw and len(raw) < MAX_BYTES:
            chunk = conn.recv(MAX_BYTES - len(raw))
            if not chunk:
                break
            raw += chunk
        line = raw.split(b"\n", 1)[0]
        try:
            req = json.loads(line.decode("utf-8"))
            if not isinstance(req, dict):
                raise ValueError("not an object")
            resp = self.handler(req)
        except Exception as exc:  # noqa: BLE001
            resp = {"ok": False, "result": "error", "layer": "agent", "error": str(exc)}
        body = (json.dumps(resp, default=str) + "\n").encode("utf-8")
        conn.sendall(body[:MAX_BYTES])


def send_command(path: Path, command: str, timeout_sec: float = 30.0) -> dict[str, Any]:
    if not hasattr(socket, "AF_UNIX"):
        raise RuntimeError("AF_UNIX not available on this platform")
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_sec)
    try:
        sock.connect(str(path))
        sock.sendall((json.dumps({"command": command}) + "\n").encode("utf-8"))
        raw = b""
        while b"\n" not in raw and len(raw) < MAX_BYTES:
            chunk = sock.recv(MAX_BYTES - len(raw))
            if not chunk:
                break
            raw += chunk
    finally:
        sock.close()
    if not raw:
        raise RuntimeError("empty IPC response")
    data = json.loads(raw.split(b"\n", 1)[0].decode("utf-8"))
    if not isinstance(data, dict):
        raise RuntimeError("invalid IPC response")
    return data

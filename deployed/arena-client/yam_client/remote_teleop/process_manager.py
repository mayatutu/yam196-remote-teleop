"""Subprocess helpers for Robot/Operator teleop process lifecycle."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import IO, Sequence


@dataclass
class StopResult:
    name: str
    verified: bool
    exit_code: int | None = None
    reason: str = ""


@dataclass
class ManagedProcess:
    name: str
    popen: subprocess.Popen[str]
    started_at: float = field(default_factory=time.monotonic)
    pgid: int | None = None

    @property
    def pid(self) -> int | None:
        return self.popen.pid

    def poll(self) -> int | None:
        return self.popen.poll()

    def is_alive(self) -> bool:
        return self.popen.poll() is None


class ProcessManager:
    def __init__(self) -> None:
        self._procs: list[ManagedProcess] = []
        self._lock = threading.RLock()

    @property
    def processes(self) -> list[ManagedProcess]:
        return list(self._procs)

    def _discard_name(self, name: str) -> None:
        """Drop all registry entries for ``name`` (dead or alive)."""
        self._procs = [p for p in self._procs if p.name != name]

    def start(
        self,
        name: str,
        command: Sequence[str],
        *,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        stdin: int | IO[str] | None = subprocess.PIPE,
        stdout: int | IO[str] | None = subprocess.PIPE,
        stderr: int | IO[str] | None = subprocess.STDOUT,
        new_process_group: bool = False,
    ) -> ManagedProcess:
        # Reset/teach cycles stop then re-start the same logical process; drop
        # stale handles so wait_alive watches the new child, not a dead one.
        with self._lock:
            self._discard_name(name)
            # Unbuffered child stdio so crash traces land in log files immediately
            # (file-backed stdout is block-buffered by default in CPython).
            child_env = dict(os.environ) if env is None else dict(env)
            child_env.setdefault("PYTHONUNBUFFERED", "1")
            extra: dict[str, object] = {}
            if new_process_group and os.name == "posix":
                extra["start_new_session"] = True
            popen = subprocess.Popen(
                list(command),
                cwd=cwd,
                env=child_env,
                stdin=stdin,
                stdout=stdout,
                stderr=stderr,
                text=True,
                bufsize=1,
                **extra,
            )
            pgid = None
            if new_process_group and os.name == "posix" and popen.pid:
                try:
                    pgid = os.getpgid(popen.pid)
                except OSError:
                    pgid = popen.pid
            managed = ManagedProcess(name=name, popen=popen, pgid=pgid)
            self._procs.append(managed)
            return managed

    def wait_alive(self, name: str, timeout_sec: float) -> ManagedProcess:
        """Require *name* to stay alive for the full *timeout_sec* window.

        Returns only after the settle period if the process never exited.
        Raises RuntimeError immediately if it exits early (includes a short
        stdout/stderr tail when available).
        """
        proc = self.get(name)
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            code = proc.poll()
            if code is not None:
                detail = self._read_output_tail(proc)
                suffix = f": {detail}" if detail else ""
                raise RuntimeError(
                    f"process {name} exited early with code {code}{suffix}"
                )
            time.sleep(0.05)
        code = proc.poll()
        if code is None:
            return proc
        detail = self._read_output_tail(proc)
        suffix = f": {detail}" if detail else ""
        raise RuntimeError(f"process {name} exited early with code {code}{suffix}")

    @staticmethod
    def _read_output_tail(proc: ManagedProcess, *, max_chars: int = 500) -> str:
        stream = getattr(proc.popen, "stdout", None)
        if stream is None:
            return ""
        try:
            # Non-blocking drain is not available on all pipes; read may block
            # if the child is still alive. Only call after poll() shows exit.
            data = stream.read() if hasattr(stream, "read") else ""
        except Exception:  # noqa: BLE001
            return ""
        text = (data or "").strip()
        return text[-max_chars:] if text else ""

    def get(self, name: str) -> ManagedProcess:
        matches = [p for p in self._procs if p.name == name]
        if not matches:
            raise KeyError(name)
        alive = [p for p in matches if p.is_alive()]
        return alive[-1] if alive else matches[-1]

    def stop(
        self,
        name: str,
        *,
        grace_sec: float = 2.0,
        terminate: bool = True,
    ) -> int | None:
        result = self.stop_verified(name, grace_sec=grace_sec, terminate=terminate)
        return result.exit_code

    def stop_verified(
        self,
        name: str,
        *,
        grace_sec: float = 2.0,
        terminate: bool = True,
    ) -> StopResult:
        with self._lock:
            try:
                proc = self.get(name)
            except KeyError:
                return StopResult(name=name, verified=True, reason="absent")
            if proc.poll() is not None:
                code = proc.poll()
                self._discard_name(name)
                return StopResult(name=name, verified=True, exit_code=code, reason="already_exited")
            if terminate:
                self._signal_group(proc, kill=False)
            deadline = time.monotonic() + grace_sec
            while time.monotonic() < deadline:
                code = proc.poll()
                if code is not None:
                    self._discard_name(name)
                    return StopResult(name=name, verified=True, exit_code=code, reason="terminated")
                time.sleep(0.05)
            self._signal_group(proc, kill=True)
            try:
                code = proc.popen.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._discard_name(name)
                return StopResult(name=name, verified=False, reason="kill_timeout")
            self._discard_name(name)
            return StopResult(name=name, verified=True, exit_code=code, reason="killed")

    @staticmethod
    def _signal_group(proc: ManagedProcess, *, kill: bool) -> None:
        if os.name == "posix" and proc.pgid:
            sig = signal.SIGKILL if kill else signal.SIGTERM
            try:
                os.killpg(proc.pgid, sig)
                return
            except OSError:
                pass
        if kill:
            proc.popen.kill()
        else:
            proc.popen.terminate()

    def stop_named(
        self,
        names: Sequence[str],
        *,
        grace_sec: float = 2.0,
    ) -> dict[str, int | None]:
        results: dict[str, int | None] = {}
        for name in names:
            try:
                results[name] = self.stop(name, grace_sec=grace_sec)
            except KeyError:
                results[name] = None
        return results

    def stop_all(self, *, grace_sec: float = 2.0) -> None:
        for proc in reversed(self._procs):
            if proc.poll() is None:
                self.stop(proc.name, grace_sec=grace_sec)

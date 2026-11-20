"""MiniPC arm stack: yam_remote_dual_server + arm_relay_server_bridge (shared)."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from .can_handoff import run_can_handoff
from .phase import RemoteTeleopConfig, _as_command, run_home_subprocess
from .process_manager import ProcessManager


@dataclass
class ArmStackRuntime:
    python: str
    client_root: Path
    config: RemoteTeleopConfig
    left_config_path: str = ""
    right_config_path: str = ""


class ArmStack:
    """Start/stop yam_server + arm_bridge without Flow control_listener."""

    def __init__(self, runtime: ArmStackRuntime) -> None:
        self.runtime = runtime
        self.pm = ProcessManager()
        self._log_files: dict[str, object] = {}
        self.session_id: str | None = None
        self.owner: str | None = None

    @property
    def running(self) -> bool:
        try:
            return (
                self.pm.get("yam_server").is_alive()
                and self.pm.get("arm_bridge").is_alive()
            )
        except KeyError:
            return False

    def _open_log(self, name: str):
        """Open a line-buffered child log instead of an unread subprocess pipe."""
        self._close_log(name)
        log_dir = self.runtime.client_root / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        handle = open(log_dir / f"{name}.log", "w", encoding="utf-8", buffering=1)
        self._log_files[name] = handle
        return handle

    def _close_log(self, name: str) -> None:
        handle = self._log_files.pop(name, None)
        if handle is not None:
            handle.close()

    def start(self, *, arm_session: str, dry_run: bool = False, owner: str = "shared") -> None:
        if self.running:
            return
        if not dry_run:
            # Defensive: reset/teach may leave dead handles if an older PM slipped through.
            self.stop()
        rt = self.runtime
        cfg = rt.config
        extra_patterns = ()
        fail_closed = owner == "teleop"
        if owner == "teleop":
            extra_patterns = ("arm_relay_server_bridge", "remote_teleop.home_move")
        if cfg.require_can_handoff and not dry_run:
            if not rt.left_config_path:
                raise RuntimeError("left_config_path required for can_handoff")
            handoff = run_can_handoff(
                python=rt.python,
                left_config=rt.left_config_path,
                right_config=rt.right_config_path or None,
                grace_sec=cfg.can_release_grace_sec,
                cwd=str(rt.client_root),
                extra_patterns=extra_patterns,
                fail_closed=fail_closed,
            )
            if not handoff.ok:
                detail = f":{handoff.diagnostic}" if handoff.diagnostic else ""
                raise RuntimeError(f"can_handoff_failed:{handoff.reason}{detail}")

        yam_cmd = list(cfg.yam_server_command)
        if not yam_cmd:
            sdk = cfg.piper_mate_sdk
            script = (
                str(Path(sdk) / "yam_remote_dual_server.py")
                if sdk
                else "yam_remote_dual_server.py"
            )
            yam_cmd = [
                rt.python,
                script,
                "--bind-ip",
                cfg.yam_server_bind_ip,
                "--port",
                str(cfg.yam_server_port),
                "--left-can",
                cfg.left_can,
                "--right-can",
                cfg.right_can,
            ]
            if cfg.yam_server_config:
                yam_cmd.extend(["--config", cfg.yam_server_config])
            # Fail closed even if a parent shell exported YAM_REMOTE_AUTO_RESUME=true.
            yam_cmd.append("--no-auto-resume")

        bridge_cmd = list(cfg.arm_bridge_command)
        if not bridge_cmd:
            sdk = cfg.low_latency_sdk or cfg.piper_mate_sdk
            script = (
                str(Path(sdk) / "arm_relay_server_bridge.py")
                if sdk
                else "arm_relay_server_bridge.py"
            )
            bridge_cmd = [
                rt.python,
                script,
                "--relay-ip",
                cfg.relay_host,
                "--relay-port",
                str(cfg.relay_arm_port),
                "--session-id",
                arm_session,
                "--dual-server-ip",
                cfg.yam_server_bind_ip,
                "--dual-server-port",
                str(cfg.yam_server_port),
            ]

        env = dict(os.environ)
        yam_client_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = yam_client_root + os.pathsep + env.get("PYTHONPATH", "")

        if dry_run:
            return

        yam_log = self._open_log("yam_remote_dual_server")
        self.pm.start(
            "yam_server",
            yam_cmd,
            cwd=str(rt.client_root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=yam_log,
            stderr=subprocess.STDOUT,
            new_process_group=True,
        )
        try:
            self.pm.wait_alive("yam_server", timeout_sec=5.0)
            time.sleep(0.5)
            bridge_log = self._open_log("arm_relay_server_bridge")
            self.pm.start(
                "arm_bridge",
                bridge_cmd,
                cwd=str(rt.client_root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=bridge_log,
                stderr=subprocess.STDOUT,
                new_process_group=True,
            )
            self.pm.wait_alive("arm_bridge", timeout_sec=5.0)
        except Exception:
            self.stop()
            raise
        self.session_id = arm_session
        self.owner = owner

    def stop(self, *, grace_sec: float = 2.0) -> None:
        self.stop_verified(grace_sec=grace_sec)

    def stop_verified(self, *, grace_sec: float = 2.0):
        from remote_teleop.process_manager import StopResult

        yam_alive = False
        try:
            yam_alive = bool(self.pm.get("yam_server").is_alive())
        except (KeyError, AttributeError):
            yam_alive = False
        if yam_alive:
            try:
                from remote_teleop.all_stop import send_all_stop

                send_all_stop(
                    host="127.0.0.1",
                    port=int(self.runtime.config.yam_server_port),
                    retries=3,
                    interval_ms=50.0,
                )
                time.sleep(0.15)
            except OSError:
                pass
        results = [
            self.pm.stop_verified("arm_bridge", grace_sec=grace_sec),
            self.pm.stop_verified("yam_server", grace_sec=grace_sec),
        ]
        self._close_log("arm_relay_server_bridge")
        self._close_log("yam_remote_dual_server")
        self.session_id = None
        self.owner = None
        if not results:
            return StopResult(name="arm_stack", verified=True, reason="absent")
        verified = all(item.verified for item in results)
        reason = ",".join(f"{item.name}:{item.reason}" for item in results)
        return StopResult(name="arm_stack", verified=verified, reason=reason)

    def _recovery_log_tail(self, path: Path, *, lines: int = 40) -> str:
        if not path.exists():
            return "(no recovery log written)"
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        return "\n".join(text.splitlines()[-lines:]) or "(recovery log empty)"

    def recover(self, *, arm_session: str, dry_run: bool = False) -> None:
        """Stop, verified motor-off, Home, and restart in that safety order."""
        self.stop()
        if dry_run:
            return
        rt = self.runtime
        if not rt.left_config_path or not rt.right_config_path:
            raise RuntimeError("dual_config_paths_required_for_recovery")
        cmd = [
            rt.python,
            "-m",
            "remote_teleop.motor_recovery",
            "--left-config-path",
            rt.left_config_path,
            "--right-config-path",
            rt.right_config_path,
        ]
        env = dict(os.environ)
        root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
        log_path = rt.client_root / "logs" / "motor_recovery.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("w", encoding="utf-8", buffering=1) as log:
            try:
                completed = subprocess.run(
                    cmd,
                    cwd=str(rt.client_root),
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    text=True,
                    check=False,
                    timeout=30.0,
                )
            except subprocess.TimeoutExpired as exc:
                detail = self._recovery_log_tail(log_path)
                raise RuntimeError(
                    f"explicit_motor_off_timeout after {exc.timeout:.1f}s; "
                    f"see {log_path}: {detail}"
                ) from exc
        if completed.returncode:
            raise RuntimeError(
                f"explicit_motor_off_failed code={completed.returncode}; "
                f"see {log_path}: {self._recovery_log_tail(log_path)}"
            )
        try:
            run_home_subprocess(
                python=rt.python,
                left_config=rt.left_config_path,
                right_config=rt.right_config_path,
                timeout_sec=120.0,
                cwd=str(rt.client_root),
                grace_sec=rt.config.can_release_grace_sec,
                wait_for_eval=False,
            )
        except Exception as exc:
            raise RuntimeError(f"home_after_motor_off_failed: {exc}") from exc
        try:
            self.start(arm_session=arm_session)
        except Exception as exc:
            self.stop()
            raise RuntimeError(f"arm_stack_start_failed: {exc}") from exc


def arm_session_for_robot(base_session: str, robot_id: str) -> str:
    """Map config arm_session + robot_id to relay session (e.g. yam_arm_SZ_YAM01)."""
    rid = str(robot_id or "").strip()
    base = str(base_session or "yam_arm").strip()
    if not rid:
        return base
    if rid in base or base.endswith(rid):
        return base
    if base == "yam_arm":
        return f"yam_arm_{rid}"
    return base

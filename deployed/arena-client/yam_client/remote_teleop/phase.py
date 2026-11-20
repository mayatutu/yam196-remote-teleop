"""Remote teleop phase orchestrator for Arena A/B PK flow."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

from .can_handoff import run_can_handoff
from .io_util import _readline_timeout
from .process_manager import ProcessManager
from .protocol import PHASE_AFTER_A, PHASE_AFTER_B

# Forward ref only for typing; avoid circular import at module load for tests.
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .arena_session import ArenaSessionController


OperatorChoice = str  # retry | skip | abort


@dataclass
class PhaseResult:
    phase: str
    attempted: bool
    result: str  # completed | skipped | failed | aborted
    duration_sec: float = 0.0
    skipped: bool = False
    aborted: bool = False
    reason: str = ""
    home_after_teleop: bool = False

    def to_meta(self) -> dict[str, Any]:
        return {
            "attempted": self.attempted,
            "result": self.result,
            "duration_sec": round(self.duration_sec, 3),
            "skipped": self.skipped,
            "aborted": self.aborted,
            "reason": self.reason,
            "home_after_teleop": self.home_after_teleop,
        }


@dataclass
class RemoteTeleopConfig:
    enabled: bool = False
    relay_host: str = ""
    relay_video_port: int = 5004
    relay_arm_port: int = 5005
    video_session: str = "cam_third"
    arm_session: str = "yam_arm"
    control_session: str = "arena_teleop_ctrl"
    yam_server_bind_ip: str = "127.0.0.1"
    yam_server_port: int = 5012
    control_heartbeat_timeout_sec: float = 5.0
    start_timeout_sec: float = 120.0
    done_timeout_sec: float = 300.0
    can_release_grace_sec: float = 2.0
    require_can_handoff: bool = True
    require_home_after_teleop: bool = True
    home_timeout_sec: float = 60.0
    left_can: str = "can_leader_l"
    right_can: str = "can_follower_r"
    yam_server_command: list[str] = field(default_factory=list)
    arm_bridge_command: list[str] = field(default_factory=list)
    control_listener_module: str = "remote_teleop.control_listener"
    piper_mate_sdk: str = ""
    low_latency_sdk: str = ""
    yam_server_config: str = ""
    meta_dir: str = "data"
    prompt_video_pi: bool = True

    @classmethod
    def from_mapping(cls, raw: dict[str, Any] | None) -> "RemoteTeleopConfig":
        raw = dict(raw or {})
        relay = dict(raw.get("relay") or {})
        robot = dict(raw.get("robot") or {})
        safety = dict(raw.get("safety") or {})
        return cls(
            enabled=bool(raw.get("enabled", False)),
            relay_host=str(relay.get("host") or raw.get("relay_host") or ""),
            relay_video_port=int(relay.get("video_port", 5004)),
            relay_arm_port=int(relay.get("arm_port", robot.get("relay_arm_port", 5005))),
            video_session=str(relay.get("video_session", "cam_third")),
            arm_session=str(relay.get("arm_session", robot.get("arm_session", "yam_arm"))),
            control_session=str(
                relay.get("arm_control_session")
                or robot.get("control_session")
                or "arena_teleop_ctrl"
            ),
            yam_server_bind_ip=str(robot.get("yam_remote_server_bind_ip", "127.0.0.1")),
            yam_server_port=int(robot.get("yam_remote_server_port", 5012)),
            control_heartbeat_timeout_sec=float(
                robot.get("control_heartbeat_timeout_sec", 5.0)
            ),
            start_timeout_sec=float(safety.get("start_timeout_sec", 120.0)),
            done_timeout_sec=float(safety.get("done_timeout_sec", 300.0)),
            can_release_grace_sec=float(safety.get("can_release_grace_sec", 2.0)),
            require_can_handoff=bool(safety.get("require_can_handoff_preflight", True)),
            require_home_after_teleop=bool(safety.get("require_home_after_teleop", True)),
            home_timeout_sec=float(safety.get("home_timeout_sec", 60.0)),
            left_can=str(robot.get("left_can", "can_leader_l")),
            right_can=str(robot.get("right_can", "can_follower_r")),
            yam_server_command=_as_command(robot.get("start_arm_server_command")),
            arm_bridge_command=_as_command(robot.get("start_arm_bridge_command")),
            piper_mate_sdk=str(raw.get("piper_mate_sdk") or robot.get("piper_mate_sdk") or ""),
            low_latency_sdk=str(
                raw.get("low_latency_sdk") or robot.get("low_latency_sdk") or ""
            ),
            yam_server_config=str(robot.get("yam_server_config") or ""),
            meta_dir=str(raw.get("meta_dir") or "data"),
            prompt_video_pi=bool(raw.get("prompt_video_pi", True)),
        )


def _as_command(value: Any) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return [str(x) for x in value]
    return shlex.split(str(value))


def _prompt_choice(label: str) -> OperatorChoice:
    while True:
        raw = input(f"{label} [retry/skip/abort]: ").strip().lower()
        if raw in {"retry", "r"}:
            return "retry"
        if raw in {"skip", "s"}:
            return "skip"
        if raw in {"abort", "a"}:
            return "abort"
        print("Enter retry, skip, or abort.")


def wait_for_eval_release(*, grace_sec: float = 2.0, poll_sec: float = 0.25, max_wait_sec: float = 15.0) -> None:
    """Block until launch_yaml_eval_molmoact is gone, then sleep grace_sec.

    Raises RuntimeError if the eval process is still present after max_wait_sec.
    """
    from .can_handoff import list_matching_pids

    deadline = time.monotonic() + max(0.0, max_wait_sec)
    while True:
        residual = list_matching_pids(("launch_yaml_eval_molmoact",))
        if not residual:
            break
        if time.monotonic() >= deadline:
            raise RuntimeError(
                f"eval_process_still_running after {max_wait_sec:.1f}s: pids={residual}"
            )
        time.sleep(max(0.05, poll_sec))
    time.sleep(max(0.0, grace_sec))


def run_home_subprocess(
    *,
    python: str,
    left_config: str,
    right_config: str | None,
    timeout_sec: float,
    cwd: str | None = None,
    grace_sec: float = 2.0,
    wait_for_eval: bool = True,
    cancel_event: Any | None = None,
    poll_sec: float = 0.1,
) -> None:
    """Run home_move in a child process; raise RuntimeError on failure/timeout."""
    run_home_subprocess_cancellable(
        python=python,
        left_config=left_config,
        right_config=right_config,
        timeout_sec=timeout_sec,
        cwd=cwd,
        grace_sec=grace_sec,
        wait_for_eval=wait_for_eval,
        cancel_event=cancel_event,
        poll_sec=poll_sec,
    )


def run_home_subprocess_cancellable(
    *,
    python: str,
    left_config: str,
    right_config: str | None,
    timeout_sec: float,
    cwd: str | None = None,
    grace_sec: float = 2.0,
    wait_for_eval: bool = True,
    cancel_event: Any | None = None,
    poll_sec: float = 0.1,
) -> None:
    """Home that can be interrupted by ``cancel_event`` within ``poll_sec``."""
    if wait_for_eval:
        wait_for_eval_release(grace_sec=grace_sec)
    else:
        time.sleep(max(0.0, grace_sec))

    cmd = [
        python,
        "-m",
        "remote_teleop.home_move",
        "--left-config-path",
        left_config,
    ]
    if right_config:
        cmd.extend(["--right-config-path", right_config])
    else:
        cmd.append("--left-only")
    env = dict(os.environ)
    yam_client_root = str(Path(__file__).resolve().parent.parent)
    env["PYTHONPATH"] = yam_client_root + os.pathsep + env.get("PYTHONPATH", "")

    import tempfile

    with tempfile.TemporaryDirectory(prefix="home_move_") as tmp:
        out_path = Path(tmp) / "stdout.txt"
        err_path = Path(tmp) / "stderr.txt"
        with out_path.open("w", encoding="utf-8") as out_f, err_path.open(
            "w", encoding="utf-8"
        ) as err_f:
            popen_kw: dict[str, Any] = {
                "cwd": cwd,
                "env": env,
                "stdout": out_f,
                "stderr": err_f,
            }
            if os.name == "posix":
                popen_kw["start_new_session"] = True
            proc = subprocess.Popen(cmd, **popen_kw)
            deadline = time.monotonic() + timeout_sec
            cancelled = False
            try:
                while True:
                    if cancel_event is not None and cancel_event.is_set():
                        cancelled = True
                        break
                    code = proc.poll()
                    if code is not None:
                        break
                    if time.monotonic() >= deadline:
                        break
                    time.sleep(max(0.05, min(poll_sec, 0.1)))
            finally:
                if proc.poll() is None:
                    _terminate_home(proc)
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2.0)

        stdout = out_path.read_text(encoding="utf-8", errors="replace").strip()
        stderr = err_path.read_text(encoding="utf-8", errors="replace").strip()
        if cancelled:
            raise RuntimeError("home_cancelled")
        if proc.poll() is None:
            raise RuntimeError(f"home_move timed out after {timeout_sec:.1f}s: {stderr or stdout or '(no output)'}")
        if proc.returncode != 0:
            detail = stderr or stdout or f"(exit {proc.returncode})"
            raise RuntimeError(f"home_move failed (code={proc.returncode}): {detail}")
        if "home_move_ok" not in stdout and "home_move_skip" not in stdout:
            raise RuntimeError(
                f"home_move missing ok/skip marker: stdout={stdout!r} stderr={stderr!r}"
            )


def _terminate_home(proc: subprocess.Popen[Any]) -> None:
    if os.name == "posix" and proc.pid:
        try:
            os.killpg(os.getpgid(proc.pid), 15)
            return
        except OSError:
            pass
    proc.terminate()


def save_phase_metadata(meta_path: Path, phase: str, result: PhaseResult) -> None:
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    existing: dict[str, Any] = {}
    if meta_path.exists():
        with meta_path.open("r", encoding="utf-8") as f:
            existing = yaml.safe_load(f) or {}
    block = dict(existing.get("remote_teleop") or {})
    block[phase] = result.to_meta()
    existing["remote_teleop"] = block
    with meta_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(existing, f, sort_keys=False)


class RemoteTeleopPhase:
    """Orchestrates one remote teleop insertion (after A or after B)."""

    def __init__(
        self,
        config: RemoteTeleopConfig,
        *,
        eval_id: str,
        phase: str,
        python: str = sys.executable,
        left_config: str,
        right_config: str | None,
        client_root: str | Path = ".",
        choose: Callable[[str], OperatorChoice] | None = None,
        confirm: Callable[[str], bool] | None = None,
        dry_run: bool = False,
        control_session: "ArenaSessionController | None" = None,
    ) -> None:
        if phase not in {PHASE_AFTER_A, PHASE_AFTER_B}:
            raise ValueError(f"invalid phase: {phase}")
        self.config = config
        self.eval_id = eval_id
        self.phase = phase
        self.python = python
        self.left_config = left_config
        self.right_config = right_config
        self.client_root = Path(client_root).resolve()
        self.choose = choose or _prompt_choice
        self.confirm = confirm or (lambda msg: input(f"{msg} [Enter to continue]: ").strip() or True)
        self.dry_run = dry_run
        self.control_session = control_session

    def run(self) -> PhaseResult:
        if not self.config.enabled:
            return PhaseResult(
                phase=self.phase,
                attempted=False,
                result="skipped",
                skipped=True,
                reason="remote_teleop.disabled",
            )

        started = time.monotonic()
        while True:
            try:
                result = self._run_once()
                result.duration_sec = time.monotonic() - started
                return result
            except _PhaseRetry as exc:
                print(f"[remote_teleop] retryable failure: {exc}")
                choice = self.choose("Remote teleop failed")
                if choice == "retry":
                    continue
                if choice == "skip":
                    return PhaseResult(
                        phase=self.phase,
                        attempted=True,
                        result="skipped",
                        skipped=True,
                        duration_sec=time.monotonic() - started,
                        reason=str(exc),
                    )
                return PhaseResult(
                    phase=self.phase,
                    attempted=True,
                    result="aborted",
                    aborted=True,
                    duration_sec=time.monotonic() - started,
                    reason=str(exc),
                )

    def _run_once(self) -> PhaseResult:
        cfg = self.config
        if cfg.prompt_video_pi:
            self.confirm(
                "Confirm Video Pi third-person sender is online "
                f"(relay {cfg.relay_host}:{cfg.relay_video_port} session {cfg.video_session})"
            )

        if cfg.require_can_handoff and not self.dry_run:
            handoff = run_can_handoff(
                python=self.python,
                left_config=self.left_config,
                right_config=self.right_config,
                grace_sec=cfg.can_release_grace_sec,
                cwd=str(self.client_root),
            )
            if not handoff.ok:
                raise _PhaseRetry(f"can_handoff_failed:{handoff.reason}")

        if self.dry_run:
            print("[remote_teleop] dry-run: skipping robot process start / wait loop")
            # Still allow operator to skip in dry-run if desired — treat as completed path
            # without second home unless require_home.
            return PhaseResult(
                phase=self.phase,
                attempted=True,
                result="completed",
                home_after_teleop=bool(cfg.require_home_after_teleop),
                reason="dry_run",
            )

        if not cfg.relay_host:
            raise _PhaseRetry("relay_host_missing")

        pm = ProcessManager()
        listener = None
        use_session = self.control_session is not None
        try:
            self._start_robot_stack(pm, start_listener=not use_session)
            if use_session:
                print(
                    "[remote_teleop] Robot yam stack ready (control via ArenaSession). "
                    "Waiting for Leader teleop_start..."
                )
                outcome = self.control_session.wait_teleop_control(
                    phase=self.phase,
                    start_timeout_sec=cfg.start_timeout_sec,
                    done_timeout_sec=cfg.done_timeout_sec,
                    heartbeat_timeout_sec=cfg.control_heartbeat_timeout_sec,
                    choose=self.choose,
                )
            else:
                listener = pm.get("control_listener")
                self._wait_listener_ready(listener)
                print(
                    "[remote_teleop] Robot teleop stack ready. "
                    "Start Operator wrapper now; waiting for teleop_start..."
                )
                outcome = self._wait_control_session(listener)
            if outcome == "completed":
                return PhaseResult(
                    phase=self.phase,
                    attempted=True,
                    result="completed",
                    home_after_teleop=bool(cfg.require_home_after_teleop),
                )
            if outcome == "skipped":
                return PhaseResult(
                    phase=self.phase,
                    attempted=True,
                    result="skipped",
                    skipped=True,
                    reason="operator_skip_during_wait",
                )
            if outcome == "aborted":
                return PhaseResult(
                    phase=self.phase,
                    attempted=True,
                    result="aborted",
                    aborted=True,
                    reason="teleop_aborted",
                )
            raise _PhaseRetry(f"teleop_outcome:{outcome}")
        finally:
            # Persistent control path: keep ArenaSession endpoint; only tear arm stack.
            stop_names = ["arm_bridge", "yam_server"]
            if not use_session:
                stop_names.append("control_listener")
            pm.stop_named(stop_names, grace_sec=2.0)

    def _start_robot_stack(self, pm: ProcessManager, *, start_listener: bool = True) -> None:
        from .arm_stack import ArmStack, ArmStackRuntime

        cfg = self.config
        stack = ArmStack(
            ArmStackRuntime(
                python=self.python,
                client_root=self.client_root,
                config=cfg,
                left_config_path=str(self.left_config),
                right_config_path=str(self.right_config) if self.right_config else "",
            )
        )
        # Phase runner owns pm; delegate stack start into our pm by copying procs.
        stack.pm = pm
        stack.start(arm_session=cfg.arm_session, dry_run=False)

        if not start_listener:
            return

        listener_cmd = [
            self.python,
            "-m",
            cfg.control_listener_module,
            "--relay-ip",
            cfg.relay_host,
            "--relay-port",
            str(cfg.relay_arm_port),
            "--session-id",
            cfg.control_session,
            "--eval-id",
            self.eval_id,
            "--phase",
            self.phase,
        ]

        env = dict(os.environ)
        yam_client_root = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = yam_client_root + os.pathsep + env.get("PYTHONPATH", "")

        pm.start("control_listener", listener_cmd, cwd=str(self.client_root), env=env)
        pm.wait_alive("control_listener", timeout_sec=5.0)

    def _wait_listener_ready(self, listener) -> None:
        deadline = time.monotonic() + 10.0
        assert listener.popen.stdout is not None
        while time.monotonic() < deadline:
            if listener.poll() is not None:
                raise _PhaseRetry("control_listener_exited_before_ready")
            line = _readline_timeout(listener.popen.stdout, 0.2)
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "ready":
                return
        raise _PhaseRetry("control_listener_ready_timeout")

    def _wait_control_session(self, listener) -> str:
        """Wait for start → heartbeats → done. Returns outcome token."""
        cfg = self.config
        assert listener.popen.stdout is not None
        assert listener.popen.stdin is not None

        state = "wait_start"
        last_hb = time.monotonic()
        start_deadline = time.monotonic() + cfg.start_timeout_sec
        done_deadline = time.monotonic() + cfg.done_timeout_sec

        while True:
            if listener.poll() is not None:
                raise _PhaseRetry("control_listener_died")

            line = _readline_timeout(listener.popen.stdout, 0.2)
            if line:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    event = {}
                name = str(event.get("event") or "")
                if name == "teleop_abort":
                    return "aborted"
                if state == "wait_start" and name == "teleop_start":
                    state = "active"
                    last_hb = time.monotonic()
                    done_deadline = time.monotonic() + cfg.done_timeout_sec
                    print("[remote_teleop] teleop_start received; active")
                elif state == "active" and name == "teleop_heartbeat":
                    last_hb = time.monotonic()
                    # heartbeat stays in active (self-loop); never jump to waitDone
                elif state == "active" and name == "teleop_done":
                    print("[remote_teleop] teleop_done received")
                    return "completed"

            now = time.monotonic()
            if state == "wait_start" and now > start_deadline:
                choice = self.choose("Timed out waiting for Operator teleop_start")
                if choice == "retry":
                    raise _PhaseRetry("start_timeout")
                if choice == "skip":
                    return "skipped"
                self._send_listener_abort(listener, "start_timeout_abort")
                return "aborted"

            if state == "active":
                if now - last_hb > cfg.control_heartbeat_timeout_sec:
                    choice = self.choose("Operator teleop_heartbeat timed out")
                    if choice == "retry":
                        raise _PhaseRetry("heartbeat_timeout")
                    if choice == "skip":
                        return "skipped"
                    self._send_listener_abort(listener, "heartbeat_timeout_abort")
                    return "aborted"
                if now > done_deadline:
                    choice = self.choose("Timed out waiting for teleop_done")
                    if choice == "retry":
                        raise _PhaseRetry("done_timeout")
                    if choice == "skip":
                        return "skipped"
                    self._send_listener_abort(listener, "done_timeout_abort")
                    return "aborted"

    @staticmethod
    def _send_listener_abort(listener, reason: str) -> None:
        try:
            assert listener.popen.stdin is not None
            listener.popen.stdin.write(
                json.dumps({"cmd": "send_abort", "reason": reason}) + "\n"
            )
            listener.popen.stdin.flush()
        except OSError as exc:
            print(f"[remote_teleop] warning: failed to send abort via listener stdin: {exc}")


class _PhaseRetry(Exception):
    pass


def run_remote_teleop_phase(
    *,
    config: dict[str, Any] | RemoteTeleopConfig,
    eval_id: str,
    phase: str,
    python: str,
    left_config: str,
    right_config: str | None,
    client_root: str | Path,
    dry_run: bool = False,
    choose: Callable[[str], OperatorChoice] | None = None,
    confirm: Callable[[str], bool] | None = None,
    control_session: "ArenaSessionController | None" = None,
) -> PhaseResult:
    cfg = config if isinstance(config, RemoteTeleopConfig) else RemoteTeleopConfig.from_mapping(config)
    orchestrator = RemoteTeleopPhase(
        cfg,
        eval_id=eval_id,
        phase=phase,
        python=python,
        left_config=left_config,
        right_config=right_config,
        client_root=client_root,
        choose=choose,
        confirm=confirm,
        dry_run=dry_run,
        control_session=control_session,
    )
    result = orchestrator.run()
    if cfg.enabled and result.attempted:
        meta_path = Path(client_root) / cfg.meta_dir / eval_id / "remote_teleop_meta.yaml"
        try:
            save_phase_metadata(meta_path, phase, result)
            print(f"[remote_teleop] wrote metadata {meta_path}")
        except OSError as exc:
            print(f"[remote_teleop] warning: failed to write metadata: {exc}")

    if result.result == "completed" and result.home_after_teleop and not dry_run:
        print("[remote_teleop] teleop completed; running second home move")
        try:
            run_home_subprocess(
                python=python,
                left_config=left_config,
                right_config=right_config,
                timeout_sec=cfg.home_timeout_sec,
                cwd=str(Path(client_root).resolve()),
                grace_sec=cfg.can_release_grace_sec,
                wait_for_eval=False,
            )
        except Exception as exc:  # noqa: BLE001
            # Home failure is more severe than teleop failure.
            raise RuntimeError(f"home_after_teleop_failed: {exc}") from exc
    elif result.skipped:
        print("[remote_teleop] skipped: no second home move")

    return result


def load_remote_teleop_meta(meta_path: Path) -> dict[str, Any]:
    if not meta_path.exists():
        return {}
    with meta_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return dict(data.get("remote_teleop") or {})

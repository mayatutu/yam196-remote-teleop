#!/usr/bin/env python3
"""MiniPC resident agent: listen for battle_start on arena_agent_ctrl, spawn eval flow."""

from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from remote_teleop.arm_stack import ArmStack, ArmStackRuntime, arm_session_for_robot
from remote_teleop.phase import RemoteTeleopConfig
from remote_teleop.protocol import (
    AGENT_EVAL_ID,
    KIND_AGENT,
    KIND_SYNC,
    MessageType,
    PHASE_BATTLE,
    make_message,
)
from remote_teleop.relay_control import RelayControlEndpoint
from remote_teleop.wrist_video_profile import WristVideoProfileManager

_TEACH_FLOW_PHASES = frozenset(
    {
        "NEED_TEACH",
        "TEACH_ARMED",
        "TEACH_RECORDING",
        "DEMO_CONFIRM",
        "DEMO_UPLOADING",
    }
)


def _load_yaml(config_path: str) -> dict[str, Any]:
    from omegaconf import OmegaConf

    # resolve=False: ${OPERATOR_STATE_TOKEN} is handled by _resolve_state_token.
    cfg = OmegaConf.to_container(OmegaConf.load(config_path), resolve=False)
    return dict(cfg or {})


def _load_remote(config_path: str) -> dict[str, Any]:
    return dict(_load_yaml(config_path).get("remote_teleop") or {})


def _load_robot_id(config_path: str) -> str:
    return str(_load_yaml(config_path).get("robot_id") or "").strip()


def _load_state_config(config_path: str) -> dict[str, Any]:
    remote = _load_remote(config_path)
    return dict(remote.get("state") or {})


def _resolve_state_token(raw: str) -> str:
    token = str(raw or "").strip()
    if token.startswith("${") and token.endswith("}"):
        return os.environ.get(token[2:-1], "dev-state-token")
    return token or "dev-state-token"


def _load_config_paths(config_path: str) -> tuple[str, str, Path]:
    cfg = _load_yaml(config_path)
    left = str(cfg.get("left_config_path") or "")
    right = str(cfg.get("right_config_path") or "")
    client_root = Path(str(cfg.get("client_root") or ".")).resolve()
    return left, right, client_root


class ArenaAgent:
    def __init__(
        self,
        *,
        config_path: str,
        python: str,
        client_root: Path,
        relay_host: str,
        relay_port: int,
        agent_session: str,
        robot_id: str = "",
        left_config_path: str = "",
        right_config_path: str = "",
        teleop_config: RemoteTeleopConfig | None = None,
        dry_run: bool = False,
    ) -> None:
        from yam_deploy.runtime import require_running
        require_running("agent")
        self.config_path = str(Path(config_path).resolve())
        self.python = python
        self.client_root = client_root.resolve()
        self.robot_id = str(robot_id or "").strip()
        self.left_config_path = left_config_path
        self.right_config_path = right_config_path
        self.teleop_config = teleop_config or RemoteTeleopConfig()
        self.dry_run = dry_run
        self.endpoint = RelayControlEndpoint(
            role="robot",
            relay_host=relay_host,
            relay_port=relay_port,
            session_id=agent_session,
        )
        self._flow: subprocess.Popen[str] | None = None
        self._last_hb = 0.0
        self._was_busy = False
        self._arm_stack: ArmStack | None = None
        self._scene_setup_active = False
        self._recovery_inflight = False
        self._state_bridge = None
        self._flow_stdout_thread: threading.Thread | None = None
        self._flow_eval_id: str | None = None
        self._flow_relay: RelayControlEndpoint | None = None
        self._last_flow_snapshot: dict[str, Any] = {}
        self._flow_terminal_ok = False
        self._flow_exit_reported = False
        self._last_flow_exit_nonterminal = False
        self._lock = threading.RLock()
        self._agent_instance_id = "agent_" + uuid.uuid4().hex[:12]
        self._connection_epoch = 0
        self._teleop_enabled = False
        self._capability_seq = 0
        self._state_synced = False
        self._teleop_session_id: str | None = None
        self._teleop_generation = 0
        self._home_cancel = threading.Event()
        self._home_running = False
        self._home_result: str | None = None
        self._arm_stack_running = False
        from yam_deploy.video_adapter import ProfileManager
        self._wrist_video = ProfileManager()
        self._state_fresh_at = 0.0
        self._safety_q: queue.Queue[dict[str, Any]] = queue.Queue()
        self._safety_thread = threading.Thread(
            target=self._safety_loop, name="teleop-safety", daemon=True
        )
        self._safety_thread.start()
        self._ipc = None
        state_cfg = _load_state_config(self.config_path)
        if state_cfg.get("enabled") and self.robot_id:
            from remote_teleop.agent_state_bridge import AgentStateBridge

            ws = str(state_cfg.get("websocket") or state_cfg.get("server", "")).replace(
                "http://", "ws://"
            ).replace("https://", "wss://")
            token = _resolve_state_token(str(state_cfg.get("token") or ""))
            self._state_bridge = AgentStateBridge(
                self,
                enabled=True,
                websocket=ws,
                token=token,
                robot_id=self.robot_id,
            )
            self._state_bridge.set_identity_provider(self._teleop_identity)

    def _teleop_identity(self) -> dict[str, Any]:
        with self._lock:
            return {
                "agent_instance_id": self._agent_instance_id,
                "connection_epoch": self._connection_epoch,
                "session_id": self._teleop_session_id,
            }

    def close(self) -> None:
        if self._ipc is not None:
            self._ipc.stop()
            self._ipc = None
        if self._state_bridge is not None:
            self._state_bridge.stop()
        self._stop_scene_setup_stack()
        self.endpoint.close()

    def _flow_relay_endpoint(self) -> RelayControlEndpoint:
        if self._flow_relay is None:
            remote = _load_remote(self.config_path)
            relay = dict(remote.get("relay") or {})
            self._flow_relay = RelayControlEndpoint(
                role="operator",
                relay_host=str(relay.get("host") or self.endpoint.relay_host),
                relay_port=int(relay.get("arm_port", self.endpoint.relay_port)),
                session_id=str(relay.get("arm_control_session", "arena_teleop_ctrl")),
            )
        return self._flow_relay

    def set_flow_eval_id(self, eval_id: str) -> None:
        self._flow_eval_id = str(eval_id or "").strip() or None

    def update_flow_snapshot(self, snap: dict[str, Any]) -> None:
        self._last_flow_snapshot = dict(snap or {})

    def clear_flow_tracking(self) -> None:
        """Drop stale Flow identity/snapshot before a new non-Flow control window."""
        self._flow_eval_id = None
        self._last_flow_snapshot = {}
        self._flow_terminal_ok = False
        self._flow_exit_reported = False
        self._last_flow_exit_nonterminal = False

    def mark_flow_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "finalize_done":
            result = payload.get("result")
            ok = payload.get("ok", result in (None, "ok"))
            self._flow_terminal_ok = bool(ok)
        elif event_type == "session_snapshot" and payload.get("phase") == "SESSION_END":
            self._flow_terminal_ok = True

    def _is_forced_nonterminal_exit(self, code: int | None, phase: str) -> bool:
        return self._flow_exit_is_nonterminal(code, phase)

    def _flow_exit_is_nonterminal(self, code: int | None, phase: str) -> bool:
        if phase == "SESSION_END":
            return bool(code)
        if phase in _TEACH_FLOW_PHASES:
            return True
        if code is not None and code != 0:
            return True
        return True

    def forward_to_flow(self, msg_type: MessageType, *, phase: str, kind: str, **kwargs: Any) -> None:
        """Inject control messages into the active Flow eval session (Relay teleop session)."""
        eval_id = self._flow_eval_id
        if not eval_id:
            print(f"[arena_agent] skip forward {msg_type.value}: no flow eval_id")
            return
        if self.robot_id and kwargs.get("robot_id") is None:
            kwargs["robot_id"] = self.robot_id
        msg = make_message(
            msg_type,
            eval_id=eval_id,
            phase=phase,
            seq=self._flow_relay_endpoint().next_seq(),
            kind=kind,
            **kwargs,
        )
        self._flow_relay_endpoint().send_message(msg)
        print(f"[arena_agent] forward_to_flow {msg_type.value} eval={eval_id} phase={phase}")

    def _flow_session_snapshot_payload(self) -> dict[str, Any]:
        busy = self._flow_busy()
        if not busy:
            self.clear_flow_tracking()
        if self._last_flow_snapshot and busy:
            out = dict(self._last_flow_snapshot)
            out.setdefault("robot_id", self.robot_id)
            if self._flow_eval_id:
                out.setdefault("eval_id", self._flow_eval_id)
            return out
        payload: dict[str, Any] = {
            "robot_id": self.robot_id,
            "associated": False,
        }
        if self._flow_eval_id:
            payload["eval_id"] = self._flow_eval_id
        if not busy:
            payload["phase"] = "IDLE"
        return payload

    def _flow_busy(self) -> bool:
        if self._flow is None:
            return False
        code = self._flow.poll()
        if code is None:
            return True
        print(f"[arena_agent] previous flow exited code={code}")
        if (
            self._flow_stdout_thread is not None
            and self._flow_stdout_thread.is_alive()
            and threading.current_thread() is not self._flow_stdout_thread
        ):
            self._flow_stdout_thread.join(timeout=1.0)
        phase = str(self._last_flow_snapshot.get("phase") or "UNKNOWN")
        self._report_nonterminal_flow_exit(code)
        self._last_flow_exit_nonterminal = self._flow_exit_is_nonterminal(code, phase)
        self._flow = None
        self._flow_stdout_thread = None
        self._flow_eval_id = None
        self._last_flow_snapshot = {}
        self._flow_terminal_ok = False
        return False

    def _report_nonterminal_flow_exit(self, code: int | None) -> None:
        if self._flow_exit_reported:
            return
        phase = str(self._last_flow_snapshot.get("phase") or "UNKNOWN")
        if phase == "SESSION_END":
            return
        forced = self._is_forced_nonterminal_exit(code, phase)
        if self._flow_terminal_ok and not forced:
            return
        if forced:
            self._flow_terminal_ok = False
        self._flow_exit_reported = True
        message = f"flow exited before terminal session phase (phase={phase}, exit={code})"
        print(f"[arena_agent] {message}")
        if self._state_bridge is not None:
            self._state_bridge.send_event(
                "agent_error",
                {
                    "code": "flow_exited_nonterminal",
                    "message": message,
                    "phase": phase,
                    "exit_code": code,
                },
            )

    def _maybe_announce_ready(self) -> None:
        """Emit arena_ready when a flow finishes (upload/convert included)."""
        busy = self._flow_busy()
        if self._was_busy and not busy:
            if self._last_flow_exit_nonterminal:
                print("[arena_agent] flow exited nonterminal — skip arena_ready")
            else:
                print("[arena_agent] idle — sending arena_ready")
                if self._state_bridge is not None:
                    self._state_bridge.send_event("arena_ready", {"text": "ready_for_next_battle"})
                else:
                    self._send(MessageType.ARENA_READY, text="ready_for_next_battle")
            self._last_flow_exit_nonterminal = False
            self._flow_exit_reported = False
        self._was_busy = busy

    def _send(self, msg_type: MessageType, **kwargs: Any) -> None:
        if self.robot_id and kwargs.get("robot_id") is None:
            kwargs["robot_id"] = self.robot_id
        phase = kwargs.pop("phase", None) or PHASE_BATTLE
        msg = make_message(
            msg_type,
            eval_id=AGENT_EVAL_ID,
            phase=phase,
            seq=self.endpoint.next_seq(),
            kind=KIND_AGENT,
            **kwargs,
        )
        self.endpoint.send_message(msg)
        try:
            self.endpoint.send_empty_heartbeat()
        except RuntimeError:
            pass

    def _teleop_config_loaded(self) -> RemoteTeleopConfig:
        if self.teleop_config.relay_host:
            return self.teleop_config
        loaded = RemoteTeleopConfig.from_mapping(_load_remote(self.config_path))
        self.teleop_config = loaded
        return loaded

    def _arm_stack_runtime(self) -> ArmStackRuntime:
        left = self.left_config_path
        if left and not Path(left).is_absolute():
            left = str((self.client_root / left).resolve())
        right = self.right_config_path
        if right and not Path(right).is_absolute():
            right = str((self.client_root / right).resolve())
        return ArmStackRuntime(
            python=self.python,
            client_root=self.client_root,
            config=self._teleop_config_loaded(),
            left_config_path=left,
            right_config_path=right,
        )

    def _stop_scene_setup_stack(self) -> None:
        if self._arm_stack is not None:
            try:
                self._arm_stack.stop()
            except Exception as exc:  # noqa: BLE001
                print(f"[arena_agent] arm stack stop: {exc}", file=sys.stderr)
            self._arm_stack = None
        self._scene_setup_active = False

    def _recover_dual_arms(self, arm_session: str) -> None:
        """Recover both arms synchronously; no partial-arm success is reported."""
        if self._flow_busy():
            raise RuntimeError("flow_busy")
        if self._arm_stack is None:
            self._arm_stack = ArmStack(self._arm_stack_runtime())
        self._scene_setup_active = False
        self._arm_stack.recover(arm_session=arm_session, dry_run=self.dry_run)
        self._scene_setup_active = True

    def _on_recover_dual_arms(self, payload: dict[str, Any]) -> None:
        raise RuntimeError("This deployment supports standalone Teleop only")
        if getattr(self, "_recovery_inflight", False):
            print("[arena_agent] duplicate dual-arm recovery ignored", file=sys.stderr)
            return
        self._recovery_inflight = True
        self._state_bridge.send_event("recovery_started", {})
        arm_session = str(payload.get("arm_session") or "")
        if not arm_session:
            arm_session = arm_session_for_robot("yam_arm", self.robot_id)

        def run_recovery() -> None:
            try:
                self._stop_scene_setup_stack()
                self._recover_dual_arms(arm_session)
                self._state_bridge.send_event("recovery_ready", {"home": True})
            except Exception as exc:  # noqa: BLE001
                print(f"[arena_agent] dual-arm recovery failed: {exc}", file=sys.stderr)
                self._stop_scene_setup_stack()
                self._state_bridge.send_event("recovery_failed", {"reason": str(exc)})
            finally:
                self._recovery_inflight = False

        threading.Thread(target=run_recovery, name="dual-arm-recovery", daemon=True).start()

    def _on_scene_setup_start(self, msg: Any) -> None:
        raise RuntimeError("This deployment supports standalone Teleop only")
        if self._teleop_session_id:
            print("[arena_agent] teleop active → scene_setup_reject")
            if self._state_bridge is not None:
                self._state_bridge.send_event("scene_setup_rejected", {"reason": "teleop_active"})
            return
        if self._flow_busy():
            print("[arena_agent] busy → scene_setup_reject")
            self._send(
                MessageType.SCENE_SETUP_REJECT,
                reason="busy",
                text="battle or upload still running",
                phase="SCENE_SETUP",
            )
            return
        self.clear_flow_tracking()
        meta = getattr(msg, "meta", None) or {}
        arm_session = str(meta.get("arm_session") or "")
        if not arm_session:
            relay = dict(_load_remote(self.config_path).get("relay") or {})
            base = str(relay.get("arm_session") or "yam_arm")
            rid = str(getattr(msg, "robot_id", None) or self.robot_id or "")
            arm_session = arm_session_for_robot(base, rid)
        print(f"[arena_agent] scene_setup_start arm_session={arm_session}")
        try:
            if self._arm_stack is None:
                self._arm_stack = ArmStack(self._arm_stack_runtime())
            self._arm_stack.start(arm_session=arm_session, dry_run=self.dry_run)
            self._scene_setup_active = True
            self._send(MessageType.SCENE_SETUP_ACK, phase="SCENE_SETUP", text="ok")
        except Exception as exc:  # noqa: BLE001
            print(f"[arena_agent] scene_setup start failed: {exc}", file=sys.stderr)
            self._stop_scene_setup_stack()
            self._send(
                MessageType.SCENE_SETUP_REJECT,
                reason="start_failed",
                text=str(exc),
                phase="SCENE_SETUP",
            )

    def _on_scene_setup_stop(self) -> None:
        print("[arena_agent] scene_setup_stop")
        self._stop_scene_setup_stack()
        if not self._flow_busy():
            self.clear_flow_tracking()
        if self._state_bridge is None and not self._flow_busy():
            self._send(MessageType.ARENA_READY, text="scene_setup_stopped")

    def _teleop_status(self) -> dict[str, Any]:
        with self._lock:
            connected = bool(self._state_bridge and getattr(self._state_bridge._client, "connected", False))
            return {
                "ok": True,
                "result": "success",
                "teleop_enabled": self._teleop_enabled,
                "agent_instance_id": self._agent_instance_id,
                "state_connected": connected,
                "state_synced": self._state_synced and connected,
                "active_session_id": self._teleop_session_id,
                "runtime_state": "active" if self._teleop_session_id else "idle",
                "arm_stack_running": self._arm_stack_running or (self._arm_stack is not None and self._arm_stack.running),
                "home_running": self._home_running,
                "home_result": self._home_result,
            }

    def _handle_ipc(self, req: dict[str, Any]) -> dict[str, Any]:
        cmd = str(req.get("command") or "")
        if cmd == "status":
            return self._teleop_status()
        if cmd == "enable":
            return self.teleop_enable()
        if cmd == "disable":
            return self.teleop_disable()
        return {"ok": False, "result": "error", "layer": "agent", "error": f"unknown command {cmd}"}

    def teleop_enable(self) -> dict[str, Any]:
        from yam_deploy.runtime import require_running
        require_running("enable")
        with self._lock:
            connected = bool(self._state_bridge and getattr(self._state_bridge._client, "connected", False))
            if not connected:
                return {
                    "ok": False,
                    "result": "error",
                    "layer": "capability",
                    "error": "State not connected; enable fail-closed",
                    **{k: v for k, v in self._teleop_status().items() if k not in {"ok", "result"}},
                }
            self._teleop_enabled = True
            self._capability_seq += 1
            seq = self._capability_seq
            self._state_synced = False
        self._send_capability(True, seq)
        return self._teleop_status()

    def teleop_disable(self) -> dict[str, Any]:
        with self._lock:
            self._teleop_enabled = False
            self._capability_seq += 1
            seq = self._capability_seq
            self._teleop_generation += 1
            gen = self._teleop_generation
            self._home_cancel.set()
            session_id = self._teleop_session_id
            connected = bool(self._state_bridge and getattr(self._state_bridge._client, "connected", False))
        self._send_capability(False, seq)
        stopped = self._verify_local_stop(generation=gen)
        if not stopped:
            return {
                "ok": False,
                "result": "error",
                "layer": "MiniPC ArmStack",
                "error": "stop not verified",
                **self._teleop_status(),
            }
        if session_id:
            self.enqueue_teleop({"op": "abort", "reason": "cli_disable", "home": False, "generation": gen})
        if not connected:
            status = self._teleop_status()
            status["result"] = "local_success_pending_sync"
            status["ok"] = True
            return status
        return self._teleop_status()

    def _send_capability(self, enabled: bool, seq: int) -> None:
        if self._state_bridge is None:
            return
        self._state_bridge.send_event(
            "teleop_capability",
            {"enabled": enabled, "teleop_enabled": enabled, "capability_seq": seq, "reason": "local_cli"},
            queue_offline=False,
        )

    def on_state_socket(self, connected: bool) -> None:
        if not connected:
            with self._lock:
                self._state_synced = False
            self._maybe_heartbeat_lost(force=True)
            return
        with self._lock:
            self._connection_epoch += 1
            self._state_fresh_at = time.monotonic()
            payload = {
                "teleop_enabled": self._teleop_enabled,
                "capability_seq": self._capability_seq,
                "agent_instance_id": self._agent_instance_id,
                "connection_epoch": self._connection_epoch,
            }
            session_id = self._teleop_session_id
        if self._state_bridge is not None:
            self._state_bridge.send_event("agent_hello", payload, queue_offline=False)
            if session_id:
                self._state_bridge.send_event(
                    "teleop_interrupted",
                    {"reason": "state_reconnect", "local_stack_stopped": not self._arm_stack_running, "session_id": session_id},
                    queue_offline=False,
                )

    def note_state_message(self) -> None:
        with self._lock:
            self._state_fresh_at = time.monotonic()

    def note_hello_ack(self, payload: dict[str, Any]) -> None:
        if not payload.get("accepted", True):
            return
        with self._lock:
            self._state_synced = True
            self._state_fresh_at = time.monotonic()

    def note_capability_ack(self, payload: dict[str, Any]) -> None:
        if payload.get("accepted"):
            with self._lock:
                self._state_synced = True
                self._state_fresh_at = time.monotonic()

    def enqueue_teleop(self, job: dict[str, Any]) -> None:
        self._safety_q.put(job)

    def _safety_loop(self) -> None:
        while True:
            job = self._safety_q.get()
            try:
                op = str(job.get("op") or "")
                if op == "prepare":
                    self._do_teleop_prepare(job)
                elif op == "stop":
                    self._do_teleop_stop(job, home=True)
                elif op == "abort":
                    self._do_teleop_stop(job, home=False)
                elif op == "retry_home":
                    self._do_teleop_home(job)
                elif op == "video_profile":
                    self._do_video_profile(job)
            except Exception as exc:  # noqa: BLE001
                print(f"[arena_agent] teleop worker: {exc}", file=sys.stderr)

    def _do_teleop_prepare(self, job: dict[str, Any]) -> None:
        gen = int(job.get("generation") or 0)
        session_id = str(job.get("session_id") or "")
        arm_session = str(job.get("arm_session") or "") or arm_session_for_robot("yam_arm", self.robot_id)
        with self._lock:
            if gen != self._teleop_generation or not self._teleop_enabled:
                self._emit("teleop_prepare_rejected", {"reason": "teleop_disabled", "session_id": session_id})
                return
            if self._flow_busy() or self._scene_setup_active:
                self._emit("teleop_prepare_rejected", {"reason": "agent_busy", "session_id": session_id})
                return
            self._teleop_session_id = session_id
        try:
            if self._arm_stack is None:
                self._arm_stack = ArmStack(self._arm_stack_runtime())
            self._arm_stack.start(arm_session=arm_session, dry_run=self.dry_run, owner="teleop")
            with self._lock:
                if gen != self._teleop_generation:
                    self._arm_stack.stop_verified()
                    return
                self._arm_stack_running = True
            self._emit("teleop_stack_ready", {"session_id": session_id, "arm_session": arm_session})
        except Exception as exc:  # noqa: BLE001
            self._stop_scene_setup_stack()
            with self._lock:
                self._arm_stack_running = False
            self._emit("teleop_prepare_rejected", {"reason": str(exc), "session_id": session_id, "layer": "MiniPC ArmStack"})

    def _verify_local_stop(self, *, generation: int) -> bool:
        if self.dry_run:
            with self._lock:
                self._arm_stack_running = False
            return True
        if self._arm_stack is None:
            with self._lock:
                self._arm_stack_running = False
            return True
        result = self._arm_stack.stop_verified()
        ok = bool(result.verified)
        with self._lock:
            if generation == self._teleop_generation:
                self._arm_stack_running = not ok
                if ok:
                    self._arm_stack = None
                    self._scene_setup_active = False
        return ok

    def _do_teleop_stop(self, job: dict[str, Any], *, home: bool) -> None:
        gen = int(job.get("generation") or 0)
        session_id = str(job.get("session_id") or self._teleop_session_id or "")
        with self._lock:
            if gen and gen != self._teleop_generation:
                return
        verified = self._verify_local_stop(generation=gen or self._teleop_generation)
        if not verified:
            self._emit("teleop_stop_failed", {"reason": "verify_failed", "session_id": session_id, "layer": "MiniPC ArmStack"})
            return
        self._emit("teleop_stack_stopped", {"session_id": session_id, "home": home})
        if home:
            self._do_teleop_home({"generation": gen or self._teleop_generation, "session_id": session_id})
        else:
            with self._lock:
                if (gen or self._teleop_generation) == self._teleop_generation:
                    self._teleop_session_id = None

    def _do_teleop_home(self, job: dict[str, Any]) -> None:
        from remote_teleop.phase import run_home_subprocess_cancellable

        gen = int(job.get("generation") or 0)
        session_id = str(job.get("session_id") or self._teleop_session_id or "")
        with self._lock:
            if gen and gen != self._teleop_generation:
                return
            self._home_cancel.clear()
            self._home_running = True
            self._home_result = None
            left = self.left_config_path
            right = self.right_config_path
        self._emit("teleop_home_started", {"session_id": session_id})
        try:
            if not self.dry_run:
                run_home_subprocess_cancellable(
                    python=self.python,
                    left_config=left,
                    right_config=right or None,
                    timeout_sec=120.0,
                    cwd=str(self.client_root),
                    grace_sec=self._teleop_config_loaded().can_release_grace_sec,
                    wait_for_eval=False,
                    cancel_event=self._home_cancel,
                )
            with self._lock:
                if gen and gen != self._teleop_generation:
                    return
                self._home_result = "ok"
                self._home_running = False
                self._teleop_session_id = None
            self._emit("teleop_home_succeeded", {"session_id": session_id})
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                if gen and gen != self._teleop_generation:
                    return
                self._home_result = "failed"
                self._home_running = False
            self._emit("teleop_home_failed", {"session_id": session_id, "reason": str(exc), "layer": "Home"})

    def _do_video_profile(self, job: dict[str, Any]) -> None:
        profile = str(job.get("profile") or "")
        session_id = str(job.get("session_id") or "")
        result = self._wrist_video.apply(profile)
        payload = {
            "session_id": session_id,
            "profile": profile,
            "reason": str(job.get("reason") or "video_health"),
            "detail": result.output,
        }
        self._emit_video_profile_result(payload, result.ok, relay_reply=bool(job.get("relay_reply")))

    def _emit_video_profile_result(
        self, payload: dict[str, Any], applied: bool, *, relay_reply: bool
    ) -> None:
        self._emit("video_profile_applied" if applied else "video_profile_failed", payload)
        if not relay_reply:
            return
        self._send(
            MessageType.VIDEO_PROFILE_RESULT,
            phase="MANUAL_TELEOP",
            reason=str(payload.get("reason") or "video_health"),
            result="applied" if applied else "failed",
            text=str(payload.get("detail") or ""),
            meta={
                "session_id": str(payload.get("session_id") or ""),
                "profile": str(payload.get("profile") or ""),
            },
        )

    def handle_video_profile_command(
        self, payload: dict[str, Any], *, relay_reply: bool = False
    ) -> None:
        """Accept only an active local Teleop session's load-shedding request."""
        if not relay_reply:
            self.note_state_message()
        profile = str(payload.get("profile") or "")
        session_id = str(payload.get("session_id") or "")
        with self._lock:
            instance = str(payload.get("agent_instance_id") or "")
            epoch = payload.get("connection_epoch")
            active_session = self._teleop_session_id
            active = bool(active_session and self._arm_stack_running)
            if instance and instance != self._agent_instance_id:
                return
            if epoch is not None and int(epoch) != self._connection_epoch:
                return
        if profile not in {"normal", "reduced", "head_only"}:
            self._emit_video_profile_result(
                {
                    "session_id": session_id,
                    "profile": profile,
                    "reason": str(payload.get("reason") or "video_health"),
                    "detail": "invalid_profile",
                },
                False,
                relay_reply=relay_reply,
            )
            return
        if not active or session_id != active_session or self._flow_busy() or self._scene_setup_active:
            self._emit_video_profile_result(
                {
                    "session_id": session_id,
                    "profile": profile,
                    "reason": str(payload.get("reason") or "video_health"),
                    "detail": "teleop_not_active",
                },
                False,
                relay_reply=relay_reply,
            )
            return
        self.enqueue_teleop(
            {
                "op": "video_profile",
                "session_id": session_id,
                "profile": profile,
                "reason": str(payload.get("reason") or "video_health"),
                "relay_reply": relay_reply,
            }
        )

    def _emit(self, event_type: str, payload: dict[str, Any]) -> None:
        if self._state_bridge is not None:
            self._state_bridge.send_event(event_type, payload, queue_offline=False)

    def handle_teleop_command(self, event_type: str, payload: dict[str, Any]) -> None:
        self.note_state_message()
        with self._lock:
            instance = str(payload.get("agent_instance_id") or "")
            epoch = payload.get("connection_epoch")
            if instance and instance != self._agent_instance_id:
                return
            if epoch is not None and int(epoch) != self._connection_epoch:
                return
            session_id = str(payload.get("session_id") or self._teleop_session_id or "")
            if event_type == "teleop_prepare":
                if not self._teleop_enabled:
                    self._emit("teleop_prepare_rejected", {"reason": "teleop_disabled", "session_id": session_id})
                    return
                self._teleop_session_id = session_id
                gen = self._teleop_generation
                arm = str(payload.get("arm_session") or "")
            elif event_type in ("teleop_stop", "teleop_abort", "teleop_retry_home"):
                if event_type != "teleop_retry_home":
                    self._teleop_generation += 1
                    self._home_cancel.set()
                gen = self._teleop_generation
                arm = ""
            else:
                return
        if event_type == "teleop_prepare":
            self.enqueue_teleop({"op": "prepare", "generation": gen, "session_id": session_id, "arm_session": arm})
        elif event_type == "teleop_stop":
            self.enqueue_teleop({"op": "stop", "generation": gen, "session_id": session_id})
        elif event_type == "teleop_abort":
            self.enqueue_teleop({"op": "abort", "generation": gen, "session_id": session_id, "reason": payload.get("reason")})
        elif event_type == "teleop_retry_home":
            self.enqueue_teleop({"op": "retry_home", "generation": gen, "session_id": session_id})

    def _maybe_heartbeat_lost(self, *, force: bool = False) -> None:
        with self._lock:
            session_id = self._teleop_session_id
            last = self._state_fresh_at
            gen = self._teleop_generation
        if not session_id:
            return
        if not force and last and (time.monotonic() - last) < 6.0:
            return
        with self._lock:
            self._teleop_generation += 1
            gen = self._teleop_generation
            self._home_cancel.set()
        self.enqueue_teleop({"op": "abort", "generation": gen, "session_id": session_id, "reason": "state_heartbeat_lost"})

    def _spawn_flow(self, bootstrap_id: str, meta: dict | None = None) -> None:
        env = dict(os.environ)
        yam_client = str(Path(__file__).resolve().parent.parent)
        env["PYTHONPATH"] = yam_client + os.pathsep + env.get("PYTHONPATH", "")
        cmd = [
            self.python,
            str(Path(yam_client) / "run_arena_eval_flow.py"),
            "--config",
            self.config_path,
            "--bootstrap-id",
            bootstrap_id,
        ]
        if meta:
            for key, flag in (
                ("instruction", "--instruction"),
                ("scene", "--scene"),
                ("institution", "--institution"),
                ("evaluator", "--evaluator"),
            ):
                val = str(meta.get(key) or "").strip()
                if val:
                    cmd.extend([flag, val])
        print(f"[arena_agent] spawning: {' '.join(cmd)}")
        use_state_flow = self._state_bridge is not None
        stdout = subprocess.PIPE if use_state_flow else None
        self._flow = subprocess.Popen(
            cmd,
            cwd=str(self.client_root),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=subprocess.STDOUT if use_state_flow else None,
            text=True,
            bufsize=1,
        )
        if use_state_flow and self._flow.stdout is not None:
            self._flow_stdout_thread = threading.Thread(
                target=self._drain_flow_stdout,
                name="flow-stdout",
                daemon=True,
            )
            self._flow_stdout_thread.start()

    def _drain_flow_stdout(self) -> None:
        proc = self._flow
        if proc is None or proc.stdout is None:
            return
        try:
            for raw in proc.stdout:
                line = raw.strip()
                if not line:
                    continue
                if not line.startswith("{"):
                    print(f"[arena_agent][flow] {line}")
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError:
                    print(f"[arena_agent][flow] {line}")
                    continue
                etype = str(evt.get("type") or "")
                payload = dict(evt.get("payload") or {})
                if etype == "eval_bind" and payload.get("eval_id"):
                    self.set_flow_eval_id(str(payload["eval_id"]))
                if etype == "session_snapshot":
                    self.update_flow_snapshot(payload)
                if self._state_bridge is not None and etype:
                    self._state_bridge.forward_flow_event(etype, payload)
        finally:
            code = proc.wait()
            print(f"[arena_agent] flow stdout drained, exit={code}")

    def _on_battle_start(self, payload: dict | None = None) -> None:
        raise RuntimeError("This deployment supports standalone Teleop only")
        if self._teleop_session_id:
            print("[arena_agent] teleop active → battle_reject")
            if self._state_bridge is not None:
                self._state_bridge.send_event("battle_reject", {"reason": "teleop_active"})
            return
        battle_meta = dict((payload or {}).get("meta") or {})
        if self._scene_setup_active:
            self._stop_scene_setup_stack()
        if self._flow_busy():
            print("[arena_agent] busy → battle_reject")
            reason = "busy"
            text = "please wait — battle or save/upload still running"
            self._send(
                MessageType.BATTLE_REJECT,
                reason=reason,
                text=text,
            )
            if self._state_bridge is not None:
                self._state_bridge.send_event("battle_reject", {"reason": reason, "text": text})
            return
        bootstrap_id = uuid.uuid4().hex[:12]
        print(f"[arena_agent] accept bootstrap_id={bootstrap_id}")
        self._send(MessageType.BATTLE_ACCEPT, bootstrap_id=bootstrap_id)
        if self._state_bridge is not None:
            self._state_bridge.send_event("battle_accept", {"bootstrap_id": bootstrap_id})
        self._was_busy = True
        self._flow_terminal_ok = False
        self._flow_exit_reported = False
        self._last_flow_exit_nonterminal = False
        self._last_flow_snapshot = {}
        try:
            self._spawn_flow(bootstrap_id, meta=battle_meta or None)
        except OSError as exc:
            print(f"[arena_agent] spawn failed: {exc}", file=sys.stderr)
            self._flow = None
            reason = "spawn_failed"
            self._send(
                MessageType.BATTLE_REJECT,
                reason=reason,
                text=str(exc),
            )
            if self._state_bridge is not None:
                self._state_bridge.send_event(
                    "battle_reject", {"reason": reason, "text": str(exc)}
                )

    def _on_idle_status_query(self) -> None:
        """Operator bootstrap / reconnect while Agent idle (no Flow)."""
        snap = make_message(
            MessageType.SESSION_SNAPSHOT,
            eval_id=AGENT_EVAL_ID,
            phase="IDLE",
            seq=self.endpoint.next_seq(),
            kind=KIND_SYNC,
            robot_id=self.robot_id or None,
            associated=False,
            need_teach=False,
            teach_quota_open=False,
            demo_upload="none",
            mate_expected=False,
        )
        self.endpoint.send_message(snap)
        print("[arena_agent] idle status_query → session_snapshot + arena_ready")
        self._send(MessageType.ARENA_READY, text="status_query_refresh")
        if self._state_bridge is not None:
            self._state_bridge.send_event(
                "session_snapshot",
                self._flow_session_snapshot_payload(),
            )
            self._state_bridge.send_event("arena_ready", {"text": "status_query_refresh"})

    def run(self) -> int:
        print(
            f"[arena_agent] idle on {self.endpoint.relay_host}:{self.endpoint.relay_port} "
            f"session={self.endpoint.session_id}"
        )
        if self._state_bridge is not None:
            self._state_bridge.start()
        from remote_teleop.teleop_ipc import TeleopIpcServer, default_socket_path

        self._ipc = TeleopIpcServer(default_socket_path(self.robot_id), self._handle_ipc)
        self._ipc.start()
        # So a Leader that starts after us knows we can accept battles.
        if self._state_bridge is None:
            self._send(MessageType.ARENA_READY, text="agent_started")
        try:
            while True:
                now = time.monotonic()
                if now - self._last_hb >= 1.0:
                    try:
                        self.endpoint.send_empty_heartbeat()
                    except OSError:
                        pass
                    self._last_hb = now
                self._maybe_announce_ready()
                self._maybe_heartbeat_lost()
                msg = self.endpoint.recv_message(timeout_sec=0.2)
                if msg is None:
                    continue
                if msg.type == MessageType.STATUS_QUERY.value:
                    if self._state_bridge is not None:
                        self._state_bridge._handle_status_query({})
                    elif not self._flow_busy():
                        self._on_idle_status_query()
                    continue
                if msg.eval_id != AGENT_EVAL_ID:
                    continue
                if msg.type == MessageType.VIDEO_PROFILE.value:
                    payload = dict(msg.meta or {})
                    payload.setdefault("reason", msg.reason or "video_health")
                    self.handle_video_profile_command(payload, relay_reply=True)
                    continue
                if msg.type == MessageType.SCENE_SETUP_START.value:
                    self._on_scene_setup_start(msg)
                    continue
                if msg.type == MessageType.SCENE_SETUP_STOP.value:
                    self._on_scene_setup_stop()
                    continue
                if msg.type == MessageType.BATTLE_START.value:
                    self._on_battle_start()
        except KeyboardInterrupt:
            print("\n[arena_agent] interrupted")
            return 130
        finally:
            self._stop_scene_setup_stack()
            if self._flow is not None and self._flow.poll() is None:
                self._flow.terminate()
            self.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Arena resident agent (battle_start → flow)")
    parser.add_argument("--config", default="yam_client/configs/arena_eval.yaml")
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--client-root", default=".")
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    remote = _load_remote(args.config)
    relay = dict(remote.get("relay") or {})
    host = str(relay.get("host") or "")
    if not host:
        print("remote_teleop.relay.host required", file=sys.stderr)
        return 2
    left_cfg, right_cfg, cfg_client_root = _load_config_paths(args.config)
    client_root = Path(args.client_root).resolve()
    if str(cfg_client_root) != ".":
        client_root = cfg_client_root
    agent = ArenaAgent(
        config_path=args.config,
        python=args.python,
        client_root=client_root,
        relay_host=host,
        relay_port=int(relay.get("arm_port", 5005)),
        agent_session=str(relay.get("agent_control_session", "arena_agent_ctrl")),
        robot_id=_load_robot_id(args.config),
        left_config_path=left_cfg,
        right_config_path=right_cfg,
        teleop_config=RemoteTeleopConfig.from_mapping(remote),
        dry_run=bool(args.dry_run),
    )
    return agent.run()


if __name__ == "__main__":
    raise SystemExit(main())

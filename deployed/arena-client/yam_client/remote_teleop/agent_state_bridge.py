"""Bridge State Server commands to ArenaAgent handlers + forward Flow events."""

from __future__ import annotations

import logging
from typing import Any

from remote_teleop.protocol import (
    KIND_FLOW,
    KIND_RESET,
    KIND_REVIEW,
    KIND_TEACH,
    MessageType,
    make_message,
)
from remote_teleop.state_agent import StateAgentClient

log = logging.getLogger(__name__)

_READY_PHASES = frozenset({"OFFLINE", "IDLE", "SESSION_END"})

_STATE_TO_RELAY: dict[str, tuple[MessageType, str, str]] = {
    "teach_start": (MessageType.TEACH_START, "TEACH_ARMED", KIND_TEACH),
    "teach_record_start": (MessageType.TEACH_RECORD_START, "TEACH_RECORDING", KIND_TEACH),
    "teach_record_stop": (MessageType.TEACH_RECORD_STOP, "DEMO_CONFIRM", KIND_TEACH),
    "demo_upload_start": (MessageType.DEMO_ACCEPT, "DEMO_UPLOADING", KIND_TEACH),
    "demo_retry": (MessageType.DEMO_RETRY, "TEACH_ARMED", KIND_TEACH),
    "demo_discard": (MessageType.DEMO_DISCARD, "FINALIZING", KIND_TEACH),
    "finalize_start": (MessageType.RETRY_FINALIZE, "FINALIZING", KIND_FLOW),
    "reset_start": (MessageType.RESET_START, "RESET_ACTIVE", KIND_RESET),
    "reset_done": (MessageType.RESET_DONE, "RESET_ACTIVE", KIND_RESET),
    "reset_abort": (MessageType.RESET_ABORT, "NEED_RESET_AFTER_A", KIND_RESET),
    "recovery_start": (MessageType.RESET_RECOVER, "RESET_ACTIVE", KIND_RESET),
}


_PREPARE_FORWARD: dict[str, str] = {
    "reset_ready": "reset_start",
    "teach_ready": "teach_start",
}


class AgentStateBridge:
    """Dual-path: Relay fallback + FastAPI State when enabled."""

    def __init__(
        self,
        agent: Any,
        *,
        enabled: bool,
        websocket: str,
        token: str,
        robot_id: str,
    ) -> None:
        self.agent = agent
        self.enabled = enabled
        self.robot_id = robot_id
        self._state_snapshot: dict[str, Any] = {}
        self._client: StateAgentClient | None = None
        self._identity_provider = None
        if enabled and websocket and token and robot_id:
            self._client = StateAgentClient(
                websocket_base=websocket,
                token=token,
                robot_id=robot_id,
                on_command=self._on_state_command,
                on_connection=self._on_connection,
            )

    def set_identity_provider(self, provider: Any) -> None:
        self._identity_provider = provider
        if self._client is not None:
            self._client.identity_provider = provider

    def _on_connection(self, connected: bool) -> None:
        on_sock = getattr(self.agent, "on_state_socket", None)
        if on_sock is not None:
            on_sock(connected)
        if not connected:
            return
        self.send_event("yam_online", {})
        snap = self.agent._flow_session_snapshot_payload()
        self.send_event("session_snapshot", snap)

    def start(self) -> None:
        if self._client:
            self._client.start()
            self.send_event("yam_online", {})

    def stop(self) -> None:
        if self._client:
            self.send_event("yam_offline", {"reason": "agent_stopping"})
            self._client.stop()

    def send_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        queue_offline: bool = True,
    ) -> None:
        if self._client is None:
            return
        self._client.send_event(event_type, payload or {}, queue_offline=queue_offline)

    def forward_flow_event(self, event_type: str, payload: dict[str, Any]) -> None:
        payload = dict(payload or {})
        if event_type == "eval_bind" and payload.get("eval_id"):
            self.agent.set_flow_eval_id(str(payload["eval_id"]))
        flow_eval_id = getattr(self.agent, "_flow_eval_id", None)
        if flow_eval_id and "eval_id" not in payload:
            payload["eval_id"] = str(flow_eval_id)
        if event_type == "session_snapshot":
            self.agent.update_flow_snapshot(payload)
        mark = getattr(self.agent, "mark_flow_event", None)
        if mark is not None:
            mark(event_type, payload)
        self.send_event(event_type, payload)

    def _update_state_snapshot(self, payload: dict[str, Any]) -> None:
        self._state_snapshot = dict(payload or {})

    def _state_phase(self) -> str:
        return str(self._state_snapshot.get("phase") or "").strip()

    def _state_allows_ready(self) -> bool:
        return self._state_phase() in _READY_PHASES

    def _handle_status_query(self, payload: dict[str, Any]) -> None:
        if payload.get("phase"):
            self._update_state_snapshot({**self._state_snapshot, **payload})

        busy = self.agent._flow_busy()
        if busy:
            snap = self.agent._flow_session_snapshot_payload()
            self.send_event("session_snapshot", snap)

        if self._state_allows_ready() and not busy:
            self.send_event("arena_ready", {"text": "status_query_refresh"})
            return

        phase = self._state_phase() or "UNKNOWN"
        # SCENE_SETUP/RESET can legitimately have no Flow process: the arm stack
        # is managed independently.  Do not replace its earlier startup error
        # with a misleading "agent idle" error during a status refresh.
        if not busy and phase not in _READY_PHASES and phase not in {"SCENE_SETUP", "RESET_ACTIVE", "TELEOP"}:
            self.send_event(
                "agent_error",
                {
                    "code": "state_active_but_agent_idle",
                    "message": f"State phase is active but Agent has no running flow (phase={phase})",
                    "phase": phase,
                },
            )

    def _relay_forward(self, event_type: str, payload: dict[str, Any]) -> None:
        spec = _STATE_TO_RELAY.get(event_type)
        if spec is None:
            return
        msg_type, phase, kind = spec
        forward_payload = dict(payload)
        for key in ("eval_id", "session_id", "phase", "substate"):
            forward_payload.pop(key, None)
        self.agent.forward_to_flow(msg_type, phase=phase, kind=kind, **forward_payload)

    def _forward_upload_confirm(self, payload: dict[str, Any]) -> None:
        review = dict(payload.get("review") or {})
        if review:
            self.agent.forward_to_flow(
                MessageType.REVIEW_SUBMIT,
                phase="AWAIT_CONFIRM",
                kind=KIND_REVIEW,
                score_a=review.get("score_a", 0),
                score_b=review.get("score_b", 0),
                preference=review.get("preference", "tie"),
                feedback=review.get("feedback", ""),
                need_demo=str(review.get("need_demo", "none")),
                demo_target=str(review.get("demo_target") or review.get("need_demo") or "none"),
            )
        self.agent.forward_to_flow(
            MessageType.UPLOAD_CONFIRM,
            phase="ASSOCIATING",
            kind=KIND_REVIEW,
        )

    def _on_state_command(self, event_type: str, payload: dict[str, Any]) -> None:
        log.info("state→agent %s payload=%s", event_type, payload)
        note = getattr(self.agent, "note_state_message", None)
        if note is not None:
            note()
        if event_type == "robot_snapshot":
            self._update_state_snapshot(payload)
        elif event_type == "agent_hello_ack":
            ack = getattr(self.agent, "note_hello_ack", None)
            if ack is not None:
                ack(payload)
        elif event_type == "teleop_capability_ack":
            ack = getattr(self.agent, "note_capability_ack", None)
            if ack is not None:
                ack(payload)
        elif event_type == "agent_heartbeat_ack":
            pass
        elif event_type in ("teleop_prepare", "teleop_stop", "teleop_abort", "teleop_retry_home"):
            handle = getattr(self.agent, "handle_teleop_command", None)
            if handle is not None:
                handle(event_type, payload)
        elif event_type == "video_profile":
            handle = getattr(self.agent, "handle_video_profile_command", None)
            if handle is not None:
                handle(payload)
        elif event_type == "scene_setup_start":
            msg = make_message(
                MessageType.SCENE_SETUP_START,
                eval_id="agent",
                phase="SCENE_SETUP",
                seq=0,
                robot_id=self.robot_id,
                meta={"arm_session": payload.get("arm_session", "")},
            )
            self.agent._on_scene_setup_start(msg)
            rt = getattr(self.agent, "_arm_stack", None)
            if rt is not None and self.agent._scene_setup_active:
                self.send_event("scene_setup_ready", {})
            elif not self.agent._scene_setup_active:
                self.send_event("scene_setup_rejected", {"reason": "start_failed"})
        elif event_type == "scene_setup_stop":
            self.agent._on_scene_setup_stop()
        elif event_type == "battle_start":
            self.agent._on_battle_start(payload)
        elif event_type == "session_abort":
            try:
                if self.agent._flow is not None and self.agent._flow.poll() is None:
                    self.agent._flow.terminate()
                    self.agent._flow = None
                self.agent._stop_scene_setup_stack()
                self.agent.set_flow_eval_id("")
                snap = self.agent._flow_session_snapshot_payload()
                snap["phase"] = "IDLE"
                self.send_event("session_snapshot", snap)
            except Exception as exc:
                log.warning("session_abort cleanup failed: %s", exc)
                self.send_event("agent_error", {"code": "session_abort", "message": str(exc)})
        elif event_type == "recovery_start":
            if self.agent._flow_busy():
                eval_id = getattr(self.agent, "_flow_eval_id", None)
                if not eval_id:
                    self.send_event("recovery_failed", {"reason": "no_flow_eval_id"})
                    return
                self._relay_forward("recovery_start", {"eval_id": eval_id})
            else:
                self.agent._on_recover_dual_arms(payload)
        elif event_type == "reset_prepare":
            arm = payload.get("arm_session") or ""
            msg = make_message(
                MessageType.SCENE_SETUP_START,
                eval_id=str(payload.get("eval_id") or "agent"),
                phase="NEED_RESET_AFTER_A",
                seq=0,
                robot_id=self.robot_id,
                meta={"arm_session": arm},
            )
            self._prepare_stack_or_ready(
                msg, "reset_ready", "reset_rejected", prepare_label="reset_prepare"
            )
        elif event_type == "reset_start":
            self._relay_forward("reset_start", payload)
        elif event_type in ("reset_done", "reset_abort"):
            self._relay_forward(event_type, payload)
            self.agent._stop_scene_setup_stack()
        elif event_type == "status_query":
            self._handle_status_query(payload)
        elif event_type == "upload_confirm":
            self._forward_upload_confirm(payload)
        elif event_type == "teach_prepare":
            arm = payload.get("arm_session") or ""
            msg = make_message(
                MessageType.SCENE_SETUP_START,
                eval_id=str(payload.get("eval_id") or "agent"),
                phase="NEED_TEACH",
                seq=0,
                robot_id=self.robot_id,
                meta={"arm_session": arm},
            )
            self._prepare_stack_or_ready(
                msg, "teach_ready", "teach_rejected", prepare_label="teach_prepare"
            )
        elif event_type in _STATE_TO_RELAY:
            self._relay_forward(event_type, payload)

    def _prepare_stack_or_ready(
        self,
        msg: Any,
        ready_event: str,
        rejected_event: str,
        *,
        prepare_label: str = "prepare",
    ) -> None:
        """Reset/teach prepare: enable YAM arm stack, then ack State Server."""
        session_id = getattr(self.agent, "_teleop_session_id", None)
        if isinstance(session_id, str) and session_id:
            self.send_event(rejected_event, {"reason": "teleop_active"})
            return
        forward_event = _PREPARE_FORWARD.get(ready_event)
        if self.agent._flow_busy():
            print(f"[arena_agent] {prepare_label}: flow busy")
            if forward_event is None:
                self.send_event(rejected_event, {"reason": "unsupported_prepare"})
                return
            eval_id = getattr(self.agent, "_flow_eval_id", None)
            if not eval_id:
                print(
                    f"[arena_agent] {prepare_label}: no flow eval_id → {rejected_event}"
                )
                self.send_event(rejected_event, {"reason": "no_flow_eval_id"})
                return
            print(
                f"[arena_agent] {prepare_label}: forward {forward_event} "
                f"eval_id={eval_id}"
            )
            self._relay_forward(forward_event, {"eval_id": eval_id})
            self.send_event(ready_event, {"flow_managed_stack": True})
            return
        self.agent._on_scene_setup_start(msg)
        if self.agent._scene_setup_active:
            self.send_event(ready_event, {})
        else:
            self.send_event(rejected_event, {"reason": "prepare_failed"})

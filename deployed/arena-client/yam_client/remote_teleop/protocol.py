"""Control-session message schema for Arena remote teleop + scoring + agent + demo.

Operator.exe path (需求 v1.2.3 / 架构 v1.2) extends the envelope with ``robot_id``
and Operator-flow message types. Legacy LeaderSession messages remain valid without
``robot_id``; new Multi-YAM traffic MUST set ``robot_id``.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any

PHASE_AFTER_A = "after_policy_a"
PHASE_AFTER_B = "after_policy_b"
PHASE_BATTLE = "battle"

# Operator.exe per-robot UI / session_snapshot phases (架构 §5 / §7.4).
OPERATOR_PHASES = frozenset(
    {
        "OFFLINE",
        "IDLE",
        "SCENE_SETUP",
        "START_CONFIRM",
        "AWAIT_ACCEPT",
        "RUNNING_POLICY_A",
        "NEED_RESET_AFTER_A",
        "RESET_ACTIVE",
        "RUNNING_POLICY_B",
        "WAITING_VIDEO",
        "WAITING_REVIEW",
        "REVIEW_ACTIVE",
        "AWAIT_CONFIRM",
        "ASSOCIATING",
        "NEED_TEACH",
        "TEACH_ARMED",
        "TEACH_RECORDING",
        "DEMO_CONFIRM",
        "DEMO_UPLOADING",
        "FINALIZING",
        "SESSION_END",
        "ERROR",
        "OPERATOR_GONE",
        "MANUAL_TELEOP",
    }
)

VALID_PHASES = frozenset({PHASE_AFTER_A, PHASE_AFTER_B, PHASE_BATTLE}) | OPERATOR_PHASES

# Fixed eval_id on the agent control session (not a real Arena eval).
AGENT_EVAL_ID = "agent"

KIND_TELEOP = "teleop"
KIND_PARTIAL_A = "partial_a"
KIND_PARTIAL_B = "partial_b"
KIND_PREF = "pref"
KIND_FEEDBACK = "feedback"
KIND_GRANT = "grant"
KIND_SESSION = "session"
KIND_OPS = "ops"
KIND_META = "meta"
KIND_AGENT = "agent"
KIND_DEMO = "demo"
KIND_RESET = "reset"
KIND_REVIEW = "review"
KIND_FLOW = "flow"
KIND_TEACH = "teach"
KIND_SYNC = "sync"
VALID_KINDS = frozenset(
    {
        KIND_TELEOP,
        KIND_PARTIAL_A,
        KIND_PARTIAL_B,
        KIND_PREF,
        KIND_FEEDBACK,
        KIND_GRANT,
        KIND_SESSION,
        KIND_OPS,
        KIND_META,
        KIND_AGENT,
        KIND_DEMO,
        KIND_RESET,
        KIND_REVIEW,
        KIND_FLOW,
        KIND_TEACH,
        KIND_SYNC,
    }
)

VALID_OPS_CHOICES = frozenset({"retry", "skip", "abort"})
VALID_DEMO_CONFIRM_CHOICES = frozenset({"accept", "retry", "discard"})
VALID_DEMO_UPLOAD_STATES = frozenset({"none", "pending", "uploading", "done", "error"})


class MessageType(str, Enum):
    # Teleop (existing)
    PROBE_REQ = "teleop_probe_req"
    PROBE_ACK = "teleop_probe_ack"
    START = "teleop_start"
    HEARTBEAT = "teleop_heartbeat"
    DONE = "teleop_done"
    ABORT = "teleop_abort"
    ACK = "teleop_ack"
    # Scoring / preference / feedback
    SCORE_REQ = "score_req"
    SCORE_RESP = "score_resp"
    SCORE_SKIP = "score_skip"
    PREF_REQ = "pref_req"
    PREF_RESP = "pref_resp"
    PREF_SKIP = "pref_skip"
    FEEDBACK_REQ = "feedback_req"
    FEEDBACK_RESP = "feedback_resp"
    FEEDBACK_SKIP = "feedback_skip"
    # Grant
    GRANT_REQ = "grant_req"
    GRANT_TELEOP = "grant_teleop"
    GRANT_SKIP = "grant_skip"
    # Session / ops
    SESSION_END = "session_end"
    OPERATOR_ACTION_REQUIRED = "operator_action_required"
    OPS_CHOICE = "ops_choice"
    # Opening meta + eval bind
    META_REQ = "meta_req"
    META_RESP = "meta_resp"
    META_SKIP = "meta_skip"
    EVAL_BIND = "eval_bind"
    # Agent lifecycle (arena_agent_ctrl)
    BATTLE_START = "battle_start"
    BATTLE_ACCEPT = "battle_accept"
    BATTLE_REJECT = "battle_reject"
    SCENE_SETUP_START = "scene_setup_start"
    SCENE_SETUP_ACK = "scene_setup_ack"
    SCENE_SETUP_REJECT = "scene_setup_reject"
    SCENE_SETUP_STOP = "scene_setup_stop"
    ARENA_READY = "arena_ready"
    YAM_ONLINE = "yam_online"
    # Correct demo (CD19: demo_abort.final) — legacy auto-demo path
    DEMO_OFFER = "demo_offer"
    DEMO_DO = "demo_do"
    DEMO_SKIP = "demo_skip"
    DEMO_START = "demo_start"
    ATTEMPT_START = "attempt_start"
    DEMO_CONFIRM_REQ = "demo_confirm_req"
    DEMO_ACCEPT = "demo_accept"
    DEMO_RETRY = "demo_retry"
    DEMO_DISCARD = "demo_discard"
    DEMO_ABORT = "demo_abort"
    # Operator.exe flow (架构 §7.2)
    NEED_RESET_AFTER_A = "need_reset_after_a"
    RESET_START = "reset_start"
    RESET_DONE = "reset_done"
    RESET_ABORT = "reset_abort"
    RESET_RECOVER = "reset_recover"
    POLICY_B_DONE = "policy_b_done"
    VIDEOS_READY = "videos_ready"
    REVIEW_SUBMIT = "review_submit"
    REVIEW_SUBMIT_ACK = "review_submit_ack"
    UPLOAD_CONFIRM = "upload_confirm"
    UPLOAD_CONFIRM_ACK = "upload_confirm_ack"
    DEMO_UPLOAD_DONE = "demo_upload_done"
    FINALIZE_DONE = "finalize_done"
    RETRY_FINALIZE = "retry_finalize"
    BATTLE_DISCARD = "battle_discard"
    TEACH_START = "teach_start"
    TEACH_RECORD_START = "teach_record_start"
    TEACH_RECORD_STOP = "teach_record_stop"
    STATUS_QUERY = "status_query"
    SESSION_SNAPSHOT = "session_snapshot"
    # Diagnostic video load-shedding. State is authoritative when present;
    # Relay is only the explicitly configured legacy fallback transport.
    VIDEO_PROFILE = "video_profile"
    VIDEO_PROFILE_RESULT = "video_profile_result"


# O→M types that MiniPC treats as idempotent under (robot_id, eval_id, type).
IDEMPOTENT_MESSAGE_TYPES = frozenset(
    {
        MessageType.UPLOAD_CONFIRM.value,
        MessageType.DEMO_ACCEPT.value,
        MessageType.BATTLE_DISCARD.value,
        MessageType.REVIEW_SUBMIT.value,
    }
)

_TYPE_DEFAULT_KIND: dict[str, str] = {
    MessageType.PROBE_REQ.value: KIND_TELEOP,
    MessageType.PROBE_ACK.value: KIND_TELEOP,
    MessageType.START.value: KIND_TELEOP,
    MessageType.HEARTBEAT.value: KIND_TELEOP,
    MessageType.DONE.value: KIND_TELEOP,
    MessageType.ABORT.value: KIND_TELEOP,
    MessageType.ACK.value: KIND_TELEOP,
    MessageType.SCORE_REQ.value: KIND_PARTIAL_A,
    MessageType.SCORE_RESP.value: KIND_PARTIAL_A,
    MessageType.SCORE_SKIP.value: KIND_PARTIAL_A,
    MessageType.PREF_REQ.value: KIND_PREF,
    MessageType.PREF_RESP.value: KIND_PREF,
    MessageType.PREF_SKIP.value: KIND_PREF,
    MessageType.FEEDBACK_REQ.value: KIND_FEEDBACK,
    MessageType.FEEDBACK_RESP.value: KIND_FEEDBACK,
    MessageType.FEEDBACK_SKIP.value: KIND_FEEDBACK,
    MessageType.GRANT_REQ.value: KIND_GRANT,
    MessageType.GRANT_TELEOP.value: KIND_GRANT,
    MessageType.GRANT_SKIP.value: KIND_GRANT,
    MessageType.SESSION_END.value: KIND_SESSION,
    MessageType.OPERATOR_ACTION_REQUIRED.value: KIND_OPS,
    MessageType.OPS_CHOICE.value: KIND_OPS,
    MessageType.META_REQ.value: KIND_META,
    MessageType.META_RESP.value: KIND_META,
    MessageType.META_SKIP.value: KIND_META,
    MessageType.EVAL_BIND.value: KIND_SESSION,
    MessageType.BATTLE_START.value: KIND_AGENT,
    MessageType.BATTLE_ACCEPT.value: KIND_AGENT,
    MessageType.BATTLE_REJECT.value: KIND_AGENT,
    MessageType.SCENE_SETUP_START.value: KIND_AGENT,
    MessageType.SCENE_SETUP_ACK.value: KIND_AGENT,
    MessageType.SCENE_SETUP_REJECT.value: KIND_AGENT,
    MessageType.SCENE_SETUP_STOP.value: KIND_AGENT,
    MessageType.ARENA_READY.value: KIND_AGENT,
    MessageType.YAM_ONLINE.value: KIND_AGENT,
    MessageType.VIDEO_PROFILE.value: KIND_AGENT,
    MessageType.VIDEO_PROFILE_RESULT.value: KIND_AGENT,
    MessageType.DEMO_OFFER.value: KIND_DEMO,
    MessageType.DEMO_DO.value: KIND_DEMO,
    MessageType.DEMO_SKIP.value: KIND_DEMO,
    MessageType.DEMO_START.value: KIND_DEMO,
    MessageType.ATTEMPT_START.value: KIND_DEMO,
    MessageType.DEMO_CONFIRM_REQ.value: KIND_DEMO,
    MessageType.DEMO_ACCEPT.value: KIND_DEMO,
    MessageType.DEMO_RETRY.value: KIND_DEMO,
    MessageType.DEMO_DISCARD.value: KIND_DEMO,
    MessageType.DEMO_ABORT.value: KIND_DEMO,
    MessageType.NEED_RESET_AFTER_A.value: KIND_RESET,
    MessageType.RESET_START.value: KIND_RESET,
    MessageType.RESET_DONE.value: KIND_RESET,
    MessageType.RESET_ABORT.value: KIND_RESET,
    MessageType.POLICY_B_DONE.value: KIND_FLOW,
    MessageType.VIDEOS_READY.value: KIND_FLOW,
    MessageType.REVIEW_SUBMIT.value: KIND_REVIEW,
    MessageType.REVIEW_SUBMIT_ACK.value: KIND_REVIEW,
    MessageType.UPLOAD_CONFIRM.value: KIND_REVIEW,
    MessageType.UPLOAD_CONFIRM_ACK.value: KIND_REVIEW,
    MessageType.DEMO_UPLOAD_DONE.value: KIND_FLOW,
    MessageType.FINALIZE_DONE.value: KIND_FLOW,
    MessageType.RETRY_FINALIZE.value: KIND_FLOW,
    MessageType.BATTLE_DISCARD.value: KIND_AGENT,
    MessageType.TEACH_START.value: KIND_TEACH,
    MessageType.TEACH_RECORD_START.value: KIND_TEACH,
    MessageType.TEACH_RECORD_STOP.value: KIND_TEACH,
    MessageType.STATUS_QUERY.value: KIND_SYNC,
    MessageType.SESSION_SNAPSHOT.value: KIND_SYNC,
}

_OPTIONAL_STR_FIELDS = (
    "robot_id",
    "reason",
    "pref",
    "text",
    "bootstrap_id",
    "instruction",
    "scene",
    "institution",
    "evaluator",
    "bound_eval_id",
    "choice",
    "preference",
    "feedback",
    "need_demo",
    "demo_target",
    "result",
    "error",
    "preview_url_a",
    "preview_url_b",
    "demo_upload",
)

_OPTIONAL_FLOAT_FIELDS = ("score", "score_a", "score_b")
_OPTIONAL_BOOL_FIELDS = (
    "final",
    "need_teach",
    "associated",
    "teach_quota_open",
    "mate_expected",
)
_OPTIONAL_INT_FIELDS = ("attempt", "max_attempts", "timeout_ms")


@dataclass(frozen=True)
class ControlMessage:
    type: str
    eval_id: str
    phase: str
    seq: int
    timestamp_ms: int
    kind: str = KIND_TELEOP
    robot_id: str | None = None
    reason: str | None = None
    score: float | None = None
    pref: str | None = None
    text: str | None = None
    bootstrap_id: str | None = None
    instruction: str | None = None
    scene: str | None = None
    institution: str | None = None
    evaluator: str | None = None
    bound_eval_id: str | None = None
    choice: str | None = None
    # Correct demo extensions (CD19)
    final: bool | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    allowed_choices: tuple[str, ...] | None = None
    timeout_ms: int | None = None
    # Operator.exe review / confirm / snapshot
    score_a: float | None = None
    score_b: float | None = None
    preference: str | None = None
    feedback: str | None = None
    need_demo: str | None = None
    demo_target: str | None = None
    result: str | None = None
    need_teach: bool | None = None
    error: str | None = None
    preview_url_a: str | None = None
    preview_url_b: str | None = None
    associated: bool | None = None
    teach_quota_open: bool | None = None
    demo_upload: str | None = None
    mate_expected: bool | None = None
    meta: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        for key in _OPTIONAL_STR_FIELDS:
            if data.get(key) is None:
                data.pop(key, None)
        for key in _OPTIONAL_FLOAT_FIELDS:
            if data.get(key) is None:
                data.pop(key, None)
        for key in _OPTIONAL_BOOL_FIELDS:
            if data.get(key) is None:
                data.pop(key, None)
        for key in _OPTIONAL_INT_FIELDS:
            if data.get(key) is None:
                data.pop(key, None)
        if data.get("allowed_choices") is None:
            data.pop("allowed_choices", None)
        else:
            data["allowed_choices"] = list(data["allowed_choices"])
        if data.get("meta") is None:
            data.pop("meta", None)
        return data

    def abort_final(self) -> bool:
        """CD19: missing final on demo_abort parses as False."""
        return bool(self.final)


def now_ms() -> int:
    return int(time.time() * 1000)


def default_kind_for_type(msg_type: str) -> str:
    return _TYPE_DEFAULT_KIND.get(str(msg_type), KIND_TELEOP)


def idempotency_key(
    robot_id: str,
    eval_id: str,
    msg_type: MessageType | str,
) -> tuple[str, str, str]:
    """Return WO25 key ``(robot_id, eval_id, type)``."""
    rid = str(robot_id).strip()
    eid = str(eval_id).strip()
    if not rid:
        raise ValueError("robot_id required for idempotency key")
    if not eid:
        raise ValueError("eval_id required for idempotency key")
    type_value = msg_type.value if isinstance(msg_type, MessageType) else str(msg_type)
    return (rid, eid, type_value)


def idempotency_key_for_message(message: ControlMessage) -> tuple[str, str, str]:
    if not message.robot_id:
        raise ValueError("robot_id required for idempotency key")
    return idempotency_key(message.robot_id, message.eval_id, message.type)


def is_idempotent_type(msg_type: MessageType | str) -> bool:
    type_value = msg_type.value if isinstance(msg_type, MessageType) else str(msg_type)
    return type_value in IDEMPOTENT_MESSAGE_TYPES


def matches_session(
    message: ControlMessage,
    *,
    eval_id: str,
    phase: str | None = None,
    kinds: frozenset[str] | set[str] | None = None,
    robot_id: str | None = None,
) -> bool:
    """Return True if message belongs to this eval (and optional filters)."""
    if message.eval_id != eval_id:
        return False
    if robot_id is not None and message.robot_id != robot_id:
        return False
    if phase is not None and message.phase != phase:
        return False
    if kinds is not None and message.kind not in kinds:
        return False
    return True


def make_message(
    msg_type: MessageType | str,
    *,
    eval_id: str,
    phase: str,
    seq: int,
    kind: str | None = None,
    robot_id: str | None = None,
    reason: str | None = None,
    score: float | None = None,
    pref: str | None = None,
    text: str | None = None,
    bootstrap_id: str | None = None,
    instruction: str | None = None,
    scene: str | None = None,
    institution: str | None = None,
    evaluator: str | None = None,
    bound_eval_id: str | None = None,
    choice: str | None = None,
    final: bool | None = None,
    attempt: int | None = None,
    max_attempts: int | None = None,
    allowed_choices: list[str] | tuple[str, ...] | None = None,
    timeout_ms: int | None = None,
    score_a: float | None = None,
    score_b: float | None = None,
    preference: str | None = None,
    feedback: str | None = None,
    need_demo: str | None = None,
    demo_target: str | None = None,
    result: str | None = None,
    need_teach: bool | None = None,
    error: str | None = None,
    preview_url_a: str | None = None,
    preview_url_b: str | None = None,
    associated: bool | None = None,
    teach_quota_open: bool | None = None,
    demo_upload: str | None = None,
    mate_expected: bool | None = None,
    meta: dict[str, Any] | None = None,
    timestamp_ms: int | None = None,
    require_robot_id: bool = False,
) -> ControlMessage:
    type_value = msg_type.value if isinstance(msg_type, MessageType) else str(msg_type)
    if phase not in VALID_PHASES:
        raise ValueError(f"invalid phase: {phase!r}")
    kind_value = kind if kind is not None else default_kind_for_type(type_value)
    if kind_value not in VALID_KINDS:
        raise ValueError(f"invalid kind: {kind_value!r}")
    if choice is not None and choice not in VALID_OPS_CHOICES:
        raise ValueError(f"invalid ops choice: {choice!r}")
    if demo_upload is not None and demo_upload not in VALID_DEMO_UPLOAD_STATES:
        raise ValueError(f"invalid demo_upload: {demo_upload!r}")
    if meta is not None and not isinstance(meta, dict):
        raise ValueError("meta must be a dict")
    rid = None if robot_id is None else str(robot_id)
    if require_robot_id and not rid:
        raise ValueError("robot_id required")
    choices_tuple: tuple[str, ...] | None = None
    if allowed_choices is not None:
        choices_tuple = tuple(str(c) for c in allowed_choices)
        bad = [c for c in choices_tuple if c not in VALID_DEMO_CONFIRM_CHOICES]
        if bad:
            raise ValueError(f"invalid demo allowed_choices: {bad!r}")
    return ControlMessage(
        type=type_value,
        eval_id=str(eval_id),
        phase=phase,
        seq=int(seq),
        timestamp_ms=int(timestamp_ms if timestamp_ms is not None else now_ms()),
        kind=kind_value,
        robot_id=rid,
        reason=reason,
        score=None if score is None else float(score),
        pref=None if pref is None else str(pref),
        text=None if text is None else str(text),
        bootstrap_id=None if bootstrap_id is None else str(bootstrap_id),
        instruction=None if instruction is None else str(instruction),
        scene=None if scene is None else str(scene),
        institution=None if institution is None else str(institution),
        evaluator=None if evaluator is None else str(evaluator),
        bound_eval_id=None if bound_eval_id is None else str(bound_eval_id),
        choice=None if choice is None else str(choice),
        final=None if final is None else bool(final),
        attempt=None if attempt is None else int(attempt),
        max_attempts=None if max_attempts is None else int(max_attempts),
        allowed_choices=choices_tuple,
        timeout_ms=None if timeout_ms is None else int(timeout_ms),
        score_a=None if score_a is None else float(score_a),
        score_b=None if score_b is None else float(score_b),
        preference=None if preference is None else str(preference),
        feedback=None if feedback is None else str(feedback),
        need_demo=None if need_demo is None else str(need_demo),
        demo_target=None if demo_target is None else str(demo_target),
        result=None if result is None else str(result),
        need_teach=None if need_teach is None else bool(need_teach),
        error=None if error is None else str(error),
        preview_url_a=None if preview_url_a is None else str(preview_url_a),
        preview_url_b=None if preview_url_b is None else str(preview_url_b),
        associated=None if associated is None else bool(associated),
        teach_quota_open=None if teach_quota_open is None else bool(teach_quota_open),
        demo_upload=None if demo_upload is None else str(demo_upload),
        mate_expected=None if mate_expected is None else bool(mate_expected),
        meta=None if meta is None else dict(meta),
    )


def encode_control_payload(message: ControlMessage | dict[str, Any]) -> bytes:
    payload = message.to_dict() if isinstance(message, ControlMessage) else dict(message)
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def _opt_str(data: dict[str, Any], key: str) -> str | None:
    raw = data.get(key)
    return None if raw is None else str(raw)


def _opt_int(data: dict[str, Any], key: str) -> int | None:
    raw = data.get(key)
    return None if raw is None else int(raw)


def _opt_bool(data: dict[str, Any], key: str) -> bool | None:
    raw = data.get(key)
    if raw is None:
        return None
    return bool(raw)


def _opt_float(data: dict[str, Any], key: str) -> float | None:
    raw = data.get(key)
    return None if raw is None else float(raw)


def decode_control_payload(raw: bytes | str) -> ControlMessage:
    if isinstance(raw, bytes):
        text = raw.decode("utf-8")
    else:
        text = raw
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("control payload must be a JSON object")
    required = ("type", "eval_id", "phase", "seq", "timestamp_ms")
    missing = [key for key in required if key not in data]
    if missing:
        raise ValueError(f"control payload missing fields: {missing}")
    phase = str(data["phase"])
    if phase not in VALID_PHASES:
        raise ValueError(f"invalid phase: {phase!r}")
    type_value = str(data["type"])
    kind = str(data["kind"]) if data.get("kind") is not None else default_kind_for_type(type_value)
    if kind not in VALID_KINDS:
        raise ValueError(f"invalid kind: {kind!r}")
    choice = _opt_str(data, "choice")
    if choice is not None and choice not in VALID_OPS_CHOICES:
        raise ValueError(f"invalid ops choice: {choice!r}")
    demo_upload = _opt_str(data, "demo_upload")
    if demo_upload is not None and demo_upload not in VALID_DEMO_UPLOAD_STATES:
        raise ValueError(f"invalid demo_upload: {demo_upload!r}")
    allowed_raw = data.get("allowed_choices")
    allowed: tuple[str, ...] | None = None
    if allowed_raw is not None:
        if not isinstance(allowed_raw, (list, tuple)):
            raise ValueError("allowed_choices must be a list")
        allowed = tuple(str(c) for c in allowed_raw)
    meta_raw = data.get("meta")
    meta: dict[str, Any] | None = None
    if meta_raw is not None:
        if not isinstance(meta_raw, dict):
            raise ValueError("meta must be a JSON object")
        meta = dict(meta_raw)
    return ControlMessage(
        type=type_value,
        eval_id=str(data["eval_id"]),
        phase=phase,
        seq=int(data["seq"]),
        timestamp_ms=int(data["timestamp_ms"]),
        kind=kind,
        robot_id=_opt_str(data, "robot_id"),
        reason=_opt_str(data, "reason"),
        score=_opt_float(data, "score"),
        pref=_opt_str(data, "pref"),
        text=_opt_str(data, "text"),
        bootstrap_id=_opt_str(data, "bootstrap_id"),
        instruction=_opt_str(data, "instruction"),
        scene=_opt_str(data, "scene"),
        institution=_opt_str(data, "institution"),
        evaluator=_opt_str(data, "evaluator"),
        bound_eval_id=_opt_str(data, "bound_eval_id"),
        choice=choice,
        final=_opt_bool(data, "final"),
        attempt=_opt_int(data, "attempt"),
        max_attempts=_opt_int(data, "max_attempts"),
        allowed_choices=allowed,
        timeout_ms=_opt_int(data, "timeout_ms"),
        score_a=_opt_float(data, "score_a"),
        score_b=_opt_float(data, "score_b"),
        preference=_opt_str(data, "preference"),
        feedback=_opt_str(data, "feedback"),
        need_demo=_opt_str(data, "need_demo"),
        demo_target=_opt_str(data, "demo_target"),
        result=_opt_str(data, "result"),
        need_teach=_opt_bool(data, "need_teach"),
        error=_opt_str(data, "error"),
        preview_url_a=_opt_str(data, "preview_url_a"),
        preview_url_b=_opt_str(data, "preview_url_b"),
        associated=_opt_bool(data, "associated"),
        teach_quota_open=_opt_bool(data, "teach_quota_open"),
        demo_upload=demo_upload,
        mate_expected=_opt_bool(data, "mate_expected"),
        meta=meta,
    )

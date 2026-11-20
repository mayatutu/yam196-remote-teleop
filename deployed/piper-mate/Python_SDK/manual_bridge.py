"""Shared Manual envelope helpers for arm relay bridges."""

from __future__ import annotations

from pathlib import Path

from manual_envelope import (
    MSG_PMRC,
    MSG_PMRS,
    MSG_PROBE_REQ,
    MSG_PROBE_RESP,
    YenvError,
    decode_yenv,
    encode_yenv,
    extract_yenv_from_relay_or_bare,
)
from relay_bridge_proto import (
    RELAY_HEADER_SIZE,
    RELAY_TYPE_HEARTBEAT,
    RELAY_TYPE_RTP,
    RelayHeaderError,
    decode_relay_header,
)


def add_manual_args(parser) -> None:
    parser.add_argument("--manual-envelope-only", action="store_true")
    parser.add_argument("--manual-session-id", default="")
    parser.add_argument("--manual-token-file", default="")
    parser.add_argument("--notify-heartbeat-path", default="")


def load_manual_token(path: str) -> str:
    token = Path(path).read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("manual token file is empty")
    return token


def extract_manual_yenv(packet: bytes, *, token: str, session_id: str, arm_session: str) -> bytes:
    return extract_yenv_from_relay_or_bare(
        packet,
        token=token,
        session_id=session_id,
        arm_session=arm_session,
        decode_relay_header=decode_relay_header,
        relay_header_error=RelayHeaderError,
        relay_header_size=RELAY_HEADER_SIZE,
        relay_type_rtp=RELAY_TYPE_RTP,
        relay_type_heartbeat=RELAY_TYPE_HEARTBEAT,
    )


def unwrap_manual(packet: bytes, *, token: str, session_id: str, arm_session: str):
    yenv = extract_manual_yenv(packet, token=token, session_id=session_id, arm_session=arm_session)
    return decode_yenv(yenv, token=token, session_id=session_id)


def write_heartbeat_notify(path: str, *, local_port: int | None = None) -> None:
    if not path:
        return
    dest = Path(path)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    import json

    tmp.write_text(json.dumps({"ok": True, "local_port": local_port}), encoding="utf-8")
    tmp.replace(dest)

#!/usr/bin/env python3
"""YENV v1 Manual Teleop inner envelope (architecture a1.2 §4)."""

from __future__ import annotations

import hmac
import struct

YENV_MAGIC = b"YENV"
YENV_VERSION = 1
YENV_SESSION_SIZE = 36
YENV_MAC_SIZE = 16
YENV_HEADER_STRUCT = struct.Struct("!4sBBH36sQH")
assert YENV_HEADER_STRUCT.size == 54

MSG_PMRC = 1
MSG_PROBE_REQ = 2
MSG_PROBE_RESP = 3
MSG_PMRS = 4
_VALID_TYPES = frozenset({MSG_PMRC, MSG_PROBE_REQ, MSG_PROBE_RESP, MSG_PMRS})

MAX_DATAGRAM = 1400
PMRC_SIZE = 88


class YenvError(ValueError):
    """Invalid or unauthenticated YENV packet."""


def _pad_session(session_id: str) -> bytes:
    raw = session_id.encode("utf-8")
    if not raw or len(raw) > YENV_SESSION_SIZE:
        raise YenvError("session_id empty or longer than 36 bytes")
    return raw + b"\x00" * (YENV_SESSION_SIZE - len(raw))


def _mac(token: str, body: bytes) -> bytes:
    key = token.encode("utf-8") if isinstance(token, str) else token
    return hmac.new(key, body, "sha256").digest()[:YENV_MAC_SIZE]


def encode_yenv(
    *,
    msg_type: int,
    session_id: str,
    token: str,
    payload: bytes = b"",
    nonce: int = 0,
) -> bytes:
    if msg_type not in _VALID_TYPES:
        raise YenvError(f"unsupported msg_type: {msg_type}")
    if msg_type == MSG_PMRC and len(payload) != PMRC_SIZE:
        raise YenvError("PMRC payload must be 88 bytes")
    if msg_type in (MSG_PROBE_REQ, MSG_PROBE_RESP) and payload:
        raise YenvError("probe payload must be empty")
    if msg_type in (MSG_PMRC, MSG_PMRS) and nonce != 0:
        raise YenvError("PMRC/PMRS nonce must be 0")
    header = YENV_HEADER_STRUCT.pack(
        YENV_MAGIC,
        YENV_VERSION,
        msg_type,
        0,
        _pad_session(session_id),
        nonce,
        len(payload),
    )
    body = header + payload
    packet = body + _mac(token, body)
    if len(packet) > MAX_DATAGRAM:
        raise YenvError(f"YENV datagram too large: {len(packet)}")
    return packet


def decode_yenv(packet: bytes, *, token: str, session_id: str) -> tuple[int, int, bytes]:
    if len(packet) < YENV_HEADER_STRUCT.size + YENV_MAC_SIZE:
        raise YenvError("YENV packet too short")
    body, mac = packet[:-YENV_MAC_SIZE], packet[-YENV_MAC_SIZE:]
    magic, version, msg_type, flags, session_raw, nonce, payload_len = (
        YENV_HEADER_STRUCT.unpack_from(body)
    )
    if magic != YENV_MAGIC:
        raise YenvError("invalid YENV magic")
    if version != YENV_VERSION:
        raise YenvError(f"unsupported YENV version: {version}")
    if flags != 0:
        raise YenvError("YENV flags must be 0")
    if msg_type not in _VALID_TYPES:
        raise YenvError(f"unsupported msg_type: {msg_type}")
    got_session = session_raw.split(b"\x00", 1)[0].decode("utf-8")
    if got_session != session_id:
        raise YenvError("session_id mismatch")
    payload = body[YENV_HEADER_STRUCT.size :]
    if len(payload) != payload_len:
        raise YenvError("payload_len mismatch")
    if not hmac.compare_digest(mac, _mac(token, body)):
        raise YenvError("HMAC mismatch")
    if msg_type == MSG_PMRC and len(payload) != PMRC_SIZE:
        raise YenvError("PMRC payload must be 88 bytes")
    return msg_type, int(nonce), payload


def extract_yenv_from_relay_or_bare(
    packet: bytes,
    *,
    token: str,
    session_id: str,
    arm_session: str,
    decode_relay_header,
    relay_header_error,
    relay_header_size: int,
    relay_type_rtp: int,
    relay_type_heartbeat: int,
) -> bytes:
    """Return raw YENV bytes from Header+YENV or bare YENV. Reject bare PMRC."""
    candidate = packet
    try:
        msg_type, got_session = decode_relay_header(packet)
    except relay_header_error:
        candidate = packet
    else:
        if got_session != arm_session:
            raise YenvError("relay arm_session mismatch")
        if msg_type not in (relay_type_rtp, relay_type_heartbeat):
            raise YenvError("unsupported relay type for Manual")
        candidate = packet[relay_header_size:]
    if candidate.startswith(b"PMRC"):
        raise YenvError("bare PMRC forbidden")
    decode_yenv(candidate, token=token, session_id=session_id)
    return candidate

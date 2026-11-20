"""RelayHeader codec (copied from low-latency-teleop relay_bridge_proto).

Kept local so arena-client does not require piper-mate on PYTHONPATH for
control-session unit tests.
"""

from __future__ import annotations

import struct

RELAY_HEADER_MAGIC = 0x4C545248
RELAY_HEADER_SIZE = 44
RELAY_SESSION_ID_SIZE = 36

RELAY_TYPE_RTP = 1
RELAY_TYPE_HEARTBEAT = 2

_RELAY_HEADER_STRUCT = struct.Struct("!IHH36s")


class RelayHeaderError(ValueError):
    """Raised when relay header bytes are invalid."""


def encode_relay_header(msg_type: int, session_id: str) -> bytes:
    if msg_type not in (RELAY_TYPE_RTP, RELAY_TYPE_HEARTBEAT):
        raise RelayHeaderError(f"unsupported relay type: {msg_type}")
    if not session_id:
        raise RelayHeaderError("session_id cannot be empty")

    session_bytes = session_id.encode("utf-8")
    if len(session_bytes) > RELAY_SESSION_ID_SIZE:
        raise RelayHeaderError(
            f"session_id too long: {len(session_bytes)} > {RELAY_SESSION_ID_SIZE}"
        )

    padded_session = session_bytes + b"\x00" * (RELAY_SESSION_ID_SIZE - len(session_bytes))
    return _RELAY_HEADER_STRUCT.pack(
        RELAY_HEADER_MAGIC,
        RELAY_HEADER_SIZE,
        msg_type,
        padded_session,
    )


def decode_relay_header(packet: bytes) -> tuple[int, str]:
    if len(packet) < RELAY_HEADER_SIZE:
        raise RelayHeaderError(f"packet too short for relay header: {len(packet)}")

    magic, header_size, msg_type, session_raw = _RELAY_HEADER_STRUCT.unpack_from(packet)
    if magic != RELAY_HEADER_MAGIC:
        raise RelayHeaderError(f"invalid relay magic: 0x{magic:08x}")
    if header_size != RELAY_HEADER_SIZE:
        raise RelayHeaderError(f"invalid relay header size: {header_size}")
    if msg_type not in (RELAY_TYPE_RTP, RELAY_TYPE_HEARTBEAT):
        raise RelayHeaderError(f"invalid relay type: {msg_type}")

    session_id = session_raw.split(b"\x00", 1)[0].decode("utf-8", errors="strict")
    if not session_id:
        raise RelayHeaderError("session_id in header is empty")

    return msg_type, session_id

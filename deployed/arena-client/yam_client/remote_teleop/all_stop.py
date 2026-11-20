"""Explicit PMRC v3 all-stop packet construction and local UDP send."""

from __future__ import annotations

import socket
import struct
import time

MAGIC = b"PMRC"
PROTOCOL_VERSION = 3
FLAG_ALL_STOP = 1 << 0
HEADER_STRUCT = struct.Struct("!4sBBBBQ")
ARM_STRUCT = struct.Struct("!BBHI6ii")
COMMAND_PACKET_SIZE = HEADER_STRUCT.size + 2 * ARM_STRUCT.size


def encode_all_stop_packet(*, timestamp_ns: int | None = None) -> bytes:
    """Build an 88-byte PMRC all-stop command (both arm blocks invalid/zero)."""
    ts = int(time.time_ns() if timestamp_ns is None else timestamp_ns)
    header = HEADER_STRUCT.pack(
        MAGIC,
        PROTOCOL_VERSION,
        FLAG_ALL_STOP,
        0,
        0,
        ts,
    )
    # flags=0 => invalid, no gripper; joints zero
    arm = ARM_STRUCT.pack(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    body = header + arm + arm
    if len(body) != COMMAND_PACKET_SIZE:
        raise RuntimeError(f"unexpected all-stop size: {len(body)}")
    return body


def send_all_stop(
    *,
    host: str = "127.0.0.1",
    port: int = 5011,
    retries: int = 3,
    interval_ms: float = 50.0,
) -> None:
    """Send all-stop to the Operator local arm client bridge."""
    packet = encode_all_stop_packet()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for attempt in range(max(1, retries)):
            sock.sendto(packet, (host, int(port)))
            if attempt + 1 < retries:
                time.sleep(max(0.0, interval_ms) / 1000.0)
    finally:
        sock.close()

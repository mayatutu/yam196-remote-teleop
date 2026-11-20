"""Single-session control transport over low-latency-teleop relay.

Wire mapping (locked by relay.c review):

- Operator = sender: sends RELAY_TYPE_RTP + JSON; receives bare JSON
- Robot = receiver: sends empty HEARTBEAT keepalive; sends HEARTBEAT+JSON
  for reverse messages; receives bare JSON

The relay strips RelayHeader when forwarding RTP / HEARTBEAT payloads, so
receivers must parse bare JSON (optionally still tolerate header+JSON).
"""

from __future__ import annotations

import socket
import time
from typing import Callable

from .protocol import ControlMessage, decode_control_payload, encode_control_payload
from .relay_header import (
    RELAY_HEADER_SIZE,
    RELAY_TYPE_HEARTBEAT,
    RELAY_TYPE_RTP,
    RelayHeaderError,
    decode_relay_header,
    encode_relay_header,
)


def parse_incoming_control_packet(data: bytes) -> ControlMessage:
    """Parse a packet as received from the relay (usually bare JSON)."""
    if len(data) >= RELAY_HEADER_SIZE:
        try:
            _msg_type, _session = decode_relay_header(data)
            return decode_control_payload(data[RELAY_HEADER_SIZE:])
        except (RelayHeaderError, ValueError, UnicodeDecodeError):
            pass
    return decode_control_payload(data)


class RelayControlEndpoint:
    """UDP endpoint bound for one control session role."""

    def __init__(
        self,
        *,
        role: str,
        relay_host: str,
        relay_port: int,
        session_id: str,
        bind_host: str = "0.0.0.0",
        bind_port: int = 0,
    ) -> None:
        if role not in {"operator", "robot"}:
            raise ValueError(f"unsupported role: {role!r}")
        self.role = role
        self.relay_host = relay_host
        self.relay_port = int(relay_port)
        self.session_id = session_id
        self._relay_addr = (relay_host, int(relay_port))
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.bind((bind_host, int(bind_port)))
        self._sock.setblocking(False)
        self._seq = 0

    @property
    def sock(self) -> socket.socket:
        return self._sock

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def send_empty_heartbeat(self) -> None:
        """Robot keepalive so relay keeps receiver_addr fresh."""
        if self.role != "robot":
            raise RuntimeError("empty HEARTBEAT is only sent by robot role")
        packet = encode_relay_header(RELAY_TYPE_HEARTBEAT, self.session_id)
        self._sock.sendto(packet, self._relay_addr)

    def send_sender_keepalive(self) -> None:
        """Operator presence: empty RTP so relay learns sender_addr.

        Without this, robot→operator payloads (e.g. meta_req) are dropped because
        the relay has no sender to forward to.
        """
        if self.role != "operator":
            raise RuntimeError("sender keepalive is only sent by operator role")
        packet = encode_relay_header(RELAY_TYPE_RTP, self.session_id)
        self._sock.sendto(packet, self._relay_addr)

    def send_message(self, message: ControlMessage) -> None:
        payload = encode_control_payload(message)
        if self.role == "operator":
            header = encode_relay_header(RELAY_TYPE_RTP, self.session_id)
        else:
            header = encode_relay_header(RELAY_TYPE_HEARTBEAT, self.session_id)
        self._sock.sendto(header + payload, self._relay_addr)

    def recv_message(self, timeout_sec: float = 0.0) -> ControlMessage | None:
        deadline = time.monotonic() + max(0.0, timeout_sec)
        while True:
            remaining = deadline - time.monotonic()
            if timeout_sec > 0 and remaining <= 0:
                return None
            try:
                data, _addr = self._sock.recvfrom(65535)
            except BlockingIOError:
                if timeout_sec <= 0:
                    return None
                time.sleep(min(0.01, max(0.0, remaining)))
                continue
            except OSError:
                return None
            try:
                return parse_incoming_control_packet(data)
            except (ValueError, UnicodeDecodeError):
                continue


def wait_for_message(
    endpoint: RelayControlEndpoint,
    *,
    predicate: Callable[[ControlMessage], bool],
    timeout_sec: float,
    robot_keepalive: bool = False,
    keepalive_interval_sec: float = 1.0,
) -> ControlMessage | None:
    """Poll until a matching message arrives or timeout."""
    deadline = time.monotonic() + timeout_sec
    last_hb = 0.0
    while time.monotonic() < deadline:
        now = time.monotonic()
        if robot_keepalive and endpoint.role == "robot":
            if now - last_hb >= keepalive_interval_sec:
                endpoint.send_empty_heartbeat()
                last_hb = now
        msg = endpoint.recv_message(timeout_sec=0.05)
        if msg is not None and predicate(msg):
            return msg
    return None

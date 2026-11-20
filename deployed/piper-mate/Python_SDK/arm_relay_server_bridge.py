#!/usr/bin/env python3
"""
Pi-side bridge: relay session <-> remote_dual_server.

Flow:
- receives PMRC from relay and forwards to local dual_server
  - real C relay forwards bare payload (header stripped)
  - header+RTP form is still accepted for tests / alternate relays
- receives PMRS from local dual_server and sends to relay (type=HEARTBEAT+payload)
- periodically sends empty heartbeat to keep receiver address fresh in relay
"""

from __future__ import annotations

import argparse
import select
import socket
import time

from relay_bridge_proto import (
    RELAY_HEADER_SIZE,
    RELAY_TYPE_HEARTBEAT,
    RELAY_TYPE_RTP,
    RelayHeaderError,
    decode_relay_header,
    encode_relay_header,
)

PMRC_MAGIC = b"PMRC"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Pi-side arm relay server bridge")
    parser.add_argument("--relay-ip", required=True, help="Relay IP or hostname")
    parser.add_argument("--relay-port", type=int, default=5004, help="Relay UDP port")
    parser.add_argument("--session-id", required=True, help="Arm relay session id")
    parser.add_argument(
        "--dual-server-ip",
        default="127.0.0.1",
        help="remote_dual_server IP",
    )
    parser.add_argument(
        "--dual-server-port",
        type=int,
        default=5012,
        help="remote_dual_server UDP port",
    )
    parser.add_argument(
        "--local-bind-ip",
        default="0.0.0.0",
        help="Local bind IP for this bridge socket",
    )
    parser.add_argument(
        "--local-bind-port",
        type=int,
        default=0,
        help="Local bind UDP port for this bridge (0 means OS assigned)",
    )
    parser.add_argument(
        "--heartbeat-interval",
        type=float,
        default=5.0,
        help="Heartbeat interval seconds (<=0 to disable periodic empty heartbeat)",
    )
    parser.add_argument(
        "--video-session",
        default="",
        help="Optional video session id; if set, must differ from --session-id",
    )
    from manual_bridge import add_manual_args

    add_manual_args(parser)
    return parser.parse_args()


def extract_pmrc_from_relay(packet: bytes, session_id: str) -> bytes | None:
    """Accept bare PMRC (real relay) or Header+RTP payload (tests)."""
    try:
        msg_type, got_session = decode_relay_header(packet)
    except RelayHeaderError:
        if packet.startswith(PMRC_MAGIC):
            return packet
        return packet if packet else None

    if got_session != session_id:
        return None
    if msg_type != RELAY_TYPE_RTP:
        return None
    payload = packet[RELAY_HEADER_SIZE:]
    return payload if payload else None


def main() -> None:
    args = parse_args()
    import ipaddress
    if not ipaddress.ip_address(args.relay_ip).is_loopback:
        from yam_deploy.runtime import require_running
        require_running("bridge")
    if args.video_session and args.video_session == args.session_id:
        raise ValueError("arm session must differ from video session")

    relay_addr = (args.relay_ip, args.relay_port)
    dual_server_addr = (args.dual_server_ip, args.dual_server_port)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.local_bind_ip, args.local_bind_port))
    sock.setblocking(False)
    local_ip, local_port = sock.getsockname()

    print(
        "arm_relay_server_bridge start "
        f"local={local_ip}:{local_port} relay={args.relay_ip}:{args.relay_port} "
        f"session={args.session_id} dual_server={args.dual_server_ip}:{args.dual_server_port}"
        f" manual={bool(args.manual_envelope_only)}"
    )

    manual_token = ""
    if args.manual_envelope_only:
        from manual_bridge import load_manual_token, unwrap_manual, write_heartbeat_notify
        from manual_envelope import MSG_PMRC, MSG_PMRS, MSG_PROBE_REQ, MSG_PROBE_RESP, encode_yenv

        if not args.manual_session_id or not args.manual_token_file:
            raise ValueError("manual envelope requires --manual-session-id and --manual-token-file")
        manual_token = load_manual_token(args.manual_token_file)

    pmrc_forwarded = 0
    pmrs_forwarded = 0
    heartbeat_sent = 0
    dropped = 0
    last_report = time.monotonic()
    next_heartbeat = time.monotonic()
    hb_notified = False

    def send_empty_heartbeat() -> None:
        nonlocal heartbeat_sent, hb_notified
        heartbeat = encode_relay_header(RELAY_TYPE_HEARTBEAT, args.session_id)
        sock.sendto(heartbeat, relay_addr)
        heartbeat_sent += 1
        if args.manual_envelope_only and not hb_notified:
            write_heartbeat_notify(args.notify_heartbeat_path, local_port=local_port)
            hb_notified = True

    # Prime receiver learning at startup.
    send_empty_heartbeat()

    while True:
        now = time.monotonic()
        if args.heartbeat_interval > 0 and now >= next_heartbeat:
            send_empty_heartbeat()
            next_heartbeat = now + args.heartbeat_interval

        timeout = 1.0
        if args.heartbeat_interval > 0:
            timeout = max(0.1, min(timeout, next_heartbeat - now))
        readable, _, _ = select.select([sock], [], [], timeout)
        if not readable:
            if now - last_report >= 5.0:
                print(
                    "[arm-server-bridge] "
                    f"pmrc_fwd={pmrc_forwarded} pmrs_fwd={pmrs_forwarded} "
                    f"hb_sent={heartbeat_sent} dropped={dropped}"
                )
                last_report = now
            continue

        packet, src = sock.recvfrom(4096)
        if src == relay_addr:
            if args.manual_envelope_only:
                try:
                    msg_type, nonce, payload = unwrap_manual(
                        packet,
                        token=manual_token,
                        session_id=args.manual_session_id,
                        arm_session=args.session_id,
                    )
                except Exception:
                    dropped += 1
                    continue
                if msg_type == MSG_PROBE_REQ:
                    resp = encode_yenv(
                        msg_type=MSG_PROBE_RESP,
                        session_id=args.manual_session_id,
                        token=manual_token,
                        nonce=nonce,
                    )
                    sock.sendto(encode_relay_header(RELAY_TYPE_HEARTBEAT, args.session_id) + resp, relay_addr)
                    continue
                if msg_type != MSG_PMRC:
                    dropped += 1
                    continue
                sock.sendto(payload, dual_server_addr)
                pmrc_forwarded += 1
                continue
            payload = extract_pmrc_from_relay(packet, args.session_id)
            if payload is None:
                dropped += 1
                continue
            sock.sendto(payload, dual_server_addr)
            pmrc_forwarded += 1
            continue

        if src != dual_server_addr:
            dropped += 1
            continue

        if args.manual_envelope_only:
            wrapped = encode_yenv(
                msg_type=MSG_PMRS,
                session_id=args.manual_session_id,
                token=manual_token,
                payload=packet,
            )
            relay_packet = encode_relay_header(RELAY_TYPE_HEARTBEAT, args.session_id) + wrapped
        else:
            relay_packet = encode_relay_header(RELAY_TYPE_HEARTBEAT, args.session_id) + packet
        sock.sendto(relay_packet, relay_addr)
        pmrs_forwarded += 1


if __name__ == "__main__":
    main()

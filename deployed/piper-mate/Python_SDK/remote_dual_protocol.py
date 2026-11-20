#!/usr/bin/env python3
"""
PMRC/PMRS v3 protocol for dual-arm PiPER Mate -> Piper teleoperation.

One UDP command packet carries both arms (88 bytes). Status remains JSON.
v1 remains in remote_protocol.py; dual-arm entry points use this module only.
"""

from __future__ import annotations

import json
import struct
from dataclasses import dataclass
from typing import Any

PROTOCOL_VERSION = 3
MAGIC = b"PMRC"
STATUS_MAGIC = "PMRS"

# Packet-level flags
FLAG_ALL_STOP = 1 << 0

# Per-arm flags
FLAG_STOP = 1 << 0
FLAG_GRIPPER_ENABLED = 1 << 1
FLAG_VALID = 1 << 2

ARM_ID_LEFT = 1
ARM_ID_RIGHT = 2
ARM_ID_ALL = 255

ARM_NAME_LEFT = "left"
ARM_NAME_RIGHT = "right"
ARM_NAMES = (ARM_NAME_LEFT, ARM_NAME_RIGHT)

ARM_ID_TO_NAME = {
    ARM_ID_LEFT: ARM_NAME_LEFT,
    ARM_ID_RIGHT: ARM_NAME_RIGHT,
}

HEADER_STRUCT = struct.Struct("!4sBBBBQ")
ARM_STRUCT = struct.Struct("!BBHI6ii")
assert HEADER_STRUCT.size == 16
assert ARM_STRUCT.size == 36
COMMAND_PACKET_SIZE = HEADER_STRUCT.size + 2 * ARM_STRUCT.size
assert COMMAND_PACKET_SIZE == 88

RESERVED_BYTE = 0
RESERVED_U16 = 0


class ProtocolError(ValueError):
    """Raised when a UDP payload is not a valid dual-arm control/status packet."""


@dataclass(frozen=True)
class ArmBlockV3:
    seq: int
    joint_targets: tuple[int, int, int, int, int, int]
    gripper_target: int = 0
    gripper_enabled: bool = True
    stop: bool = False
    valid: bool = True


@dataclass(frozen=True)
class DualCommandV3:
    timestamp_ns: int
    left: ArmBlockV3
    right: ArmBlockV3
    all_stop: bool = False


@dataclass(frozen=True)
class DualCommandPacket:
    """Per-arm view used by server apply path (compatible field set with v2)."""

    arm_id: int
    seq: int
    timestamp_ns: int
    joint_targets: tuple[int, int, int, int, int, int]
    gripper_target: int = 0
    gripper_enabled: bool = True
    stop: bool = False


@dataclass(frozen=True)
class ArmStatus:
    enabled: bool
    seq: int
    executed_count: int
    dropped_old: int
    dropped_stale: int
    invalid_count: int
    unavailable_count: int
    timeout_count: int
    safe_state: bool
    last_packet_age_ms: float | None
    message: str = ""


@dataclass(frozen=True)
class DualStatusPacket:
    arms: dict[str, ArmStatus]


def _pack_arm(block: ArmBlockV3) -> bytes:
    if len(block.joint_targets) != 6:
        raise ProtocolError("joint_targets must contain exactly 6 values")
    flags = 0
    if block.stop:
        flags |= FLAG_STOP
    if block.gripper_enabled:
        flags |= FLAG_GRIPPER_ENABLED
    if block.valid:
        flags |= FLAG_VALID
    seq = int(block.seq) if block.valid else 0
    return ARM_STRUCT.pack(
        flags,
        RESERVED_BYTE,
        RESERVED_U16,
        seq & 0xFFFFFFFF,
        *block.joint_targets,
        block.gripper_target,
    )


def _unpack_arm(data: bytes) -> ArmBlockV3:
    (
        flags,
        _reserved,
        _reserved2,
        seq,
        joint_1,
        joint_2,
        joint_3,
        joint_4,
        joint_5,
        joint_6,
        gripper_target,
    ) = ARM_STRUCT.unpack(data)
    return ArmBlockV3(
        seq=int(seq),
        joint_targets=(joint_1, joint_2, joint_3, joint_4, joint_5, joint_6),
        gripper_target=gripper_target,
        gripper_enabled=bool(flags & FLAG_GRIPPER_ENABLED),
        stop=bool(flags & FLAG_STOP),
        valid=bool(flags & FLAG_VALID),
    )


def encode_command(packet: DualCommandV3) -> bytes:
    flags = 0
    if packet.all_stop:
        flags |= FLAG_ALL_STOP
    header = HEADER_STRUCT.pack(
        MAGIC,
        PROTOCOL_VERSION,
        flags,
        RESERVED_BYTE,
        RESERVED_BYTE,
        packet.timestamp_ns,
    )
    body = header + _pack_arm(packet.left) + _pack_arm(packet.right)
    if len(body) != COMMAND_PACKET_SIZE:
        raise ProtocolError(f"internal encode size error: {len(body)}")
    return body


def decode_command(payload: bytes) -> DualCommandV3:
    if len(payload) != COMMAND_PACKET_SIZE:
        raise ProtocolError(f"invalid command packet size: {len(payload)}")

    magic, version, flags, _r0, _r1, timestamp_ns = HEADER_STRUCT.unpack_from(payload, 0)
    if magic != MAGIC:
        raise ProtocolError("invalid command magic")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version: {version}")

    left = _unpack_arm(payload[HEADER_STRUCT.size : HEADER_STRUCT.size + ARM_STRUCT.size])
    right = _unpack_arm(payload[HEADER_STRUCT.size + ARM_STRUCT.size :])
    return DualCommandV3(
        timestamp_ns=int(timestamp_ns),
        left=left,
        right=right,
        all_stop=bool(flags & FLAG_ALL_STOP),
    )


def is_no_motion_probe(cmd: DualCommandV3) -> bool:
    """Legal 88B probe: both arms valid=0, not all-stop, not per-arm stop."""
    return (
        not cmd.all_stop
        and not cmd.left.valid
        and not cmd.right.valid
        and not cmd.left.stop
        and not cmd.right.stop
    )


def encode_no_motion_probe(*, timestamp_ns: int = 0) -> bytes:
    zero = ArmBlockV3(
        seq=0,
        joint_targets=(0, 0, 0, 0, 0, 0),
        gripper_target=0,
        gripper_enabled=False,
        stop=False,
        valid=False,
    )
    return encode_command(
        DualCommandV3(timestamp_ns=timestamp_ns, left=zero, right=zero, all_stop=False)
    )


def arm_block_to_packet(
    arm_id: int,
    block: ArmBlockV3,
    timestamp_ns: int,
) -> DualCommandPacket:
    return DualCommandPacket(
        arm_id=arm_id,
        seq=block.seq,
        timestamp_ns=timestamp_ns,
        joint_targets=block.joint_targets,
        gripper_target=block.gripper_target,
        gripper_enabled=block.gripper_enabled,
        stop=block.stop,
    )


def _arm_status_to_dict(status: ArmStatus) -> dict[str, Any]:
    return {
        "enabled": status.enabled,
        "seq": status.seq,
        "executed_count": status.executed_count,
        "dropped_old": status.dropped_old,
        "dropped_stale": status.dropped_stale,
        "invalid_count": status.invalid_count,
        "unavailable_count": status.unavailable_count,
        "timeout_count": status.timeout_count,
        "safe_state": status.safe_state,
        "last_packet_age_ms": status.last_packet_age_ms,
        "message": status.message,
    }


def _arm_status_from_dict(data: dict[str, Any]) -> ArmStatus:
    return ArmStatus(
        enabled=bool(data["enabled"]),
        seq=int(data["seq"]),
        executed_count=int(data["executed_count"]),
        dropped_old=int(data["dropped_old"]),
        dropped_stale=int(data["dropped_stale"]),
        invalid_count=int(data["invalid_count"]),
        unavailable_count=int(data["unavailable_count"]),
        timeout_count=int(data["timeout_count"]),
        safe_state=bool(data["safe_state"]),
        last_packet_age_ms=(
            None
            if data.get("last_packet_age_ms") is None
            else float(data["last_packet_age_ms"])
        ),
        message=str(data.get("message", "")),
    )


def encode_status(packet: DualStatusPacket) -> bytes:
    arms: dict[str, Any] = {}
    for arm_name in ARM_NAMES:
        if arm_name not in packet.arms:
            raise ProtocolError(f"missing arm status: {arm_name}")
        arms[arm_name] = _arm_status_to_dict(packet.arms[arm_name])

    data = {
        "magic": STATUS_MAGIC,
        "version": PROTOCOL_VERSION,
        "arms": arms,
    }
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


def decode_status(payload: bytes) -> DualStatusPacket:
    try:
        data = json.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("invalid status packet") from exc

    if data.get("magic") != STATUS_MAGIC:
        raise ProtocolError("invalid status magic")
    if data.get("version") != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported status version: {data.get('version')}")

    arms_data = data.get("arms")
    if not isinstance(arms_data, dict):
        raise ProtocolError("missing arms object in status packet")

    arms: dict[str, ArmStatus] = {}
    for arm_name in ARM_NAMES:
        if arm_name not in arms_data:
            raise ProtocolError(f"missing arm status: {arm_name}")
        arms[arm_name] = _arm_status_from_dict(arms_data[arm_name])

    return DualStatusPacket(arms=arms)

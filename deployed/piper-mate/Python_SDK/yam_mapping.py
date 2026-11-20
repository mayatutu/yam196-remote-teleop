#!/usr/bin/env python3
"""
YAM mapping helpers for PiPER Mate dual-arm teleoperation.

This module is intentionally side-effect free so it can be reused by:
- runtime server
- dry-run verification
- future unit tests
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

U16_MOD = 65536


@dataclass(frozen=True)
class ArmMappingConfig:
    joint_signs: np.ndarray
    joint_scales: np.ndarray
    joint_limits: np.ndarray  # shape (6, 2), radians
    joint_rate_limits: np.ndarray  # shape (6,), rad/s — NORMAL track (absolute + delta)
    gripper_scale: float
    gripper_offset: float
    gripper_limits: tuple[float, float]
    # Absolute map: yam = leader * signs * scales + offsets (radians).
    joint_offsets: np.ndarray = field(default_factory=lambda: np.zeros(6, dtype=np.float64))
    # ALIGNING park rate limits (rad/s). Defaults to joint_rate_limits if omitted at construct.
    align_rate_limits: np.ndarray = field(default_factory=lambda: np.full(6, 2.0, dtype=np.float64))


def clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(value, max_value))


def packet_joint_mdeg_to_rad(joint_targets: tuple[int, int, int, int, int, int]) -> np.ndarray:
    # PMRC v3 in this repo carries millidegree joint targets.
    deg = np.asarray(joint_targets, dtype=np.float64) * 0.001
    return np.deg2rad(deg)


def packet_gripper_um_to_m(gripper_target: int) -> float:
    return float(gripper_target) / 1_000_000.0


def compute_joint_target_absolute(
    leader_rad: np.ndarray,
    mapping: ArmMappingConfig,
) -> np.ndarray:
    """Map leader joint angles (rad) to YAM joint targets (rad) in absolute space."""
    leader = np.asarray(leader_rad, dtype=np.float64)
    target = leader * mapping.joint_signs * mapping.joint_scales + mapping.joint_offsets
    return np.clip(target, mapping.joint_limits[:, 0], mapping.joint_limits[:, 1])


def compute_joint_target_from_delta(
    leader_current_rad: np.ndarray,
    leader_base_rad: np.ndarray,
    yam_base_rad: np.ndarray,
    mapping: ArmMappingConfig,
) -> np.ndarray:
    # With yam_base = abs(leader_base), this equals compute_joint_target_absolute(leader_current).
    delta = (leader_current_rad - leader_base_rad) * mapping.joint_signs * mapping.joint_scales
    target = yam_base_rad + delta
    return np.clip(target, mapping.joint_limits[:, 0], mapping.joint_limits[:, 1])


def apply_joint_rate_limit(
    prev_joint_target: np.ndarray | None,
    new_joint_target: np.ndarray,
    dt_s: float,
    joint_rate_limits: np.ndarray,
) -> np.ndarray:
    if prev_joint_target is None:
        return new_joint_target
    if dt_s <= 0:
        return prev_joint_target
    max_step = joint_rate_limits * dt_s
    delta = new_joint_target - prev_joint_target
    clipped_delta = np.clip(delta, -max_step, max_step)
    return prev_joint_target + clipped_delta


def joints_within_tol(a: np.ndarray, b: np.ndarray, tol_rad: float) -> bool:
    return bool(np.max(np.abs(np.asarray(a, dtype=np.float64) - np.asarray(b, dtype=np.float64))) <= tol_rad)


def compute_gripper_target_abs(gripper_m: float, mapping: ArmMappingConfig) -> float:
    target = gripper_m * mapping.gripper_scale + mapping.gripper_offset
    return clamp(target, min(mapping.gripper_limits), max(mapping.gripper_limits))


def pack_command_vector_7(joint_target_rad: np.ndarray, gripper_target: float) -> np.ndarray:
    command = np.zeros(7, dtype=np.float64)
    command[:6] = joint_target_rad
    command[6] = gripper_target
    return command


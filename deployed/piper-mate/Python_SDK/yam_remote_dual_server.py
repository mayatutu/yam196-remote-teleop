#!/usr/bin/env python3
"""
Raspberry Pi-side dual-arm YAM UDP server (PMRC/PMRS v3 compatible).

Design goals:
- keep wire compatibility with existing remote_dual_client.py
- direct i2rt control path (no gello runtime dependency)
- per-arm isolation (fault in one arm must not crash the other arm loop)
- all_stop short-circuit: decode -> safe_state, bypass mapper
- dry_run mode works without CAN/i2rt hardware
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import socket
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from remote_dual_protocol import (
    ARM_ID_LEFT,
    ARM_ID_RIGHT,
    ARM_NAME_LEFT,
    ARM_NAME_RIGHT,
    ARM_NAMES,
    ArmStatus,
    DualCommandV3,
    DualStatusPacket,
    ProtocolError,
    arm_block_to_packet,
    decode_command,
    encode_status,
    is_no_motion_probe,
)
from yam_mapping import (
    ArmMappingConfig,
    apply_joint_rate_limit,
    compute_gripper_target_abs,
    compute_joint_target_absolute,
    compute_joint_target_from_delta,
    joints_within_tol,
    pack_command_vector_7,
    packet_gripper_um_to_m,
    packet_joint_mdeg_to_rad,
)

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    yaml = None

try:
    from i2rt.robots.get_robot import get_yam_robot
    from i2rt.robots.utils import GripperType
except Exception:
    get_yam_robot = None
    GripperType = None


STATE_WAITING_BASELINE = "waitingBaseline"
STATE_ALIGNING = "aligning"
STATE_NORMAL = "normal"
STATE_STALE_HOLD = "staleHold"
STATE_SAFE = "armSafeState"
STATE_UNAVAILABLE = "armUnavailable"

SEQ_WRAP_THRESHOLD = 65400
# Client restart: seq jumps from high values back near 0 (same as remote_dual_server).
SEQ_RESYNC_PREV_MIN = 500
SEQ_RESYNC_NEW_MAX = 64
SEQ_RESYNC_GAP_MIN = 400
# Late UDP frames within this gap are dropped, not treated as a replay/jump.
SEQ_REORDER_TOL = 128

MSG_NOT_APPLYING = (
    "receiving but not applying (fail-closed; restart server or new authorized session)"
)
MSG_MOTOR_CHAIN_DEAD = (
    "MOTOR CHAIN DEAD — stop teleop, re-enable motors (reset CAN + set_timeout), restart server"
)
WATCHDOG_HARD_REASON = "watchdog hard timeout"
SOFT_RECOVERY_PACKETS = 3
COMM_WATCHDOG_STATES = (
    STATE_WAITING_BASELINE,
    STATE_ALIGNING,
    STATE_NORMAL,
    STATE_STALE_HOLD,
)

# Damiao / i2rt MotorErrorCode names → operator hints (see i2rt motor_drivers/utils.py).
MOTOR_FAULT_HINTS: dict[str, str] = {
    "disabled": (
        "电机已失能(0x0)。常为过流/过载/通信保护之后的结果，不一定等于根因码本身；"
        "查此前是否有 over current / overload / loss communication。"
    ),
    "over current": "过流(0xA)：目标跳变过大、机械卡死或负载过大。",
    "overload": "过载(0xE)：持续力矩过高，放慢遥操作或检查碰撞/限位。",
    "over voltage": "过压(0x8)：检查供电与电源线。",
    "under voltage": "欠压(0x9)：检查供电与接触。",
    "mosfet over temperature": "MOS 过温(0xB)：停机降温后再试。",
    "motor over temperature": "绕组过温(0xC)：停机降温后再试。",
    "loss communication": "丢通信(0xD)：检查 CAN / 线束 / set_timeout 看门狗。",
    "normal": "正常(0x1)。",
}
MOTOR_ID_TO_JOINT: dict[int, str] = {
    1: "J1",
    2: "J2",
    3: "J3",
    4: "J4(DM4310较弱)",
    5: "J5(DM4310较弱)",
    6: "J6(DM4310较弱)",
    7: "gripper",
}

DEFAULT_HANG_RING_SIZE = 120  # ~2s at 60Hz
# i2rt often logs the real fault on a background thread before the control thread dies.
_LAST_MOTOR_ERROR_LINES: deque[str] = deque(maxlen=16)


class _MotorErrorLogCapture(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:
            return
        low = msg.lower()
        if "motor id:" in low or "failed with info" in low or "error: disabled" in low:
            _LAST_MOTOR_ERROR_LINES.append(msg)


def install_motor_error_log_capture() -> None:
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, _MotorErrorLogCapture):
            return
    handler = _MotorErrorLogCapture()
    handler.setLevel(logging.ERROR)
    root.addHandler(handler)

DEFAULT_JOINT_LIMITS = np.array(
    [
        [-2.767, 3.28],
        [-0.15, 3.80],
        [-0.15, 3.28],
        [-1.72, 1.72],
        [-1.72, 1.72],
        [-2.24, 2.24],
    ],
    dtype=np.float64,
)


class MockRobot:
    def __init__(self, joint_limits: np.ndarray):
        self._state = np.zeros(7, dtype=np.float64)
        self._joint_limits = joint_limits.copy()
        self._closed = False

    def get_joint_pos(self) -> np.ndarray:
        return self._state.copy()

    def command_joint_pos(self, joint_pos: np.ndarray) -> None:
        target = np.asarray(joint_pos, dtype=np.float64).copy()
        target[:6] = np.clip(target[:6], self._joint_limits[:, 0], self._joint_limits[:, 1])
        self._state = target

    def get_observations(self) -> dict[str, np.ndarray]:
        z = np.zeros_like(self._state)
        return {
            "joint_pos": self._state.copy(),
            "joint_vel": z.copy(),
            "joint_eff": z.copy(),
        }

    def get_robot_info(self) -> dict[str, Any]:
        return {
            "joint_limits": self._joint_limits.copy(),
            "gripper_limits": np.array([0.0, 1.0], dtype=np.float64),
        }

    def close(self) -> None:
        self._closed = True

    def zero_torque_mode(self) -> None:
        self._zero_torque = True


@dataclass
class ArmRuntime:
    name: str
    can_name: str
    mapping: ArmMappingConfig
    enabled: bool = True
    robot: Any | None = None
    state: str = STATE_WAITING_BASELINE
    state_reason: str = "startup"
    seq_last: int = -1
    last_valid_monotonic: float | None = None
    last_packet_timestamp_ns: int = 0
    leader_base_rad: np.ndarray | None = None
    yam_base_rad: np.ndarray | None = None
    align_target_rad: np.ndarray | None = None
    align_started_monotonic: float | None = None
    baseline_window: deque[tuple[float, np.ndarray]] = field(default_factory=lambda: deque(maxlen=8))
    last_joint_command: np.ndarray | None = None
    last_command_monotonic: float | None = None
    executed_count: int = 0
    dropped_old: int = 0
    dropped_stale: int = 0
    invalid_count: int = 0
    unavailable_count: int = 0
    timeout_count: int = 0
    # packets_since_log == successful executes in the log window; recv_since_log == accepted seq.
    packets_since_log: int = 0
    recv_since_log: int = 0
    message: str = ""
    disabled_message: str = ""
    # Latest J3 diagnostics for periodic logs (Mate delta vs YAM target/limits).
    debug_j3_leader_delta_deg: float | None = None
    debug_j3_target_rad: float | None = None
    # Set True when this cycle accepted a forward/first/wrap seq (for post-execute watchdog refresh).
    touched_this_cycle: bool = False
    # Pre-hang telemetry.
    debug_dt_s: float = 0.0
    debug_step_clipped: bool = False
    hang_ring: deque = field(default_factory=lambda: deque(maxlen=DEFAULT_HANG_RING_SIZE))
    debug_run_dir: Path | None = None
    last_fault_detail: str = ""
    hang_dumped: bool = False
    stale_recovery_count: int = 0
    watchdog_recovery_allowed: bool = False

    def set_state(self, new_state: str, reason: str) -> None:
        self.state = new_state
        self.state_reason = reason
        self.message = reason

    @property
    def safe_state(self) -> bool:
        return self.state == STATE_SAFE

    @property
    def unavailable(self) -> bool:
        return self.state == STATE_UNAVAILABLE

    def clear_baseline(self) -> None:
        self.leader_base_rad = None
        self.yam_base_rad = None
        self.align_target_rad = None
        self.align_started_monotonic = None
        self.baseline_window.clear()


def parse_float_list(text: str, expected_len: int) -> np.ndarray:
    values = [float(x.strip()) for x in text.split(",") if x.strip() != ""]
    if len(values) != expected_len:
        raise ValueError(f"Expected {expected_len} values, got {len(values)} from '{text}'")
    return np.asarray(values, dtype=np.float64)


def load_yaml_config(path: str | None) -> dict[str, Any]:
    if not path:
        return {}
    if yaml is None:
        raise RuntimeError("PyYAML is required when --config is set")
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError("Top-level YAML config must be a mapping/object")
    return data


def build_defaults_from_layers(config_data: dict[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "bind_ip": "0.0.0.0",
        "port": 5010,
        "left_can": "can_left",
        "right_can": "can_right",
        "timeout_ms": 600.0,
        "stale_hold_ms": 150.0,
        "hard_timeout_ms": 600.0,
        "status_rate": 1.0,
        "allow_single_arm": True,
        "auto_resume": False,
        "dry_run": False,
        "no_motion_probe": False,
        "run_preinit": False,
        "baseline_samples": 3,
        "baseline_max_vel_deg_s": 5.0,
        "gripper_type": "linear_4310",
        "zero_gravity_mode": True,
        "gripper_enabled": True,
        "mapping_mode": "absolute",
        "left_joint_signs": "1,1,1,1,1,1",
        "right_joint_signs": "1,1,1,1,1,1",
        "left_joint_scales": "1,1,1,1,1,1",
        "right_joint_scales": "1,1,1,1,1,1",
        "left_joint_offsets": "0,0,0,0,0,0",
        "right_joint_offsets": "0,0,0,0,0,0",
        "left_align_rate_limit_rad_s": "2,2,2,2,2,2",
        "right_align_rate_limit_rad_s": "2,2,2,2,2,2",
        # Absolute NORMAL also applies these caps (UDP teleop safety).
        "left_joint_rate_limit_rad_s": "8,8,6,8,8,8",
        "right_joint_rate_limit_rad_s": "8,8,6,8,8,8",
        "align_tol_rad": 0.03,
        "align_timeout_s": 15.0,
        "left_gripper_scale": 12.5,  # 0.08m * 12.5 clamped by gripper_max
        "right_gripper_scale": 12.5,
        "left_gripper_offset": 0.0,
        "right_gripper_offset": 0.0,
        "left_gripper_min": 0.0,
        "left_gripper_max": 0.75,
        "right_gripper_min": 0.0,
        "right_gripper_max": 0.75,
        "debug_run_dir": "",
        "hang_ring_size": DEFAULT_HANG_RING_SIZE,
    }

    # YAML layer
    defaults.update({k: v for k, v in config_data.items() if k in defaults})
    if "hard_timeout_ms" not in config_data and "timeout_ms" in config_data:
        defaults["hard_timeout_ms"] = float(defaults["timeout_ms"])

    # Env layer
    env_map = {
        "bind_ip": "YAM_REMOTE_BIND_IP",
        "port": "YAM_REMOTE_PORT",
        "left_can": "YAM_REMOTE_LEFT_CAN",
        "right_can": "YAM_REMOTE_RIGHT_CAN",
        "timeout_ms": "YAM_REMOTE_TIMEOUT_MS",
        "stale_hold_ms": "YAM_REMOTE_STALE_HOLD_MS",
        "hard_timeout_ms": "YAM_REMOTE_HARD_TIMEOUT_MS",
        "status_rate": "YAM_REMOTE_STATUS_RATE",
        "allow_single_arm": "YAM_REMOTE_ALLOW_SINGLE_ARM",
        "dry_run": "YAM_REMOTE_DRY_RUN",
        "run_preinit": "YAM_REMOTE_RUN_PREINIT",
    }
    for key, env_name in env_map.items():
        value = os.environ.get(env_name)
        if value is None:
            continue
        if isinstance(defaults[key], bool):
            defaults[key] = value.lower() in ("1", "true", "yes", "on")
        elif isinstance(defaults[key], int):
            defaults[key] = int(value)
        elif isinstance(defaults[key], float):
            defaults[key] = float(value)
        else:
            defaults[key] = value
    if os.environ.get("YAM_REMOTE_HARD_TIMEOUT_MS") is None and os.environ.get("YAM_REMOTE_TIMEOUT_MS"):
        defaults["hard_timeout_ms"] = float(defaults["timeout_ms"])
    return defaults


def build_parser(defaults: dict[str, Any]) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Dual-arm YAM UDP server (PMRC/PMRS v3)")
    parser.add_argument("--config", default=None, help="Optional YAML config path")
    parser.add_argument("--bind-ip", default=defaults["bind_ip"])
    parser.add_argument("--port", type=int, default=int(defaults["port"]))
    parser.add_argument("--left-can", default=defaults["left_can"])
    parser.add_argument("--right-can", default=defaults["right_can"])
    parser.add_argument("--timeout-ms", type=float, default=float(defaults["timeout_ms"]))
    parser.add_argument("--stale-hold-ms", type=float, default=float(defaults["stale_hold_ms"]))
    parser.add_argument("--hard-timeout-ms", type=float, default=float(defaults["hard_timeout_ms"]))
    parser.add_argument("--status-rate", type=float, default=float(defaults["status_rate"]))
    parser.add_argument(
        "--allow-single-arm",
        action=argparse.BooleanOptionalAction,
        default=bool(defaults["allow_single_arm"]),
    )
    parser.add_argument(
        "--auto-resume",
        action=argparse.BooleanOptionalAction,
        default=bool(defaults["auto_resume"]),
    )
    parser.add_argument(
        "--dry-run",
        action=argparse.BooleanOptionalAction,
        default=bool(defaults["dry_run"]),
    )
    parser.add_argument(
        "--no-motion-probe",
        action=argparse.BooleanOptionalAction,
        default=bool(defaults.get("no_motion_probe", False)),
        help="Accept dual-arm valid=0 probe PMRC without changing safe/enable/seq/targets",
    )
    parser.add_argument(
        "--run-preinit",
        action=argparse.BooleanOptionalAction,
        default=bool(defaults["run_preinit"]),
    )
    parser.add_argument(
        "--gripper-enabled",
        action=argparse.BooleanOptionalAction,
        default=bool(defaults["gripper_enabled"]),
    )
    parser.add_argument("--gripper-type", default=defaults["gripper_type"])
    parser.add_argument(
        "--zero-gravity-mode",
        action=argparse.BooleanOptionalAction,
        default=bool(defaults["zero_gravity_mode"]),
    )
    parser.add_argument("--baseline-samples", type=int, default=int(defaults["baseline_samples"]))
    parser.add_argument(
        "--baseline-max-vel-deg-s",
        type=float,
        default=float(defaults["baseline_max_vel_deg_s"]),
    )
    parser.add_argument(
        "--mapping-mode",
        choices=("absolute", "delta"),
        default=str(defaults["mapping_mode"]),
        help="absolute: GELLO-style park then abs follow; delta: relative from lock pose",
    )
    parser.add_argument("--left-joint-signs", default=str(defaults["left_joint_signs"]))
    parser.add_argument("--right-joint-signs", default=str(defaults["right_joint_signs"]))
    parser.add_argument("--left-joint-scales", default=str(defaults["left_joint_scales"]))
    parser.add_argument("--right-joint-scales", default=str(defaults["right_joint_scales"]))
    parser.add_argument("--left-joint-offsets", default=str(defaults["left_joint_offsets"]))
    parser.add_argument("--right-joint-offsets", default=str(defaults["right_joint_offsets"]))
    parser.add_argument(
        "--left-align-rate-limit-rad-s",
        default=str(defaults["left_align_rate_limit_rad_s"]),
    )
    parser.add_argument(
        "--right-align-rate-limit-rad-s",
        default=str(defaults["right_align_rate_limit_rad_s"]),
    )
    parser.add_argument(
        "--left-joint-rate-limit-rad-s",
        default=str(defaults["left_joint_rate_limit_rad_s"]),
    )
    parser.add_argument(
        "--right-joint-rate-limit-rad-s",
        default=str(defaults["right_joint_rate_limit_rad_s"]),
    )
    parser.add_argument("--align-tol-rad", type=float, default=float(defaults["align_tol_rad"]))
    parser.add_argument("--align-timeout-s", type=float, default=float(defaults["align_timeout_s"]))
    parser.add_argument("--left-gripper-scale", type=float, default=float(defaults["left_gripper_scale"]))
    parser.add_argument("--right-gripper-scale", type=float, default=float(defaults["right_gripper_scale"]))
    parser.add_argument("--left-gripper-offset", type=float, default=float(defaults["left_gripper_offset"]))
    parser.add_argument("--right-gripper-offset", type=float, default=float(defaults["right_gripper_offset"]))
    parser.add_argument("--left-gripper-min", type=float, default=float(defaults["left_gripper_min"]))
    parser.add_argument("--left-gripper-max", type=float, default=float(defaults["left_gripper_max"]))
    parser.add_argument("--right-gripper-min", type=float, default=float(defaults["right_gripper_min"]))
    parser.add_argument("--right-gripper-max", type=float, default=float(defaults["right_gripper_max"]))
    parser.add_argument(
        "--debug-run-dir",
        default=str(defaults.get("debug_run_dir", "")),
        help="Directory for hang telemetry CSV (default: data/mate_teleop_debug/<timestamp>)",
    )
    parser.add_argument(
        "--hang-ring-size",
        type=int,
        default=int(defaults.get("hang_ring_size", DEFAULT_HANG_RING_SIZE)),
        help="Pre-hang ring buffer length in frames (default 120 ≈ 2s at 60Hz)",
    )
    return parser


def parse_args() -> argparse.Namespace:
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--config", default=None)
    pre_args, _ = pre_parser.parse_known_args()
    config_data = load_yaml_config(pre_args.config)
    defaults = build_defaults_from_layers(config_data)
    parser = build_parser(defaults)
    args = parser.parse_args()
    if args.timeout_ms <= 0:
        raise ValueError("--timeout-ms must be greater than 0")
    if args.stale_hold_ms <= 0:
        raise ValueError("--stale-hold-ms must be greater than 0")
    if args.hard_timeout_ms <= 0:
        raise ValueError("--hard-timeout-ms must be greater than 0")
    if args.stale_hold_ms >= args.hard_timeout_ms:
        raise ValueError("--stale-hold-ms must be less than --hard-timeout-ms")
    args.timeout_ms = float(args.hard_timeout_ms)
    if args.status_rate <= 0:
        raise ValueError("--status-rate must be greater than 0")
    if args.baseline_samples <= 0:
        raise ValueError("--baseline-samples must be greater than 0")
    if args.baseline_max_vel_deg_s <= 0:
        raise ValueError("--baseline-max-vel-deg-s must be greater than 0")
    if args.align_tol_rad <= 0:
        raise ValueError("--align-tol-rad must be greater than 0")
    if args.align_timeout_s <= 0:
        raise ValueError("--align-timeout-s must be greater than 0")
    mode = str(args.mapping_mode).strip().lower()
    if mode not in ("absolute", "delta"):
        raise ValueError("--mapping-mode must be 'absolute' or 'delta'")
    args.mapping_mode = mode
    if int(args.hang_ring_size) <= 0:
        raise ValueError("--hang-ring-size must be greater than 0")
    return args


def ensure_debug_run_dir(args: argparse.Namespace) -> Path:
    """Create (or reuse) the run directory under YAM data/ for hang dumps."""
    configured = str(getattr(args, "debug_run_dir", "") or "").strip()
    if configured:
        run_dir = Path(configured).expanduser()
    else:
        # Prefer cwd/data when launched from ~/YAM; else repo-root/data.
        cwd_data = Path.cwd() / "data" / "mate_teleop_debug"
        repo_root = Path(__file__).resolve().parents[2]
        base = cwd_data if (Path.cwd() / "piper-mate").is_dir() else (repo_root / "data" / "mate_teleop_debug")
        run_dir = base / time.strftime("%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    args.debug_run_dir = str(run_dir)
    return run_dir


def read_arm_feedback(robot: Any) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Return (q6, eff6, temp_mos6, temp_rotor6); missing temps are nan."""
    q = np.full(6, np.nan, dtype=np.float64)
    eff = np.full(6, np.nan, dtype=np.float64)
    t_mos = np.full(6, np.nan, dtype=np.float64)
    t_rotor = np.full(6, np.nan, dtype=np.float64)
    if robot is None:
        return q, eff, t_mos, t_rotor
    try:
        if hasattr(robot, "get_observations"):
            obs = robot.get_observations()
            pos = np.asarray(obs.get("joint_pos", []), dtype=np.float64).reshape(-1)
            ef = np.asarray(obs.get("joint_eff", []), dtype=np.float64).reshape(-1)
            n = min(6, pos.shape[0])
            q[:n] = pos[:n]
            n_e = min(6, ef.shape[0])
            eff[:n_e] = ef[:n_e]
            if "temp_mos" in obs:
                tm = np.asarray(obs["temp_mos"], dtype=np.float64).reshape(-1)
                t_mos[: min(6, tm.shape[0])] = tm[:6]
            if "temp_rotor" in obs:
                tr = np.asarray(obs["temp_rotor"], dtype=np.float64).reshape(-1)
                t_rotor[: min(6, tr.shape[0])] = tr[:6]
            return q, eff, t_mos, t_rotor
    except Exception:
        pass
    try:
        pos = np.asarray(robot.get_joint_pos(), dtype=np.float64).reshape(-1)
        n = min(6, pos.shape[0])
        q[:n] = pos[:n]
    except Exception:
        pass
    return q, eff, t_mos, t_rotor


def record_hang_sample(arm: ArmRuntime, command: np.ndarray) -> None:
    """Push one pre-command sample into the arm hang ring buffer."""
    q_star = np.asarray(command[:6], dtype=np.float64)
    q, eff, t_mos, t_rotor = read_arm_feedback(arm.robot)
    err = q_star - q
    sample = {
        "t_mono": time.monotonic(),
        "seq": int(arm.seq_last),
        "state": arm.state,
        "dt_s": float(arm.debug_dt_s),
        "step_clipped": bool(arm.debug_step_clipped),
        "q_star": q_star.copy(),
        "q": q.copy(),
        "err": err.copy(),
        "eff": eff.copy(),
        "temp_mos": t_mos.copy(),
        "temp_rotor": t_rotor.copy(),
        "max_abs_err": float(np.nanmax(np.abs(err))) if np.any(np.isfinite(err)) else float("nan"),
        "max_abs_eff": float(np.nanmax(np.abs(eff))) if np.any(np.isfinite(eff)) else float("nan"),
    }
    arm.hang_ring.append(sample)


def dump_hang_telemetry(arm: ArmRuntime, detail: str = "") -> Path | None:
    """Write hang ring CSV under arm.debug_run_dir; return path or None."""
    if arm.hang_dumped:
        return None
    run_dir = arm.debug_run_dir
    if run_dir is None:
        return None
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    out_path = run_dir / f"hang_{arm.name}.csv"
    fieldnames = [
        "t_mono",
        "seq",
        "state",
        "dt_s",
        "step_clipped",
        "max_abs_err",
        "max_abs_eff",
    ]
    for i in range(1, 7):
        fieldnames.extend(
            [
                f"qstar_j{i}",
                f"q_j{i}",
                f"err_j{i}",
                f"eff_j{i}",
                f"temp_mos_j{i}",
                f"temp_rotor_j{i}",
            ]
        )
    with out_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for sample in arm.hang_ring:
            row: dict[str, Any] = {
                "t_mono": f"{sample['t_mono']:.6f}",
                "seq": sample["seq"],
                "state": sample["state"],
                "dt_s": f"{sample['dt_s']:.6f}",
                "step_clipped": int(sample["step_clipped"]),
                "max_abs_err": sample["max_abs_err"],
                "max_abs_eff": sample["max_abs_eff"],
            }
            for i in range(6):
                j = i + 1
                row[f"qstar_j{j}"] = float(sample["q_star"][i])
                row[f"q_j{j}"] = float(sample["q"][i])
                row[f"err_j{j}"] = float(sample["err"][i])
                row[f"eff_j{j}"] = float(sample["eff"][i])
                row[f"temp_mos_j{j}"] = float(sample["temp_mos"][i])
                row[f"temp_rotor_j{j}"] = float(sample["temp_rotor"][i])
            writer.writerow(row)
        if detail:
            f.write(f"# fault_detail: {detail.replace(chr(10), ' | ')}\n")
    arm.hang_dumped = True
    print(f"\n[{arm.name}] hang telemetry dumped → {out_path} (n={len(arm.hang_ring)})", flush=True)
    return out_path


def resolve_fault_detail(detail: str = "") -> str:
    text = (detail or "").strip()
    if text and "motor id:" in text.lower():
        return text
    if _LAST_MOTOR_ERROR_LINES:
        # Prefer the newest line that parses to a motor id.
        for line in reversed(_LAST_MOTOR_ERROR_LINES):
            mid, _ = parse_motor_fault_from_text(line)
            if mid is not None:
                if text:
                    return f"{text} | {line}"
                return line
        if not text:
            return _LAST_MOTOR_ERROR_LINES[-1]
    return text


def run_preinit_if_requested(args):
    if args.run_preinit:
        raise RuntimeError('Preinitialization is prohibited in this deployment')
    if not args.dry_run:
        from yam_deploy.runtime import require_running
        require_running('server')
        if args.allow_single_arm or args.bind_ip != '127.0.0.1' or args.port != 5012 or args.left_can != 'can_left' or args.right_can != 'can_right':
            raise RuntimeError('Real Server must use both commissioned arms and the configured loopback endpoint')


def build_mapping_from_args(args: argparse.Namespace, arm_name: str, joint_limits: np.ndarray) -> ArmMappingConfig:
    if arm_name == ARM_NAME_LEFT:
        signs = parse_float_list(args.left_joint_signs, 6)
        scales = parse_float_list(args.left_joint_scales, 6)
        offsets = parse_float_list(args.left_joint_offsets, 6)
        rate_limits = parse_float_list(args.left_joint_rate_limit_rad_s, 6)
        align_rates = parse_float_list(args.left_align_rate_limit_rad_s, 6)
        g_scale = args.left_gripper_scale
        g_offset = args.left_gripper_offset
        g_limits = (args.left_gripper_min, args.left_gripper_max)
    else:
        signs = parse_float_list(args.right_joint_signs, 6)
        scales = parse_float_list(args.right_joint_scales, 6)
        offsets = parse_float_list(args.right_joint_offsets, 6)
        rate_limits = parse_float_list(args.right_joint_rate_limit_rad_s, 6)
        align_rates = parse_float_list(args.right_align_rate_limit_rad_s, 6)
        g_scale = args.right_gripper_scale
        g_offset = args.right_gripper_offset
        g_limits = (args.right_gripper_min, args.right_gripper_max)

    return ArmMappingConfig(
        joint_signs=signs,
        joint_scales=scales,
        joint_limits=joint_limits.copy(),
        joint_rate_limits=rate_limits,
        gripper_scale=float(g_scale),
        gripper_offset=float(g_offset),
        gripper_limits=(float(min(g_limits)), float(max(g_limits))),
        joint_offsets=offsets,
        align_rate_limits=align_rates,
    )


def create_robot(can_name, args):
    if args.dry_run:
        return MockRobot(DEFAULT_JOINT_LIMITS)
    from yam_deploy.robot import open_arm
    return open_arm(can_name)


def setup_arms(args: argparse.Namespace) -> dict[str, ArmRuntime]:
    run_dir = ensure_debug_run_dir(args)
    ring_size = int(getattr(args, "hang_ring_size", DEFAULT_HANG_RING_SIZE))
    arms: dict[str, ArmRuntime] = {}
    for name, can_name in ((ARM_NAME_LEFT, args.left_can), (ARM_NAME_RIGHT, args.right_can)):
        arm = ArmRuntime(name=name, can_name=can_name, mapping=build_mapping_from_args(args, name, DEFAULT_JOINT_LIMITS))
        arm.hang_ring = deque(maxlen=ring_size)
        arm.debug_run_dir = run_dir
        try:
            arm.robot = create_robot(can_name, args)
            info = arm.robot.get_robot_info()
            joint_limits = info.get("joint_limits")
            if joint_limits is not None:
                # Temporary teleop safety: keep J3 below ~150deg (full Mate range is ~170deg).
                limits = np.asarray(joint_limits, dtype=np.float64).copy()
                limits[2, 1] = min(float(limits[2, 1]), float(np.deg2rad(150.0)))
                arm.mapping = build_mapping_from_args(args, name, limits)
            arm.set_state(STATE_WAITING_BASELINE, "initialized")
        except Exception as exc:
            arm.unavailable_count += 1
            arm.enabled = False
            arm.disabled_message = str(exc)
            arm.set_state(STATE_UNAVAILABLE, f"startup unavailable: {exc}")
            if not args.allow_single_arm:
                from yam_deploy.robot import close_many
                close_many([a.robot for a in arms.values()] + [arm.robot])
                raise
        arms[name] = arm

    if not any(arm.enabled for arm in arms.values()):
        raise RuntimeError("No arm available after initialization")
    notes = run_dir / "notes.txt"
    notes.write_text(
        f"hang telemetry run dir\n"
        f"created={time.strftime('%Y-%m-%d %H:%M:%S')}\n"
        f"hang_ring_size={ring_size}\n"
        f"mapping_mode={args.mapping_mode}\n"
        f"track_rate left={args.left_joint_rate_limit_rad_s} right={args.right_joint_rate_limit_rad_s}\n",
        encoding="utf-8",
    )
    return arms


def enter_safe_state(
    arm: ArmRuntime,
    reason: str,
    *,
    auto_resume: bool | None = None,
    recovery_allowed: bool = False,
) -> None:
    was_safe = arm.state == STATE_SAFE
    prev_reason = arm.state_reason
    arm.set_state(STATE_SAFE, reason)
    arm.clear_baseline()
    arm.last_joint_command = None
    arm.last_command_monotonic = None
    arm.stale_recovery_count = 0
    arm.watchdog_recovery_allowed = bool(recovery_allowed)
    robot = arm.robot
    if robot is not None:
        zero = getattr(robot, "zero_torque_mode", None)
        if callable(zero):
            try:
                zero()
            except Exception as exc:  # noqa: BLE001
                print(f"\n[{arm.name}] zero_torque_mode failed: {exc}", flush=True)
    if not was_safe or prev_reason != reason:
        resume_txt = ""
        if auto_resume is not None:
            resume_txt = f" (auto_resume={auto_resume})"
        print(f"\n[{arm.name}] ENTER {STATE_SAFE}: {reason}{resume_txt}", flush=True)


def enter_stale_hold(arm: ArmRuntime) -> None:
    if arm.state == STATE_STALE_HOLD:
        return
    arm.set_state(STATE_STALE_HOLD, "watchdog stale hold")
    arm.stale_recovery_count = 0
    print(f"\n[{arm.name}] ENTER {STATE_STALE_HOLD}: hold last command", flush=True)


def maybe_resync_client_seq(arm: ArmRuntime, new_seq: int) -> bool:
    """Detect client restart (high seq -> near-zero) and reset seq window."""
    if (
        arm.seq_last >= SEQ_RESYNC_PREV_MIN
        and new_seq <= SEQ_RESYNC_NEW_MAX
        and (arm.seq_last - new_seq) >= SEQ_RESYNC_GAP_MIN
    ):
        print(
            f"\n[{arm.name}] Seq restart detected: last={arm.seq_last} "
            f"incoming={new_seq}; resyncing",
            flush=True,
        )
        arm.seq_last = -1
        return True
    return False


def classify_seq(last_seq: int, new_seq: int) -> str:
    """Classify a u32 command seq. Small UDP reordering is not a safety jump."""
    if last_seq < 0:
        return "first"
    if new_seq == last_seq:
        return "duplicate"
    if new_seq < last_seq:
        backward = last_seq - new_seq
        if last_seq > SEQ_WRAP_THRESHOLD and new_seq <= SEQ_RESYNC_NEW_MAX:
            return "wrap"
        if backward <= SEQ_REORDER_TOL:
            return "backward"
        return "jump"
    return "forward"


def is_baseline_quality_good(
    window: deque[tuple[float, np.ndarray]],
    required_samples: int,
    max_vel_rad_s: float,
) -> bool:
    if len(window) < required_samples:
        return False
    items = list(window)[-required_samples:]
    for (t0, q0), (t1, q1) in zip(items[:-1], items[1:]):
        dt = max(1e-6, t1 - t0)
        vel = np.max(np.abs((q1 - q0) / dt))
        if vel > max_vel_rad_s:
            return False
    return True


def arm_chain_alive(robot: Any) -> bool:
    """True if the i2rt control thread is still running (or dry-run MockRobot)."""
    if robot is None:
        return False
    motor_chain = getattr(robot, "motor_chain", None)
    if motor_chain is None:
        # MockRobot / dry-run: no motor_chain attribute.
        return True
    return bool(getattr(motor_chain, "running", False))


def parse_motor_fault_from_text(text: str) -> tuple[int | None, str | None]:
    """Extract (motor_id, error_name) from i2rt / RuntimeError strings."""
    motor_id: int | None = None
    error_name: str | None = None
    m_id = re.search(r"motor id:\s*(\d+)", text, flags=re.IGNORECASE)
    if m_id:
        motor_id = int(m_id.group(1))
    if motor_id is None:
        m_info = re.search(r"failed with info\s*\[\s*(\d+)", text, flags=re.IGNORECASE)
        if m_info:
            motor_id = int(m_info.group(1))
    m_err = re.search(r"error:\s*([^\n,(]+)", text, flags=re.IGNORECASE)
    if m_err:
        error_name = m_err.group(1).strip().rstrip(".")
    return motor_id, error_name


def format_motor_fault_report(arm: ArmRuntime, detail: str = "") -> str:
    """Human-readable multi-line fault report for operators."""
    detail = resolve_fault_detail(detail)
    motor_id, error_name = parse_motor_fault_from_text(detail)
    joint = MOTOR_ID_TO_JOINT.get(motor_id, "?") if motor_id is not None else "?"
    err_key = (error_name or "").lower()
    hint = MOTOR_FAULT_HINTS.get(err_key, "未知/未解析错误码；请对照上方原始异常与 i2rt 日志。")
    lines = [
        f"[{arm.name}] MOTOR FAULT DETAIL can={arm.can_name}",
        f"  motor_id={motor_id if motor_id is not None else '?'} joint={joint}",
        f"  error={error_name or '(unparsed)'} — {hint}",
    ]
    if arm.debug_j3_leader_delta_deg is not None or arm.debug_j3_target_rad is not None:
        j3_d = (
            f"{arm.debug_j3_leader_delta_deg:+.1f}deg"
            if arm.debug_j3_leader_delta_deg is not None
            else "-"
        )
        j3_t = f"{arm.debug_j3_target_rad:+.3f}" if arm.debug_j3_target_rad is not None else "-"
        lines.append(f"  last j3_d={j3_d} j3_t={j3_t} rad")
    if arm.last_joint_command is not None and arm.last_joint_command.shape[0] >= 4:
        q = arm.last_joint_command
        lines.append(
            f"  last cmd J3={q[2]:+.3f} J4={q[3]:+.3f} rad "
            f"(J3={np.rad2deg(q[2]):+.1f}deg J4={np.rad2deg(q[3]):+.1f}deg)"
        )
    if arm.hang_ring:
        last = arm.hang_ring[-1]
        lines.append(
            f"  last sample max|err|={last['max_abs_err']:.4f} rad "
            f"max|eff|={last['max_abs_eff']:.4f} clipped={int(last['step_clipped'])}"
        )
    if detail:
        lines.append(f"  raw: {detail}")
    lines.append(
        "  recover: stop teleop → reset CAN + set_timeout --timeout → restart server; "
        "大行程时注意 absolute track 限速与机械负载。"
    )
    return "\n".join(lines)


def mark_motor_chain_dead(
    arm: ArmRuntime,
    detail: str = "",
    *,
    arms: dict[str, ArmRuntime] | None = None,
    auto_resume: bool = True,
) -> None:
    if arm.state == STATE_UNAVAILABLE and MSG_MOTOR_CHAIN_DEAD in (arm.message or ""):
        return
    detail = resolve_fault_detail(detail)
    suffix = f" ({detail})" if detail else ""
    msg = MSG_MOTOR_CHAIN_DEAD + suffix
    arm.unavailable_count += 1
    arm.enabled = False
    report = format_motor_fault_report(arm, detail)
    arm.set_state(STATE_UNAVAILABLE, msg)
    arm.clear_baseline()
    print(f"\n[{arm.name}] {msg}", flush=True)
    print(report, flush=True)
    dump_hang_telemetry(arm, detail)
    if arms is None:
        return
    for peer in arms.values():
        if peer.name == arm.name or not peer.enabled:
            continue
        if peer.state in (STATE_UNAVAILABLE, STATE_SAFE):
            continue
        enter_safe_state(peer, "peer MOTOR CHAIN DEAD", auto_resume=auto_resume)


def maybe_lock_baseline(arm: ArmRuntime, leader_rad: np.ndarray, args: argparse.Namespace, now: float) -> bool:
    arm.baseline_window.append((now, leader_rad.copy()))
    max_vel_rad_s = np.deg2rad(args.baseline_max_vel_deg_s)
    if not is_baseline_quality_good(arm.baseline_window, args.baseline_samples, max_vel_rad_s):
        return False
    arm.leader_base_rad = arm.baseline_window[-1][1].copy()
    current = np.asarray(arm.robot.get_joint_pos(), dtype=np.float64)
    if current.shape[0] < 7:
        raise RuntimeError(f"{arm.name}: expected 7 dofs, got {current.shape[0]}")
    arm.last_joint_command = current[:6].copy()
    arm.last_command_monotonic = now
    arm.baseline_window.clear()

    mode = str(getattr(args, "mapping_mode", "absolute")).lower()
    if mode == "delta":
        arm.yam_base_rad = current[:6].copy()
        arm.align_target_rad = None
        arm.align_started_monotonic = None
        arm.set_state(STATE_NORMAL, "baseline locked (delta)")
        return True

    # absolute (GELLO-style): freeze abs(leader) and park before free tracking.
    arm.yam_base_rad = compute_joint_target_absolute(arm.leader_base_rad, arm.mapping)
    arm.align_target_rad = arm.yam_base_rad.copy()
    arm.align_started_monotonic = now
    err = float(np.max(np.abs(arm.last_joint_command - arm.align_target_rad)))
    if err > 0.25:
        print(
            f"\n[{arm.name}] align warn: max |yam-target|={err:.3f} rad; "
            "parking slowly — hold Mate still",
            flush=True,
        )
    arm.set_state(STATE_ALIGNING, "aligning")
    return True


def compute_command_for_arm(
    arm: ArmRuntime,
    packet_arm: Any,
    now: float,
    args: argparse.Namespace,
    *,
    arms: dict[str, ArmRuntime] | None = None,
) -> np.ndarray | None:
    # Note: DualCommandPacket has no `.valid` field; callers must skip invalid
    # ArmBlockV3 before converting via arm_block_to_packet().
    new_seq = int(packet_arm.seq)
    maybe_resync_client_seq(arm, new_seq)
    seq_result = classify_seq(arm.seq_last, new_seq)
    if seq_result in ("duplicate", "backward"):
        arm.dropped_old += 1
        if arm.state == STATE_STALE_HOLD:
            arm.stale_recovery_count = 0
        return None
    if seq_result == "jump":
        arm.dropped_stale += 1
        enter_safe_state(arm, "seq abnormal jump", auto_resume=bool(args.auto_resume))
        return None

    arm.seq_last = new_seq
    arm.last_valid_monotonic = now
    arm.touched_this_cycle = True
    arm.recv_since_log += 1

    if packet_arm.stop:
        enter_safe_state(arm, "stop requested", auto_resume=bool(args.auto_resume))
        return None

    if not arm.enabled:
        arm.unavailable_count += 1
        return None

    if arm.state == STATE_UNAVAILABLE:
        return None

    if not arm_chain_alive(arm.robot):
        mark_motor_chain_dead(
            arm,
            arms=arms,
            auto_resume=bool(args.auto_resume),
        )
        return None

    if arm.state == STATE_SAFE:
        if args.auto_resume or arm.watchdog_recovery_allowed:
            msg = (
                "watchdog hard-timeout rebaseline"
                if arm.watchdog_recovery_allowed and not args.auto_resume
                else "auto_resume rebaseline"
            )
            arm.watchdog_recovery_allowed = False
            arm.set_state(STATE_WAITING_BASELINE, msg)
            arm.clear_baseline()
        else:
            arm.message = MSG_NOT_APPLYING
            return None

    leader_joint_rad = packet_joint_mdeg_to_rad(tuple(packet_arm.joint_targets))
    leader_gripper_m = packet_gripper_um_to_m(int(packet_arm.gripper_target))

    if arm.state == STATE_STALE_HOLD:
        arm.stale_recovery_count += 1
        if arm.stale_recovery_count < SOFT_RECOVERY_PACKETS:
            arm.message = (
                f"staleHold confirm {arm.stale_recovery_count}/{SOFT_RECOVERY_PACKETS}"
            )
            return None
        mode = str(getattr(args, "mapping_mode", "absolute")).lower()
        if mode == "delta":
            if arm.leader_base_rad is None or arm.yam_base_rad is None:
                arm.set_state(STATE_WAITING_BASELINE, "staleHold rebaseline")
                arm.clear_baseline()
            else:
                arm.align_target_rad = compute_joint_target_from_delta(
                    leader_joint_rad,
                    arm.leader_base_rad,
                    arm.yam_base_rad,
                    arm.mapping,
                )
                arm.align_started_monotonic = now
                arm.last_command_monotonic = now
                arm.stale_recovery_count = 0
                arm.set_state(STATE_ALIGNING, "staleHold slew")
        else:
            arm.align_target_rad = compute_joint_target_absolute(leader_joint_rad, arm.mapping)
            arm.align_started_monotonic = now
            arm.last_command_monotonic = now
            arm.stale_recovery_count = 0
            arm.set_state(STATE_ALIGNING, "staleHold slew")
        if arm.last_joint_command is None:
            current = np.asarray(arm.robot.get_joint_pos(), dtype=np.float64)
            arm.last_joint_command = current[:6].copy()

    if arm.state == STATE_WAITING_BASELINE:
        locked = maybe_lock_baseline(arm, leader_joint_rad, args, now)
        if not locked:
            return None

    if arm.leader_base_rad is None or arm.yam_base_rad is None:
        return None

    mode = str(getattr(args, "mapping_mode", "absolute")).lower()
    arm.debug_dt_s = 0.0
    arm.debug_step_clipped = False

    if arm.state == STATE_ALIGNING:
        if arm.align_target_rad is None:
            arm.align_target_rad = arm.yam_base_rad.copy()
        desired = arm.align_target_rad.copy()
        target_joints = desired
        if arm.last_joint_command is not None and arm.last_command_monotonic is not None:
            dt = now - arm.last_command_monotonic
            arm.debug_dt_s = float(dt)
            target_joints = apply_joint_rate_limit(
                prev_joint_target=arm.last_joint_command,
                new_joint_target=desired,
                dt_s=dt,
                joint_rate_limits=arm.mapping.align_rate_limits,
            )
            arm.debug_step_clipped = not np.allclose(desired, target_joints, atol=1e-9, equal_nan=False)
        if joints_within_tol(target_joints, arm.align_target_rad, float(args.align_tol_rad)):
            target_joints = arm.align_target_rad.copy()
            arm.set_state(STATE_NORMAL, "aligned")
            print(f"\n[{arm.name}] align complete → normal", flush=True)
    elif mode == "delta":
        desired = compute_joint_target_from_delta(
            leader_joint_rad,
            arm.leader_base_rad,
            arm.yam_base_rad,
            arm.mapping,
        )
        target_joints = desired
        if arm.last_joint_command is not None and arm.last_command_monotonic is not None:
            dt = now - arm.last_command_monotonic
            arm.debug_dt_s = float(dt)
            target_joints = apply_joint_rate_limit(
                prev_joint_target=arm.last_joint_command,
                new_joint_target=desired,
                dt_s=dt,
                joint_rate_limits=arm.mapping.joint_rate_limits,
            )
            arm.debug_step_clipped = not np.allclose(desired, target_joints, atol=1e-9, equal_nan=False)
    else:
        # absolute NORMAL: absolute target + track rate limit (UDP teleop safety).
        desired = compute_joint_target_absolute(leader_joint_rad, arm.mapping)
        target_joints = desired
        if arm.last_joint_command is not None and arm.last_command_monotonic is not None:
            dt = now - arm.last_command_monotonic
            arm.debug_dt_s = float(dt)
            target_joints = apply_joint_rate_limit(
                prev_joint_target=arm.last_joint_command,
                new_joint_target=desired,
                dt_s=dt,
                joint_rate_limits=arm.mapping.joint_rate_limits,
            )
            arm.debug_step_clipped = not np.allclose(desired, target_joints, atol=1e-9, equal_nan=False)

    # Track raw Mate J3 delta (before sign/scale) vs commanded YAM J3 after mapping.
    arm.debug_j3_leader_delta_deg = float(
        np.rad2deg(leader_joint_rad[2] - arm.leader_base_rad[2])
    )
    arm.debug_j3_target_rad = float(target_joints[2])

    if args.gripper_enabled and packet_arm.gripper_enabled:
        target_gripper = compute_gripper_target_abs(leader_gripper_m, arm.mapping)
    else:
        current = np.asarray(arm.robot.get_joint_pos(), dtype=np.float64)
        target_gripper = float(current[6]) if current.shape[0] > 6 else 0.0

    return pack_command_vector_7(target_joints, target_gripper)


def execute_arm_command(
    arm: ArmRuntime,
    command: np.ndarray,
    *,
    arms: dict[str, ArmRuntime] | None = None,
    auto_resume: bool = True,
) -> None:
    if arm.state not in (STATE_NORMAL, STATE_ALIGNING):
        return
    if not arm_chain_alive(arm.robot):
        mark_motor_chain_dead(arm, arms=arms, auto_resume=auto_resume)
        return
    try:
        record_hang_sample(arm, command)
        arm.robot.command_joint_pos(command)
        if not arm_chain_alive(arm.robot):
            mark_motor_chain_dead(
                arm,
                "died during command_joint_pos",
                arms=arms,
                auto_resume=auto_resume,
            )
            return
        arm.executed_count += 1
        arm.packets_since_log += 1
        arm.last_joint_command = command[:6].copy()
        arm.last_command_monotonic = time.monotonic()
    except Exception as exc:
        detail = str(exc)
        arm.unavailable_count += 1
        if "Motor error" in detail or "motor id:" in detail.lower():
            mark_motor_chain_dead(
                arm,
                detail,
                arms=arms,
                auto_resume=auto_resume,
            )
            return
        arm.set_state(STATE_UNAVAILABLE, f"execute failed: {exc}")
        print(f"\n[{arm.name}] execute failed: {exc}", flush=True)
        print(format_motor_fault_report(arm, detail), flush=True)
        dump_hang_telemetry(arm, detail)


def handle_all_stop(
    arms: dict[str, ArmRuntime],
    now: float,
    reason: str,
    *,
    auto_resume: bool | None = None,
) -> None:
    for arm in arms.values():
        if not arm.enabled:
            continue
        arm.last_valid_monotonic = now
        enter_safe_state(arm, reason, auto_resume=auto_resume)


def check_watchdogs(
    arms: dict[str, ArmRuntime],
    timeout_ms: float | None = None,
    *,
    stale_hold_ms: float = 150.0,
    hard_timeout_ms: float | None = None,
    auto_resume: bool = False,
    align_timeout_s: float = 15.0,
) -> None:
    if hard_timeout_ms is None:
        hard_timeout_ms = float(timeout_ms) if timeout_ms is not None else 600.0
    now = time.monotonic()
    for arm in arms.values():
        if not arm.enabled:
            continue
        if arm.state == STATE_ALIGNING:
            if arm.align_started_monotonic is not None:
                if (now - arm.align_started_monotonic) > align_timeout_s:
                    enter_safe_state(arm, "align timeout", auto_resume=auto_resume)
                    continue
        if arm.state in (STATE_SAFE, STATE_UNAVAILABLE):
            continue
        if arm.last_valid_monotonic is None:
            continue
        age_ms = (now - arm.last_valid_monotonic) * 1000.0
        if arm.state in COMM_WATCHDOG_STATES and age_ms > hard_timeout_ms:
            arm.timeout_count += 1
            enter_safe_state(
                arm,
                WATCHDOG_HARD_REASON,
                auto_resume=auto_resume,
                recovery_allowed=True,
            )
            continue
        if arm.state in (STATE_NORMAL, STATE_ALIGNING) and age_ms > stale_hold_ms:
            enter_stale_hold(arm)


def receive_one_command(
    sock: socket.socket,
    arms: dict[str, ArmRuntime],
) -> tuple[DualCommandV3 | None, tuple[str, int] | None]:
    """Receive at most one UDP command per call (FIFO; do not drain/skip).

    YAM dual-arm CAN apply is slower than Piper; draining to the latest packet
    drops intermediate trajectory samples and feels stuttery with rate limits.
    """
    try:
        payload, client_addr = sock.recvfrom(4096)
    except (socket.timeout, BlockingIOError):
        return None, None

    try:
        cmd = decode_command(payload)
    except ProtocolError as exc:
        for arm in arms.values():
            arm.invalid_count += 1
            arm.message = f"invalid packet: {exc}"
        return None, None

    return cmd, client_addr


def arm_motion_fingerprint(arm: ArmRuntime) -> tuple:
    """Fields the no-motion probe contract must leave unchanged."""
    target = None if arm.last_joint_command is None else tuple(np.asarray(arm.last_joint_command).tolist())
    return (arm.state, arm.enabled, arm.seq_last, target)


def handle_no_motion_probe(
    arms: dict[str, ArmRuntime],
    sock: socket.socket,
    client_addr: tuple[str, int],
) -> None:
    """Reply PMRS without dispatching motion. Raises if fingerprint changes."""
    before = {name: arm_motion_fingerprint(arm) for name, arm in arms.items()}
    status = build_status(arms)
    sock.sendto(encode_status(status), client_addr)
    after = {name: arm_motion_fingerprint(arm) for name, arm in arms.items()}
    if before != after:
        raise RuntimeError(f"no-motion probe mutated arm state: {before} -> {after}")


def dispatch_command(
    cmd: DualCommandV3,
    arms: dict[str, ArmRuntime],
    now: float,
    args: argparse.Namespace,
) -> None:
    if cmd.all_stop:
        handle_all_stop(arms, now, "all-stop requested", auto_resume=bool(args.auto_resume))
        return

    for arm in arms.values():
        arm.touched_this_cycle = False

    for arm_name, arm_id, block in (
        (ARM_NAME_LEFT, ARM_ID_LEFT, cmd.left),
        (ARM_NAME_RIGHT, ARM_ID_RIGHT, cmd.right),
    ):
        if not block.valid:
            continue
        arm = arms[arm_name]
        arm.last_packet_timestamp_ns = int(cmd.timestamp_ns)
        packet_arm = arm_block_to_packet(arm_id, block, int(cmd.timestamp_ns))
        command = compute_command_for_arm(arm, packet_arm, now, args, arms=arms)
        if command is not None:
            execute_arm_command(
                arm,
                command,
                arms=arms,
                auto_resume=bool(args.auto_resume),
            )

    # Scheme A: refresh watchdog after slow execute so a single long CAN call
    # does not falsely trip timeout_ms.
    post_mono = time.monotonic()
    for arm in arms.values():
        if arm.touched_this_cycle and arm.enabled:
            arm.last_valid_monotonic = post_mono


def build_status(arms: dict[str, ArmRuntime]) -> DualStatusPacket:
    status_arms: dict[str, ArmStatus] = {}
    for arm_name in ARM_NAMES:
        arm = arms[arm_name]
        msg = arm.message or arm.disabled_message
        status_arms[arm_name] = ArmStatus(
            enabled=arm.enabled,
            seq=max(arm.seq_last, 0),
            executed_count=arm.executed_count,
            dropped_old=arm.dropped_old,
            dropped_stale=arm.dropped_stale,
            invalid_count=arm.invalid_count,
            unavailable_count=arm.unavailable_count,
            timeout_count=arm.timeout_count,
            safe_state=(arm.state == STATE_SAFE),
            last_packet_age_ms=None,
            message=msg,
        )
    return DualStatusPacket(arms=status_arms)


def print_binding(arms: dict[str, ArmRuntime], args: argparse.Namespace) -> None:
    print("YAM dual server binding:")
    print(f"  config={args.config or '(defaults/env only)'}")
    for name in ARM_NAMES:
        arm = arms[name]
        print(f"  {name}: can={arm.can_name} enabled={arm.enabled} state={arm.state}")
    print(
        f"  mapping_mode={args.mapping_mode} "
        f"align_tol={args.align_tol_rad} align_timeout={args.align_timeout_s}s"
    )
    print(
        f"  effective_joint_signs: left={args.left_joint_signs} "
        f"right={args.right_joint_signs}"
    )
    print(
        f"  joint_offsets: left={args.left_joint_offsets} "
        f"right={args.right_joint_offsets}"
    )
    print(
        f"  track_rate_limit_rad_s: left={args.left_joint_rate_limit_rad_s} "
        f"right={args.right_joint_rate_limit_rad_s}"
    )
    if str(args.mapping_mode).lower() == "absolute":
        print(
            "  WARN: absolute NORMAL applies track rate limits; large Mate Motions "
            "are chased gradually. signs 只表方向；零位差用 offsets。"
            " 大行程+硬负载仍可能触发电机保护(常报 disabled)。",
            flush=True,
        )
    env_overrides = [
        "YAM_REMOTE_BIND_IP",
        "YAM_REMOTE_PORT",
        "YAM_REMOTE_LEFT_CAN",
        "YAM_REMOTE_RIGHT_CAN",
        "YAM_REMOTE_TIMEOUT_MS",
        "YAM_REMOTE_STATUS_RATE",
        "YAM_REMOTE_ALLOW_SINGLE_ARM",
        "YAM_REMOTE_DRY_RUN",
        "YAM_REMOTE_RUN_PREINIT",
    ]
    active_overrides = [f"{k}={os.environ[k]}" for k in env_overrides if k in os.environ]
    if active_overrides:
        print(f"  env_overrides: {'; '.join(active_overrides)}")
    else:
        print("  env_overrides: (none)")
    if "YAM_REMOTE_AUTO_RESUME" in os.environ:
        print(
            "  ignored_env_override: YAM_REMOTE_AUTO_RESUME "
            "(use explicit --auto-resume for diagnostics only)"
        )
    print(
        f"  stale_hold={args.stale_hold_ms:.0f}ms hard_timeout={args.hard_timeout_ms:.0f}ms "
        f"status_rate={args.status_rate:.1f}Hz "
        f"dry_run={args.dry_run} auto_resume={args.auto_resume} "
        f"allow_single_arm={args.allow_single_arm}"
    )
    print(
        f"  hang_telemetry_dir={args.debug_run_dir} "
        f"hang_ring_size={args.hang_ring_size}"
    )


def log_status_periodic(arms: dict[str, ArmRuntime], elapsed: float) -> None:
    """Emit one durable diagnostic line per arm each reporting interval."""
    for name in ARM_NAMES:
        arm = arms[name]
        recv_rate = arm.recv_since_log / max(elapsed, 1e-9)
        exec_rate = arm.packets_since_log / max(elapsed, 1e-9)
        print(
            f"[YAM_STATUS] arm={name} can={arm.can_name} seq={arm.seq_last} "
            f"state={arm.state} recv_hz={recv_rate:.1f} exec_hz={exec_rate:.1f} "
            f"executed_count={arm.executed_count} dropped_old={arm.dropped_old} "
            f"dropped_stale={arm.dropped_stale} invalid={arm.invalid_count} "
            f"timeout={arm.timeout_count} safe={arm.safe_state} "
            f"unavailable={arm.unavailable_count} message={arm.message or '-'}",
            flush=True,
        )


def run() -> None:
    install_motor_error_log_capture()
    args = parse_args()
    run_preinit_if_requested(args)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind((args.bind_ip, args.port))
    sock.settimeout(0.01)
    try:
        arms = setup_arms(args)
    except BaseException:
        sock.close()
        raise
    print_binding(arms, args)
    import signal
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)

    last_status_time = time.monotonic()
    last_log_time = last_status_time

    try:
        while True:
            check_watchdogs(
                arms,
                stale_hold_ms=float(args.stale_hold_ms),
                hard_timeout_ms=float(args.hard_timeout_ms),
                auto_resume=bool(args.auto_resume),
                align_timeout_s=float(args.align_timeout_s),
            )
            now = time.monotonic()
            cmd, client_addr = receive_one_command(sock, arms)

            if cmd is not None and client_addr is not None:
                if bool(getattr(args, "no_motion_probe", False)) and is_no_motion_probe(cmd):
                    handle_no_motion_probe(arms, sock, client_addr)
                else:
                    dispatch_command(cmd, arms, now, args)
                    status = build_status(arms)
                    sock.sendto(encode_status(status), client_addr)

            now = time.monotonic()
            if now - last_status_time >= 1.0 / args.status_rate:
                elapsed = now - last_log_time
                log_status_periodic(arms, elapsed)
                for arm in arms.values():
                    arm.packets_since_log = 0
                    arm.recv_since_log = 0
                last_status_time = now
                last_log_time = now

    except KeyboardInterrupt:
        print("\nStopping YAM dual server...")
    finally:
        handle_all_stop(
            arms,
            time.monotonic(),
            "shutdown",
            auto_resume=bool(args.auto_resume),
        )
        from yam_deploy.robot import close_many
        try:
            close_many([a.robot for a in arms.values()])
        finally:
            sock.close()


if __name__ == "__main__":
    run()


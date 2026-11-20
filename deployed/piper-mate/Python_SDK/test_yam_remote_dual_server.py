#!/usr/bin/env python3
"""Unit tests for yam_remote_dual_server (dry_run / MockRobot, no CAN)."""

from __future__ import annotations

import argparse
import os
import socket
import time
import unittest
from unittest import mock

import numpy as np

from remote_dual_protocol import (
    ARM_ID_LEFT,
    ARM_NAME_LEFT,
    ARM_NAME_RIGHT,
    ArmBlockV3,
    DualCommandV3,
    encode_command,
)
from yam_mapping import ArmMappingConfig, compute_joint_target_absolute
from yam_remote_dual_server import (
    DEFAULT_JOINT_LIMITS,
    MSG_MOTOR_CHAIN_DEAD,
    MSG_NOT_APPLYING,
    SEQ_REORDER_TOL,
    SOFT_RECOVERY_PACKETS,
    STATE_ALIGNING,
    STATE_NORMAL,
    STATE_SAFE,
    STATE_STALE_HOLD,
    STATE_UNAVAILABLE,
    STATE_WAITING_BASELINE,
    WATCHDOG_HARD_REASON,
    ArmRuntime,
    build_defaults_from_layers,
    build_parser,
    MockRobot,
    check_watchdogs,
    classify_seq,
    compute_command_for_arm,
    dispatch_command,
    dump_hang_telemetry,
    enter_safe_state,
    enter_stale_hold,
    execute_arm_command,
    format_motor_fault_report,
    handle_all_stop,
    mark_motor_chain_dead,
    maybe_resync_client_seq,
    parse_motor_fault_from_text,
    record_hang_sample,
    receive_one_command,
    resolve_fault_detail,
    _LAST_MOTOR_ERROR_LINES,
)


def _mapping(*, gripper_max: float = 0.75) -> ArmMappingConfig:
    return ArmMappingConfig(
        joint_signs=np.ones(6, dtype=np.float64),
        joint_scales=np.ones(6, dtype=np.float64),
        joint_limits=DEFAULT_JOINT_LIMITS.copy(),
        joint_rate_limits=np.full(6, 20.0, dtype=np.float64),
        gripper_scale=12.5,
        gripper_offset=0.0,
        gripper_limits=(0.0, gripper_max),
        joint_offsets=np.zeros(6, dtype=np.float64),
        align_rate_limits=np.full(6, 50.0, dtype=np.float64),  # fast park in tests
    )


def make_args(**overrides: object) -> argparse.Namespace:
    defaults: dict[str, object] = {
        "auto_resume": True,
        "gripper_enabled": True,
        "baseline_samples": 3,
        "baseline_max_vel_deg_s": 5.0,
        "timeout_ms": 600.0,
        "stale_hold_ms": 150.0,
        "hard_timeout_ms": 600.0,
        "mapping_mode": "absolute",
        "align_tol_rad": 0.03,
        "align_timeout_s": 15.0,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def make_arm(name: str = ARM_NAME_LEFT) -> ArmRuntime:
    robot = MockRobot(DEFAULT_JOINT_LIMITS)
    return ArmRuntime(name=name, can_name="can_test", mapping=_mapping(), robot=robot)


def _block(
    seq: int,
    joints: tuple[int, int, int, int, int, int] = (0, 0, 0, 0, 0, 0),
    *,
    gripper_um: int = 0,
    stop: bool = False,
    valid: bool = True,
) -> ArmBlockV3:
    return ArmBlockV3(
        seq=seq,
        joint_targets=joints,
        gripper_target=gripper_um,
        gripper_enabled=True,
        stop=stop,
        valid=valid,
    )


def _packet_from_block(arm_id: int, block: ArmBlockV3, ts: int = 1):
    from remote_dual_protocol import arm_block_to_packet

    return arm_block_to_packet(arm_id, block, ts)


def _lock_baseline(arm: ArmRuntime, args: argparse.Namespace, seq: int = 1) -> None:
    """Feed still samples to leave waitingBaseline; park until NORMAL if absolute."""
    now = time.monotonic()
    for i in range(args.baseline_samples):
        pkt = _packet_from_block(ARM_ID_LEFT, _block(seq + i), ts=i + 1)
        cmd = compute_command_for_arm(arm, pkt, now + 0.05 * i, args)
        if cmd is not None:
            execute_arm_command(arm, cmd)
    # Absolute mode may still be aligning if start pose != abs(leader).
    for j in range(80):
        if arm.state == STATE_NORMAL:
            break
        pkt = _packet_from_block(ARM_ID_LEFT, _block(seq + 100 + j), ts=100 + j)
        cmd = compute_command_for_arm(arm, pkt, now + 1.0 + 0.02 * j, args)
        if cmd is not None:
            execute_arm_command(arm, cmd)
    assert arm.state == STATE_NORMAL, arm.message


class YamRemoteDualServerTests(unittest.TestCase):
    def test_auto_resume_environment_override_is_ignored(self) -> None:
        with mock.patch.dict(os.environ, {"YAM_REMOTE_AUTO_RESUME": "true"}):
            defaults = build_defaults_from_layers({"auto_resume": False})
        self.assertFalse(defaults["auto_resume"])
        self.assertTrue(build_parser(defaults).parse_args(["--auto-resume"]).auto_resume)

    def test_gripper_max_clamps_full_open(self) -> None:
        from yam_mapping import compute_gripper_target_abs, packet_gripper_um_to_m

        mapping = _mapping(gripper_max=0.75)
        cmd = compute_gripper_target_abs(packet_gripper_um_to_m(80_000), mapping)
        self.assertAlmostEqual(cmd, 0.75)

    def test_auto_resume_rebaseline_after_safe(self) -> None:
        args = make_args(auto_resume=True)
        arm = make_arm()
        _lock_baseline(arm, args, seq=1)
        self.assertEqual(arm.state, STATE_NORMAL)
        exec_before = arm.executed_count

        enter_safe_state(arm, "watchdog timeout", auto_resume=True)
        self.assertEqual(arm.state, STATE_SAFE)

        now = time.monotonic()
        pkt = _packet_from_block(ARM_ID_LEFT, _block(10), ts=10)
        cmd = compute_command_for_arm(arm, pkt, now, args)
        self.assertIsNone(cmd)
        self.assertEqual(arm.state, STATE_WAITING_BASELINE)
        self.assertIn("auto_resume", arm.message)

        for i in range(1, args.baseline_samples):
            pkt = _packet_from_block(ARM_ID_LEFT, _block(10 + i), ts=10 + i)
            cmd = compute_command_for_arm(arm, pkt, now + 0.05 * i, args)
            if cmd is not None:
                execute_arm_command(arm, cmd)
        for j in range(40):
            if arm.state == STATE_NORMAL:
                break
            pkt = _packet_from_block(ARM_ID_LEFT, _block(20 + j), ts=20 + j)
            cmd = compute_command_for_arm(arm, pkt, now + 1.0 + 0.02 * j, args)
            if cmd is not None:
                execute_arm_command(arm, cmd)
        self.assertEqual(arm.state, STATE_NORMAL)
        self.assertGreaterEqual(arm.executed_count, exec_before)

    def test_no_auto_resume_stays_safe_and_messages(self) -> None:
        args = make_args(auto_resume=False)
        arm = make_arm()
        _lock_baseline(arm, args, seq=1)
        enter_safe_state(arm, "all-stop requested", auto_resume=False)

        pkt = _packet_from_block(ARM_ID_LEFT, _block(20), ts=20)
        cmd = compute_command_for_arm(arm, pkt, time.monotonic(), args)
        self.assertIsNone(cmd)
        self.assertEqual(arm.state, STATE_SAFE)
        self.assertEqual(arm.message, MSG_NOT_APPLYING)
        self.assertFalse(arm.watchdog_recovery_allowed)

    def test_safe_state_releases_hold_torque(self) -> None:
        arm = make_arm()
        arm.last_joint_command = np.ones(6, dtype=np.float64)
        arm.last_command_monotonic = time.monotonic()
        enter_safe_state(arm, "all-stop requested", auto_resume=False)
        self.assertIsNone(arm.last_joint_command)
        self.assertIsNone(arm.last_command_monotonic)
        self.assertTrue(getattr(arm.robot, "_zero_torque", False))
        pkt = _packet_from_block(ARM_ID_LEFT, _block(20), ts=20)
        cmd = compute_command_for_arm(arm, pkt, time.monotonic(), make_args(auto_resume=False))
        self.assertIsNone(cmd)
        self.assertEqual(arm.state, STATE_SAFE)

    def test_seq_resync_on_client_restart(self) -> None:
        args = make_args(auto_resume=True)
        arm = make_arm()
        _lock_baseline(arm, args, seq=1)
        arm.seq_last = 1000
        arm.state = STATE_NORMAL

        self.assertTrue(maybe_resync_client_seq(arm, 1))
        self.assertEqual(arm.seq_last, -1)

        pkt = _packet_from_block(ARM_ID_LEFT, _block(1), ts=1)
        compute_command_for_arm(arm, pkt, time.monotonic(), args)
        self.assertNotEqual(arm.state, STATE_SAFE)
        self.assertEqual(arm.seq_last, 1)

    def test_classify_seq_small_backward_is_not_jump(self) -> None:
        self.assertEqual(classify_seq(12, 11), "backward")
        self.assertEqual(classify_seq(50, 11), "backward")
        self.assertEqual(classify_seq(10, 10), "duplicate")
        self.assertEqual(classify_seq(10, 11), "forward")
        self.assertEqual(classify_seq(300, 11), "jump")
        self.assertEqual(classify_seq(70050, 70051), "forward")
        self.assertEqual(classify_seq(70050, 70011), "backward")

    def test_seq_out_of_order_drop_stays_normal(self) -> None:
        args = make_args(auto_resume=False)
        arm = make_arm()
        _lock_baseline(arm, args, seq=1)
        self.assertEqual(arm.state, STATE_NORMAL)
        arm.seq_last = 12
        exec_before = arm.executed_count
        dropped_before = arm.dropped_old

        pkt = _packet_from_block(ARM_ID_LEFT, _block(11), ts=11)
        cmd = compute_command_for_arm(arm, pkt, time.monotonic(), args)
        self.assertIsNone(cmd)
        self.assertEqual(arm.state, STATE_NORMAL)
        self.assertEqual(arm.seq_last, 12)
        self.assertEqual(arm.dropped_old, dropped_before + 1)
        self.assertEqual(arm.executed_count, exec_before)

        pkt = _packet_from_block(ARM_ID_LEFT, _block(13), ts=13)
        cmd = compute_command_for_arm(arm, pkt, time.monotonic(), args)
        self.assertIsNotNone(cmd)
        execute_arm_command(arm, cmd)
        self.assertEqual(arm.state, STATE_NORMAL)
        self.assertEqual(arm.seq_last, 13)
        self.assertGreater(arm.executed_count, exec_before)

    def test_seq_jump_still_enters_safe(self) -> None:
        args = make_args(auto_resume=False)
        arm = make_arm()
        arm.seq_last = 300
        arm.state = STATE_NORMAL
        self.assertGreater(300 - 11, SEQ_REORDER_TOL)
        pkt = _packet_from_block(ARM_ID_LEFT, _block(11), ts=1)
        compute_command_for_arm(arm, pkt, time.monotonic(), args)
        self.assertEqual(arm.state, STATE_SAFE)
        self.assertIn("jump", arm.message)
        self.assertEqual(arm.seq_last, 300)

    def test_seq_above_u16_small_backward_is_not_jump(self) -> None:
        args = make_args(auto_resume=False)
        arm = make_arm()
        _lock_baseline(arm, args, seq=1)
        arm.seq_last = 70050
        arm.state = STATE_NORMAL
        pkt = _packet_from_block(ARM_ID_LEFT, _block(70011), ts=70011)
        cmd = compute_command_for_arm(arm, pkt, time.monotonic(), args)
        self.assertIsNone(cmd)
        self.assertEqual(arm.state, STATE_NORMAL)
        self.assertEqual(arm.seq_last, 70050)

        pkt = _packet_from_block(ARM_ID_LEFT, _block(70051), ts=70051)
        cmd = compute_command_for_arm(arm, pkt, time.monotonic(), args)
        self.assertIsNotNone(cmd)
        self.assertEqual(arm.seq_last, 70051)

    def test_receive_one_command_is_fifo_not_drain(self) -> None:
        class FakeSocket:
            def __init__(self, responses: list[object]) -> None:
                self.responses = responses

            def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
                item = self.responses.pop(0)
                if isinstance(item, Exception):
                    raise item
                payload, addr = item
                return payload, addr

        cmd1 = DualCommandV3(timestamp_ns=1, left=_block(1), right=_block(1))
        cmd2 = DualCommandV3(timestamp_ns=2, left=_block(9), right=_block(9))
        fake = FakeSocket(
            [
                (encode_command(cmd1), ("127.0.0.1", 1111)),
                (encode_command(cmd2), ("127.0.0.1", 2222)),
            ]
        )
        arms = {ARM_NAME_LEFT: make_arm(ARM_NAME_LEFT), ARM_NAME_RIGHT: make_arm(ARM_NAME_RIGHT)}
        first, addr1 = receive_one_command(fake, arms)  # type: ignore[arg-type]
        self.assertIsNotNone(first)
        assert first is not None
        self.assertEqual(first.left.seq, 1)
        self.assertEqual(addr1, ("127.0.0.1", 1111))
        second, addr2 = receive_one_command(fake, arms)  # type: ignore[arg-type]
        self.assertIsNotNone(second)
        assert second is not None
        self.assertEqual(second.left.seq, 9)
        self.assertEqual(addr2, ("127.0.0.1", 2222))

    def test_receive_one_command_timeout_returns_none(self) -> None:
        class FakeSocket:
            def recvfrom(self, _size: int) -> tuple[bytes, tuple[str, int]]:
                raise socket.timeout()

        arms = {ARM_NAME_LEFT: make_arm(ARM_NAME_LEFT)}
        cmd, addr = receive_one_command(FakeSocket(), arms)  # type: ignore[arg-type]
        self.assertIsNone(cmd)
        self.assertIsNone(addr)

    def test_watchdog_ignores_slow_execute(self) -> None:
        from remote_dual_protocol import ARM_ID_RIGHT

        args = make_args(auto_resume=True, timeout_ms=50.0)
        left = make_arm(ARM_NAME_LEFT)
        right = make_arm(ARM_NAME_RIGHT)
        arms = {ARM_NAME_LEFT: left, ARM_NAME_RIGHT: right}
        _lock_baseline(left, args, seq=1)
        now = time.monotonic()
        for i in range(args.baseline_samples):
            pkt = _packet_from_block(ARM_ID_RIGHT, _block(1 + i), ts=i + 1)
            cmd = compute_command_for_arm(right, pkt, now + 0.05 * i, args)
            if cmd is not None:
                execute_arm_command(right, cmd)
        for j in range(40):
            if right.state == STATE_NORMAL:
                break
            pkt = _packet_from_block(ARM_ID_RIGHT, _block(50 + j), ts=50 + j)
            cmd = compute_command_for_arm(right, pkt, now + 1.0 + 0.02 * j, args)
            if cmd is not None:
                execute_arm_command(right, cmd)
        self.assertEqual(right.state, STATE_NORMAL)

        slow = mock.Mock(side_effect=lambda _cmd: time.sleep(0.08))
        left.robot.command_joint_pos = slow  # type: ignore[method-assign]
        right.robot.command_joint_pos = mock.Mock()  # type: ignore[method-assign]

        cmd = DualCommandV3(
            timestamp_ns=100,
            left=_block(10, (1000, 0, 0, 0, 0, 0)),
            right=_block(10, (1000, 0, 0, 0, 0, 0)),
        )
        dispatch_command(cmd, arms, time.monotonic(), args)
        check_watchdogs(arms, args.timeout_ms, auto_resume=True)
        self.assertEqual(left.state, STATE_NORMAL)
        self.assertEqual(right.state, STATE_NORMAL)

    def test_dispatch_applies_both_arms(self) -> None:
        args = make_args(auto_resume=True)
        left = make_arm(ARM_NAME_LEFT)
        right = make_arm(ARM_NAME_RIGHT)
        arms = {ARM_NAME_LEFT: left, ARM_NAME_RIGHT: right}
        from remote_dual_protocol import ARM_ID_RIGHT

        _lock_baseline(left, args, seq=1)
        now = time.monotonic()
        for i in range(args.baseline_samples):
            compute_command_for_arm(
                right, _packet_from_block(ARM_ID_RIGHT, _block(i + 1)), now + 0.05 * i, args
            )
        for j in range(40):
            if right.state == STATE_NORMAL:
                break
            pkt = _packet_from_block(ARM_ID_RIGHT, _block(40 + j), ts=40 + j)
            cmd = compute_command_for_arm(right, pkt, now + 1.0 + 0.02 * j, args)
            if cmd is not None:
                execute_arm_command(right, cmd)
        self.assertEqual(left.state, STATE_NORMAL)
        self.assertEqual(right.state, STATE_NORMAL)

        left.executed_count = 0
        right.executed_count = 0
        dispatch_command(
            DualCommandV3(
                timestamp_ns=99,
                left=_block(10, (500, 0, 0, 0, 0, 0)),
                right=_block(10, (600, 0, 0, 0, 0, 0)),
            ),
            arms,
            time.monotonic(),
            args,
        )
        self.assertEqual(left.executed_count, 1)
        self.assertEqual(right.executed_count, 1)

    def test_motor_chain_dead_marks_unavailable_and_peer_safe(self) -> None:
        args = make_args(auto_resume=True)
        left = make_arm(ARM_NAME_LEFT)
        right = make_arm(ARM_NAME_RIGHT)
        arms = {ARM_NAME_LEFT: left, ARM_NAME_RIGHT: right}
        _lock_baseline(left, args, seq=1)
        _lock_baseline(right, args, seq=1)

        class _DeadChain:
            running = False

        left.robot.motor_chain = _DeadChain()  # type: ignore[attr-defined]
        pkt = _packet_from_block(ARM_ID_LEFT, _block(20, (1000, 0, 0, 0, 0, 0)), ts=20)
        cmd = compute_command_for_arm(left, pkt, time.monotonic(), args, arms=arms)
        self.assertIsNone(cmd)
        self.assertEqual(left.state, STATE_UNAVAILABLE)
        self.assertIn("MOTOR CHAIN DEAD", left.message)
        self.assertFalse(left.enabled)
        self.assertEqual(right.state, STATE_SAFE)
        self.assertIn("peer MOTOR CHAIN DEAD", right.message)

    def test_absolute_lock_enters_aligning_when_sagged(self) -> None:
        args = make_args(auto_resume=True, align_tol_rad=0.03)
        arm = make_arm()
        arm.mapping = ArmMappingConfig(
            joint_signs=np.ones(6),
            joint_scales=np.ones(6),
            joint_limits=DEFAULT_JOINT_LIMITS.copy(),
            joint_rate_limits=np.full(6, 20.0),
            gripper_scale=12.5,
            gripper_offset=0.0,
            gripper_limits=(0.0, 0.75),
            align_rate_limits=np.full(6, 0.5),  # slow so we observe ALIGNING
        )
        arm.robot._state[:6] = 1.2
        now = time.monotonic()
        for i in range(args.baseline_samples):
            pkt = _packet_from_block(ARM_ID_LEFT, _block(1 + i), ts=i + 1)
            compute_command_for_arm(arm, pkt, now + 0.05 * i, args)
        self.assertEqual(arm.state, STATE_ALIGNING)
        np.testing.assert_allclose(arm.yam_base_rad, 0.0, atol=1e-9)
        np.testing.assert_allclose(arm.align_target_rad, 0.0, atol=1e-9)

    def test_absolute_free_track_after_aligned(self) -> None:
        args = make_args(auto_resume=True)
        arm = make_arm()
        _lock_baseline(arm, args, seq=1)
        self.assertEqual(arm.state, STATE_NORMAL)

        # Enough dt that track rate limit (20 rad/s in test mapping) does not clip 30deg.
        t0 = float(arm.last_command_monotonic or time.monotonic())
        moved = _packet_from_block(
            ARM_ID_LEFT,
            _block(10, (30_000, 0, 0, 0, 0, 0)),
            ts=10,
        )
        cmd_moved = compute_command_for_arm(arm, moved, t0 + 1.0, args)
        self.assertIsNotNone(cmd_moved)
        assert cmd_moved is not None
        expected_moved = compute_joint_target_absolute(
            np.deg2rad(np.array([30.0, 0, 0, 0, 0, 0])),
            arm.mapping,
        )
        np.testing.assert_allclose(cmd_moved[:6], expected_moved, atol=1e-6)
        execute_arm_command(arm, cmd_moved)
        arm.last_command_monotonic = t0 + 1.0

        home = _packet_from_block(ARM_ID_LEFT, _block(11, (0, 0, 0, 0, 0, 0)), ts=11)
        cmd_home = compute_command_for_arm(arm, home, t0 + 2.0, args)
        self.assertIsNotNone(cmd_home)
        assert cmd_home is not None
        expected_home = compute_joint_target_absolute(np.zeros(6), arm.mapping)
        np.testing.assert_allclose(cmd_home[:6], expected_home, atol=1e-6)

    def test_absolute_normal_applies_track_rate_limit(self) -> None:
        args = make_args(auto_resume=True)
        arm = make_arm()
        arm.mapping = ArmMappingConfig(
            joint_signs=np.ones(6, dtype=np.float64),
            joint_scales=np.ones(6, dtype=np.float64),
            joint_limits=DEFAULT_JOINT_LIMITS.copy(),
            joint_rate_limits=np.full(6, 1.0, dtype=np.float64),
            gripper_scale=12.5,
            gripper_offset=0.0,
            gripper_limits=(0.0, 0.75),
            joint_offsets=np.zeros(6, dtype=np.float64),
            align_rate_limits=np.full(6, 50.0, dtype=np.float64),
        )
        _lock_baseline(arm, args, seq=1)
        self.assertEqual(arm.state, STATE_NORMAL)
        t0 = float(arm.last_command_monotonic or time.monotonic())
        arm.last_command_monotonic = t0
        # 90deg J3 in 0.1s would need 15.7 rad/s; limit is 1 rad/s → max step 0.1 rad.
        moved = _packet_from_block(
            ARM_ID_LEFT,
            _block(20, (0, 0, 90_000, 0, 0, 0)),
            ts=20,
        )
        cmd = compute_command_for_arm(arm, moved, t0 + 0.1, args)
        self.assertIsNotNone(cmd)
        assert cmd is not None
        full = compute_joint_target_absolute(
            np.deg2rad(np.array([0.0, 0.0, 90.0, 0.0, 0.0, 0.0])),
            arm.mapping,
        )
        prev = float(arm.last_joint_command[2])
        self.assertLessEqual(abs(cmd[2] - prev), 0.1 + 1e-6)
        self.assertLess(abs(cmd[2]), abs(full[2]) - 0.5)

    def test_parse_motor_fault_and_report_hints(self) -> None:
        mid, err = parse_motor_fault_from_text(
            "Motor error detected: motor id: 4, error: disabled"
        )
        self.assertEqual(mid, 4)
        self.assertEqual(err, "disabled")
        mid2, err2 = parse_motor_fault_from_text(
            "motor id: 3, error: over current (reported temp_mos=33C)"
        )
        self.assertEqual(mid2, 3)
        self.assertEqual(err2, "over current")

        arm = make_arm(ARM_NAME_RIGHT)
        arm.debug_j3_leader_delta_deg = -150.0
        arm.debug_j3_target_rad = 2.6
        arm.last_joint_command = np.array([0.0, 0.0, 2.6, 0.5, 0.0, 0.0])
        report = format_motor_fault_report(
            arm, "Motor error detected: motor id: 4, error: disabled"
        )
        self.assertIn("motor_id=4", report)
        self.assertIn("J4", report)
        self.assertIn("disabled", report)
        self.assertIn("0x0", report)
        self.assertIn("j3_d=-150.0deg", report)
        report_oc = format_motor_fault_report(arm, "motor id: 3, error: over current")
        self.assertIn("过流", report_oc)
        self.assertIn("0xA", report_oc)

        mid3, err3 = parse_motor_fault_from_text(
            "1th motor at DMChainCanInterface failed with info [2, 'DM4340']"
        )
        self.assertEqual(mid3, 2)
        self.assertIsNone(err3)

    def test_hang_telemetry_dump_on_chain_dead(self) -> None:
        import tempfile
        from pathlib import Path

        arm = make_arm(ARM_NAME_RIGHT)
        with tempfile.TemporaryDirectory() as tmp:
            arm.debug_run_dir = Path(tmp)
            cmd = np.array([0.1, 0.2, 0.3, 0.0, 0.0, 0.0, 0.0], dtype=np.float64)
            arm.debug_dt_s = 0.016
            arm.debug_step_clipped = True
            arm.seq_last = 42
            record_hang_sample(arm, cmd)
            record_hang_sample(arm, cmd + 0.01)
            self.assertEqual(len(arm.hang_ring), 2)

            _LAST_MOTOR_ERROR_LINES.clear()
            _LAST_MOTOR_ERROR_LINES.append(
                "motor id: 2, error: disabled (reported temp_mos=35C)"
            )
            mark_motor_chain_dead(arm, detail="")  # empty detail → use captured log
            out = Path(tmp) / "hang_right.csv"
            self.assertTrue(out.is_file())
            text = out.read_text(encoding="utf-8")
            self.assertIn("err_j1", text)
            self.assertIn("eff_j2", text)
            self.assertIn("step_clipped", text)
            self.assertIn("MOTOR CHAIN DEAD", arm.message)
            # FAULT DETAIL should resolve motor id from captured log
            report = format_motor_fault_report(arm, "")
            self.assertIn("motor_id=2", report)

    def test_resolve_fault_detail_prefers_motor_id_line(self) -> None:
        _LAST_MOTOR_ERROR_LINES.clear()
        _LAST_MOTOR_ERROR_LINES.append("unrelated")
        _LAST_MOTOR_ERROR_LINES.append("motor id: 6, error: disabled")
        resolved = resolve_fault_detail("")
        self.assertIn("motor id: 6", resolved)

    def test_delta_mode_locks_to_current(self) -> None:
        args = make_args(mapping_mode="delta")
        arm = make_arm()
        arm.robot._state[:6] = 0.5
        now = time.monotonic()
        for i in range(args.baseline_samples):
            pkt = _packet_from_block(ARM_ID_LEFT, _block(1 + i), ts=i + 1)
            cmd = compute_command_for_arm(arm, pkt, now + 0.05 * i, args)
            if cmd is not None:
                execute_arm_command(arm, cmd)
        self.assertEqual(arm.state, STATE_NORMAL)
        np.testing.assert_allclose(arm.yam_base_rad, 0.5, atol=1e-9)
        self.assertIsNone(arm.align_target_rad)

    def test_legacy_timeout_ms_aliases_hard_timeout(self) -> None:
        defaults = build_defaults_from_layers({"timeout_ms": 300})
        self.assertEqual(defaults["hard_timeout_ms"], 300.0)
        defaults = build_defaults_from_layers({"stale_hold_ms": 150, "hard_timeout_ms": 600})
        self.assertEqual(defaults["stale_hold_ms"], 150)
        self.assertEqual(defaults["hard_timeout_ms"], 600)

    def test_stale_hold_and_hard_timeout_thresholds(self) -> None:
        args = make_args(auto_resume=False)
        arm = make_arm()
        _lock_baseline(arm, args, seq=1)
        held = arm.last_joint_command.copy()
        t0 = time.monotonic()
        arm.last_valid_monotonic = t0
        arms = {ARM_NAME_LEFT: arm}

        with mock.patch("yam_remote_dual_server.time.monotonic", return_value=t0 + 0.149):
            check_watchdogs(arms, stale_hold_ms=150.0, hard_timeout_ms=600.0)
        self.assertEqual(arm.state, STATE_NORMAL)
        self.assertFalse(getattr(arm.robot, "_zero_torque", False))

        with mock.patch("yam_remote_dual_server.time.monotonic", return_value=t0 + 0.151):
            check_watchdogs(arms, stale_hold_ms=150.0, hard_timeout_ms=600.0)
        self.assertEqual(arm.state, STATE_STALE_HOLD)
        np.testing.assert_allclose(arm.last_joint_command, held)
        self.assertFalse(getattr(arm.robot, "_zero_torque", False))

        with mock.patch("yam_remote_dual_server.time.monotonic", return_value=t0 + 0.599):
            check_watchdogs(arms, stale_hold_ms=150.0, hard_timeout_ms=600.0)
        self.assertEqual(arm.state, STATE_STALE_HOLD)
        np.testing.assert_allclose(arm.last_joint_command, held)

        with mock.patch("yam_remote_dual_server.time.monotonic", return_value=t0 + 0.601):
            check_watchdogs(arms, stale_hold_ms=150.0, hard_timeout_ms=600.0)
        self.assertEqual(arm.state, STATE_SAFE)
        self.assertEqual(arm.state_reason, WATCHDOG_HARD_REASON)
        self.assertTrue(arm.watchdog_recovery_allowed)
        self.assertTrue(getattr(arm.robot, "_zero_torque", False))
        self.assertIsNone(arm.last_joint_command)

    def test_stale_hold_soft_recovery_needs_three_forward_packets(self) -> None:
        args = make_args(auto_resume=False)
        arm = make_arm()
        _lock_baseline(arm, args, seq=1)
        enter_stale_hold(arm)
        exec_before = arm.executed_count
        now = time.monotonic()
        seq = arm.seq_last + 1
        moved = (30000, 0, 0, 0, 0, 0)

        cmd = compute_command_for_arm(
            arm, _packet_from_block(ARM_ID_LEFT, _block(seq, moved)), now, args
        )
        self.assertIsNone(cmd)
        self.assertEqual(arm.state, STATE_STALE_HOLD)
        self.assertEqual(arm.stale_recovery_count, 1)

        cmd = compute_command_for_arm(
            arm, _packet_from_block(ARM_ID_LEFT, _block(seq + 1, moved)), now + 0.02, args
        )
        self.assertIsNone(cmd)
        self.assertEqual(arm.state, STATE_STALE_HOLD)
        self.assertEqual(arm.executed_count, exec_before)

        cmd = compute_command_for_arm(
            arm, _packet_from_block(ARM_ID_LEFT, _block(seq + 2, moved)), now + 0.04, args
        )
        self.assertIsNotNone(cmd)
        self.assertEqual(arm.state, STATE_ALIGNING)
        self.assertIn("slew", arm.message)
        execute_arm_command(arm, cmd)
        self.assertGreater(arm.executed_count, exec_before)

    def test_stale_hold_duplicate_resets_confirm_count(self) -> None:
        args = make_args(auto_resume=False)
        arm = make_arm()
        _lock_baseline(arm, args, seq=1)
        enter_stale_hold(arm)
        now = time.monotonic()
        seq = arm.seq_last + 1
        compute_command_for_arm(arm, _packet_from_block(ARM_ID_LEFT, _block(seq)), now, args)
        compute_command_for_arm(arm, _packet_from_block(ARM_ID_LEFT, _block(seq)), now + 0.01, args)
        self.assertEqual(arm.state, STATE_STALE_HOLD)
        self.assertEqual(arm.stale_recovery_count, 0)
        compute_command_for_arm(
            arm, _packet_from_block(ARM_ID_LEFT, _block(seq + 1)), now + 0.02, args
        )
        self.assertEqual(arm.state, STATE_STALE_HOLD)
        self.assertEqual(arm.stale_recovery_count, 1)

    def test_hard_timeout_safe_uses_full_baseline_not_soft_slew(self) -> None:
        args = make_args(auto_resume=False)
        arm = make_arm()
        _lock_baseline(arm, args, seq=1)
        enter_safe_state(arm, WATCHDOG_HARD_REASON, recovery_allowed=True)
        exec_before = arm.executed_count
        now = time.monotonic()
        seq = max(arm.seq_last, 0) + 1
        for i in range(SOFT_RECOVERY_PACKETS):
            cmd = compute_command_for_arm(
                arm,
                _packet_from_block(ARM_ID_LEFT, _block(seq + i)),
                now + 0.05 * i,
                args,
            )
            if i == 0:
                self.assertEqual(arm.state, STATE_WAITING_BASELINE)
                self.assertIn("rebaseline", arm.message)
            if cmd is not None:
                execute_arm_command(arm, cmd)
        self.assertNotEqual(arm.state, STATE_STALE_HOLD)
        self.assertIn(arm.state, (STATE_WAITING_BASELINE, STATE_ALIGNING, STATE_NORMAL))
        if arm.state == STATE_WAITING_BASELINE:
            self.assertEqual(arm.executed_count, exec_before)

    def test_all_stop_stays_safe_without_auto_resume(self) -> None:
        args = make_args(auto_resume=False)
        left = make_arm(ARM_NAME_LEFT)
        right = make_arm(ARM_NAME_RIGHT)
        arms = {ARM_NAME_LEFT: left, ARM_NAME_RIGHT: right}
        _lock_baseline(left, args, seq=1)
        _lock_baseline(right, args, seq=1)
        handle_all_stop(arms, time.monotonic(), "all-stop requested", auto_resume=False)
        self.assertEqual(left.state, STATE_SAFE)
        self.assertFalse(left.watchdog_recovery_allowed)
        cmd = compute_command_for_arm(
            left, _packet_from_block(ARM_ID_LEFT, _block(left.seq_last + 1)), time.monotonic(), args
        )
        self.assertIsNone(cmd)
        self.assertEqual(left.state, STATE_SAFE)
        self.assertEqual(left.message, MSG_NOT_APPLYING)

    def test_align_timeout_stays_safe_without_auto_resume(self) -> None:
        args = make_args(auto_resume=False, align_timeout_s=15.0)
        arm = make_arm()
        _lock_baseline(arm, args, seq=1)
        arm.set_state(STATE_ALIGNING, "aligning")
        arm.align_started_monotonic = time.monotonic() - 16.0
        arm.last_valid_monotonic = time.monotonic()
        check_watchdogs({ARM_NAME_LEFT: arm}, stale_hold_ms=150.0, hard_timeout_ms=600.0, align_timeout_s=15.0)
        self.assertEqual(arm.state, STATE_SAFE)
        self.assertIn("align timeout", arm.message)
        self.assertFalse(arm.watchdog_recovery_allowed)
        cmd = compute_command_for_arm(
            arm, _packet_from_block(ARM_ID_LEFT, _block(arm.seq_last + 1)), time.monotonic(), args
        )
        self.assertIsNone(cmd)
        self.assertEqual(arm.state, STATE_SAFE)

    def test_waiting_baseline_keeps_hard_deadline(self) -> None:
        args = make_args(auto_resume=False)
        arm = make_arm()
        arm.set_state(STATE_WAITING_BASELINE, "startup")
        t0 = time.monotonic()
        arm.last_valid_monotonic = t0
        with mock.patch("yam_remote_dual_server.time.monotonic", return_value=t0 + 0.601):
            check_watchdogs({ARM_NAME_LEFT: arm}, stale_hold_ms=150.0, hard_timeout_ms=600.0)
        self.assertEqual(arm.state, STATE_SAFE)
        self.assertEqual(arm.state_reason, WATCHDOG_HARD_REASON)
        self.assertTrue(arm.watchdog_recovery_allowed)

    def test_per_arm_stop_stays_safe_without_auto_resume(self) -> None:
        args = make_args(auto_resume=False)
        arm = make_arm()
        _lock_baseline(arm, args, seq=1)
        cmd = compute_command_for_arm(
            arm,
            _packet_from_block(ARM_ID_LEFT, _block(arm.seq_last + 1, stop=True)),
            time.monotonic(),
            args,
        )
        self.assertIsNone(cmd)
        self.assertEqual(arm.state, STATE_SAFE)
        self.assertIn("stop requested", arm.message)
        self.assertFalse(arm.watchdog_recovery_allowed)
        cmd = compute_command_for_arm(
            arm, _packet_from_block(ARM_ID_LEFT, _block(arm.seq_last + 1)), time.monotonic(), args
        )
        self.assertIsNone(cmd)
        self.assertEqual(arm.state, STATE_SAFE)


if __name__ == "__main__":
    unittest.main()

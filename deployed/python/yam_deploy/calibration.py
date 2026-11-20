"""Frozen helpers from .196 colosseum_client/robots/yam.py. See manifest."""
import json
import logging
import threading
import numpy as np
log = logging.getLogger(__name__)
_CONTROL_JOIN_TIMEOUT = 5.0
_GRIPPER_STARTUP_TOLERANCE_RAD = .02

def _close_arm(arm):
    """Let I2RT stop its robot loop, then join CAN workers before socket close.

    The pinned I2RT DMChain discards its worker handle. Discover only workers
    bound to this chain; do not change the SDK globally or other arms' threads.
    """
    chain = getattr(arm, 'motor_chain', None)
    interface = getattr(chain, 'motor_interface', None)
    if interface is None:
        arm.close()
        return
    workers = [t for t in threading.enumerate()
               if getattr(getattr(t, '_target', None), '__self__', None) is chain]
    original_close = interface.close

    def close_after_workers():
        chain.running = False
        pending = []
        for worker in workers:
            if worker is not threading.current_thread():
                worker.join(timeout=_CONTROL_JOIN_TIMEOUT)
            if worker.is_alive():
                pending.append(worker.name)
        # Always attempt SDK motor/socket shutdown, even after a join timeout.
        # A timeout remains a reported failure, never a successful teardown.
        original_close()
        if pending:
            raise RuntimeError(f'I2RT CAN workers did not stop before shutdown: {pending}')

    interface.close = close_after_workers
    try:
        arm.close()
    finally:
        interface.close = original_close

def _vector(value, size, name):
    result = np.asarray(value, dtype=np.float64)
    if result.shape != (size,) or not np.isfinite(result).all():
        raise ValueError(f'{name} must contain {size} finite values')
    return result.copy()

def _rebase_gripper_limits(arm, limits):
    """Change only the gripper mapping while startup gripper control is passive.

    I2RT stores the limits in both JointMapper and the final target clipper.
    Update both and refresh normalized feedback atomically with its robot loop.
    Motor offsets, directions and arm control parameters remain unchanged.
    """
    from i2rt.robots.utils import JointMapper
    mapper = JointMapper(index_range_map={6: limits}, total_dofs=7)
    with arm._command_lock, arm._state_lock:
        if (arm._gripper_index != 6 or len(arm.motor_chain) != 7
                or any(getattr(arm._commands, name)[6] != 0
                       for name in ('kp', 'kd', 'torques'))):
            raise RuntimeError('Cannot rebase gripper calibration while gripper control is active')
        states = arm.motor_chain.read_states()
        if len(states) != 7 or states[6].id != 7:
            raise RuntimeError('Unexpected I2RT motor order during gripper calibration rebase')
        arm._gripper_limits = limits.copy()
        arm.remapper = mapper
        arm._joint_state = arm._motor_state_to_joint_state(states)
        arm._commands.pos[6] = states[6].pos
        arm._last_gripper_command_qpos = states[6].pos
        # Discard any startup limiter target computed using the old mapping.
        arm._gripper_force_limiter._gripper_adjusted_qpos = None
        arm._gripper_force_limiter._is_clogged = False

def _open_arm(channel, gripper_limits, *, calibration_offset=None):
    try:
        from i2rt.robots.get_robot import get_yam_robot
        from i2rt.robots.utils import GripperType
    except ImportError as exc:
        raise RuntimeError('Install I2RT in the Client environment; see docs/yam.md') from exc
    # Do not let the SDK immediately hold a gripper pose in the wrong calibrated
    # coordinate range. Gravity-compensation startup leaves gripper PD gains zero.
    arm = get_yam_robot(channel=channel, gripper_type=GripperType.LINEAR_4310,
                        gripper_limits_override=gripper_limits,
                        zero_gravity_mode=True)
    try:
        chain = arm.motor_chain
        calibrated = _vector(gripper_limits, 2, 'gripper_limits')
        limits = calibrated.copy()
        offsets = _vector(chain.motor_offset, 7, 'I2RT motor_offset')
        directions = _vector(chain.motor_direction, 7, 'I2RT motor_direction')
        if calibration_offset is not None:
            if directions[6] not in (-1, 1):
                raise RuntimeError('Gripper motor direction must be +1 or -1')
            # SDK q = (raw - offset) * direction. Preserve raw physical endpoints.
            shift = (calibration_offset - offsets[6]) * directions[6]
            # The motor's raw frame can itself come up one full turn away after
            # a power cycle, so the recorded offset alone is ambiguous by 2*pi.
            # Gripper travel is under one turn: at most one whole-turn shift can
            # contain the measured position. Anything else still fails below.
            position = float(next(s for s in chain.read_states() if s.id == 7).pos)
            span = [min(calibrated) - _GRIPPER_STARTUP_TOLERANCE_RAD,
                    max(calibrated) + _GRIPPER_STARTUP_TOLERANCE_RAD]
            if span[1] - span[0] < 2 * np.pi and not span[0] <= position - shift <= span[1]:
                turns = [k for k in (-2, -1, 1, 2)
                         if span[0] <= position - shift - 2 * np.pi * k <= span[1]]
                if len(turns) == 1:
                    log.warning('YAM %s gripper calibration shifted by %+d turn(s) to match startup position',
                                channel, turns[0])
                    shift += 2 * np.pi * turns[0]
            limits += shift
            if not np.isfinite(limits).all() or limits[0] == limits[1]:
                raise ValueError('Invalid rebased gripper limits')
            if shift != 0:
                _rebase_gripper_limits(arm, limits)
        state = next(s for s in chain.read_states() if s.id == 7)
        measured = float(state.pos)  # SDK direction/offset-adjusted radians.
        current = _vector(arm.get_joint_pos(), 7, 'I2RT startup joint position')
        print(json.dumps({
            'event': 'yam_gripper_startup', 'channel': channel, 'motor_id': 7,
            'calibrated_closed_rad': float(calibrated[0]),
            'calibrated_open_rad': float(calibrated[1]),
            'calibration_offset_rad': calibration_offset,
            'effective_closed_rad': float(limits[0]),
            'effective_open_rad': float(limits[1]),
            'measured_position_rad': measured,
            'measured_normalized': float(current[6]),
            'motor_offset_rad': float(offsets[6]),
            'motor_direction': float(directions[6]),
            'chain_running': bool(chain.running),
        }, allow_nan=False), flush=True)
        low, high = min(limits), max(limits)
        # Check both cached representations before granting position control.
        mapped = limits[0] + current[6] * (limits[1] - limits[0])
        if not chain.running or any(
            not np.isfinite(p) or p < low - _GRIPPER_STARTUP_TOLERANCE_RAD
            or p > high + _GRIPPER_STARTUP_TOLERANCE_RAD for p in (measured, mapped)
        ):
            raise RuntimeError(
                f'YAM {channel} gripper startup mismatch; position hold NOT enabled: '
                f'measured={measured:.9g}, calibrated=[{limits[0]:.9g}, {limits[1]:.9g}], '
                f'motor_offset={offsets[6]:.9g}, normalized={current[6]:.9g}. '
                'Check calibration and startup coordinates; do not widen limits to bypass this check.')
        current[6] = np.clip(current[6], 0, 1)
        arm.command_joint_pos(current)
        return arm
    except BaseException:
        try:
            _close_arm(arm)
        except Exception:
            log.exception('YAM startup validation cleanup failed on %s', channel)
        raise

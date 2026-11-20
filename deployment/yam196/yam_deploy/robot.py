"""One calibrated construction/close path for Server and Home."""
from __future__ import annotations

from . import runtime


class CalibratedArm:
    def __init__(self, robot):
        self._robot = robot

    def __getattr__(self, name):
        return getattr(self._robot, name)

    def close(self):
        from .calibration import _close_arm
        _close_arm(self._robot)


def open_arm(channel: str):
    cfg = runtime.require_running('robot')
    selected = [v for v in cfg['arms'].values() if v['channel'] == channel]
    if len(selected) != 1:
        raise ValueError(f'Unknown CAN channel: {channel}')
    arm = selected[0]
    from .calibration import _open_arm
    return CalibratedArm(_open_arm(channel, arm['gripper_limits'],
                                  calibration_offset=arm['calibration_offset']))


def close_many(robots):
    errors = []
    for robot in robots:
        if robot is None:
            continue
        try:
            robot.close()
        except Exception as exc:
            errors.append(str(exc))
    if errors:
        raise RuntimeError('Robot cleanup failed: ' + '; '.join(errors))

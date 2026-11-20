#!/usr/bin/env python3
"""Drive YAM arm(s) to agent.start_joints (home) in an isolated subprocess."""

from __future__ import annotations

import argparse
import os
import signal
import sys
from pathlib import Path
from typing import Any

import numpy as np
from omegaconf import OmegaConf

from gello_min.env import RobotEnv
from gello_min.launch_utils import instantiate_from_dict, move_to_start_position
from gello_min.robot import BimanualRobot

# Max |Δq| (rad) to treat current pose as already at start_joints.
_HOME_EPS_RAD = 0.05


def _build_robot_from_cfg(cfg: dict[str, Any]):
    robot_cfg = dict(cfg["robot"])
    if isinstance(robot_cfg.get("config"), str):
        robot_cfg["config"] = OmegaConf.to_container(
            OmegaConf.load(robot_cfg["config"]), resolve=True
        )
    return instantiate_from_dict(robot_cfg)


def _target_joints(
    *,
    bimanual: bool,
    left_cfg: dict[str, Any],
    right_cfg: dict[str, Any] | None,
) -> np.ndarray | None:
    if bimanual:
        if right_cfg is None:
            return None
        left_start = left_cfg["agent"].get("start_joints")
        right_start = right_cfg["agent"].get("start_joints")
        if left_start is None or right_start is None:
            return None
        return np.concatenate([np.asarray(left_start, dtype=float), np.asarray(right_start, dtype=float)])
    start = left_cfg["agent"].get("start_joints")
    if start is None:
        return None
    return np.asarray(start, dtype=float)


def _close_robot(robot: Any) -> None:
    close_fn = getattr(robot, "close", None)
    if callable(close_fn):
        close_fn()


def run_home_move(*, left_config_path, right_config_path=None, left_only=False, home_eps_rad=_HOME_EPS_RAD):
    from yam_deploy.runtime import require_running
    from yam_deploy.robot import close_many
    require_running('home')
    left_cfg = OmegaConf.to_container(OmegaConf.load(left_config_path), resolve=True)
    right_cfg = (OmegaConf.to_container(OmegaConf.load(right_config_path), resolve=True)
                 if right_config_path is not None and not left_only else None)
    bimanual = right_cfg is not None
    target = _target_joints(bimanual=bimanual, left_cfg=left_cfg, right_cfg=right_cfg)
    if target is None or target.shape != (14 if bimanual else 7,) or not np.isfinite(target).all():
        raise RuntimeError('A finite, validated Home target is required before opening hardware')
    def terminate(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, terminate)
    opened = []
    result = 1
    try:
        opened.append(_build_robot_from_cfg(left_cfg))
        if bimanual:
            opened.append(_build_robot_from_cfg(right_cfg))
        robot = BimanualRobot(*opened) if bimanual else opened[0]
        env = RobotEnv(robot, control_rate_hz=100.0, camera_dict={})
        curr = np.asarray(env.get_obs()['joint_positions'], dtype=float)
        if curr.shape != target.shape or not np.isfinite(curr).all():
            raise RuntimeError('Invalid Home feedback')
        if np.max(np.abs(curr - target)) >= home_eps_rad:
            move_to_start_position(env, bimanual=bimanual, left_cfg=left_cfg, right_cfg=right_cfg)
        final = np.asarray(env.get_obs()['joint_positions'], dtype=float)
        if final.shape != target.shape or not np.isfinite(final).all() or np.max(np.abs(final-target)) >= home_eps_rad:
            raise RuntimeError('Home position not reached')
        result = 0
    except (Exception, KeyboardInterrupt) as exc:
        print(f'home_move_failed: {exc}', file=sys.stderr)
    finally:
        try:
            close_many(opened)
        except Exception as exc:
            print(f'home_move_close_failed: {exc}', file=sys.stderr)
            result = 1
    return result


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Move YAM arms to start_joints (home)")
    parser.add_argument("--left-config-path", required=True)
    parser.add_argument("--right-config-path", default=None)
    parser.add_argument("--left-only", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    # run_home_move always os._exit's; return is unreachable in normal use.
    return run_home_move(
        left_config_path=args.left_config_path,
        right_config_path=args.right_config_path,
        left_only=args.left_only,
    )


if __name__ == "__main__":
    raise SystemExit(main())

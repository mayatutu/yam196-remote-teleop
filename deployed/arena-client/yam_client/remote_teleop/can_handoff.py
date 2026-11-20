"""CAN handoff gate before starting YAM remote teleop server."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence


DEFAULT_SUSPICIOUS_PATTERNS = (
    "launch_yaml_eval_molmoact",
    "yam_remote_dual_server",
)

# Patterns that must not be alive before starting teleop (block handoff).
BLOCKING_RESIDUAL_PATTERNS = (
    "launch_yaml_eval_molmoact",
    "yam_remote_dual_server",
)


@dataclass(frozen=True)
class HandoffResult:
    ok: bool
    reason: str = ""
    residual_pids: tuple[int, ...] = ()
    diagnostic: str = ""


def list_matching_pids(patterns: Sequence[str], *, fail_closed: bool = False) -> list[int]:
    """Best-effort residual process scan (Linux/ps). Returns empty on failure."""
    try:
        completed = subprocess.run(
            ["ps", "-eo", "pid,args"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        if fail_closed:
            raise RuntimeError("residual_scan_failed")
        return []
    if completed.returncode != 0:
        if fail_closed:
            raise RuntimeError("residual_scan_failed")
        return []
    self_pid = os.getpid()
    matches: list[int] = []
    for line in completed.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        if pid == self_pid:
            continue
        args = parts[1]
        if any(pat in args for pat in patterns):
            matches.append(pid)
    return matches


def _residual_blocking_pids(
    patterns: Sequence[str] = BLOCKING_RESIDUAL_PATTERNS,
) -> list[int]:
    return list_matching_pids(patterns)


def run_can_probe_subprocess(**kwargs):
    # Passive ownership check. Does not prove CAN exclusivity or initialize motors.
    from yam_deploy.runtime import require_running
    from yam_deploy.control import System
    require_running('handoff')
    pids = System().blockers(include_managed=True, include_video=False)
    return HandoffResult(ok=not pids, reason='residual_processes' if pids else 'passive_owner_check', residual_pids=tuple(pids))


def run_can_handoff(
    *,
    python: str = sys.executable,
    left_config: str,
    right_config: str | None,
    grace_sec: float = 2.0,
    probe_timeout_sec: float = 30.0,
    cwd: str | None = None,
    suspicious_patterns: Sequence[str] = DEFAULT_SUSPICIOUS_PATTERNS,
    skip_residual_check: bool = False,
    extra_patterns: Sequence[str] = (),
    fail_closed: bool = False,
) -> HandoffResult:
    """Full handoff gate: residual scan → grace → probe → post-probe scan."""
    patterns = tuple(suspicious_patterns) + tuple(extra_patterns)
    if not skip_residual_check:
        # Fail if eval launcher OR a leftover YAM teleop server still holds CAN.
        try:
            blocking = list_matching_pids(patterns, fail_closed=fail_closed)
        except RuntimeError as exc:
            return HandoffResult(ok=False, reason=str(exc))
        if blocking:
            eval_pids = list_matching_pids(("launch_yaml_eval_molmoact",))
            yam_pids = list_matching_pids(("yam_remote_dual_server",))
            if eval_pids:
                return HandoffResult(
                    ok=False,
                    reason="eval_process_still_running",
                    residual_pids=tuple(eval_pids),
                )
            if yam_pids:
                return HandoffResult(
                    ok=False,
                    reason="yam_remote_dual_server_still_running",
                    residual_pids=tuple(yam_pids),
                )
            return HandoffResult(
                ok=False,
                reason="residual_control_process_running",
                residual_pids=tuple(blocking),
            )

    time.sleep(max(0.0, grace_sec))

    probe = run_can_probe_subprocess(
        python=python,
        left_config=left_config,
        right_config=right_config,
        timeout_sec=probe_timeout_sec,
        cwd=cwd,
    )
    if not probe.ok:
        return probe

    # Post-probe复核: eval / yam server must still be gone after probe exits.
    time.sleep(0.2)
    if not skip_residual_check:
        try:
            post = list_matching_pids(patterns, fail_closed=fail_closed)
        except RuntimeError as exc:
            return HandoffResult(ok=False, reason=str(exc))
        if post:
            eval_pids = list_matching_pids(("launch_yaml_eval_molmoact",))
            yam_pids = list_matching_pids(("yam_remote_dual_server",))
            if eval_pids:
                return HandoffResult(
                    ok=False,
                    reason="eval_process_reappeared_after_probe",
                    residual_pids=tuple(eval_pids),
                )
            if yam_pids:
                return HandoffResult(
                    ok=False,
                    reason="yam_remote_dual_server_reappeared_after_probe",
                    residual_pids=tuple(yam_pids),
                )
            return HandoffResult(
                ok=False,
                reason="residual_control_process_after_probe",
                residual_pids=tuple(post),
            )
    return HandoffResult(ok=True, reason="ok")

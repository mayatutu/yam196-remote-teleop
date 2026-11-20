"""Preserve the Agent's ProfileResult interface without invoking legacy scripts."""
import subprocess
from . import runtime
from remote_teleop.wrist_video_profile import ProfileResult


class ProfileManager:
    def __init__(self):
        self.current_profile = 'unknown'

    def apply(self, profile, *, timeout_sec=20.0):
        if profile not in ('normal', 'reduced', 'head_only'):
            return ProfileResult(profile, False, 'invalid profile')
        try:
            r = subprocess.run([str(runtime.ROOT / 'current/.venv/bin/python'),
                                str(runtime.ROOT / 'current/launch.py'), 'profile', profile],
                               capture_output=True, text=True, timeout=timeout_sec)
            ok = r.returncode == 0
            if ok:
                self.current_profile = profile
            return ProfileResult(profile, ok, (r.stdout + r.stderr).strip())
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProfileResult(profile, False, str(exc))

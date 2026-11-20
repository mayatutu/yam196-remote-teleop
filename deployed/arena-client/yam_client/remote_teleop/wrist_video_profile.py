"""MiniPC-side, explicit lifecycle for the optional dual D405 wrist streams."""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path


VALID_PROFILES = frozenset({"normal", "reduced", "head_only"})


@dataclass(frozen=True)
class ProfileResult:
    profile: str
    ok: bool
    output: str


class WristVideoProfileManager:
    """Run the deployment script without interacting with eval's camera server."""

    def __init__(self, *, client_root: Path, script: Path | None = None) -> None:
        self.client_root = Path(client_root).resolve()
        self.script = script or (
            self.client_root.parent / "deployment" / "run-dual-wrist-d405-senders.sh"
        )
        self.current_profile = "unknown"

    def apply(self, profile: str, *, timeout_sec: float = 20.0) -> ProfileResult:
        if profile not in VALID_PROFILES:
            return ProfileResult(profile, False, f"invalid profile: {profile}")
        if not self.script.is_file():
            return ProfileResult(profile, False, f"wrist sender script not found: {self.script}")
        try:
            completed = subprocess.run(
                ["bash", str(self.script), "set-profile", profile],
                cwd=str(self.client_root),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=timeout_sec,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return ProfileResult(profile, False, str(exc))
        output = (completed.stdout or "").strip()
        if completed.returncode:
            return ProfileResult(profile, False, output or f"exit={completed.returncode}")
        self.current_profile = profile
        return ProfileResult(profile, True, output)

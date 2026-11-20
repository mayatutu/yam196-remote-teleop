"""MiniPC CLI: python3 -m remote_teleop.teleop_control {status,enable,disable}."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from remote_teleop.teleop_ipc import default_socket_path, send_command


def _robot_id(config_path: str) -> str:
    from omegaconf import OmegaConf

    cfg = OmegaConf.to_container(OmegaConf.load(config_path), resolve=False) or {}
    return str(cfg.get("robot_id") or "").strip() or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local teleop capability CLI")
    parser.add_argument("command", choices=("status", "enable", "disable"))
    parser.add_argument("--config", default=os.environ.get("CONFIG", "yam_client/configs/arena_eval.yaml"))
    parser.add_argument("--socket", default="")
    args = parser.parse_args(argv)
    path = Path(args.socket) if args.socket else default_socket_path(_robot_id(args.config))
    try:
        resp = send_command(path, args.command)
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"ok": False, "layer": "agent", "error": str(exc)}), file=sys.stderr)
        print(f"agent layer: IPC failed ({exc})", file=sys.stderr)
        return 1
    print(json.dumps(resp, default=str))
    if not resp.get("ok"):
        layer = resp.get("layer") or "agent"
        err = resp.get("error") or resp.get("result") or "failed"
        print(f"{layer} layer: {err}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

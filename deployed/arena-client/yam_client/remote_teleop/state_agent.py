"""MiniPC Agent WebSocket client to FastAPI State Server."""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import deque
from typing import Any, Callable

log = logging.getLogger(__name__)

AgentEventCallback = Callable[[str, dict[str, Any]], None]
ConnectionCallback = Callable[[bool], None]
IdentityProvider = Callable[[], dict[str, Any]]

_OFFLINE_QUEUE_MAX = 256
_NO_OFFLINE_QUEUE = frozenset({"agent_hello", "agent_heartbeat", "teleop_capability"})


class StateAgentClient:
    """Maintains WS /ws/robots/{robot_id} to State Server."""

    def __init__(
        self,
        *,
        websocket_base: str,
        token: str,
        robot_id: str,
        on_command: AgentEventCallback | None = None,
        on_connection: ConnectionCallback | None = None,
        identity_provider: IdentityProvider | None = None,
    ) -> None:
        self.websocket_base = websocket_base.rstrip("/")
        self.token = token
        self.robot_id = robot_id
        self.on_command = on_command
        self.on_connection = on_connection
        self.identity_provider = identity_provider
        self.connected = False
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._send_lock = threading.Lock()
        self._ws: Any = None
        self._offline_queue: deque[tuple[str, dict[str, Any]]] = deque(maxlen=_OFFLINE_QUEUE_MAX)
        self._hb_thread: threading.Thread | None = None

    def _ws_url(self) -> str:
        base = self.websocket_base
        if base.startswith("ws"):
            return f"{base}/ws/robots/{self.robot_id}?token={self.token}"
        return f"ws://{base.split('://')[-1]}/ws/robots/{self.robot_id}?token={self.token}"

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name=f"state-agent-{self.robot_id}", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._send_lock:
            self.connected = False
            if self._ws is not None:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None
        if self.on_connection:
            self.on_connection(False)

    def _encode_event(self, event_type: str, payload: dict[str, Any] | None) -> str:
        pl = dict(payload or {})
        ident = self.identity_provider() if self.identity_provider else {}
        body: dict[str, Any] = {
            "type": event_type,
            "robot_id": self.robot_id,
            "payload": pl,
            "agent_instance_id": ident.get("agent_instance_id") or pl.get("agent_instance_id"),
            "connection_epoch": ident.get("connection_epoch") if ident.get("connection_epoch") is not None else pl.get("connection_epoch"),
        }
        session_id = ident.get("session_id") or pl.get("session_id")
        if session_id:
            body["session_id"] = session_id
        for key in ("eval_id", "phase", "substate", "operation_id", "arm_session"):
            if key in pl:
                body[key] = pl[key]
        return json.dumps(body)

    def _flush_offline(self, ws: Any) -> None:
        while self._offline_queue:
            etype, pl = self._offline_queue.popleft()
            try:
                ws.send(self._encode_event(etype, pl))
            except Exception as exc:
                log.warning("flush offline event %s failed: %s", etype, exc)
                self._offline_queue.appendleft((etype, pl))
                break

    def send_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        queue_offline: bool = True,
    ) -> None:
        pl = dict(payload or {})
        with self._send_lock:
            ws = self._ws
            if ws is not None and self.connected:
                try:
                    ws.send(self._encode_event(event_type, pl))
                    return
                except Exception as exc:
                    log.debug("send_event %s on live ws failed: %s", event_type, exc)
                    self.connected = False
            if queue_offline and event_type not in _NO_OFFLINE_QUEUE:
                self._offline_queue.append((event_type, pl))

    def _loop(self) -> None:
        try:
            import websocket  # type: ignore
        except ImportError:
            log.warning("websocket-client not installed; State Agent WS disabled")
            return
        url = self._ws_url()
        while not self._stop.is_set():
            ws = None
            try:
                ws = websocket.create_connection(url, timeout=10)
                with self._send_lock:
                    self._ws = ws
                    self.connected = True
                if self.on_connection:
                    self.on_connection(True)
                self._flush_offline(ws)
                last_hb = 0.0
                while not self._stop.is_set():
                    now = time.monotonic()
                    if now - last_hb >= 2.0:
                        self.send_event("agent_heartbeat", {}, queue_offline=False)
                        last_hb = now
                    ws.settimeout(1.0)
                    try:
                        raw = ws.recv()
                    except Exception as exc:
                        name = type(exc).__name__
                        if "Timeout" in name or "timeout" in str(exc).lower():
                            continue
                        break
                    if not raw:
                        break
                    data = json.loads(raw)
                    etype = data.get("type", "")
                    payload = data.get("payload") or {}
                    if self.on_command and etype:
                        merged = {
                            **payload,
                            **{
                                k: data[k]
                                for k in (
                                    "session_id",
                                    "eval_id",
                                    "agent_instance_id",
                                    "connection_epoch",
                                    "operation_id",
                                    "arm_session",
                                )
                                if k in data
                            },
                        }
                        self.on_command(etype, merged)
                ws.close()
            except Exception as exc:
                log.debug("state agent ws: %s", exc)
            finally:
                with self._send_lock:
                    self.connected = False
                    if self._ws is ws:
                        self._ws = None
            if self.on_connection:
                self.on_connection(False)
            time.sleep(2.0)

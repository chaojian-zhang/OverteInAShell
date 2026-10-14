"""Link session: owns the server, picks the active bridge and routes messages.

This module does not import ``bpy``. Scene work is delegated to a *sync*
object (``scene_sync.SceneSync`` in Blender, a fake in tests) with methods:

* ``on_bridge_connected(hello)`` / ``on_bridge_disconnected()``
* ``collect_outgoing() -> list[dict]`` local edits to send this tick
* ``handle_message(msg) -> list[dict]`` apply a bridge message, returns replies
"""

from __future__ import annotations

import json
import os
import time
from typing import Callable, Optional

from .wsserver import LinkServer

BRIDGE_FILE = os.path.join(os.path.dirname(__file__), "bridge", "overte_bridge.js")
BRIDGE_PATH = "/overte_bridge.js"
WS_PLACEHOLDER = "__OVERTE_LIVE_LINK_WS_URL__"
PROTOCOL_VERSION = 1


def bridge_script(ws_url: str) -> str:
    with open(BRIDGE_FILE, "r", encoding="utf-8") as f:
        return f.read().replace(WS_PLACEHOLDER, ws_url)


class LinkSession:
    def __init__(self, sync, host: str = "127.0.0.1", port: int = 9420, log: Callable[[str], None] = print):
        self.sync = sync
        self.log = log
        self.server = LinkServer(host, port, http_handler=self._http)
        self.active: Optional[int] = None          # cid of the bridge we sync with
        self.bridges: dict[int, dict] = {}         # cid -> hello info (None until hello)
        self.pending: dict[int, Callable] = {}     # req id -> callback(msg, binary)
        self._next_req = 1
        self._await_binary: Optional[tuple] = None  # (callback, header) for "fetched"
        self.last_error = ""
        self.started_at = 0.0
        if hasattr(sync, "attach"):
            sync.attach(self)

    # ---------------------------------------------------------- lifecycle
    def start(self) -> int:
        port = self.server.start()
        self.started_at = time.time()
        self.log(f"Overte Live Link listening on {self.server.host}:{port}")
        return port

    def stop(self) -> None:
        self.server.stop()
        if self.active is not None:
            self.sync.on_bridge_disconnected()
        self.active = None
        self.bridges.clear()
        self.pending.clear()

    @property
    def running(self) -> bool:
        return self.server.running

    @property
    def port(self) -> int:
        return self.server.port

    @property
    def bridge_info(self) -> Optional[dict]:
        return self.bridges.get(self.active) if self.active is not None else None

    def bridge_url(self, host: Optional[str] = None) -> str:
        h = host or (self.server.host if self.server.host not in ("0.0.0.0", "") else "127.0.0.1")
        return f"http://{h}:{self.server.port}{BRIDGE_PATH}"

    # --------------------------------------------------------------- HTTP
    def _http(self, target: str, headers: dict):
        path = target.split("?", 1)[0]
        if path == BRIDGE_PATH:
            host = headers.get("host") or f"127.0.0.1:{self.server.port}"
            return 200, "application/javascript; charset=utf-8", bridge_script(f"ws://{host}/ws").encode("utf-8")
        if path == "/":
            return 200, "text/plain; charset=utf-8", (
                "Overte Live Link is running.\n"
                f"Add {BRIDGE_PATH} on this host as a Persistent Script in your domain settings,\n"
                "or load it in Interface via Edit > Running Scripts > From URL.\n").encode()
        return None

    # ---------------------------------------------------------- messaging
    def send(self, msg: dict) -> bool:
        if self.active is None:
            return False
        return self.server.send(self.active, json.dumps(msg, separators=(",", ":")))

    def request(self, msg: dict, callback: Callable, binary: Optional[bytes] = None) -> int:
        """Send ``msg`` with a fresh ``req`` id; ``callback(reply, data)`` runs on reply."""
        req = self._next_req
        self._next_req += 1
        msg = dict(msg, req=req)
        self.pending[req] = callback
        if not self.send(msg):
            self.pending.pop(req, None)
            callback({"type": "error", "req": req, "message": "no bridge connected"}, None)
            return req
        if binary is not None:
            self.server.send(self.active, binary)
        return req

    def tick(self) -> None:
        """Run one sync step. Call regularly from Blender's main thread."""
        if not self.running:
            return
        # 1. local changes first, so incoming changes are merged against fresh state
        if self.active is not None and self.bridge_info:
            for msg in self.sync.collect_outgoing():
                self.send(msg)
        # 2. incoming
        for ev in self.server.poll():
            kind, cid = ev[0], ev[1]
            if kind == "open":
                self.bridges[cid] = None
                if self.active is None:
                    self.active = cid
            elif kind == "close":
                self.bridges.pop(cid, None)
                if cid == self.active:
                    self.active = None
                    self.pending.clear()
                    self._await_binary = None
                    self.sync.on_bridge_disconnected()
                    self.log("Overte bridge disconnected")
                    self._promote()
            elif kind == "message":
                self._on_message(cid, ev[2])

    def _promote(self) -> None:
        for cid, info in self.bridges.items():
            if info:
                self._activate(cid, info)
                return
            if self.active is None:
                self.active = cid

    def _activate(self, cid: int, hello: dict) -> None:
        self.active = cid
        self.log(f"Overte bridge connected ({hello.get('mode', '?')} mode)")
        for msg in self.sync.on_bridge_connected(hello):
            self.send(msg)

    def _on_message(self, cid: int, data) -> None:
        if isinstance(data, (bytes, bytearray)):
            if cid == self.active and self._await_binary:
                cb, header = self._await_binary
                self._await_binary = None
                cb(header, bytes(data))
            return
        try:
            msg = json.loads(data)
        except ValueError:
            return
        mtype = msg.get("type")
        if mtype == "hello":
            if msg.get("version") != PROTOCOL_VERSION:
                self.last_error = f"bridge protocol {msg.get('version')} != {PROTOCOL_VERSION}; update the bridge script"
                self.log(self.last_error)
            self.bridges[cid] = msg
            if self.active is None or self.active == cid or not self.bridges.get(self.active):
                self._activate(cid, msg)
            return
        if cid != self.active:
            return  # standby bridges are ignored
        req = msg.get("req")
        if req is not None and req in self.pending:
            cb = self.pending.pop(req)
            if mtype == "fetched" and not msg.get("error"):
                self._await_binary = (cb, msg)  # content follows as a binary frame
            else:
                cb(msg, None)
            if mtype in ("added", "edited"):
                pass  # also routed to sync below
            else:
                return
        if mtype == "error":
            self.last_error = msg.get("message", "error")
            self.log(f"Overte bridge error: {self.last_error}")
            return
        for reply in self.sync.handle_message(msg) or ():
            self.send(reply)

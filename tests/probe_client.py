"""Client for tests/overte_probe.js, an independent agent inside the test domain.

No pytest or bpy imports: also used by the GUI end-to-end script inside Blender.
"""

import json
import os
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Probe:
    """Talks to tests/overte_probe.js, an independent agent inside the domain."""

    def __init__(self, port, server_cls=None):
        if server_cls is None:
            from overte_live_link.wsserver import LinkServer as server_cls

        src = open(os.path.join(ROOT, "tests", "overte_probe.js"), encoding="utf-8").read()

        def http(target, headers):
            if target.startswith("/overte_probe.js"):
                return 200, "application/javascript", src.replace(
                    "__OVERTE_LIVE_LINK_WS_URL__", f"ws://{headers['host']}/ws").encode()
            return None

        self.server = server_cls("127.0.0.1", port, http_handler=http)
        self.server.start()
        self.cid = None
        self._req = 0
        self._replies = {}

    def url(self):
        return f"http://127.0.0.1:{self.server.port}/overte_probe.js"

    def pump(self):
        for ev in self.server.poll():
            if ev[0] == "open":
                self.cid = ev[1]
            elif ev[0] == "close" and ev[1] == self.cid:
                self.cid = None
            elif ev[0] == "message":
                msg = json.loads(ev[2])
                if "req" in msg:
                    self._replies[msg["req"]] = msg["data"]

    def wait_connected(self, timeout=90):
        end = time.time() + timeout
        while time.time() < end:
            self.pump()
            if self.cid is not None:
                return
            time.sleep(0.1)
        raise TimeoutError("probe agent did not connect")

    def call(self, op, timeout=10, retries=3, **kw):
        # The domain occasionally restarts an agent (e.g. after a failed STUN
        # lookup with no internet access); wait for the probe to come back and
        # retry. Only idempotent reads are retried after a lost reply.
        for attempt in range(retries + 1):
            if self.cid is None:
                self.wait_connected(60)
            self._req += 1
            req = self._req
            cid = self.cid
            self.server.send(cid, json.dumps(dict(kw, op=op, req=req)))
            end = time.time() + timeout
            while time.time() < end and self.cid == cid:
                self.pump()
                if req in self._replies:
                    return self._replies.pop(req)
                time.sleep(0.02)
            if op not in ("get", "list"):
                break
        raise TimeoutError(f"probe {op} timed out")

    def entities(self):
        return {p["id"].lower(): p for p in self.call("list")}

    def stop(self):
        self.server.stop()

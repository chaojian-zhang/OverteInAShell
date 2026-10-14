"""Fixtures for integration tests against a real local Overte domain.

Integration tests are marked ``overte``. They need the overte-server package
(``/opt/overte`` or ``$OVERTE_DIR``) and ``bpy``; they are skipped otherwise.
"""

import json
import os
import shutil
import subprocess
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OVERTE_DIR = os.environ.get("OVERTE_DIR", "/opt/overte")
LINK_PORT = int(os.environ.get("OVERTE_TEST_LINK_PORT", "19420"))
PROBE_PORT = int(os.environ.get("OVERTE_TEST_PROBE_PORT", "19421"))


class Probe:
    """Talks to tests/overte_probe.js, an independent agent inside the domain."""

    def __init__(self, port):
        from overte_live_link.wsserver import LinkServer

        src = open(os.path.join(ROOT, "tests", "overte_probe.js"), encoding="utf-8").read()

        def http(target, headers):
            if target.startswith("/overte_probe.js"):
                return 200, "application/javascript", src.replace(
                    "__OVERTE_LIVE_LINK_WS_URL__", f"ws://{headers['host']}/ws").encode()
            return None

        self.server = LinkServer("127.0.0.1", port, http_handler=http)
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

    def call(self, op, timeout=10, **kw):
        self._req += 1
        req = self._req
        self.server.send(self.cid, json.dumps(dict(kw, op=op, req=req)))
        end = time.time() + timeout
        while time.time() < end:
            self.pump()
            if req in self._replies:
                return self._replies.pop(req)
            time.sleep(0.02)
        raise TimeoutError(f"probe {op} timed out")

    def entities(self):
        return {p["id"].lower(): p for p in self.call("list")}

    def stop(self):
        self.server.stop()


def _launcher(*args, env):
    return subprocess.run([os.path.join(ROOT, "scripts", "run_local_domain.sh"), *args],
                          env=env, check=True, capture_output=True, text=True, cwd=ROOT)


@pytest.fixture(scope="module")
def overte_env(tmp_path_factory):
    """A fresh local domain running the bridge (served by a Blender session) and the probe."""
    if not os.path.exists(os.path.join(OVERTE_DIR, "domain-server")):
        pytest.skip(f"Overte server not installed in {OVERTE_DIR}")
    bpy = pytest.importorskip("bpy")
    from overte_live_link import runtime  # noqa: F401  (imports bpy-dependent modules)
    from overte_live_link.scene_sync import SceneSync
    from overte_live_link.session import LinkSession

    bpy.ops.wm.read_factory_settings(use_empty=True)
    logs = []
    sync = SceneSync(bpy.context.scene, log=logs.append, poll_ms=50)
    session = LinkSession(sync, "127.0.0.1", LINK_PORT, log=logs.append)
    session.start()
    probe = Probe(PROBE_PORT)

    home = tmp_path_factory.mktemp("overte-domain")
    env = dict(os.environ, DOMAIN_HOME=str(home), FRESH="1", OVERTE_DIR=OVERTE_DIR)
    _launcher("start", session.bridge_url(), probe.url(), env=env)
    try:
        probe.wait_connected()
        end = time.time() + 90
        while time.time() < end and not sync.synced:
            session.tick()
            time.sleep(0.05)
        assert sync.synced, f"bridge never synced; log: {logs}"
        yield {"bpy": bpy, "session": session, "sync": sync, "probe": probe, "logs": logs, "home": home}
    finally:
        try:
            _launcher("stop", env=env)
        finally:
            session.stop()
            probe.stop()
            if os.environ.get("KEEP_OVERTE_LOGS"):
                shutil.copytree(home / "run", os.path.join(ROOT, ".overte-test-logs"), dirs_exist_ok=True)

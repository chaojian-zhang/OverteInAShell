"""Fixtures for integration tests against a real local Overte domain.

Integration tests are marked ``overte``. They need the overte-server package
(``/opt/overte`` or ``$OVERTE_DIR``) and ``bpy``; they are skipped otherwise.
"""

import os
import shutil
import subprocess
import time

import pytest

from probe_client import Probe

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OVERTE_DIR = os.environ.get("OVERTE_DIR", "/opt/overte")
LINK_PORT = int(os.environ.get("OVERTE_TEST_LINK_PORT", "19420"))
PROBE_PORT = int(os.environ.get("OVERTE_TEST_PROBE_PORT", "19421"))


def _launcher(*args, env):
    r = subprocess.run([os.path.join(ROOT, "scripts", "run_local_domain.sh"), *args],
                       env=env, capture_output=True, text=True, cwd=ROOT)
    if r.returncode != 0:
        raise RuntimeError(f"run_local_domain.sh {args[0]} failed ({r.returncode}):\n{r.stdout}\n{r.stderr}")
    return r


@pytest.fixture(scope="module")
def overte_env(tmp_path_factory, request):
    """A fresh local domain running the bridge (served by a Blender session) and the probe."""
    if not os.path.exists(os.path.join(OVERTE_DIR, "domain-server")):
        pytest.skip(f"Overte server not installed in {OVERTE_DIR}")
    bpy = pytest.importorskip("bpy")
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
    try:
        _launcher("start", session.bridge_url(), probe.url(), env=env)
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
            if os.environ.get("KEEP_OVERTE_LOGS") and (home / "run").exists():
                dest = os.path.join(ROOT, ".overte-test-logs", request.module.__name__)
                shutil.copytree(home / "run", dest, dirs_exist_ok=True)

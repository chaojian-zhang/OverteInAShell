import json
import time
import urllib.request

from websockets.sync.client import connect

from overte_live_link.session import LinkSession


class FakeSync:
    def __init__(self):
        self.connected = []
        self.disconnected = 0
        self.handled = []
        self.outgoing = []

    def on_bridge_connected(self, hello):
        self.connected.append(hello)
        return [{"type": "configure"}, {"type": "sync"}]

    def on_bridge_disconnected(self):
        self.disconnected += 1

    def collect_outgoing(self):
        out, self.outgoing = self.outgoing, []
        return out

    def handle_message(self, msg):
        self.handled.append(msg)
        return []


def pump(s, cond, timeout=5):
    end = time.time() + timeout
    while time.time() < end:
        s.tick()
        if cond():
            return True
        time.sleep(0.01)
    raise AssertionError("timeout")


def test_bridge_script_is_served_with_ws_url_and_token():
    s = LinkSession(FakeSync(), "127.0.0.1", 0, log=lambda m: None, token="s3cret")
    s.start()
    try:
        assert s.bridge_url().endswith("/overte_bridge.js?token=s3cret")
        js = urllib.request.urlopen(s.bridge_url(), timeout=5).read().decode()
        assert f'var WS_URL = "ws://127.0.0.1:{s.port}/ws?token=s3cret";' in js
        assert "__OVERTE_LIVE_LINK_WS_URL__" not in js
    finally:
        s.stop()


def test_token_is_enforced():
    import pytest
    from websockets.exceptions import InvalidStatus

    sync = FakeSync()
    s = LinkSession(sync, "127.0.0.1", 0, log=lambda m: None)  # generated token
    s.start()
    try:
        assert len(s.token) >= 16
        for bad in ("", "?token=nope"):
            with pytest.raises(urllib.error.HTTPError) as err:
                urllib.request.urlopen(f"http://127.0.0.1:{s.port}/overte_bridge.js{bad}", timeout=5)
            assert err.value.code == 403
            with pytest.raises(InvalidStatus):
                connect(f"ws://127.0.0.1:{s.port}/ws{bad}")
        with connect(f"ws://127.0.0.1:{s.port}/ws?token={s.token}") as ws:
            ws.send(json.dumps({"type": "hello", "version": 1}))
            pump(s, lambda: sync.connected)
    finally:
        s.stop()


def test_active_and_standby_bridges():
    sync = FakeSync()
    s = LinkSession(sync, "127.0.0.1", 0, log=lambda m: None, token="")
    s.start()
    url = f"ws://127.0.0.1:{s.port}/ws"
    try:
        a = connect(url)
        a.send(json.dumps({"type": "hello", "version": 1, "mode": "agent"}))
        pump(s, lambda: sync.connected)
        assert json.loads(a.recv(timeout=5))["type"] == "configure"
        assert json.loads(a.recv(timeout=5))["type"] == "sync"

        b = connect(url)
        b.send(json.dumps({"type": "hello", "version": 1, "mode": "interface"}))
        b.send(json.dumps({"type": "changes", "upserts": []}))  # standby: ignored
        a.send(json.dumps({"type": "changes", "upserts": [], "from": "a"}))
        pump(s, lambda: sync.handled)
        assert [m.get("from") for m in sync.handled] == ["a"]

        # Local edits go to the active bridge only.
        sync.outgoing = [{"type": "edit", "edits": []}]
        pump(s, lambda: not sync.outgoing)
        assert json.loads(a.recv(timeout=5))["type"] == "edit"

        # Active bridge leaves: the standby takes over and is resynced.
        a.close()
        pump(s, lambda: len(sync.connected) == 2)
        assert sync.disconnected == 1
        assert sync.connected[-1]["mode"] == "interface"
        assert json.loads(b.recv(timeout=5))["type"] == "configure"
        b.close()
    finally:
        s.stop()


def test_request_reply_and_binary():
    sync = FakeSync()
    s = LinkSession(sync, "127.0.0.1", 0, log=lambda m: None, token="")
    s.start()
    try:
        ws = connect(f"ws://127.0.0.1:{s.port}/ws")
        ws.send(json.dumps({"type": "hello", "version": 1}))
        pump(s, lambda: sync.connected)
        ws.recv(timeout=5), ws.recv(timeout=5)
        got = []
        s.request({"type": "upload", "path": "/x.glb"}, lambda m, d: got.append((m, d)), binary=b"\x00\x01")
        hdr = json.loads(ws.recv(timeout=5))
        assert hdr["type"] == "upload" and ws.recv(timeout=5) == b"\x00\x01"
        ws.send(json.dumps({"type": "uploaded", "req": hdr["req"], "url": "atp:/x.glb"}))
        pump(s, lambda: got)
        assert got[0][0]["url"] == "atp:/x.glb"

        s.request({"type": "fetch", "url": "atp:/x.glb"}, lambda m, d: got.append((m, d)))
        hdr = json.loads(ws.recv(timeout=5))
        ws.send(json.dumps({"type": "fetched", "req": hdr["req"], "error": None, "size": 3}))
        ws.send(b"abc")
        pump(s, lambda: len(got) == 2)
        assert got[1][1] == b"abc"
        ws.close()
    finally:
        s.stop()

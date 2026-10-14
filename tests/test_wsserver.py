import json
import time
import urllib.request

import pytest
from websockets.sync.client import connect

from overte_live_link.wsserver import LinkServer


def wait_events(server, n=1, timeout=5.0):
    out = []
    end = time.time() + timeout
    while time.time() < end and len(out) < n:
        out += server.poll()
        time.sleep(0.01)
    return out


@pytest.fixture
def server():
    def http(target, headers):
        if target.startswith("/hello.js"):
            return 200, "application/javascript", ("var host = '%s';" % headers.get("host")).encode()
        return None

    s = LinkServer("127.0.0.1", 0, http_handler=http)
    s.start()
    yield s
    s.stop()


def test_http_get(server):
    body = urllib.request.urlopen(f"http://127.0.0.1:{server.port}/hello.js?x=1", timeout=5).read()
    assert body == f"var host = '127.0.0.1:{server.port}';".encode()
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(f"http://127.0.0.1:{server.port}/missing", timeout=5)
    assert err.value.code == 404


def test_text_roundtrip_and_close(server):
    with connect(f"ws://127.0.0.1:{server.port}/ws") as ws:
        (ev,) = wait_events(server)
        assert ev[0] == "open"
        cid = ev[1]
        assert server.clients() == [cid]
        ws.send(json.dumps({"type": "hello", "text": "héllo ✓"}))
        (ev,) = wait_events(server)
        assert ev == ("message", cid, json.dumps({"type": "hello", "text": "héllo ✓"}))
        assert server.send(cid, "pong ✓")
        assert ws.recv(timeout=5) == "pong ✓"
    (ev,) = wait_events(server)
    assert ev == ("close", cid)
    assert server.clients() == []


def test_large_fragmented_binary(server):
    payload = bytes(range(256)) * 20000  # ~5 MB
    with connect(f"ws://127.0.0.1:{server.port}/ws", max_size=None) as ws:
        (_, cid), = wait_events(server)
        # websockets sends an iterable as a fragmented message
        ws.send([payload[i:i + 70000] for i in range(0, len(payload), 70000)])
        (ev,) = wait_events(server, timeout=20)
        assert ev[0] == "message" and ev[2] == payload
        server.send(cid, payload)
        assert ws.recv(timeout=20) == payload


def test_ping_and_broadcast(server):
    with connect(f"ws://127.0.0.1:{server.port}/") as a, connect(f"ws://127.0.0.1:{server.port}/ws") as b:
        assert len(wait_events(server, 2)) == 2
        a.ping().wait(timeout=5)
        assert server.broadcast("all") == 2
        assert a.recv(timeout=5) == "all"
        assert b.recv(timeout=5) == "all"


def test_server_disconnect(server):
    with connect(f"ws://127.0.0.1:{server.port}/ws") as ws:
        (_, cid), = wait_events(server)
        server.disconnect(cid)
        with pytest.raises(Exception):
            ws.recv(timeout=5)
    assert ("close", cid) in wait_events(server)


def test_stop_and_restart_same_port():
    s = LinkServer("127.0.0.1", 0)
    port = s.start()
    s.stop()
    s2 = LinkServer("127.0.0.1", port)
    assert s2.start() == port
    s2.stop()

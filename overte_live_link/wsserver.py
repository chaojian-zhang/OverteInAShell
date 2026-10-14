"""Minimal, dependency-free WebSocket + HTTP server.

Blender ships no WebSocket library, so this module implements just enough of
RFC 6455 (text/binary messages, fragmentation, ping/pong, close) plus plain
HTTP GET on the same port. The HTTP side is used to serve the Overte bridge
script, so a domain can load it straight from Blender.

The server runs on a background thread using ``selectors``. All interaction
from other threads (Blender's main thread) is thread safe:

* ``poll()`` drains received events: ``("open", cid)``,
  ``("message", cid, str | bytes)``, ``("close", cid)``.
* ``send(cid, data)`` queues a text (``str``) or binary (``bytes``) message.

Nothing here imports ``bpy``; it is unit-tested with plain Python.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import queue
import selectors
import socket
import struct
import threading
from typing import Callable, Optional, Tuple

__all__ = ["LinkServer", "HttpResponse"]

_WS_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_MAX_HEADER = 64 * 1024
_MAX_MESSAGE = 512 * 1024 * 1024  # generous: mesh uploads travel over this link

OP_CONT, OP_TEXT, OP_BINARY, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA

# (status code, content type, body) or None for 404
HttpResponse = Optional[Tuple[int, str, bytes]]
HttpHandler = Callable[[str, dict], HttpResponse]

_REASONS = {200: "OK", 400: "Bad Request", 404: "Not Found", 500: "Internal Server Error"}


def encode_frame(opcode: int, payload: bytes, fin: bool = True, mask: Optional[bytes] = None) -> bytes:
    """Encode one WebSocket frame. Servers send unmasked frames; tests use ``mask``."""
    head = bytearray()
    head.append((0x80 if fin else 0) | opcode)
    mask_bit = 0x80 if mask else 0
    n = len(payload)
    if n < 126:
        head.append(mask_bit | n)
    elif n < 1 << 16:
        head.append(mask_bit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(mask_bit | 127)
        head += struct.pack("!Q", n)
    if mask:
        head += mask
        payload = _apply_mask(payload, mask)
    return bytes(head) + payload


def _apply_mask(data: bytes, mask: bytes) -> bytes:
    if not data:
        return data
    # XOR via big integers is far faster than a Python byte loop.
    reps = (len(data) + 3) // 4
    key = int.from_bytes((mask * reps)[: len(data)], "big")
    return (int.from_bytes(data, "big") ^ key).to_bytes(len(data), "big")


class _Conn:
    __slots__ = ("cid", "sock", "inbuf", "outbuf", "is_ws", "frag_op", "frag", "closing", "addr")

    def __init__(self, cid: int, sock: socket.socket, addr):
        self.cid = cid
        self.sock = sock
        self.addr = addr
        self.inbuf = bytearray()
        self.outbuf = bytearray()
        self.is_ws = False
        self.frag_op: Optional[int] = None
        self.frag = bytearray()
        self.closing = False  # close after outbuf drains


class LinkServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 9420,
                 http_handler: Optional[HttpHandler] = None, ws_paths=("/ws", "/")):
        self.host = host
        self.port = port
        self.http_handler = http_handler
        self.ws_paths = tuple(ws_paths)
        self._events: "queue.Queue[tuple]" = queue.Queue()
        self._lock = threading.Lock()
        self._conns: dict[int, _Conn] = {}
        self._ids = itertools.count(1)
        self._thread: Optional[threading.Thread] = None
        self._running = False
        self._sel: Optional[selectors.BaseSelector] = None
        self._listener: Optional[socket.socket] = None
        self._wake_r: Optional[socket.socket] = None
        self._wake_w: Optional[socket.socket] = None

    # ------------------------------------------------------------------ API
    def start(self) -> int:
        """Bind and start serving. Returns the bound port (useful with port=0)."""
        if self._running:
            return self.port
        lst = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        lst.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        lst.bind((self.host, self.port))
        lst.listen(16)
        lst.setblocking(False)
        self.port = lst.getsockname()[1]
        self._listener = lst
        self._wake_r, self._wake_w = socket.socketpair()
        self._wake_r.setblocking(False)
        self._sel = selectors.DefaultSelector()
        self._sel.register(lst, selectors.EVENT_READ, "listen")
        self._sel.register(self._wake_r, selectors.EVENT_READ, "wake")
        self._running = True
        self._thread = threading.Thread(target=self._run, name="OverteLinkServer", daemon=True)
        self._thread.start()
        return self.port

    def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        self._wake()
        if self._thread:
            self._thread.join(timeout=3)
        self._thread = None

    @property
    def running(self) -> bool:
        return self._running

    def clients(self) -> list[int]:
        with self._lock:
            return [c.cid for c in self._conns.values() if c.is_ws and not c.closing]

    def client_address(self, cid: int):
        with self._lock:
            c = self._conns.get(cid)
            return c.addr if c else None

    def poll(self) -> list[tuple]:
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    def send(self, cid: int, data) -> bool:
        if isinstance(data, str):
            frame = encode_frame(OP_TEXT, data.encode("utf-8"))
        else:
            frame = encode_frame(OP_BINARY, bytes(data))
        return self._queue(cid, frame)

    def broadcast(self, data) -> int:
        n = 0
        for cid in self.clients():
            n += bool(self.send(cid, data))
        return n

    def disconnect(self, cid: int) -> None:
        self._queue(cid, encode_frame(OP_CLOSE, struct.pack("!H", 1000)), close_after=True)

    # ------------------------------------------------------------ internals
    def _queue(self, cid: int, raw: bytes, close_after: bool = False) -> bool:
        with self._lock:
            c = self._conns.get(cid)
            if c is None or (c.closing and not close_after):
                return False
            c.outbuf += raw
            if close_after:
                c.closing = True
        self._wake()
        return True

    def _wake(self) -> None:
        try:
            if self._wake_w:
                self._wake_w.send(b"x")
        except OSError:
            pass

    def _run(self) -> None:
        sel = self._sel
        assert sel is not None
        try:
            while self._running:
                # Update write interest for connections with pending output.
                with self._lock:
                    conns = list(self._conns.values())
                for c in conns:
                    want = selectors.EVENT_READ | (selectors.EVENT_WRITE if c.outbuf else 0)
                    try:
                        key = sel.get_key(c.sock)
                        if key.events != want:
                            sel.modify(c.sock, want, c)
                    except (KeyError, ValueError):
                        pass
                for key, mask in sel.select(timeout=0.5):
                    if key.data == "listen":
                        self._accept()
                    elif key.data == "wake":
                        try:
                            self._wake_r.recv(4096)
                        except OSError:
                            pass
                    else:
                        c: _Conn = key.data
                        if mask & selectors.EVENT_READ:
                            self._on_readable(c)
                        if mask & selectors.EVENT_WRITE and c.cid in self._conns:
                            self._on_writable(c)
        finally:
            with self._lock:
                conns = list(self._conns.values())
            for c in conns:
                self._drop(c)
            sel.close()
            for s in (self._listener, self._wake_r, self._wake_w):
                try:
                    if s:
                        s.close()
                except OSError:
                    pass
            self._listener = self._wake_r = self._wake_w = None

    def _accept(self) -> None:
        try:
            sock, addr = self._listener.accept()
        except OSError:
            return
        sock.setblocking(False)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        c = _Conn(next(self._ids), sock, addr)
        with self._lock:
            self._conns[c.cid] = c
        self._sel.register(sock, selectors.EVENT_READ, c)

    def _drop(self, c: _Conn) -> None:
        with self._lock:
            known = self._conns.pop(c.cid, None)
        if known is None:
            return
        try:
            self._sel.unregister(c.sock)
        except (KeyError, ValueError):
            pass
        try:
            c.sock.close()
        except OSError:
            pass
        if c.is_ws:
            self._events.put(("close", c.cid))

    def _on_writable(self, c: _Conn) -> None:
        with self._lock:
            data = bytes(c.outbuf[:1 << 20])
        try:
            n = c.sock.send(data)
        except BlockingIOError:
            return
        except OSError:
            self._drop(c)
            return
        with self._lock:
            del c.outbuf[:n]
            done = c.closing and not c.outbuf
        if done:
            self._drop(c)

    def _on_readable(self, c: _Conn) -> None:
        try:
            chunk = c.sock.recv(1 << 18)
        except BlockingIOError:
            return
        except OSError:
            self._drop(c)
            return
        if not chunk:
            self._drop(c)
            return
        c.inbuf += chunk
        try:
            if not c.is_ws:
                self._handle_http(c)
            if c.is_ws:
                self._handle_frames(c)
        except _ProtocolError:
            self._queue(c.cid, encode_frame(OP_CLOSE, struct.pack("!H", 1002)), close_after=True)

    # ---------------------------------------------------------------- HTTP
    def _handle_http(self, c: _Conn) -> None:
        end = c.inbuf.find(b"\r\n\r\n")
        if end < 0:
            if len(c.inbuf) > _MAX_HEADER:
                self._drop(c)
            return
        head = bytes(c.inbuf[:end]).decode("latin-1")
        del c.inbuf[: end + 4]
        lines = head.split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            self._http_reply(c, 400, "text/plain", b"bad request")
            return
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        path = target.split("?", 1)[0]
        if headers.get("upgrade", "").lower() == "websocket" and path in self.ws_paths:
            key = headers.get("sec-websocket-key", "")
            accept = base64.b64encode(hashlib.sha1(key.encode() + _WS_GUID).digest()).decode()
            resp = ("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode()
            with self._lock:
                c.outbuf += resp
                c.is_ws = True
            self._events.put(("open", c.cid))
            self._wake()
            return
        if method not in ("GET", "HEAD"):
            self._http_reply(c, 400, "text/plain", b"only GET is supported")
            return
        result: HttpResponse = None
        if self.http_handler:
            try:
                result = self.http_handler(target, headers)
            except Exception as exc:  # never kill the server thread
                result = (500, "text/plain", f"error: {exc}".encode())
        if result is None:
            result = (404, "text/plain", b"not found")
        status, ctype, body = result
        self._http_reply(c, status, ctype, b"" if method == "HEAD" else body, length=len(body))

    def _http_reply(self, c: _Conn, status: int, ctype: str, body: bytes, length: Optional[int] = None) -> None:
        head = (f"HTTP/1.1 {status} {_REASONS.get(status, 'Status')}\r\n"
                f"Content-Type: {ctype}\r\nContent-Length: {len(body) if length is None else length}\r\n"
                "Cache-Control: no-store\r\nConnection: close\r\n\r\n").encode()
        with self._lock:
            c.outbuf += head + body
            c.closing = True
        self._wake()

    # ----------------------------------------------------------- WebSocket
    def _handle_frames(self, c: _Conn) -> None:
        buf = c.inbuf
        while True:
            if len(buf) < 2:
                return
            b0, b1 = buf[0], buf[1]
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            n = b1 & 0x7F
            pos = 2
            if n == 126:
                if len(buf) < 4:
                    return
                n = struct.unpack_from("!H", buf, 2)[0]
                pos = 4
            elif n == 127:
                if len(buf) < 10:
                    return
                n = struct.unpack_from("!Q", buf, 2)[0]
                pos = 10
            if n > _MAX_MESSAGE:
                raise _ProtocolError("frame too large")
            mask = b""
            if masked:
                if len(buf) < pos + 4:
                    return
                mask = bytes(buf[pos:pos + 4])
                pos += 4
            if len(buf) < pos + n:
                return
            payload = bytes(buf[pos:pos + n])
            del buf[: pos + n]
            if masked:
                payload = _apply_mask(payload, mask)
            self._on_frame(c, fin, opcode, payload)

    def _on_frame(self, c: _Conn, fin: bool, opcode: int, payload: bytes) -> None:
        if opcode == OP_PING:
            self._queue(c.cid, encode_frame(OP_PONG, payload))
        elif opcode == OP_PONG:
            pass
        elif opcode == OP_CLOSE:
            self._queue(c.cid, encode_frame(OP_CLOSE, payload[:2]), close_after=True)
        elif opcode in (OP_TEXT, OP_BINARY):
            if c.frag_op is not None:
                raise _ProtocolError("new message during fragmented message")
            if fin:
                self._deliver(c, opcode, payload)
            else:
                c.frag_op = opcode
                c.frag = bytearray(payload)
        elif opcode == OP_CONT:
            if c.frag_op is None:
                raise _ProtocolError("unexpected continuation")
            c.frag += payload
            if len(c.frag) > _MAX_MESSAGE:
                raise _ProtocolError("message too large")
            if fin:
                op, data = c.frag_op, bytes(c.frag)
                c.frag_op, c.frag = None, bytearray()
                self._deliver(c, op, data)
        else:
            raise _ProtocolError(f"bad opcode {opcode}")

    def _deliver(self, c: _Conn, opcode: int, payload: bytes) -> None:
        if opcode == OP_TEXT:
            try:
                data = payload.decode("utf-8")
            except UnicodeDecodeError:
                raise _ProtocolError("invalid utf-8")
            self._events.put(("message", c.cid, data))
        else:
            self._events.put(("message", c.cid, payload))


class _ProtocolError(Exception):
    pass

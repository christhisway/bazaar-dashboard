"""Copies a Bazaar client's WebSocket frames to the dashboard, from inside the client.

``setup`` installs a ``.pth`` file into the client's Python, which imports
``_bazaar_dashboard_boot`` at every interpreter start.  That checks whether the
process is the configured client (``python -m bazaar`` in the configured
directory) and, only then, calls :func:`activate` here.  Nothing in the
client's code changes.

Once active, the tap wraps ``send`` and ``recv`` on the ``websockets``
library's client connections -- the asyncio, sync and legacy flavours, each as
its module is first imported -- so every binary frame the client sends or
receives is also put on a queue.  A background thread drains the queue to the
dashboard over a local TCP socket.

The rule this file lives by: **the tap must never change what the client
does.**  So:

* Every hook catches everything.  A broken tap loses frames, never trades.
* The queue is bounded and ``put_nowait``; when the dashboard is slow, absent,
  or gone, frames are dropped, not waited for.
* The socket is only ever touched by the background thread.
* Only frame bytes are sent.  Never headers and never the URL, which is where a
  token could be; tokens never appear in Bazaar frames.

Clients may also call :func:`report` to tell the dashboard what the wire does
not carry -- what the agent decided, and why.  It is a no-op when the tap is
not active, so a client can call it unconditionally::

    try:
        from bazaar_dashboard_tap import report
    except ImportError:
        def report(event, **fields): pass

The events the dashboard understands are in its README.

Standard library only: this runs in the client's interpreter, which has
whatever the client installed and nothing else.
"""

import atexit
import importlib.abc
import json
import os
import queue
import socket
import struct
import sys
import threading
import time

#: Record kinds; ``bazaar_dashboard/listener.py`` must agree.
HELLO, SENT, RECEIVED, REPORT = b"H", b">", b"<", b"E"
VERSION = 1

#: Frames waiting for the thread.  A full queue drops new frames.
QUEUE_SIZE = 5000
#: How long to wait before retrying a dashboard that refused the connection.
RETRY_SECONDS = 1.0
#: How long the client's exit may wait for queued frames to go out -- the last
#: state of a run is the one most worth seeing.
FLUSH_SECONDS = 1.0

_sender = None


# --- the public API -----------------------------------------------------------

def report(event: str, **fields) -> None:
    """Tell the dashboard something only the client knows.  Never raises."""
    sender = _sender
    if sender is None:
        return
    try:
        sender.put(REPORT, json.dumps({"event": event, **fields}, default=str).encode())
    except Exception:
        pass


def active() -> bool:
    """Whether frames from this process are going to a dashboard."""
    return _sender is not None


def activate(host: str, port: int, hello: dict) -> None:
    """Start the sender and wrap ``websockets``.  Called once, by the boot module."""
    global _sender
    if _sender is not None:
        return
    _sender = _Sender(host, port, {**hello, "tap_version": VERSION})
    _sender.start()
    atexit.register(_sender.flush, FLUSH_SECONDS)
    _patch_websockets()


# --- the frames -----------------------------------------------------------------

def _frame(kind: bytes, message) -> None:
    sender = _sender
    if sender is None:
        return
    try:
        if isinstance(message, (bytes, bytearray, memoryview)):
            sender.put(kind, bytes(message))
    except Exception:
        pass


def _wrap_async(cls) -> None:
    send, recv = cls.send, cls.recv
    if getattr(send, "_bazaar_tap", False):
        return

    async def tapped_send(self, message, *args, **kwargs):
        result = await send(self, message, *args, **kwargs)
        _frame(SENT, message)
        return result

    async def tapped_recv(self, *args, **kwargs):
        message = await recv(self, *args, **kwargs)
        _frame(RECEIVED, message)
        return message

    tapped_send._bazaar_tap = tapped_recv._bazaar_tap = True
    cls.send, cls.recv = tapped_send, tapped_recv


def _wrap_sync(cls) -> None:
    send, recv = cls.send, cls.recv
    if getattr(send, "_bazaar_tap", False):
        return

    def tapped_send(self, message, *args, **kwargs):
        result = send(self, message, *args, **kwargs)
        _frame(SENT, message)
        return result

    def tapped_recv(self, *args, **kwargs):
        message = recv(self, *args, **kwargs)
        _frame(RECEIVED, message)
        return message

    tapped_send._bazaar_tap = tapped_recv._bazaar_tap = True
    cls.send, cls.recv = tapped_send, tapped_recv


#: Module -> (class name, wrapper).  Client connections only: wrapping the
#: shared base class would tap servers too.  Async iteration (``async for``)
#: calls ``recv``, so it is covered without wrapping ``__aiter__``.
_TARGETS = {
    "websockets.asyncio.client": ("ClientConnection", _wrap_async),
    "websockets.sync.client": ("ClientConnection", _wrap_sync),
    "websockets.legacy.client": ("WebSocketClientProtocol", _wrap_async),
}


def _patch_module(module) -> None:
    try:
        name, wrap = _TARGETS[module.__name__]
        wrap(getattr(module, name))
    except Exception:
        pass


class _PatchOnImport(importlib.abc.MetaPathFinder):
    """Wraps each target module's class as soon as the module finishes loading.

    Waiting for the client's own import, rather than importing ``websockets``
    here, means the tap costs nothing in a process that never opens a socket,
    and never imports the deprecated legacy API into one that does not use it.
    """

    def find_spec(self, fullname, path, target=None):
        if fullname not in _TARGETS:
            return None
        for finder in sys.meta_path:
            if finder is self or not hasattr(finder, "find_spec"):
                continue
            spec = finder.find_spec(fullname, path, target)
            if spec is not None:
                break
        else:
            return None
        loader = spec.loader
        exec_module = getattr(loader, "exec_module", None)
        if exec_module is None:
            return None

        def exec_and_patch(module):
            exec_module(module)
            _patch_module(module)

        try:
            loader.exec_module = exec_and_patch
        except Exception:
            return None
        return spec


def _patch_websockets() -> None:
    for name in _TARGETS:
        if name in sys.modules:
            _patch_module(sys.modules[name])
    sys.meta_path.insert(0, _PatchOnImport())


# --- the sender -------------------------------------------------------------------

class _Sender(threading.Thread):
    """Owns the socket.  Drains the queue to the dashboard, or drops it on the floor."""

    def __init__(self, host: str, port: int, hello: dict):
        super().__init__(name="bazaar-dashboard-tap", daemon=True)
        self.host, self.port = host, port
        self.hello = json.dumps(hello).encode()
        self.queue: queue.Queue = queue.Queue(maxsize=QUEUE_SIZE)
        self.dropped = 0
        self._sock: socket.socket | None = None
        self._next_attempt = 0.0

    def put(self, kind: bytes, payload: bytes) -> None:
        try:
            self.queue.put_nowait(_record(kind, payload))
        except queue.Full:
            self.dropped += 1

    def flush(self, seconds: float) -> None:
        """Give queued frames a moment to go out before the process exits."""
        # Unfinished, not merely queued: the last frame may be mid-send.
        deadline = time.monotonic() + seconds
        while self.queue.unfinished_tasks and time.monotonic() < deadline and self.is_alive():
            time.sleep(0.01)

    def run(self) -> None:
        while True:
            record = self.queue.get()
            try:
                sock = self._connected()
                if sock is None:
                    self.dropped += 1
                    continue
                sock.sendall(record)
            except Exception:
                self._close()
                self.dropped += 1
            finally:
                self.queue.task_done()

    def _connected(self) -> socket.socket | None:
        if self._sock is not None:
            return self._sock
        now = time.monotonic()
        if now < self._next_attempt:
            return None
        try:
            sock = socket.create_connection((self.host, self.port), timeout=0.5)
            # A dashboard that stops reading must not wedge this thread forever;
            # time out, drop the connection, and try again later.
            sock.settimeout(5.0)
            sock.sendall(_record(HELLO, self.hello))
        except OSError:
            self._next_attempt = now + RETRY_SECONDS
            return None
        self._sock = sock
        return sock

    def _close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._next_attempt = time.monotonic() + RETRY_SECONDS


def _record(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 1 + len(payload)) + kind + payload


def hello_for(module: str, argv: list[str]) -> dict:
    """What the dashboard is told about this process.

    The policy is read off a ``--policy`` flag if there is one, as a label for
    the page.  The rest of the command line is not sent: a client could take
    its token as an argument.
    """
    policy = None
    for i, arg in enumerate(argv):
        if arg == "--policy" and i + 1 < len(argv):
            policy = argv[i + 1]
        elif arg.startswith("--policy="):
            policy = arg.split("=", 1)[1]
    return {"pid": os.getpid(), "module": module, "cwd": os.getcwd(), "policy": policy}

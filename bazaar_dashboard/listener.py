"""Where the tap inside the client connects, and what happens to what it sends.

The tap opens one TCP connection per client process and streams records over
it, each a 4-byte big-endian length followed by that many bytes: a one-byte
kind, then the payload.

====  =================  ==================================================
kind  payload            meaning
====  =================  ==================================================
H     JSON object        hello: who the client is; always the first record
>     raw frame          a binary WebSocket frame the client sent
<     raw frame          a binary WebSocket frame the client received
E     JSON object        a report from the client (see ``Dashboard.on_report``)
====  =================  ==================================================

The tap resends its hello whenever it reconnects, so a dashboard started after
the client still learns who it is talking to.

One dashboard shows one client.  The newest hello takes over the page; an
older connection that is still open keeps being logged, but no longer drawn.
That is what a restarted client looks like, and the newest is the one wanted.
"""

import asyncio
import json
import struct

from .hub import Dashboard
from .sessionlog import SessionLog
from .wire import decode

#: These must match ``tap/bazaar_dashboard_tap.py``, which cannot import them:
#: it runs inside the client's interpreter, where this package is not.
HELLO, SENT, RECEIVED, REPORT = b"H", b">", b"<", b"E"
#: A record larger than this is a corrupted stream, not a frame.
MAX_RECORD = 64 * 1024 * 1024


class TapListener:
    """Accepts tap connections and feeds the one that spoke last to the hub."""

    def __init__(self, dashboard: Dashboard, runs_dir, say=print):
        self.dashboard = dashboard
        self.runs_dir = runs_dir
        self._say = say
        self._server: asyncio.Server | None = None
        self._current: object | None = None
        self._writers: set[asyncio.StreamWriter] = set()
        #: Sessions seen, and frames that could not be decoded, for the tests and the log.
        self.sessions: list[SessionLog] = []
        self.bad_frames = 0

    async def start(self, host: str, port: int) -> None:
        """Listen, or raise ``OSError`` if the port is taken.

        No fallback, unlike the page: the tap was told this port at setup and
        has no other way to find the dashboard.
        """
        self._server = await asyncio.start_server(self._handle, host, port)

    async def stop(self) -> None:
        if self._server is not None:
            # wait_closed waits for every connection, so end them first.
            for writer in list(self._writers):
                writer.close()
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def port(self) -> int:
        return self._server.sockets[0].getsockname()[1]

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        token = object()
        log: SessionLog | None = None
        self._writers.add(writer)
        try:
            while True:
                try:
                    header = await reader.readexactly(4)
                except asyncio.IncompleteReadError:
                    break
                (length,) = struct.unpack(">I", header)
                if not 1 <= length <= MAX_RECORD:
                    self._say(f"tap: dropping a connection that sent a {length}-byte record")
                    break
                record = await reader.readexactly(length)
                kind, payload = record[:1], record[1:]
                if kind == HELLO:
                    client = json.loads(payload)
                    if log is None:
                        log = SessionLog(self.runs_dir, client)
                        self.sessions.append(log)
                    self._current = token
                    self.dashboard.begin_session(client)
                    self._say(f"client connected: pid {client.get('pid')}, "
                              f"python -m {client.get('module')} in {client.get('cwd')}")
                    continue
                if log is None:
                    continue  # nothing is attributed to a client that never said hello
                self._record(token, log, kind, payload)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            self._writers.discard(writer)
            writer.close()
            if log is not None:
                try:
                    path = log.close()
                    self._say(f"client disconnected; history: {path}")
                except Exception as exc:
                    self._say(f"client disconnected; history not written ({type(exc).__name__}: {exc})")
                if self._current is token:
                    self.dashboard.end_session()
                    self._say("waiting for the next client ...")

    def _record(self, token, log: SessionLog, kind: bytes, payload: bytes) -> None:
        live = self._current is token
        try:
            if kind == REPORT:
                report = json.loads(payload)
                log.report(report)
                if live:
                    self.dashboard.on_report(report)
            elif kind == RECEIVED:
                message = decode.decode_bytes(payload)
                log.message("received", message)
                if live:
                    self._dispatch(message)
            elif kind == SENT:
                message = decode.decode_client_bytes(payload)
                log.message("sent", message)
                if live:
                    self.dashboard.on_command(message)
        except Exception:
            # A frame that does not decode is not this schema's -- or not a
            # Bazaar frame at all.  Count it and carry on with the next.
            self.bad_frames += 1

    def _dispatch(self, message) -> None:
        kind = message.which()
        if kind == "state":
            self.dashboard.on_state(message.state)
        elif kind == "result":
            self.dashboard.on_result(message.result)
        elif kind == "protocol_error":
            self.dashboard.on_protocol_error(message.protocol_error)

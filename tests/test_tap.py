"""The tap and the listener: frames out of a client process and onto the page.

The end-to-end tests run a real client in a subprocess -- real ``websockets``,
real frames from ``fixtures/`` -- because the tap wraps library classes for the
whole interpreter, which a test must not do to pytest's own.
"""

import asyncio
import json
import os
import subprocess
import sys
import time

from bazaar_dashboard import settings
from bazaar_dashboard.hub import Dashboard
from bazaar_dashboard.listener import HELLO, RECEIVED, REPORT, SENT, TapListener
from bazaar_dashboard.wire.generated import bazaar_pb2 as pb

import _bazaar_dashboard_boot as boot
import bazaar_dashboard_tap as tap

from .test_dashboard import FIXTURES

STATE = (FIXTURES / "04_state.bin").read_bytes()
RESULT = (FIXTURES / "03_result.bin").read_bytes()  # answers student-advertise-1


def advertise_frame(request_id="student-advertise-1") -> bytes:
    message = pb.ClientMessage()
    out = message.advertise
    out.type, out.protocol_version, out.run_id, out.request_id = 1, "2.0", "sample-7b99d56793b886f0", request_id
    out.body.selling.SetInParent()
    out.body.selling.items.append(1)
    out.body.seeking.SetInParent()
    out.body.expires_tick = 5
    return message.SerializeToString()


# --- the record format ------------------------------------------------------------

def test_the_tap_and_the_listener_agree_on_record_kinds():
    assert (tap.HELLO, tap.SENT, tap.RECEIVED, tap.REPORT) == (HELLO, SENT, RECEIVED, REPORT)


def test_the_hello_names_the_policy_but_not_the_rest_of_the_command_line():
    hello = tap.hello_for("bazaar", ["python", "-m", "bazaar", "--policy", "scrooge", "--token", "secret"])
    assert hello["policy"] == "scrooge" and hello["module"] == "bazaar"
    assert "secret" not in json.dumps(hello)
    assert tap.hello_for("bazaar", ["python", "-m", "bazaar", "--policy=jesus"])["policy"] == "jesus"


def test_report_is_a_quiet_no_op_when_the_tap_is_not_active():
    assert not tap.active()
    tap.report("decided", actions=[])


# --- which processes are tapped -------------------------------------------------------

def test_the_module_is_read_off_the_command_line_after_any_interpreter_flags():
    assert boot.module_of(["python", "-m", "bazaar", "--policy", "x"]) == "bazaar"
    assert boot.module_of(["python", "-u", "-X", "dev", "-m", "bazaar"]) == "bazaar"
    assert boot.module_of(["python", "-mbazaar"]) == "bazaar"
    assert boot.module_of(["python", "script.py", "-m", "bazaar"]) is None
    assert boot.module_of(["python", "-c", "pass"]) is None
    assert boot.module_of(["python"]) is None


def test_only_the_configured_module_in_the_configured_directory_matches(tmp_path):
    client = tmp_path / "client"
    (client / "sub").mkdir(parents=True)
    config = {"clients": [{"dir": str(client), "module": "bazaar"}]}
    assert boot.match(config, ["python", "-m", "bazaar"], str(client)) is not None
    assert boot.match(config, ["python", "-m", "bazaar"], str(client / "sub")) is not None
    assert boot.match(config, ["python", "-m", "bazaar.sandbox"], str(client)) is None
    assert boot.match(config, ["python", "-m", "pytest"], str(client)) is None
    assert boot.match(config, ["python", "-m", "bazaar"], str(tmp_path)) is None
    assert boot.match(config, ["python", "-m", "bazaar"], str(tmp_path / "client-other")) is None


# --- end to end ---------------------------------------------------------------------------

CLIENT = """
import asyncio, sys
import bazaar_dashboard_tap as tap

port, state, result, command = int(sys.argv[1]), *(bytes.fromhex(a) for a in sys.argv[2:5])
tap.activate("127.0.0.1", port, {"pid": 1234, "module": "bazaar", "cwd": "/client", "policy": "scrooge"})

from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

async def game(ws):
    # The game's side is a server connection: it must not be tapped as well.
    await ws.send(state)
    await ws.recv()
    await ws.send(result)
    await ws.wait_closed()

async def main():
    async with serve(game, "127.0.0.1", 0) as server:
        url = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}"
        async with connect(url) as ws:
            await ws.recv()
            tap.report("decided", tick=0, world_version=1, actions=[
                {"key": 1, "description": "advertise water", "reason": "stay visible", "score": 1.5}])
            tap.report("sent", key=1, request_id="student-advertise-1")
            await ws.send(command)
            await ws.recv()

asyncio.run(main())
"""


def run_client(port: int, timeout: float = 20) -> subprocess.CompletedProcess:
    env = {**os.environ, "PYTHONPATH": str(settings.TAP_DIR)}
    return subprocess.run(
        [sys.executable, "-c", CLIENT, str(port), STATE.hex(), RESULT.hex(), advertise_frame().hex()],
        env=env, capture_output=True, text=True, timeout=timeout,
    )


def test_a_client_process_is_followed_frame_by_frame_and_logged(tmp_path):
    said = []

    async def scenario():
        dashboard = Dashboard()
        listener = TapListener(dashboard, tmp_path, say=said.append)
        await listener.start("127.0.0.1", 0)
        try:
            done = await asyncio.to_thread(run_client, listener.port)
            assert done.returncode == 0, done.stderr
            for _ in range(200):  # the listener notices the exit a moment later
                if listener.sessions and dashboard.client and not dashboard.client["connected"]:
                    break
                await asyncio.sleep(0.02)
        finally:
            await listener.stop()
        return dashboard, listener

    dashboard, listener = asyncio.run(scenario())
    assert listener.bad_frames == 0 and dashboard.errors == 0
    assert dashboard.agent == "scrooge"
    assert dashboard.state.self_station_id == "P01"
    [record] = dashboard.actions
    assert (record.status, record.request_id, record.reason, record.score) == ("ok", "student-advertise-1",
                                                                               "stay visible", 1.5)
    assert record.terms["selling"] == ["water"]  # filled in from the frame the report did not carry
    assert dashboard.client["connected"] is False

    [session] = listener.sessions
    rows = [json.loads(line) for line in (session.directory / "messages.jsonl").read_text().splitlines()]
    assert [(r["direction"], r["kind"]) for r in rows] == [("received", "state"), ("sent", "advertise"),
                                                           ("received", "result")]
    assert (session.directory / "history.json").exists()
    assert any(line.startswith("client connected: pid 1234") for line in said)


def test_without_a_dashboard_the_client_runs_as_if_the_tap_were_not_there(tmp_path):
    async def free_port() -> int:
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        server.close()
        await server.wait_closed()
        return port

    started = time.monotonic()
    done = run_client(asyncio.run(free_port()))
    assert done.returncode == 0, done.stderr
    assert done.stderr == ""
    assert time.monotonic() - started < 10


def test_a_newer_client_takes_over_the_page_and_the_older_is_only_logged(tmp_path):
    async def scenario():
        dashboard = Dashboard()
        listener = TapListener(dashboard, tmp_path, say=lambda _: None)
        await listener.start("127.0.0.1", 0)

        async def connect(pid):
            reader, writer = await asyncio.open_connection("127.0.0.1", listener.port)
            hello = json.dumps({"pid": pid, "module": "bazaar", "cwd": "/c", "policy": f"p{pid}"}).encode()
            writer.write(tap._record(HELLO, hello))
            await writer.drain()
            return writer

        try:
            old = await connect(1)
            await asyncio.sleep(0.05)
            new = await connect(2)
            await asyncio.sleep(0.05)
            old.write(tap._record(RECEIVED, STATE))
            await old.drain()
            await asyncio.sleep(0.05)
            assert dashboard.agent == "p2" and dashboard.state is None
            for writer in (old, new):
                writer.close()
            await asyncio.sleep(0.05)
        finally:
            await listener.stop()
        return listener

    listener = asyncio.run(scenario())
    first = listener.sessions[0].directory / "messages.jsonl"
    assert len(first.read_text().splitlines()) == 1  # logged, though not drawn


def test_a_frame_that_is_not_bazaar_is_counted_and_skipped(tmp_path):
    async def scenario():
        dashboard = Dashboard()
        listener = TapListener(dashboard, tmp_path, say=lambda _: None)
        await listener.start("127.0.0.1", 0)
        try:
            _, writer = await asyncio.open_connection("127.0.0.1", listener.port)
            writer.write(tap._record(HELLO, b'{"pid": 1}') + tap._record(RECEIVED, b"\xff\xfe not protobuf")
                         + tap._record(RECEIVED, STATE))
            await writer.drain()
            await asyncio.sleep(0.1)
            writer.close()
        finally:
            await listener.stop()
        return dashboard, listener

    dashboard, listener = asyncio.run(scenario())
    assert listener.bad_frames == 1
    assert dashboard.state is not None

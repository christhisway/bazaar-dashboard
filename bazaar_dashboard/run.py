"""``start``: the page, the tap listener, and waiting.

Kept apart from ``__main__`` because it needs the generated bindings, which a
fresh checkout does not have until ``setup`` has run.
"""

import asyncio
import signal

from . import install, settings


async def serve(port: int) -> int:
    config = install.read_config()
    if not config.get("clients"):
        raise install.SetupError("no client is connected yet; run ./setup.sh <client-dir> first")
    if not install.bindings_ready():
        raise install.SetupError("the protobuf bindings are missing; run ./setup.sh <client-dir> again")

    from .hub import Dashboard
    from .listener import TapListener
    from .server import DashboardServer

    dashboard = Dashboard()
    page = DashboardServer(dashboard)
    listener = TapListener(dashboard, settings.RUNS_DIR)
    host, tap_port = config.get("host", settings.DASHBOARD.host), int(config["port"])
    try:
        await listener.start(host, tap_port)
    except OSError as exc:
        raise install.SetupError(f"port {tap_port} is taken ({exc}); is another dashboard already running?")
    url = await page.start(host, port)

    print(f"dashboard: {url}")
    for client in config["clients"]:
        print(f"waiting for the client: python -m {client['module']} in {client['dir']}")
    # Ctrl+C cancels this coroutine; a plain kill should end it as cleanly, so
    # a session still open gets its history written.
    stop = asyncio.Event()
    try:
        asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, stop.set)
    except (NotImplementedError, RuntimeError):
        pass  # not on this platform; Ctrl+C still works
    try:
        await stop.wait()
    finally:
        await listener.stop()
        await page.stop()
    return 0

"""Decides, at interpreter start, whether this process is a client to tap.

The ``.pth`` file ``setup`` installs runs :func:`boot` in *every* process the
client's interpreter starts -- pip, pytest, the editor's language server -- so
this must be cheap and silent.  It looks only at ``sys.orig_argv`` (the command
line as typed, which is already set while ``.pth`` files run) and the working
directory, and imports nothing more unless both match a client named in
``config.json`` (or the file ``$BAZAAR_DASHBOARD_TAP_CONFIG`` names):

* the command is ``python -m <module>``, for exactly the configured module --
  ``python -m bazaar.sandbox`` is not ``python -m bazaar``;
* the working directory is the configured client directory, or inside it.

Any error ends the check quietly.  A broken boot must cost the client nothing.
"""

import os
import sys

#: Overridable for tests, and for anyone keeping two configs side by side.
CONFIG_ENV = "BAZAAR_DASHBOARD_TAP_CONFIG"


def config_path() -> str:
    return os.environ.get(CONFIG_ENV) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


def module_of(argv) -> str | None:
    """The ``-m`` module in a command line, or ``None``.

    Handles ``-m mod`` and ``-mmod``, after any interpreter flags, including
    the ones that take a separate value (``-X dev``, ``-W error``).
    """
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "-m":
            return argv[i + 1] if i + 1 < len(argv) else None
        if arg.startswith("-m") and len(arg) > 2:
            return arg[2:]
        if arg == "-c" or not arg.startswith("-"):
            return None  # a script or -c: not a module run
        i += 2 if arg in _FLAGS_WITH_VALUES else 1
    return None


_FLAGS_WITH_VALUES = ("-X", "-W", "--check-hash-based-pycs")


def _inside(path: str, root: str) -> bool:
    path, root = os.path.realpath(path), os.path.realpath(root)
    return path == root or path.startswith(root.rstrip(os.sep) + os.sep)


def match(config: dict, argv, cwd: str) -> dict | None:
    """The configured client this command line is, or ``None``."""
    module = module_of(argv)
    if module is None:
        return None
    for client in config.get("clients", []):
        if client.get("module") == module and _inside(cwd, client.get("dir", "")):
            return client
    return None


def boot() -> None:
    try:
        argv = getattr(sys, "orig_argv", None) or sys.argv
        if module_of(argv) is None:
            return  # the common case, decided without touching the disk
        import json

        with open(config_path(), encoding="utf-8") as handle:
            config = json.load(handle)
        client = match(config, argv, os.getcwd())
        if client is None:
            return
        import bazaar_dashboard_tap as tap

        tap.activate(config.get("host", "127.0.0.1"), int(config["port"]),
                     tap.hello_for(client["module"], list(argv)))
    except Exception:
        pass

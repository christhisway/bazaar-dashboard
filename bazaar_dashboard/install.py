"""Connects the dashboard to a client: ``setup`` and ``uninstall``.

Setup does four things, each safe to repeat:

1. Makes sure the dashboard's own dependencies are importable, installing
   ``requirements.txt`` with pip if not.
2. Generates the protobuf bindings from ``proto/bazaar.proto``, unless they
   are already newer than it.
3. Records the client -- its directory and the module its start command runs --
   in ``tap/config.json``, with the port the tap should send to.
4. Drops ``bazaar_dashboard_tap.pth`` into the client interpreter's
   site-packages: its own if writable (a virtualenv), otherwise the user's.
   That file puts ``tap/`` on the path and runs the boot check at every start.

It then checks its own work in the client's interpreter, from the client's
directory, so a setup that cannot work says so now rather than at the first run.

Everything that runs in the *client's* interpreter is done through a
subprocess: that interpreter may be a different Python from the one running
setup, and is the only one whose site-packages count.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import settings


class SetupError(Exception):
    """Something setup cannot fix by itself; the message says what to do."""


# --- the dashboard's side -------------------------------------------------------

REQUIRED = {"websockets": "websockets", "google.protobuf": "protobuf", "grpc_tools": "grpcio-tools"}


def missing_requirements() -> list[str]:
    missing = []
    for module, package in REQUIRED.items():
        try:
            __import__(module)
        except ImportError:
            missing.append(package)
    return missing


def ensure_requirements(install: bool, say=print) -> None:
    missing = missing_requirements()
    if not missing:
        return
    if not install:
        raise SetupError(f"missing {', '.join(missing)}; run: {sys.executable} -m pip install -r requirements.txt")
    say(f"installing {', '.join(missing)} ...")
    subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(settings.ROOT / "requirements.txt")],
                   check=True)


def generate_bindings(say=print) -> None:
    """``proto/bazaar.proto`` -> ``bazaar_dashboard/wire/generated/bazaar_pb2.py``, if out of date."""
    target = settings.GENERATED / "bazaar_pb2.py"
    if target.exists() and target.stat().st_mtime >= settings.PROTO.stat().st_mtime:
        return
    settings.GENERATED.mkdir(parents=True, exist_ok=True)
    (settings.GENERATED / "__init__.py").touch()
    subprocess.run(
        [sys.executable, "-m", "grpc_tools.protoc", f"-I{settings.PROTO.parent}",
         f"--python_out={settings.GENERATED}", f"--pyi_out={settings.GENERATED}", str(settings.PROTO)],
        check=True,
    )
    say(f"bindings: {target}")


def bindings_ready() -> bool:
    return (settings.GENERATED / "bazaar_pb2.py").exists()


# --- the client's side ------------------------------------------------------------

def check_client_dir(client_dir: Path, module: str) -> None:
    """The start command is ``python -m <module>`` run from ``client_dir``; can it be?"""
    if not client_dir.is_dir():
        raise SetupError(f"no such directory: {client_dir}")
    base = client_dir.joinpath(*module.split("."))
    if not ((base / "__main__.py").exists() or base.with_suffix(".py").exists()):
        raise SetupError(
            f"`python -m {module}` would not find a module in {client_dir} "
            f"(looked for {base / '__main__.py'}); pass --module if the client runs another one"
        )


def find_client_python(client_dir: Path) -> str:
    """The interpreter that ``python -m ...`` resolves to for this client.

    A virtualenv in the client's directory wins, since that is what an
    activated shell there would run; otherwise whatever ``python`` is on PATH.
    """
    for venv in (".venv", "venv", "env"):
        candidate = client_dir / venv / "bin" / "python"
        if candidate.exists():
            return str(candidate)
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            return found
    raise SetupError("no python on PATH; pass --python with the interpreter the client runs under")


_SITE_PROBE = """
import json, site, sys, sysconfig
print(json.dumps({
    "purelib": sysconfig.get_paths()["purelib"],
    "usersite": site.getusersitepackages(),
    "user_enabled": bool(site.ENABLE_USER_SITE),
    "version": sys.version.split()[0],
}))
"""


def _run_python(python: str, code: str, cwd: Path | None = None) -> str:
    done = subprocess.run([python, "-c", code], cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0:
        raise SetupError(f"{python} failed: {done.stderr.strip() or done.stdout.strip()}")
    return done.stdout


def site_dir_for(python: str) -> Path:
    """Where a ``.pth`` file will be read by ``python`` at every start."""
    probe = json.loads(_run_python(python, _SITE_PROBE))
    purelib = Path(probe["purelib"])
    if purelib.is_dir() and os.access(purelib, os.W_OK):
        return purelib
    if probe["user_enabled"]:
        usersite = Path(probe["usersite"])
        usersite.mkdir(parents=True, exist_ok=True)
        return usersite
    raise SetupError(
        f"cannot install into {python}: {purelib} is not writable and user site-packages is disabled. "
        "Use a virtualenv for the client, or run setup with permission to write there."
    )


def pth_contents(tap_dir: Path) -> str:
    """Two lines: put ``tap/`` on the path, then run the boot check.

    The second line is wrapped so that if the dashboard is moved or deleted,
    the client's interpreter starts as it always did instead of printing an
    import error at every launch.  ``site`` runs a ``.pth`` line starting with
    ``import`` through ``exec``, which is why the ``try`` has to be a string.
    """
    guarded = "try:\\n import _bazaar_dashboard_boot as b\\n b.boot()\\nexcept Exception:\\n pass"
    return f"{tap_dir}\nimport sys; exec(\"{guarded}\")\n"


# --- config ---------------------------------------------------------------------------

def read_config() -> dict:
    try:
        return json.loads(settings.TAP_CONFIG.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def write_config(config: dict) -> None:
    settings.TAP_CONFIG.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")


def with_client(config: dict, client_dir: Path, module: str, python: str, pth: Path, port: int) -> dict:
    """``config`` with this client added, replacing any earlier entry for the same directory."""
    clients = [c for c in config.get("clients", []) if c.get("dir") != str(client_dir)]
    clients.append({"dir": str(client_dir), "module": module, "python": python})
    pths = sorted(set(config.get("pth", [])) | {str(pth)})
    return {"host": settings.DASHBOARD.host, "port": port, "clients": clients, "pth": pths}


# --- the commands -------------------------------------------------------------------------

_VERIFY = """
import json, os, sys
import bazaar_dashboard_tap, _bazaar_dashboard_boot as boot
config = json.load(open(boot.config_path()))
print(json.dumps(boot.match(config, [sys.executable, "-m", {module!r}], os.getcwd()) is not None))
"""


def setup(client_dir: str, *, module: str = settings.DEFAULT_MODULE, python: str | None = None,
          tap_port: int = settings.DASHBOARD.tap_port, install: bool = True, site_dir: str | None = None,
          say=print) -> None:
    client = Path(client_dir).expanduser().resolve()
    check_client_dir(client, module)

    ensure_requirements(install, say)
    generate_bindings(say)

    python = python or find_client_python(client)
    say(f"client: python -m {module} in {client}")
    say(f"client python: {python}")
    pth = (Path(site_dir) if site_dir else site_dir_for(python)) / settings.PTH_NAME

    write_config(with_client(read_config(), client, module, python, pth, tap_port))
    pth.write_text(pth_contents(settings.TAP_DIR), encoding="utf-8")
    say(f"hook: {pth}")

    try:
        _run_python(python, "import websockets", cwd=client)
    except SetupError:
        say(f"warning: {python} cannot import websockets, so there is nothing for the tap to watch yet")

    if json.loads(_run_python(python, _VERIFY.format(module=module), cwd=client)) is not True:
        raise SetupError(f"the hook is installed but did not recognise `python -m {module}` in {client}")
    say("check: the client's python loads the tap and recognises the client")


def uninstall(say=print) -> None:
    """Remove every hook setup installed, and forget every client."""
    config = read_config()
    for path in config.get("pth", []):
        try:
            Path(path).unlink()
            say(f"removed {path}")
        except FileNotFoundError:
            pass
    if settings.TAP_CONFIG.exists():
        settings.TAP_CONFIG.unlink()
    say("the dashboard is no longer connected to any client")

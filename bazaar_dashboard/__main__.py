"""The dashboard's command line.

    python -m bazaar_dashboard setup CLIENT_DIR [--module M] [--python PY] [--tap-port N]
    python -m bazaar_dashboard start [--port N]
    python -m bazaar_dashboard uninstall
    python -m bazaar_dashboard history [PATH ...]

``setup.sh`` and ``start.sh`` beside this package run the first two.  The order
on a fresh machine is setup, then start, then the client's own start command;
after that, start and the client are all it takes.
"""

import argparse
import asyncio
import sys

from . import install, settings


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m bazaar_dashboard", description="Live dashboard for a Bazaar client.")
    commands = parser.add_subparsers(dest="command", required=True)

    setup = commands.add_parser("setup", help="connect the dashboard to a client (run once)")
    setup.add_argument("client_dir", help="the client's repository: where its start command is run from")
    setup.add_argument("--module", default=settings.DEFAULT_MODULE,
                       help=f"the module the client runs with `python -m` (default {settings.DEFAULT_MODULE})")
    setup.add_argument("--python", help="the interpreter the client runs under (default: the client's .venv, else python on PATH)")
    setup.add_argument("--tap-port", type=int, default=settings.DASHBOARD.tap_port,
                       help=f"local port the client sends frames to (default {settings.DASHBOARD.tap_port})")
    setup.add_argument("--site-dir", help="where to put the hook (default: the client python's site-packages, else its user site)")
    setup.add_argument("--no-install", action="store_true", help="do not pip install missing dependencies")

    start = commands.add_parser("start", help="serve the page and wait for the client")
    start.add_argument("--port", type=int, default=settings.DASHBOARD.port,
                       help=f"port for the page (default {settings.DASHBOARD.port}; taken -> any free port)")

    commands.add_parser("uninstall", help="disconnect the dashboard from every client")

    history = commands.add_parser("history", help="(re)write history.json for logged sessions")
    history.add_argument("paths", nargs="*", default=[str(settings.RUNS_DIR)],
                         help="session directories, or directories of them (default: runs/)")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    try:
        if args.command == "setup":
            install.setup(args.client_dir, module=args.module, python=args.python,
                          tap_port=args.tap_port, install=not args.no_install, site_dir=args.site_dir)
            print("\nconnected. Next: ./start.sh, then start the client as usual.")
            return 0
        if args.command == "uninstall":
            install.uninstall()
            return 0
        if args.command == "history":
            return history(args.paths)
        from .run import serve

        return asyncio.run(serve(args.port))
    except install.SetupError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0


def history(paths: list[str]) -> int:
    from pathlib import Path

    from .history import run_dirs, write_summary

    failed = done = 0
    for path in paths:
        if not Path(path).is_dir():
            print(f"not a directory: {path}")
            failed += 1
            continue
        for directory in run_dirs(path):
            try:
                print(write_summary(directory))
                done += 1
            except Exception as exc:
                print(f"{directory}: {type(exc).__name__}: {exc}")
                failed += 1
    print(f"{done} summarised, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())

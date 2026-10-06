# Bazaar dashboard

A live browser page for a Bazaar trading client. It shows your planet's reserves
and how long they last, open offers, recent trades, a map of the other stations,
what your agent is after, and how every command ended. It works with any Python
client that talks to the game through the `websockets` library and is started
with `python -m <module>`. Your client's code doesn't need to change.

## Quick start

Run these three commands, in this order:

```bash
# 1. Once per machine: connect the dashboard to your client's repository.
./setup.sh /path/to/your/client

# 2. Each session: serve the page and wait for the client.
./start.sh
#    -> dashboard: http://127.0.0.1:8765/

# 3. Start your client as you always do, from its repository.
cd /path/to/your/client && python -m bazaar --policy whatever
```

Open the printed URL. The page fills in as soon as the client connects, and it
keeps the last picture up after the client stops, until the next one starts.
Steps 2 and 3 can run in either order, because a dashboard started mid-run picks
the client up within a second.

`setup.sh` installs any missing dependencies (`websockets`, `protobuf`,
`grpcio-tools`) with pip, generates the protobuf bindings, installs the hook into
your client's Python, and then checks that the hook works. If any step fails, it
tells you what to do.

## Requirements

- **Python 3.10 or newer**, for the dashboard and for the client.
- **A client that uses the `websockets` library.** Its asyncio, sync and legacy
  client APIs are all covered.
- **A client started as `python -m <module>`** from its repository. The default
  module is `bazaar`; pass `--module` to setup for another.
- **The same machine or container as the client.** The hook lives in the
  client's Python and talks to the dashboard over `127.0.0.1`.
- **A Unix-like shell** for `setup.sh` and `start.sh`. On Windows, run
  `python -m bazaar_dashboard setup …` and `python -m bazaar_dashboard start`
  directly.

### When the client runs in a devcontainer

The dashboard has to be visible inside the container, because that is where the
client's Python runs. Keep this repository beside the client's on your machine,
and bind-mount it in the client's `.devcontainer/devcontainer.json`:

```jsonc
"mounts": [
    "source=${localWorkspaceFolder}/../bazaar-dashboard,target=/workspaces/bazaar-dashboard,type=bind"
]
```

Rebuild the container, then run setup inside it:
`/workspaces/bazaar-dashboard/setup.sh /workspaces/<client>`. A rebuild wipes the
container's site-packages, so setup must run again after every rebuild. A
`postCreateCommand` can run it for you. The folder has to exist on your machine
before the rebuild; Docker refuses to start a container whose bind-mount source
is missing.

## How it works

```
 your client process                                   ./start.sh
┌──────────────────────────────┐   local TCP :8766   ┌─────────────────────────┐
│ python -m bazaar             │ ──────────────────▶ │ listener → hub → page   │ ──▶ browser :8765
│   websockets ⇄ game server   │   every frame,      │ runs/<session>/…        │
│   └ tap (copies each frame)  │   plus reports      └─────────────────────────┘
└──────────────────────────────┘
```

- **The hook.** Setup puts `bazaar_dashboard_tap.pth` into your client
  interpreter's site-packages. That is the virtualenv's own if it is writable,
  otherwise your user site-packages. Python runs every `.pth` file at startup,
  so this one runs a quick check in every process that interpreter starts.
- **The check.** The tap switches on only when the command is exactly
  `python -m <module>` and the working directory is inside the client
  directory you gave setup. Everything else is left alone, including
  `python -m bazaar.sandbox`, pytest, pip and your editor.
- **The tap.** When it is switched on, it wraps `send` and `recv` on the
  `websockets` client connection classes (asyncio, sync and legacy). Each binary
  frame goes onto a bounded queue, and a background thread sends the queue to
  the dashboard.
- **The dashboard.** It decodes the frames with its own copy of
  `bazaar.proto`, follows each command from sent to answered by request ID, and
  draws the page.

### The tap never changes what your client does

- **Errors.** Every hook catches its own errors.
- **Frames are dropped, never waited for.** If the dashboard is not running,
  is slow, or crashes, frames are dropped. Your client neither waits nor
  fails. A tap with no dashboard to send to costs about nothing, and stopping
  the dashboard mid-run does not affect the run.
- **Only frame bytes are sent.** Connection headers and the URL are never sent,
  and those are the only places a token could be. Of the command line, only the
  `--policy` value is read, to label the page.
- **Measured cost.** On a 200 ms-tick run the client's event-loop lag was the
  same with and without the tap. The dashboard received all 412 frames and
  dropped none.

## Showing your agent's reasoning (optional)

On its own, the dashboard sees what the wire carries: commands and their
results. It cannot see why your agent chose them. To add reasons, scores, and
actions your agent decided on but never sent, call `report` from your client:

```python
try:
    from bazaar_dashboard_tap import report      # importable once setup has run
except ImportError:
    def report(event, **fields): pass            # no dashboard: do nothing

report("agent", name="my-agent")
report("decided", tick=state.tick, world_version=state.world_version, actions=[
    {"key": 1, "description": "offer water for food", "reason": "food is low", "score": 2.0,
     "command": {"kind": "offer", "recipient_id": "P02", "give": [2, 0, 0], "receive": [0, 1, 0],
                 "expires_tick": 12}},
])
report("sent", key=1, request_id="my-request-17")        # just before sending it
report("rejected", key=2, detail="why it was not sent")  # decided, but you refused to send it
report("errored", request_id="my-request-17", detail="TimeoutError")  # no answer ever came
```

`report` never raises and does nothing when the tap is off, so you can call it
unconditionally. Results need no report, because they arrive on the wire.

| Event | Fields | Effect |
| --- | --- | --- |
| `agent` | `name` | The agent's name on the page. Without it, the `--policy` flag is used. |
| `decided` | `tick`, `world_version`, `actions` | Adds each action as *queued*. Anything still queued from an earlier decision becomes *skipped*. |
| `sent` | `key`, `request_id` | Links a decided action to the command on the wire. |
| `rejected` | `key`, `detail` | The action was never sent, and this is why. |
| `errored` | `request_id`, `detail` | The command got no answer, for a reason only the client knows. |

Each entry in `actions` has a `key` that you choose and that is unique within
the process. The `description`, `reason`, `score` and `command` fields are all
optional. A `command` gives the action's terms, using the same field names as
`bazaar.proto`:

- `{"kind": "advertise", "selling": ["water"], "seeking": ["food"], "expires_tick": N}`
- `{"kind": "offer", "recipient_id": "P02", "give": [w, f, c], "receive": [w, f, c], "expires_tick": N}`
- `{"kind": "accept", "offer_id": "..."}`
- `{"kind": "withdraw", "object_id": "..."}`

Resources may be written `water` or `RESOURCE_WATER`. Bundles may be
`[water, food, components]` or `{"water": 1, ...}`. The dashboard ignores
events it does not recognise.

## Commands

| Command | What it does |
| --- | --- |
| `./setup.sh CLIENT_DIR` | Connects the dashboard to a client. Run it again to re-check, or after moving either directory. |
| `./setup.sh CLIENT_DIR --module mybot` | For a client started with `python -m mybot`. The default is `bazaar`. |
| `./setup.sh CLIENT_DIR --python /path/to/python` | Names the client's interpreter. The default is `CLIENT_DIR/.venv` (or `venv`, or `env`), then `python` on PATH. |
| `./setup.sh CLIENT_DIR --tap-port N` | Uses another local port for frames. The default is 8766. |
| `./start.sh` | Serves the page on 8765, or on a free port if 8765 is taken, and waits for the client. Stop it with Ctrl+C. |
| `./start.sh --port N` | Serves the page on port N instead. |
| `python -m bazaar_dashboard uninstall` | Removes the hook from the client's Python and forgets every client. |
| `python -m bazaar_dashboard history [PATH ...]` | Rewrites `history.json` for logged sessions. The default is `runs/`. |

`setup.sh` and `start.sh` use `python3`, or whatever `$PYTHON` names. Inside a
devcontainer, VS Code forwards the page's port to your machine. On a remote
machine, use `ssh -L 8765:127.0.0.1:8765 <host>`. The page and the tap port both
bind to `127.0.0.1` only, because the page shows your stock and plans.

Thresholds and history lengths are `DashboardSettings` in
`bazaar_dashboard/settings.py`. `http://127.0.0.1:8765/view.json` serves the same
data as JSON, for `curl`. Skins are chosen with `?skin=<id>` or from the header
menu.

## Session logs and history

The dashboard keeps its own record of each client session it sees, in
`runs/<date>-<time>-<pid>/`:

| File | Holds |
| --- | --- |
| `messages.jsonl` | Every frame, decoded, one JSON object per line: `direction`, `kind`, `message`. |
| `reports.jsonl` | Every report the client sent. |
| `session.json` | Who the client was: pid, module, directory and policy. |
| `history.json` | A summary written when the session ends, however it ends. It is for the history tab, which does not exist yet. |

`history.json` is `{schema_version, session, context, log, runs: [...]}`, with
one entry in `runs` per run ID seen. Each entry has:

| Key | What it holds |
| --- | --- |
| `result` | `status` (`finished`, `aborted` or `interrupted`), `progress`, the first and last tick, whether our station failed, and the server's outcome. |
| `health` | Final, minimum and maximum health, and shortage counts. |
| `totals` | Produced, consumed, unmet, imported and exported, per resource. |
| `trades` | Counts, what we gave and got, and the same per counterparty. |
| `offers` | How the offers we made ended, and the acceptance rate. |
| `commands` | Sent and retried counts, how each kind ended (`ok`, `failed`, `errored`, `unanswered`), and failure codes. |
| `series` | One shared `tick` axis with health, inventory, unmet upkeep, trades and commands per tick. |

The log has no timestamps, so everything is measured in ticks. `trades` and
`totals` are the run's whole ledger, so a client restarted mid-game counts the
earlier session's trades too. `commands` and `series` cover only what that
session saw. `history` reads any `messages.jsonl` in this shape, including one a
client wrote itself.

## When something is off

| Symptom | Likely cause |
| --- | --- |
| The page says *Waiting for the client* while the client is running | The client was started some other way: from another directory, as `python bazaar/__main__.py`, under another interpreter, or with `python -I` or `-s` (which skip `.pth` files). Run `./setup.sh` again with the right `--python` or `--module`. |
| Setup fails with *not writable and user site-packages is disabled* | The client's Python is a system install in a virtualenv that hides user site-packages. Run setup from a shell that can write to it, or give the client its own virtualenv. |
| `./start.sh` fails with *port … is taken* | Another dashboard is already running, or something else holds port 8766. Re-run setup with `--tap-port`, then start again. |
| Commands show no reason or score | The client does not call `report`. That is expected, and everything else still works. |
| The page shows another planet than expected | The newest client to connect wins the page. Older connections are still logged. |

To remove the dashboard completely, run `python -m bazaar_dashboard uninstall`,
then delete this directory. If you delete the directory first, the leftover
`.pth` file does nothing and prints nothing. Remove it whenever you like; setup
printed its path.

## Layout

| Path | Job |
| --- | --- |
| `setup.sh`, `start.sh` | The two commands. |
| `tap/` | Runs inside the client. `_bazaar_dashboard_boot.py` decides whether this process is the client; `bazaar_dashboard_tap.py` wraps `websockets`, sends frames and provides `report`. Standard library only. `config.json` is written by setup. |
| `bazaar_dashboard/install.py` | `setup` and `uninstall`. |
| `bazaar_dashboard/run.py` | `start`: the page server plus the tap listener. |
| `bazaar_dashboard/listener.py` | Receives the tap's stream; feeds the newest client to the hub and logs every client. |
| `bazaar_dashboard/hub.py` | `Dashboard`: the memory of one session, merging wire frames with reports by request ID. Its hooks swallow their own errors. |
| `bazaar_dashboard/view.py` | Pure: one state plus the hub's memory in, the page's JSON out. |
| `bazaar_dashboard/server.py` | Serves the page, the skins, `/view.json` and the live feed on one port. |
| `bazaar_dashboard/sessionlog.py`, `history/` | `runs/<session>/` and its `history.json`. |
| `bazaar_dashboard/wire/` | The dashboard's own copy of the client's `model` and `decode`, plus bindings generated from `proto/bazaar.proto`. |
| `bazaar_dashboard/static/` | `index.html`, and one `<id>.css` + `<id>.js` pair per skin in `skins/`. A skin's CSS is scoped to `:root[data-skin="<id>"]`. |
| `docs/design/` | The design mock-ups the skins came from. |

## Tests

```bash
pip install pytest
python -m pytest -q        # from this directory
```

The tap tests run a real client in a subprocess. The setup tests install a real
hook into a throwaway user site-packages (`PYTHONUSERBASE`), so they never touch
your own Python. They skip themselves inside a virtualenv, where user
site-packages is disabled.

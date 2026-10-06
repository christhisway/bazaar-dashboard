"""Every tunable and every path the dashboard uses, in one place.

Two ports, both on localhost only: the page a person opens, and the one the tap
inside the client connects to.  The tap's port is written into the tap's own
config by ``setup``, because the client has to know it before the dashboard is
even running; the page's port is chosen at ``start``.
"""

import os
from dataclasses import dataclass
from pathlib import Path

#: The dashboard's own directory: ``bazaar-dashboard/``.
ROOT = Path(__file__).resolve().parent.parent
PACKAGE = Path(__file__).resolve().parent

#: What the browser loads.  Served by exact name only.
STATIC = PACKAGE / "static"

#: The schema, and where ``setup`` generates bindings from it.
PROTO = ROOT / "proto" / "bazaar.proto"
GENERATED = PACKAGE / "wire" / "generated"

#: The directory the client's interpreter puts on ``sys.path`` (via the ``.pth``
#: file ``setup`` installs), and the config the tap reads there.
TAP_DIR = ROOT / "tap"
#: ``$BAZAAR_DASHBOARD_TAP_CONFIG`` overrides it here and in the boot module alike.
TAP_CONFIG = Path(os.environ.get("BAZAAR_DASHBOARD_TAP_CONFIG") or TAP_DIR / "config.json")
#: What ``setup`` names the file it drops into the client's site-packages.
PTH_NAME = "bazaar_dashboard_tap.pth"

#: One directory per client session the dashboard saw: its frames and summary.
RUNS_DIR = ROOT / "runs"

#: The client's start command is ``python -m <module>``; only that is tapped.
DEFAULT_MODULE = "bazaar"


@dataclass(frozen=True)
class DashboardSettings:
    """The live page.

    It binds to localhost only: the page shows a station's inventory and
    plans, which is nobody else's business, and VS Code forwards the port out
    of a devcontainer on its own.
    """

    host: str = "127.0.0.1"
    port: int = 8765
    #: Where the tap inside the client sends frames.  Fixed, not a fallback:
    #: the client was told this port at setup.
    tap_port: int = 8766

    #: How much of the past each panel keeps.  The page is a glance, not a log;
    #: the full record is in ``runs/``.
    recent_trades: int = 30
    recent_actions: int = 150
    history_ticks: int = 60

    #: Ticks of cover below which a reserve is shown as critical, then low.
    critical_cover_ticks: float = 2.0
    low_cover_ticks: float = 5.0
    #: Where each reserve's cover gauge reads full.  Past this, more stock
    #: changes nothing worth looking at.
    cover_gauge_ticks: float = 20.0

    #: The agent panel's two windows, in ticks: the counts, then the strip.
    recent_window_ticks: int = 10
    strip_ticks: int = 30
    #: Highlights the panel lists, and notable changes the hub keeps for it.
    highlights: int = 6
    events_kept: int = 80
    #: A trade settled within this many ticks still shows its cargo lane.
    cargo_ticks: int = 2


DASHBOARD = DashboardSettings()

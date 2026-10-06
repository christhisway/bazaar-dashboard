"""One directory per client session under ``runs/``: every frame, and a summary.

The dashboard keeps its own record rather than reading the client's, because
it cannot know where -- or whether -- a client it has never seen logs anything.
``messages.jsonl`` holds every frame the tap passed on, one JSON object per
line in the same shape the original client used (``direction``, ``kind``,
``message``), so ``history.summary`` reads either.  ``reports.jsonl`` holds
what the client reported about its decisions, and ``session.json`` who the
client was.  When the session ends, ``history.json`` is written beside them.

Nothing here can see a token: the tap passes frames, never connection headers
or URLs, and tokens only ever travel in headers.
"""

import json
import time
from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path

from .history import MESSAGES_FILE, write_summary

REPORTS_FILE = "reports.jsonl"
SESSION_FILE = "session.json"


def jsonable(value):
    """Render a model object as plain JSON types, naming enums.

    Field names mirror the proto, so a logged message reads like the schema.
    Enums become their names (``"RESOURCE_WATER"``) rather than integers,
    because a log is for a human.
    """
    if is_dataclass(value):
        return {f.name: jsonable(getattr(value, f.name)) for f in fields(value)}
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    return value


class SessionLog:
    """Appends one session's frames and reports, and summarises them at the end.

    Files are held open for the session: a busy client sends several frames a
    second, and reopening the log for each would cost more than writing it.
    """

    def __init__(self, root: Path | str, client: dict, clock=time.time):
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(clock()))
        self.directory = Path(root) / f"{stamp}-{client.get('pid', 'unknown')}"
        self.directory.mkdir(parents=True, exist_ok=True)
        self.client = client
        (self.directory / SESSION_FILE).write_text(json.dumps(client, indent=2) + "\n", encoding="utf-8")
        self._messages = (self.directory / MESSAGES_FILE).open("a", encoding="utf-8")
        self._reports = (self.directory / REPORTS_FILE).open("a", encoding="utf-8")

    def message(self, direction: str, message) -> None:
        """Log one decoded frame.  ``direction`` is ``"sent"`` or ``"received"``."""
        row = {"direction": direction, "kind": message.which(), "message": jsonable(message.inner())}
        self._messages.write(json.dumps(row) + "\n")

    def report(self, report: dict) -> None:
        self._reports.write(json.dumps(report, default=str) + "\n")

    def close(self) -> Path:
        """Close the logs and write ``history.json``; returns its path."""
        self._messages.close()
        self._reports.close()
        return write_summary(self.directory, context={
            "module": self.client.get("module"),
            "policy": self.client.get("policy"),
            "client_dir": self.client.get("cwd"),
        })

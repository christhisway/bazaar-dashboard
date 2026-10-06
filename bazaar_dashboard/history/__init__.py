"""What the dashboard's history tab is built from: one JSON summary per session.

``summary`` turns a session's message log into plain data; ``files`` reads the
log and writes ``history.json`` beside it.  The log is the dashboard's own
(``runs/<session>/messages.jsonl``, written by ``sessionlog``), but any log in
the same shape works -- including one a client wrote itself.
"""

from .files import MESSAGES_FILE, SUMMARY_FILE, run_dirs, write_summary
from .summary import SCHEMA_VERSION, summarize_session

__all__ = ["MESSAGES_FILE", "SCHEMA_VERSION", "SUMMARY_FILE", "run_dirs", "summarize_session", "write_summary"]

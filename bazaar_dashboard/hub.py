"""The dashboard's memory of one client session, fed by the tap.

A state says what *is*; it does not say what the client tried.  Two sources
fill that in.  The wire is always there: every command the client sends and
every result it gets back pass through the tap, so the hub can follow each
command from sent to answered on its own.  Reports are optional: a client that
calls the tap's ``report`` adds what the wire never carries -- what its agent
decided, why, with what score, and what it decided but never sent.  The hub
merges the two by request ID, so a client gets the same page either way, only
with or without the reasons.

This is the one stateful piece of the dashboard; what the page shows is
computed by ``view.render``.

Every hook swallows its own errors.  A display must never be the reason a
frame is lost, so a failure is counted and shown on the page instead.
"""

import functools
import json
import time
from collections import Counter, deque

from .settings import DASHBOARD, DashboardSettings
from .view import FAILED_STATUSES, ActionRecord, command_spec, command_terms, describe, failure_key, \
    notable_changes, render
from .wire import model


def _guarded(method):
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            self.errors += 1
            self.last_error = f"{method.__name__}: {type(exc).__name__}: {exc}"
            return None

    return wrapper


#: Statuses a record can still move on from.  Once settled, a later report or
#: frame about the same command -- a retry's duplicate result, say -- is ignored.
OPEN_STATUSES = ("queued", "sent")


class Dashboard:
    """Remembers what the page needs and tells subscribers when it changes.

    Subscribers are called with the page's JSON as a string; the server is the
    only one in practice.  A dashboard nobody subscribes to still records
    everything, which is how the tests use it.
    """

    def __init__(self, settings: DashboardSettings = DASHBOARD, clock=time.time, agent: str | None = None):
        self.settings = settings
        self._clock = clock
        self._subscribers = []
        #: The agent's name, for the page's agent panel: reported by the
        #: client, or read off its ``--policy`` flag by the tap.
        self.agent = agent
        #: Who is connected, as the tap described them; ``None`` before any.
        self.client: dict | None = None
        self._reset()
        self.errors = 0
        self.last_error: str | None = None

    def _reset(self) -> None:
        self.state: model.State | None = None
        #: ``(tick, inventory)`` pairs, one per tick, oldest first.
        self.history: deque[tuple[int, model.Bundle]] = deque(maxlen=self.settings.history_ticks)
        self.actions: deque[ActionRecord] = deque(maxlen=self.settings.recent_actions)
        #: Running counts of each status reached, over the whole session.
        self.totals: Counter[str] = Counter()
        #: What changed between states that is worth a line on the page, oldest
        #: first; see ``view.notable_changes``.
        self.events: deque[dict] = deque(maxlen=self.settings.events_kept)
        #: Commands that failed, by the tick they were decided in, then by
        #: ``view.failure_key``.  Kept apart from ``actions``, which a busy
        #: agent turns over within a few ticks.
        self.failures: dict[int, Counter[str]] = {}
        self.last_decision: dict | None = None
        #: Wall-clock time we first saw the current tick, for the countdown.
        self.tick_seen_at_ms: float | None = None

    def subscribe(self, callback) -> None:
        self._subscribers.append(callback)

    # --- what the page shows ----------------------------------------------
    def view(self) -> dict:
        page = {"state": None} if self.state is None else render(
            self.state,
            history=self.history,
            actions=self.actions,
            totals=self.totals,
            last_decision=self.last_decision,
            tick_seen_at_ms=self.tick_seen_at_ms,
            events=self.events,
            failures=self.failures,
            agent=self.agent,
            settings=self.settings,
        )
        page["client"] = self.client
        page["dashboard"] = {"errors": self.errors, "last_error": self.last_error}
        return page

    def payload(self) -> str:
        """The view as JSON, or a stub naming the failure if it cannot render."""
        try:
            return json.dumps(self.view())
        except Exception as exc:
            self.errors += 1
            self.last_error = f"view: {type(exc).__name__}: {exc}"
            return json.dumps({"state": None, "client": self.client,
                               "dashboard": {"errors": self.errors, "last_error": self.last_error}})

    def publish(self) -> None:
        if not self._subscribers:
            return
        payload = self.payload()
        for callback in self._subscribers:
            try:
                callback(payload)
            except Exception as exc:
                self.errors += 1
                self.last_error = f"publish: {type(exc).__name__}: {exc}"

    # --- the session ---------------------------------------------------------
    @_guarded
    def begin_session(self, client: dict) -> None:
        """A client process connected.  Whatever an earlier one showed is gone.

        ``client`` is what the tap's hello said: ``pid``, ``module``, ``cwd``
        and, when it could read one, ``policy``.
        """
        self._reset()
        self.agent = client.get("policy")
        self.client = {**client, "connected": True, "since_ms": self._clock() * 1000}
        self.publish()

    @_guarded
    def end_session(self) -> None:
        """The client went away.  Its last picture stays up until the next one."""
        if self.client is not None:
            self.client = {**self.client, "connected": False, "since_ms": self._clock() * 1000}
        self.publish()

    # --- frames: what the server pushed --------------------------------------
    @_guarded
    def on_state(self, state: model.State) -> None:
        previous = self.state
        self.state = state
        if previous is not None and (state.run_id != previous.run_id or state.tick < previous.tick):
            # A new run: the old curve, events and failures mean nothing now.
            self.history.clear()
            self.events.clear()
            self.failures.clear()
            previous = None
        if previous is None or previous.tick != state.tick:
            self.tick_seen_at_ms = self._clock() * 1000
        self.events.extend(notable_changes(previous, state, self.settings))
        point = (state.tick, state.observation.inventory)
        if self.history and self.history[-1][0] == state.tick:
            self.history[-1] = point
        else:
            self.history.append(point)
        for tick in [t for t in self.failures if t <= state.tick - self.settings.history_ticks]:
            del self.failures[tick]
        self.publish()

    @_guarded
    def on_result(self, result: model.Result) -> None:
        record = self._by_request(result.request_id)
        if record is not None and record.status in OPEN_STATUSES:
            record.detail = result.code.name.removeprefix("RESULT_CODE_")
            self._settle(record, "ok" if result.ok else "failed")
            self.publish()

    @_guarded
    def on_protocol_error(self, error: model.ProtocolError) -> None:
        record = self._by_request(error.request_id) if error.request_id else None
        if record is not None and record.status in OPEN_STATUSES:
            record.detail = error.code.name
            self._settle(record, "error")
            self.publish()

    # --- frames: what the client sent ----------------------------------------
    @_guarded
    def on_command(self, message: model.ClientMessage) -> None:
        """A command went out.  Track it, unless a report already does.

        A retry reuses its request ID, so a second frame with the same ID is
        the same command and adds nothing.
        """
        spec = command_spec(message)
        if spec is None:
            return  # sync and ready are housekeeping, not actions
        request_id = message.inner().request_id
        record = self._by_request(request_id)
        if record is not None:
            if not record.terms:
                record.terms = command_terms(spec, self.state)
            return
        state = self.state
        record = ActionRecord(
            world_version=state.world_version if state else 0,
            tick=state.tick if state else 0,
            description=describe(spec),
            terms=command_terms(spec, state),
            request_id=request_id,
        )
        self.actions.append(record)
        self._settle(record, "sent")
        self.publish()

    # --- reports: what only the client knows ---------------------------------
    @_guarded
    def on_report(self, report: dict) -> None:
        """One event from the client's ``report`` call.  See the README for the shapes.

        Unknown events are ignored, so a client written against a newer
        dashboard still works with this one.
        """
        handler = {
            "agent": self._report_agent,
            "decided": self._report_decided,
            "sent": self._report_sent,
            "rejected": self._report_rejected,
            "errored": self._report_errored,
        }.get(report.get("event"))
        if handler is not None:
            handler(report)
            self.publish()

    def _report_agent(self, report: dict) -> None:
        self.agent = report.get("name") or self.agent

    def _report_decided(self, report: dict) -> None:
        # Anything still queued from an earlier decision was never sent.
        for record in self.actions:
            if record.status == "queued":
                self._settle(record, "skipped")
        state = self.state
        world_version = report.get("world_version", state.world_version if state else 0)
        tick = report.get("tick", state.tick if state else 0)
        decided = report.get("actions") or []
        for action in decided:
            spec = action.get("command") or {}
            score = action.get("score")
            self.actions.append(
                ActionRecord(
                    world_version=world_version,
                    tick=tick,
                    description=action.get("description") or describe(spec),
                    reason=action.get("reason"),
                    score=None if score is None else float(score),
                    terms=command_terms(spec, state) if spec else {},
                    key=action.get("key"),
                )
            )
        self.last_decision = {"world_version": world_version, "tick": tick, "count": len(decided)}

    def _report_sent(self, report: dict) -> None:
        record = self._by_key(report.get("key"))
        if record is not None:
            record.request_id = report.get("request_id")
            self._settle(record, "sent")

    def _report_rejected(self, report: dict) -> None:
        record = self._by_key(report.get("key"))
        if record is not None:
            record.detail = report.get("detail")
            self._settle(record, "rejected")

    def _report_errored(self, report: dict) -> None:
        record = self._by_request(report.get("request_id"))
        if record is not None and record.status in OPEN_STATUSES:
            record.detail = report.get("detail")
            self._settle(record, "error")

    # --- bookkeeping --------------------------------------------------------
    def _settle(self, record: ActionRecord, status: str) -> None:
        record.status = status
        self.totals[status] += 1
        if status in FAILED_STATUSES:
            self.failures.setdefault(record.tick, Counter())[failure_key(status, record.detail)] += 1

    def _by_key(self, key) -> ActionRecord | None:
        if key is None:
            return None
        for record in reversed(self.actions):
            if record.key == key and record.status == "queued":
                return record
        return None

    def _by_request(self, request_id: str | None) -> ActionRecord | None:
        if request_id is None:
            return None
        for record in reversed(self.actions):
            if record.request_id == request_id:
                return record
        return None

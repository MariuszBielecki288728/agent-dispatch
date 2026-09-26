"""One editable per-Issue status comment with a periodic heartbeat (Issue #17).

This module owns the single "what is happening with my task?" surface a maintainer
can read from GitHub without SSH access to the VM: **one** comment on the source
Issue, created once a task is actually claimed, edited by a heartbeat while the
Command Code subprocess is alive, and edited again — never duplicated — as the task
moves through publication, recovery, pause and completion.

Each rule below is one a simpler implementation gets wrong:

**One comment, owned by a machine-readable marker.** :data:`MARKER_TEMPLATE` names
the exact ``(repo, Issue)`` the comment belongs to. Ownership is proven by that
marker, never by "the text looks similar", so a human's comment is never edited.
The SQLite row for ``(repo, Issue)`` is written *before* the GitHub create call —
the same intent-before-action discipline the push and PR paths use — so a crash
between "GitHub created it" and "SQLite recorded the id" is recoverable: the next
publisher scans the Issue's comments for the marker and adopts what is already
there instead of posting a second one.

**Only the owning thread creates; any thread may edit.** :meth:`StatusPublisher.publish`
never creates a comment unless the caller explicitly allows it, and the heartbeat
thread never does. Two writers therefore cannot both decide "there is no comment
yet" and each POST one.

**A truncated comment scan is a refusal, not an absence.** If the comment listing
hits the page cap, "no marker found" is unprovable, so nothing is created.

**Status reporting is nonfatal.** Every GitHub interaction is bounded by a short
timeout, and every failure is logged and swallowed. A slow, rate-limited, timed-out
or denied comment write must never fail, restart or extend a run; must never change
the task's ``phase``, claim, attempt budget or run validation; and is never retried
under another identity.

**Truthful, templated content.** A comment is rendered from fixed templates plus a
small set of machine-derived values: the state label, the pinned model and effort,
attempt numbers, UTC timestamps that were actually observed, and the verified owned
PR. Free-form operator text (``tasks.last_error``), wrapper stderr, NDJSON, prompts
and filesystem paths are *never* interpolated. Nothing is estimated: there is no
progress percentage, ETA, token count or cost, and a live subprocess is reported as
alive without claiming the model is generating tokens.

**No second scheduler.** :class:`RunHeartbeat` is one daemon thread per live run,
started by the orchestrator and stopped and joined before any terminal edit, so a
five-minute update happens while ``CommandCodeDriver.run()`` is blocked reading the
NDJSON stream.

**A late heartbeat can never overwrite a terminal state — structurally.** One
:class:`StatusPublisher` is shared by the entire run lifecycle (``Starting``,
heartbeat, ``Publishing``, terminal sync), so every write takes the same lock and
sees the same terminal flag. The owning thread sets that flag under the lock before
its first terminal write, and a background write that begins afterwards is refused.
Joining the thread is *not* what provides this guarantee: a join is allowed to time
out while a heartbeat is still inside the transport, so ordering that depended on it
would leave a real window. A heartbeat tick also only ever PATCHes the comment it
already owns — marker re-resolution, adoption and recovery stay on the owning thread.

**Stale status is repaired, never left fresh.** A persisted ``Starting``/``Running``
state is only ever written by a live process, so finding one at startup means the
previous heartbeat died. The reconcile pass re-derives the state from durable task
state and run history and says so in the comment, which is also why an orphaned
``running`` database row can never produce a "completed" status.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping

from .config import Config
from .github import GitHubClient, GitHubError, IssueComment
from .logging_setup import Logger
from .store import (
    PAUSE_MAINTAINER,
    RECOVERY_COMMIT_FAILED,
    RECOVERY_INTERRUPTED,
    RECOVERY_PR_FAILED,
    RECOVERY_PUSH_FAILED,
    StatusComment,
    Store,
    Task,
)

#: How long any single status-comment API call may take. Deliberately much shorter
#: than the general wrapper timeout: a status update still waiting when the run ends
#: is worthless, and this bounds how long an in-flight heartbeat write can delay a
#: terminal update. The run itself is on another thread and is never blocked by it.
STATUS_TIMEOUT_SECONDS = 20.0

#: Machine-readable ownership marker, placed on the first line of every status
#: comment. HTML comments are invisible when GitHub renders one, and the payload is
#: deliberately boring: the exact repository and Issue it belongs to, nothing else.
#: Scoping it to ``(repo, Issue)`` is what stops a marker on one Issue from being
#: read as ownership of another.
MARKER_TEMPLATE = '<!-- agent-dispatch:status repo="{repo}" issue={issue} -->'

#: The vocabulary shown to the maintainer. A closed set, so a state string in a
#: comment is always one of these rather than an ad-hoc sentence.
STATE_QUEUED = "Queued"
STATE_STARTING = "Starting"
STATE_RUNNING = "Running"
STATE_PUBLISHING = "Publishing"
STATE_AWAITING_REVIEW = "Awaiting review"
STATE_RECOVERING = "Recovering publication"
STATE_NEEDS_ATTENTION = "Needs attention"
STATE_PAUSED = "Paused"
STATE_FAILED = "Failed"
STATE_INTERRUPTED = "Interrupted"
STATE_FINISHED = "Finished"

ALL_STATES = (
    STATE_QUEUED,
    STATE_STARTING,
    STATE_RUNNING,
    STATE_PUBLISHING,
    STATE_AWAITING_REVIEW,
    STATE_RECOVERING,
    STATE_NEEDS_ATTENTION,
    STATE_PAUSED,
    STATE_FAILED,
    STATE_INTERRUPTED,
    STATE_FINISHED,
)

#: States that assert an agent process is in flight. Only a live dispatcher writes
#: these, so finding one persisted at startup means a previous heartbeat went stale.
LIVE_STATES = frozenset({STATE_STARTING, STATE_RUNNING})

#: States whose presence in the database means "a status was being updated when the
#: process disappeared", which is what the reconcile pass has to announce.
STALE_PRONE_STATES = frozenset({STATE_STARTING, STATE_RUNNING, STATE_PUBLISHING})

#: Added when a status is re-derived after a process died, so a reader is never left
#: believing a stale "Running" was current.
STALE_HEARTBEAT_NOTE = (
    "This replaced a status written by a dispatcher process that is no longer running, "
    "so the previous heartbeat was stale. The state below was re-derived from durable "
    "task state and recorded run history."
)

#: Fixed next-action text per parked publication stage. Every string here is a
#: template owned by this module — never operator or agent text — so a public status
#: comment cannot leak stderr, a prompt or a filesystem path.
_PUBLICATION_NEXT_ACTION = {
    RECOVERY_COMMIT_FAILED: (
        "The model finished, but its changes could not be committed. They are preserved in the "
        "task's worktree and the dispatcher will commit, push and open the pull request on its "
        "next pass — no further model run."
    ),
    RECOVERY_PUSH_FAILED: (
        "The model finished but the branch could not be pushed. The dispatcher will retry the "
        "push and open the pull request on its next pass — no further model run."
    ),
    RECOVERY_PR_FAILED: (
        "The branch is pushed, but the pull request was not created yet. The dispatcher will "
        "retry that on its next pass — no further model run."
    ),
    RECOVERY_INTERRUPTED: (
        "The previous run was interrupted and its partial edits were deliberately not published. "
        "A human decision is needed before anything is pushed."
    ),
}

_DEFAULT_PUBLICATION_NEXT_ACTION = (
    "The model finished and publication is incomplete. The dispatcher will finish committing, "
    "pushing and opening the pull request on its next pass — no further model run."
)

_FOOTER = (
    "_Status written by agent-dispatch. Review feedback belongs on the pull request; a comment "
    "here never starts agent work by itself._"
)


class StatusCommentError(Exception):
    """Kept for callers that want to re-raise explicitly; nothing in this module raises."""


def marker_for(repo: str, issue_number: int) -> str:
    """The exact ownership marker for one ``(repo, Issue)`` pair."""
    return MARKER_TEMPLATE.format(repo=repo, issue=issue_number)


def contains_marker(body: str, repo: str, issue_number: int) -> bool:
    """Whether ``body`` carries *this* ``(repo, Issue)``'s marker.

    Exact substring, so a marker for another repository or another Issue never
    matches, and a comment that merely resembles a status comment never does.
    """
    return marker_for(repo, issue_number) in (body or "")


def format_utc(value: str | None) -> str | None:
    """Render an observed ISO-8601 timestamp as ``YYYY-MM-DD HH:MM``, or ``None``.

    ``None`` means "not observed", and every caller omits the line rather than
    printing a placeholder: an invented timestamp is worse than an absent one.
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")


def elapsed_seconds(start: str | None, end: str | None) -> int | None:
    """Whole seconds between two observed timestamps, or ``None`` when unparseable.

    Never negative: a clock that jumped backwards reports ``0`` rather than an
    impossible elapsed time.
    """
    if not start or not end:
        return None
    try:
        first = datetime.fromisoformat(start)
        last = datetime.fromisoformat(end)
    except (TypeError, ValueError):
        return None
    if first.tzinfo is None:
        first = first.replace(tzinfo=timezone.utc)
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return max(0, int((last - first).total_seconds()))


def format_elapsed(seconds: int | None) -> str | None:
    """``5 min`` / ``42 s`` — coarse on purpose; sub-minute precision is noise."""
    if seconds is None:
        return None
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    return f"{minutes} min"


def describe_task(task: Task) -> tuple[str, str | None]:
    """The state label and next action for a durable task row.

    This is the **only** place a status comment's state is derived from stored
    state, so the heartbeat, the dispatch path, the per-poll synchronisation and the
    CLI mutations cannot drift into telling different stories.

    Two rules about the phase are deliberately *not* literal translations:

    * A ``running`` row is reported as :data:`STATE_INTERRUPTED`, not ``Running``.
      With one writer holding the single-instance lock and dispatch being
      synchronous, a running row observed by this function has no live process
      behind it — either reconciliation has already repaired it, or reconciliation
      could not run because GitHub was unreachable. Claiming a live run from a
      database row alone is exactly the "fresh-looking running status" this design
      forbids.
    * A task that **owns a verified pull request** is reported as awaiting review
      whatever its phase says. ``queued`` here would be actively misleading: this
      worker's own PR already exists, so no further implementation run will happen
      for it. This is the state a remove-then-re-add of the trigger label leaves
      behind.
    """
    if task.phase == "paused":
        if task.pause_reason == PAUSE_MAINTAINER:
            return STATE_PAUSED, (
                "Paused by the maintainer. `unpause` continues this task, and "
                "`resume-publish` finishes a publication that was already pending."
            )
        return STATE_PAUSED, (
            "The trigger label was removed, so dispatch intent is withdrawn. Re-adding the label "
            "continues this task from where it stopped; existing work is kept."
        )

    if task.phase == "finished":
        return STATE_FINISHED, None

    if task.is_publish_pending:
        # Finished model work waiting on commit/push/PR. Never an implementation run,
        # so the state is about publication, not about an agent.
        return STATE_RECOVERING, _PUBLICATION_NEXT_ACTION.get(
            task.recovery_stage, _DEFAULT_PUBLICATION_NEXT_ACTION
        )

    if task.phase == "running":
        return STATE_INTERRUPTED, (
            "The dispatcher process that owned this run is not running now. On its next start it "
            "will finish or retry this task from recorded evidence; nothing is published on the "
            "strength of this row alone."
        )

    if task.pr_number is not None:
        return STATE_AWAITING_REVIEW, (
            f"Pull request #{task.pr_number} is this task's published work. Waiting for a human "
            "to review and merge; no further agent work starts on its own."
        )

    if task.phase == "awaiting_review":
        return STATE_NEEDS_ATTENTION, (
            "Recorded as awaiting review without a verified pull request. Check the task on the "
            "dispatcher host (`agent-dispatch open`) before acting."
        )

    if task.phase == "queued":
        if task.attempts > 0:
            # A failed attempt within the budget leaves the task queued for another
            # try, so the durable phase is truthful but uninformative: saying only
            # "Queued" would hide the fact that a run just failed.
            return STATE_FAILED, (
                f"Attempt {task.attempts} did not complete successfully. The task is still within "
                "its attempt budget, so the dispatcher will retry it from a fresh session; "
                "`agent-dispatch pause` stops that if it is not wanted."
            )
        return STATE_QUEUED, "Waiting for the dispatcher to start an implementation run."

    if task.phase == "feedback_queued":
        return STATE_QUEUED, "A review round is queued for this task."

    if task.phase == "failed":
        return STATE_FAILED, (
            "The attempt did not complete and the attempt budget is exhausted. "
            "`agent-dispatch retry` allows one more implementation run."
        )

    if task.phase == "needs_attention":
        if task.recovery_stage == RECOVERY_INTERRUPTED:
            return STATE_INTERRUPTED, _PUBLICATION_NEXT_ACTION[RECOVERY_INTERRUPTED]
        return STATE_NEEDS_ATTENTION, (
            "A human decision is needed. The task details are on the dispatcher host "
            "(`agent-dispatch open`); no agent work starts from this state."
        )

    return STATE_NEEDS_ATTENTION, None  # pragma: no cover - PHASES is a closed set


@dataclass(frozen=True)
class StatusView:
    """Everything a status comment may say. Rendered by :func:`render_comment`.

    Deliberately narrow: there is no field for prompt text, agent output, stderr,
    a log path or a task body, so no code path can leak one into a public comment by
    passing a value through.
    """

    repo: str
    issue_number: int
    state: str
    model: str
    effort: str | None = None
    attempt: int | None = None
    max_attempts: int | None = None
    run_started_at: str | None = None
    last_checked_at: str | None = None
    elapsed_seconds: int | None = None
    #: When the comment body being rendered was generated. Shown for non-live states
    #: as "Status updated", so a reader can always tell how fresh the text is without
    #: it ever implying that a process is running.
    updated_at: str | None = None
    #: ``True`` only when the subprocess was actually spawned; ``False`` while the
    #: dispatcher is still preparing, and ``None`` when nothing claims to know.
    process_alive: bool | None = None
    #: Timestamp of the last stream event actually observed, and how many were seen.
    last_event_at: str | None = None
    events_observed: int | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    next_action: str | None = None
    note: str | None = None

    @property
    def live(self) -> bool:
        """Whether volatile status timestamps belong in this render."""
        return self.state in {STATE_STARTING, STATE_RUNNING, STATE_PUBLISHING}

    @property
    def runtime_live(self) -> bool:
        """Whether *runtime liveness* fields may be rendered.

        Narrower than :attr:`live` on purpose. ``Publishing`` means the runtime has
        already stopped cleanly and publication is what is in flight, so the process
        block must not appear at all: rendering it there said "not started yet",
        which is the opposite of what actually happened.
        """
        return self.state in {STATE_STARTING, STATE_RUNNING}


def task_view(
    task: Task,
    *,
    state: str | None = None,
    next_action: str | None = None,
    note: str | None = None,
    run_started_at: str | None = None,
    process_alive: bool | None = None,
    last_event_at: str | None = None,
    events_observed: int | None = None,
    now: str | None = None,
    max_attempts: int | None = None,
    interrupted: bool = False,
) -> StatusView:
    """Build the view for a task row, optionally overriding the derived state.

    The pinned identity comes from the task row (``runtime_model``/``runtime_effort``
    are written at claim time), so a later configuration change cannot make the
    comment describe a model that never ran this task.

    ``interrupted`` is supplied by the caller from **recorded run history**
    (:meth:`~agent_dispatch.store.Store.last_run_interrupted`), not from a note that
    happened to be in scope. That is what makes the stale-heartbeat explanation
    survive a restart and appear on every later synchronisation instead of only once.
    """
    if interrupted and not state:
        state = STATE_INTERRUPTED
    derived_state, derived_action = describe_task(task)
    resolved_state = state or derived_state
    clock = now or utcnow_iso()
    started = run_started_at or (task.last_run_at if resolved_state in LIVE_STATES else None)
    # The "Status updated" stamp is taken from the TASK ROW, not from the render
    # clock, whenever the state is not live. This has to be stable: rendering the
    # current time made the body differ on any poll that crossed a minute boundary,
    # so the "body unchanged, skip the write" comparison could not recognise an
    # unchanged status and the dispatcher re-edited an idle comment on every poll —
    # breaking the guarantee that a poll with nothing running costs no GitHub write.
    # The row's own `updated_at` is the moment the state being described was last
    # written, which is both stable and the more accurate thing to show.
    stamp = (
        clock if resolved_state in LIVE_STATES | {STATE_PUBLISHING} else task.updated_at or clock
    )
    return StatusView(
        repo=task.repo,
        issue_number=task.issue_number,
        state=resolved_state,
        model=task.runtime_model,
        effort=task.runtime_effort,
        attempt=task.attempts if task.attempts > 0 else None,
        max_attempts=max_attempts,
        run_started_at=started,
        last_checked_at=clock if resolved_state in LIVE_STATES | {STATE_PUBLISHING} else None,
        elapsed_seconds=(
            elapsed_seconds(started, clock)
            if resolved_state in LIVE_STATES | {STATE_PUBLISHING}
            else None
        ),
        updated_at=stamp,
        process_alive=process_alive,
        last_event_at=last_event_at,
        events_observed=events_observed,
        pr_number=task.pr_number,
        pr_url=task.pr_url,
        next_action=next_action if next_action is not None else derived_action,
        note=note,
    )


def render_comment(view: StatusView) -> str:
    """Render the comment body. Pure, template-only, and safe for public view."""
    lines = [marker_for(view.repo, view.issue_number), "### agent-dispatch · task status", ""]
    lines.append(f"- **Status:** {view.state}")

    runtime = f"- **Runtime:** Command Code · model `{view.model}`"
    if view.effort:
        runtime += f" · thinking effort `{view.effort}`"
    lines.append(runtime)

    if view.attempt:
        if view.max_attempts:
            lines.append(f"- **Attempt:** {view.attempt} of {view.max_attempts}")
        else:
            lines.append(f"- **Attempt:** {view.attempt}")

    if view.live:
        started = format_utc(view.run_started_at)
        if started:
            lines.append(f"- **Run started:** {started} UTC")
        checked = format_utc(view.last_checked_at)
        if checked:
            lines.append(f"- **Last checked:** {checked} UTC")
        elapsed = format_elapsed(view.elapsed_seconds)
        if elapsed:
            lines.append(f"- **Elapsed:** {elapsed}")

    if view.runtime_live:
        # Runtime liveness is only meaningful while a runtime is meant to be alive.
        # `Publishing` is deliberately excluded: its runtime already exited cleanly,
        # so any process line there would be describing the wrong thing (it used to
        # say "not started yet").
        if view.process_alive is True:
            lines.append(
                "- **Command Code process:** alive — task still running. "
                "This reports the subprocess only; it is not a claim that the model is "
                "generating tokens, and no progress estimate is available."
            )
        elif view.process_alive is False:
            lines.append("- **Command Code process:** not started yet.")
        event_at = format_utc(view.last_event_at)
        if event_at:
            events = (
                f" ({view.events_observed} events observed)"
                if view.events_observed is not None
                else ""
            )
            lines.append(f"- **Last observed runtime event:** {event_at} UTC{events}")
    elif not view.live and view.updated_at:
        # Non-live states still carry the one timestamp that is definitely true: when
        # this text was generated. Labelled differently on purpose, so it can never be
        # mistaken for evidence that something is running now.
        stamp = format_utc(view.updated_at)
        if stamp:
            lines.append(f"- **Status updated:** {stamp} UTC")

    if view.pr_number is not None:
        if view.pr_url:
            lines.append(
                f"- **Pull request:** #{view.pr_number} ({view.pr_url}) — created by this task"
            )
        else:
            lines.append(f"- **Pull request:** #{view.pr_number}")

    if view.next_action:
        lines.append(f"- **Next:** {view.next_action}")
    if view.note:
        lines.append(f"- **Note:** {view.note}")

    lines += ["", _FOOTER]
    return "\n".join(lines)


def utcnow_iso() -> str:
    """Current UTC time as ISO-8601 with an explicit offset (second resolution)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def status_client(config: Config) -> GitHubClient:
    """The client used for status comments: the approved wrapper, short timeout.

    A separate client instance rather than a second credential path: the command
    comes from the same configuration key, so an identity change is impossible.
    """
    return GitHubClient(config.github.command, timeout_seconds=STATUS_TIMEOUT_SECONDS)


class StatusPublisher:
    """Owns one ``(repo, Issue)`` status comment: resolution, edits and creation.

    Thread-safe for publishing. SQLite writes happen only on the thread that
    constructed it — the heartbeat thread publishes to GitHub and logs, but never
    touches the database, which is what keeps a background thread off the worker's
    SQLite connection entirely.
    """

    def __init__(
        self,
        *,
        client: GitHubClient,
        store: Store,
        log: Logger,
        repo: str,
        issue_number: int,
        max_attempts: int | None = None,
    ) -> None:
        self._client = client
        self._store = store
        self._log = log
        self._repo = repo
        self._issue = issue_number
        self._max_attempts = max_attempts
        #: Set once terminal publication has begun. Every write — heartbeat or owning
        #: thread — re-checks this UNDER the lock below, which is what makes "no
        #: heartbeat can land after a terminal state" structural rather than a
        #: consequence of the join succeeding. A join can time out while a heartbeat is
        #: still inside the transport (a failed PATCH plus a marker re-scan plus a
        #: second PATCH is several sequential bounded calls), so relying on the join
        #: alone would leave a real window for `Running` to overwrite `Awaiting review`.
        self._terminal = False
        #: Serialises comment writes between the heartbeat thread and the terminal
        #: update, which is what stops a late heartbeat from overwriting a terminal
        #: state (it re-checks the stop flag before each write, under this lock).
        self._lock = threading.RLock()
        self._comment_id: int | None = None
        self._comment_url: str | None = None
        #: Set when a comment listing was truncated: "no marker found" is then
        #: unprovable, so nothing may be created.
        self._scan_unprovable = False
        self._create_refused_logged = False

    # ------------------------------------------------------------------ facts

    @property
    def marker(self) -> str:
        return marker_for(self._repo, self._issue)

    @property
    def comment_id(self) -> int | None:
        return self._comment_id

    @property
    def comment_url(self) -> str | None:
        return self._comment_url

    def recorded(self) -> StatusComment | None:
        """The persisted ownership row, if this task ever tried to own a comment."""
        return self._store.status_comment(self._repo, self._issue)

    # ------------------------------------------------------------- lifecycle

    def begin(self, task: Task, *, attempt: int, run_started_at: str) -> bool:
        """Create or adopt the comment and publish ``Starting``.

        Called after the claim and before the runtime is spawned. The ownership row
        is written first, so a crash between the GitHub create and the id being
        recorded leaves the intent behind for the next resolution to complete.
        """
        self._store.note_status_intent(
            repo=self._repo,
            issue_number=self._issue,
            task_id=task.id,
            state=STATE_STARTING,
        )
        view = task_view(
            task,
            state=STATE_STARTING,
            run_started_at=run_started_at,
            process_alive=False,
            max_attempts=self._max_attempts or None,
        )
        return self._publish_and_record(view, allow_create=True)

    def publishing(self, task: Task, *, run_started_at: str, attempt: int) -> bool:
        """Publish ``Publishing``: the runtime stopped cleanly, publication has not.

        Distinct from :data:`STATE_AWAITING_REVIEW` on purpose — a clean model result
        is not a pull request, and the maintainer is told which of the two is true.
        """
        view = task_view(
            task,
            state=STATE_PUBLISHING,
            run_started_at=run_started_at,
            process_alive=False,
            max_attempts=self._max_attempts or None,
            next_action=(
                "Committing, pushing and opening the pull request for the work the model "
                "produced. No further model run is involved."
            ),
        )
        view = replace(view, attempt=attempt)
        return self._publish_and_record(view, allow_create=False)

    def failed(self, task: Task, *, attempt: int, note: str | None = None) -> bool:
        """Publish a terminal failure state derived from the task row."""
        view = task_view(
            task,
            state=STATE_FAILED,
            note=note,
            max_attempts=self._max_attempts or None,
        )
        return self._publish_and_record(replace(view, attempt=attempt), allow_create=False)

    def sync_from_task(self, task: Task, *, note: str | None = None) -> bool:
        """Re-derive the state from the durable row and publish it if it changed.

        Idempotent: a body identical to the last published one is not sent, so a
        periodic synchronisation pass costs nothing on GitHub while nothing is
        running. Safe to call for any task, including one no comment was ever created
        for — in that case it is a no-op rather than a new comment.
        """
        row = self.recorded()
        if row is None:
            return False
        interrupted = self._store.last_run_interrupted(task.id)
        if interrupted and note is None:
            # Derived from run history every time, so the explanation is present on
            # every later pass rather than only in the call that first noticed.
            note = STALE_HEARTBEAT_NOTE
        view = task_view(
            task, note=note, max_attempts=self._max_attempts or None, interrupted=interrupted
        )
        return self._publish_and_record(view, allow_create=True)

    def heartbeat_view(self, task: Task, *, attempt: int, run_started_at: str) -> StatusView:
        """The base view the heartbeat thread mutates for each tick.

        Built once, on the owning thread, from an immutable snapshot: the heartbeat
        never re-reads the task row, so it cannot race a concurrent write, and it
        cannot report metadata that changed underneath it.
        """
        view = task_view(
            task,
            state=STATE_STARTING,
            run_started_at=run_started_at,
            process_alive=False,
            max_attempts=self._max_attempts or None,
        )
        return replace(view, attempt=attempt, model=task.runtime_model, effort=task.runtime_effort)

    # -------------------------------------------------------------- transport

    def begin_terminal(self) -> None:
        """Close the window in which a heartbeat may still write.

        Called by the owning thread once the run has stopped and terminal publication
        is about to start. After this, any *background* write is refused, and the
        refusal is decided under the same lock the terminal write takes — so it holds
        even if `stop_and_join()` timed out and the heartbeat is still inside the
        transport. A heartbeat that re-checks its stop flag only *before* a call could
        otherwise land `Running` on top of `Awaiting review`.
        """
        with self._lock:
            self._terminal = True

    def publish(self, view: StatusView, *, allow_create: bool, background: bool = False) -> bool:
        """Edit the owned comment (adopting it by marker if needed) or create it.

        Returns whether a write reached GitHub. Never raises: a status update is
        best-effort by contract, and an exception from here could otherwise fail a
        run whose actual work succeeded.

        ``background`` marks a heartbeat write. Such a write is refused outright once
        terminal publication has begun, and the check happens inside the lock rather
        than before it, so the ordering does not depend on the join having succeeded.
        """
        body = render_comment(view)
        try:
            with self._lock:
                if background and self._terminal:
                    self._log.info(
                        "status_heartbeat_suppressed",
                        repo=self._repo,
                        issue=self._issue,
                        detail="terminal publication has begun; the heartbeat write was dropped",
                    )
                    return False
                return self._publish_locked(body, allow_create=allow_create)
        except Exception as exc:  # noqa: BLE001 - status reporting is never fatal
            self._log.warning(
                "status_comment_unexpected_error",
                repo=self._repo,
                issue=self._issue,
                error=f"{type(exc).__name__}: {exc}",
            )
            return False

    def _publish_locked(self, body: str, *, allow_create: bool) -> bool:
        if self._comment_id is None:
            self._resolve_locked()
        if self._comment_id is not None:
            if self._edit(self._comment_id, body):
                return True
            # The edit failed. That is NOT evidence the comment is gone: a rate limit,
            # a timeout or a transient network error fails identically to a deletion.
            # Re-resolve from GitHub before concluding anything, and treat ANY marked
            # comment found as proof that one still exists locally.
            found = self._scan()
            if found is not None:
                if found.id != self._comment_id:
                    self._adopt(found)
                    if self._edit(found.id, body):
                        return True
                # A marked comment exists and could not be edited. Creating a second
                # one is never the answer, so stop here and let a later pass retry
                # this same id. Falling through to `_create` here was a real bug: a
                # transient PATCH failure on the terminal/per-poll path (which passes
                # `allow_create=True`) would POST a duplicate status comment.
                return False
        if not allow_create:
            return False
        if self._scan_unprovable:
            self._log.warning(
                "status_comment_not_created",
                repo=self._repo,
                issue=self._issue,
                detail=(
                    "the Issue's comment listing was truncated, so an existing status comment "
                    "cannot be ruled out; not creating a second one"
                ),
            )
            return False
        # No marked comment was found and the scan was provably complete, so there is
        # genuinely nothing to edit: creating the first one is safe.
        return self._create(body)

    def _resolve_locked(self) -> None:
        """Find the owned comment: recorded id first, then a marker scan."""
        row = self.recorded()
        if row is not None and row.comment_id:
            self._comment_id = row.comment_id
            self._comment_url = row.comment_url
            return
        found = self._scan()
        if found is not None:
            self._adopt(found)

    def _scan(self) -> IssueComment | None:
        """Find this ``(repo, Issue)``'s marker comment, if one provably exists.

        Returns ``None`` both when there is none and when that cannot be proven; the
        two are distinguished by :attr:`_scan_unprovable`, which the caller checks
        before creating anything.
        """
        try:
            comments = self._client.list_issue_comments(self._repo, self._issue)
        except GitHubError as exc:
            self._scan_unprovable = True
            self._warn("status_comment_scan_failed", kind=exc.kind, error=str(exc))
            return None
        if self._client.comment_scan_truncated:
            self._scan_unprovable = True
            self._warn(
                "status_comment_scan_truncated",
                detail="the comment listing hit the page cap; ownership is unprovable",
            )
            return None
        matches = sorted(
            (item for item in comments if contains_marker(item.body, self._repo, self._issue)),
            key=lambda item: item.id,
        )
        if not matches:
            return None
        if len(matches) > 1:
            # More than one exact marker means ownership is genuinely ambiguous, and
            # the Issue text is explicit that ambiguity must be reported rather than
            # resolved by guessing. Editing one of them would be a coin flip: it could
            # be a human's comment that happens to carry our marker, and a wrong pick
            # also compounds the duplicate problem this design exists to prevent.
            # Fail closed: report, treat ownership as unprovable, and create nothing.
            # An operator resolves it by deleting the extra comment.
            self._scan_unprovable = True
            self._warn(
                "status_comment_ambiguous",
                count=len(matches),
                ids=",".join(str(item.id) for item in matches),
                detail=(
                    "more than one marked status comment exists, so ownership is ambiguous; "
                    "not editing or creating any comment until an operator leaves exactly one"
                ),
            )
            return None
        return matches[0]

    def _adopt(self, comment: IssueComment) -> None:
        self._comment_id = comment.id
        self._comment_url = comment.url
        self._log.info(
            "status_comment_adopted",
            repo=self._repo,
            issue=self._issue,
            comment_id=comment.id,
            detail="reused the marked status comment found on the Issue",
        )

    def _edit(self, comment_id: int, body: str) -> bool:
        try:
            comment = self._client.edit_issue_comment(self._repo, comment_id, body)
        except GitHubError as exc:
            self._warn(
                "status_comment_edit_failed",
                comment_id=comment_id,
                kind=exc.kind,
                retryable=exc.retryable,
                error=str(exc),
                detail="the run is unaffected; the next pass edits the same comment",
            )
            return False
        self._comment_id = comment.id
        self._comment_url = comment.url or self._comment_url
        return True

    def _create(self, body: str) -> bool:
        try:
            comment = self._client.create_issue_comment(self._repo, self._issue, body)
        except GitHubError as exc:
            # A create that timed out may still have landed. Nothing is concluded:
            # the recorded intent (comment_id = NULL) makes the next resolution scan
            # the Issue for the marker and adopt it instead of posting a second one.
            self._warn(
                "status_comment_create_failed",
                kind=exc.kind,
                retryable=exc.retryable,
                error=str(exc),
                detail="no duplicate is created; the next pass adopts it by marker if it landed",
            )
            return False
        self._adopt(comment)
        self._log.info(
            "status_comment_created",
            repo=self._repo,
            issue=self._issue,
            comment_id=comment.id,
        )
        return True

    def _publish_and_record(self, view: StatusView, *, allow_create: bool) -> bool:
        """Publish, then persist ownership/state. Owning-thread only (SQLite writes).

        The body comparison happens against the *persisted* body, not an in-memory
        copy, so a fresh process (a later poll, a restart) still skips an edit whose
        content has not changed.
        """
        body = render_comment(view)
        row = self.recorded()
        if (
            row is not None
            and row.last_body == body
            and row.comment_id
            and self._comment_id
            in (
                None,
                row.comment_id,
            )
        ):
            self._comment_id = row.comment_id
            self._comment_url = row.comment_url
            return False

        published = self.publish(view, allow_create=allow_create)
        if published:
            self._store.record_status_published(
                repo=self._repo,
                issue_number=self._issue,
                comment_id=self._comment_id,
                comment_url=self._comment_url,
                state=view.state,
                body=body,
            )
        else:
            self._store.record_status_error(
                repo=self._repo,
                issue_number=self._issue,
                detail=f"status update for state {view.state} did not reach GitHub",
            )
        return published

    def _warn(self, event: str, **fields: Any) -> None:
        self._log.warning(event, repo=self._repo, issue=self._issue, **fields)


class RunHeartbeat:
    """One daemon thread that keeps a live run's status comment current.

    Started by the orchestrator immediately before the runtime is spawned and
    stopped and joined before any terminal edit. It publishes ``Running`` as soon as
    the process spawn is confirmed — never before, because claiming a model is
    running while the dispatcher is still preparing would be a lie — and then edits
    the comment every ``interval_seconds`` until it is told to stop.

    The thread only ever *edits*; if no comment could be created, it does nothing.
    It never writes to SQLite, so the worker's connection stays owned by one thread.
    """

    def __init__(
        self,
        publisher: StatusPublisher,
        *,
        base_view: StatusView,
        interval_seconds: float,
        log: Logger,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], str] = utcnow_iso,
        poll_seconds: float = 0.05,
    ) -> None:
        self._publisher = publisher
        self._base_view = base_view
        self._interval = max(float(interval_seconds), 0.0)
        self._log = log
        self._clock = clock
        self._now = now
        self._poll = max(poll_seconds, 0.001)
        self._stop = threading.Event()
        self._spawned = threading.Event()
        self._state_lock = threading.Lock()
        self._last_event_at: str | None = None
        self._events_observed = 0
        self._deadline = 0.0
        self._thread: threading.Thread | None = None
        #: Set once ``Running`` has actually been published, so a test can assert the
        #: dispatcher never claimed a live model before a live process.
        self.running_published = False
        #: Number of comment edits this heartbeat performed.
        self.ticks = 0

    # ---------------------------------------------------------------- signals

    def note_spawned(self) -> None:
        """Called by the driver the moment the subprocess exists."""
        self._spawned.set()

    def note_event(self, event: Mapping[str, Any]) -> None:
        """Called by the driver for every parsed stream event.

        Only the observation time and a count are kept: the event payload itself is
        never rendered into the comment, so agent output cannot leak through this
        path.
        """
        with self._state_lock:
            self._events_observed += 1
            self._last_event_at = self._now()

    @property
    def events_observed(self) -> int:
        with self._state_lock:
            return self._events_observed

    # --------------------------------------------------------------- lifecycle

    def start(self) -> None:
        if self._thread is not None:  # pragma: no cover - defensive
            return
        self._thread = threading.Thread(
            target=self._loop,
            name=f"status-heartbeat-{self._base_view.repo}#{self._base_view.issue_number}",
            daemon=True,
        )
        self._thread.start()

    def stop_and_join(self, timeout: float | None = None) -> bool:
        """Stop the heartbeat and wait for it, so a late tick cannot overwrite.

        Returns whether the thread is confirmed finished. A ``False`` means an edit
        was in flight (a slow GitHub call) and the terminal update will queue behind
        it on the publisher's lock — bounded by the status client's timeout, and
        never able to reorder the two writes.
        """
        self._stop.set()
        thread = self._thread
        if thread is None:
            return True
        thread.join(timeout if timeout is not None else STATUS_TIMEOUT_SECONDS + 5.0)
        if thread.is_alive():
            self._log.warning(
                "status_heartbeat_join_timed_out",
                repo=self._base_view.repo,
                issue=self._base_view.issue_number,
                detail="a status edit is still in flight; the terminal update waits for it",
            )
            return False
        return True

    # -------------------------------------------------------------------- loop

    def _loop(self) -> None:
        # Wait for the SPAWN signal before doing anything else, rather than sleeping a
        # whole poll interval and *then* checking. Sleeping first delayed the first
        # `Running` tick by up to `_poll`, and if the run ended inside that window the
        # stop was seen before any tick: the comment went straight from `Starting` to
        # the terminal state without ever reporting `Running`.
        while not self._stop.is_set() and not self._spawned.is_set():
            self._spawned.wait(self._poll)
        if self._stop.is_set():
            return
        while not self._stop.is_set():
            now = self._clock()
            if not self.running_published or now >= self._deadline:
                self._tick()
                # Measured AFTER the tick, so the next one is due a full interval
                # after this one *finished*. Scheduling from the pre-tick time would
                # fire immediately again whenever a write took longer than the
                # interval, turning a slow GitHub call into back-to-back writes.
                self._deadline = self._clock() + self._interval
            # Sleep until the next tick is due (or until stopped), never longer than
            # `_poll` so the stop flag is still noticed promptly. Waiting the full
            # interval in one go is avoided because `stop_and_join` must be able to
            # interrupt it; waiting on an already-set event is avoided because it
            # would spin.
            remaining = self._deadline - self._clock()
            delay = self._poll if remaining <= 0.0 else min(self._poll, remaining)
            if self._stop.wait(delay):
                return

    def _tick(self) -> None:
        if self._stop.is_set():
            # Narrowed under the publisher's lock as well: a tick that began just
            # before the stop must not land after the terminal edit.
            return
        if self._publisher.comment_id is None:
            # No comment is owned, so there is nothing to edit. Creating one is
            # explicitly not this thread's job (only the owning thread may create), and
            # scanning the Issue on a timer would cost an API call every interval to
            # learn the same thing. A later synchronisation pass creates it if the
            # earlier failure was transient.
            return
        with self._state_lock:
            last_event_at = self._last_event_at
            events = self._events_observed
        view = replace(
            self._base_view,
            state=STATE_RUNNING,
            process_alive=True,
            last_checked_at=self._now(),
            last_event_at=last_event_at,
            events_observed=events or None,
        )
        view = replace(
            view,
            elapsed_seconds=elapsed_seconds(view.run_started_at, view.last_checked_at),
        )
        # `background=True`: this tick may only patch the comment it already knows, and
        # it is dropped outright once terminal publication has begun. Marker
        # re-resolution, adoption and recovery all stay on the owning thread — a
        # heartbeat that could re-scan would also be a heartbeat that can make several
        # sequential API calls and outlive its join budget.
        if self._publisher.publish(view, allow_create=False, background=True):
            self.ticks += 1
        self.running_published = True


def sync_task_status(
    config: Config,
    store: Store,
    log: Logger,
    task: Task,
    *,
    client: GitHubClient | None = None,
    note: str | None = None,
) -> bool:
    """Refresh one task's status comment from its durable row. Never fatal.

    Returns whether a write reached GitHub. A task that never owned a comment is a
    no-op: nothing here creates a status for work that was never claimed.
    """
    try:
        publisher = StatusPublisher(
            client=client or status_client(config),
            store=store,
            log=log,
            repo=task.repo,
            issue_number=task.issue_number,
            max_attempts=config.worker.max_attempts,
        )
        return publisher.sync_from_task(task, note=note)
    except Exception as exc:  # noqa: BLE001 - status reporting is never fatal
        log.warning(
            "status_sync_failed",
            repo=task.repo,
            issue=task.issue_number,
            error=f"{type(exc).__name__}: {exc}",
            detail="the task's own state, phase and attempt budget are unaffected",
        )
        return False


def sync_all_status(
    config: Config,
    store: Store,
    log: Logger,
    *,
    client: GitHubClient | None = None,
    notes: Mapping[str, str] | None = None,
    tasks: Iterable[Task] | None = None,
) -> list[str]:
    """Refresh every owned status comment from durable state. Never fatal.

    Used by the startup reconcile pass, by the per-poll pass and by ``resume-publish``,
    so there is exactly one place that decides what a comment should say for a given
    task state. Editing is skipped when the rendered body is unchanged, which is what
    keeps a poll from writing to GitHub while nothing is running.
    """
    shared = client or status_client(config)
    notes = notes or {}
    summary: list[str] = []
    candidates = list(tasks) if tasks is not None else store.list_tasks()
    for task in candidates:
        row = store.status_comment(task.repo, task.issue_number)
        if row is None:
            continue
        note = notes.get(task.ref)
        if note is None and (
            row.last_state in STALE_PRONE_STATES or store.last_run_interrupted(task.id)
        ):
            # The last thing published came from a process that is gone, or the newest
            # recorded run was abandoned by one. Either way, say so rather than letting
            # a stale "Running" read as current.
            note = STALE_HEARTBEAT_NOTE
        try:
            publisher = StatusPublisher(
                client=shared,
                store=store,
                log=log,
                repo=task.repo,
                issue_number=task.issue_number,
                max_attempts=config.worker.max_attempts,
            )
            if publisher.sync_from_task(task, note=note):
                summary.append(f"{task.ref}: status comment updated")
        except Exception as exc:  # noqa: BLE001 - status reporting is never fatal
            log.warning(
                "status_sync_failed",
                repo=task.repo,
                issue=task.issue_number,
                error=f"{type(exc).__name__}: {exc}",
            )
    return summary


def comment_state_for(store: Store, repo: str, issue_number: int) -> str | None:
    """The last state published for a task, for ``open``/``status`` output."""
    row = store.status_comment(repo, issue_number)
    return row.last_state if row is not None else None

"""Durable task queue (SQLite).

Implements the state contract from the approved design (``docs/architecture.md``
§6) with the columns from the reviewed SQLite sketch, plus a small "last observed
on GitHub" block so ``status`` can explain itself between polls.

Division of responsibility, per the design:

* **GitHub is the source of truth** for Issue/PR *content and existence*.
* **This database tracks dispatcher state**: phase, attempts, pinned runtime
  identity, and (from #4) branch/worktree/session/PR identity.

``UNIQUE (repo, issue_number)`` prevents duplicate **task rows**. It is not a
claim of exactly-once external side effects — that is handled by the
intent-before-action rules, which are #4/#5 scope.

Issue #3 never moves a task to ``running``: discovering or queueing an Issue is
not evidence that anything was implemented.
"""

from __future__ import annotations

import os
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .util import utcnow_iso

SCHEMA_VERSION = 1

#: Local phase values from the approved state contract.
PHASES = (
    "queued",
    "running",
    "awaiting_review",
    "feedback_queued",
    "paused",
    "failed",
    "needs_attention",
    "finished",
)

#: Why a task is paused. This distinction is what makes the reconciliation rule
#: unambiguous: a task paused because the maintainer withdrew `take-it` becomes
#: dispatchable again as soon as the label returns, while an explicit maintainer
#: `pause` is sticky and survives polls and restarts.
PAUSE_LABEL_WITHDRAWN = "label_withdrawn"
PAUSE_MAINTAINER = "maintainer"

#: Run outcomes recorded in the `runs` table.
RUN_RUNNING = "running"
RUN_SUCCEEDED = "succeeded"
RUN_FAILED = "failed"

#: Why a task was parked for recovery. Recorded explicitly instead of inferred from
#: the phase, because the two cases need opposite repairs:
#:
#: * :data:`RECOVERY_COMMIT_FAILED` — the agent run **completed and was validated**, and
#:   only publishing failed. Local commits and/or uncommitted edits are finished work,
#:   so recovery may commit them and publish without spending a model call.
#: * :data:`RECOVERY_PUSH_FAILED` / :data:`RECOVERY_PR_FAILED` — a run completed and
#:   published, but the push or PR step failed. Recovery re-does only that step.
#:
#: An interrupted *runtime* leaves no stage at all (or :data:`RECOVERY_INTERRUPTED`),
#: which is what keeps half-written edits from being committed as if they were done.
RECOVERY_COMMIT_FAILED = "commit_failed"
RECOVERY_PUSH_FAILED = "push_failed"
RECOVERY_PR_FAILED = "pr_failed"
RECOVERY_INTERRUPTED = "interrupted"

#: Stages whose preserved local work is finished work and may be published without a
#: model call. Deliberately excludes `interrupted`.
PUBLISHABLE_STAGES = frozenset({RECOVERY_COMMIT_FAILED, RECOVERY_PUSH_FAILED, RECOVERY_PR_FAILED})

#: The one phase a publish-pending task can correctly be in. Publication is finished
#: work waiting on a push/PR, so it belongs in the same "needs a human or a
#: reconciliation pass" bucket as other ambiguous states — never in `queued`, which
#: means "an implementation run is wanted".
PUBLISH_PENDING_PHASE = "needs_attention"

#: `recovery_stage` values that mean "do not start a model run". Interpolated into one
#: SQL predicate below; every member is a module-level constant defined in this file,
#: never caller input, so the interpolation cannot carry anything user-supplied.
_PUBLISHABLE_STAGE_SQL = "(" + ", ".join(f"'{stage}'" for stage in sorted(PUBLISHABLE_STAGES)) + ")"

#: SQL predicate: true when this task has finished work awaiting publication, so a
#: model run must not be started for it. Used by every mutation that could otherwise
#: turn such a task back into an implementation queue entry.
_IS_PUBLISH_PENDING = f"recovery_stage IN {_PUBLISHABLE_STAGE_SQL}"

#: Phases with no further automatic transitions.
TERMINAL_PHASES = frozenset({"finished"})

#: Phases a task can be paused from.
PAUSABLE_PHASES = frozenset(
    {"queued", "awaiting_review", "feedback_queued", "failed", "needs_attention"}
)

#: Phases `retry` may reschedule.
RETRYABLE_PHASES = frozenset({"failed", "needs_attention"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (
  version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id                INTEGER PRIMARY KEY,
  repo              TEXT    NOT NULL,          -- "owner/name"
  issue_number      INTEGER NOT NULL,
  title             TEXT,
  phase             TEXT    NOT NULL DEFAULT 'queued',
  branch            TEXT,
  worktree_path     TEXT,
  base_branch       TEXT,
  pr_number         INTEGER,                   -- PR created by this worker (#4+)
  -- pinned runtime identity (never changed after start)
  runtime_driver    TEXT    NOT NULL,
  runtime_model     TEXT    NOT NULL,
  runtime_effort    TEXT,
  permission_mode   TEXT    NOT NULL,
  session_id        TEXT,
  -- review loop (#5)
  review_round      INTEGER NOT NULL DEFAULT 0,
  feedback_cursor   TEXT,
  -- bookkeeping
  attempts          INTEGER NOT NULL DEFAULT 0,
  last_error        TEXT,
  last_run_at       TEXT,
  pause_reason      TEXT,                      -- label_withdrawn | maintainer
  created_at        TEXT    NOT NULL,
  updated_at        TEXT    NOT NULL,
  UNIQUE (repo, issue_number)                   -- one task row per Issue, always
);

-- Last observed GitHub state. GitHub remains the source of truth; these columns
-- only record what the dispatcher saw and when, so `status` can explain why a
-- task is or is not dispatchable without re-polling.
CREATE TABLE IF NOT EXISTS observed_state (
  task_id           INTEGER PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  trigger_present   INTEGER NOT NULL DEFAULT 0,  -- take-it currently on the Issue
  issue_state       TEXT,                        -- open | closed (last observed)
  linked_pr_number  INTEGER,                     -- pre-existing PR for this Issue
  linked_pr_state   TEXT,                        -- open | merged | closed
  observed_at       TEXT
);

-- One row per agent run. Why a separate table rather than columns on `tasks`:
-- a task accumulates runs across retries, and the audit question "which session
-- IDs were attempted, and how did each end?" must stay answerable. `tasks`
-- keeps only the *current* identity.
CREATE TABLE IF NOT EXISTS runs (
  id                INTEGER PRIMARY KEY,
  task_id           INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  run_id            TEXT    NOT NULL,           -- filename-safe, timestamp-prefixed
  kind              TEXT    NOT NULL,           -- implementation | retry
  resumed_from      TEXT,                       -- session this run tried to continue
  session_id        TEXT,                       -- session the runtime reported
  outcome           TEXT    NOT NULL,           -- running | succeeded | failed
  exit_code         INTEGER,
  subtype           TEXT,
  tool_hook_blocked INTEGER NOT NULL DEFAULT 0,
  timed_out         INTEGER NOT NULL DEFAULT 0,
  produced_work     INTEGER,
  detail            TEXT,                       -- validated reasons, never a transcript
  log_path          TEXT,
  started_at        TEXT    NOT NULL,
  finished_at       TEXT,
  UNIQUE (task_id, run_id)
);

-- Intent-before-action ledger. A side effect that reaches GitHub (a push, a PR)
-- is recorded as *intended* before it is attempted and *confirmed* only after it
-- succeeded, so a crash in between leaves a recoverable expectation rather than
-- a guess — and a restart adopts what exists instead of repeating the call.
CREATE TABLE IF NOT EXISTS operations (
  id                INTEGER PRIMARY KEY,
  task_id           INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  kind              TEXT    NOT NULL,           -- push_branch | create_pr
  state             TEXT    NOT NULL,           -- intended | confirmed | failed
  detail            TEXT,
  external_id       TEXT,                       -- e.g. the created PR number
  created_at        TEXT    NOT NULL,
  updated_at        TEXT    NOT NULL
);
"""


#: Session IDs of runs that ended cleanly, and are therefore resumable later.
#: An interrupted run has no transcript (runtime §2.3.1), so it must never be
#: offered as a resume target — which is why resumability is a query, not a flag.
RESUMABLE_RUN_OUTCOMES = frozenset({"succeeded"})


@dataclass(frozen=True)
class Run:
    """One recorded agent run for a task."""

    id: int
    task_id: int
    run_id: str
    kind: str
    resumed_from: str | None
    session_id: str | None
    outcome: str
    exit_code: int | None
    subtype: str | None
    tool_hook_blocked: bool
    timed_out: bool
    produced_work: bool | None
    detail: str | None
    log_path: str | None
    started_at: str
    finished_at: str | None

    @property
    def resumable(self) -> bool:
        """Whether this run's session may be continued by a later round (#5).

        A clean completion is the only thing that writes a transcript, so a run
        that was killed, blocked or failed is explicitly *not* resumable even
        though its session ID was captured.
        """
        return (
            self.outcome in RESUMABLE_RUN_OUTCOMES
            and bool(self.session_id)
            and not self.tool_hook_blocked
            and not self.timed_out
        )


@dataclass(frozen=True)
class Operation:
    """A recorded intent-before-action side effect."""

    id: int
    task_id: int
    kind: str
    state: str
    detail: str | None
    external_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Task:
    """One ``(repo, Issue)`` task row, joined with its last observed GitHub state."""

    id: int
    repo: str
    issue_number: int
    title: str | None
    phase: str
    branch: str | None
    worktree_path: str | None
    base_branch: str | None
    pr_number: int | None
    runtime_driver: str
    runtime_model: str
    runtime_effort: str | None
    permission_mode: str
    session_id: str | None
    review_round: int
    feedback_cursor: str | None
    attempts: int
    last_error: str | None
    last_run_at: str | None
    pause_reason: str | None
    created_at: str
    updated_at: str
    trigger_present: bool
    issue_state: str | None
    linked_pr_number: int | None
    linked_pr_state: str | None
    observed_at: str | None
    pr_url: str | None = None
    pr_created_at: str | None = None
    dispatched_at: str | None = None
    #: Why the task was parked for recovery, or ``None``. See
    #: :data:`RECOVERY_COMMIT_FAILED` and friends for why the phase alone is not
    #: enough to decide whether preserved edits are finished work.
    recovery_stage: str | None = None

    @property
    def has_publishable_stage(self) -> bool:
        """Whether preserved local work may be published without a model call."""
        return self.recovery_stage in PUBLISHABLE_STAGES

    @property
    def is_publish_pending(self) -> bool:
        """Whether finished work is still awaiting publication.

        Such a task must never be dispatched for implementation: the model already
        completed and only the push/PR step remains, so starting another run would
        spend credits twice and let a second run modify work that is already done.
        """
        return self.has_publishable_stage

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.issue_number}"

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def has_own_pr(self) -> bool:
        """Whether this worker created a PR for the task.

        ``linked_pr_number`` is any PR that *references* the Issue and is only an
        observation; ownership is this column alone (§4, and the #3 review finding
        that a second poll must never promote a foreign PR to owned).
        """
        return self.pr_number is not None

    def dispatchability(self) -> tuple[bool, str]:
        """Whether this task could be dispatched, and why not when it cannot.

        Deliberately says nothing about live GitHub state: it is the cheap local
        pre-check used for reporting, and the authoritative re-check happens
        immediately before claiming, under a conditional SQL transition. A stale
        ``status`` preview must never be what authorizes a run.
        """
        if self.issue_state == "closed":
            return False, "Issue is closed"
        if not self.trigger_present:
            return False, "label removed (dispatch intent withdrawn)"
        if self.is_publish_pending:
            # Reported before the phase check so the reason is the useful one even if a
            # stale or hand-edited row also happens to be `queued`.
            return False, (
                "finished work is awaiting publication; run publish-only recovery "
                "(`agent-dispatch run`) instead of another implementation run"
            )
        if self.has_own_pr:
            return False, f"this worker already owns PR #{self.pr_number}"
        if self.linked_pr_number is not None:
            return False, f"PR #{self.linked_pr_number} already exists for this Issue"
        if self.phase != "queued":
            return False, f"phase is {self.phase}"
        return True, "eligible for dispatch"


class Store:
    """Thin, explicit SQLite wrapper. No ORM, no state-machine engine."""

    def __init__(self, db_path: Path | str, *, read_only: bool = False) -> None:
        self.db_path = Path(db_path)
        self.read_only = read_only
        if str(db_path) == ":memory:":
            self.read_only = False
            self._conn = sqlite3.connect(":memory:", isolation_level=None)
        elif read_only:
            uri = f"file:{self.db_path}?mode=ro"
            self._conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        else:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._conn = sqlite3.connect(str(self.db_path), isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        if not read_only:
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        if not read_only:
            self._migrate()

    @classmethod
    def in_memory(cls) -> "Store":
        """A scratch store used by ``dry-run`` so nothing touches the disk."""
        return cls(":memory:")

    @classmethod
    def open_read_only(cls, db_path: Path | str) -> "Store":
        """Open the state database for reading without being able to write it.

        Read paths (``status``, ``dry-run``) must not create, migrate or modify the
        state file, and must not become a second, unsynchronised writer competing
        with the worker that owns the lock. When the database does not exist yet
        this returns an empty scratch store rather than creating one, so an
        observability command stays free of side effects.
        """
        path = Path(db_path)
        if not path.is_file():
            return cls.in_memory()
        return cls(path, read_only=True)

    def fork_to_memory(self) -> "Store":
        """Copy this database into a scratch in-memory store and return it.

        ``dry-run`` uses this so a simulated poll makes the *same* decisions as a
        real one while the on-disk state file is opened read-only and never
        written. Returns an empty store when this one does not exist yet.
        """
        clone = Store.in_memory()
        for table in ("tasks", "observed_state", "runs", "operations"):
            for row in self._conn.execute(f"SELECT * FROM {table}"):
                columns = list(row.keys())
                placeholders = ", ".join("?" for _ in columns)
                clone._conn.execute(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                    tuple(row[column] for column in columns),
                )
        return clone

    def _migrate(self) -> None:
        self._conn.executescript(_SCHEMA)
        self._add_missing_columns()
        row = self._conn.execute("SELECT version FROM schema_version LIMIT 1").fetchone()
        if row is None:
            self._conn.execute("INSERT INTO schema_version (version) VALUES (?)", (SCHEMA_VERSION,))
        elif int(row["version"]) != SCHEMA_VERSION:
            raise RuntimeError(
                f"state database {self.db_path} has schema version {row['version']}, "
                f"this build expects {SCHEMA_VERSION}"
            )

    #: Columns added after the first release, applied to an existing database so
    #: an upgrade does not require deleting state. Deliberately additive only:
    #: nothing is dropped or rewritten, and no row is ever deleted here.
    _ADDED_COLUMNS: tuple[tuple[str, str, str], ...] = (
        ("tasks", "pause_reason", "TEXT"),
        # Issue #4
        ("tasks", "pr_url", "TEXT"),
        ("tasks", "pr_created_at", "TEXT"),
        ("tasks", "dispatched_at", "TEXT"),
        # Issue #4 review round 2. A bare `needs_attention` phase does not say
        # *why* the task was parked, so recovery could not tell "the agent finished
        # and only publishing failed" (recoverable without a model call) from "the
        # agent was killed mid-edit" (must NOT be auto-committed).
        ("tasks", "recovery_stage", "TEXT"),
    )

    def _add_missing_columns(self) -> None:
        """Apply the additive column migrations, tolerating a losing race.

        "Check then ALTER" is not atomic: two processes opening the same database at
        once — the worker and an explicit `run`, or a test's subprocess — can both
        observe a column as missing and both issue the ``ALTER``, and the loser fails
        with ``duplicate column name``. That surfaced as a crash while a run was in
        flight, i.e. exactly when the service was already recovering from something.

        ``ADD COLUMN`` is the intended state either way, so a duplicate-column failure
        means another process already applied it and is treated as success. The column
        is re-checked to keep this honest rather than blindly swallowing the error.
        """
        for table, column, column_type in self._ADDED_COLUMNS:
            if self._column_present(table, column):
                continue
            try:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")
            except sqlite3.OperationalError as exc:
                if not self._column_present(table, column):
                    raise
                self._log_migration_race(table, column, exc)

    def _column_present(self, table: str, column: str) -> bool:
        existing = {str(info["name"]) for info in self._conn.execute(f"PRAGMA table_info({table})")}
        return column in existing

    def _log_migration_race(self, table: str, column: str, exc: sqlite3.OperationalError) -> None:
        """Record a benign migration race once, without a logger dependency.

        This module deliberately has no logger of its own, so the note goes to stderr
        only when the caller opted into verbosity via the environment. Silence here
        would hide a real concurrency signal; a normal run stays quiet.
        """
        if os.environ.get("AGENT_DISPATCH_DEBUG_MIGRATION"):
            print(
                f"note: {table}.{column} was added concurrently by another process ({exc})",
                file=sys.stderr,
            )

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ reads

    def list_tasks(self, repo: str | None = None) -> list[Task]:
        sql = (
            "SELECT t.*, o.trigger_present, o.issue_state, o.linked_pr_number, o.linked_pr_state, o.observed_at "
            "FROM tasks t LEFT JOIN observed_state o ON o.task_id = t.id"
        )
        params: tuple[Any, ...] = ()
        if repo is not None:
            sql += " WHERE t.repo = ?"
            params = (repo,)
        sql += " ORDER BY t.repo, t.issue_number"
        return [_row_to_task(row) for row in self._conn.execute(sql, params)]

    def get_task(self, repo: str, issue_number: int) -> Task | None:
        row = self._conn.execute(
            "SELECT t.*, o.trigger_present, o.issue_state, o.linked_pr_number, o.linked_pr_state, o.observed_at "
            "FROM tasks t LEFT JOIN observed_state o ON o.task_id = t.id "
            "WHERE t.repo = ? AND t.issue_number = ?",
            (repo, issue_number),
        ).fetchone()
        return _row_to_task(row) if row is not None else None

    def count_by_phase(self) -> dict[str, int]:
        counts = {phase: 0 for phase in PHASES}
        for row in self._conn.execute("SELECT phase, COUNT(*) AS n FROM tasks GROUP BY phase"):
            counts[str(row["phase"])] = int(row["n"])
        return counts

    def active_task_count(self) -> int:
        """Tasks with a live agent process. The MVP allows at most one, globally."""
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM tasks WHERE phase = 'running'"
        ).fetchone()
        return int(row["n"])

    # ----------------------------------------------------------------- writes

    def upsert_discovered(
        self,
        *,
        repo: str,
        issue_number: int,
        title: str | None,
        base_branch: str,
        runtime_driver: str,
        runtime_model: str,
        runtime_effort: str | None,
        permission_mode: str,
        trigger_present: bool,
        issue_state: str | None,
        linked_pr_number: int | None,
        linked_pr_state: str | None,
        phase: str | None = None,
        status_error: str | None = None,
    ) -> bool:
        """Create the task row if absent, otherwise refresh only the observation.

        Returns ``True`` when a new row was created. The ``UNIQUE (repo,
        issue_number)`` constraint means repeated polls can never duplicate a row;
        an existing row is reused when a label is removed and later re-added.
        """
        now = utcnow_iso()
        existing = self.get_task(repo, issue_number)
        created = False

        if existing is None:
            cursor = self._conn.execute(
                "INSERT INTO tasks (repo, issue_number, title, phase, base_branch, runtime_driver, "
                "runtime_model, runtime_effort, permission_mode, last_error, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (repo, issue_number) DO NOTHING",
                (
                    repo,
                    issue_number,
                    title,
                    phase or "queued",
                    base_branch,
                    runtime_driver,
                    runtime_model,
                    runtime_effort,
                    permission_mode,
                    status_error,
                    now,
                    now,
                ),
            )
            if cursor.rowcount == 0:
                # Lost a race with another writer for the same Issue: reuse the row.
                existing = self.get_task(repo, issue_number)
                created = False
            else:
                created = True
                task_id = int(cursor.lastrowid)
                self._conn.execute(
                    "INSERT INTO observed_state (task_id, trigger_present, issue_state, linked_pr_number, "
                    "linked_pr_state, observed_at) VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT (task_id) DO NOTHING",
                    (
                        task_id,
                        int(trigger_present),
                        issue_state,
                        linked_pr_number,
                        linked_pr_state,
                        now,
                    ),
                )
                return True

        task = existing or self.get_task(repo, issue_number)
        if task is None:  # pragma: no cover - defensive
            raise RuntimeError(f"failed to create or load task {repo}#{issue_number}")

        updates: dict[str, Any] = {"title": title, "updated_at": now}
        if phase is not None and task.phase not in TERMINAL_PHASES:
            updates["phase"] = phase
        if status_error is not None:
            updates["last_error"] = status_error

        assignments = ", ".join(f"{key} = ?" for key in updates)
        self._conn.execute(
            f"UPDATE tasks SET {assignments} WHERE id = ?",
            (*updates.values(), task.id),
        )
        self._conn.execute(
            "INSERT INTO observed_state (task_id, trigger_present, issue_state, linked_pr_number, linked_pr_state, observed_at) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (task_id) DO UPDATE SET trigger_present=excluded.trigger_present, "
            "issue_state=excluded.issue_state, linked_pr_number=excluded.linked_pr_number, "
            "linked_pr_state=excluded.linked_pr_state, observed_at=excluded.observed_at",
            (task.id, int(trigger_present), issue_state, linked_pr_number, linked_pr_state, now),
        )
        return created

    def pause(self, repo: str, issue_number: int) -> Task:
        task = self._require(repo, issue_number)
        if task.phase in TERMINAL_PHASES:
            raise ValueError(f"{task.ref} is {task.phase} and cannot be paused")
        if task.phase not in PAUSABLE_PHASES:
            raise ValueError(
                f"{task.ref} is {task.phase} and cannot be paused; pause applies to "
                f"{', '.join(sorted(PAUSABLE_PHASES))} only"
            )
        self._conn.execute(
            "UPDATE tasks SET phase = 'paused', pause_reason = ?, updated_at = ? WHERE id = ?",
            (PAUSE_MAINTAINER, utcnow_iso(), task.id),
        )
        return self._require(repo, issue_number)

    def unpause(self, repo: str, issue_number: int) -> Task:
        """Release a maintainer pause.

        A task with finished work awaiting publication returns to
        ``needs_attention`` rather than the implementation queue: unpausing a
        publish-pending task means "carry on with the publication", and putting it in
        ``queued`` would let the next poll start a second model run on work that is
        already done.
        """
        task = self._require(repo, issue_number)
        if task.phase != "paused":
            raise ValueError(f"{task.ref} is {task.phase}, not paused")
        return self._release_to(
            task, PUBLISH_PENDING_PHASE if task.is_publish_pending else "queued"
        )

    def _release_to(self, task: Task, phase: str) -> Task:
        """Move a task out of a paused/parked state to ``phase``, clearing the pause."""
        self._conn.execute(
            "UPDATE tasks SET phase = ?, pause_reason = NULL, updated_at = ? WHERE id = ?",
            (phase, utcnow_iso(), task.id),
        )
        return self._require(task.repo, task.issue_number)

    def pause_for_withdrawn_label(self, task_id: int, note: str) -> None:
        """Suspend a task because the trigger label was removed.

        Recorded with :data:`PAUSE_LABEL_WITHDRAWN` so the state is not confused
        with a deliberate maintainer pause, and so re-adding the label can safely
        release it again without a human step.
        """
        self._conn.execute(
            "UPDATE tasks SET phase = 'paused', pause_reason = ?, last_error = ?, updated_at = ? WHERE id = ?",
            (PAUSE_LABEL_WITHDRAWN, note, utcnow_iso(), task_id),
        )

    def release_label_withdrawn_pause(self, task_id: int) -> bool:
        """Return a label-withdrawn task to the queue. Returns True if released.

        A maintainer pause is left untouched: only a pause that *this* rule
        created is reversed when the trigger label comes back.

        Publish-pending work is released to ``needs_attention`` instead of ``queued``,
        for the same reason as :meth:`unpause`: re-adding `take-it` restores
        *dispatch intent*, but a task whose model run already completed does not need
        another implementation run — it needs its push/PR finished.
        """
        cursor = self._conn.execute(
            f"UPDATE tasks SET phase = CASE WHEN {_IS_PUBLISH_PENDING} THEN ? ELSE 'queued' END, "
            "pause_reason = NULL, updated_at = ? "
            "WHERE id = ? AND phase = 'paused' AND pause_reason = ?",
            (PUBLISH_PENDING_PHASE, utcnow_iso(), task_id, PAUSE_LABEL_WITHDRAWN),
        )
        return cursor.rowcount > 0

    def retry(self, repo: str, issue_number: int) -> Task:
        """Re-queue a failed/needs_attention task with a cleared attempt budget.

        Refused for a publish-pending task. ``retry`` means "try the implementation
        again", which is precisely what must not happen when the model already
        completed and only publishing failed: it would spend credits twice and let a
        second run modify work that is already finished. The refusal names the action
        that does help, so the operator is not left guessing.
        """
        task = self._require(repo, issue_number)
        if task.is_publish_pending:
            raise ValueError(
                f"{task.ref} has finished work awaiting publication "
                f"(recovery_stage={task.recovery_stage}); retrying would start a second "
                "implementation run. Use `agent-dispatch run` (or let the worker's "
                "startup reconciliation) to finish committing, pushing and opening the "
                "pull request instead."
            )
        if task.phase not in RETRYABLE_PHASES:
            raise ValueError(
                f"{task.ref} is {task.phase}; retry applies to {', '.join(sorted(RETRYABLE_PHASES))} only"
            )
        self._conn.execute(
            "UPDATE tasks SET phase = 'queued', attempts = 0, last_error = NULL, updated_at = ? WHERE id = ?",
            (utcnow_iso(), task.id),
        )
        return self._require(repo, issue_number)

    def resume_publication(self, repo: str, issue_number: int) -> Task:
        """Return a paused publish-pending task to publication. Returns the task.

        This is the publish-pending counterpart of :meth:`retry`, for an operator who
        paused such a task and wants it finished without waiting for a worker poll.
        """
        task = self._require(repo, issue_number)
        if not task.is_publish_pending:
            raise ValueError(
                f"{task.ref} has no pending publication "
                f"(recovery_stage={task.recovery_stage or 'none'}); nothing to resume"
            )
        return self._release_to(task, PUBLISH_PENDING_PHASE)

    def mark_finished(self, task_id: int, note: str) -> None:
        self._conn.execute(
            "UPDATE tasks SET phase = 'finished', last_error = ?, updated_at = ? WHERE id = ?",
            (note, utcnow_iso(), task_id),
        )

    def record_observation(
        self,
        task_id: int,
        *,
        trigger_present: bool,
        issue_state: str | None,
        linked_pr_number: int | None,
        linked_pr_state: str | None,
    ) -> None:
        """Refresh the last-observed GitHub state without touching the task row.

        Used when nothing about the task itself changed — e.g. a poll that only
        confirms a PR this task already owns. Keeps ``updated_at`` meaningful as
        "when dispatcher state last changed" instead of "when we last polled".
        """
        now = utcnow_iso()
        self._conn.execute(
            "INSERT INTO observed_state (task_id, trigger_present, issue_state, linked_pr_number, "
            "linked_pr_state, observed_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (task_id) DO UPDATE SET trigger_present=excluded.trigger_present, "
            "issue_state=excluded.issue_state, linked_pr_number=excluded.linked_pr_number, "
            "linked_pr_state=excluded.linked_pr_state, observed_at=excluded.observed_at",
            (task_id, int(trigger_present), issue_state, linked_pr_number, linked_pr_state, now),
        )

    def mark_needs_attention(self, task_id: int, note: str) -> None:
        self._conn.execute(
            "UPDATE tasks SET phase = 'needs_attention', last_error = ?, updated_at = ? WHERE id = ?",
            (note, utcnow_iso(), task_id),
        )

    def park_for_recovery(self, task_id: int, *, stage: str, note: str) -> None:
        """Park a task in ``needs_attention`` together with **why** it was parked.

        The stage is what makes the difference between the two repairs that look
        identical from the outside: publishing already-finished work (no model call)
        versus discarding an interrupted runtime's partial edits in favour of a fresh
        attempt. Inferring that from the phase alone is what let a crashed agent's
        half-written changes be committed as though they were complete.
        """
        self._conn.execute(
            "UPDATE tasks SET phase = 'needs_attention', recovery_stage = ?, last_error = ?, "
            "updated_at = ? WHERE id = ?",
            (stage, note, utcnow_iso(), task_id),
        )

    def clear_recovery_stage(self, task_id: int) -> None:
        """Forget the parking reason once the task is no longer parked."""
        self._conn.execute(
            "UPDATE tasks SET recovery_stage = NULL, updated_at = ? WHERE id = ?",
            (utcnow_iso(), task_id),
        )

    def last_completed_run(self, task_id: int) -> Run | None:
        """The most recent run that completed **cleanly at the runtime level**.

        "Clean" means the runtime reported success without blocked tools or a
        timeout. This is the evidence that any edits left in the worktree belong to a
        finished piece of work rather than to a process that was killed mid-edit, and
        it is why this is a query over ``runs`` rather than a flag someone sets.
        """
        row = self._conn.execute(
            "SELECT * FROM runs WHERE task_id = ? AND outcome = 'succeeded' "
            "AND tool_hook_blocked = 0 AND timed_out = 0 AND subtype = 'success' "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return _row_to_run(row) if row is not None else None

    def has_unfinished_run(self, task_id: int) -> bool:
        """Whether the newest run never completed (interrupted or failed).

        Used to refuse auto-committing: a newer unfinished run means the worktree may
        hold half-written edits, and the newest run is the one that describes the
        current contents.
        """
        runs = self.run_history(task_id)
        if not runs:
            return False
        return runs[-1].outcome != RUN_SUCCEEDED

    def is_run_completed(self, run_row_id: int) -> bool:
        """Whether one specific run row records a clean runtime completion."""
        row = self._conn.execute(
            "SELECT outcome, tool_hook_blocked, timed_out, subtype FROM runs WHERE id = ?",
            (run_row_id,),
        ).fetchone()
        if row is None:
            return False
        return (
            str(row["outcome"]) == RUN_SUCCEEDED
            and not bool(row["tool_hook_blocked"])
            and not bool(row["timed_out"])
            and row["subtype"] == "success"
        )

    def record_pr_ownership(self, task_id: int, pr_number: int, note: str | None = None) -> None:
        """Record that this worker's own run owns ``pr_number``.

        **Only the #4 PR-creation/adoption workflow may call this**, after it has
        verified that the PR was produced by this task. Discovery must never call
        it: the mere existence of a PR linked to an Issue — a human's PR, or an
        earlier unrelated one — is not evidence that this worker created it, and
        treating it as owned would let a later review round act on someone else's
        pull request.

        If ``note`` is given it is stored as the task's last error/message.
        """
        self._conn.execute(
            "UPDATE tasks SET pr_number = ?, last_error = COALESCE(?, last_error), updated_at = ? WHERE id = ?",
            (pr_number, note, utcnow_iso(), task_id),
        )

    # ------------------------------------------------- issue #4: execution

    def claim_for_run(
        self, task_id: int, *, branch: str, worktree_path: str, base_branch: str
    ) -> bool:
        """Atomically move a task to ``running`` and record its owned paths.

        The phase check is part of the ``UPDATE``'s ``WHERE`` clause rather than a
        read-then-write, so two callers cannot both believe they claimed the same
        task: SQLite applies one statement atomically, and ``rowcount == 0`` is a
        truthful "someone else got it first".

        Eligibility is *also* re-checked against ``observed_state`` in the same
        statement. A stale ``status`` preview is never what authorizes a run, and a
        task whose label was withdrawn (or whose Issue closed) between the poll and
        the claim must lose the race rather than start an agent.

        A publish-pending task is refused here too. This is the last line of defence:
        several operator paths (``retry``, ``unpause``, re-adding the trigger label)
        used to be able to put such a task back into ``queued``, and a row left that
        way by an older build must not start a second model run on work that already
        completed.
        """
        now = utcnow_iso()
        cursor = self._conn.execute(
            "UPDATE tasks SET phase = 'running', branch = ?, worktree_path = ?, base_branch = ?, "
            "dispatched_at = ?, updated_at = ? "
            "WHERE id = ? AND phase = 'queued' "
            f"AND COALESCE({_IS_PUBLISH_PENDING}, 0) = 0 "
            "AND (SELECT COALESCE(issue_state, 'open') FROM observed_state WHERE task_id = tasks.id) "
            "     != 'closed' "
            "AND (SELECT trigger_present FROM observed_state WHERE task_id = tasks.id) = 1",
            (branch, worktree_path, base_branch, now, now, task_id),
        )
        return cursor.rowcount > 0

    def record_runtime_identity(
        self, task_id: int, *, driver: str, model: str, effort: str | None, permission_mode: str
    ) -> None:
        """Pin the runtime identity that a run actually used.

        Written at claim time so a config change between the poll and the run
        cannot leave the row describing a different model than the one invoked, and
        so a later resume re-passes the values recorded here rather than whatever
        the config happens to say at that moment.
        """
        self._conn.execute(
            "UPDATE tasks SET runtime_driver = ?, runtime_model = ?, runtime_effort = ?, "
            "permission_mode = ?, updated_at = ? WHERE id = ?",
            (driver, model, effort, permission_mode, utcnow_iso(), task_id),
        )

    def record_session(self, task_id: int, session_id: str) -> None:
        """Persist the session ID as soon as the runtime emits it.

        Captured immediately (it arrives on the stream's first line) so an
        interrupted run is *traceable* — which is not the same as resumable: an
        interrupted first run has no transcript, so ``runs.outcome`` stays
        ``failed`` and :meth:`resumable_session` will not offer it.
        """
        self._conn.execute(
            "UPDATE tasks SET session_id = ?, updated_at = ? WHERE id = ?",
            (session_id, utcnow_iso(), task_id),
        )

    def start_run(
        self,
        task_id: int,
        *,
        run_id: str,
        kind: str,
        resumed_from: str | None,
        log_path: str,
    ) -> int:
        """Open a ``runs`` row in the ``running`` state and return its id."""
        now = utcnow_iso()
        cursor = self._conn.execute(
            "INSERT INTO runs (task_id, run_id, kind, resumed_from, outcome, log_path, started_at) "
            "VALUES (?, ?, ?, ?, 'running', ?, ?)",
            (task_id, run_id, kind, resumed_from, log_path, now),
        )
        self._conn.execute(
            "UPDATE tasks SET attempts = attempts + 1, last_run_at = ?, updated_at = ? WHERE id = ?",
            (now, now, task_id),
        )
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_row_id: int,
        *,
        outcome: str,
        session_id: str | None,
        exit_code: int | None,
        subtype: str | None,
        tool_hook_blocked: bool,
        timed_out: bool,
        produced_work: bool | None,
        detail: str | None,
    ) -> None:
        """Close a ``runs`` row with the validated outcome.

        ``produced_work`` stays ``None`` when the run never got far enough to
        evaluate it; that is a different fact from ``False`` ("evaluated, and the
        worktree was empty"), and collapsing them would hide an interrupted run.
        """
        self._conn.execute(
            "UPDATE runs SET outcome = ?, session_id = COALESCE(?, session_id), exit_code = ?, "
            "subtype = ?, tool_hook_blocked = ?, timed_out = ?, produced_work = ?, detail = ?, "
            "finished_at = ? WHERE id = ?",
            (
                outcome,
                session_id,
                exit_code,
                subtype,
                int(tool_hook_blocked),
                int(timed_out),
                None if produced_work is None else int(produced_work),
                detail,
                utcnow_iso(),
                run_row_id,
            ),
        )

    def run_history(self, task_id: int) -> list[Run]:
        rows = self._conn.execute(
            "SELECT * FROM runs WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        return [_row_to_run(row) for row in rows]

    def resumable_session(self, task_id: int) -> str | None:
        """The session ID a follow-up round may continue, or ``None``.

        Returns a session only from a run that completed **cleanly**. An
        interrupted run has no transcript, so offering its ID would fail with
        "neither an existing .jsonl transcript nor a known session-id prefix" —
        which is why #5 can refuse a handoff with a clear reason instead of
        silently starting a different conversation.
        """
        row = self._conn.execute(
            "SELECT session_id FROM runs WHERE task_id = ? AND outcome = 'succeeded' "
            "AND tool_hook_blocked = 0 AND timed_out = 0 AND session_id IS NOT NULL "
            "ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        return str(row["session_id"]) if row is not None else None

    def set_phase(self, task_id: int, phase: str, note: str | None = None) -> None:
        """Move a task to ``phase``, refusing an unknown value."""
        if phase not in PHASES:
            raise ValueError(f"{phase!r} is not a phase in the approved state contract")
        self._conn.execute(
            "UPDATE tasks SET phase = ?, last_error = COALESCE(?, last_error), updated_at = ? "
            "WHERE id = ?",
            (phase, note, utcnow_iso(), task_id),
        )

    def set_owned_worktree(
        self, task_id: int, *, branch: str, worktree_path: str, base_branch: str
    ) -> None:
        """Record ownership of a branch/worktree without changing the phase.

        Used when the owned paths are (re)established during reconciliation, so
        "which branch and directory belong to this task" is always answerable from
        the row rather than re-derived from a naming convention.
        """
        self._conn.execute(
            "UPDATE tasks SET branch = ?, worktree_path = ?, base_branch = ?, updated_at = ? "
            "WHERE id = ?",
            (branch, worktree_path, base_branch, utcnow_iso(), task_id),
        )

    def record_owned_pr(
        self,
        task_id: int,
        *,
        pr_number: int,
        pr_url: str | None = None,
        note: str | None = None,
    ) -> None:
        """Record a PR this workflow verified as the task's own, plus its URL."""
        self._conn.execute(
            "UPDATE tasks SET pr_number = ?, pr_url = ?, pr_created_at = COALESCE(pr_created_at, ?), "
            "last_error = COALESCE(?, last_error), updated_at = ? WHERE id = ?",
            (pr_number, pr_url, utcnow_iso(), note, utcnow_iso(), task_id),
        )

    # ------------------------------------------------- intent-before-action

    def intend_operation(self, task_id: int, *, kind: str, detail: str) -> int:
        """Record that a GitHub side effect is *about* to be attempted.

        Written before the call, so a crash mid-push or mid-PR-create leaves a
        recoverable expectation ("we intended to push branch X") instead of an
        ambiguous state that a restart would have to guess about.
        """
        now = utcnow_iso()
        cursor = self._conn.execute(
            "INSERT INTO operations (task_id, kind, state, detail, created_at, updated_at) "
            "VALUES (?, ?, 'intended', ?, ?, ?)",
            (task_id, kind, detail, now, now),
        )
        return int(cursor.lastrowid)

    def confirm_operation(self, operation_id: int, *, external_id: str | None = None) -> None:
        self._conn.execute(
            "UPDATE operations SET state = 'confirmed', external_id = ?, updated_at = ? WHERE id = ?",
            (external_id, utcnow_iso(), operation_id),
        )

    def fail_operation(self, operation_id: int, *, detail: str) -> None:
        self._conn.execute(
            "UPDATE operations SET state = 'failed', detail = ?, updated_at = ? WHERE id = ?",
            (detail, utcnow_iso(), operation_id),
        )

    def latest_operation(self, task_id: int, kind: str) -> Operation | None:
        row = self._conn.execute(
            "SELECT * FROM operations WHERE task_id = ? AND kind = ? ORDER BY id DESC LIMIT 1",
            (task_id, kind),
        ).fetchone()
        return _row_to_operation(row) if row is not None else None

    def operations_for(self, task_id: int) -> list[Operation]:
        rows = self._conn.execute(
            "SELECT * FROM operations WHERE task_id = ? ORDER BY id", (task_id,)
        ).fetchall()
        return [_row_to_operation(row) for row in rows]

    def _require(self, repo: str, issue_number: int) -> Task:
        task = self.get_task(repo, issue_number)
        if task is None:
            raise ValueError(f"no task recorded for {repo}#{issue_number}")
        return task


def _row_to_task(row: Mapping[str, Any]) -> Task:
    keys = set(row.keys())

    def observed(key: str, default: Any) -> Any:
        return row[key] if key in keys else default

    return Task(
        id=int(row["id"]),
        repo=str(row["repo"]),
        issue_number=int(row["issue_number"]),
        title=row["title"],
        phase=str(row["phase"]),
        branch=row["branch"],
        worktree_path=row["worktree_path"],
        base_branch=row["base_branch"],
        pr_number=row["pr_number"],
        runtime_driver=str(row["runtime_driver"]),
        runtime_model=str(row["runtime_model"]),
        runtime_effort=row["runtime_effort"],
        permission_mode=str(row["permission_mode"]),
        session_id=row["session_id"],
        review_round=int(row["review_round"]),
        feedback_cursor=row["feedback_cursor"],
        attempts=int(row["attempts"]),
        last_error=row["last_error"],
        last_run_at=row["last_run_at"],
        pause_reason=observed("pause_reason", None),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
        trigger_present=bool(observed("trigger_present", 0)),
        issue_state=observed("issue_state", None),
        linked_pr_number=observed("linked_pr_number", None),
        linked_pr_state=observed("linked_pr_state", None),
        observed_at=observed("observed_at", None),
        pr_url=observed("pr_url", None),
        pr_created_at=observed("pr_created_at", None),
        dispatched_at=observed("dispatched_at", None),
        recovery_stage=observed("recovery_stage", None),
    )


def _row_to_run(row: Mapping[str, Any]) -> Run:
    produced = row["produced_work"]
    return Run(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        run_id=str(row["run_id"]),
        kind=str(row["kind"]),
        resumed_from=row["resumed_from"],
        session_id=row["session_id"],
        outcome=str(row["outcome"]),
        exit_code=row["exit_code"],
        subtype=row["subtype"],
        tool_hook_blocked=bool(row["tool_hook_blocked"]),
        timed_out=bool(row["timed_out"]),
        produced_work=None if produced is None else bool(produced),
        detail=row["detail"],
        log_path=row["log_path"],
        started_at=str(row["started_at"]),
        finished_at=row["finished_at"],
    )


def _row_to_operation(row: Mapping[str, Any]) -> Operation:
    return Operation(
        id=int(row["id"]),
        task_id=int(row["task_id"]),
        kind=str(row["kind"]),
        state=str(row["state"]),
        detail=row["detail"],
        external_id=row["external_id"],
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def phase_summary(counts: Mapping[str, int]) -> str:
    """Compact ``phase=count`` summary for logs, omitting empty phases."""
    parts: Iterable[str] = (f"{phase}={counts[phase]}" for phase in PHASES if counts.get(phase))
    return " ".join(parts) or "empty"

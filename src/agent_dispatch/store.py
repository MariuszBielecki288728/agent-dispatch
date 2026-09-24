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

import sqlite3
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

#: Phases with no further automatic transitions.
TERMINAL_PHASES = frozenset({"finished"})

#: Phases a task can be paused from.
PAUSABLE_PHASES = frozenset({"queued", "awaiting_review", "feedback_queued", "failed", "needs_attention"})

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
"""


@dataclass(frozen=True)
class Task:
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

    @property
    def ref(self) -> str:
        return f"{self.repo}#{self.issue_number}"

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    def dispatchability(self) -> tuple[bool, str]:
        """Whether this task could be dispatched, and why not when it cannot.

        Issue #3 uses this only for reporting: nothing is ever promoted to
        ``running`` here. It exists so ``status`` and ``dry-run`` tell the truth
        about which queued Issues would be picked up.
        """
        if self.issue_state == "closed":
            return False, "Issue is closed"
        if not self.trigger_present:
            return False, "label removed (dispatch intent withdrawn)"
        if self.linked_pr_number is not None:
            return False, f"PR #{self.linked_pr_number} already exists for this Issue"
        if self.phase != "queued":
            return False, f"phase is {self.phase}"
        return True, "waiting for a dispatcher (#4)"


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

    def fork_to_memory(self) -> "Store":
        """Copy this database into a scratch in-memory store and return it.

        ``dry-run`` uses this so a simulated poll makes the *same* decisions as a
        real one while the on-disk state file is opened read-only and never
        written. Returns an empty store when this one does not exist yet.
        """
        clone = Store.in_memory()
        for table in ("tasks", "observed_state"):
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
    )

    def _add_missing_columns(self) -> None:
        for table, column, column_type in self._ADDED_COLUMNS:
            existing = {
                str(info["name"])
                for info in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_type}")

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ reads

    def list_tasks(self, repo: str | None = None) -> list[Task]:
        sql = "SELECT t.*, o.trigger_present, o.issue_state, o.linked_pr_number, o.linked_pr_state, o.observed_at " \
              "FROM tasks t LEFT JOIN observed_state o ON o.task_id = t.id"
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
        row = self._conn.execute("SELECT COUNT(*) AS n FROM tasks WHERE phase = 'running'").fetchone()
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
                    (task_id, int(trigger_present), issue_state, linked_pr_number, linked_pr_state, now),
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
            raise ValueError(f"{task.ref} is {task.phase} and cannot be paused")
        self._conn.execute(
            "UPDATE tasks SET phase = 'paused', pause_reason = ?, updated_at = ? WHERE id = ?",
            (PAUSE_MAINTAINER, utcnow_iso(), task.id),
        )
        return self._require(repo, issue_number)

    def unpause(self, repo: str, issue_number: int) -> Task:
        task = self._require(repo, issue_number)
        if task.phase != "paused":
            raise ValueError(f"{task.ref} is {task.phase}, not paused")
        self._conn.execute(
            "UPDATE tasks SET phase = 'queued', pause_reason = NULL, updated_at = ? WHERE id = ?",
            (utcnow_iso(), task.id),
        )
        return self._require(repo, issue_number)

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
        """
        cursor = self._conn.execute(
            "UPDATE tasks SET phase = 'queued', pause_reason = NULL, updated_at = ? "
            "WHERE id = ? AND phase = 'paused' AND pause_reason = ?",
            (utcnow_iso(), task_id, PAUSE_LABEL_WITHDRAWN),
        )
        return cursor.rowcount > 0

    def retry(self, repo: str, issue_number: int) -> Task:
        """Re-queue a failed/needs_attention task with a cleared attempt budget."""
        task = self._require(repo, issue_number)
        if task.phase not in RETRYABLE_PHASES:
            raise ValueError(
                f"{task.ref} is {task.phase}; retry applies to {', '.join(sorted(RETRYABLE_PHASES))} only"
            )
        self._conn.execute(
            "UPDATE tasks SET phase = 'queued', attempts = 0, last_error = NULL, updated_at = ? WHERE id = ?",
            (utcnow_iso(), task.id),
        )
        return self._require(repo, issue_number)

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
    )


def phase_summary(counts: Mapping[str, int]) -> str:
    """Compact ``phase=count`` summary for logs, omitting empty phases."""
    parts: Iterable[str] = (f"{phase}={counts[phase]}" for phase in PHASES if counts.get(phase))
    return " ".join(parts) or "empty"

"""The execution orchestrator: one task at a time, Issue → PR.

Wires the #3 queue to a real Command Code run in a task-owned worktree, then to
exactly one pull request. The design constraints it implements:

**One active task globally.** The caller holds the single-instance lock, and
``worker.max_concurrent_tasks`` is forced to 1 by configuration validation, so
"at most one agent task" is structural rather than a scheduling heuristic.

**The claim is the authority, not a preview.** A ``status`` snapshot is a stale
candidate list; eligibility is refreshed immediately before claiming and the
claim itself is a conditional SQL transition
(:meth:`~agent_dispatch.store.Store.claim_for_run`). A label withdrawn between the
poll and the claim therefore loses the race instead of starting an agent.

**Intent before action.** The push and the PR creation are recorded as intended
*before* being attempted and confirmed only afterwards, so a crash in between
leaves a recoverable expectation. On restart the owned branch and PR are looked up
on GitHub and adopted; a second agent run is never launched for work that already
produced a PR.

**Ownership is recorded, never inferred.** ``tasks.pr_number`` is written only
here, only after a PR was created by this workflow or matched on the exact head
branch of a branch this task owns. A PR that merely references the Issue stays an
observation (the #3 bug that must not be reintroduced).

**Bounded retries, honest outcomes.** A blocked-tool stream, a missing or
mismatched session ID, a timeout, or a cap-hit exit are all failures, each with
its own recorded reason. ``tool_hook_blocked`` in particular arrives disguised as a
success, which is why it is checked before anything else is believed.

Deliberately **not** here: the ``agent:fix`` review loop, feedback grouping and
same-session follow-up rounds — that is Issue #5.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, RepoConfig
from .gitcmd import Git, GitError
from .github import ErrorKind, GitHubClient, GitHubError, Issue
from .instruction import build_instruction
from .logging_setup import Logger
from .runlogs import prune, run_log_path
from .runtime import (
    CommandCodeDriver,
    RunResult,
    RuntimeSpawnError,
    next_run_id,
    redact_argv,
)
from .statuscomment import (
    STALE_HEARTBEAT_NOTE,
    STALE_PRONE_STATES,
    RunHeartbeat,
    StatusPublisher,
    status_client,
    sync_all_status,
)
from .store import (
    INTERRUPTED_RUN_MARKER,
    PUBLISH_RECONCILE_PHASES,
    RECOVERY_COMMIT_FAILED,
    RECOVERY_INTERRUPTED,
    RECOVERY_PR_FAILED,
    RECOVERY_PUSH_FAILED,
    RUN_FAILED,
    RUN_RUNNING,
    RUN_SUCCEEDED,
    Store,
    Task,
)
from .util import utcnow_iso
from .worktree import WorktreeError, WorktreeManager, WorktreeState

#: Machine-readable reasons a dispatch attempt did not proceed. Stable codes so
#: callers never match on prose.
BLOCKED_ERROR = "blocked_error"
BLOCKED_NO_ELIGIBLE_TASK = "no_eligible_task"
BLOCKED_ELIGIBILITY_RACE = "eligibility_race"
BLOCKED_ACTIVE_TASK = "active_task"
BLOCKED_WORKTREE = "worktree_unowned"
BLOCKED_RUNTIME_MISSING = "runtime_missing"

#: Outcomes an attempt can report.
OUTCOME_PR_READY = "awaiting_review"
OUTCOME_FAILED = "failed"
OUTCOME_NEEDS_ATTENTION = "needs_attention"
OUTCOME_ADOPTED = "adopted_existing_pr"
OUTCOME_BLOCKED = "blocked"

#: Whether publish-only recovery applies to a task, and what it found.
#: These three must not be collapsed: "handled", "provably nothing published" and
#: "cannot tell" lead to three different actions, and treating unknown as absent is
#: how a task ends up spending a second model call on work that already exists.
PUBLISH_HANDLED = "handled"
PUBLISH_ABSENT = "absent"
PUBLISH_UNKNOWN = "unknown"
PUBLISH_NOT_APPLICABLE = "not_applicable"


@dataclass
class RecoveryResult:
    """What publish-only recovery found and did for one task."""

    status: str
    notes: list[str] = field(default_factory=list)

    @property
    def handled(self) -> bool:
        return self.status == PUBLISH_HANDLED


@dataclass
class DispatchOutcome:
    """What one dispatch attempt did. ``action`` is the closed set above."""

    action: str
    task_ref: str | None = None
    reason: str | None = None
    pr_number: int | None = None
    pr_url: str | None = None
    session_id: str | None = None
    run: RunResult | None = None
    attempts: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def dispatched(self) -> bool:
        return self.action in {OUTCOME_PR_READY, OUTCOME_ADOPTED}

    def summary(self) -> str:
        parts = [f"{self.task_ref or '-'}: {self.action}"]
        if self.pr_number is not None:
            parts.append(f"PR #{self.pr_number}")
        if self.session_id:
            parts.append(f"session {self.session_id}")
        if self.reason:
            parts.append(self.reason)
        return " — ".join(parts)


class Orchestrator:
    """Execute at most one queued task per call, through to a pull request."""

    def __init__(
        self,
        config: Config,
        store: Store,
        client: GitHubClient,
        log: Logger,
        *,
        git: Git | None = None,
        runtime_env: dict[str, str] | None = None,
        status: bool = True,
        status_github: GitHubClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.client = client
        self.log = log
        self.git = git or Git(
            credential_helper=config.github.credential_helper,
            credential_helper_reset=config.github.credential_helper_reset,
        )
        #: Environment handed to the agent subprocess. Carries the reset-then-wrapper
        #: Git config pairs so Git commands the agent issues inherit the approved
        #: helper ordering without any file being modified.
        self.runtime_env = self.git.env(runtime_env)
        #: Whether this instance may create/edit Issue status comments (#17). On by
        #: default on the write paths: a status comment is the point of the feature.
        #: The read-only commands never construct an Orchestrator at all, so they
        #: cannot post one by accident.
        self.status = status
        #: Client used only for status comments. Deliberately a distinct instance with
        #: a short timeout: a status edit must not be able to occupy the single task
        #: lock for as long as a full listing call, and it uses the same configured
        #: wrapper, so no second credential path exists.
        self._status_client = status_github or (status_client(config) if status else None)
        #: UTC start time of the run in flight, for the status comment. Set at the
        #: start of :meth:`_run_once`, which is the only place that knows it.
        self._run_started_at: str | None = None

    # ------------------------------------------------------------------ entry

    def dispatch_next(self) -> DispatchOutcome:
        """Run the oldest eligible task, or explain why nothing ran.

        Only *one* task per call: the MVP allows a single active task globally, so
        looping here would silently break that guarantee.
        """
        if self.store.active_task_count() > 0:
            running = [task for task in self.store.list_tasks() if task.phase == "running"]
            ref = running[0].ref if running else "unknown"
            # One active task total, across all repositories.
            return DispatchOutcome(
                action=OUTCOME_BLOCKED,
                task_ref=ref,
                reason=BLOCKED_ACTIVE_TASK,
                notes=[f"{ref} is already running; the MVP allows one active task globally"],
            )

        candidate = self._oldest_eligible()
        if candidate is None:
            return DispatchOutcome(
                action=OUTCOME_BLOCKED,
                reason=BLOCKED_NO_ELIGIBLE_TASK,
                notes=["no queued task is eligible for dispatch"],
            )
        return self.dispatch_task(candidate)

    def dispatch_task(self, task: Task) -> DispatchOutcome:
        """Execute one specific task through to a PR (or a recorded failure).

        The cheap local eligibility check runs first, so an explicit ``run
        --issue N`` cannot bypass the rules the poll applies — a paused, failed or
        already-owned task is refused here even when it was named directly.
        """
        allowed, reason = task.dispatchability()
        if not allowed:
            return DispatchOutcome(
                action=OUTCOME_BLOCKED,
                task_ref=task.ref,
                reason="not_dispatchable",
                notes=[f"{task.ref} is not dispatchable locally: {reason}"],
            )

        try:
            repo = self.config.repo(task.repo)
        except Exception as exc:
            # An unlisted repository is a configuration fault, not a task fault.
            self.store.set_phase(task.id, "needs_attention", f"repository not allowlisted: {exc}")
            return DispatchOutcome(
                action=OUTCOME_NEEDS_ATTENTION,
                task_ref=task.ref,
                reason="repo_not_allowlisted",
                notes=[str(exc)],
            )

        # Re-validate against GitHub *before* spending anything, because the local
        # row may be stale by up to one poll interval. `_revalidate` returns either
        # an eligible Issue or the outcome explaining why nothing was dispatched —
        # never both, and never neither.
        issue, blocked = self._revalidate(task, repo)
        if issue is None:
            return (
                blocked
                if blocked is not None
                else DispatchOutcome(
                    action=OUTCOME_BLOCKED, task_ref=task.ref, reason="not_eligible"
                )
            )

        return self._execute(task, repo, issue)

    # ------------------------------------------------------- status comment

    def _publisher(self, task: Task) -> StatusPublisher | None:
        """A status publisher for this task, or ``None`` when status is disabled.

        Constructed per task rather than cached, because the ``(repo, Issue)`` pair is
        part of what a publisher owns and caching one across tasks is exactly how a
        status ends up on the wrong Issue.
        """
        if not self.status or self._status_client is None:
            return None
        return StatusPublisher(
            client=self._status_client,
            store=self.store,
            log=self.log,
            repo=task.repo,
            issue_number=task.issue_number,
            max_attempts=self.config.worker.max_attempts,
        )

    def sync_task_status(
        self,
        task: Task,
        *,
        note: str | None = None,
        publisher: StatusPublisher | None = None,
    ) -> bool:
        """Re-derive one task's status comment from durable state. Never fatal.

        Called after any transition that changes what the comment should say
        (pause/unpause/retry/resume-publish, a close, a withdrawal). Editing is
        skipped when the rendered body is unchanged, so this costs nothing on GitHub
        while a task sits still.

        ``publisher`` is supplied by the run lifecycle so the terminal write shares the
        heartbeat's lock and terminal flag. Callers outside a run omit it and get a
        short-lived publisher, which is correct there because no heartbeat exists.
        """
        publisher = publisher or self._publisher(task)
        if publisher is None:
            return False
        try:
            return publisher.sync_from_task(task, note=note)
        except Exception as exc:  # noqa: BLE001 - status reporting is never fatal
            self.log.warning(
                "status_sync_failed",
                repo=task.repo,
                issue=task.issue_number,
                error=f"{type(exc).__name__}: {exc}",
                detail="the task's own state, phase and attempt budget are unaffected",
            )
            return False

    def sync_status_comments(self, *, notes: dict[str, str] | None = None) -> list[str]:
        """Refresh every owned status comment from durable state. Never fatal.

        This is the repair path for the cases where nothing else would touch a
        comment: a stale ``Running`` left by a killed process, a task moved by an
        operator command, or a publication finished by a later poll. Every comment is
        edited in place — none is created here, because a task that never claimed a
        comment never had a status to begin with.
        """
        if not self.status or self._status_client is None:
            return []
        return sync_all_status(
            self.config,
            self.store,
            self.log,
            client=self._status_client,
            notes=notes,
        )

    def _stale_notes(self) -> dict[str, str]:
        """Per-task explainer for comments whose last published state was volatile.

        A persisted ``Starting``/``Running``/``Publishing`` can only have been written
        by a process that is no longer alive (one writer holds the single-instance
        lock, and dispatch is synchronous), so the first synchronisation after a start
        says so rather than letting a stale "Running" read as current.
        """
        notes: dict[str, str] = {}
        for task in self.store.list_tasks():
            row = self.store.status_comment(task.repo, task.issue_number)
            if row is not None and row.last_state in STALE_PRONE_STATES:
                notes[task.ref] = STALE_HEARTBEAT_NOTE
        return notes

    # ------------------------------------------------------------ eligibility

    def _oldest_eligible(self) -> Task | None:
        tasks = [task for task in self.store.list_tasks() if task.phase == "queued"]
        for task in sorted(tasks, key=lambda item: (item.created_at, item.id)):
            allowed, reason = task.dispatchability()
            if allowed:
                return task
            if task.is_publish_pending:
                # Defensive: a publish-pending row found in `queued` is inconsistent
                # state (an older build, or a path that forgot the invariant). Repair it
                # rather than leaving it silently undispatchable forever — and never
                # start a model run for it.
                self._escalate_publish_pending(task)
                continue
            self.log.debug(
                "task_not_dispatchable", repo=task.repo, issue=task.issue_number, reason=reason
            )
        return None

    def _escalate_publish_pending(self, task: Task) -> None:
        """Move a mis-phased publish-pending task back to where publication expects it."""
        detail = (
            f"needs_attention: found in {task.phase!r} with finished work awaiting "
            f"publication (recovery_stage={task.recovery_stage}); no implementation run was "
            "started — publication is finished by the next `agent-dispatch run` or worker start"
        )
        self.store.set_phase(task.id, "needs_attention", detail)
        self.log.warning(
            "publish_pending_escalated",
            repo=task.repo,
            issue=task.issue_number,
            stage=task.recovery_stage,
        )
        self._sync_after_transition(task)

    def _sync_after_transition(self, task: Task) -> None:
        """Update this task's status comment from the row a transition just wrote.

        Re-reads the task rather than using the caller's snapshot, so the comment
        describes the state *after* the transition. A no-op for a task that never
        owned a comment, and never fatal.
        """
        fresh = self.store.get_task(task.repo, task.issue_number)
        if fresh is not None:
            self.sync_task_status(fresh)

    def _revalidate(
        self, task: Task, repo: RepoConfig
    ) -> tuple[Issue | None, DispatchOutcome | None]:
        """Re-check live eligibility. Returns ``(issue, outcome)``.

        A failed check **consumes no model call** and never launches an agent. The
        three conditions that matter — the Issue is still open, still carries the
        trigger label, and has no PR already attached — are all read from GitHub,
        not from the stored observation, because the stored observation is exactly
        what may be stale.
        """
        try:
            issue = self.client.open_issue(repo.slug, task.issue_number)
        except GitHubError as exc:
            note = f"could not re-read {task.ref} before claiming: {exc.kind}: {exc}"
            self.log.warning("dispatch_revalidation_failed", issue=task.issue_number, detail=note)
            return None, DispatchOutcome(
                action=OUTCOME_BLOCKED,
                task_ref=task.ref,
                reason=exc.kind,
                notes=[note, "nothing was dispatched and no state was changed"],
            )

        if issue.is_pull_request:
            self.store.mark_needs_attention(
                task.id, f"#{task.issue_number} is a pull request, not an Issue"
            )
            return None, DispatchOutcome(
                action=OUTCOME_NEEDS_ATTENTION,
                task_ref=task.ref,
                reason="pull_request_object",
            )

        if issue.state == "closed":
            self.store.mark_finished(task.id, f"Issue closed ({issue.url})")
            self._sync_after_transition(task)
            return None, DispatchOutcome(
                action=OUTCOME_BLOCKED, task_ref=task.ref, reason="issue_closed"
            )

        trigger = self.config.github.trigger_label
        if not issue.has_label(trigger):
            self.store.pause_for_withdrawn_label(
                task.id,
                f"label '{trigger}' was removed before dispatch; dispatch intent withdrawn",
            )
            self._sync_after_transition(task)
            return None, DispatchOutcome(
                action=OUTCOME_BLOCKED, task_ref=task.ref, reason="trigger_label_missing"
            )

        # A PR already attached to this Issue means implementation work exists. It
        # is adopted as an *observation* (never as ownership) and no agent runs.
        try:
            pulls = self.client.list_pulls(repo.slug)
        except GitHubError as exc:
            note = f"could not check for an existing PR: {exc.kind}: {exc}"
            self.log.warning("dispatch_pr_check_failed", issue=task.issue_number, detail=note)
            return None, DispatchOutcome(
                action=OUTCOME_BLOCKED, task_ref=task.ref, reason=exc.kind, notes=[note]
            )
        if self.client.pr_scan_truncated:
            return None, DispatchOutcome(
                action=OUTCOME_BLOCKED,
                task_ref=task.ref,
                reason=ErrorKind.INCOMPLETE_SCAN,
                notes=[
                    "PR listing was truncated, so an existing PR cannot be ruled out; "
                    "no agent was started"
                ],
            )

        linked = _linked_pr(pulls, repo.slug, task.issue_number)
        if linked is not None and (task.pr_number is None or task.pr_number != linked.number):
            self.store.record_observation(
                task.id,
                trigger_present=True,
                issue_state=issue.state,
                linked_pr_number=linked.number,
                linked_pr_state=_pr_state(linked),
            )
            self.store.set_phase(
                task.id,
                "awaiting_review",
                f"PR #{linked.number} already exists for this Issue and is not owned by this "
                "worker; recorded as an observation only",
            )
            return None, DispatchOutcome(
                action=OUTCOME_BLOCKED,
                task_ref=task.ref,
                reason="pre_existing_pr",
                pr_number=linked.number,
                pr_url=linked.url,
                notes=[
                    f"#{task.issue_number} already has PR #{linked.number}; no implementation task "
                    "was created and the PR is explicitly NOT recorded as owned"
                ],
            )

        return issue, None

    # --------------------------------------------------------------- execution

    def _execute(self, task: Task, repo: RepoConfig, issue: Issue) -> DispatchOutcome:
        """Provision, claim, run, evaluate, push and open exactly one PR.

        The branch name and worktree path are derived by
        :meth:`~agent_dispatch.worktree.WorktreeManager.ensure` from the recorded
        values (falling back to the deterministic convention), so there is exactly
        one derivation of "where does this task live" rather than two that could
        disagree.
        """
        title = task.title or issue.title or f"issue-{task.issue_number}"

        manager = self._manager(repo)

        try:
            provision = manager.ensure(
                issue_number=task.issue_number,
                title=title,
                recorded_branch=task.branch,
                recorded_path=task.worktree_path,
            )
        except WorktreeError as exc:
            # An unknown path at the expected location is never adopted or deleted.
            self.store.mark_needs_attention(task.id, f"worktree not usable: {exc}")
            return DispatchOutcome(
                action=OUTCOME_NEEDS_ATTENTION,
                task_ref=task.ref,
                reason=BLOCKED_WORKTREE,
                notes=[str(exc)],
            )

        for note in provision.notes:
            self.log.info(
                "worktree_provisioned", repo=repo.slug, issue=task.issue_number, note=note
            )

        # Attempts are bounded before the claim, so a task that already exhausted
        # its budget cannot be run again by a fresh poll.
        if task.attempts >= self.config.worker.max_attempts:
            self.store.set_phase(
                task.id,
                "failed",
                f"attempt budget exhausted ({task.attempts}/{self.config.worker.max_attempts}); "
                "use `agent-dispatch retry` to allow another attempt",
            )
            return DispatchOutcome(
                action=OUTCOME_FAILED,
                task_ref=task.ref,
                reason="attempts_exhausted",
                attempts=task.attempts,
            )

        # Record the owned paths even if the claim then loses the race, so the row
        # always answers "which branch/worktree belongs to this task".
        self.store.set_owned_worktree(
            task.id,
            branch=provision.state.branch,
            worktree_path=str(provision.state.path),
            base_branch=repo.base_branch,
        )

        claimed = self.store.claim_for_run(
            task.id,
            branch=provision.state.branch,
            worktree_path=str(provision.state.path),
            base_branch=repo.base_branch,
        )
        if not claimed:
            # Someone else claimed it, or the label was withdrawn / the Issue closed
            # between the GitHub re-check and this statement.
            fresh = self.store.get_task(task.repo, task.issue_number)
            return DispatchOutcome(
                action=OUTCOME_BLOCKED,
                task_ref=task.ref,
                reason=BLOCKED_ELIGIBILITY_RACE,
                notes=[
                    "the conditional claim did not apply: the task is no longer queued and "
                    f"eligible (now {fresh.phase if fresh else 'unknown'})"
                ],
            )

        self.store.record_runtime_identity(
            task.id,
            driver=repo.runtime.driver,
            model=repo.runtime.model,
            effort=repo.runtime.effort,
            permission_mode=repo.runtime.permission_mode,
        )

        # Re-read the row so the run and its status comment are driven by the identity
        # just PINNED, not by the snapshot taken before the claim. `task` was loaded
        # during discovery, so a config change in between would otherwise let the
        # driver invoke the new model while the comment rendered the old one — the
        # comment claiming a model that never ran this task, which is exactly what the
        # pinned identity exists to prevent.
        pinned = self.store.get_task(task.repo, task.issue_number) or task

        # ONE publisher for the whole run lifecycle (Starting -> Running heartbeats ->
        # Publishing -> terminal sync). Sharing the instance is what shares its lock and
        # its terminal flag with the heartbeat; constructing a second one for the
        # terminal write would give that write an unrelated lock and reopen the window
        # in which a slow heartbeat could overwrite a terminal state.
        publisher = self._publisher(pinned)

        instruction = build_instruction(
            repo_slug=repo.slug,
            issue=issue,
            base_branch=repo.base_branch,
            branch=provision.state.branch,
            worktree_path=provision.state.path,
            agents_file=repo.agents_file,
            is_retry=provision.state.dirty or provision.state.has_commits,
            previous_error=pinned.last_error,
        )
        for note in instruction.notes:
            self.log.info("instruction_note", repo=repo.slug, issue=task.issue_number, note=note)

        result, run_row, early = self._run_once(
            task=pinned,
            repo=repo,
            worktree=provision.state.path,
            instruction=instruction.text,
            kind="implementation",
            session_id=None,
            issue=issue,
            publisher=publisher,
        )
        if early is not None:
            # The runtime could not be started at all, so `_finish` never runs. The
            # status comment must still be brought up to date: `Starting` was already
            # published, and leaving it there would describe a process that does not
            # exist — the same stale-running lie this feature exists to prevent.
            #
            # Re-read first: `_run_once` records the failure, so syncing from the
            # pre-failure object would render the OLD state (e.g. `queued`) for a task
            # the database already knows is `failed`.
            if publisher is not None:
                publisher.begin_terminal()
            self.sync_task_status(
                self.store.get_task(task.repo, task.issue_number) or pinned, publisher=publisher
            )
            return early
        return self._finish(
            pinned,
            repo,
            manager,
            provision.state,
            result,
            run_row=run_row,
            issue=issue,
            publisher=publisher,
            run_started_at=self._run_started_at,
        )

    def _run_once(
        self,
        *,
        task: Task,
        repo: RepoConfig,
        worktree: Path,
        instruction: str,
        kind: str,
        session_id: str | None,
        issue: Issue | None = None,
        publisher: StatusPublisher | None = None,
    ) -> tuple[RunResult | None, int, DispatchOutcome | None]:
        """Invoke the runtime once, recording the run's start and end.

        Returns ``(result, run_row_id, early_outcome)``. ``early_outcome`` is set
        when the runtime could not be started at all, in which case the failed run
        is already recorded and the caller must not continue to the publish step.

        Also owns the status-comment lifecycle for a run (Issue #17): ``Starting`` is
        published **after the claim and before the spawn**, the heartbeat starts as
        soon as the subprocess exists, and both the heartbeat and the ``Publishing``
        transition happen here because this is the only place that knows a run's real
        start and stop.

        The ``publisher`` is supplied by :meth:`_execute` and used for the whole run
        lifecycle, so the heartbeat and every terminal write share ONE lock and one
        terminal flag. That is what makes "no heartbeat lands after a terminal state"
        structural instead of dependent on the join succeeding.
        """
        attempt = task.attempts + 1
        self._run_started_at = utcnow_iso()
        heartbeat: RunHeartbeat | None = None
        if publisher is not None:
            # After the claim: nothing before this point has done any work, so a
            # status comment for an Issue that was only *considered* would be a
            # second, duplicate thread created for no reason.
            publisher.begin(task, attempt=attempt, run_started_at=self._run_started_at)

        driver = CommandCodeDriver(
            repo.runtime,
            run_log_dir=self.config.worker.run_log_dir,
            repo=repo.slug,
            issue_number=task.issue_number,
            binary=self.config.worker.commandcode_path or "commandcode",
            timeout_seconds=float(self.config.worker.run_timeout_seconds),
            env=self.runtime_env,
        )
        run_id = next_run_id(kind)
        # The same helper the retention/pruning logic uses, so the recorded path,
        # the path the driver writes to, and the path `open` reports cannot drift.
        log_path = run_log_path(
            Path(self.config.worker.run_log_dir), repo.slug, task.issue_number, run_id
        )
        run_row = self.store.start_run(
            task.id,
            run_id=run_id,
            kind=kind,
            resumed_from=session_id,
            log_path=str(log_path),
        )

        self.log.info(
            "agent_run_started",
            repo=repo.slug,
            issue=task.issue_number,
            run_id=run_id,
            kind=kind,
            resumed_from=session_id,
            model=repo.runtime.model,
            effort=repo.runtime.effort,
            permission_flag=repo.runtime.permission_flag,
            max_turns=repo.runtime.max_turns,
            timeout_seconds=self.config.worker.run_timeout_seconds,
            worktree=str(worktree),
            argv=" ".join(redact_argv(driver.build_argv(instruction, session_id=session_id))),
        )

        started = time.monotonic()
        if publisher is not None:
            # One bounded thread for the lifetime of this run. Started here — not
            # before — so `Starting` is published without a live process and the
            # heartbeat only begins editing once the subprocess really exists. It
            # keeps working while this thread is blocked reading the stream, which is
            # why it cannot be driven from the polling loop.
            heartbeat = RunHeartbeat(
                publisher,
                base_view=publisher.heartbeat_view(
                    task, attempt=attempt, run_started_at=self._run_started_at or ""
                ),
                interval_seconds=self.config.worker.status_heartbeat_seconds,
                log=self.log,
            )
            heartbeat.start()
        try:
            result = driver.run(
                worktree=worktree,
                instruction=instruction,
                run_id=run_id,
                session_id=session_id,
                # Persist the ID as soon as the stream reports it, not after the
                # process exits: a hard crash mid-run would otherwise leave no trace
                # of which session was attempted, which is precisely the case where
                # that answer is needed. This does NOT make the run resumable — an
                # interrupted first run still has no transcript.
                on_session=lambda new_id: self.store.record_session(task.id, new_id),
                # The two progress signals the status comment needs. Both are
                # informational: the driver swallows any exception either raises, so
                # a misbehaving reporter cannot change this run's outcome.
                on_spawn=(heartbeat.note_spawned if heartbeat is not None else None),
                on_event=(heartbeat.note_event if heartbeat is not None else None),
            )
        except RuntimeSpawnError as exc:
            # The runtime is not installed or cannot start: a configuration fault.
            # Recorded as a failed run so the attempt budget still advances.
            if heartbeat is not None:
                # Stop and join before the terminal edit so a late heartbeat cannot
                # overwrite it.
                heartbeat.stop_and_join()
            self.store.finish_run(
                run_row,
                outcome=RUN_FAILED,
                session_id=None,
                exit_code=None,
                subtype=None,
                tool_hook_blocked=False,
                timed_out=False,
                produced_work=None,
                detail=str(exc),
            )
            self.store.set_phase(task.id, "failed", str(exc))
            return (
                None,
                run_row,
                DispatchOutcome(
                    action=OUTCOME_FAILED,
                    task_ref=task.ref,
                    reason=BLOCKED_RUNTIME_MISSING,
                    attempts=task.attempts + 1,
                    notes=[str(exc)],
                ),
            )

        if result.session_id:
            # Idempotent with the callback above; this covers a runtime that only
            # revealed the ID in its final result line.
            self.store.record_session(task.id, result.session_id)
        if result.session_callback_error:
            self.log.warning(
                "session_id_not_persisted",
                repo=repo.slug,
                issue=task.issue_number,
                error=result.session_callback_error,
                detail="the run is unaffected, but the session ID could not be stored",
            )
        # The subprocess has stopped, so nothing may describe it as alive any more.
        # Joining here — before any terminal edit — is what guarantees a late
        # heartbeat cannot land on top of `Publishing` / `Failed`.
        if heartbeat is not None:
            heartbeat.stop_and_join()
        self.log.info(
            "agent_run_finished",
            duration_s=round(time.monotonic() - started),
            **result.summary_fields(),
        )
        return result, run_row, None

    # -------------------------------------------------------------- post-run

    def _finish(
        self,
        task: Task,
        repo: RepoConfig,
        manager: WorktreeManager,
        before: WorktreeState,
        result: RunResult | None,
        *,
        run_row: int,
        issue: Issue,
        publisher: StatusPublisher | None = None,
        run_started_at: str | None = None,
    ) -> DispatchOutcome:
        """Evaluate a finished run, publish its status, then push and open one PR.

        Wraps :meth:`_evaluate` so that **every** exit path updates the same Issue
        comment from the durable row that path just wrote. Doing it here, rather than
        after each individual return, is what makes it impossible for a new failure
        branch to be added later that silently leaves the comment saying `Running`.
        """
        if result is not None and result.ok and publisher is not None:
            # Runtime completion is not publication completion. `Publishing` is an
            # honest intermediate state: the model is done, the pull request does not
            # exist yet. Claiming `Awaiting review` here is exactly the overstatement
            # this feature is meant to remove.
            #
            # This is the first terminal write, so the heartbeat is closed out first:
            # the flag is set under the shared lock, which is what stops a heartbeat
            # that is STILL inside a slow status call from landing `Running` after this
            # point. Merely having joined the thread is not enough, because that join
            # is allowed to time out.
            publisher.begin_terminal()
            publisher.publishing(
                task, run_started_at=run_started_at or "", attempt=task.attempts + 1
            )
        elif publisher is not None:
            # A failed run publishes no `Publishing`, but it is still terminal: the
            # sync below must not race a heartbeat either.
            publisher.begin_terminal()
        outcome = self._evaluate(
            task,
            repo,
            manager,
            before,
            result,
            run_row=run_row,
            issue=issue,
        )
        if publisher is not None:
            # Re-read rather than reuse `task`: the row was mutated by the path above,
            # and syncing from a stale snapshot is how a comment ends up reporting the
            # state before the transition instead of after it. The SAME publisher is
            # passed through so this final write is serialised with the heartbeat.
            fresh = self.store.get_task(task.repo, task.issue_number)
            if fresh is not None:
                self.sync_task_status(fresh, publisher=publisher)
        return outcome

    def _evaluate(
        self,
        task: Task,
        repo: RepoConfig,
        manager: WorktreeManager,
        before: WorktreeState,
        result: RunResult | None,
        *,
        run_row: int,
        issue: Issue,
    ) -> DispatchOutcome:
        """Evaluate a finished run, then push and open exactly one PR if it worked."""
        if result is None:  # pragma: no cover - the spawn-error path returns early
            return DispatchOutcome(action=OUTCOME_FAILED, task_ref=task.ref)

        after = manager.inspect(before.path, before.branch)
        attempted = task.attempts + 1

        if not result.ok:
            detail = result.validation.summary()
            self.store.finish_run(
                run_row,
                outcome=RUN_FAILED,
                session_id=result.session_id,
                exit_code=result.exit_code,
                subtype=result.subtype,
                tool_hook_blocked=result.tool_hook_blocked,
                timed_out=result.timed_out,
                produced_work=after.produced_work,
                detail=detail,
            )
            return self._record_failure(task, result, after, attempted, detail)

        # A registered worktree that is no longer on the branch this task owns can
        # still look owned (right path, right registration) while its commits and
        # working tree belong to a different branch. Committing or pushing here would
        # attribute someone else's work to this Issue, so nothing is published until
        # a human resolves it.
        if not after.branch_matches:
            detail = (
                f"worktree {after.path} is checked out on {after.checked_out_branch!r} but this "
                f"task owns {after.branch!r}; refusing to commit or push from the wrong branch"
            )
            self.store.finish_run(
                run_row,
                outcome=RUN_FAILED,
                session_id=result.session_id,
                exit_code=result.exit_code,
                subtype=result.subtype,
                tool_hook_blocked=result.tool_hook_blocked,
                timed_out=result.timed_out,
                produced_work=after.produced_work,
                detail=detail,
            )
            self.store.mark_needs_attention(task.id, detail)
            return DispatchOutcome(
                action=OUTCOME_NEEDS_ATTENTION,
                task_ref=task.ref,
                reason="worktree_branch_mismatch",
                session_id=result.session_id,
                run=result,
                notes=[detail, "the run's edits are preserved in the worktree"],
            )

        # Accepted run. The work may be committed, uncommitted, or (rarely) absent;
        # all three are recorded, because "the agent decided nothing was needed" is
        # a legitimate outcome while "the agent wrote nothing" after a success is a
        # fact the operator needs to see rather than have smoothed over.
        committed, commit_note = self._commit_pending(task, repo, manager, after, issue=issue)
        after = manager.inspect(after.path, after.branch)
        # `commit_note` is non-None only when a commit was attempted, so its presence
        # with a still-dirty tree means the attempt failed. Checking the tree rather
        # than trusting the return value keeps this correct if the commit partially
        # applied (e.g. `git add` succeeded and `git commit` did not).
        commit_failed = commit_note is not None and not committed and after.dirty
        if commit_failed:
            commit_note = f"could not commit the run's changes: {commit_note}"

        self.store.finish_run(
            run_row,
            outcome=RUN_FAILED if commit_failed else RUN_SUCCEEDED,
            session_id=result.session_id,
            exit_code=result.exit_code,
            subtype=result.subtype,
            tool_hook_blocked=False,
            timed_out=False,
            produced_work=after.produced_work,
            detail=(
                commit_note
                if commit_failed
                else (None if after.produced_work else "successful run produced no diff")
            ),
        )

        if commit_failed:
            # STOP before push/PR. `git push` cannot carry uncommitted edits, so
            # publishing now would either fail confusingly or — if the branch has
            # earlier commits — open a PR that silently omits exactly the work this
            # run produced. The edits stay in the worktree and a publish-only
            # recovery can finish the job once the cause is fixed, with no second
            # model call.
            detail = (
                f"run #{result.run_id} succeeded but its changes could not be committed "
                f"({commit_note}); nothing was pushed and no PR was created. The edits are "
                "preserved in the worktree."
            )
            self.store.park_for_recovery(task.id, stage=RECOVERY_COMMIT_FAILED, note=detail)
            return DispatchOutcome(
                action=OUTCOME_NEEDS_ATTENTION,
                task_ref=task.ref,
                reason="commit_failed",
                session_id=result.session_id,
                run=result,
                notes=[
                    detail,
                    "fix the cause (e.g. worker.commit_identity_email), then run "
                    "`agent-dispatch run` to commit, push and open the PR without "
                    "running the agent again",
                ],
            )

        if not after.produced_work:
            self.store.set_phase(
                task.id,
                "needs_attention",
                f"run #{result.run_id} completed successfully but the worktree shows no change "
                f"against {before.base_sha or repo.base_branch}; nothing to push",
            )
            return DispatchOutcome(
                action=OUTCOME_NEEDS_ATTENTION,
                task_ref=task.ref,
                reason="no_work_produced",
                session_id=result.session_id,
                run=result,
                notes=[
                    "a successful run produced no commits and no uncommitted changes; "
                    "no branch was pushed and no PR was created"
                ],
            )

        return self._publish(task, repo, manager, after, result=result)

    def _commit_pending(
        self,
        task: Task,
        repo: RepoConfig,
        manager: WorktreeManager,
        state: WorktreeState,
        *,
        issue: Issue | None,
    ) -> tuple[bool, str | None]:
        """Commit anything the agent left uncommitted. Returns ``(committed, note)``.

        ``note`` is ``None`` only when there was nothing to commit, so a caller can
        tell "clean tree" apart from "the commit failed" — the distinction that
        decides whether publishing may proceed at all.
        """
        if not state.dirty:
            return False, None
        if not state.branch_matches:
            return False, (
                f"refusing to commit: worktree is on {state.checked_out_branch!r}, not "
                f"{state.branch!r}"
            )
        committed, note = manager.commit_all(state.path, state.branch, _commit_message(task, issue))
        if committed:
            self.log.info(
                "run_changes_committed", repo=repo.slug, issue=task.issue_number, note=note
            )
        else:
            self.log.warning(
                "run_changes_not_committed", repo=repo.slug, issue=task.issue_number, note=note
            )
        return committed, note

    def _record_failure(
        self, task: Task, result: RunResult, after: WorktreeState, attempted: int, detail: str
    ) -> DispatchOutcome:
        """Record a failed run, preserving whatever it wrote."""
        budget_left = attempted < self.config.worker.max_attempts
        notes = [detail]
        if after.produced_work:
            notes.append(
                "partial work is preserved in the owned worktree "
                f"({after.describe()}); the next attempt starts a fresh session there"
            )
        if result.resumed_from and not result.fresh_session_started:
            notes.append(
                "this was a resume attempt; if it did not complete cleanly the session still has "
                "no transcript, so a retry starts fresh rather than pretending to continue"
            )

        phase = "queued" if budget_left else "failed"
        self.store.set_phase(
            task.id,
            phase,
            f"attempt {attempted}/{self.config.worker.max_attempts} {detail}"
            + ("" if budget_left else " (attempt budget exhausted; use `agent-dispatch retry`)"),
        )
        return DispatchOutcome(
            action=OUTCOME_FAILED,
            task_ref=task.ref,
            reason="run_failed",
            session_id=result.session_id,
            run=result,
            attempts=attempted,
            notes=notes,
        )

    def _publish(
        self,
        task: Task,
        repo: RepoConfig,
        manager: WorktreeManager,
        state: WorktreeState,
        *,
        result: RunResult | None = None,
    ) -> DispatchOutcome:
        """Push the owned branch and create or adopt exactly one PR.

        Shared by the post-run path and publish-only recovery. ``result=None`` means
        no fresh run happened in this call: either a crash left work that was never
        published, or the previous run's commit failed and is now retried. The agent
        is never re-invoked for either, because the code already exists on the
        branch.

        Requires a real owned worktree. Pushing needs one, and inventing a working
        directory would run ``git push`` in whatever directory the dispatcher process
        happens to be in — potentially an unrelated repository — so this refuses
        instead. Recovery for a task whose worktree is gone reconciles the PR through
        the GitHub wrapper without a local push.
        """
        if not (state.exists and state.branch_matches):
            raise ValueError(
                f"refusing to push {state.branch!r} without an owned worktree at {state.path} "
                f"(exists={state.exists}, checked_out={state.checked_out_branch!r})"
            )

        notes: list[str] = []

        # --- push (intent-before-action) ---
        push_op = self.store.intend_operation(
            task.id, kind="push_branch", detail=f"branch {state.branch}"
        )
        pushed, push_note = manager.push(state.path, state.branch)
        if pushed:
            self.store.confirm_operation(push_op)
            notes.append(push_note)
            self.log.info(
                "branch_pushed", repo=repo.slug, issue=task.issue_number, branch=state.branch
            )
        else:
            self.store.fail_operation(push_op, detail=push_note)
            notes.append(push_note)
            # The branch may already be up to date from an earlier attempt; that is
            # not a failure if the remote really has it. Verified rather than
            # assumed, so a genuine push failure is not silently swallowed.
            if not self._branch_on_remote(repo, state.branch):
                self.store.park_for_recovery(
                    task.id,
                    stage=RECOVERY_PUSH_FAILED,
                    note=f"push failed and no branch reached the remote: {push_note}",
                )
                return DispatchOutcome(
                    action=OUTCOME_FAILED,
                    task_ref=task.ref,
                    reason="push_failed",
                    session_id=result.session_id if result else task.session_id,
                    run=result,
                    notes=notes,
                )

        # The push reported success (or the branch was already present). Neither of
        # those proves the remote tip is the work we just produced: a branch pushed by
        # an earlier attempt still exists, so "the branch is there" would let a real
        # push failure pass unnoticed and open a PR **without the new commits**.
        # Compare the remote tip against the local one and refuse to publish on a
        # mismatch — or when the remote tip cannot be read at all, since an unreadable
        # answer is not evidence that the work landed. The invariant documented here is
        # "proceed only when the tips match", so `None` fails closed too.
        remote_tip = self._remote_branch_tip(repo, state.branch)
        if remote_tip is None or (state.head_sha and remote_tip != state.head_sha):
            detail = (
                f"could not confirm that {state.branch} on the remote holds the produced work "
                f"(remote tip {remote_tip or 'unreadable'}, local tip {state.head_sha or 'unknown'}); "
                "no PR was created"
            )
            self.store.park_for_recovery(task.id, stage=RECOVERY_PUSH_FAILED, note=detail)
            return DispatchOutcome(
                action=OUTCOME_NEEDS_ATTENTION,
                task_ref=task.ref,
                reason="remote_tip_unverified",
                session_id=result.session_id if result else task.session_id,
                run=result,
                notes=notes + [detail],
            )

        # --- PR: adopt or create, never both ---
        return self._ensure_pull_request(task, repo, state, result=result, notes=notes)

    def _ensure_pull_request(
        self,
        task: Task,
        repo: RepoConfig,
        state: WorktreeState,
        *,
        result: RunResult | None,
        notes: list[str],
    ) -> DispatchOutcome:
        """Make exactly one PR exist for the owned branch, then wait for review.

        Shared by the post-run path and startup reconciliation, because "the branch
        is pushed but the PR is not confirmed" must behave identically in both: look
        first, adopt what exists, create only what is missing, and **never re-run the
        agent** — the code already exists on the branch (architecture §9).

        An adopted PR is verified to reference this Issue before ownership is
        recorded: an exact head-branch match proves the PR is on our branch, not that
        it belongs to this task.
        """
        pr_op = self.store.intend_operation(
            task.id, kind="create_pr", detail=f"{state.branch} -> {repo.base_branch}"
        )
        try:
            existing = self.client.find_pull_by_head(repo.slug, state.branch)
        except GitHubError as exc:
            self.store.fail_operation(pr_op, detail=str(exc))
            self.store.park_for_recovery(
                task.id,
                stage=RECOVERY_PR_FAILED,
                note=f"branch {state.branch} is pushed, but GitHub could not be queried for an "
                f"existing PR ({exc.kind}); `agent-dispatch run` will retry publish-only "
                "recovery (no new model call)",
            )
            return DispatchOutcome(
                action=OUTCOME_NEEDS_ATTENTION,
                task_ref=task.ref,
                reason="pr_lookup_failed",
                session_id=result.session_id if result else task.session_id,
                run=result,
                notes=notes + [str(exc)],
            )

        if existing is not None:
            # An exact head-branch match is strong but not sufficient. The head
            # repository must be this repo (not a fork whose branch happens to share
            # the name), and the PR must actually reference this Issue. Anything else
            # is ambiguous, and ambiguity must stop for a human rather than let the
            # dispatcher adopt a pull request it did not create.
            if not existing.head_ref_matches_owner(repo.slug):
                self.store.fail_operation(pr_op, detail=f"head owner mismatch: {existing.head_ref}")
                self.store.mark_needs_attention(
                    task.id,
                    f"branch {state.branch} resolves to PR #{existing.number} ({existing.url}), but "
                    f"its head is {existing.head_label!r} rather than {repo.slug!r}; refusing to "
                    "record it as owned",
                )
                return DispatchOutcome(
                    action=OUTCOME_NEEDS_ATTENTION,
                    task_ref=task.ref,
                    reason="pr_head_owner_mismatch",
                    pr_number=existing.number,
                    pr_url=existing.url,
                    notes=notes
                    + [
                        f"PR #{existing.number} was NOT adopted: its head is {existing.head_label!r}"
                    ],
                )

            if not existing.references_issue_in_text(repo.slug, task.issue_number):
                self.store.fail_operation(
                    pr_op, detail=f"PR #{existing.number} does not reference the Issue"
                )
                self.store.mark_needs_attention(
                    task.id,
                    f"PR #{existing.number} ({existing.url}) is on our branch {state.branch} but "
                    f"nothing in it references #{task.issue_number}; refusing to record it as "
                    "owned",
                )
                return DispatchOutcome(
                    action=OUTCOME_NEEDS_ATTENTION,
                    task_ref=task.ref,
                    reason="pr_issue_link_unproven",
                    pr_number=existing.number,
                    pr_url=existing.url,
                    notes=notes
                    + [
                        f"PR #{existing.number} was NOT adopted: nothing in it references "
                        f"#{task.issue_number}"
                    ],
                )

            self.store.confirm_operation(pr_op, external_id=str(existing.number))
            # One atomic write: recording the PR, clearing the recovery stage and
            # entering `awaiting_review` are only true together. Three separate
            # autocommitted statements left crash windows that could strand the row
            # as `Recovering publication` forever (or `needs_attention`) with an
            # owned PR — see `Store.finalise_publication`.
            self.store.finalise_publication(
                task.id,
                pr_number=existing.number,
                pr_url=existing.url,
                note=f"adopted the existing PR for owned branch {state.branch}",
            )
            notes.append(
                f"PR #{existing.number} already existed for the owned branch {state.branch}; "
                "adopted it instead of creating a second one"
            )
            return DispatchOutcome(
                action=OUTCOME_ADOPTED,
                task_ref=task.ref,
                pr_number=existing.number,
                pr_url=existing.url,
                session_id=result.session_id if result else task.session_id,
                run=result,
                notes=notes,
            )

        body = _pr_body(task, result, state, repo)
        try:
            pull = self.client.create_pull(
                repo.slug,
                head_branch=state.branch,
                base_branch=repo.base_branch,
                title=_pr_title(task),
                body=body,
            )
        except GitHubError as exc:
            self.store.fail_operation(pr_op, detail=str(exc))
            # The branch is pushed and the PR is not confirmed: record the intent as
            # recoverable and let publish-only recovery adopt or create it without
            # re-running the agent.
            self.store.park_for_recovery(
                task.id,
                stage=RECOVERY_PR_FAILED,
                note=f"branch {state.branch} is pushed but PR creation failed ({exc.kind}): {exc}. "
                "`agent-dispatch run` will retry publish-only recovery (no new model call).",
            )
            return DispatchOutcome(
                action=OUTCOME_NEEDS_ATTENTION,
                task_ref=task.ref,
                reason="pr_create_failed",
                session_id=result.session_id if result else task.session_id,
                run=result,
                notes=notes + [str(exc)],
            )

        # Record the real number GitHub returned. This is the only place ownership
        # is written, and it happens after the PR provably exists.
        self.store.confirm_operation(pr_op, external_id=str(pull.number))
        # Atomic with the stage clear and the phase change: a crash between separate
        # statements could leave an owned PR beside a stale publishable stage.
        self.store.finalise_publication(task.id, pr_number=pull.number, pr_url=pull.url)
        notes.append(f"created PR #{pull.number} referencing #{task.issue_number}")
        self.log.info(
            "pull_request_created",
            repo=repo.slug,
            issue=task.issue_number,
            pr=pull.number,
            url=pull.url,
            session_id=result.session_id if result else task.session_id,
        )
        return DispatchOutcome(
            action=OUTCOME_PR_READY,
            task_ref=task.ref,
            pr_number=pull.number,
            pr_url=pull.url,
            session_id=result.session_id if result else task.session_id,
            run=result,
            notes=notes,
        )

    def _remote_branch_tip(self, repo: RepoConfig, branch: str) -> str | None:
        """The SHA of ``branch`` on the remote, or ``None`` when it is absent.

        Returning the SHA rather than a boolean is what makes "the push delivered our
        work" checkable. A pre-existing branch from an earlier attempt answers "exists"
        while pointing at *older* commits, so a plain existence check would accept a
        failed push and publish a PR without the new work.

        A Git failure propagates: unreadable is not the same as absent, and guessing
        in that direction risks publishing the wrong thing.
        """
        result = self.git.run(
            ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"], cwd=repo.path
        )
        if not result.ok:
            raise GitError(
                f"git ls-remote failed for {branch}: {result.stderr.strip()}",
                argv=["ls-remote", "origin", branch],
            )
        line = result.stdout.strip()
        if not line:
            return None
        # `ls-remote` prints "<sha>\t<ref>".
        sha = line.split()[0].strip()
        return sha or None

    def _branch_on_remote(self, repo: RepoConfig, branch: str) -> bool:
        """Whether ``branch`` exists on the remote, asked through Git.

        Asked via ``git ls-remote`` rather than the GitHub API on purpose. A
        ``dispatch/issue-N-slug`` branch name contains a slash, and the API path
        ``repos/{slug}/branches/{branch}`` requires it to be percent-encoded — a
        detail that is easy to get wrong and that fails with an indistinguishable
        404. Git handles the ref natively, and this uses the same approved credential
        ordering as every other Git call here.

        A Git failure propagates as :class:`~agent_dispatch.gitcmd.GitError`: "cannot
        reach the remote" is not "the branch is absent", and guessing wrong in that
        direction is how a duplicate push or a spurious PR gets made.
        """
        result = self.git.run(
            ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"], cwd=repo.path
        )
        if not result.ok:
            raise GitError(
                f"git ls-remote failed for {branch}: {result.stderr.strip()}",
                argv=["ls-remote", "origin", branch],
            )
        return bool(result.stdout.strip())

    # --------------------------------------------------------- reconciliation

    def reconcile_publish_pending(self) -> list[str]:
        """Finish publication for parked tasks. Cheap, so it can run every poll.

        Called by the worker on each poll, unlike :meth:`reconcile`, which walks every
        task's branch and remote and therefore runs once at startup. This one touches
        only tasks that are already known to be publish-pending (`needs_attention`
        with a publishable stage), and never starts a runtime.

        Without it, a task that becomes publish-pending *while the service is running*
        — re-adding `take-it` to a withdrawn task, a manual `unpause`, or
        ``resume-publish`` — would sit untouched until the service restarted, because
        startup reconciliation had already run for this process.

        Only phases named by :data:`~agent_dispatch.store.PUBLISH_RECONCILE_PHASES` are
        touched. A publishable stage survives a manual ``pause`` on purpose, so
        ``is_publish_pending`` alone would make this pass publish work an operator had
        deliberately stopped — silently undoing ``pause`` and pushing to GitHub for a
        task that reports itself as paused.
        """
        notes: list[str] = []
        for task in self.store.list_tasks():
            if task.is_terminal or not task.is_publish_pending:
                continue
            if task.phase not in PUBLISH_RECONCILE_PHASES:
                continue
            task_notes = self._reconcile_publish_pending(task)
            notes.extend(task_notes)
            if task_notes:
                # Publication reached a new conclusion for this task, so its status
                # comment is updated in the same pass — the maintainer should not have
                # to wait another poll to see that recovery succeeded or failed.
                fresh = self.store.get_task(task.repo, task.issue_number)
                if fresh is not None:
                    self.sync_task_status(fresh)
        return notes

    def reconcile(self) -> list[str]:
        """Repair state left by a crash, using GitHub and Git as evidence.

        Called once under the single-instance lock at worker startup (and by the
        explicit ``run`` command) before any dispatch, so a crash cannot leave a task
        permanently stuck. The cases that matter:

        * a ``running`` row is orphaned by definition — the lock guarantees no other
          worker is alive, so a live-looking ``running`` row is always a previous
          process's;
        * work that was already pushed but whose PR was never recorded is published
          **without re-running the agent**;
        * only tasks with no published work at all return to the queue for a bounded
          fresh-session retry.

        The published-work check is what stops a crash during ``git push``/PR creation
        from spending a second model call on work that already exists on the branch.

        The status-comment pass runs afterwards, over every task that owns a comment,
        so a status left by a killed process is re-derived from the repaired state and
        explicitly marked as having replaced a stale heartbeat.
        """
        notes: list[str] = []

        for task in self.store.list_tasks():
            if task.is_terminal:
                continue
            if task.phase == "running":
                notes.extend(self._reconcile_running(task))
            elif task.phase in PUBLISH_RECONCILE_PHASES:
                # A `needs_attention` row may be recoverable: the published-work
                # check decides. It is NOT reset to `queued` here, because that is
                # how a stale-queue row silently spends a second model call.
                #
                # A paused task is deliberately excluded: its publishable stage is
                # preserved so that unpausing can restore publication, and acting on it
                # here would publish work the operator had stopped.
                notes.extend(self._reconcile_publish_pending(task))

        # One status synchronisation for every owned comment, after every task above
        # has its final phase. Parked or interrupted comments are the case a
        # heartbeat structurally cannot cover: the process that owned them is gone.
        notes.extend(self.sync_status_comments(notes=self._stale_notes()))

        if self.config.worker.run_log_dir.is_dir():
            try:
                prune(self.config.worker.run_log_dir, self.config.worker.run_log_keep)
            except OSError as exc:  # pragma: no cover - retention is best effort
                self.log.debug("run_log_prune_failed", error=str(exc))
        return notes

    def _reconcile_running(self, task: Task) -> list[str]:
        """A ``running`` row from a previous process. No agent is started here.

        Closing the open run is unconditional — no process is alive to own it — and it
        happens **before** deciding what to do with the task, because the run outcomes
        are the evidence that decision needs.

        What happens to the task then depends on evidence, not on the phase:

        * an open run means the agent was killed mid-flight, so its worktree may hold
          half-written edits: those are preserved for a bounded fresh-session retry and
          explicitly **not** committed or published, since a branch left by an earlier
          attempt would otherwise make partial work look publishable;
        * no open run, with a completed run and a published branch, is the crash
          *during publish* window, which is finished off publish-only.
        """
        # Capture the completion evidence BEFORE closing the open run: the open run is
        # itself the proof that the agent may have been killed mid-edit, so asking
        # afterwards (when it has already been recorded as failed) would read a
        # different answer than the one that matters. `publishable` is True only when
        # no run was in flight, which is exactly the crash-during-publish window.
        runs = self.store.run_history(task.id)
        open_runs = [run for run in runs if run.outcome == RUN_RUNNING]
        newest = runs[-1] if runs else None
        publishable = not open_runs and newest is not None and newest.outcome == RUN_SUCCEEDED

        for run in open_runs:
            self.store.finish_run(
                run.id,
                outcome=RUN_FAILED,
                session_id=run.session_id,
                exit_code=None,
                subtype=None,
                tool_hook_blocked=False,
                timed_out=False,
                produced_work=None,
                detail=INTERRUPTED_RUN_MARKER,
            )

        if not task.branch:
            # Interrupted before a worktree/branch was recorded: nothing can have
            # been published, so this is a plain retry.
            return self._requeue_interrupted(task, "interrupted before a branch was recorded")

        # Whether finished work exists decides publish-only vs. agent-again. This is
        # the crash-during-publish window: the run completed and the branch may be on
        # the remote, so the repair must publish WITHOUT another run.
        recovery = self._recover_publish_only(task, run_completed=publishable)
        if recovery.handled:
            # The task is no longer parked for recovery.
            self.store.clear_recovery_stage(task.id)
            return recovery.notes
        if recovery.status == PUBLISH_UNKNOWN:
            self.store.park_for_recovery(
                task.id,
                stage=RECOVERY_INTERRUPTED,
                note="interrupted run: could not determine whether its branch was published, so it "
                "was not retried; re-run `agent-dispatch run` when the remote is reachable",
            )
            return recovery.notes or [f"{task.ref}: publish state unknown after an interruption"]

        try:
            repo = self.config.repo(task.repo)
        except Exception:
            self.store.mark_needs_attention(task.id, "repository is no longer allowlisted")
            return [f"{task.ref}: running row left as needs_attention (repo not allowlisted)"]

        # `recovery.notes` explains why publishing was refused (typically "the newest
        # run never completed"), so it is carried into the requeue detail rather than
        # dropped: the operator needs to know the edits were deliberately not published.
        refusal = " ".join(recovery.notes)
        detail = "interrupted run recovered"
        if task.worktree_path:
            manager = self._manager(repo)
            state = manager.inspect(Path(task.worktree_path), task.branch)
            detail = (
                f"interrupted run recovered: previous session {task.session_id or 'unknown'} is not "
                f"resumable (an interrupted first run has no transcript), worktree {state.describe()}"
            )
            if state.produced_work:
                detail += " — its changes are preserved"
        if refusal:
            detail += f". {refusal}"
            self.store.set_phase(task.id, task.phase, refusal)
        return self._requeue_interrupted(task, detail)

    def _requeue_interrupted(self, task: Task, detail: str) -> list[str]:
        """Return an interrupted task to the queue for a bounded fresh-session retry."""
        budget_left = task.attempts < self.config.worker.max_attempts
        self.store.set_phase(task.id, "queued" if budget_left else "failed", detail)
        self.log.warning(
            "running_row_reconciled", repo=task.repo, issue=task.issue_number, detail=detail
        )
        return [f"{task.ref}: {detail}"]

    def _reconcile_publish_pending(self, task: Task) -> list[str]:
        """Finish publishing work that already exists, or escalate honestly.

        Handles the windows that leave a task parked without an owned PR: a crash
        after ``git push``, a failed push, a failed PR lookup/creation, and a run
        whose changes could not be committed. All resolve through the same
        publish-only path, so the dispatcher's promise that ``run`` will finish the
        job without a model call is actually true.

        The decision is driven by the **persisted recovery stage** rather than by the
        phase, because ``needs_attention`` alone cannot distinguish:

        * work that a *completed, validated* run produced and only publishing failed
          for — recoverable locally, no model call; from
        * edits left by an *interrupted* runtime — which must not be committed as
          though they were finished.
        """
        if task.pr_number is not None:
            # Ownership already recorded, so there is nothing to publish. A task left
            # in needs_attention for an unrelated reason is NOT silently cleared.
            #
            # Self-heal: an owned PR means publication *succeeded*, so any surviving
            # publishable stage is provably stale. That combination could be written by
            # the pre-atomic finalisation below (record the PR, crash before clearing
            # the stage), and without this repair it would persist forever — the early
            # return here is exactly what stopped it from ever being cleared, and
            # `describe_task` reads the stage before the PR, so the Issue comment would
            # report "Recovering publication" for finished work indefinitely.
            if task.has_publishable_stage:
                self.store.clear_recovery_stage(task.id)
                return [
                    f"cleared a stale recovery stage ({task.recovery_stage}): PR "
                    f"#{task.pr_number} is already owned, so publication had completed"
                ]
            return []

        result = self._recover_publish_only(task)
        if result.handled:
            return result.notes

        if result.status == PUBLISH_UNKNOWN:
            # Cannot tell whether work was published, or the local state needs a human.
            # Never guess by re-running the model: park the task and say what to do.
            #
            # Parking matters: `needs_attention` is what makes the unresolved state
            # visible in `status` and stops a later poll from treating the row as
            # dispatchable. Leaving the phase alone would hide it.
            #
            # The stage is preserved rather than forced to `interrupted`. "Unknown"
            # does not mean "the runtime was interrupted": it also covers a transient
            # local failure (an `ls-remote` outage, or a commit that failed again) while
            # the evidence that finished work exists is still intact. Overwriting it
            # would drop the very evidence recovery depends on, and the task could then
            # never be pushed again — a one-off outage permanently destroying
            # recoverability. `_recover_publish_only` re-parks a more specific stage
            # when it has one, so this reads the row back instead of assuming.
            self._park_preserving_stage(
                task,
                result.notes,
                fallback=(
                    f"could not determine whether {task.branch} is published; no agent was "
                    "started. Re-run `agent-dispatch run` when the remote is reachable to "
                    "retry publish-only recovery."
                ),
            )
            return result.notes or [f"{task.ref}: publish state unknown; left for a later attempt"]

        if result.status == PUBLISH_ABSENT:
            self.store.mark_needs_attention(
                task.id,
                f"parked as {task.recovery_stage or task.phase} but nothing is published for "
                f"{task.branch}; left for the maintainer rather than re-running the agent",
            )
            return [
                f"{task.ref}: nothing published for {task.branch}; escalated to needs_attention"
            ]
        return []

    def _park_preserving_stage(self, task: Task, notes: list[str], *, fallback: str) -> None:
        """Park a task without discarding a more specific recovery stage.

        `PUBLISH_UNKNOWN` means "this attempt could not finish", which is not the same
        as "the runtime was interrupted". If `_recover_publish_only` already recorded a
        precise stage (`commit_failed`, `push_failed`, `pr_failed`) then that evidence is
        still true and must survive: it is what authorises the next attempt to push.
        Downgrading it to `interrupted` would make the task permanently unrecoverable
        after a single transient failure.

        A task that already has a publishable stage keeps it; anything else takes the
        given fallback stage, which is the honest answer for genuinely ambiguous work.
        """
        current = self.store.get_task(task.repo, task.issue_number)
        stage = (
            current.recovery_stage
            if current is not None and current.has_publishable_stage
            else RECOVERY_INTERRUPTED
        )
        self.store.park_for_recovery(task.id, stage=stage, note=" ".join(notes) or fallback)

    def _recover_publish_only(
        self, task: Task, *, run_completed: bool | None = None
    ) -> "RecoveryResult":
        """Publish work that a completed run produced, without running the agent.

        Two pieces of persisted evidence decide whether that is safe, and both are
        required:

        1. **a completed, validated run** (:meth:`Store.last_completed_run`) — proof
           that the edits in the worktree belong to a finished piece of work rather
           than to a process killed mid-edit; and
        2. **no newer unfinished run** (:meth:`Store.has_unfinished_run`) — the newest
           run is the one that describes what is currently on disk, so an
           interrupted attempt on top of an older success means the contents are not
           trustworthy.

        Local commits and a *parked publish stage* also count, so a first run whose
        orchestrator commit failed is recoverable even though nothing reached the
        remote yet.
        """
        if not task.branch:
            return RecoveryResult(PUBLISH_NOT_APPLICABLE)
        try:
            repo = self.config.repo(task.repo)
        except Exception:
            return RecoveryResult(PUBLISH_NOT_APPLICABLE)

        # Never auto-commit work from an unfinished run: the worktree may hold
        # half-written changes from a process that was killed mid-edit. `run_completed`
        # overrides the query when the caller already knows, which is what keeps the
        # `running` path from reading the run history it has just amended.
        unfinished = (
            not run_completed
            if run_completed is not None
            else self.store.has_unfinished_run(task.id)
        )
        if unfinished and not task.has_publishable_stage:
            detail = (
                f"the newest run for {task.ref} never completed, so the worktree may hold "
                "half-written edits; they are left for a bounded fresh-session retry rather "
                "than committed and published"
            )
            return RecoveryResult(PUBLISH_ABSENT, [f"{task.ref}: {detail}"])

        has_completed = (
            run_completed
            if run_completed is not None
            else self.store.last_completed_run(task.id) is not None
        )
        if not has_completed and not task.has_publishable_stage:
            # No completed run and no recorded publish stage: there is no evidence of
            # finished work, so do not invent any.
            return RecoveryResult(PUBLISH_ABSENT)

        try:
            remote_tip = self._remote_branch_tip(repo, task.branch)
        except GitError as exc:
            self.log.warning(
                "publish_state_unknown", repo=task.repo, issue=task.issue_number, detail=str(exc)
            )
            return RecoveryResult(PUBLISH_UNKNOWN, [f"{task.ref}: {exc}"])

        manager = self._manager(repo)
        worktree = Path(task.worktree_path) if task.worktree_path else None
        if worktree is not None and worktree.is_dir():
            state = manager.inspect(worktree, task.branch)
            if not state.branch_matches:
                detail = (
                    f"cannot publish {task.branch}: worktree {worktree} is checked out on "
                    f"{state.checked_out_branch!r}; refusing to commit or push from the wrong branch"
                )
                self.store.park_for_recovery(task.id, stage=RECOVERY_INTERRUPTED, note=detail)
                return RecoveryResult(PUBLISH_UNKNOWN, [f"{task.ref}: {detail}"])

            if state.dirty:
                # Safe now only because a completed run produced these edits. Commit
                # them so the branch can carry them; `git push` cannot.
                committed, note = manager.commit_all(
                    state.path, state.branch, _recovery_commit_message(task)
                )
                if not committed:
                    detail = (
                        f"cannot publish {task.branch}: committing the preserved changes failed "
                        f"({note}); the edits are preserved in the worktree"
                    )
                    self.store.park_for_recovery(task.id, stage=RECOVERY_COMMIT_FAILED, note=detail)
                    return RecoveryResult(PUBLISH_UNKNOWN, [f"{task.ref}: {detail}"])
                state = manager.inspect(state.path, task.branch)

            if remote_tip is None or (state.head_sha and remote_tip != state.head_sha):
                # Nothing published yet (the failed-first-commit case), or the remote
                # is behind local work (the failed-push case). Both need a push, and
                # both are safe because the commits came from a completed run.
                #
                # Gated on evidence that *this service* was interrupted while
                # publishing: a recorded publish stage, or a `running` row whose run had
                # already completed (the crash-during-push window). `needs_attention`
                # is also where a human parks a task for reasons this code cannot see,
                # and pushing new commits on their behalf is not a decision to make
                # from a phase alone.
                publishing_interrupted = task.has_publishable_stage or (
                    run_completed is True and task.phase == "running"
                )
                if not publishing_interrupted:
                    detail = (
                        f"needs_attention: has local commits for {task.branch} but the remote "
                        "does not have them, and no recorded publish failure justifies pushing "
                        "them. Left for the maintainer."
                    )
                    # Parked so the unresolved state is visible in `status` and the row is
                    # not treated as dispatchable; the local commits are kept.
                    self.store.park_for_recovery(task.id, stage=RECOVERY_INTERRUPTED, note=detail)
                    return RecoveryResult(PUBLISH_UNKNOWN, [f"{task.ref}: {detail}"])
                return self._publish_local(task, repo, manager, state)
        else:
            # No local worktree to commit from. Publishing is still possible only when
            # the remote branch genuinely is ours, which the tip comparison below
            # establishes; nothing is ever run against an invented working directory.
            if remote_tip is None:
                return RecoveryResult(PUBLISH_ABSENT)
            state = WorktreeState(
                path=worktree or repo.path,
                branch=task.branch,
                exists=False,
                is_registered_worktree=False,
                remote_branch_exists=True,
            )

        outcome = self._ensure_pull_request(task, repo, state, result=None, notes=[])
        detail = f"publish-only recovery: {outcome.summary()}"
        self.log.warning(
            "publish_only_recovered",
            repo=task.repo,
            issue=task.issue_number,
            action=outcome.action,
            detail=detail,
        )
        return RecoveryResult(PUBLISH_HANDLED, [f"{task.ref}: {detail}", *outcome.notes])

    def _publish_local(
        self,
        task: Task,
        repo: RepoConfig,
        manager: WorktreeManager,
        state: WorktreeState,
    ) -> "RecoveryResult":
        """Push locally-committed work and open its PR. No agent involvement.

        Used when a completed run's changes exist locally but never reached the remote
        — the failed-first-commit case, where the earlier recovery could not help
        because it only looked for a remote branch and so called finished work
        \"absent\".
        """
        outcome = self._publish(task, repo, manager, state, result=None)
        detail = f"publish-only recovery from local commits: {outcome.summary()}"
        self.log.warning(
            "publish_only_recovered_local",
            repo=task.repo,
            issue=task.issue_number,
            action=outcome.action,
            detail=detail,
        )
        if outcome.action in {OUTCOME_PR_READY, OUTCOME_ADOPTED}:
            return RecoveryResult(PUBLISH_HANDLED, [f"{task.ref}: {detail}", *outcome.notes])
        if outcome.action == OUTCOME_FAILED:
            return RecoveryResult(PUBLISH_ABSENT, [f"{task.ref}: {detail}", *outcome.notes])
        return RecoveryResult(PUBLISH_UNKNOWN, [f"{task.ref}: {detail}", *outcome.notes])

    def _manager(self, repo: RepoConfig) -> WorktreeManager:
        """A WorktreeManager for ``repo``, configured exactly like dispatch uses."""
        return WorktreeManager(
            self.git,
            source_path=repo.path,
            worktree_root=self.config.worker.worktree_root,
            base_branch=repo.base_branch,
            repo_slug=repo.slug,
            credential_helper=self.config.github.credential_helper,
            write_repo_local_config=self.config.worker.write_repo_local_credentials,
            commit_identity=(
                self.config.worker.commit_identity_name,
                self.config.worker.commit_identity_email,
            ),
        )


# --------------------------------------------------------------------- helpers


def _linked_pr(pulls: list, repo: str, issue_number: int):
    matches = [pr for pr in pulls if pr.references_issue(repo, issue_number)]
    if not matches:
        return None
    open_matches = [pr for pr in matches if pr.state == "open" and not pr.merged]
    if open_matches:
        return max(open_matches, key=lambda pr: pr.number)
    return max(matches, key=lambda pr: pr.number)


def _pr_state(pr) -> str:
    return "merged" if pr.merged else pr.state


def _pr_title(task: Task) -> str:
    title = (task.title or f"Issue #{task.issue_number}").strip()
    return title if len(title) <= 250 else title[:249] + "…"


def _pr_body(task: Task, result: RunResult | None, state: WorktreeState, repo: RepoConfig) -> str:
    """An honest PR summary: what changed, what was validated, what was not.

    The agent's own final text is included as **quoted task output**, clearly
    attributed, rather than presented as the orchestrator's verified claims. The
    orchestrator only asserts what it observed itself.

    ``result=None`` means crash recovery created this PR rather than a fresh run, and
    the body says so instead of implying a validation this process never performed.
    """
    subjects = (
        "\n".join(f"- {line}" for line in state.committed_subjects) or "- (no commits listed)"
    )

    if result is None:
        validation = (
            "This pull request was created by the dispatcher's crash recovery: the branch had "
            "already been pushed by an earlier run, but the pull request had not been recorded "
            "before the process stopped. **No fresh validation was performed for this pull "
            "request.** The run that produced the branch is recorded in the task history "
            "(inspect it with `agent-dispatch open`)."
        )
        agent_section = "_No run summary is attached to a recovered pull request._"
        session = task.session_id or "unknown"
    else:
        validation = (
            "Observed by the dispatcher:\n\n"
            "- Command Code run completed cleanly "
            f"(`subtype={result.subtype}`, exit `{result.exit_code}`).\n"
            "- No `tool_hook_blocked` events: every tool call the agent attempted was permitted.\n"
            f"- Produced work against `{repo.base_branch}`: {state.describe()}.\n\n"
            "**Not verified by the dispatcher:** the agent's own claims about tests and checks "
            "below were not independently re-run by the orchestrator. Treat them as the agent's "
            "report."
        )
        agent_summary = result.final_text.strip()
        if len(agent_summary) > 6000:
            agent_summary = agent_summary[:6000] + "\n… (truncated)"
        agent_section = f"```\n{agent_summary or '(the runtime reported no final text)'}\n```"
        session = result.session_id or "unknown"

    sections = [
        # Both a human-readable reference and a repo-qualified URL. The URL is not
        # decoration: it is what makes the Issue link machine-verifiable later, so a
        # PR the dispatcher created can be recognised as this task's own (see
        # `PullRequest.references_issue_in_text`). A bare `#N` is not enough evidence
        # -- it also appears in prose -- so the full URL is always included.
        f"Implements #{task.issue_number} ({_issue_url(repo.slug, task.issue_number)}).",
        "",
        "## Commits",
        "",
        subjects,
        "",
        "## Validation",
        "",
        validation,
        "",
        "## Agent's final summary (unverified task output)",
        "",
        agent_section,
        "",
        f"Session: `{session}` · model `{repo.runtime.model}` · "
        "run log kept outside the repository.",
    ]
    return "\n".join(sections)


def _issue_url(repo: str, issue_number: int) -> str:
    """The canonical GitHub URL for an Issue, used in PR bodies and prompts."""
    return f"https://github.com/{repo}/issues/{issue_number}"


def _commit_message(task: Task, issue: Issue | None) -> str:
    subject = (
        task.title or (issue.title if issue else None) or f"issue {task.issue_number}"
    ).strip()
    if len(subject) > 68:
        subject = subject[:67] + "…"
    return (
        f"{subject}\n\n"
        f"Implemented by agent-dispatch for #{task.issue_number}.\n"
        "The agent left these changes uncommitted; the dispatcher committed them so the branch "
        "could be pushed. See the pull request for the run's own summary.\n"
    )


def _recovery_commit_message(task: Task) -> str:
    """Message for a commit made by publish-only recovery, not by a fresh run.

    Says so explicitly: the operator reading `git log` should be able to tell that
    this commit was made while finishing an interrupted publish, not by an agent run
    that just happened.
    """
    subject = (task.title or f"issue {task.issue_number}").strip()
    if len(subject) > 68:
        subject = subject[:67] + "…"
    return (
        f"{subject}\n\n"
        f"Implemented by agent-dispatch for #{task.issue_number}.\n"
        "Committed by publish-only crash recovery: an earlier run left these changes and the "
        "process ended before they were committed and published. No new agent run was made.\n"
    )

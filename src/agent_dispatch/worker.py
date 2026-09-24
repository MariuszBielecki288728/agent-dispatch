"""The polling worker.

One process, one SQLite database, one single-instance lock, at most **one active
task total** (``docs/architecture.md`` §3, §8). The loop:

```
reconcile + discover: take-it Issues for every allowlisted repo
persist queue state (idempotent)
if a task is eligible and nothing is running: execute it through to one PR   (#4)
sleep(poll_interval_seconds)
```

Issue #3 established that discovery alone queues nothing to GitHub and never
executes. Issue #4 adds execution, but keeps the two responsibilities separate:
this module still owns polling and reporting, while every decision about *running*
an agent lives in :class:`~agent_dispatch.orchestrator.Orchestrator` — including
the atomic claim, so a stale poll can never authorize a run.

``reconcile=False`` (used by ``dry-run``/``status``) performs the poll and reports
what *would* be dispatched, but never starts an agent. That guarantee comes from
the read-only scratch store, not from this flag: the flag decides whether to ask
for execution at all, while the store makes the write impossible.
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field

from .config import Config
from .discovery import Discovery, DiscoveryResult
from .github import GitHubClient, GitHubError
from .logging_setup import Logger
from .orchestrator import DispatchOutcome, Orchestrator
from .runlogs import prune
from .runtime import CommandCodeDriver, RuntimeSpawnError
from .store import Store, phase_summary


@dataclass
class PollOutcome:
    """Everything one poll observed, for logging and for ``dry-run`` output."""

    result: DiscoveryResult | None = None
    error: str | None = None
    error_kind: str | None = None
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)
    dispatchable: list[str] = field(default_factory=list)
    #: Set only when this poll actually executed a task (never in a simulated poll).
    dispatch: DispatchOutcome | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and (self.result is None or self.result.ok)


class Worker:
    """Discovery/reconciliation loop over an explicit store."""

    def __init__(
        self,
        config: Config,
        store: Store,
        log: Logger,
        *,
        reconcile: bool = True,
        execute: bool = False,
        client: GitHubClient | None = None,
    ) -> None:
        self.config = config
        self.store = store
        self.log = log
        #: ``reconcile=False`` means "simulate only": the poll still computes every
        #: decision, but it must be handed a scratch store so nothing durable is
        #: written. It does **not** disable store writes by itself — the read-only
        #: store is the guarantee. Previously named ``dry_run``, which implied a
        #: safety it did not provide on its own.
        self.reconcile = reconcile
        #: Whether an eligible task may actually be executed. **OFF by default**, so
        #: that discovering, testing or embedding this loop cannot spend model
        #: credits or mutate GitHub as a side effect of a poll. The operator-facing
        #: ``worker`` command turns it on explicitly, which is where the intent to
        #: run an agent is actually expressed.
        self.execute = execute and reconcile
        self._client = client
        self._stop = False

    # -------------------------------------------------------------- the loop

    def run_forever(self) -> int:
        """Poll until interrupted. Returns a process exit code."""
        self._install_signal_handlers()
        interval = self.config.worker.poll_interval_seconds
        self.log.info(
            "worker_started",
            repos=len(self.config.repos),
            interval_seconds=interval,
            trigger_label=self.config.github.trigger_label,
            max_concurrent_tasks=self.config.worker.max_concurrent_tasks,
            reconcile=self.reconcile,
        )

        while not self._stop:
            self.poll_once()
            if self._stop:
                break
            # Sleep in short slices so SIGTERM/SIGINT are honoured promptly.
            slept = 0.0
            while slept < interval and not self._stop:
                slice_seconds = min(1.0, interval - slept)
                time.sleep(slice_seconds)
                slept += slice_seconds

        self.log.info("worker_stopped")
        return 0

    def poll_once(self) -> PollOutcome:
        """A single discovery + reconcile cycle, then at most one agent run."""
        started = time.monotonic()
        before = self.store.count_by_phase()
        client = self._client or GitHubClient(self.config.github.command, timeout_seconds=60.0)

        # Preflight the one thing that can be verified locally and cheaply: the
        # wrapper exists and is executable. A missing wrapper is a configuration
        # fault, so it is reported once at the top level rather than repeated per
        # repository, and no task state is touched.
        try:
            client.check_available()
        except GitHubError as exc:
            self.log.error("poll_failed", kind=exc.kind, retryable=exc.retryable, error=str(exc))
            return PollOutcome(error=str(exc), error_kind=exc.kind, before=before, after=before)

        discovery = Discovery(self.config, self.store, client, self.log)

        try:
            result = discovery.poll_once()
        except GitHubError as exc:
            # A preflight-level failure (missing wrapper, auth, network). Reported
            # honestly; every existing task is left exactly as it was.
            self.log.error("poll_failed", kind=exc.kind, retryable=exc.retryable, error=str(exc))
            return PollOutcome(error=str(exc), error_kind=exc.kind, before=before, after=before)

        after = self.store.count_by_phase()
        tasks = self.store.list_tasks()
        dispatchable = [task.ref for task in tasks if task.dispatchability()[0]]

        for repo_outcome in result.repos:
            if repo_outcome.failed:
                self.log.warning(
                    "repo_skipped",
                    repo=repo_outcome.slug,
                    kind=repo_outcome.error_kind,
                    error=repo_outcome.error,
                )

        self.log.info(
            "poll_complete",
            duration_ms=int((time.monotonic() - started) * 1000),
            queued=sum(outcome.queued for outcome in result.repos),
            requeued=sum(outcome.requeued for outcome in result.repos),
            existing_pr=sum(outcome.skipped_has_pr for outcome in result.repos),
            withdrawn=sum(outcome.trigger_withdrawn for outcome in result.repos),
            finished=sum(outcome.finished for outcome in result.repos),
            failed_repos=len(result.failed_repos),
            phases=phase_summary(after),
            dispatchable=len(dispatchable),
        )

        if dispatchable and self.store.active_task_count() == 0 and not self.reconcile:
            # A simulated poll reports what it would do; it never does it.
            self.log.info(
                "dispatch_simulated",
                reason="simulated poll: no agent is started and no state is written",
                tasks=",".join(dispatchable[:10]),
            )

        dispatch: DispatchOutcome | None = None
        if self.execute and dispatchable and self.store.active_task_count() == 0:
            # A missing runtime is a *configuration* fault, exactly like a missing
            # GitHub wrapper: it is reported once and touches no task state. Without
            # this check the first task would be claimed and marked `failed` for a
            # reason that is not about the task at all.
            runtime_problem = self._runtime_preflight()
            if runtime_problem:
                self.log.error("dispatch_unavailable", detail=runtime_problem)
            else:
                # The decision to run belongs to the orchestrator, which re-validates
                # against GitHub and claims the task under a conditional transition.
                # This list is only a hint that something *might* be runnable.
                dispatch = Orchestrator(self.config, self.store, client, self.log).dispatch_next()
                self.log.info("dispatch_result", action=dispatch.action, detail=dispatch.summary())

        if self.reconcile:
            try:
                removed = prune(self.config.worker.run_log_dir, self.config.worker.run_log_keep)
                if removed:
                    self.log.debug("run_logs_pruned", count=len(removed))
            except OSError as exc:  # pragma: no cover - retention is best effort
                self.log.debug("run_log_prune_failed", error=str(exc))

        return PollOutcome(
            result=result,
            before=before,
            after=self.store.count_by_phase(),
            dispatchable=dispatchable,
            dispatch=dispatch,
        )

    def _runtime_preflight(self) -> str | None:
        """Why the agent runtime cannot be started right now, or ``None``.

        Uses the driver's own resolved binary rather than a bare ``which``, so this
        check cannot disagree with what an actual run would try to spawn.
        """
        try:
            CommandCodeDriver(
                next(iter(self.config.repos.values())).runtime,
                run_log_dir=self.config.worker.run_log_dir,
                repo="preflight",
                issue_number=0,
                binary=self.config.worker.commandcode_path or "commandcode",
            ).check_available()
        except RuntimeSpawnError as exc:
            return str(exc)
        except StopIteration:  # pragma: no cover - configuration forbids zero repos
            return "no repositories configured"
        return None

    # ------------------------------------------------------------- signals

    def _install_signal_handlers(self) -> None:
        def handler(signum: int, _frame: object) -> None:
            self.log.info("signal_received", signal=signal.Signals(signum).name)
            self._stop = True

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except ValueError:  # pragma: no cover - not on the main thread
                pass

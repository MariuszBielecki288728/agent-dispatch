"""The polling worker.

One process, one SQLite database, one single-instance lock, at most **one active
task total** (``docs/architecture.md`` §3, §8). The loop:

```
reconcile + discover: take-it Issues for every allowlisted repo
persist queue state (idempotent)
report what is queued and why nothing is dispatched
sleep(poll_interval_seconds)
```

Issue #3 is explicit that discovery and queueing **do not** dispatch anything.
The phases this release can produce are ``queued``, ``paused``, ``finished`` and
``needs_attention``; ``running``/``awaiting_review`` belong to #4, and ``status``
says so rather than implying an implementation exists.

The worker does not own the lock or the store: the caller (CLI or tests) does.
That keeps the loop testable and makes "two workers cannot share one lock"
provable without a second loop implementation.
"""

from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field

from .config import Config
from .discovery import Discovery, DiscoveryResult
from .github import GitHubClient, GitHubError
from .logging_setup import Logger
from .runlogs import prune
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

    @property
    def ok(self) -> bool:
        return self.error is None and (self.result is None or self.result.ok)


class Worker:
    """Discovery/reconciliation loop over an explicit store."""

    def __init__(self, config: Config, store: Store, log: Logger, *, dry_run: bool = False) -> None:
        self.config = config
        self.store = store
        self.log = log
        self.dry_run = dry_run
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
            dry_run=self.dry_run,
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
        """A single discovery + reconcile cycle. Never starts an agent."""
        started = time.monotonic()
        before = self.store.count_by_phase()
        client = GitHubClient(self.config.github.command, timeout_seconds=60.0)

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

        if dispatchable and self.store.active_task_count() == 0 and not self.dry_run:
            # Truthful statement of scope: this release queues, it does not execute.
            self.log.info(
                "dispatch_deferred",
                reason="agent execution is Issue #4 scope; this service only queues",
                tasks=",".join(dispatchable[:10]),
            )

        if not self.dry_run:
            try:
                removed = prune(self.config.worker.run_log_dir, self.config.worker.run_log_keep)
                if removed:
                    self.log.debug("run_logs_pruned", count=len(removed))
            except OSError as exc:  # pragma: no cover - retention is best effort
                self.log.debug("run_log_prune_failed", error=str(exc))

        return PollOutcome(result=result, before=before, after=after, dispatchable=dispatchable)

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

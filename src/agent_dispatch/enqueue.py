"""Explicit one-Issue enqueue.

``agent-dispatch enqueue`` exists so an operator does not have to wait for the next
poll — **not** so it can apply looser rules than the poll does.

Eligibility and persistence both live in
:meth:`~agent_dispatch.discovery.Discovery.evaluate_and_record`, which the polling
worker calls for every labelled Issue and which this command calls for the single
Issue it was given. Every guard the worker applies therefore applies here for free:

* a PR number is refused rather than queued as an Issue,
* a closed Issue is refused,
* a missing trigger label is refused,
* a pre-existing linked PR prevents a duplicate implementation task,
* a foreign PR is recorded as an observation and never claimed as owned,
* a truncated PR listing fails closed instead of queueing.

Nothing is written to GitHub, and the trigger label is never applied silently.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import Config, RepoConfig
from .discovery import ACCEPTED_ACTIONS, ACTION_ALREADY_QUEUED, ACTION_OBSERVED_PR, ACTION_OWN_PR_RECONCILED, Discovery
from .github import GitHubClient, GitHubError
from .logging_setup import Logger
from .store import Store, Task


@dataclass(frozen=True)
class EnqueueOutcome:
    """Result of an explicit enqueue attempt."""

    accepted: bool
    task: Task | None
    message: str
    created: bool = False
    reason: str | None = None


def enqueue_issue(
    config: Config,
    store: Store,
    log: Logger,
    repo: RepoConfig,
    issue_number: int,
    *,
    client: GitHubClient,
) -> EnqueueOutcome:
    """Inspect exactly one Issue and report the shared decision's outcome."""
    discovery = Discovery(config, store, client, log)

    try:
        record = discovery.inspect_issue(repo, issue_number)
    except GitHubError as exc:
        # Includes an unreadable Issue and a truncated PR listing. Either way the
        # answer is "not queued", never "queued anyway".
        log.error(
            "enqueue_unavailable",
            repo=repo.slug,
            issue=issue_number,
            kind=exc.kind,
            error=str(exc),
        )
        return EnqueueOutcome(
            False,
            store.get_task(repo.slug, issue_number),
            f"{repo.slug}#{issue_number}: not queued — {exc.kind}: {exc}",
            reason=exc.kind,
        )

    task = store.get_task(repo.slug, issue_number)
    verdict = record.verdict

    if record.action in ACCEPTED_ACTIONS:
        just_created = record.action != ACTION_ALREADY_QUEUED
        log.info(
            "issue_enqueued" if just_created else "issue_already_queued",
            repo=repo.slug,
            issue=issue_number,
            phase=task.phase if task is not None else None,
        )
        if just_created:
            message = f"{repo.slug}#{issue_number}: queued"
        else:
            message = f"{repo.slug}#{issue_number}: already queued (row reused)"
        return EnqueueOutcome(True, task, message, created=just_created)

    if record.action in {ACTION_OWN_PR_RECONCILED, ACTION_OBSERVED_PR}:
        log.error("enqueue_rejected", repo=repo.slug, issue=issue_number, detail=verdict.message)
        return EnqueueOutcome(False, task, f"{repo.slug}#{issue_number}: {verdict.message}", reason=verdict.reason)

    detail = record.note or verdict.message
    if task is None:
        detail += " (no task row was created)"
    else:
        detail += f" (existing row kept as {task.phase}; existing work is not duplicated)"
    log.error("enqueue_rejected", repo=repo.slug, issue=issue_number, detail=detail)
    return EnqueueOutcome(False, task, f"{repo.slug}#{issue_number}: {detail}", reason=verdict.reason)

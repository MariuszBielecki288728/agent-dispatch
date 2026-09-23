"""Discovery and reconciliation.

Reads allowlisted repositories for Issues labelled ``take-it`` and reconciles
them against the durable queue. Rules implemented here come straight from the
approved trigger protocol (§7 of ``docs/architecture.md``) and Issue #3:

* Poll **only** repositories present in the allowlist.
* Enumerate *already-labelled* open Issues on every poll, not just new events, so
  a restart rediscovers work instead of replaying a stream.
* Distinguish Issues from PR objects (GitHub's issues endpoint returns both).
* An Issue that already has a relevant PR must **not** create a new
  implementation task.
* Removing ``take-it`` prevents dispatch without deleting the row; re-adding it
  reuses the same row.
* A closed Issue ends the task.
* **Never** promote a task to ``running``: discovery is not implementation.

Reconciliation is deliberately a full re-derivation of local state from GitHub on
each poll (bounded by the allowlist), rather than an event log. That is what makes
"restart with previously labelled Issues" and "duplicate poll" behave identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .config import Config, RepoConfig
from .github import MAX_PAGES, GitHubClient, GitHubError, Issue, PullRequest
from .logging_setup import Logger
from .store import Store, Task


@dataclass
class RepoDiscovery:
    """Outcome of polling one repository."""

    slug: str
    queued: int = 0
    requeued: int = 0
    adopted: int = 0
    own_pr_reconciled: int = 0
    skipped_has_pr: int = 0
    trigger_withdrawn: int = 0
    finished: int = 0
    unresolved: int = 0
    error: str | None = None
    error_kind: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.error is not None


@dataclass
class DiscoveryResult:
    repos: list[RepoDiscovery] = field(default_factory=list)

    @property
    def failed_repos(self) -> list[RepoDiscovery]:
        return [repo for repo in self.repos if repo.failed]

    @property
    def ok(self) -> bool:
        return not self.failed_repos


class Discovery:
    def __init__(self, config: Config, store: Store, client: GitHubClient, log: Logger) -> None:
        self.config = config
        self.store = store
        self.client = client
        self.log = log

    # ------------------------------------------------------------------ entry

    def poll_once(self) -> DiscoveryResult:
        """One full discovery pass over the allowlist."""
        result = DiscoveryResult()
        for slug in sorted(self.config.repos):
            result.repos.append(self._poll_repo(self.config.repo(slug)))
        return result

    # ------------------------------------------------------------ per-repo

    def _poll_repo(self, repo: RepoConfig) -> RepoDiscovery:
        outcome = RepoDiscovery(slug=repo.slug)
        trigger = self.config.github.trigger_label

        try:
            issues = self.client.list_issues_with_label(repo.slug, trigger)
        except GitHubError as exc:
            # Honest failure: report it and leave every existing task untouched.
            # A failed poll must never mark an Issue queued, running or completed.
            self._report_repo_failure(outcome, repo, exc)
            return outcome

        # Pull data is only ever consulted for an Issue that is currently
        # labelled, so when nothing is labelled the query is skipped. This is
        # exactly equivalent for decisions, and keeps an idle repository's poll
        # from re-listing every PR every interval.
        #
        # It is also fail-closed by construction: if the PR query fails, the
        # whole repo poll is reported as failed rather than queueing an Issue
        # whose pre-existing PR could not be ruled out.
        pulls: list[PullRequest] = []
        if issues:
            try:
                pulls = self.client.list_pulls(repo.slug)
            except GitHubError as exc:
                self._report_repo_failure(outcome, repo, exc)
                return outcome

            if self.client.pr_scan_truncated:
                outcome.notes.append(
                    f"{repo.slug}: PR listing hit the {MAX_PAGES}-page cap, so a pre-existing PR for a "
                    "labelled Issue may not have been detected. Reduce the repository's open/closed PR "
                    "volume or raise the per-page size; the worker will not guess."
                )
                self.log.warning("pr_scan_truncated", repo=repo.slug, pages=MAX_PAGES)

        self._log_pagination_note(repo, issues, pulls)

        open_issue_numbers = {issue.number for issue in issues}

        for issue in sorted(issues, key=lambda item: item.number):
            linked = _linked_pr(pulls, repo.slug, issue.number)
            adopted = self.store.get_task(repo.slug, issue.number)
            # A PR recorded against this task is the one a future #4 run created;
            # anything else is pre-existing work that must not be scheduled again.
            is_own_pr = task_has_own_pr(adopted, linked)
            if linked is not None and is_own_pr:
                # This task already owns the PR (recorded by a future #4 run):
                # refresh the observation and leave the task row untouched.
                outcome.own_pr_reconciled += 1
                self.store.record_observation(
                    adopted.id,  # type: ignore[union-attr]
                    trigger_present=True,
                    issue_state=issue.state,
                    linked_pr_number=linked.number,
                    linked_pr_state=_pr_state(linked),
                )
                continue
            if linked is not None and not is_own_pr:
                # Pre-existing work: do not schedule an implementation task.
                if adopted is None:
                    created = self.store.upsert_discovered(
                        repo=repo.slug,
                        issue_number=issue.number,
                        title=issue.title,
                        base_branch=repo.base_branch,
                        runtime_driver=repo.runtime.driver,
                        runtime_model=repo.runtime.model,
                        runtime_effort=repo.runtime.effort,
                        permission_mode=repo.runtime.permission_mode,
                        trigger_present=True,
                        issue_state=issue.state,
                        linked_pr_number=linked.number,
                        linked_pr_state=_pr_state(linked),
                        phase="awaiting_review",
                        status_error=(
                            f"pre-existing PR #{linked.number} ({_pr_state(linked)}) already exists for this "
                            "Issue: no new implementation task was created"
                        ),
                    )
                    if created:
                        outcome.skipped_has_pr += 1
                        self.log.info(
                            "existing_pr_adopted",
                            repo=repo.slug,
                            issue=issue.number,
                            pr=linked.number,
                            pr_state=_pr_state(linked),
                        )
                else:
                    if adopted.pr_number != linked.number:
                        self.store.record_pr_adopted(
                            adopted.id,
                            linked.number,
                            f"pre-existing PR #{linked.number} ({_pr_state(linked)}) detected for this Issue",
                        )
                    outcome.adopted += 1
                continue

            task = self.store.get_task(repo.slug, issue.number)
            before_phase = task.phase if task is not None else None

            created = self.store.upsert_discovered(
                repo=repo.slug,
                issue_number=issue.number,
                title=issue.title,
                base_branch=repo.base_branch,
                runtime_driver=repo.runtime.driver,
                runtime_model=repo.runtime.model,
                runtime_effort=repo.runtime.effort,
                permission_mode=repo.runtime.permission_mode,
                trigger_present=True,
                issue_state=issue.state,
                linked_pr_number=None,
                linked_pr_state=None,
            )
            if created:
                outcome.queued += 1
                self.log.info(
                    "issue_queued",
                    repo=repo.slug,
                    issue=issue.number,
                    title=_short(issue.title),
                    phase="queued",
                )
            elif before_phase == "paused" and self.store.release_label_withdrawn_pause(task.id):
                # The label came back and the task had been suspended by *this*
                # rule (not by a maintainer pause), so it becomes dispatchable
                # again using the existing row, branch and worktree.
                outcome.requeued += 1
                self.log.info(
                    "trigger_restored",
                    repo=repo.slug,
                    issue=issue.number,
                    phase="queued",
                )
            elif before_phase == "needs_attention":
                outcome.notes.append(
                    f"#{issue.number}: already recorded as needs_attention; leaving it for the maintainer"
                )

        # Known tasks whose Issue is no longer returned by the trigger-label query
        # need their label state checked before anything is decided.
        known = [task for task in self.store.list_tasks(repo.slug) if task.issue_number not in open_issue_numbers]
        for task in known:
            self._reconcile_withdrawn(repo, task, outcome)

        return outcome

    def _reconcile_withdrawn(self, repo: RepoConfig, task: Task, outcome: RepoDiscovery) -> None:
        """Handle a known task whose Issue no longer carries the trigger label.

        Distinguishes *label removed* (intent withdrawn → suspend, keep the row)
        from *Issue closed* (work ended → terminal). Never deletes the row, so
        re-adding the label reuses the same task and cannot create a duplicate.
        """
        if task.phase == "finished":
            return
        try:
            issue = self.client.open_issue(repo.slug, task.issue_number)
        except GitHubError as exc:
            if exc.kind == "denied_repo":
                outcome.notes.append(f"#{task.issue_number}: Issue not readable ({exc.kind}); left unchanged")
                self._flag_needs_attention(task, f"Issue unreadable during reconciliation: {exc}")
            else:
                # Transient: leave the task exactly as it was and try again next poll.
                outcome.notes.append(
                    f"#{task.issue_number}: could not verify state ({exc.kind}); task left unchanged"
                )
            return

        trigger_present = issue.has_label(self.config.github.trigger_label)

        if issue.state == "closed":
            self.store.upsert_discovered(
                repo=repo.slug,
                issue_number=task.issue_number,
                title=issue.title,
                base_branch=repo.base_branch,
                runtime_driver=task.runtime_driver,
                runtime_model=task.runtime_model,
                runtime_effort=task.runtime_effort,
                permission_mode=task.permission_mode,
                trigger_present=trigger_present,
                issue_state="closed",
                linked_pr_number=task.linked_pr_number,
                linked_pr_state=task.linked_pr_state,
            )
            self.store.mark_finished(task.id, f"Issue closed ({issue.url})")
            outcome.finished += 1
            self.log.info("issue_closed", repo=repo.slug, issue=task.issue_number)
            return

        if not trigger_present:
            self.store.upsert_discovered(
                repo=repo.slug,
                issue_number=task.issue_number,
                title=issue.title,
                base_branch=repo.base_branch,
                runtime_driver=task.runtime_driver,
                runtime_model=task.runtime_model,
                runtime_effort=task.runtime_effort,
                permission_mode=task.permission_mode,
                trigger_present=False,
                issue_state=issue.state,
                linked_pr_number=task.linked_pr_number,
                linked_pr_state=task.linked_pr_state,
            )
            if task.phase != "paused":
                self.store.pause_for_withdrawn_label(
                    task.id,
                    f"label '{self.config.github.trigger_label}' removed: dispatch intent withdrawn "
                    f"(existing work kept; re-adding the label reuses this task) — {issue.url}",
                )
                outcome.trigger_withdrawn += 1
                self.log.info(
                    "trigger_withdrawn",
                    repo=repo.slug,
                    issue=task.issue_number,
                    label=self.config.github.trigger_label,
                )
            return

        # Label is present but the labelled query did not return it: a paging or
        # filtering surprise. Report instead of guessing.
        self.store.upsert_discovered(
            repo=repo.slug,
            issue_number=task.issue_number,
            title=issue.title,
            base_branch=repo.base_branch,
            runtime_driver=task.runtime_driver,
            runtime_model=task.runtime_model,
            runtime_effort=task.runtime_effort,
            permission_mode=task.permission_mode,
            trigger_present=True,
            issue_state=issue.state,
            linked_pr_number=task.linked_pr_number,
            linked_pr_state=task.linked_pr_state,
        )
        outcome.unresolved += 1
        outcome.notes.append(
            f"#{task.issue_number}: labelled '{self.config.github.trigger_label}' but absent from the "
            "labelled query — check pagination"
        )

    # ---------------------------------------------------------------- helpers

    def _report_repo_failure(self, outcome: RepoDiscovery, repo: RepoConfig, exc: GitHubError) -> None:
        """Record a repository-level failure without touching any task state.

        A failed poll must never mark an Issue queued, running or completed, and
        must never be retried under another identity. Existing rows keep their
        phase and are simply re-examined on the next poll.
        """
        outcome.error = str(exc)
        outcome.error_kind = exc.kind
        self.log.error(
            "discovery_failed",
            repo=repo.slug,
            kind=exc.kind,
            retryable=exc.retryable,
            error=str(exc),
        )

    def _flag_needs_attention(self, task: Task, note: str) -> None:
        if task.phase != "needs_attention":
            self.store.mark_needs_attention(task.id, note)
            self.log.warning("needs_attention", repo=task.repo, issue=task.issue_number, reason=note)

    def _log_pagination_note(self, repo: RepoConfig, issues: list[Issue], pulls: list[PullRequest]) -> None:
        per_page = self.client.per_page
        if len(issues) and len(issues) % per_page == 0:
            self.log.warning(
                "pagination_boundary",
                repo=repo.slug,
                issues=len(issues),
                note="issue count is an exact multiple of per_page; verify the next page is empty",
            )


def task_has_own_pr(task: Task | None, linked: PullRequest | None) -> bool:
    """Whether ``linked`` is the PR this task already owns.

    Ownership is only ever *recorded*, never inferred from a directory or branch
    name — matching the design's rule that ownership lives in the task row. A PR
    recorded in the row is ours; anything else is foreign (pre-existing) work.
    """
    if task is None or linked is None:
        return False
    return task.pr_number == linked.number


def _linked_pr(pulls: list[PullRequest], repo: str, issue_number: int) -> PullRequest | None:
    matches = [pr for pr in pulls if pr.references_issue(repo, issue_number)]
    if not matches:
        return None
    # Prefer an open PR; otherwise the highest-numbered historical one.
    open_matches = [pr for pr in matches if pr.state == "open" and not pr.merged]
    if open_matches:
        return max(open_matches, key=lambda pr: pr.number)
    return max(matches, key=lambda pr: pr.number)


def _pr_state(pr: PullRequest) -> str:
    if pr.merged:
        return "merged"
    return pr.state


def _short(text: str) -> str:
    return text if len(text) <= 60 else text[:59] + "…"

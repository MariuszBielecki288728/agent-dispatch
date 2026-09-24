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
from .github import MAX_PAGES, ErrorKind, GitHubClient, GitHubError, Issue, PullRequest
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
    #: Issue number -> why it was not queued. Populated for every labelled Issue
    #: the poll deliberately refused, so `enqueue` can report the same verdict the
    #: poll reached instead of re-deriving one.
    rejections: dict[int, str] = field(default_factory=dict)
    #: Issue numbers for which this poll created the task row. Lets `enqueue`
    #: distinguish "just queued" from "was already known" without guessing.
    queued_issue_numbers: set[int] = field(default_factory=set)

    @property
    def failed(self) -> bool:
        return self.error is not None


@dataclass(frozen=True)
class IssueVerdict:
    """Whether one Issue may be queued, and why not when it may not."""

    queueable: bool
    reason: str
    message: str


@dataclass(frozen=True)
class RecordOutcome:
    """What one :meth:`Discovery.evaluate_and_record` call decided and did.

    ``action`` is a small closed set so callers count outcomes without matching on
    log prose: ``queued``, ``requeued``, ``own_pr_reconciled``, ``observed_pr``,
    ``needs_attention`` and ``unchanged``.
    """

    verdict: IssueVerdict
    action: str
    note: str | None = None


#: Actions returned in :attr:`RecordOutcome.action`.
ACTION_QUEUED = "queued"
ACTION_REQUEUED = "requeued"
ACTION_ALREADY_QUEUED = "already_queued"
ACTION_OWN_PR_RECONCILED = "own_pr_reconciled"
ACTION_OBSERVED_PR = "observed_pr"
ACTION_NEEDS_ATTENTION = "needs_attention"
ACTION_UNCHANGED = "unchanged"

#: Actions meaning the Issue is (or remains) queued for dispatch. `enqueue`
#: reports these as success; the poll treats them as ordinary progress.
ACCEPTED_ACTIONS = frozenset({ACTION_QUEUED, ACTION_REQUEUED, ACTION_ALREADY_QUEUED})


class RejectionReason:
    """Stable reason codes, so callers never match on prose."""

    PULL_REQUEST_OBJECT = "pull_request_object"
    ISSUE_CLOSED = "issue_closed"
    TRIGGER_LABEL_MISSING = "trigger_label_missing"
    PRE_EXISTING_PR = "pre_existing_pr"
    QUEUEABLE = "queueable"


def evaluate_issue(
    *,
    issue: Issue,
    trigger_label: str,
    linked_pr: PullRequest | None,
) -> IssueVerdict:
    """The single decision used to decide whether an Issue may be queued.

    Shared deliberately: the polling worker and the explicit ``enqueue`` command
    must not each grow their own eligibility rules, because a divergence between
    them is how duplicate implementation work gets scheduled (#4 consumes this
    queue). This function decides only; see
    :meth:`Discovery.evaluate_and_record` for the matching persistence.
    """
    if issue.is_pull_request:
        return IssueVerdict(
            False,
            RejectionReason.PULL_REQUEST_OBJECT,
            f"#{issue.number} is a pull request, not an Issue: GitHub's issues endpoint "
            "answers for both, and a PR must never be queued as an Issue",
        )
    if issue.state == "closed":
        return IssueVerdict(
            False,
            RejectionReason.ISSUE_CLOSED,
            f"#{issue.number} is closed ({issue.url}); closed Issues are not queued",
        )
    if not issue.has_label(trigger_label):
        return IssueVerdict(
            False,
            RejectionReason.TRIGGER_LABEL_MISSING,
            f"#{issue.number} does not carry '{trigger_label}'; add the label first — "
            "this service does not apply it silently",
        )
    if linked_pr is not None:
        return IssueVerdict(
            False,
            RejectionReason.PRE_EXISTING_PR,
            f"#{issue.number} already has PR #{linked_pr.number} ({_pr_state(linked_pr)}): "
            "no new implementation task is created",
        )
    return IssueVerdict(True, RejectionReason.QUEUEABLE, f"#{issue.number} may be queued")


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
            result.repos.append(self.poll_repo(slug))
        return result

    def poll_repo(self, slug: str) -> RepoDiscovery:
        """Poll one allowlisted repository. Refuses unlisted slugs."""
        return self._poll_repo(self.config.repo(slug))

    def inspect_issue(self, repo: RepoConfig, issue_number: int) -> RecordOutcome:
        """Address one Issue **by number** and record the decision.

        Needed because the poll enumerates only *labelled* Issues: an operator
        asking about a specific number may name a PR, a closed Issue, or an
        unlabelled one, and each deserves its real reason rather than silence.

        The PR link is resolved only when the Issue itself is otherwise eligible,
        which avoids spending an extra listing on something already refused — and
        ensures a refusal never clears a previously observed PR link.
        """
        issue = self.client.open_issue(repo.slug, issue_number)
        trigger = self.config.github.trigger_label

        if issue.is_pull_request or issue.state == "closed" or not issue.has_label(trigger):
            return self.evaluate_and_record(repo, issue, None, linked_pr_known=False)

        pulls = self.client.list_pulls(repo.slug)
        if self.client.pr_scan_truncated:
            # Fail closed, exactly as the poll does. `enqueue` must never queue an
            # Issue whose pre-existing PR could not be ruled out.
            raise GitHubError(
                f"PR listing for {repo.slug} stopped at the {MAX_PAGES}-page cap, so a "
                f"pre-existing PR for #{issue_number} cannot be ruled out",
                kind=ErrorKind.INCOMPLETE_SCAN,
            )
        linked = _linked_pr(pulls, repo.slug, issue_number)
        return self.evaluate_and_record(repo, issue, linked, linked_pr_known=True)

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
                # Fail closed. A truncated PR listing means a pre-existing PR for a
                # labelled Issue may sit on a page we never read, so queueing now
                # could duplicate existing work. Report and leave every task row
                # exactly as it was — including previously observed PR links.
                self._report_repo_failure(
                    outcome,
                    repo,
                    GitHubError(
                        f"PR listing for {repo.slug} stopped at the {MAX_PAGES}-page cap, so a "
                        "pre-existing PR for a labelled Issue may not have been seen. No Issues "
                        "were queued and no task state was changed. Reduce the repository's pull "
                        "request volume (or raise the wrapper's page size) and let the next poll "
                        "retry.",
                        kind=ErrorKind.INCOMPLETE_SCAN,
                    ),
                )
                return outcome

        self._log_pagination_note(repo, issues, pulls)

        open_issue_numbers = {issue.number for issue in issues}

        for issue in sorted(issues, key=lambda item: item.number):
            linked = _linked_pr(pulls, repo.slug, issue.number)
            record = self.evaluate_and_record(
                repo, issue, linked, linked_pr_known=True
            )
            self._count(outcome, issue, record)

        # Known tasks whose Issue is no longer returned by the trigger-label query
        # need their label state checked before anything is decided.
        known = [task for task in self.store.list_tasks(repo.slug) if task.issue_number not in open_issue_numbers]
        for task in known:
            self._reconcile_withdrawn(repo, task, outcome)

        return outcome

    def _count(self, outcome: RepoDiscovery, issue: Issue, record: RecordOutcome) -> None:
        """Tally one decision into the repository outcome and log it."""
        action = record.action
        if action == ACTION_QUEUED:
            outcome.queued += 1
            outcome.queued_issue_numbers.add(issue.number)
            self.log.info("issue_queued", issue=issue.number, title=_short(issue.title), phase="queued")
        elif action == ACTION_REQUEUED:
            outcome.requeued += 1
            self.log.info("trigger_restored", issue=issue.number, phase="queued")
        elif action == ACTION_ALREADY_QUEUED:
            # Re-discovered on a later poll; already waiting for a dispatcher.
            pass
        elif action == ACTION_OWN_PR_RECONCILED:
            outcome.own_pr_reconciled += 1
        elif action == ACTION_OBSERVED_PR:
            outcome.skipped_has_pr += 1
            outcome.rejections[issue.number] = record.verdict.message
        elif action == ACTION_NEEDS_ATTENTION:
            outcome.notes.append(record.note or record.verdict.message)
        elif record.note is not None:
            outcome.notes.append(record.note)

    # ------------------------------------------------------- shared recording

    def evaluate_and_record(
        self,
        repo: RepoConfig,
        issue: Issue,
        linked_pr: PullRequest | None,
        *,
        linked_pr_known: bool = True,
    ) -> RecordOutcome:
        """Decide eligibility for one Issue **and** persist the decision.

        This is the one implementation of "what happens to this Issue", used by
        both the polling worker (``linked_pr_known=True``, having resolved the PR
        link from a single listing) and the explicit ``enqueue`` command.

        ``linked_pr_known=False`` means "the PR link was not resolved", which is
        what an unlabelled or closed Issue looks like when addressed directly by
        number. The verdict is then still a refusal, but it is derived from the
        Issue itself rather than from an unresolved PR lookup, so the reported
        reason is truthful instead of a guess.

        Ownership is never taken here: ``tasks.pr_number`` is written only by the
        #4 PR-creation/adoption workflow via ``Store.record_pr_ownership``. A PR
        that merely references the Issue is stored as an *observation*, so a later
        review round cannot act on someone else's pull request.
        """
        trigger = self.config.github.trigger_label

        if not linked_pr_known:
            verdict = evaluate_issue(issue=issue, trigger_label=trigger, linked_pr=None)
            if verdict.queueable:
                # Guard against a silent no-op: this mode exists to explain a
                # refusal, and must not be used for an Issue that a PR lookup was
                # supposed to have been performed for.
                raise ValueError(
                    "evaluate_and_record(linked_pr_known=False) requires an Issue that is "
                    "already ineligible; resolve the PR link with linked_pr_known=True"
                )
            return RecordOutcome(verdict, ACTION_UNCHANGED, note=verdict.message)

        existing = self.store.get_task(repo.slug, issue.number)
        is_own_pr = task_has_own_pr(existing, linked_pr)

        if linked_pr is not None and is_own_pr:
            # This task already owns the PR (recorded by #4): refresh the
            # observation and leave the task row itself untouched.
            self.store.record_observation(
                existing.id,  # type: ignore[union-attr]
                trigger_present=issue.has_label(trigger),
                issue_state=issue.state,
                linked_pr_number=linked_pr.number,
                linked_pr_state=_pr_state(linked_pr),
            )
            verdict = evaluate_issue(issue=issue, trigger_label=trigger, linked_pr=None)
            return RecordOutcome(verdict, ACTION_OWN_PR_RECONCILED)

        if linked_pr is not None:
            verdict = evaluate_issue(issue=issue, trigger_label=trigger, linked_pr=linked_pr)
            if existing is None:
                self.store.upsert_discovered(
                    repo=repo.slug,
                    issue_number=issue.number,
                    title=issue.title,
                    base_branch=repo.base_branch,
                    runtime_driver=repo.runtime.driver,
                    runtime_model=repo.runtime.model,
                    runtime_effort=repo.runtime.effort,
                    permission_mode=repo.runtime.permission_mode,
                    trigger_present=issue.has_label(trigger),
                    issue_state=issue.state,
                    linked_pr_number=linked_pr.number,
                    linked_pr_state=_pr_state(linked_pr),
                    phase="awaiting_review",
                    status_error=verdict.message,
                )
                self.log.info(
                    "existing_pr_adopted",
                    repo=repo.slug,
                    issue=issue.number,
                    pr=linked_pr.number,
                    pr_state=_pr_state(linked_pr),
                )
            else:
                self.store.record_observation(
                    existing.id,
                    trigger_present=issue.has_label(trigger),
                    issue_state=issue.state,
                    linked_pr_number=linked_pr.number,
                    linked_pr_state=_pr_state(linked_pr),
                )
            return RecordOutcome(verdict, ACTION_OBSERVED_PR)

        verdict = evaluate_issue(issue=issue, trigger_label=trigger, linked_pr=None)
        if not verdict.queueable:
            # Closed, or the trigger label is gone. `enqueue` reaches this path;
            # the poll normally does not, because it only enumerates labelled
            # Issues. No row is created for an Issue that may not be queued.
            return RecordOutcome(verdict, ACTION_UNCHANGED, note=verdict.message)

        before_phase = existing.phase if existing is not None else None
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
            return RecordOutcome(verdict, ACTION_QUEUED)

        if before_phase == "paused" and self.store.release_label_withdrawn_pause(existing.id):  # type: ignore[union-attr]
            # The label came back and the task had been suspended by *this* rule
            # (not by a maintainer pause), so it becomes dispatchable again using
            # the existing row, branch and worktree.
            return RecordOutcome(verdict, ACTION_REQUEUED)

        if before_phase == "awaiting_review":
            # The PR that previously blocked this Issue is no longer visible. Say
            # so instead of quietly re-queueing: a human decided the earlier state.
            pr = existing.pr_number or existing.linked_pr_number  # type: ignore[union-attr]
            return RecordOutcome(
                verdict,
                ACTION_NEEDS_ATTENTION,
                note=(
                    f"#{issue.number}: previously recorded awaiting_review with PR #{pr}, but no "
                    "linked PR is visible now; left unchanged for the maintainer"
                ),
            )

        if before_phase == "needs_attention":
            return RecordOutcome(
                verdict,
                ACTION_NEEDS_ATTENTION,
                note=f"#{issue.number}: already recorded as needs_attention; left for the maintainer",
            )

        if before_phase == "queued":
            # Idempotent: the row exists and is already waiting for a dispatcher.
            # This is a success for an explicit `enqueue`, not a rejection.
            return RecordOutcome(verdict, ACTION_ALREADY_QUEUED)

        return RecordOutcome(verdict, ACTION_UNCHANGED, note=f"#{issue.number}: already recorded as {before_phase}")

    # ------------------------------------------------------------ per-repo

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

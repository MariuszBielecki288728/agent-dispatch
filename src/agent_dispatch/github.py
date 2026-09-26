"""GitHub adapter.

The **only** sanctioned GitHub access path on this VM is the configured wrapper
command (``github.command``; on this VM ``gh-craftlypse``). Hard rules from the
approved design and Issue #3:

* the wrapper is read from configuration — its historical name is never
  hardcoded into the package;
* no ``gh auth login``, no direct authenticated ``gh``, no token export,
  inspection or printing — this module never touches ``GH_TOKEN`` and never
  passes a credential in an environment dict of its own;
* only allowlisted repositories are polled (enforced by :class:`~agent_dispatch.config.Config`);
* an inaccessible repo, a missing wrapper, an auth failure, a rate limit or a
  network error is reported **honestly** and must never mark an Issue as
  completed, and never silently retried as another identity.

Everything is executed through :meth:`GitHubClient._run`, which is the single
seam the offline test-suite replaces with a fake wrapper executable — so the
tests exercise the real subprocess, pagination and error-classification code
rather than a parallel mock implementation.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

#: Hard cap on pagination so a pathological response cannot loop forever.
MAX_PAGES = 50
DEFAULT_PER_PAGE = 100

#: `dispatch/issue-<N>-<slug>` is the branch naming convention from §4. Until #4
#: creates branches, it is also the cheapest deterministic "this PR is mine" signal.
DISPATCH_BRANCH_RE = re.compile(r"^dispatch/issue-(\d+)(?:-|$)")

_CLOSING_REF_RE = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b[:\s]*"
    r"(?:https?://github\.com/(?P<urlrepo>[\w.-]+/[\w.-]+)/issues/(?P<urlnum>\d+)|#(?P<num>\d+))"
)

_ISSUE_URL_RE = re.compile(r"https?://github\.com/(?P<repo>[\w.-]+/[\w.-]+)/issues/(?P<num>\d+)")


class ErrorKind:
    """Coarse failure classes used for honest reporting and retry policy."""

    MISSING_WRAPPER = "missing_wrapper"
    DENIED_REPO = "denied_repo"
    AUTH = "auth"
    RATE_LIMIT = "rate_limit"
    NETWORK = "network"
    TIMEOUT = "timeout"
    MALFORMED = "malformed_response"
    #: A listing was cut short by :data:`MAX_PAGES`, so an absence of results
    #: cannot be proved. Treated as a failure rather than as "nothing found".
    INCOMPLETE_SCAN = "incomplete_scan"
    UNKNOWN = "unknown"


class GitHubError(Exception):
    """A GitHub operation failed. ``kind`` drives reporting; never a silent success."""

    def __init__(self, message: str, *, kind: str = ErrorKind.UNKNOWN, detail: str = "") -> None:
        self.kind = kind
        self.detail = detail
        super().__init__(message)

    @property
    def retryable(self) -> bool:
        """Rate limits and transient network faults are retried later; denials are not."""
        return self.kind in {ErrorKind.RATE_LIMIT, ErrorKind.NETWORK, ErrorKind.TIMEOUT}


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    state: str
    url: str
    labels: tuple[str, ...] = ()
    #: GitHub's issues endpoint returns pull requests too, distinguished only by a
    #: ``pull_request`` marker. It is captured here because discarding it is how a
    #: PR number gets mistaken for an Issue (see ``open_issue``).
    is_pull_request: bool = False
    #: The Issue's own description. Untrusted task content: it is fenced and
    #: labelled as data by :mod:`agent_dispatch.instruction`, never treated as
    #: operator authorization.
    body: str = ""

    def has_label(self, label: str) -> bool:
        return label in self.labels


@dataclass(frozen=True)
class PullRequest:
    number: int
    state: str
    merged: bool
    head_ref: str
    url: str
    title: str = ""
    body: str = ""
    head_sha: str = ""
    #: ``owner/name`` that hosts the head branch. For a pull request opened from a
    #: fork this is the fork, not the base repository, which is why an exact
    #: branch-name match alone cannot prove a PR is ours.
    head_repo: str = ""
    head_owner: str = ""

    @property
    def head_label(self) -> str:
        """Human-readable head identity, e.g. ``owner:branch`` or just the branch."""
        if self.head_owner and self.head_ref:
            return f"{self.head_owner}:{self.head_ref}"
        return self.head_ref

    def head_ref_matches_owner(self, repo: str) -> bool:
        """Whether this PR's head branch lives in ``repo`` rather than a fork.

        A branch *name* is not unique across GitHub: ``dispatch/issue-7-x`` in a fork
        can collide with ours. Comparing the head repository (and, when reported,
        the owner) is what keeps a fork's pull request from being adopted as this
        task's. Unknown head information fails closed when the base repo is known,
        because adopting the wrong PR is worse than asking a human.
        """
        if not self.head_ref:
            return False
        if not self.head_repo:
            # Older/short payloads omit the head repository. Treat as unproven.
            return False
        return self.head_repo.lower() == repo.lower()

    def references_issue(self, repo: str, issue_number: int) -> bool:
        """Whether this PR appears to implement ``repo#issue_number``.

        Two conservative signals, both requiring a real link rather than a stray
        ``#N`` in prose:

        1. the deterministic dispatch branch name for that Issue, or
        2. a closing keyword / the Issue URL in the PR body.

        Signal 1 makes this **unsuitable for deciding ownership**: a branch name is a
        naming convention, not evidence that a PR implements this Issue. Use
        :meth:`references_issue_in_text` for that, and see its docstring for why the
        distinction matters.
        """
        if DISPATCH_BRANCH_RE.match(self.head_ref or ""):
            match = DISPATCH_BRANCH_RE.match(self.head_ref or "")
            if match and int(match.group(1)) == issue_number:
                return True
        return self.references_issue_in_text(repo, issue_number)

    def references_issue_in_text(self, repo: str, issue_number: int) -> bool:
        """Whether the PR *text* explicitly links to ``repo#issue_number``.

        Deliberately ignores the branch name. For deciding whether a PR is this
        task's own, a branch-name match is the wrong evidence: anyone can push a
        branch called ``dispatch/issue-7-...``, and a PR that merely *sits on* such a
        branch while implementing something else would otherwise be adopted as this
        task's work. Requiring a closing keyword or the Issue URL in the title/body
        makes ownership a claim the PR itself makes.

        Used for ownership decisions. The looser :meth:`references_issue` stays for
        *discovery*, where treating a dispatch-named branch as "this Issue already has
        work" is the conservative choice.
        """
        text = f"{self.title}\n{self.body}"
        for closing in _CLOSING_REF_RE.finditer(text):
            if closing.group("urlrepo") and closing.group("urlrepo").lower() == repo.lower():
                if int(closing.group("urlnum")) == issue_number:
                    return True
            elif closing.group("num") and int(closing.group("num")) == issue_number:
                return True

        for url_match in _ISSUE_URL_RE.finditer(text):
            if (
                url_match.group("repo").lower() == repo.lower()
                and int(url_match.group("num")) == issue_number
            ):
                return True
        return False


@dataclass(frozen=True)
class IssueComment:
    """One comment on an Issue, as GitHub reports it.

    ``body`` is the raw markdown, because the only thing this is used for is finding
    this service's own machine marker. ``url`` is the ``html_url`` a maintainer can
    open, never an API URL.
    """

    id: int
    body: str = ""
    url: str = ""


def _to_issue_comment(raw: dict[str, Any]) -> IssueComment:
    body = raw.get("body")
    return IssueComment(
        id=int(raw.get("id", 0)),
        body=str(body) if isinstance(body, str) else "",
        url=str(raw.get("html_url") or ""),
    )


@dataclass
class PreflightReport:
    """What the wrapper can actually prove right now. Nothing is assumed."""

    wrapper_command: str
    wrapper_found: bool
    wrapper_executable: bool
    identity: str | None = None
    repo_access: dict[str, str] = field(default_factory=dict)  # slug -> "ok" | "<kind>: <msg>"
    errors: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.wrapper_found and not self.errors


class GitHubClient:
    """Invoke the configured wrapper for issues, PRs and labels."""

    def __init__(
        self,
        command: str,
        *,
        timeout_seconds: float = 60.0,
        per_page: int = DEFAULT_PER_PAGE,
        runner: Callable[[Sequence[str], float], "subprocess.CompletedProcess[str]"] | None = None,
    ) -> None:
        self.command = command
        self.timeout_seconds = timeout_seconds
        self.per_page = per_page
        self._runner = runner
        #: Set when the most recent list call stopped at :data:`MAX_PAGES`. A
        #: truncated PR scan means a pre-existing PR might not have been seen, so
        #: callers must surface it instead of assuming "no PR exists".
        self.pr_scan_truncated = False
        #: Set when the most recent **comment** listing stopped at the page cap. A
        #: truncated comment scan means "no status comment of mine exists" is
        #: unprovable, so callers must refuse to create another one rather than
        #: risking a duplicate status thread.
        self.comment_scan_truncated = False
        self._last_page_reached_cap = False

    # ------------------------------------------------------------------ core

    def wrapper_path(self) -> Path | None:
        """Resolve the configured wrapper to a concrete path, if it exists."""
        expanded = os.path.expandvars(os.path.expanduser(self.command))
        candidate = Path(expanded)
        if candidate.is_file():
            return candidate
        located = shutil.which(self.command)
        return Path(located) if located else None

    def check_available(self) -> Path:
        path = self.wrapper_path()
        if path is None:
            raise GitHubError(
                f"GitHub wrapper command not found: {self.command!r}. "
                "Set github.command in the config to the approved wrapper "
                "(this VM: /home/craftlypse/.local/bin/gh-craftlypse).",
                kind=ErrorKind.MISSING_WRAPPER,
            )
        if not os.access(path, os.X_OK):
            raise GitHubError(
                f"GitHub wrapper {path} is not executable.",
                kind=ErrorKind.MISSING_WRAPPER,
            )
        return path

    def _execute(self, args: Sequence[str]) -> "subprocess.CompletedProcess[str]":
        if self._runner is not None:
            return self._runner(args, self.timeout_seconds)
        return subprocess.run(
            [self.command, *args],
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )

    def _run_json(self, args: Sequence[str]) -> Any:
        """Run the wrapper and parse a JSON response.

        Only whole JSON documents are requested (never ``--jq`` scalars), so
        parsing is unambiguous and a malformed response is a hard error rather
        than something silently coerced.

        Raises :class:`GitHubError` with a classified ``kind`` on any failure.
        The error message never contains credential material: only the wrapper's
        own stderr text and the argument list are surfaced.
        """
        self.check_available()
        argv = list(args)
        try:
            proc = self._execute(argv)
        except FileNotFoundError as exc:
            raise GitHubError(
                f"GitHub wrapper {self.command!r} could not be executed: {exc}",
                kind=ErrorKind.MISSING_WRAPPER,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitHubError(
                f"GitHub wrapper timed out after {self.timeout_seconds:.0f}s: {_argv_hint(argv)}",
                kind=ErrorKind.TIMEOUT,
            ) from exc

        if proc.returncode != 0:
            raise _classify_failure(proc.returncode, proc.stderr or "", argv)

        text = (proc.stdout or "").strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise GitHubError(
                f"GitHub wrapper returned unparseable JSON for {_argv_hint(argv)}: {exc}",
                kind=ErrorKind.MALFORMED,
                detail=(proc.stdout or "")[:400],
            ) from exc

    # ------------------------------------------------------------- read ops

    def identity(self) -> str:
        """The account the configured credential actually acts as."""
        data = self._run_json(["api", "user"])
        if isinstance(data, dict) and data.get("login"):
            return str(data["login"])
        raise GitHubError(
            "GitHub wrapper returned no identity from 'api user'",
            kind=ErrorKind.MALFORMED,
        )

    def repo_accessible(self, slug: str) -> str:
        """Return the API's own view of ``slug``; raises if it is not accessible."""
        data = self._run_json(["api", f"repos/{slug}"])
        if isinstance(data, dict) and data.get("full_name"):
            return str(data["full_name"])
        raise GitHubError(
            f"GitHub returned no data for repository {slug}", kind=ErrorKind.MALFORMED
        )

    def list_labels(self, slug: str) -> set[str]:
        names: set[str] = set()
        for page in self._paginate(f"repos/{slug}/labels"):
            if isinstance(page, dict) and "name" in page:
                names.add(str(page["name"]))
        return names

    def create_label(self, slug: str, name: str, *, color: str, description: str) -> bool:
        """Create a label if absent. Idempotent; returns True when it was created.

        Called only from the explicit, maintainer-invoked ``setup-labels``
        command — never as a side effect of polling.
        """
        if name in self.list_labels(slug):
            return False

        self.check_available()
        argv = [
            "api",
            "-X",
            "POST",
            f"repos/{slug}/labels",
            "-f",
            f"name={name}",
            "-f",
            f"color={color}",
            "-f",
            f"description={description}",
        ]
        try:
            proc = self._execute(argv)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise GitHubError(f"failed to create label {name!r} in {slug}: {exc}") from exc

        if proc.returncode == 0:
            return True

        stderr = proc.stderr or ""
        # Already exists (e.g. created between our check and the POST) is success.
        if "already_exists" in stderr or "422" in stderr and "already" in stderr.lower():
            return False
        raise _classify_failure(proc.returncode, stderr, argv)

    def list_issues_with_label(self, slug: str, label: str) -> list[Issue]:
        """Open Issues carrying ``label``.

        Pull requests are filtered out explicitly: GitHub's issues endpoint
        returns PRs too, which is how a naive implementation schedules a PR as if
        it were an Issue.
        """
        issues: list[Issue] = []
        for raw in self._paginate(f"repos/{slug}/issues", {"state": "open", "labels": label}):
            if not isinstance(raw, dict):
                continue
            if "pull_request" in raw:
                continue  # a PR object, not an Issue
            issues.append(_to_issue(raw))
        return issues

    def list_pulls(self, slug: str, state: str = "all") -> list[PullRequest]:
        """Every PR for ``slug``, and whether the scan hit the page cap.

        ``pr_scan_truncated`` is set to ``True`` when pagination stopped at
        :data:`MAX_PAGES` with more data pending, so the caller can report an
        incomplete scan rather than silently treating "not found" as "absent".
        """
        self.pr_scan_truncated = False
        pulls: list[PullRequest] = []
        for raw in self._paginate(f"repos/{slug}/pulls", {"state": state}):
            if isinstance(raw, dict) and "number" in raw:
                pulls.append(_to_pull(raw))
        if self._last_page_reached_cap:
            self.pr_scan_truncated = True
        return pulls

    def open_issue(self, slug: str, issue_number: int) -> Issue:
        """Fetch one object by number, flagging it when it is actually a PR.

        ``GET /repos/{slug}/issues/{n}`` answers for **both** Issues and pull
        requests, so the ``pull_request`` marker is preserved on the returned
        :class:`Issue`. Callers must refuse ``is_pull_request`` objects rather
        than treating a PR number as an Issue.
        """
        raw = self._run_json(["api", f"repos/{slug}/issues/{issue_number}"])
        if not isinstance(raw, dict):
            raise GitHubError(
                f"GitHub returned no data for {slug}#{issue_number}", kind=ErrorKind.MALFORMED
            )
        return _to_issue(raw)

    def get_pull(self, slug: str, pr_number: int) -> PullRequest:
        """Fetch one pull request by number, with its head SHA."""
        raw = self._run_json(["api", f"repos/{slug}/pulls/{pr_number}"])
        if not isinstance(raw, dict):
            raise GitHubError(
                f"GitHub returned no data for {slug} PR #{pr_number}", kind=ErrorKind.MALFORMED
            )
        return _to_pull(raw)

    def find_pull_by_head(self, slug: str, head_branch: str) -> PullRequest | None:
        """Find the open PR whose head is exactly ``head_branch``, if any.

        This is the recovery primitive: it answers "did my push already turn into a
        PR before the crash?" without trusting a PR's *title* or a stray ``#N`` in
        prose. Only an exact head-branch match counts, and the caller must still
        verify the PR references the intended Issue before recording ownership.
        """
        owner = slug.split("/", 1)[0]
        head_param = f"{owner}:{head_branch}"
        for raw in self._paginate(f"repos/{slug}/pulls", {"state": "open", "head": head_param}):
            if not isinstance(raw, dict) or "number" not in raw:
                continue
            pull = _to_pull(raw)
            if pull.head_ref == head_branch:
                return pull
        return None

    # -------------------------------------------------------- comment ops (#17)

    def list_issue_comments(self, slug: str, issue_number: int) -> list[IssueComment]:
        """Every comment on an Issue, and whether the scan hit the page cap.

        ``comment_scan_truncated`` is set when pagination stopped at :data:`MAX_PAGES`
        with more data pending. This is the recovery primitive for a crash between
        creating a status comment and recording its id: the marker is found by
        scanning this list. When the list is truncated, an absence of the marker is
        **not** evidence of its absence, so the caller must not create a new comment.
        """
        self.comment_scan_truncated = False
        comments: list[IssueComment] = []
        endpoint = f"repos/{slug}/issues/{issue_number}/comments"
        for raw in self._paginate(endpoint):
            if isinstance(raw, dict) and "id" in raw:
                comments.append(_to_issue_comment(raw))
        if self._last_page_reached_cap:
            self.comment_scan_truncated = True
        return comments

    def create_issue_comment(self, slug: str, issue_number: int, body: str) -> IssueComment:
        """Post one comment on an Issue and return the real object GitHub created.

        The returned ``id`` is what is recorded as ownership, never a number parsed
        out of a fragment. A failure is raised, and the caller records the intent as
        unresolved rather than assuming the comment landed — a create that timed out
        may well have succeeded, which is why the next pass re-scans by marker.
        """
        self.check_available()
        argv = ["api", "-X", "POST", f"repos/{slug}/issues/{issue_number}/comments"]
        argv += ["-f", f"body={body}"]

        try:
            proc = self._execute(argv)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise GitHubError(f"failed to comment on {slug}#{issue_number}: {exc}") from exc

        if proc.returncode != 0:
            raise _classify_failure(proc.returncode, proc.stderr or "", argv)

        try:
            raw = json.loads((proc.stdout or "").strip() or "null")
        except json.JSONDecodeError as exc:
            raise GitHubError(
                "GitHub accepted the comment but returned an unparseable body, so its id is "
                "unknown; the next status pass will find it by marker instead of posting again",
                kind=ErrorKind.MALFORMED,
                detail=(proc.stdout or "")[:400],
            ) from exc
        if not isinstance(raw, dict) or "id" not in raw:
            raise GitHubError(
                "GitHub returned no comment id after creating one; the next status pass will "
                "find it by marker instead of posting again",
                kind=ErrorKind.MALFORMED,
            )
        return _to_issue_comment(raw)

    def edit_issue_comment(self, slug: str, comment_id: int, body: str) -> IssueComment:
        """Replace the body of one existing comment. Never creates a new one.

        ``PATCH`` on a comment id is the whole point of Issue #17: one comment, edited
        for every heartbeat. A non-zero exit is reported honestly so the caller can
        warn and move on — a status edit is never allowed to affect a run.
        """
        self.check_available()
        argv = ["api", "-X", "PATCH", f"repos/{slug}/issues/comments/{comment_id}"]
        argv += ["-f", f"body={body}"]

        try:
            proc = self._execute(argv)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise GitHubError(f"failed to edit comment {comment_id} on {slug}: {exc}") from exc

        if proc.returncode != 0:
            raise _classify_failure(proc.returncode, proc.stderr or "", argv)

        try:
            raw = json.loads((proc.stdout or "").strip() or "null")
        except json.JSONDecodeError:
            # The edit succeeded as far as GitHub is concerned; only the echo is
            # unparseable, so the caller keeps the id it already has.
            return IssueComment(id=comment_id)
        if not isinstance(raw, dict) or "id" not in raw:
            return IssueComment(id=comment_id)
        return _to_issue_comment(raw)

    def create_pull(
        self,
        slug: str,
        *,
        head_branch: str,
        base_branch: str,
        title: str,
        body: str,
        draft: bool = False,
    ) -> PullRequest:
        """Open a pull request and return the real object GitHub created.

        The returned ``number`` is what gets recorded as task ownership — never a
        number parsed out of a response fragment, and never inferred from a branch
        name. Idempotency is the caller's job (check :meth:`find_pull_by_head`
        first): a second POST for an existing head/base pair is an API error, and
        treating that error as "no PR" would be exactly the duplicate-creation bug
        this workflow exists to avoid.
        """
        self.check_available()
        argv = ["api", "-X", "POST", f"repos/{slug}/pulls"]
        argv += ["-f", f"title={title}"]
        argv += ["-f", f"head={head_branch}"]
        argv += ["-f", f"base={base_branch}"]
        argv += ["-f", f"body={body}"]
        if draft:
            argv += ["-F", "draft=true"]

        try:
            proc = self._execute(argv)
        except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
            raise GitHubError(f"failed to create a pull request in {slug}: {exc}") from exc

        if proc.returncode != 0:
            raise _classify_failure(proc.returncode, proc.stderr or "", argv)

        try:
            raw = json.loads((proc.stdout or "").strip() or "null")
        except json.JSONDecodeError as exc:
            raise GitHubError(
                "GitHub accepted the pull request but returned an unparseable body, so the PR "
                "number is unknown; check GitHub before retrying",
                kind=ErrorKind.MALFORMED,
                detail=(proc.stdout or "")[:400],
            ) from exc
        if not isinstance(raw, dict) or "number" not in raw:
            raise GitHubError(
                "GitHub returned no pull request number after creating one; check GitHub before "
                "retrying so a duplicate is not created",
                kind=ErrorKind.MALFORMED,
            )
        return _to_pull(raw)

    def _paginate(self, endpoint: str, params: dict[str, str] | None = None) -> list[Any]:
        """Collect all pages of a list endpoint, page by page.

        ``--paginate`` is not used because it concatenates separate JSON
        documents per page; an explicit page loop keeps parsing unambiguous and
        makes the pagination behaviour directly testable offline.
        """
        self._last_page_reached_cap = False
        collected: list[Any] = []
        page = 1
        while page <= MAX_PAGES:
            args = ["api", "-X", "GET", endpoint]
            for key, value in (params or {}).items():
                args += ["-f", f"{key}={value}"]
            args += ["-f", f"per_page={self.per_page}", "-f", f"page={page}"]

            data = self._run_json(args)
            if data is None:
                break
            if isinstance(data, dict):
                # A single object means the endpoint is not a list.
                collected.append(data)
                break
            if not isinstance(data, list):
                raise GitHubError(
                    f"GitHub wrapper returned {type(data).__name__} for {endpoint} (expected a list)",
                    kind=ErrorKind.MALFORMED,
                )
            collected.extend(data)
            if len(data) < self.per_page:
                # A short page proves the listing ended.
                break
            page += 1
        else:
            # The loop ran out of pages, not out of data: the listing is
            # incomplete. Callers decide how to report that; nothing is guessed.
            self._last_page_reached_cap = True
        return collected

    # ------------------------------------------------------------ preflight

    def preflight(self, slugs: Sequence[str], required_labels: Sequence[str]) -> PreflightReport:
        """Verify only what the wrapper can actually prove.

        Deliberately does **not** claim to validate token scopes: that is not
        observable through the allowed wrapper, and Issue #3 requires honesty
        about unverified capabilities.
        """
        report = PreflightReport(
            wrapper_command=self.command,
            wrapper_found=False,
            wrapper_executable=False,
        )
        path = self.wrapper_path()
        if path is None:
            report.errors.append(
                f"wrapper not found: {self.command!r} (configure github.command to the approved wrapper)"
            )
            return report

        report.wrapper_found = True
        report.wrapper_executable = os.access(path, os.X_OK)
        if not report.wrapper_executable:
            report.errors.append(f"wrapper {path} is not executable")
            return report

        try:
            report.identity = self.identity()
        except GitHubError as exc:
            report.errors.append(f"authentication check failed ({exc.kind}): {exc}")

        for slug in slugs:
            try:
                self.repo_accessible(slug)
                report.repo_access[slug] = "ok"
            except GitHubError as exc:
                report.repo_access[slug] = f"{exc.kind}: {exc}"
                if exc.kind == ErrorKind.DENIED_REPO:
                    report.errors.append(
                        f"{slug}: not accessible with the configured credential "
                        "(add the repository to the credential's permitted set; "
                        "this service will not try another identity)"
                    )
                else:
                    report.errors.append(f"{slug}: {exc.kind}: {exc}")

            try:
                labels = self.list_labels(slug)
            except GitHubError as exc:
                report.notes.append(f"{slug}: could not read labels ({exc.kind}): {exc}")
                continue
            missing = [name for name in required_labels if name not in labels]
            if missing:
                report.notes.append(
                    f"{slug}: missing labels: {', '.join(missing)} "
                    "— create them explicitly with `agent-dispatch setup-labels --repo "
                    f"{slug}` (the trigger protocol stays inert until then)"
                )

        return report


def _to_issue(raw: dict[str, Any]) -> Issue:
    labels = tuple(
        str(item.get("name"))
        for item in (raw.get("labels") or [])
        if isinstance(item, dict) and item.get("name")
    )
    body = raw.get("body")
    return Issue(
        number=int(raw.get("number", 0)),
        title=str(raw.get("title") or ""),
        state=str(raw.get("state") or "unknown"),
        url=str(raw.get("html_url") or ""),
        labels=labels,
        is_pull_request="pull_request" in raw,
        body=str(body) if isinstance(body, str) else "",
    )


def _to_pull(raw: dict[str, Any]) -> PullRequest:
    merged_at = raw.get("merged_at")
    head = raw.get("head") or {}
    body = raw.get("body")
    head_repo_raw = head.get("repo") or {}
    head_repo = ""
    if isinstance(head_repo_raw, Mapping):
        head_repo = str(head_repo_raw.get("full_name") or "")
    head_owner = ""
    head_user = head.get("user") or head_repo_raw.get("owner") or {}
    if isinstance(head_user, Mapping):
        head_owner = str(head_user.get("login") or "")
    return PullRequest(
        number=int(raw.get("number", 0)),
        state=str(raw.get("state") or "unknown"),
        merged=bool(merged_at),
        head_ref=str(head.get("ref") or ""),
        url=str(raw.get("html_url") or ""),
        title=str(raw.get("title") or ""),
        body=str(body) if isinstance(body, str) else "",
        head_sha=str(head.get("sha") or ""),
        head_repo=head_repo,
        head_owner=head_owner,
    )


def _argv_hint(argv: Sequence[str]) -> str:
    return " ".join(argv[:4]) + (" …" if len(argv) > 4 else "")


def _classify_failure(returncode: int, stderr: str, argv: Sequence[str]) -> GitHubError:
    """Turn a wrapper failure into a classified, honest error.

    Never guesses success, never suggests another identity, and never includes
    credential material — only the wrapper's own stderr text.
    """
    text = (stderr or "").strip()
    lowered = text.lower()
    hint = _argv_hint(argv)

    if "rate limit" in lowered or "secondary rate" in lowered:
        return GitHubError(
            f"GitHub rate limit hit for {hint}: {text}",
            kind=ErrorKind.RATE_LIMIT,
            detail=text,
        )
    if (
        "no such host" in lowered
        or "connection refused" in lowered
        or "dial tcp" in lowered
        or "network is unreachable" in lowered
        or "tls handshake" in lowered
        or "could not resolve host" in lowered
    ):
        return GitHubError(
            f"network error contacting GitHub for {hint}: {text}",
            kind=ErrorKind.NETWORK,
            detail=text,
        )
    if (
        "bad credentials" in lowered
        or "http 401" in lowered
        or "authentication" in lowered
        or "token" in lowered
        and "invalid" in lowered
    ):
        return GitHubError(
            f"GitHub authentication failed for {hint}: {text}",
            kind=ErrorKind.AUTH,
            detail=text,
        )
    if (
        "http 404" in lowered
        or "not found" in lowered
        or "http 403" in lowered
        or "forbidden" in lowered
    ):
        return GitHubError(
            f"GitHub denied access for {hint}: {text}",
            kind=ErrorKind.DENIED_REPO,
            detail=text,
        )
    return GitHubError(
        f"GitHub wrapper failed (exit {returncode}) for {hint}: {text}",
        kind=ErrorKind.UNKNOWN,
        detail=text,
    )

"""Task-owned branch and worktree management.

One ``(repo, Issue)`` task owns exactly one branch and one worktree, both recorded
in SQLite (§4 of ``docs/architecture.md``). The rules this module enforces:

* **Ordinary Git.** ``git worktree add`` is used rather than the runtime's own
  ``-w`` flag, so the orchestrator owns naming, lifecycle and credentials.
* **A separate source clone.** A worktree shares ``--git-common-dir`` with the
  clone it was created from, so repo-local Git config is *shared*. Provisioning
  therefore points at an orchestrator-managed source clone, never the maintainer's
  working copy, and refuses a source path that looks like a normal checkout whose
  config we would be editing.
* **Ownership is recorded, never inferred.** A branch or directory that matches
  our naming convention is a *hint*; only the task row makes it ours. An unknown
  worktree at the expected path is escalated instead of adopted or deleted.
* **Nothing is deleted.** Cleanup is an explicit operator action, never a side
  effect of completing or failing a task, because an interrupted run's uncommitted
  edits are real work.

"Dirty" is not used as a proxy for anything: a clean worktree may contain
successful commits, and a dirty one is expected after an interruption.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .gitcmd import Git, GitError, dispatch_branch_name, task_worktree_path


class WorktreeError(Exception):
    """The owned branch/worktree could not be established or inspected."""


@dataclass
class WorktreeState:
    """What is actually on disk and in Git right now for one task."""

    path: Path
    branch: str
    exists: bool
    is_registered_worktree: bool
    head_sha: str | None = None
    base_sha: str | None = None
    committed_count: int = 0
    committed_subjects: list[str] = field(default_factory=list)
    uncommitted_files: list[str] = field(default_factory=list)
    untracked_files: list[str] = field(default_factory=list)
    ahead: bool = False
    remote_branch_exists: bool = False

    @property
    def dirty(self) -> bool:
        return bool(self.uncommitted_files or self.untracked_files)

    @property
    def has_commits(self) -> bool:
        return self.committed_count > 0

    @property
    def produced_work(self) -> bool:
        """Committed *or* uncommitted change against the recorded base.

        Both count. A clean tree with commits is fully successful work; a dirty
        tree with no commits is an interrupted run whose edits must be preserved.
        """
        return self.has_commits or self.dirty

    def describe(self) -> str:
        parts = [
            f"branch={self.branch}",
            f"commits={self.committed_count}",
            f"uncommitted={len(self.uncommitted_files)}",
            f"untracked={len(self.untracked_files)}",
        ]
        if self.ahead:
            parts.append("ahead_of_base=yes")
        return " ".join(parts)


@dataclass
class ProvisionOutcome:
    """Result of ensuring the task's branch/worktree exists."""

    state: WorktreeState
    created_worktree: bool
    created_branch: bool
    fetched_base: bool
    notes: list[str] = field(default_factory=list)


class WorktreeManager:
    """Provision and inspect one task's owned branch and worktree."""

    def __init__(
        self,
        git: Git,
        *,
        source_path: Path | str,
        worktree_root: Path | str,
        base_branch: str,
        repo_slug: str,
        credential_helper: str | None = None,
        write_repo_local_config: bool = False,
    ) -> None:
        self.git = git
        self.source_path = Path(source_path)
        self.worktree_root = Path(worktree_root)
        self.base_branch = base_branch
        self.repo_slug = repo_slug
        self.credential_helper = credential_helper
        #: OFF by default. Writing the reset-then-wrapper sequence into the clone's
        #: config also edits the *shared* Git common config of every worktree that
        #: clone owns — which is fine for a dedicated orchestrator clone and not
        #: fine for a personal checkout. The credential env pairs cover both the
        #: orchestrator's own Git and the Git the agent spawns, so this is an
        #: optional extra rather than the primary mechanism.
        self.write_repo_local_config = write_repo_local_config

    # ------------------------------------------------------------- provisioning

    def fetch_base(self) -> bool:
        """Fetch the configured base branch fresh, so a task starts from current work."""
        result = self.git.run(["fetch", "origin", self.base_branch], cwd=self.source_path)
        return result.ok

    def ensure(
        self,
        *,
        issue_number: int,
        title: str,
        recorded_branch: str | None = None,
        recorded_path: Path | str | None = None,
    ) -> ProvisionOutcome:
        """Create or reuse the owned branch/worktree, checking before creating.

        ``recorded_branch``/``recorded_path`` come from the task row. When they are
        present they are authoritative: after a restart the orchestrator must find
        the *existing* branch and worktree rather than deriving a new name, which
        is what stops a crash from creating a second branch for the same Issue.
        """
        branch = recorded_branch or dispatch_branch_name(issue_number, title)
        path = (
            Path(recorded_path)
            if recorded_path
            else task_worktree_path(self.worktree_root, self.repo_slug, issue_number)
        )
        notes: list[str] = []

        fetched = self.fetch_base()
        if not fetched:
            notes.append(
                f"could not fetch origin/{self.base_branch}; using the local base ref "
                "(the push will fail loudly rather than silently diverging)"
            )

        created_branch = False
        created_worktree = False

        if path.exists():
            registered = self._is_registered(path)
            if not registered:
                # The path exists but is not a worktree Git knows about. It is not
                # ours to delete and not ours to adopt: report and let the caller
                # escalate to needs_attention.
                raise WorktreeError(
                    f"worktree path {path} already exists but is not a registered Git worktree "
                    f"of {self.source_path}; refusing to delete or adopt an unknown path"
                )
            if not recorded_path:
                # A registered worktree at the deterministic path with no task row
                # entry: possibly a previous run of ours, possibly a foreign one.
                # Recorded ownership is what decides, so do not guess.
                existing_branch = self._current_branch(path)
                if existing_branch and existing_branch != branch:
                    raise WorktreeError(
                        f"worktree {path} is registered on branch {existing_branch!r}, but this task "
                        f"expects {branch!r}; ownership is not provable, so nothing was changed"
                    )
                notes.append(f"reusing the registered worktree at {path} (branch {branch})")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._ensure_local_base()
            self._create_worktree(path, branch, issue_number)
            created_worktree = True
            created_branch = True
            notes.append(f"created worktree {path} on new branch {branch}")

        # Optional: repo-local credential config for this clone. Written only when
        # explicitly enabled, because it also edits the shared common config of
        # every worktree the clone owns.
        if self.credential_helper and self.write_repo_local_config:
            try:
                self.git.configure_repo_local(self.source_path)
                notes.append("repo-local credential ordering refreshed (reset, then wrapper)")
            except GitError as exc:
                notes.append(f"could not refresh repo-local credential config: {exc}")

        state = self.inspect(path, branch)
        return ProvisionOutcome(
            state=state,
            created_worktree=created_worktree,
            created_branch=created_branch,
            fetched_base=fetched,
            notes=notes,
        )

    def _ensure_local_base(self) -> None:
        """Make sure a local ref for the base branch exists before branching from it.

        ``origin/<base>`` is preferred, but a clone that has never checked the base
        out may only have the remote-tracking ref — which is fine — while a clone
        with no fetch at all has neither. Failing here with a clear message beats
        creating a worktree on an arbitrary commit.
        """
        for candidate in (f"origin/{self.base_branch}", self.base_branch):
            if self._rev_parse(candidate) is not None:
                return
        raise WorktreeError(
            f"neither 'origin/{self.base_branch}' nor '{self.base_branch}' resolves in "
            f"{self.source_path}; cannot create a task branch without a base"
        )

    def _create_worktree(self, path: Path, branch: str, issue_number: int) -> None:
        base = f"origin/{self.base_branch}"
        if self._rev_parse(base) is None:
            base = self.base_branch

        # A local branch of that name may already exist from a previous attempt
        # that was cleaned up on disk but not in Git. Check before adding.
        if self._branch_exists(branch):
            raise WorktreeError(
                f"branch {branch!r} already exists in {self.source_path} but no worktree is recorded "
                f"for {self.repo_slug}#{issue_number}; refusing to reuse or reset an unowned branch"
            )

        result = self.git.run(
            ["worktree", "add", "-b", branch, str(path), base], cwd=self.source_path
        )
        if not result.ok:
            raise WorktreeError(
                f"git worktree add failed for {path} on {branch}: {result.stderr.strip()}"
            )

    # ------------------------------------------------------------- inspection

    def inspect(self, path: Path | str, branch: str) -> WorktreeState:
        """Read the real state of an owned worktree against the recorded base."""
        path = Path(path)
        if not path.is_dir():
            return WorktreeState(
                path=path, branch=branch, exists=False, is_registered_worktree=False
            )

        registered = self._is_registered(path)
        base_ref = f"origin/{self.base_branch}"
        if self._rev_parse(base_ref) is None:
            base_ref = self.base_branch
        base_sha = self._rev_parse(base_ref)

        head_sha = self._rev_parse("HEAD", cwd=path)
        subjects: list[str] = []
        committed = 0
        if base_sha and head_sha:
            log = self.git.run(["log", "--format=%h %s", f"{base_sha}..HEAD"], cwd=path)
            if log.ok:
                lines = [line for line in log.stdout.splitlines() if line.strip()]
                committed = len(lines)
                subjects = lines[:10]

        status = self.git.run(["status", "--porcelain"], cwd=path)
        uncommitted: list[str] = []
        untracked: list[str] = []
        if status.ok:
            for line in status.stdout.splitlines():
                if not line.strip():
                    continue
                path_part = line[3:].strip()
                if line.startswith("??"):
                    untracked.append(path_part)
                else:
                    uncommitted.append(path_part)

        remote = self.git.run(
            ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"], cwd=self.source_path
        )
        remote_exists = remote.ok and bool(remote.stdout.strip())
        return WorktreeState(
            path=path,
            branch=branch,
            exists=True,
            is_registered_worktree=registered,
            head_sha=head_sha,
            base_sha=base_sha,
            committed_count=committed,
            committed_subjects=subjects,
            uncommitted_files=uncommitted,
            untracked_files=untracked,
            ahead=committed > 0,
            remote_branch_exists=remote_exists,
        )

    def commit_all(self, path: Path | str, branch: str, message: str) -> tuple[bool, str]:
        """Commit whatever the agent left uncommitted, so the branch is pushable.

        Returns ``(committed, note)``. Only called for an **accepted** run: the
        orchestrator must never commit the leftovers of a failed or blocked run as
        if they were finished work.
        """
        path = Path(path)
        status = self.git.run(["status", "--porcelain"], cwd=path)
        if not status.ok or not status.stdout.strip():
            return False, "nothing to commit; the run left a clean tree"

        # Stage everything the agent created, including new files. This is the one
        # place a broad `add -A` is correct: the worktree is owned exclusively by
        # this task, and run logs are written outside every repository precisely so
        # they cannot be swept up here (§6).
        staged = self.git.run(["add", "-A"], cwd=path)
        if not staged.ok:
            return False, f"git add -A failed: {staged.stderr.strip()}"

        message_path = None
        try:
            import tempfile

            with tempfile.NamedTemporaryFile(
                "w", suffix=".txt", delete=False, encoding="utf-8"
            ) as handle:
                handle.write(message if message.endswith("\n") else message + "\n")
                message_path = handle.name
            commit = self.git.run(["commit", "-F", message_path], cwd=path)
        finally:
            if message_path:
                Path(message_path).unlink(missing_ok=True)

        if not commit.ok:
            return False, f"git commit failed: {commit.stderr.strip()}"
        return True, f"committed the run's remaining changes on {branch}"

    def push(self, path: Path | str, branch: str) -> tuple[bool, str]:
        """Push the owned branch with upstream set. Never force-pushes."""
        path = Path(path)
        result = self.git.run(["push", "-u", "origin", branch], cwd=path, check=False)
        if result.ok:
            return True, f"pushed {branch} to origin"
        return False, f"git push failed: {result.stderr.strip() or f'exit {result.returncode}'}"

    # ---------------------------------------------------------------- helpers

    def _is_registered(self, path: Path) -> bool:
        listing = self.git.run(["worktree", "list", "--porcelain"], cwd=self.source_path)
        if not listing.ok:
            return False
        target = str(path.resolve())
        for line in listing.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            candidate = line[len("worktree ") :].strip()
            try:
                if str(Path(candidate).resolve()) == target:
                    return True
            except OSError:  # pragma: no cover - unresolvable path
                continue
        return False

    def _current_branch(self, path: Path) -> str | None:
        result = self.git.run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path)
        if not result.ok:
            return None
        name = result.stdout.strip()
        return name or None

    def _rev_parse(self, ref: str, *, cwd: Path | str | None = None) -> str | None:
        result = self.git.run(
            ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
            cwd=cwd if cwd is not None else self.source_path,
        )
        return result.stdout.strip() if result.ok and result.stdout.strip() else None

    def _branch_exists(self, branch: str) -> bool:
        result = self.git.run(
            ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"], cwd=self.source_path
        )
        return result.ok


def source_clone_warning(source_path: Path | str, worktree_root: Path | str) -> str | None:
    """Warn when the configured source looks like the maintainer's own checkout.

    Provisioning writes repo-local Git config, and a worktree shares the clone's Git
    common config — so pointing at a personal checkout would mean editing the
    maintainer's Git configuration. This cannot be detected with certainty, so the
    check is deliberately advisory: it reports a likely problem instead of blocking
    a legitimate single-VM layout, and it never claims to have proved ownership.
    """
    path = Path(source_path)
    if not path.is_dir():
        return f"{path} does not exist"
    if any(part in {"src", "Documents", "Desktop"} for part in path.parts):
        return (
            f"{path} looks like a personal checkout; provisioning writes repo-local Git config "
            "into the worktree's shared common config"
        )
    return None


def is_dispatch_branch(name: str, issue_number: int | None = None) -> bool:
    """Whether ``name`` matches the dispatch branch convention.

    A naming match is explicitly **not** proof of ownership: it is used for
    reporting and for a guarded "is this probably ours" check before creating a
    branch, never to adopt a PR or a worktree.
    """
    from .gitcmd import DISPATCH_BRANCH_RE

    match = DISPATCH_BRANCH_RE.match(name or "")
    if not match:
        return False
    return issue_number is None or int(match.group(1)) == issue_number


def git_available() -> tuple[bool, str]:
    """Whether ``git`` can be executed at all, with a human-readable detail."""
    try:
        proc = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"git is unavailable: {exc}"
    if proc.returncode != 0:
        return False, f"git --version failed: {proc.stderr.strip()}"
    return True, proc.stdout.strip()

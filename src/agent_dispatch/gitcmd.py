"""Git operations and the approved credential path.

Issue #4 needs Git for three things: provisioning an owned worktree, evaluating
what a run produced, and pushing the owned branch. All three must authenticate
with the **configured wrapper**, never with an ambient helper.

The credential hazard (``docs/architecture.md`` §2.4) is that
``credential.helper`` is *multi-valued* and ``-c <key>=<value>`` **appends**: a
bare ``-c credential.<url>.helper=!<cmd>`` leaves every inherited helper in front
of the approved one, so an ambient entry is consulted first and can answer with
someone else's credential. Clearing the list and *then* setting the wrapper is
the only ordering that works.

Two mechanisms cover the two different lifetimes, and this module implements
both — deliberately, because neither alone is sufficient:

``invocation_config()`` / :meth:`Git.env`
    ``GIT_CONFIG_COUNT`` / ``GIT_CONFIG_KEY_n`` / ``GIT_CONFIG_VALUE_n`` are
    inherited by child processes, so a process subtree gets the reset-then-wrapper
    pair without touching any file. Used for every orchestrator-owned command
    *and* handed to the agent subprocess so Git invoked by the agent inherits the
    same order.

:meth:`Git.configure_repo_local`
    The same reset-then-wrapper sequence written into the repository's **local**
    config, so a shell the agent starts outside our environment still finds it.
    A worktree shares ``--git-common-dir`` with its source clone, which is why a
    dedicated orchestrator-managed source clone is used rather than the
    maintainer's checkout.

Measured on this VM (2026-09-25): the env pair suppresses an ambient helper that
*does* return real credentials for ``github.com`` — verified with an instrumented
decoy helper, and with the control case (no env pair) confirming the decoy is
otherwise called first. So the reset is load-bearing rather than defensive.

This remains an **operational guardrail on a personal VM, not isolation**: an
unrestricted agent could reconfigure Git. The goal is that the *default* path
authenticates with the approved wrapper.
"""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

from .util import repo_slug_dirname, shorten, utcnow_iso

#: Git's own knob for the config-pair env mechanism; the count must match the
#: number of KEY/VALUE pairs or Git ignores/errors on them.
_GIT_CONFIG_COUNT = "GIT_CONFIG_COUNT"

#: Keys a repository-local config must never be rewritten for. Anything outside
#: ``credential.*`` and our own marker would mean this module silently editing a
#: maintainer's Git configuration.
_HELPER_KEY_PREFIX = "credential."

#: ``dispatch/issue-<N>-<slug>`` — the deterministic branch shape from §4.
DISPATCH_BRANCH_RE = re.compile(r"^dispatch/issue-(\d+)(?:-(?P<slug>[A-Za-z0-9._-]*))?$")

_SLUG_SAFE_RE = re.compile(r"[^a-z0-9]+")


class GitError(Exception):
    """A Git command failed. Carries the command's own stderr, never credentials."""

    def __init__(
        self, message: str, *, argv: Sequence[str] | None = None, stderr: str = ""
    ) -> None:
        self.argv = list(argv or [])
        self.stderr = stderr
        super().__init__(message)

    @property
    def argv_hint(self) -> str:
        argv = [part for part in self.argv if not part.startswith("credential.")]
        return " ".join(argv[:4]) + (" …" if len(argv) > 4 else "")


def branch_slug(title: str, *, limit: int = 48) -> str:
    """Filesystem/Git-safe kebab slug for a title, truncated on a word boundary.

    Deterministic for a given title, which is what makes "the branch for this
    Issue" derivable after a restart instead of guessed.
    """
    lowered = title.strip().lower()
    slug = _SLUG_SAFE_RE.sub("-", lowered).strip("-")
    if len(slug) <= limit:
        return slug or "task"
    head = slug[:limit]
    if "-" in head:
        head = head[: head.rfind("-")]
    return head.strip("-") or slug[:limit]


def dispatch_branch_name(issue_number: int, title: str) -> str:
    """The one canonical branch name for an Issue's implementation branch."""
    return f"dispatch/issue-{issue_number}-{branch_slug(title)}"


def task_worktree_path(worktree_root: Path | str, repo: str, issue_number: int) -> Path:
    """``<worktree_root>/<owner>__<name>/issue-<N>`` — the documented layout (§4).

    Ownership is *recorded in SQLite*, never inferred from this path; the path is
    only a deterministic, collision-free location.
    """
    return Path(worktree_root) / repo_slug_dirname(repo) / f"issue-{issue_number}"


def credential_config_pairs(
    credential_helper: str, *, helper_reset: str = "", host: str = "github.com"
) -> list[tuple[str, str]]:
    """The ordered reset-then-wrapper pairs for ``credential.helper``.

    Order is the whole point: the empty value first resets the inherited list, then
    the approved helper is added. Returning them as an ordered list (rather than a
    dict) keeps that ordering visible instead of relying on dict behaviour.
    """
    key = f"credential.https://{host}.helper"
    return [(key, helper_reset), (key, credential_helper)]


def invocation_config(
    credential_helper: str, *, helper_reset: str = "", host: str = "github.com"
) -> tuple[str, ...]:
    """``key=value`` strings suitable for ``git -c`` and for :meth:`Git.env`.

    Note the literal empty value: ``credential.https://github.com.helper=`` is a
    list *reset*, not a no-op, so it is representable as ``-c`` but **not** as an
    environment pair (see :func:`env_config_items`).
    """
    pairs = credential_config_pairs(credential_helper, helper_reset=helper_reset, host=host)
    return tuple(f"{key}={value}" for key, value in pairs)


def env_config_items(
    credential_helper: str, *, helper_reset: str = "", host: str = "github.com"
) -> tuple[tuple[str, str], ...]:
    """The GIT_CONFIG_COUNT-style pairs, with an empty reset preserved as ``""``.

    Git's ``GIT_CONFIG_VALUE_n`` accepts an empty string, which is why the reset
    survives here and why this is a separate function from
    :func:`invocation_config`: a shell-quoted ``-c`` pair and an env pair are
    different representations of the same ordering.
    """
    return tuple(credential_config_pairs(credential_helper, helper_reset=helper_reset, host=host))


def apply_env_config(
    base: Mapping[str, str] | None, items: Sequence[tuple[str, str]]
) -> dict[str, str]:
    """Return ``base`` with the ``GIT_CONFIG_*`` pair mechanism set for ``items``.

    Any pre-existing ``GIT_CONFIG_COUNT``/``KEY_*``/``VALUE_*`` variables are
    removed first: leaving them would make Git see a count that does not match the
    keys, and the pairs are the guarantee, so they must not be merged blindly.
    """
    env = dict(base or {})
    for key in list(env):
        if key == _GIT_CONFIG_COUNT or key.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            del env[key]
    if not items:
        return env

    env[_GIT_CONFIG_COUNT] = str(len(items))
    for index, (key, value) in enumerate(items):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


@dataclass
class GitResult:
    """Outcome of one Git invocation."""

    returncode: int
    stdout: str
    stderr: str
    argv: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class Git:
    """Run Git with the approved credential ordering applied.

    ``check=False`` by default: a failed fetch or a missing branch is a *finding*
    the orchestrator reports, not an exception. Only :meth:`run_checked` raises.
    """

    def __init__(
        self,
        *,
        credential_helper: str | None = None,
        credential_helper_reset: str = "",
        timeout_seconds: float = 300.0,
        env: Mapping[str, str] | None = None,
        log_redaction: bool = True,
    ) -> None:
        self.credential_helper = credential_helper
        self.credential_helper_reset = credential_helper_reset
        self.timeout_seconds = timeout_seconds
        self._base_env = dict(env or {})
        #: Belt and braces: command lines are already scrubbed by ``_scrub``, but
        #: any helper string is never echoed into a log or error message.
        self.log_redaction = log_redaction

    # ------------------------------------------------------------ environment

    def env(self, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        """A child environment carrying the reset-then-wrapper config pairs.

        Used for orchestrator commands *and* for the agent subprocess, so Git
        invoked by the agent inherits the same helper order without any file being
        modified.
        """
        env = dict(os.environ)
        env.update(self._base_env)
        if extra:
            env.update(extra)
        if not self.credential_helper:
            return env
        return apply_env_config(
            env,
            env_config_items(self.credential_helper, helper_reset=self.credential_helper_reset),
        )

    # ------------------------------------------------------------- invocation

    def _argv(self, args: Sequence[str], *, cwd: Path | str | None) -> list[str]:
        argv = ["git"]
        if cwd is not None:
            argv += ["-C", str(cwd)]
        if self.credential_helper:
            for pair in invocation_config(
                self.credential_helper, helper_reset=self.credential_helper_reset
            ):
                argv += ["-c", pair]
        argv += list(args)
        return argv

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | str | None = None,
        env: Mapping[str, str] | None = None,
        check: bool = False,
    ) -> GitResult:
        argv = self._argv(args, cwd=cwd)
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
                env=self.env(env),
            )
        except FileNotFoundError as exc:
            raise GitError(f"git is not available: {exc}", argv=args) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitError(
                f"git timed out after {self.timeout_seconds:.0f}s: {_scrub(args)}", argv=args
            ) from exc

        result = GitResult(
            returncode=proc.returncode,
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            argv=list(args),
        )
        if check and not result.ok:
            raise GitError(
                f"git {_scrub(args)} failed (exit {result.returncode}): {shorten(result.stderr)}",
                argv=args,
                stderr=result.stderr,
            )
        return result

    def run_checked(self, args: Sequence[str], *, cwd: Path | str | None = None) -> GitResult:
        return self.run(args, cwd=cwd, check=True)

    # ------------------------------------------------------- repo-local config

    def configure_repo_local(
        self,
        repo_path: Path | str,
        *,
        host: str = "github.com",
        marker_key: str = "agent-dispatch.managed",
    ) -> int:
        """Write the reset-then-wrapper sequence into the repository-local config.

        ``--replace-all`` is required: plain ``git config`` on an already-set key
        *replaces the first* occurrence and leaves later ones in place, so a naive
        write would slowly accumulate helpers. Replacing all values, then
        re-adding in order, is what makes repeated provisioning idempotent.

        Returns the number of ``credential.*`` values now present, so a caller can
        assert the ordering rather than assume it.
        """
        if not self.credential_helper:
            raise GitError("no credential helper configured; refusing to write repo-local config")

        # Never leave stale helpers of ours behind: drop every existing value for
        # the key (including any inherited from a previous run), keeping the
        # operation scoped to this one key.
        key = f"credential.https://{host}.helper"
        self.run(["config", "--unset-all", key], cwd=repo_path)

        exit_code = 0
        for _, value in env_config_items(
            self.credential_helper, helper_reset=self.credential_helper_reset, host=host
        ):
            result = self.run(["config", "--add", key, value], cwd=repo_path)
            if not result.ok:
                exit_code = result.returncode

        # A marker so `doctor`/`status` can tell an orchestrator-provisioned clone
        # from a maintainer's checkout without guessing from the path.
        self.run(
            ["config", marker_key, f"true since {utcnow_iso()}"],
            cwd=repo_path,
        )
        if exit_code != 0:
            raise GitError(f"could not write repo-local credential config in {repo_path}")
        return self.helper_count(repo_path, host=host)

    def helper_count(self, repo_path: Path | str, *, host: str = "github.com") -> int:
        """How many ``credential.https://<host>.helper`` values are configured.

        Read back **without** the credential env override, because the question is
        what a child process that only reads repo-local config would see.
        """
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "config",
                "--get-all",
                f"credential.https://{host}.helper",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if result.returncode not in (0, 1):
            return 0
        return len([line for line in (result.stdout or "").splitlines() if line.strip()])

    def effective_helpers(self, repo_path: Path | str, *, host: str = "github.com") -> list[str]:
        """The helper chain a child process would resolve, with the reset applied.

        Git exposes the reset as an *empty* line followed by the real helper. Both
        are returned so a caller can assert the order (empty first) instead of
        only counting entries. Read without the env override on purpose.
        """
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_path),
                "config",
                "--get-all",
                f"credential.https://{host}.helper",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return (result.stdout or "").splitlines()


def _scrub(args: Sequence[str]) -> str:
    """Render an argv hint with credential-ish values removed.

    ``git -c credential...helper=!<wrapper>`` is safe to name (it holds no secret),
    but the rule for this codebase is that command lines never carry anything
    credential-shaped into a log. Only the first few positional parts are shown.
    """
    positional = [part for part in args if not part.startswith(("credential.", "http."))]
    return " ".join(positional[:4]) + (" …" if len(positional) > 4 else "")

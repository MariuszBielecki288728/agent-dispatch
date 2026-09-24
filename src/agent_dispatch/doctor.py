"""``doctor`` — environment and capability preflight.

Reports **only what can actually be verified** through the approved wrapper and
the local filesystem. In particular it does not claim to validate token scopes,
which are not observable through the wrapper, and it does not invent a fallback
identity. Missing labels are reported with actionable guidance instead of being
created silently.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config
from .github import ErrorKind, GitHubClient, GitHubError
from .util import is_within


@dataclass
class Check:
    name: str
    status: str  # ok | warn | fail | skip
    detail: str

    @property
    def symbol(self) -> str:
        return {"ok": "✓", "warn": "!", "fail": "✗", "skip": "·"}.get(self.status, "?")


@dataclass
class DoctorReport:
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add(self, name: str, status: str, detail: str) -> None:
        self.checks.append(Check(name=name, status=status, detail=detail))

    @property
    def failed(self) -> list[Check]:
        return [check for check in self.checks if check.status == "fail"]

    @property
    def warned(self) -> list[Check]:
        return [check for check in self.checks if check.status == "warn"]

    @property
    def ok(self) -> bool:
        return not self.failed


def run_doctor(config: Config, *, skip_github: bool = False, timeout: float = 60.0) -> DoctorReport:
    report = DoctorReport()

    report.add("python", "ok" if sys.version_info >= (3, 11) else "fail", sys.version.split()[0])
    report.add("config", "ok", f"{config.source_path}")
    report.add(
        "allowlist",
        "ok",
        f"{len(config.repos)} repository(ies): {', '.join(sorted(config.repos)) or '<none>'}",
    )

    _check_state_paths(config, report)
    _check_wrapper(config, report, skip_github=skip_github, timeout=timeout)
    _check_runtime_binary(config, report)
    _check_systemd(report)
    _check_vcs_tools(config, report)
    return report


def _check_state_paths(config: Config, report: DoctorReport) -> None:
    """Assert the state/log/lock/worktree placements the design requires."""
    placements = {
        "state_db": config.worker.state_db,
        "run_log_dir": config.worker.run_log_dir,
        "lock_file": config.worker.lock_file,
        "worktree_root": config.worker.worktree_root,
    }
    offenders: list[str] = []
    for name, location in placements.items():
        for slug, repo in config.repos.items():
            if is_within(location, repo.path):
                offenders.append(f"{name} is inside {slug}")
    if offenders:
        report.add("state_placement", "fail", "; ".join(offenders))
    else:
        report.add(
            "state_placement",
            "ok",
            "state db, run logs, lock and worktree root are outside every target repository",
        )

    state_db = config.worker.state_db
    parent = state_db.parent
    if parent.is_dir():
        writable = os.access(parent, os.W_OK)
    else:
        nearest = parent
        while not nearest.exists() and nearest != nearest.parent:
            nearest = nearest.parent
        writable = os.access(nearest, os.W_OK)
    report.add(
        "state_db_writable",
        "ok" if writable else "warn",
        f"{state_db}" + ("" if writable else " (parent not writable yet; created on first run)"),
    )


def _check_wrapper(config: Config, report: DoctorReport, *, skip_github: bool, timeout: float) -> None:
    client = GitHubClient(config.github.command, timeout_seconds=timeout)
    path = client.wrapper_path()
    if path is None:
        report.add(
            "github_wrapper",
            "fail",
            f"not found: {config.github.command!r} — set github.command to the approved wrapper",
        )
        return
    if not os.access(path, os.X_OK):
        report.add("github_wrapper", "fail", f"{path} is not executable")
        return
    report.add("github_wrapper", "ok", str(path))

    if "gh auth git-credential" in config.github.credential_helper:
        report.add("credential_helper", "fail", "configured helper would call raw `gh`, which is not approved here")
    elif config.github.credential_helper_reset != "":
        report.add("credential_helper", "fail", "credential_helper_reset must be empty (it resets the helper list)")
    else:
        report.add(
            "credential_helper",
            "ok",
            "reset-then-wrapper ordering configured (used by #4 for Git operations; not exercised here)",
        )

    if skip_github:
        report.add("github_api", "skip", "--skip-github requested")
        return

    try:
        identity = client.identity()
        report.add("github_api", "ok", f"wrapper authenticated as {identity}")
    except GitHubError as exc:
        status = "fail" if exc.kind in {ErrorKind.AUTH, ErrorKind.MISSING_WRAPPER} else "warn"
        report.add("github_api", status, f"{exc.kind}: {exc}")
        if exc.kind in {ErrorKind.NETWORK, ErrorKind.TIMEOUT, ErrorKind.RATE_LIMIT}:
            report.notes.append("Transient GitHub failure: the worker keeps existing tasks unchanged and retries.")
        return

    required_labels = (config.github.trigger_label, config.github.review_handoff_label)
    for slug in sorted(config.repos):
        try:
            client.repo_accessible(slug)
            report.add(f"repo:{slug}", "ok", "readable with the configured credential")
        except GitHubError as exc:
            status = "fail" if exc.kind in {ErrorKind.DENIED_REPO, ErrorKind.AUTH} else "warn"
            report.add(f"repo:{slug}", status, f"{exc.kind}: {exc}")
            if exc.kind == ErrorKind.DENIED_REPO:
                report.notes.append(
                    f"{slug} is not in the credential's permitted set. Add it to the wrapper's allowed "
                    "repositories; this service will not retry with another identity."
                )
            continue

        try:
            labels = client.list_labels(slug)
        except GitHubError as exc:
            report.add(f"labels:{slug}", "warn", f"could not read labels ({exc.kind}): {exc}")
            continue

        # Only the trigger label matters for dispatch in #3; the review label is
        # reported because #5 will need it, and because its absence today is why
        # the trigger protocol is inert.
        missing = [name for name in required_labels if name not in labels]
        if missing:
            report.add(
                f"labels:{slug}",
                "warn",
                f"missing: {', '.join(missing)} — create intentionally with: "
                f"agent-dispatch setup-labels --repo {slug}",
            )
            report.notes.append(
                f"Until '{config.github.trigger_label}' exists in {slug}, discovery finds nothing. "
                "That is the safe default, not a bug."
            )
        else:
            report.add(f"labels:{slug}", "ok", ", ".join(required_labels))


def _check_runtime_binary(config: Config, report: DoctorReport) -> None:
    configured = config.worker.commandcode_path
    found = shutil.which(configured) if configured else shutil.which("commandcode")
    if found:
        report.add(
            "agent_runtime",
            "ok",
            f"{found} (present; invocation is Issue #4 scope and is NOT exercised here)",
        )
    else:
        report.add(
            "agent_runtime",
            "warn",
            "commandcode not found on PATH; not required for Issue #3, which runs no agent",
        )


def _check_systemd(report: DoctorReport) -> None:
    if not shutil.which("systemctl"):
        report.add("systemd_user", "warn", "systemctl not available; use the foreground `worker` command")
        return
    try:
        state = subprocess.run(
            ["systemctl", "--user", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        report.add("systemd_user", "warn", f"could not query systemd --user: {exc}")
        return

    linger = ""
    username = os.environ.get("USER") or os.environ.get("LOGNAME")
    if username:
        try:
            linger = subprocess.run(
                ["loginctl", "show-user", username, "-p", "Linger", "--value"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            ).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            linger = ""

    detail = f"systemd --user is {state or 'unknown'}"
    if linger == "yes":
        report.add("systemd_user", "ok", detail + "; Linger=yes (worker survives logout)")
    else:
        report.add(
            "systemd_user",
            "warn",
            detail + f"; Linger={linger or 'unknown'} — run `loginctl enable-linger {username or '$USER'}` "
            "so the worker survives logout",
        )


def _check_vcs_tools(config: Config, report: DoctorReport) -> None:
    git = shutil.which("git")
    if not git:
        report.add("git", "fail", "git not found on PATH")
        return
    report.add("git", "ok", git)

    missing: list[str] = []
    for slug, repo in config.repos.items():
        if not (Path(repo.path) / ".git").exists():
            missing.append(slug)
    if missing:
        report.add("source_clones", "warn", f"no Git checkout at configured path: {', '.join(missing)}")
    else:
        report.add(
            "source_clones",
            "ok",
            "configured source paths are Git checkouts; the maintainer's normal checkout is not touched",
        )

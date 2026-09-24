"""Run-log placement and bounded retention.

The *placement* contract is a design requirement that is easy to violate by
accident (§4/§6 of ``docs/architecture.md``):

* run logs live under ``worker.run_log_dir``, which configuration enforces to be
  **outside every target repository and worktree**, so ``git add -A`` can never
  sweep a raw agent transcript into a PR, and the ownership/diff evaluation in
  §4 is not corrupted by an orchestrator-created file;
* retention is bounded by count (``worker.run_log_keep``);
* log *contents* are never printed to CI or pasted into GitHub — ``status``
  reports paths only.
"""

from __future__ import annotations

from pathlib import Path

from .util import repo_slug_dirname


def run_dir(run_log_dir: Path, repo: str, issue_number: int) -> Path:
    """Directory holding the NDJSON logs for one task (created on demand)."""
    return Path(run_log_dir) / repo_slug_dirname(repo) / f"issue-{issue_number}"


def run_log_path(run_log_dir: Path, repo: str, issue_number: int, run_id: str) -> Path:
    """Deterministic per-run log path, outside every repository."""
    return run_dir(run_log_dir, repo, issue_number) / f"{run_id}.ndjson"


def prune(run_log_dir: Path, keep: int) -> list[Path]:
    """Keep the newest ``keep`` log files per task; return the files removed.

    Deterministic (sorted by name, which is timestamp-prefixed by #4), cheap, and
    never touches anything outside ``run_log_dir``.
    """
    removed: list[Path] = []
    root = Path(run_log_dir)
    if keep < 1 or not root.is_dir():
        return removed

    for task_dir in sorted(path for path in root.rglob("*") if path.is_dir()):
        logs = sorted(path for path in task_dir.glob("*.ndjson") if path.is_file())
        if len(logs) <= keep:
            continue
        for path in logs[: len(logs) - keep]:
            try:
                path.unlink()
            except OSError:
                continue
            removed.append(path)
    return removed

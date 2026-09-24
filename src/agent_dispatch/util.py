"""Small shared helpers: timestamps, path expansion, repository slugs."""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path

REPO_SLUG_RE = re.compile(r"^[^/\s]+/[^/\s]+$")
_SAFE_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


def utcnow_iso() -> str:
    """Current UTC time as an ISO-8601 string with an explicit offset."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_repo_slug(value: str) -> bool:
    return bool(REPO_SLUG_RE.match(value))


def repo_slug_dirname(repo: str) -> str:
    """Filesystem-safe directory name for an ``owner/name`` slug.

    Used for log directories. Never used to infer ownership — ownership lives in
    the task row (see ``docs/architecture.md`` §4).
    """
    return _SAFE_SLUG_RE.sub("__", repo)


def expand_path(value: str, *, base: Path | None = None) -> Path:
    """Expand ``~``/``$VAR`` without requiring the path to exist.

    The service stores config, state and logs outside target repositories, so
    paths are usually expressed with ``~`` in the example config.
    """
    expanded = os.path.expanduser(os.path.expandvars(value))
    path = Path(expanded)
    if not path.is_absolute():
        path = (base or Path.cwd()) / path
    return path.resolve(strict=False)


def is_within(child: Path, parent: Path) -> bool:
    """True when ``child`` is ``parent`` or lives underneath it (no symlink games)."""
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def shorten(text: str, limit: int = 200) -> str:
    """Collapse whitespace and truncate, for log lines and CLI tables."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"

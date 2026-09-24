"""agent-dispatch — MVP foundation service (Issue #3).

Scope of this package as of Issue #3:

* one installable CLI, one long-running polling worker, one SQLite database,
* a single-instance lock and concise structured logs,
* GitHub discovery through a **configurable wrapper command** (all GitHub
  access on the development VM goes through ``gh-craftlypse``).

Explicitly **not** implemented here (Issues #4/#5): starting coding agents,
managing worktrees, pushing branches, creating PRs, resuming sessions, and
processing review feedback. Nothing in this package must ever report one of
those capabilities as done.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"

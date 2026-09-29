"""agent-dispatch — polling dispatch service for Issues #3–#6.

Scope of this package:

* one installable CLI, one long-running polling worker, one SQLite database,
* a single-instance lock, bounded limits and concise structured logs,
* GitHub discovery through a **configurable wrapper command** (all GitHub
  access on the development VM goes through ``gh-craftlypse``),
* one owned Git worktree, pinned session and single PR per task (#4), one
  editable Issue status comment with a live heartbeat (#17), and the explicit
  ``agent:fix`` single-round review loop that resumes the **same** session (#5).

Deliberately **not** implemented here: multi-user authorization, automatic
merge or approval, more than one concurrent task, container isolation, or a
provider-plugin framework. Nothing in this package may report one of those as
done.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"

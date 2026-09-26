"""Command-line interface.

Operator surface for Issues #3–#4:

| Command | Purpose |
|---|---|
| ``doctor`` | Verify wrapper, allowlist, labels, state placement, runtime, systemd |
| ``status`` | Show every task row, its phase, and why it is or is not dispatchable |
| ``dry-run`` | One poll that writes nothing to disk and nothing to GitHub |
| ``worker`` | The polling loop: discover, queue, and (from #4) execute one task |
| ``run`` | Execute one queued task now, through to exactly one PR |
| ``open`` | Print the owned branch, worktree, session and PR for a task |
| ``enqueue`` | Queue a labelled Issue now, without waiting for the next poll |
| ``pause`` / ``unpause`` / ``retry`` | Act on a queued task |
| ``resume-publish`` | Finish a publication the model already produced (no model call) |
| ``review`` | Start one review round for a PR that carries the handoff label (#5) |
| ``prune-logs`` | Bounded retention for run logs |
| ``setup-labels`` | Explicit, idempotent, maintainer-invoked label creation |

The ``agent:fix`` review loop (#5) is implemented here: the label on an **open PR
this task owns** is the only thing that starts a round, and review comments on their
own never do.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .doctor import run_doctor
from .enqueue import enqueue_issue
from .github import GitHubClient, GitHubError
from .lockfile import LockBusyError, WorkerLock
from .logging_setup import Logger, make_logger
from .orchestrator import OUTCOME_BLOCKED, OUTCOME_REVIEW_DONE, Orchestrator
from .runlogs import prune
from .store import Store, phase_summary
from .worker import PollOutcome, Worker

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_USAGE = 2
EXIT_BUSY = 3

#: Labels this project intentionally creates. Colours are chosen to stand out from
#: GitHub's defaults so a maintainer can spot dispatch state at a glance.
MANAGED_LABELS: dict[str, tuple[str, str]] = {
    "take-it": ("0E8A16", "Dispatch intent: queue this Issue for an agent"),
    "agent:fix": ("1D76DB", "Explicit handoff: one consolidated review round"),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-dispatch",
        description=(
            "Poll allowlisted GitHub repositories for Issues labelled `take-it`, maintain a durable "
            "idempotent dispatch queue, and execute one task at a time in a task-owned Git worktree "
            "through to a single pull request."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  agent-dispatch doctor\n"
            "  agent-dispatch dry-run\n"
            "  agent-dispatch status\n"
            "  agent-dispatch run\n"
            "  agent-dispatch open --repo owner/name --issue 12\n"
            "  agent-dispatch worker --interval 120\n"
            "  agent-dispatch pause --repo owner/name --issue 12\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"agent-dispatch {__version__}")
    parser.add_argument(
        "--config",
        metavar="PATH",
        help="configuration file (default: ~/.config/agent-dispatch/config.toml)",
    )
    parser.add_argument(
        "--log-format", choices=("text", "json"), default="text", help="log output format"
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="include debug-level log lines"
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    doctor = subparsers.add_parser(
        "doctor", help="verify environment, wrapper, allowlist, labels, state placement"
    )
    doctor.add_argument(
        "--skip-github", action="store_true", help="do local checks only; make no API calls"
    )
    doctor.add_argument(
        "--timeout", type=float, default=60.0, help="per-call wrapper timeout in seconds"
    )

    status = subparsers.add_parser("status", help="show queued tasks and dispatchability")
    status.add_argument("--repo", help="restrict output to one allowlisted repository")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.add_argument(
        "--no-sync", action="store_true", help="read local state only; make no API calls"
    )

    dry = subparsers.add_parser(
        "dry-run",
        help="poll once and print decisions; writes nothing to disk or GitHub",
    )
    dry.add_argument(
        "--no-sync",
        action="store_true",
        help="report local state only; make no API calls",
    )

    worker = subparsers.add_parser("worker", help="run the polling worker in the foreground")
    worker.add_argument("--interval", type=int, help="override worker.poll_interval_seconds")
    worker.add_argument(
        "--once", action="store_true", help="run a single poll and exit (still writes state)"
    )
    worker.add_argument("--timeout", type=int, help="override worker.run_timeout_seconds")
    worker.add_argument(
        "--no-execute",
        action="store_true",
        help="discover and queue only; do not start an agent even for eligible tasks",
    )

    run = subparsers.add_parser(
        "run", help="execute one queued task now, through to exactly one pull request"
    )
    run.add_argument("--repo", help="restrict to one allowlisted repository")
    run.add_argument("--issue", type=int, help="execute this Issue instead of the oldest queued")
    run.add_argument(
        "--skip-poll",
        action="store_true",
        help="do not poll GitHub first; use the existing queue state",
    )
    open_parser = subparsers.add_parser(
        "open", help="show the owned branch, worktree, session and PR for one task"
    )
    open_parser.add_argument("--repo", required=True, help="allowlisted repository (owner/name)")
    open_parser.add_argument("--issue", type=int, required=True, help="Issue number")

    enqueue = subparsers.add_parser(
        "enqueue", help="queue one labelled Issue now instead of waiting for a poll"
    )
    enqueue.add_argument("--repo", required=True, help="allowlisted repository (owner/name)")
    enqueue.add_argument("--issue", type=int, required=True, help="Issue number")

    for name, help_text in (
        ("pause", "pause a queued task (dispatch intent withdrawn locally)"),
        ("unpause", "release a paused task; publish-pending work returns to publication"),
        (
            "retry",
            "re-queue a failed task with a fresh budget (refused while publication is pending)",
        ),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--repo", required=True, help="allowlisted repository (owner/name)")
        sub.add_argument("--issue", type=int, required=True, help="Issue number")

    resume = subparsers.add_parser(
        "resume-publish",
        help="return a paused task whose finished work awaits publication (no model call)",
    )
    resume.add_argument("--repo", required=True, help="allowlisted repository (owner/name)")
    resume.add_argument("--issue", type=int, required=True, help="Issue number")

    review = subparsers.add_parser(
        "review",
        help=(
            "start one review round now for a task whose PR carries the review-handoff label "
            "(resumes the original session; takes the single-instance lock)"
        ),
    )
    review.add_argument("--repo", help="allowlisted repository (owner/name)")
    review.add_argument("--issue", type=int, help="Issue number to review")
    review.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "report which handoffs are pending and what each round would carry, without "
            "claiming a round, clearing a label or starting an agent"
        ),
    )

    prune_parser = subparsers.add_parser(
        "prune-logs", help="delete run logs beyond worker.run_log_keep"
    )
    prune_parser.add_argument("--dry-run", action="store_true", help="report what would be removed")

    labels = subparsers.add_parser(
        "setup-labels",
        help="explicitly create the dispatch labels in an allowlisted repository (idempotent)",
    )
    labels.add_argument("--repo", required=True, help="allowlisted repository (owner/name)")
    labels.add_argument(
        "--yes", action="store_true", help="required acknowledgement that this writes to GitHub"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    log = make_logger(args.log_format, verbose=args.verbose)

    try:
        config = load_config(args.config)
    except ConfigError as exc:
        log.error("config_invalid", error=str(exc))
        return EXIT_FAILURE

    handlers = {
        "doctor": _cmd_doctor,
        "status": _cmd_status,
        "dry-run": _cmd_dry_run,
        "worker": _cmd_worker,
        "run": _cmd_run,
        "open": _cmd_open,
        "enqueue": _cmd_enqueue,
        "pause": _cmd_pause,
        "unpause": _cmd_unpause,
        "retry": _cmd_retry,
        "resume-publish": _cmd_resume_publish,
        "review": _cmd_review,
        "prune-logs": _cmd_prune_logs,
        "setup-labels": _cmd_setup_labels,
    }
    return handlers[args.command](args, config, log)


# --------------------------------------------------------------------- doctor


def _cmd_doctor(args: argparse.Namespace, config: Config, log: Logger) -> int:
    report = run_doctor(config, skip_github=args.skip_github, timeout=args.timeout)

    print(f"agent-dispatch {__version__} — doctor")
    print(f"config: {config.source_path}")
    print()
    for check in report.checks:
        print(f"  {check.symbol} {check.name:<22} {check.detail}")
    if report.notes:
        print()
        for note in report.notes:
            print(f"  note: {note}")
    print()

    if report.failed:
        print(f"{len(report.failed)} check(s) failed.")
        return EXIT_FAILURE
    if report.warned:
        print(f"OK with {len(report.warned)} warning(s).")
        return EXIT_OK
    print("All checks passed.")
    return EXIT_OK


# --------------------------------------------------------------------- status


def _cmd_status(args: argparse.Namespace, config: Config, log: Logger) -> int:
    """Show queue state. **Never writes durable state.**

    ``status`` is an observability command: it must not create or migrate the state
    database and must not become a second, unsynchronised writer alongside the
    worker that owns the single-instance lock (which it does not take). It
    therefore always reads a read-only snapshot:

    * ``--no-sync`` reads the on-disk state directly and makes no API calls;
    * the default reads the on-disk state, performs one poll against a scratch copy,
      and displays what that poll *would* produce — persisting nothing.
    """
    if args.repo is not None:
        config.repo(args.repo)  # allowlist check

    # The store that will be displayed. `--no-sync` reads the on-disk state
    # directly; the default polls a scratch copy and displays that instead. Either
    # way the on-disk file is only ever opened read-only.
    on_disk = Store.open_read_only(config.worker.state_db)
    view: Store = on_disk
    try:
        if not args.no_sync:
            poll = _poll_snapshot(config, on_disk, log)
            if poll.outcome.ok:
                on_disk.close()
                view = poll.store
            else:
                log.warning(
                    "status_sync_incomplete",
                    detail=poll.outcome.error or "one or more repositories were inaccessible",
                    note="showing on-disk state only; nothing was persisted",
                )
                poll.close()

        tasks = view.list_tasks(args.repo)
        counts = view.count_by_phase()
        active = view.active_task_count()

        if args.json:
            payload = {
                "config": str(config.source_path),
                "phases": counts,
                "active_tasks": active,
                "max_concurrent_tasks": config.worker.max_concurrent_tasks,
                "tasks": [
                    {
                        "repo": task.repo,
                        "issue_number": task.issue_number,
                        "title": task.title,
                        "phase": task.phase,
                        "dispatchable": task.dispatchability()[0],
                        "dispatch_blocker": task.dispatchability()[1],
                        "trigger_present": task.trigger_present,
                        "issue_state": task.issue_state,
                        "linked_pr_number": task.linked_pr_number,
                        "linked_pr_state": task.linked_pr_state,
                        "branch": task.branch,
                        "worktree_path": task.worktree_path,
                        "session_id": task.session_id,
                        "pr_number": task.pr_number,
                        "pr_url": task.pr_url,
                        "attempts": task.attempts,
                        "last_error": task.last_error,
                        "last_run_at": task.last_run_at,
                        "pause_reason": task.pause_reason,
                        "observed_at": task.observed_at,
                        "status_comment_id": _status_column(view, task, "comment_id"),
                        "status_comment_state": _status_column(view, task, "last_state"),
                        "status_comment_url": _status_column(view, task, "comment_url"),
                    }
                    for task in tasks
                ],
            }
            print(json.dumps(payload, indent=2))
            return EXIT_OK

        print(f"agent-dispatch {__version__} — status")
        print(f"config: {config.source_path}")
        if not args.no_sync:
            print("view  : read-only snapshot + simulated poll (nothing was persisted)")
        else:
            print("view  : on-disk state only (no API calls)")
        print(f"phases: {phase_summary(counts)}")
        print(
            f"active tasks: {active} / {config.worker.max_concurrent_tasks} "
            "(one task at a time, globally — Issue #4)"
        )
        print()
        if not tasks:
            print(
                "  no tasks recorded. Label an Issue `"
                + config.github.trigger_label
                + "` and run `agent-dispatch dry-run`."
            )
            return EXIT_OK

        header = f"  {'TASK':<28} {'PHASE':<16} {'INTENT':<7} {'PR':<7} DISPATCH"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for task in tasks:
            allowed, blocker = task.dispatchability()
            intent = "yes" if task.trigger_present else "no"
            # An owned PR is the one this worker created; an observed one merely
            # references the Issue. The distinction is the point (see #3's review).
            if task.pr_number is not None:
                pr = f"own#{task.pr_number}"
            elif task.linked_pr_number is not None:
                pr = f"obs#{task.linked_pr_number}"
            else:
                pr = "-"
            state = "ready" if allowed else blocker
            print(f"  {task.ref:<28} {task.phase:<16} {intent:<7} {pr:<7} {state}")
            if task.branch or task.session_id:
                print(f"      branch: {task.branch or '-'}  session: {task.session_id or '-'}")
            comment = view.status_comment(task.repo, task.issue_number)
            if comment is not None:
                shown = f"#{comment.comment_id}" if comment.comment_id else "pending"
                print(
                    f"      status comment: {shown} "
                    f"({comment.last_state or 'unknown'})"
                    + (f" — {comment.last_error}" if comment.last_error else "")
                )
            if task.last_error:
                print(f"      note: {task.last_error}")

        print()
        print("  own#N = a PR this worker created; obs#N = a PR that merely references the Issue.")
        print(
            "  In this release an eligible task is executed, pushed and opened as one PR; "
            "the review loop is Issue #5."
        )
        print(
            "  Each claimed task has ONE status comment on its Issue, edited in place while it "
            "runs (Issue #17)."
        )
        return EXIT_OK
    finally:
        view.close()


# ------------------------------------------------------------------- dry-run


@dataclass
class SnapshotPoll:
    """A poll simulated against a scratch copy of the state database."""

    store: Store
    outcome: PollOutcome
    dispatchable: list[str]

    def close(self) -> None:
        self.store.close()


def _poll_snapshot(config: Config, base: Store, log: Logger) -> SnapshotPoll:
    """Run one poll against a scratch copy of ``base``.

    Shared by ``status`` and ``dry-run`` so both make identical decisions while the
    real state file is opened read-only and never written. The caller always gets a
    snapshot back — including an incomplete one — so it can report honestly what
    happened instead of pretending the poll succeeded.

    The poll logs through a ``simulated`` logger: its decisions are real but its
    writes are not, so an operator reading the journal is never told that an Issue
    was queued when nothing durable happened.
    """
    scratch = base.fork_to_memory()
    simulated_log = Logger(fmt=log.fmt, stream=log.stream, verbose=log.verbose, simulated=True)
    outcome = Worker(config, scratch, simulated_log, reconcile=False).poll_once()
    tasks = scratch.list_tasks()
    dispatchable = [task.ref for task in tasks if task.dispatchability()[0]]
    return SnapshotPoll(store=scratch, outcome=outcome, dispatchable=dispatchable)


def _status_column(view: Store, task, column: str):
    """One field of a task's status-comment row, or ``None`` when it has none.

    ``status`` is a read-only view: this only reads the row the write paths already
    recorded, and never resolves, creates or edits a comment.
    """
    row = view.status_comment(task.repo, task.issue_number)
    return getattr(row, column) if row is not None else None


def _cmd_dry_run(args: argparse.Namespace, config: Config, log: Logger) -> int:
    """One poll with identical decisions, persisting nothing.

    The state database is opened read-only and mirrored into scratch memory, so the
    simulated result matches a real poll while the state file is never opened for
    writing — and GitHub is only ever read.

    ``--no-sync`` keeps the same "persist nothing" guarantee but skips the poll
    entirely, so it makes **no API calls at all**. Without it, running the flag as
    documented would still execute the configured GitHub wrapper.
    """
    base = Store.open_read_only(config.worker.state_db)
    try:
        if not config.worker.state_db.is_file():
            log.info("dry_run_state_empty", source=str(config.worker.state_db))

        if args.no_sync:
            log.info("dry_run_no_sync", note="local state only; the GitHub wrapper is not invoked")
            print()
            print("dry-run summary (local state only — no API calls, nothing written)")
            print(f"  state : {config.worker.state_db}")
            print(f"  phases: {phase_summary(base.count_by_phase())}")
            print("  dispatchable now: suppressed (requires a poll to prove)")
            print("  result: OK (no sync requested)")
            return EXIT_OK

        before = base.count_by_phase()
        poll = _poll_snapshot(config, base, log)
    finally:
        base.close()

    try:
        after = poll.store.count_by_phase()
        dispatchable = poll.dispatchable
        result = poll.outcome
    finally:
        poll.close()

    print()
    print("dry-run summary (nothing was written to disk or GitHub)")
    print(f"  before: {phase_summary(before)}")
    print(f"  after : {phase_summary(after)}")
    if result.result is not None:
        for repo_outcome in result.result.repos:
            if repo_outcome.failed:
                print(
                    f"  {repo_outcome.slug}: FAILED ({repo_outcome.error_kind}) {repo_outcome.error}"
                )
                continue
            print(
                f"  {repo_outcome.slug}: queued={repo_outcome.queued} requeued={repo_outcome.requeued} "
                f"existing_pr={repo_outcome.skipped_has_pr} "
                f"withdrawn={repo_outcome.trigger_withdrawn} finished={repo_outcome.finished}"
            )
            for note in repo_outcome.notes:
                print(f"      note: {note}")
    print(
        f"  dispatchable now: {len(dispatchable)} (an eligible task is executed by `run`/`worker`)"
    )
    for ref in dispatchable[:10]:
        print(f"      {ref}")

    if not result.ok:
        print(f"  result: FAILED — {result.error or 'one or more repositories were inaccessible'}")
        return EXIT_FAILURE
    print("  result: OK")
    return EXIT_OK


# ------------------------------------------------------------------- execute


def _cmd_run(args: argparse.Namespace, config: Config, log: Logger) -> int:
    """Execute one queued task through to a PR.

    Takes the single-instance lock first, so an explicit ``run`` and the systemd
    worker can never have two agents live at once — the MVP allows exactly one
    active task globally, and that guarantee is worth more than the convenience of
    a lock-free shortcut.
    """
    if args.repo is not None:
        config.repo(args.repo)  # allowlist check before anything else

    lock = WorkerLock(
        config.worker.lock_file,
        command=f"agent-dispatch run --config {config.source_path}",
    )
    try:
        lock.acquire()
    except LockBusyError as exc:
        log.error(
            "lock_busy",
            path=str(exc.path),
            holder=str(exc.holder),
            detail="another worker or run holds the lock; refusing to start a second agent",
        )
        return EXIT_BUSY

    try:
        store = Store(config.worker.state_db)
        try:
            client = GitHubClient(config.github.command)
            try:
                client.check_available()
            except GitHubError as exc:
                log.error("run_failed", kind=exc.kind, error=str(exc))
                return EXIT_FAILURE

            if not args.skip_poll:
                # Refresh the queue first, so `run` cannot act on a stale snapshot.
                # This is the same poll the worker performs, not a second rule set.
                # `execute=False` on purpose: this command decides which single task
                # to run *after* the refresh, and letting the poll dispatch first
                # would make the choice implicit.
                outcome = Worker(config, store, log, execute=False).poll_once()
                if not outcome.ok:
                    log.error(
                        "run_poll_failed",
                        detail=outcome.error or "one or more repositories were inaccessible",
                    )
                    return EXIT_FAILURE

            orchestrator = Orchestrator(config, store, client, log)

            # Crash recovery before any new work: an orphaned `running` row, or a
            # branch/PR created but not confirmed, is repaired from evidence
            # instead of being repeated.
            #
            # Review rounds first, for the ordering reason the worker documents: this
            # pass settles a round whose turn already ran, and the implementation passes
            # would misread an open round as an implementation crash — requeuing a
            # killed review turn as a fresh implementation run, or publishing its
            # partial work without acknowledging the feedback. This command is where
            # that matters most, because nothing else here runs the review pass.
            for note in orchestrator.reconcile_review_rounds():
                print(f"review reconcile: {note}")
            for note in orchestrator.reconcile():
                print(f"reconcile: {note}")

            if args.issue is not None:
                if args.repo is None:
                    print("--issue requires --repo so the task can be identified", file=sys.stderr)
                    return EXIT_USAGE
                task = store.get_task(args.repo, args.issue)
                if task is None:
                    print(f"{args.repo}#{args.issue}: no task row recorded", file=sys.stderr)
                    return EXIT_FAILURE
                result = orchestrator.dispatch_task(task)
            else:
                result = orchestrator.dispatch_next()

            print(f"result: {result.summary()}")
            for note in result.notes:
                print(f"  note: {note}")
            if result.run is not None:
                for name, ok in result.run.validation.checks.items():
                    print(f"  check {name}: {'pass' if ok else 'FAIL'}")
                if result.run.log_path is not None:
                    # Path only, never contents: raw transcripts stay out of the
                    # terminal and out of anything an operator might paste.
                    print(f"  run log: {result.run.log_path}")

            if result.action == OUTCOME_BLOCKED:
                return EXIT_OK
            if result.dispatched:
                return EXIT_OK
            return EXIT_FAILURE
        finally:
            store.close()
    finally:
        lock.release()
        log.info("lock_released", path=str(config.worker.lock_file))


# ---------------------------------------------------------------------- open


def _cmd_open(args: argparse.Namespace, config: Config, log: Logger) -> int:
    """Print the paths and IDs needed to inspect a task by hand.

    Read-only by construction: it opens the state database without write access, so
    a look-up can never create or migrate the file. Nothing here resumes a session
    — the operator can copy the session ID and resume it in the worktree terminal,
    which is the supported handoff (no native VS Code Chat integration is claimed).
    """
    config.repo(args.repo)
    store = Store.open_read_only(config.worker.state_db)
    try:
        task = store.get_task(args.repo, args.issue)
        if task is None:
            print(f"{args.repo}#{args.issue}: no task recorded", file=sys.stderr)
            return EXIT_FAILURE

        runs = store.run_history(task.id)
        resumable = store.resumable_session(task.id)
        status_row = store.status_comment(task.repo, task.issue_number)
        print(f"{task.ref} — {task.title or '(no title)'}")
        print(
            f"  phase          : {task.phase}"
            + (f" ({task.pause_reason})" if task.pause_reason else "")
        )
        print(f"  branch         : {task.branch or '-'}")
        print(f"  worktree       : {task.worktree_path or '-'}")
        print(f"  base           : {task.base_branch or '-'}")
        print(
            "  owned PR       : "
            + (f"#{task.pr_number} {task.pr_url or ''}" if task.pr_number else "-")
        )
        print(
            "  observed PR    : "
            + (
                f"#{task.linked_pr_number} ({task.linked_pr_state})"
                if task.linked_pr_number
                else "-"
            )
        )
        print(
            f"  pinned runtime : {task.runtime_driver} / {task.runtime_model} / {task.runtime_effort}"
        )
        print(f"  attempts       : {task.attempts}")
        print(f"  session        : {task.session_id or '-'}")
        if status_row is not None:
            print(
                "  status comment : "
                + (
                    f"#{status_row.comment_id} {status_row.comment_url or ''}"
                    if status_row.comment_id
                    else "not created yet (ownership intended)"
                )
            )
            print(f"  comment state  : {status_row.last_state or '-'}")
            if status_row.published_at:
                print(f"  comment edit   : {status_row.published_at}")
            if status_row.last_error:
                print(f"  comment warning: {status_row.last_error}")
        print(
            "  resumable      : "
            + (
                f"yes — {resumable}"
                if resumable
                else "no — a session is resumable only after a run that completed cleanly"
            )
        )
        if task.last_error:
            print(f"  last message   : {task.last_error}")
        if runs:
            print("  runs:")
            for run in runs:
                outcome = (
                    run.outcome
                    + (" (blocked tools)" if run.tool_hook_blocked else "")
                    + (" (timed out)" if run.timed_out else "")
                )
                print(
                    f"    {run.run_id}  {run.kind:<16} {outcome:<24} "
                    f"session={run.session_id or '-'} exit={run.exit_code} "
                    f"work={run.produced_work}"
                )
                if run.log_path:
                    print(f"      log: {run.log_path}")

        if task.worktree_path and task.branch and task.session_id and resumable:
            print()
            print("  to inspect or resume the session in a terminal:")
            print(f"    cd {task.worktree_path}")
            print(f"    commandcode --session {task.session_id}")
        return EXIT_OK
    finally:
        store.close()


# -------------------------------------------------------------------- worker


def _cmd_worker(args: argparse.Namespace, config: Config, log: Logger) -> int:
    if args.interval is not None:
        if args.interval < 1:
            log.error("invalid_interval", interval=args.interval)
            return EXIT_USAGE
        object.__setattr__(config.worker, "poll_interval_seconds", args.interval)
    if args.timeout is not None:
        object.__setattr__(config.worker, "run_timeout_seconds", args.timeout)

    lock = WorkerLock(
        config.worker.lock_file,
        command=f"agent-dispatch worker --config {config.source_path}",
    )
    try:
        lock.acquire()
    except LockBusyError as exc:
        log.error(
            "lock_busy",
            path=str(exc.path),
            holder=str(exc.holder),
            detail="another worker already holds the lock; refusing to start a second one",
        )
        return EXIT_BUSY

    try:
        store = Store(config.worker.state_db)
        try:
            worker = Worker(config, store, log, execute=not args.no_execute)
            if args.once:
                log.info(
                    "worker_once",
                    note="single poll"
                    + ("" if not args.no_execute else "; agent execution disabled by --no-execute"),
                )
                outcome = worker.poll_once()
                if outcome.dispatch is not None:
                    print(f"dispatch: {outcome.dispatch.summary()}")
                    for note in outcome.dispatch.notes:
                        print(f"  note: {note}")
                return EXIT_OK if outcome.ok else EXIT_FAILURE
            return worker.run_forever()
        finally:
            store.close()
    finally:
        lock.release()
        log.info("lock_released", path=str(config.worker.lock_file))


# ------------------------------------------------------------------- enqueue


def _cmd_enqueue(args: argparse.Namespace, config: Config, log: Logger) -> int:
    """Queue one Issue using the same decision path as the polling worker.

    Deliberately not a second, simpler eligibility check: `enqueue` runs the real
    discovery pass and reports its verdict, so it cannot queue an Issue that a poll
    would refuse (a PR number, a closed Issue, a missing label, or an Issue that
    already has a PR).
    """
    repo = config.repo(args.repo)
    client = GitHubClient(config.github.command)
    store = Store(config.worker.state_db)
    try:
        outcome = enqueue_issue(config, store, log, repo, args.issue, client=client)
        print(outcome.message)
        return EXIT_OK if outcome.accepted else EXIT_FAILURE
    finally:
        store.close()


# ------------------------------------------------- pause / unpause / retry


def _cmd_pause(args: argparse.Namespace, config: Config, log: Logger) -> int:
    return _mutate(args, config, log, "pause")


def _cmd_unpause(args: argparse.Namespace, config: Config, log: Logger) -> int:
    return _mutate(args, config, log, "unpause")


def _cmd_retry(args: argparse.Namespace, config: Config, log: Logger) -> int:
    return _mutate(args, config, log, "retry")


def _cmd_resume_publish(args: argparse.Namespace, config: Config, log: Logger) -> int:
    """Finish publication for a paused publish-pending task.

    The publish-pending counterpart of ``retry``: it commits, pushes and opens the PR
    for work the model already completed, and never starts a runtime.

    It takes the single-instance lock and runs the same publish-only reconciliation the
    worker does, rather than only moving the row and relying on a live worker to notice.
    Re-arming alone would leave an already-running service to pick it up on a later
    poll; doing the work here means the command's stated effect is what actually
    happens. When the worker does hold the lock, the command reports that it has
    re-armed the task and the running worker will finish it — which the worker's
    per-poll pass guarantees.

    Exits 0 only when the work is actually published, or when a live worker holds the
    lock and has explicitly taken responsibility for the next poll. A second failure
    leaves the task publish-pending, and says so with a non-zero exit rather than
    reporting success for an attempt that did not land.
    """
    config.repo(args.repo)  # allowlist check before anything else
    store = Store(config.worker.state_db)
    try:
        try:
            task = store.resume_publication(args.repo, args.issue)
        except ValueError as exc:
            log.error(
                "action_rejected",
                action="resume-publish",
                repo=args.repo,
                issue=args.issue,
                error=str(exc),
            )
            return EXIT_FAILURE
        print(f"{task.ref}: publishing re-armed → {task.phase}")
    finally:
        store.close()

    lock = WorkerLock(
        config.worker.lock_file,
        command=f"agent-dispatch resume-publish --config {config.source_path}",
    )
    try:
        lock.acquire()
    except LockBusyError as exc:
        # The running worker owns publication: it reconciles publish-pending tasks on
        # every poll, so re-arming the row is genuinely sufficient here.
        log.info(
            "publish_deferred_to_worker",
            path=str(exc.path),
            holder=str(exc.holder),
            detail="the running worker will finish this publication on its next poll",
        )
        print(
            "a worker holds the lock; the task is re-armed and will be published on its next poll"
        )
        return EXIT_OK

    try:
        store = Store(config.worker.state_db)
        try:
            client = GitHubClient(config.github.command)
            try:
                client.check_available()
            except GitHubError as exc:
                log.error("publish_failed", kind=exc.kind, error=str(exc))
                return EXIT_FAILURE
            orchestrator = Orchestrator(config, store, client, log)
            notes = orchestrator.reconcile_publish_pending()
            for note in notes:
                print(f"publish: {note}")
            # Publication may have concluded for this task, so its Issue status
            # comment is brought up to date in the same command rather than waiting
            # for the next worker poll.
            fresh = store.get_task(args.repo, args.issue)
            if fresh is not None:
                orchestrator.sync_task_status(fresh)
            return _resume_publish_result(args, store, log, notes)
        finally:
            store.close()
    finally:
        lock.release()


# ------------------------------------------------------------------- review


def _cmd_review(args: argparse.Namespace, config: Config, log: Logger) -> int:
    """Start one review round for a task whose PR carries the handoff label.

    Structured exactly like ``run``: same single-instance lock, same shared code
    path, and the same "reconcile first, then act" order. The lock matters here for
    the same reason it does for ``run`` — a review round resumes a session in a task's
    worktree, and the MVP allows exactly one agent at a time globally. A second
    process doing this concurrently would put two writers in one worktree.

    ``--dry-run`` reports what each pending handoff *would* carry and claims nothing,
    so an operator can check why a label that is visibly present is not starting a
    round without consuming the handoff. It opens the database read-only, so it cannot
    create or migrate state either.
    """
    if args.repo is not None:
        config.repo(args.repo)  # allowlist check before anything else
    if args.dry_run:
        return _review_dry_run(args, config, log)

    lock = WorkerLock(
        config.worker.lock_file,
        command=f"agent-dispatch review --config {config.source_path}",
    )
    try:
        lock.acquire()
    except LockBusyError as exc:
        log.error(
            "lock_busy",
            path=str(exc.path),
            holder=str(exc.holder),
            detail="a worker or run holds the lock; refusing to start a second agent",
        )
        return EXIT_BUSY

    store = Store(config.worker.state_db)
    try:
        client = GitHubClient(config.github.command)
        try:
            client.check_available()
        except GitHubError as exc:
            log.error("review_failed", kind=exc.kind, error=str(exc))
            return EXIT_FAILURE

        orchestrator = Orchestrator(config, store, client, log)

        # Crash recovery first, exactly as `run` does: a round interrupted by a dead
        # process must be settled before a new one is considered, or the new round
        # would start on top of a task whose state another round still owns.
        for note in orchestrator.reconcile_review_rounds():
            print(f"review reconcile: {note}")
        for note in orchestrator.reconcile_publish_pending():
            print(f"publish reconcile: {note}")

        if args.issue is not None:
            if args.repo is None:
                print("--issue requires --repo so the task can be identified", file=sys.stderr)
                return EXIT_USAGE
            task = store.get_task(args.repo, args.issue)
            if task is None:
                print(f"{args.repo}#{args.issue}: no task row recorded", file=sys.stderr)
                return EXIT_FAILURE
            outcomes = [orchestrator.dispatch_review_round(task)]
        else:
            outcomes = orchestrator.dispatch_review_rounds(only_repo=args.repo)

        if not outcomes:
            print("no task has a pending review handoff (label present, round not yet claimed)")
            return EXIT_OK

        code = EXIT_OK
        for outcome in outcomes:
            print(f"result: {outcome.summary()}")
            for note in outcome.notes:
                print(f"  note: {note}")
            if outcome.run is not None:
                for name, ok in outcome.run.validation.checks.items():
                    print(f"  check {name}: {'pass' if ok else 'FAIL'}")
                if outcome.run.log_path is not None:
                    print(f"  run log: {outcome.run.log_path}")
            if outcome.action == OUTCOME_REVIEW_DONE:
                continue
            if outcome.action == OUTCOME_BLOCKED:
                # Deferrals and skips are not failures: nothing was attempted because
                # nothing was ready. Reporting a non-zero exit for "the label is not
                # there yet" would make a scheduled invocation look broken.
                continue
            code = EXIT_FAILURE
        return code
    finally:
        store.close()
        lock.release()
        log.info("lock_released", path=str(config.worker.lock_file))


def _review_dry_run(args: argparse.Namespace, config: Config, log: Logger) -> int:
    """Report pending handoffs and what each round would carry. Claims nothing.

    Reads through the read-only store, so it cannot create, migrate or write the state
    database — the same guarantee ``status`` and ``dry-run`` provide. Nothing is
    claimed, no label is cleared, and no agent is started; the decision is recomputed
    from live GitHub state exactly as a real round would see it.
    """

    store = Store.open_read_only(config.worker.state_db)
    try:
        client = GitHubClient(config.github.command, timeout_seconds=30.0)
        try:
            client.check_available()
        except GitHubError as exc:
            print(f"cannot reach the GitHub wrapper: {exc.kind}: {exc}", file=sys.stderr)
            return EXIT_FAILURE

        orchestrator = Orchestrator(config, store, client, log, status=False)
        tasks = [
            task
            for task in store.list_tasks(args.repo)
            if task.phase == "awaiting_review" and not task.is_terminal
        ]
        if not tasks:
            print("no task is awaiting review, so no handoff can be pending")
            return EXIT_OK

        pending = 0
        for task in tasks:
            decision = orchestrator.review_loop().evaluate(task, config.repo(task.repo))
            print(f"{task.ref}: {decision.action} ({decision.reason})")
            print(f"  {decision.message}")
            if decision.claimed:
                pending += 1
                new = sorted(decision.claimed)
                print(
                    f"  would claim {len(new)} feedback item(s), new since the acknowledged cursor:"
                )
                print(f"    {len(new)} item id(s) recorded in the round snapshot")
                print(f"  would resume session {decision.session_id}")
                print(f"  would push to PR #{decision.pr_number} on branch {task.branch}")
            for note in decision.notes:
                print(f"  note: {note}")
        print()
        if pending:
            print(
                f"{pending} handoff(s) ready. Run `agent-dispatch review` (without --dry-run) to "
                "start at most one round."
            )
        else:
            print(
                "No handoff is claimable right now. A round needs the review-handoff label on an "
                "OPEN PR this task owns, new or edited feedback since the last round, and a cleanly "
                "completed session. A label left in place is not a repeated request: remove it and "
                "add it again for another round."
            )
        return EXIT_OK
    finally:
        store.close()


def _resume_publish_result(
    args: argparse.Namespace, store: Store, log: Logger, notes: list[str]
) -> int:
    """Report whether the attempted publication actually completed.

    The command promises to *finish* publication, so its exit status has to describe
    the outcome rather than the attempt. A second failure leaves the task exactly where
    it was — publish-pending with no owned PR — and reporting shell success for that
    would be a lie an operator or a script could act on. The durable task state is
    re-read rather than inferred from ``notes``, which describe what was tried.
    """
    task = store.get_task(args.repo, args.issue)
    if task is None:
        print(f"{args.repo}#{args.issue}: no task row recorded", file=sys.stderr)
        return EXIT_FAILURE
    if task.pr_number is not None:
        if not notes:
            # Already published before this attempt: nothing was pending after all.
            print(f"{task.ref}: nothing left to publish")
        else:
            print(f"{task.ref}: publication finished → {task.phase} (PR #{task.pr_number})")
        return EXIT_OK

    # Still publish-pending with no owned PR: the attempt did not get the work
    # published, whatever the notes said about it.
    detail = " ".join(notes[-1:]) if notes else "no progress was recorded"
    log.error(
        "publish_incomplete",
        repo=task.repo,
        issue=task.issue_number,
        phase=task.phase,
        recovery_stage=task.recovery_stage,
        detail=detail,
    )
    print(
        f"{task.ref}: publication is still incomplete → {task.phase}"
        + (f" (recovery_stage={task.recovery_stage})" if task.recovery_stage else "")
        + (
            ". The task is unchanged and remains recoverable; re-run `agent-dispatch run` "
            "once the cause is fixed, or let the worker's next poll retry it."
            if task.is_publish_pending
            else ". It is not awaiting publication any more; check the note above."
        ),
        file=sys.stderr,
    )
    return EXIT_FAILURE


def _mutate(args: argparse.Namespace, config: Config, log: Logger, action: str) -> int:
    config.repo(args.repo)
    store = Store(config.worker.state_db)
    try:
        try:
            task = getattr(store, action)(args.repo, args.issue)
        except ValueError as exc:
            log.error(
                "action_rejected", action=action, repo=args.repo, issue=args.issue, error=str(exc)
            )
            return EXIT_FAILURE
        log.info(
            "action_applied",
            action=action,
            repo=task.repo,
            issue=task.issue_number,
            phase=task.phase,
        )
        print(
            f"{task.ref}: {action} → {task.phase}"
            + (f" ({task.last_error})" if task.last_error else "")
        )
        # The Issue status comment follows the durable state, so an operator command
        # cannot leave a comment describing the phase before it. Best-effort by
        # design: an unreachable GitHub must not fail a command that already changed
        # local state, and never rolls the change back.
        _sync_status_after_mutation(config, store, log, task)
        return EXIT_OK
    finally:
        store.close()


def _sync_status_after_mutation(config: Config, store: Store, log: Logger, task) -> None:
    """Update one task's Issue status comment after a local transition.

    Never fatal and never a precondition: the mutation has already been applied and
    recorded, and a GitHub outage must not turn a successful local change into a
    reported failure. A task that never owned a comment is a no-op.
    """
    try:
        Orchestrator(config, store, GitHubClient(config.github.command), log).sync_task_status(task)
    except Exception as exc:  # noqa: BLE001 - status reporting is never fatal
        log.warning(
            "status_sync_failed",
            repo=task.repo,
            issue=task.issue_number,
            error=f"{type(exc).__name__}: {exc}",
            detail="the task's own state was already changed and is unaffected",
        )


# -------------------------------------------------------------- prune-logs


def _cmd_prune_logs(args: argparse.Namespace, config: Config, log: Logger) -> int:
    run_log_dir = config.worker.run_log_dir
    keep = config.worker.run_log_keep
    if not run_log_dir.is_dir():
        print(f"no run logs to prune ({run_log_dir} does not exist)")
        return EXIT_OK

    logs = sorted(Path(run_log_dir).rglob("*.ndjson"))
    if args.dry_run:
        print(f"{len(logs)} log(s) under {run_log_dir}; keeping the newest {keep} per task")
        return EXIT_OK

    removed = prune(run_log_dir, keep)
    log.info("run_logs_pruned", removed=len(removed), keep=keep, dir=str(run_log_dir))
    print(f"removed {len(removed)} log file(s); retained up to {keep} per task")
    return EXIT_OK


# ------------------------------------------------------------- setup-labels


def _cmd_setup_labels(args: argparse.Namespace, config: Config, log: Logger) -> int:
    repo = config.repo(args.repo)
    if not args.yes:
        print(
            "setup-labels writes to GitHub (creating labels in "
            f"{repo.slug}). Re-run with --yes to confirm."
        )
        return EXIT_USAGE

    client = GitHubClient(config.github.command)
    try:
        existing = client.list_labels(repo.slug)
    except GitHubError as exc:
        log.error("label_read_failed", repo=repo.slug, kind=exc.kind, error=str(exc))
        return EXIT_FAILURE

    wanted = [
        (
            config.github.trigger_label,
            *MANAGED_LABELS.get(config.github.trigger_label, ("0E8A16", "Dispatch intent")),
        ),
        (
            config.github.review_handoff_label,
            *MANAGED_LABELS.get(config.github.review_handoff_label, ("1D76DB", "Review handoff")),
        ),
    ]

    created = 0
    for name, color, description in wanted:
        if name in existing:
            print(f"  {name}: already present")
            continue
        try:
            if client.create_label(repo.slug, name, color=color, description=description):
                created += 1
                print(f"  {name}: created")
            else:
                print(f"  {name}: already present")
        except GitHubError as exc:
            log.error(
                "label_create_failed", repo=repo.slug, label=name, kind=exc.kind, error=str(exc)
            )
            return EXIT_FAILURE

    log.info("labels_ready", repo=repo.slug, created=created)
    print(
        f"{repo.slug}: {created} label(s) created. The trigger protocol is now live for this repository."
    )
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    sys.exit(main())

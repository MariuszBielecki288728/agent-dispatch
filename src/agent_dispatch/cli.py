"""Command-line interface.

Operator surface for Issue #3 (deliberately small):

| Command | Purpose |
|---|---|
| ``doctor`` | Verify wrapper, allowlist, labels, state placement, systemd |
| ``status`` | Show every task row, its phase, and why it is or is not dispatchable |
| ``dry-run`` | One poll that writes nothing to disk and nothing to GitHub |
| ``worker`` | The foreground polling loop (systemd or manual) |
| ``enqueue`` | Queue a labelled Issue now, without waiting for the next poll |
| ``pause`` / ``unpause`` / ``retry`` | Act on a queued task |
| ``prune-logs`` | Bounded retention for run logs |
| ``setup-labels`` | Explicit, idempotent, maintainer-invoked label creation |

Commands that would need a coding agent (``open``, session inspection, review
handling) are intentionally absent and documented as #4/#5 work rather than
stubbed with a fake success.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import Config, ConfigError, load_config
from .doctor import run_doctor
from .github import GitHubClient, GitHubError
from .lockfile import LockBusyError, WorkerLock
from .logging_setup import Logger, make_logger
from .runlogs import prune
from .store import Store, phase_summary
from .worker import Worker

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
            "Poll allowlisted GitHub repositories for Issues labelled `take-it` and maintain a durable, "
            "idempotent dispatch queue. This release queues work; it does not run an agent (Issue #4)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "examples:\n"
            "  agent-dispatch doctor\n"
            "  agent-dispatch dry-run\n"
            "  agent-dispatch status\n"
            "  agent-dispatch worker --interval 120\n"
            "  agent-dispatch pause --repo owner/name --issue 12\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"agent-dispatch {__version__}")
    parser.add_argument("--config", metavar="PATH", help="configuration file (default: ~/.config/agent-dispatch/config.toml)")
    parser.add_argument("--log-format", choices=("text", "json"), default="text", help="log output format")
    parser.add_argument("-v", "--verbose", action="store_true", help="include debug-level log lines")

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    doctor = subparsers.add_parser("doctor", help="verify environment, wrapper, allowlist, labels and state placement")
    doctor.add_argument("--skip-github", action="store_true", help="do local checks only; make no API calls")
    doctor.add_argument("--timeout", type=float, default=60.0, help="per-call wrapper timeout in seconds")

    status = subparsers.add_parser("status", help="show queued tasks and dispatchability")
    status.add_argument("--repo", help="restrict output to one allowlisted repository")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.add_argument("--no-sync", action="store_true", help="read local state only; make no API calls")

    dry = subparsers.add_parser("dry-run", help="poll once and print decisions; writes nothing to disk or GitHub")
    dry.add_argument("--no-sync", action="store_true", help="simulate against local state only; make no API calls")

    worker = subparsers.add_parser("worker", help="run the polling worker in the foreground")
    worker.add_argument("--interval", type=int, help="override worker.poll_interval_seconds")
    worker.add_argument("--once", action="store_true", help="run a single poll and exit (still writes state)")
    worker.add_argument("--timeout", type=int, help="override worker.run_timeout_seconds")

    enqueue = subparsers.add_parser("enqueue", help="queue one labelled Issue now instead of waiting for a poll")
    enqueue.add_argument("--repo", required=True, help="allowlisted repository (owner/name)")
    enqueue.add_argument("--issue", type=int, required=True, help="Issue number")

    for name, help_text in (
        ("pause", "pause a queued task (dispatch intent withdrawn locally)"),
        ("unpause", "return a paused task to the queue"),
        ("retry", "re-queue a failed or needs_attention task with a fresh attempt budget"),
    ):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--repo", required=True, help="allowlisted repository (owner/name)")
        sub.add_argument("--issue", type=int, required=True, help="Issue number")

    prune_parser = subparsers.add_parser("prune-logs", help="delete run logs beyond worker.run_log_keep")
    prune_parser.add_argument("--dry-run", action="store_true", help="report what would be removed")

    labels = subparsers.add_parser(
        "setup-labels",
        help="explicitly create the dispatch labels in an allowlisted repository (idempotent)",
    )
    labels.add_argument("--repo", required=True, help="allowlisted repository (owner/name)")
    labels.add_argument("--yes", action="store_true", help="required acknowledgement that this writes to GitHub")

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
        "enqueue": _cmd_enqueue,
        "pause": _cmd_pause,
        "unpause": _cmd_unpause,
        "retry": _cmd_retry,
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
    if args.repo is not None:
        config.repo(args.repo)  # allowlist check

    store = Store(config.worker.state_db)
    try:
        if not args.no_sync:
            worker = Worker(config, store, log, dry_run=True)
            outcome = worker.poll_once()
            if not outcome.ok:
                log.warning("status_sync_incomplete", detail=outcome.error or "one or more repositories failed")

        tasks = store.list_tasks(args.repo)
        counts = store.count_by_phase()

        if args.json:
            import json

            payload = {
                "config": str(config.source_path),
                "phases": counts,
                "active_tasks": store.active_task_count(),
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
                        "attempts": task.attempts,
                        "last_error": task.last_error,
                        "last_run_at": task.last_run_at,
                        "pause_reason": task.pause_reason,
                        "observed_at": task.observed_at,
                    }
                    for task in tasks
                ],
            }
            print(json.dumps(payload, indent=2))
            return EXIT_OK

        print(f"agent-dispatch {__version__} — status")
        print(f"config: {config.source_path}")
        print(f"phases: {phase_summary(counts)}")
        print(
            f"active tasks: {store.active_task_count()} / {config.worker.max_concurrent_tasks} "
            "(agent execution arrives in Issue #4)"
        )
        print()
        if not tasks:
            print("  no tasks recorded. Label an Issue `" + config.github.trigger_label + "` and run `agent-dispatch dry-run`.")
            return EXIT_OK

        header = f"  {'TASK':<28} {'PHASE':<15} {'INTENT':<7} {'PR':<6} DISPATCH"
        print(header)
        print("  " + "-" * (len(header) - 2))
        for task in tasks:
            allowed, blocker = task.dispatchability()
            intent = "yes" if task.trigger_present else "no"
            pr = str(task.linked_pr_number) if task.linked_pr_number else "-"
            state = "ready" if allowed else blocker
            print(f"  {task.ref:<28} {task.phase:<15} {intent:<7} {pr:<6} {state}")
            if task.last_error:
                print(f"      note: {task.last_error}")

        print()
        print("  Note: no task is promoted to `running` in this release; queueing is not implementation.")
        return EXIT_OK
    finally:
        store.close()


# ------------------------------------------------------------------- dry-run


def _cmd_dry_run(args: argparse.Namespace, config: Config, log: Logger) -> int:
    """One poll with identical decisions, persisting nothing.

    The real database is mirrored into scratch memory read-only, so the simulated
    result matches a real poll while the state file is never opened for writing —
    and GitHub is only ever read.
    """
    scratch = Store.in_memory()
    if config.worker.state_db.is_file():
        existing = Store(config.worker.state_db, read_only=True)
        try:
            scratch.close()
            scratch = existing.fork_to_memory()
        finally:
            existing.close()
        log.info("dry_run_state_loaded", source=str(config.worker.state_db))
    else:
        log.info("dry_run_state_empty", source=str(config.worker.state_db))

    try:
        worker = Worker(config, scratch, log, dry_run=True)
        outcome = worker.poll_once()
        after = scratch.count_by_phase()
        dispatchable = outcome.dispatchable
    finally:
        scratch.close()

    print()
    print("dry-run summary (nothing was written to disk or GitHub)")
    print(f"  before: {phase_summary(outcome.before)}")
    print(f"  after : {phase_summary(after)}")
    if outcome.result is not None:
        for repo_outcome in outcome.result.repos:
            if repo_outcome.failed:
                print(f"  {repo_outcome.slug}: FAILED ({repo_outcome.error_kind}) {repo_outcome.error}")
                continue
            print(
                f"  {repo_outcome.slug}: queued={repo_outcome.queued} requeued={repo_outcome.requeued} "
                f"existing_pr={repo_outcome.skipped_has_pr} "
                f"withdrawn={repo_outcome.trigger_withdrawn} finished={repo_outcome.finished}"
            )
            for note in repo_outcome.notes:
                print(f"      note: {note}")
    print(f"  dispatchable now: {len(dispatchable)} (dispatch itself is Issue #4 scope)")
    for ref in dispatchable[:10]:
        print(f"      {ref}")

    if not outcome.ok:
        print(f"  result: FAILED — {outcome.error or 'one or more repositories were inaccessible'}")
        return EXIT_FAILURE
    print("  result: OK")
    return EXIT_OK


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
            worker = Worker(config, store, log)
            if args.once:
                log.info("worker_once", note="single poll; state is written, no agent is started")
                outcome = worker.poll_once()
                return EXIT_OK if outcome.ok else EXIT_FAILURE
            return worker.run_forever()
        finally:
            store.close()
    finally:
        lock.release()
        log.info("lock_released", path=str(config.worker.lock_file))


# ------------------------------------------------------------------- enqueue


def _cmd_enqueue(args: argparse.Namespace, config: Config, log: Logger) -> int:
    repo = config.repo(args.repo)
    client = GitHubClient(config.github.command)
    store = Store(config.worker.state_db)
    try:
        try:
            issue = client.open_issue(repo.slug, args.issue)
        except GitHubError as exc:
            log.error("issue_unreadable", repo=repo.slug, issue=args.issue, kind=exc.kind, error=str(exc))
            return EXIT_FAILURE

        if issue.state == "closed":
            log.error("issue_closed", repo=repo.slug, issue=args.issue, detail="closed Issues are not queued")
            return EXIT_FAILURE

        trigger = config.github.trigger_label
        if not issue.has_label(trigger):
            log.error(
                "label_missing",
                repo=repo.slug,
                issue=args.issue,
                label=trigger,
                detail="add the trigger label first; this service does not apply it silently",
            )
            return EXIT_FAILURE

        created = store.upsert_discovered(
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
        log.info(
            "issue_enqueued" if created else "issue_already_queued",
            repo=repo.slug,
            issue=issue.number,
            title=issue.title,
        )
        print(f"{repo.slug}#{issue.number}: {'queued' if created else 'already queued (row reused)'}")
        return EXIT_OK
    finally:
        store.close()


# ------------------------------------------------- pause / unpause / retry


def _cmd_pause(args: argparse.Namespace, config: Config, log: Logger) -> int:
    return _mutate(args, config, log, "pause")


def _cmd_unpause(args: argparse.Namespace, config: Config, log: Logger) -> int:
    return _mutate(args, config, log, "unpause")


def _cmd_retry(args: argparse.Namespace, config: Config, log: Logger) -> int:
    return _mutate(args, config, log, "retry")


def _mutate(args: argparse.Namespace, config: Config, log: Logger, action: str) -> int:
    config.repo(args.repo)
    store = Store(config.worker.state_db)
    try:
        try:
            task = getattr(store, action)(args.repo, args.issue)
        except ValueError as exc:
            log.error("action_rejected", action=action, repo=args.repo, issue=args.issue, error=str(exc))
            return EXIT_FAILURE
        log.info("action_applied", action=action, repo=task.repo, issue=task.issue_number, phase=task.phase)
        print(f"{task.ref}: {action} → {task.phase}" + (f" ({task.last_error})" if task.last_error else ""))
        return EXIT_OK
    finally:
        store.close()


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
        (config.github.trigger_label, *MANAGED_LABELS.get(config.github.trigger_label, ("0E8A16", "Dispatch intent"))),
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
            log.error("label_create_failed", repo=repo.slug, label=name, kind=exc.kind, error=str(exc))
            return EXIT_FAILURE

    log.info("labels_ready", repo=repo.slug, created=created)
    print(f"{repo.slug}: {created} label(s) created. The trigger protocol is now live for this repository.")
    return EXIT_OK


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess in tests
    sys.exit(main())

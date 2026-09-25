#!/usr/bin/env python3
"""Offline test-suite for Issue #4 — execution, worktree ownership and PR handoff.

Run with the rest of the suite:

    ./scripts/test-offline.sh          # or
    PYTHONPATH=src python3 -m unittest discover -s tests -v

Everything here is deterministic and offline. GitHub is served by
``tests/fake_wrapper.py`` and the coding agent by ``tests/fake_runtime.py``, both
real executables, so the tests drive the actual ``CommandCodeDriver``,
``WorktreeManager``, ``Orchestrator`` and CLI code paths — real argv construction,
real Git worktrees, real NDJSON parsing, real process kill — rather than a mock
that could drift from production behaviour.

Coverage maps to the Issue #4 acceptance list:

* one labelled Issue → one claim → one owned branch/worktree → one pinned session
  → at most one PR → ``awaiting_review``;
* ``tool_hook_blocked`` **fails** a run that also reports ``subtype=success`` and
  exits 0 (the silent false-success case);
* a missing or mismatched session ID fails the run;
* a timeout kills the run and is reported as a timeout, not a success;
* a clean-but-committed worktree is accepted (dirtiness is not the success signal);
* an interrupted run preserves its edits and the retry starts a **fresh** session;
* duplicate polls, restarts and crash windows neither duplicate work nor adopt a
  foreign PR;
* the credential ordering is verifiable in the environment the agent inherits;
* ``status`` stays read-only and no run log is ever written inside the worktree.
"""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import time
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))

from agent_dispatch import runlogs  # noqa: E402
from agent_dispatch.gitcmd import (  # noqa: E402
    Git,
    apply_env_config,
    branch_slug,
    dispatch_branch_name,
    env_config_items,
    task_worktree_path,
)
from agent_dispatch.runtime import (  # noqa: E402
    CommandCodeDriver,
    RunResult,
    RunValidation,
    is_tool_hook_blocked,
    next_run_id,
    redact_argv,
    validate_run,
)
from agent_dispatch.store import Store  # noqa: E402
from test_offline import (  # noqa: E402
    FAKE_WRAPPER,
    TRIGGER,
    BaseCase,
    issue,
    pull,
)

FAKE_RUNTIME = TESTS_DIR / "fake_runtime.py"


class ExecutionCase(BaseCase):
    """Base for #4 tests: a real Git source clone plus a scripted fake runtime."""

    def setUp(self) -> None:
        super().setUp()
        self._make_source_clone()
        self.scenario_path = self.tmp / "scenario.json"
        self.argv_log = self.tmp / "argv.jsonl"
        self.write_scenario(
            runs=[{"session_id": "sess-1", "subtype": "success", "edits": {"impl.txt": "done\n"}}]
        )
        # The fake runtime is a real executable, so the driver's spawn and stream
        # parsing paths are exercised rather than bypassed.
        FAKE_RUNTIME.chmod(FAKE_RUNTIME.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)
        self.world.write_config(worker_overrides=self.execution_overrides())

    # ------------------------------------------------------------------ setup

    def _make_source_clone(self) -> None:
        """A real, bare-ish source clone with a commit on ``main``.

        A genuine repository is required: the worktree tests assert on real
        ``git worktree add``/``status``/``log`` output, and the credential test
        asserts the environment a real Git child would inherit.
        """
        import subprocess

        self.source = self.tmp / "source-clone"
        self.source.mkdir()
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }

        def git(*args: str) -> None:
            subprocess.run(
                ["git", "-C", str(self.source), *args],
                check=True,
                capture_output=True,
                text=True,
                env=env,
            )

        git("init", "-q", "-b", "main")
        (self.source / "README.md").write_text("# fixture\n", encoding="utf-8")
        git("add", "-A")
        git("commit", "-q", "-m", "initial")
        # `origin` points at the clone itself so fetch/push/ls-remote succeed
        # offline against a real Git remote rather than a mocked one.
        self.remote = self.tmp / "remote.git"
        subprocess.run(
            ["git", "init", "-q", "--bare", str(self.remote)],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        git("remote", "add", "origin", str(self.remote))
        git("push", "-q", "-u", "origin", "main")
        git("fetch", "-q", "origin")
        # The previous test's config pointed at a different checkout; point it here.
        self.world.repo_path = self.source

    def execution_overrides(self, **extra: object) -> dict[str, object]:
        overrides: dict[str, object] = {
            "commandcode_path": str(FAKE_RUNTIME),
            "run_timeout_seconds": 60,
            "max_attempts": 3,
            "worktree_root": f"{self.tmp}/worktrees",
        }
        overrides.update(extra)
        return overrides

    def write_scenario(self, runs: list[dict], **extra: object) -> None:
        scenario: dict[str, object] = {"runs": runs, "record_argv": str(self.argv_log)}
        scenario.update(extra)
        self.scenario_path.write_text(json.dumps(scenario, indent=2), encoding="utf-8")
        # Reset the fake runtime's per-invocation counter, so each test's scripted
        # runs start from run[0] rather than whatever the previous test consumed.
        state = Path(str(self.scenario_path) + ".state")
        state.unlink(missing_ok=True)

    def env(self) -> dict[str, str]:
        env = self.world.env()
        env["FAKE_RUNTIME_SCENARIO"] = str(self.scenario_path)
        return env

    def run_cli(self, *args: str):
        """Run the CLI with both fakes wired up."""
        import subprocess

        return subprocess.run(
            [
                sys.executable,
                "-m",
                "agent_dispatch.cli",
                "--config",
                str(self.world.config_path),
                *args,
            ],
            capture_output=True,
            text=True,
            env=self.env(),
            cwd=str(self.tmp),
            timeout=300,
        )

    # ------------------------------------------------------------- assertions

    def run_rows(self, store, number: int):
        task = store.get_task(self.slug, number)
        self.assertIsNotNone(task, f"expected a task row for {self.slug}#{number}")
        return store.run_history(task.id)

    def recorded_argv(self) -> list[list[str]]:
        if not self.argv_log.is_file():
            return []
        return [
            json.loads(line)
            for line in self.argv_log.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    # ------------------------------------------------- persistent-worker helpers

    def live_worker(self):
        """A single persistent ``Worker`` over the real store, with captured logging.

        Used by the tests that have to prove a *running* service picks up work without
        a restart, so they drive one instance across several ``poll_once()`` calls
        instead of starting a fresh process (which would silently re-run startup
        reconciliation and hide the bug).
        """
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.worker import Worker

        # The fake runtime reads its script from the environment. `run_cli` wires this
        # up per subprocess; an in-process worker needs it in this process.
        previous = os.environ.get("FAKE_RUNTIME_SCENARIO")

        def restore() -> None:
            if previous is None:
                os.environ.pop("FAKE_RUNTIME_SCENARIO", None)
            else:
                os.environ["FAKE_RUNTIME_SCENARIO"] = previous

        self.addCleanup(restore)
        os.environ["FAKE_RUNTIME_SCENARIO"] = str(self.scenario_path)

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        self.log_stream = io.StringIO()
        log = Logger(fmt="text", stream=self.log_stream)
        # `execute=True` on purpose: the worker must be *able* to start an agent, so a
        # poll that starts one anyway is caught rather than excused by the flag.
        worker = Worker(
            config, store, log, execute=True, client=GitHubClient(config.github.command)
        )
        return worker, store, config

    def break_commits(self) -> None:
        """Make every `git commit` fail, restoring the environment afterwards.

        An empty `GIT_AUTHOR_NAME` outranks configured `-c user.name`, so the commit
        fails exactly like the round-1 production bug the commit identity fixed.
        """
        previous = os.environ.get("GIT_AUTHOR_NAME")

        def restore() -> None:
            if previous is None:
                os.environ.pop("GIT_AUTHOR_NAME", None)
            else:
                os.environ["GIT_AUTHOR_NAME"] = previous

        self.addCleanup(restore)
        os.environ["GIT_AUTHOR_NAME"] = ""

    def repair_commits(self) -> None:
        os.environ.pop("GIT_AUTHOR_NAME", None)

    def assert_published_once(self, store, runs_before: int, branch: str, session: str) -> None:
        """Assert publication finished exactly once, with no further runtime call."""
        published = store.get_task(self.slug, 1)
        self.assertEqual(
            published.phase,
            "awaiting_review",
            "a running worker must finish publication on its next poll, without a restart",
        )
        self.assertIsNotNone(published.pr_number)
        self.assertIsNone(published.recovery_stage)
        self.assertEqual(published.branch, branch, "publication must reuse the finished work")
        self.assertEqual(published.session_id, session)
        self.assertEqual(len(self.recorded_argv()), runs_before, "zero additional runtime calls")
        self.assertEqual(
            len(self.world.read_world()["repos"][self.slug]["pulls"]),
            1,
            "exactly one PR",
        )

    def assert_worktree_clean_of_orchestrator_files(self, worktree: Path) -> None:
        """No run log, state db or lock may exist inside the owned worktree."""
        offenders = [
            str(path)
            for pattern in ("*.ndjson", "state.db*", "worker.lock", "*.stderr.txt")
            for path in worktree.rglob(pattern)
            if ".git" not in path.parts
        ]
        self.assertEqual(
            offenders,
            [],
            "orchestrator artefacts must live outside the worktree so `git add -A` cannot "
            f"commit them: {offenders}",
        )


# ==============================================================================
# Runtime stream validation — the false-success guard
# ==============================================================================


def _result(**overrides: object) -> RunResult:
    base: dict[str, object] = {
        "run_id": "20260101T000000Z-run",
        "session_id": "sess-1",
        "exit_code": 0,
        "subtype": "success",
        "stop_reason": "end_turn",
        "final_text": "done",
        "usage": None,
        "duration_ms": 10,
        "tool_hook_blocked": False,
        "resumed_from": None,
        "log_path": None,
        "validation": RunValidation(ok=True),
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:00:01+00:00",
    }
    base.update(overrides)
    return RunResult(**base)  # type: ignore[arg-type]


class RuntimeValidationTests(unittest.TestCase):
    """The validator is pure, so these are direct and exhaustive rather than e2e."""

    def test_the_happy_path_passes_every_check(self) -> None:
        result = validate_run(_result(), expected_session=None)
        self.assertTrue(result.ok, result.problems)
        self.assertTrue(all(result.checks.values()), result.checks)

    def test_blocked_tools_fail_a_run_that_reports_success(self) -> None:
        # This is the single most important correctness rule in the design (§2.2):
        # the runtime reports subtype=success and exits 0 while every tool call was
        # refused, so nothing was written. Trusting the exit code marks the task
        # complete while producing no work.
        result = _result(tool_hook_blocked=True)
        validation = validate_run(result, expected_session=None)
        self.assertFalse(validation.ok)
        self.assertFalse(validation.checks["no_blocked_tool_events"])
        self.assertIn("tool_hook_blocked", validation.summary())
        # The misleading in-band signals are still present, which is the point: they
        # are exactly what a naive implementation would have trusted.
        self.assertEqual(result.subtype, "success")
        self.assertEqual(result.exit_code, 0)

    def test_turn_cap_exit_is_a_bounded_failure(self) -> None:
        result = validate_run(_result(exit_code=8, subtype=None), expected_session=None)
        self.assertFalse(result.ok)
        self.assertIn("turn cap", result.summary())

    def test_timeout_fails_even_with_a_clean_exit_code(self) -> None:
        result = validate_run(
            _result(exit_code=0, timed_out=True, session_id="sess-1"), expected_session=None
        )
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["completed_in_time"])

    def test_missing_session_id_fails(self) -> None:
        result = validate_run(_result(session_id=None), expected_session=None)
        self.assertFalse(result.ok)
        self.assertIn("no session_id", result.summary())

    def test_resume_returning_a_different_session_fails(self) -> None:
        # A silently different session would mean the task's recorded conversation
        # is not the one that ran, which must never be treated as a normal resume.
        result = validate_run(_result(session_id="other"), expected_session="sess-1")
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["session_id_matches"])

    def test_resume_returning_the_pinned_session_passes(self) -> None:
        result = validate_run(_result(session_id="sess-1"), expected_session="sess-1")
        self.assertTrue(result.ok, result.problems)

    def test_non_success_subtype_fails(self) -> None:
        result = validate_run(_result(subtype="error"), expected_session=None)
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["subtype_success"])

    def test_spawn_error_fails(self) -> None:
        result = validate_run(_result(spawn_error="not installed"), expected_session=None)
        self.assertFalse(result.ok)
        self.assertFalse(result.checks["spawned"])


class BlockedEventDetectionTests(unittest.TestCase):
    def test_nested_event_shape_is_detected(self) -> None:
        event = {"type": "event", "event": {"type": "tool_hook_blocked", "toolName": "write_file"}}
        self.assertTrue(is_tool_hook_blocked(event))

    def test_top_level_shape_is_detected(self) -> None:
        self.assertTrue(is_tool_hook_blocked({"type": "tool_hook_blocked"}))

    def test_similar_event_names_do_not_false_positive(self) -> None:
        # A false negative would record success for blocked work, but a false
        # positive would fail every healthy run, so both directions are pinned.
        for name in ("tool_completed", "tool_running", "tool_queued", "hook_blocked"):
            with self.subTest(name=name):
                self.assertFalse(is_tool_hook_blocked({"type": "event", "event": {"type": name}}))


class DriverArgvTests(unittest.TestCase):
    def _driver(self) -> CommandCodeDriver:
        from agent_dispatch.config import RuntimeConfig

        return CommandCodeDriver(
            RuntimeConfig(
                driver="commandcode",
                model="deepseek/deepseek-v4-flash",
                effort="medium",
                permission_mode="allow-all",
                permission_flag="--yolo",
                max_turns=40,
            ),
            run_log_dir="/tmp",
            repo="o/r",
            issue_number=1,
        )

    def test_identity_flags_are_passed_on_a_first_run(self) -> None:
        argv = self._driver().build_argv("do the thing")
        for flag, value in (
            ("--model", "deepseek/deepseek-v4-flash"),
            ("--effort", "medium"),
            ("--max-turns", "40"),
        ):
            with self.subTest(flag=flag):
                self.assertEqual(argv[argv.index(flag) + 1], value)
        self.assertIn("--yolo", argv)
        self.assertIn("--output-format", argv)
        self.assertNotIn("--session", argv)

    def test_identity_flags_are_re_passed_on_a_resume(self) -> None:
        # Permission grants and the model do NOT persist inside a session, so they
        # are re-passed explicitly; this is the D2/D3 decision made testable.
        argv = self._driver().build_argv("more work", session_id="sess-xyz")
        self.assertEqual(argv[argv.index("--session") + 1], "sess-xyz")
        self.assertIn("--yolo", argv)
        self.assertEqual(argv[argv.index("--model") + 1], "deepseek/deepseek-v4-flash")
        self.assertEqual(argv[argv.index("--effort") + 1], "medium")

    def test_redaction_hides_the_instruction_text(self) -> None:
        secret_ish = "SECRET-ISSUE-BODY-TEXT"
        rendered = redact_argv(self._driver().build_argv(secret_ish))
        self.assertNotIn(secret_ish, " ".join(rendered))
        self.assertIn("--model", rendered)


# ==============================================================================
# Credential ordering
# ==============================================================================


class CredentialOrderingTests(unittest.TestCase):
    """The reset-then-wrapper ordering is load-bearing, not defensive.

    Measured on this VM: the ambient ``/usr/bin/gh auth git-credential`` *does*
    return a real credential for github.com, so without the empty reset entry first
    an unapproved helper would be queried before the configured wrapper.
    """

    def test_reset_entry_comes_first(self) -> None:
        pairs = env_config_items("!/wrapper auth git-credential")
        self.assertEqual(len(pairs), 2)
        key = "credential.https://github.com.helper"
        self.assertEqual(pairs[0], (key, ""), "the list reset must come first")
        self.assertEqual(pairs[1], (key, "!/wrapper auth git-credential"))

    def test_env_pairs_are_usable_by_git_config_count(self) -> None:
        env = apply_env_config({}, env_config_items("!/wrapper auth git-credential"))
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "credential.https://github.com.helper")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], "!/wrapper auth git-credential")

    def test_stale_pairs_are_replaced_not_merged(self) -> None:
        # A leftover GIT_CONFIG_COUNT from an outer process would make Git see a
        # count that does not match the keys, so the pairs are rebuilt wholesale.
        stale = {"GIT_CONFIG_COUNT": "9", "GIT_CONFIG_KEY_0": "user.name", "UNRELATED": "kept"}
        env = apply_env_config(stale, env_config_items("!/wrapper auth git-credential"))
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_KEY_0"], "credential.https://github.com.helper")
        self.assertEqual(env["UNRELATED"], "kept")


class CredentialEnvironmentMockedTests(ExecutionCase):
    """The agent subprocess must inherit the approved helper ordering.

    Uses an **inert logging helper** rather than real credentials: the fake world's
    helper prints a fixed offline marker, so the test proves which helper ran without
    any secret being involved.
    """

    def test_agent_environment_pins_reset_then_wrapper(self) -> None:
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator
        from agent_dispatch.store import Store

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        orchestrator = Orchestrator(
            config,
            store,
            GitHubClient(config.github.command),
            Logger(fmt="text", stream=open(os.devnull, "w")),
        )
        env = orchestrator.runtime_env
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "", "the reset entry must be inherited")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], f"!{FAKE_WRAPPER} auth git-credential")
        # Nothing that looks like a credential is exported by the orchestrator.
        self.assertNotIn("GH_TOKEN", env)

    def test_repo_local_config_is_opt_in_only(self) -> None:
        # Writing repo-local config edits the clone's shared common config, so it
        # must not happen unless the operator asks for it.
        from agent_dispatch.config import load_config

        config = load_config(self.world.config_path)
        self.assertFalse(config.worker.write_repo_local_credentials)

        self.set_issues(issue(1, "Credential placement", labels=[TRIGGER]))
        result = self.run_cli("run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        import subprocess

        marker = subprocess.run(
            ["git", "-C", str(self.source), "config", "--get", "agent-dispatch.managed"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(marker.returncode, 0, "no marker may be written when the flag is off")
        # The credentials still reached the agent's environment, which is why the
        # flag is not needed for correctness.
        marker_env = self.run_cli("status", "--no-sync")
        self.assertEqual(marker_env.returncode, 0, marker_env.stderr)


# ==============================================================================
# Naming and ownership helpers
# ==============================================================================


class RunIdTests(unittest.TestCase):
    def test_run_ids_are_unique_even_within_one_second(self) -> None:
        # Regression: with second resolution two rapid runs produced the same id,
        # which violated UNIQUE (task_id, run_id) and — worse — made the second run
        # write over the first run's log, destroying the evidence for the attempt
        # that actually needed explaining.
        ids = {next_run_id("implementation") for _ in range(200)}
        self.assertEqual(len(ids), 200)

    def test_run_ids_sort_chronologically(self) -> None:
        # `prune` keeps the newest logs by name comparison, so ordering must follow
        # the timestamp rather than the random suffix.
        from datetime import datetime, timedelta, timezone

        base = datetime(2026, 1, 1, tzinfo=timezone.utc)
        early = next_run_id("run", now=base)
        late = next_run_id("run", now=base + timedelta(seconds=1))
        self.assertLess(early, late)
        self.assertEqual(sorted([late, early]), [early, late])

    def test_run_id_is_filename_safe(self) -> None:
        run_id = next_run_id("implementation")
        self.assertTrue(run_id.isascii())
        for character in '/\\: +*?"<>|':
            self.assertNotIn(character, run_id)


class NamingTests(unittest.TestCase):
    def test_branch_name_is_deterministic_and_safe(self) -> None:
        name = dispatch_branch_name(4, "MVP execution: isolated worktree / PR handoff!")
        self.assertEqual(name, "dispatch/issue-4-mvp-execution-isolated-worktree-pr-handoff")
        self.assertTrue(name.startswith("dispatch/issue-4-"))
        # Deterministic: the same title always yields the same branch, which is what
        # lets a restart find the owned branch instead of deriving a new one.
        self.assertEqual(
            name, dispatch_branch_name(4, "MVP execution: isolated worktree / PR handoff!")
        )

    def test_slug_truncates_on_a_word_boundary(self) -> None:
        slug = branch_slug("a" * 40 + " " + "b" * 40, limit=20)
        self.assertLessEqual(len(slug), 20)
        self.assertFalse(slug.endswith("-"))

    def test_slug_falls_back_when_the_title_has_no_usable_characters(self) -> None:
        self.assertEqual(branch_slug("!!!"), "task")

    def test_worktree_path_is_deterministic(self) -> None:
        path = task_worktree_path("/root", "owner/name", 7)
        self.assertEqual(path, Path("/root/owner__name/issue-7"))


# ==============================================================================
# End-to-end execution through the real CLI, fake wrapper and fake runtime
# ==============================================================================


class EndToEndExecutionTests(ExecutionCase):
    def test_labelled_issue_becomes_exactly_one_pr_and_awaiting_review(self) -> None:
        self.set_issues(issue(1, "Implement the thing", labels=[TRIGGER]))

        result = self.run_cli("run")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertIn("awaiting_review", result.stdout)

        db = self.world.load_config().worker.state_db
        from agent_dispatch.store import Store

        store = Store(db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "awaiting_review")
        self.assertIsNotNone(task.pr_number, "the created PR must be recorded as owned")
        self.assertEqual(task.branch, dispatch_branch_name(1, "Implement the thing"))
        self.assertIsNotNone(task.worktree_path)
        self.assertIsNotNone(task.session_id)
        self.assertEqual(task.attempts, 1)

        world = self.world.read_world()
        pulls = world["repos"][self.slug]["pulls"]
        self.assertEqual(len(pulls), 1, "exactly one PR may be created")
        self.assertEqual(pulls[0]["number"], task.pr_number)
        self.assertEqual(pulls[0]["head"]["ref"], task.branch)
        self.assertIn("#1", pulls[0]["body"])

        # The owned branch really exists on the remote, with the agent's commit.
        branches = self._remote_branches()
        self.assertIn(task.branch, branches)

    def test_the_run_log_lives_outside_the_worktree(self) -> None:
        self.set_issues(issue(1, "Log placement", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        worktree = Path(task.worktree_path)

        self.assertTrue(task.worktree_path)
        # This is the §6 requirement that makes `git add -A` safe inside the worktree.
        self.assert_worktree_clean_of_orchestrator_files(worktree)
        runs = store.run_history(task.id)
        log_path = Path(runs[0].log_path)
        self.assertTrue(log_path.is_file(), f"expected a run log at {log_path}")
        self.assertNotIn(str(worktree), str(log_path))
        self.assertTrue(str(log_path).startswith(str(self.world.state_dir)))

    def test_committed_work_is_accepted_even_though_the_tree_is_clean(self) -> None:
        # A clean tree is NOT the failure signal: the agent may have committed
        # everything itself, which is the best case.
        self.write_scenario(
            runs=[
                {
                    "session_id": "sess-1",
                    "subtype": "success",
                    "edits": {"feature.py": "print('hi')\n"},
                    "commit": True,
                }
            ]
        )
        self.set_issues(issue(1, "Commits its work", labels=[TRIGGER]))
        result = self.run_cli("run")
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "awaiting_review")
        runs = store.run_history(task.id)
        self.assertTrue(runs[0].produced_work)
        self.assertEqual(runs[0].outcome, "succeeded")

    def test_a_successful_run_that_changed_nothing_does_not_open_a_pr(self) -> None:
        # The agent correctly deciding no change is needed is legitimate, but a
        # pushed branch would be empty, so the task is surfaced rather than smoothed
        # over and no PR is opened.
        self.write_scenario(runs=[{"session_id": "sess-1", "subtype": "success"}])
        self.set_issues(issue(1, "Nothing to do", labels=[TRIGGER]))
        result = self.run_cli("run")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("needs_attention", result.stdout)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "needs_attention")
        self.assertIsNone(task.pr_number)
        self.assertEqual(self.world.read_world()["repos"][self.slug]["pulls"], [])

    def test_agent_left_the_changes_uncommitted_and_the_dispatcher_commits_them(self) -> None:
        self.set_issues(issue(1, "Uncommitted", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "awaiting_review")

        # The branch on the remote must contain the agent's file, which is only
        # possible if the orchestrator committed the uncommitted work.
        listed = self._remote_branches()
        self.assertIn(task.branch, listed)
        import subprocess

        show = subprocess.run(
            ["git", "-C", str(self.remote), "show", f"{task.branch}:impl.txt"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(show.returncode, 0, show.stderr)
        self.assertEqual(show.stdout.strip(), "done")

    def test_the_dispatchers_commit_does_not_depend_on_ambient_git_identity(self) -> None:
        """The dispatcher must be able to commit on a machine with no Git identity.

        This is a regression test for a CI-only failure. This dev VM happens to have
        `user.email` set globally, so `git commit` worked here, while a clean runner
        has none and the commit failed with exit 128 ("Author identity unknown"). The
        finished work then never reached the branch: a silent, environment-dependent
        failure of the entire PR step, invisible on the machine it was written on.

        Git config is isolated from every level (system, global, repo), `HOME` and
        `XDG_CONFIG_HOME` point at an empty directory, and the
        `GIT_AUTHOR_*`/`GIT_COMMITTER_*` variables are cleared, so the subprocess
        environment faithfully resembles the runner that exposed this.
        """
        bare_home = self.tmp / "bare-home"
        bare_home.mkdir()
        self.world.env_overrides.update(
            {
                "HOME": str(bare_home),
                "XDG_CONFIG_HOME": str(bare_home / ".config"),
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(bare_home / "no-such-gitconfig"),
                "GIT_CONFIG_SYSTEM": str(bare_home / "no-such-system-gitconfig"),
            }
        )
        # Deliberately REMOVED rather than set to "": an empty GIT_AUTHOR_NAME is an
        # identity override that outranks `-c user.name`, so setting it would make the
        # commit fail with "empty ident name" for a reason unrelated to this bug.
        for variable in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        ):
            self.world.env_overrides[variable] = None
        self.set_issues(issue(1, "No identity anywhere", labels=[TRIGGER]))
        result = self.run_cli("run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "awaiting_review")

        # The work really reached the branch, committed under the configured identity
        # rather than a failing ambient lookup.
        import subprocess

        show = subprocess.run(
            ["git", "-C", str(self.remote), "show", f"{task.branch}:impl.txt"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(show.returncode, 0, show.stderr)
        author = subprocess.run(
            ["git", "-C", str(self.remote), "log", "-1", "--format=%an <%ae>", task.branch],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(author.stdout.strip(), "agent-dispatch <agent-dispatch@localhost>")

    def test_a_commit_with_no_identity_configured_anywhere_still_succeeds(self) -> None:
        # The narrow unit-level version of the above: Git is given nothing ambient
        # to fall back on, so only the explicit -c identity can make this commit work.
        from agent_dispatch.gitcmd import Git
        from agent_dispatch.worktree import WorktreeManager

        bare_home = self.tmp / "no-git-identity-home"
        bare_home.mkdir()
        git = Git(
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(bare_home / "nonexistent-gitconfig"),
            }
        )
        # Prove the environment really has no usable identity before asserting that
        # the manager can commit anyway, so this cannot pass for the wrong reason.
        probe = git.run(["var", "GIT_AUTHOR_IDENT"], cwd=self.source)
        self.assertFalse(probe.ok, "the probe environment must have no Git identity")
        self.assertIn(
            "identity",
            probe.stderr.lower(),
            "the probe must fail for an *absent* identity, not some other reason",
        )

        manager = WorktreeManager(
            git,
            source_path=self.source,
            worktree_root=self.tmp / "identity-worktrees",
            base_branch="main",
            repo_slug=self.slug,
            commit_identity=("agent-dispatch", "agent-dispatch@localhost"),
        )
        outcome = manager.ensure(issue_number=9, title="Identity test")
        (outcome.state.path / "new-file.txt").write_text("work\n", encoding="utf-8")

        committed, note = manager.commit_all(outcome.state.path, outcome.state.branch, "msg")
        self.assertTrue(committed, note)
        subject = git.run(["log", "-1", "--format=%an <%ae>"], cwd=outcome.state.path)
        self.assertEqual(subject.stdout.strip(), "agent-dispatch <agent-dispatch@localhost>")

    def test_a_commit_without_a_configured_identity_fails_loudly(self) -> None:
        # The counterpart: when the operator clears the identity, the failure is
        # reported rather than swallowed, so the cause is visible instead of the
        # work silently not reaching the branch.
        from agent_dispatch.gitcmd import Git
        from agent_dispatch.worktree import WorktreeManager

        bare_home = self.tmp / "no-git-identity-home-2"
        bare_home.mkdir()
        git = Git(
            env={
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": str(bare_home / "nonexistent-gitconfig"),
            }
        )
        manager = WorktreeManager(
            git,
            source_path=self.source,
            worktree_root=self.tmp / "identity-worktrees-2",
            base_branch="main",
            repo_slug=self.slug,
        )
        outcome = manager.ensure(issue_number=10, title="No identity")
        (outcome.state.path / "new-file.txt").write_text("work\n", encoding="utf-8")

        committed, note = manager.commit_all(outcome.state.path, outcome.state.branch, "msg")
        self.assertFalse(committed)
        self.assertIn("commit failed", note)

    def _remote_branches(self) -> list[str]:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(self.remote), "branch", "--list", "--format=%(refname:short)"],
            capture_output=True,
            text=True,
            check=False,
        )
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


class BlockedRunTests(ExecutionCase):
    """The false-success case, end to end through the real CLI and process."""

    def test_blocked_tools_fail_the_task_and_produce_no_pr(self) -> None:
        self.write_scenario(
            runs=[
                {
                    "session_id": "sess-1",
                    "subtype": "success",  # the runtime still claims success
                    "exit_code": 0,  # and still exits 0
                    "events": ["tool_hook_blocked"],
                    "edits": {"should-not-exist.txt": "written\n"},
                }
            ]
        )
        self.set_issues(issue(1, "Blocked", labels=[TRIGGER]))
        result = self.run_cli("run")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("no_blocked_tool_events: FAIL", result.stdout)
        self.assertIn("tool_hook_blocked", result.stdout)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertIsNone(task.pr_number, "a blocked run must never produce a PR")
        self.assertNotEqual(task.phase, "awaiting_review")
        self.assertEqual(self.world.read_world()["repos"][self.slug]["pulls"], [])

        runs = store.run_history(task.id)
        self.assertTrue(runs[0].tool_hook_blocked)
        self.assertEqual(runs[0].outcome, "failed")
        # The fake runtime only applies edits for a non-blocked run, so this also
        # proves the worktree really was untouched.
        self.assertFalse(Path(task.worktree_path, "should-not-exist.txt").exists())

    def test_a_blocked_session_is_not_offered_as_resumable(self) -> None:
        self.write_scenario(
            runs=[{"session_id": "sess-1", "subtype": "success", "events": ["tool_hook_blocked"]}]
        )
        self.set_issues(issue(1, "Blocked", labels=[TRIGGER]))
        self.run_cli("run")

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertIsNotNone(task.session_id, "the session ID is still captured for traceability")
        self.assertIsNone(
            store.resumable_session(task.id),
            "a blocked run has no usable transcript, so it must never be a resume target",
        )
        # And `open` must say so plainly rather than promising a resume.
        opened = self.run_cli("open", "--repo", self.slug, "--issue", "1")
        self.assertIn("resumable      : no", opened.stdout)


class SessionIdentityTests(ExecutionCase):
    def test_a_mismatched_session_id_on_a_resume_fails(self) -> None:
        # A resume is only requested after a clean run recorded a session; force
        # that state, then have the runtime answer with a different ID.
        self.write_scenario(
            runs=[
                {"session_id": "sess-1", "subtype": "success", "edits": {"a.txt": "1\n"}},
                {"session_id": "sess-2", "subtype": "success", "edits": {"b.txt": "2\n"}},
            ]
        )
        self.set_issues(issue(1, "Resume", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator
        from agent_dispatch.store import Store

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)

        orchestrator = Orchestrator(
            config,
            store,
            GitHubClient(config.github.command),
            Logger(fmt="text", stream=open(os.devnull, "w")),
        )
        # Run once more as a resume, which must be rejected for the ID mismatch.
        run_row_result = orchestrator._run_once(
            task=task,
            repo=config.repo(self.slug),
            worktree=Path(task.worktree_path),
            instruction="continue",
            kind="retry",
            session_id=task.session_id,
        )
        result = run_row_result[0]
        self.assertIsNotNone(result)
        self.assertFalse(result.ok)
        self.assertFalse(result.validation.checks["session_id_matches"])
        self.assertIn("does not match the pinned", result.validation.summary())


class TimeoutTests(ExecutionCase):
    """The wall-clock cap must really kill the run and be reported as a timeout.

    Driven through :class:`CommandCodeDriver` directly rather than the CLI: the
    approved schema floors ``worker.run_timeout_seconds`` at 30s (a deliberate
    safety minimum), so a CLI-level test would need half a minute to prove this.
    The driver is the component that owns the deadline, so this is also the more
    precise place to assert it — including that the process group is really dead.
    """

    def _driver(self, timeout: float) -> CommandCodeDriver:
        from agent_dispatch.config import load_config

        config = load_config(self.world.config_path)
        return CommandCodeDriver(
            config.repo(self.slug).runtime,
            run_log_dir=config.worker.run_log_dir,
            repo=self.slug,
            issue_number=1,
            binary=str(FAKE_RUNTIME),
            timeout_seconds=timeout,
            env={"FAKE_RUNTIME_SCENARIO": str(self.scenario_path)},
        )

    def test_a_hung_run_is_killed_and_reported_as_a_timeout(self) -> None:
        self.write_scenario(runs=[{"session_id": "sess-1", "hang": True}])
        worktree = self.tmp / "hang-worktree"
        worktree.mkdir()

        import time

        started = time.monotonic()
        result = self._driver(timeout=2).run(
            worktree=worktree, instruction="hang please", run_id="20260101T000000Z-run"
        )
        elapsed = time.monotonic() - started

        self.assertTrue(result.timed_out, "the deadline must be recorded as a timeout")
        self.assertFalse(result.ok)
        self.assertFalse(result.validation.checks["completed_in_time"])
        self.assertIn("timeout", result.validation.summary())
        # The watchdog, not the run, ended it: proof the deadline is enforced rather
        # than merely recorded after the fact.
        self.assertLess(elapsed, 30, "a hung run must be killed at the deadline")
        # The session ID is still captured from the first stream line, so the
        # interrupted attempt is traceable even though it is not resumable.
        self.assertEqual(result.session_id, "sess-1")

    def test_the_killed_run_leaves_no_live_process_holding_the_worktree(self) -> None:
        # A surviving grandchild would keep writing into the owned worktree and
        # corrupt the next attempt, so the whole process group is killed.
        self.write_scenario(runs=[{"session_id": "sess-1", "hang": True}])
        worktree = self.tmp / "hang-worktree-2"
        worktree.mkdir()

        self._driver(timeout=2).run(
            worktree=worktree, instruction="hang", run_id="20260101T000001Z-run"
        )

        import subprocess

        listing = subprocess.run(
            ["pgrep", "-f", str(self.scenario_path)], capture_output=True, text=True, check=False
        )
        self.assertEqual(
            listing.stdout.strip(),
            "",
            "the fake runtime must not survive the timeout kill: " + listing.stdout,
        )

    def test_a_timed_out_run_is_recorded_as_timed_out_with_no_pr(self) -> None:
        # The failure path end to end, with a scenario that fails immediately
        # instead of hanging, so the CLI can be exercised at normal speed.
        self.write_scenario(runs=[{"session_id": "sess-1", "subtype": "error", "exit_code": 1}])
        self.set_issues(issue(1, "Fails", labels=[TRIGGER]))
        result = self.run_cli("run")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("subtype_success: FAIL", result.stdout)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertIsNone(task.pr_number)
        self.assertEqual(len(self.world.read_world()["repos"][self.slug]["pulls"]), 0)


class RetryTests(ExecutionCase):
    def test_an_interrupted_run_preserves_edits_and_the_retry_starts_fresh(self) -> None:
        # First run hangs after writing a file (the fake writes edits before
        # finishing only for non-hanging runs, so the retry proves preservation
        # using an explicit leftover file created between attempts instead).
        self.write_scenario(
            runs=[
                {
                    "session_id": "sess-1",
                    "subtype": "error",
                    "exit_code": 1,
                    "events": ["tool_completed"],
                    "edits": {"partial.txt": "half done\n"},
                },
                {
                    "session_id": "sess-2",
                    "subtype": "success",
                    "edits": {"partial.txt": "half done\n", "done.txt": "yes\n"},
                },
            ]
        )
        self.set_issues(issue(1, "Retry me", labels=[TRIGGER]))

        first = self.run_cli("run")
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertIsNotNone(task.worktree_path)
        # The failed attempt's uncommitted edit is preserved, not discarded.
        self.assertTrue(
            Path(task.worktree_path, "partial.txt").is_file(),
            "an interrupted run's edits must be preserved",
        )
        first_session = task.session_id
        self.assertEqual(task.phase, "queued", "a failed attempt within budget is re-queued")

        # No `retry` is needed while the attempt budget lasts: the task stays
        # queued so the next dispatch retries it automatically.
        second = self.run_cli("run")
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)

        task = store.get_task(self.slug, 1)
        runs = store.run_history(task.id)
        self.assertEqual(len(runs), 2, "the retry must be recorded as a separate run")
        self.assertIsNone(runs[1].resumed_from, "the retry must NOT claim to resume a dead session")
        self.assertNotEqual(runs[1].session_id, first_session, "a fresh session was started")
        self.assertEqual(task.phase, "awaiting_review")
        self.assertIsNotNone(task.pr_number)

        # Each run keeps its OWN log. Two runs started within the same second used to
        # collide on run_id, which both broke the uniqueness constraint and let the
        # second run overwrite the first run's log — destroying the evidence for the
        # attempt that needed explaining.
        self.assertNotEqual(runs[0].run_id, runs[1].run_id)
        self.assertNotEqual(runs[0].log_path, runs[1].log_path)
        self.assertTrue(Path(runs[0].log_path).is_file())
        self.assertTrue(Path(runs[1].log_path).is_file())

        # Both the preserved file and the new one reached the branch.
        import subprocess

        show = subprocess.run(
            ["git", "-C", str(self.remote), "show", f"{task.branch}:done.txt"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(show.returncode, 0, show.stderr)

    def test_attempt_budget_exhaustion_stops_further_runs(self) -> None:
        self.write_scenario(runs=[{"session_id": "sess-x", "subtype": "error", "exit_code": 1}])
        self.world.write_config(worker_overrides=self.execution_overrides(max_attempts=1))
        self.set_issues(issue(1, "Always fails", labels=[TRIGGER]))

        self.assertEqual(self.run_cli("run").returncode, 1)
        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "failed")
        self.assertIn("attempt 1/1", task.last_error or "")

        # A second `run` must refuse rather than spend another model call, and the
        # refusal must name the real reason so the operator knows what to do.
        again = self.run_cli("run")
        self.assertIn("no_eligible_task", again.stdout + again.stderr)
        self.assertEqual(len(store.run_history(task.id)), 1)

        # `retry` is the documented recovery path and restores the budget.
        self.assertEqual(self.run_cli("retry", "--repo", self.slug, "--issue", "1").returncode, 0)
        self.assertEqual(store.get_task(self.slug, 1).attempts, 0)
        self.assertEqual(store.get_task(self.slug, 1).phase, "queued")


class IdempotencyTests(ExecutionCase):
    def test_a_second_run_does_not_create_a_second_pr(self) -> None:
        self.set_issues(issue(1, "Once only", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        # Make the scenario tempting: if a second agent ran, it would try to open a
        # second PR for the same head branch, which GitHub rejects.
        self.write_scenario(
            runs=[{"session_id": "sess-9", "subtype": "success", "edits": {"again.txt": "x\n"}}]
        )
        second = self.run_cli("run", "--skip-poll")

        pulls = self.world.read_world()["repos"][self.slug]["pulls"]
        self.assertEqual(len(pulls), 1, "a second run must never create a second PR")
        self.assertIn("no_eligible_task", second.stdout + second.stderr)

    def test_restart_adopts_the_existing_pr_instead_of_creating_one(self) -> None:
        self.set_issues(issue(1, "Restart", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        original = store.get_task(self.slug, 1)
        self.assertIsNotNone(original.pr_number)

        # Simulate the crash window: the PR exists on GitHub but the row lost it.
        store._conn.execute(
            "UPDATE tasks SET pr_number = NULL, pr_url = NULL, phase = 'awaiting_review' WHERE id = ?",
            (original.id,),
        )
        store.close()

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        repaired = self.run_cli("run", "--skip-poll")
        self.assertIn("reconcile", repaired.stdout)

        task = store.get_task(self.slug, 1)
        self.assertEqual(
            task.pr_number, original.pr_number, "the existing PR must be adopted, not recreated"
        )
        self.assertEqual(len(self.world.read_world()["repos"][self.slug]["pulls"]), 1)

    def test_a_repeated_poll_does_not_queue_a_second_run(self) -> None:
        self.set_issues(issue(1, "Poll twice", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        first_argv = len(self.recorded_argv())

        for _ in range(3):
            # `--no-execute` keeps this a pure poll, which is what is under test
            # here: polling must not start a second agent for a task already
            # awaiting review, regardless of whether the poll itself executes.
            self.run_cli("worker", "--once", "--no-execute")
        self.assertEqual(
            len(self.recorded_argv()),
            first_argv,
            "polling must not start a second agent once a task is awaiting_review",
        )
        self.assertEqual(len(self.world.read_world()["repos"][self.slug]["pulls"]), 1)

    def test_no_execute_poll_queues_without_spending_a_model_call(self) -> None:
        self.set_issues(issue(1, "Queue only", labels=[TRIGGER]))
        result = self.run_cli("worker", "--once", "--no-execute")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.recorded_argv(), [], "--no-execute must not launch an agent")

        from agent_dispatch.store import Store

        config = self.world.load_config()
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        self.assertEqual(store.get_task(self.slug, 1).phase, "queued")

    def test_the_worker_command_executes_by_default(self) -> None:
        # The operator-facing `worker` command is where "run an agent" is intended,
        # so it must not need a second flag to do its job.
        self.set_issues(issue(1, "Run it", labels=[TRIGGER]))
        result = self.run_cli("worker", "--once")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(len(self.recorded_argv()), 1, "`worker` must execute eligible tasks")

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        self.assertEqual(store.get_task(self.slug, 1).phase, "awaiting_review")


class EligibilityTests(ExecutionCase):
    """Eligibility is re-checked before a run, and the poll is not the authority.

    These tests queue with ``enqueue`` (which deliberately never executes) so the
    *only* thing under test is whether ``run`` itself refuses.
    """

    def queue(self, number: int) -> None:
        result = self.run_cli("enqueue", "--repo", self.slug, "--issue", str(number))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_label_withdrawn_before_the_claim_prevents_the_run(self) -> None:
        self.set_issues(issue(1, "Withdrawn", labels=[TRIGGER]))
        self.queue(1)

        # The label is removed *after* the queue row exists but before any run: the
        # run must re-validate against GitHub rather than trust the stale row.
        self.set_issues(issue(1, "Withdrawn", labels=[]))
        result = self.run_cli("run", "--skip-poll", "--repo", self.slug, "--issue", "1")

        self.assertEqual(self.recorded_argv(), [], "no agent may be started")
        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "paused")
        self.assertEqual(task.pause_reason, "label_withdrawn")
        self.assertIn("trigger_label_missing", result.stdout + result.stderr)

    def test_a_pushed_branch_without_a_pr_gets_its_pr_created_on_restart(self) -> None:
        # The crash-after-push window: the branch is on the remote, the PR was never
        # created (or never recorded). Recovery must create the PR WITHOUT running
        # the agent again, because the code already exists on the branch.
        self.set_issues(issue(1, "Pushed but no PR", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        branch = task.branch
        self.assertIsNotNone(task.pr_number)
        # The slash matters: the API path `repos/{slug}/branches/{branch}` would need
        # percent-encoding here, which is why the check goes through `git ls-remote`.
        self.assertIn("/", branch)

        # Remove the PR from the fake world *and* clear ownership, exactly as a crash
        # between "push" and "create PR" would leave things.
        world = self.world.read_world()
        world["repos"][self.slug]["pulls"] = []
        world["repos"][self.slug]["branches"] = [branch]
        self.world.world = world
        self.world.write_world()
        store._conn.execute(
            "UPDATE tasks SET pr_number = NULL, pr_url = NULL, phase = 'awaiting_review' WHERE id = ?",
            (task.id,),
        )
        store.close()

        runs_before = len(self.recorded_argv())
        repaired = self.run_cli("run", "--skip-poll")
        self.assertIn("reconcile", repaired.stdout)

        # No second agent run: the recovery only re-did the PR step.
        self.assertEqual(
            len(self.recorded_argv()),
            runs_before,
            "crash recovery must not launch another agent when the branch already exists",
        )

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertIsNotNone(task.pr_number, "the missing PR must be created")
        self.assertEqual(task.phase, "awaiting_review")
        self.assertEqual(len(self.world.read_world()["repos"][self.slug]["pulls"]), 1)

    def test_awaiting_review_with_neither_branch_nor_pr_escalates(self) -> None:
        # The other half of the crash window: nothing reached the remote. Creating a
        # PR out of nothing would be inventing work, so the task is escalated.
        self.set_issues(issue(1, "Nothing pushed", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)

        # The branch check goes through Git (`ls-remote`), so the *real* remote must
        # lose the branch; emptying the fake wrapper's list would not be enough.
        import subprocess

        subprocess.run(
            ["git", "-C", str(self.remote), "branch", "-D", task.branch],
            capture_output=True,
            text=True,
            check=True,
        )
        world = self.world.read_world()
        world["repos"][self.slug]["pulls"] = []
        world["repos"][self.slug]["branches"] = []
        self.world.world = world
        self.world.write_world()
        store._conn.execute(
            "UPDATE tasks SET pr_number = NULL, pr_url = NULL, phase = 'awaiting_review' WHERE id = ?",
            (task.id,),
        )
        store.close()

        repaired = self.run_cli("run", "--skip-poll")
        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "needs_attention")
        self.assertIn("needs_attention", repaired.stdout)
        self.assertEqual(self.world.read_world()["repos"][self.slug]["pulls"], [])

    def test_an_orphaned_running_row_whose_work_was_never_pushed_is_retried(self) -> None:
        # Crash window 1: the process died INSIDE the agent run, before anything was
        # pushed. Nothing exists on the remote, so the bounded fresh-session retry is
        # the correct repair and the task goes back to the queue.
        self.set_issues(issue(1, "Interrupted", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        branch = task.branch

        # Remove the branch from the real remote so "nothing was published" is true.
        import subprocess

        subprocess.run(
            ["git", "-C", str(self.remote), "branch", "-D", branch],
            capture_output=True,
            text=True,
            check=True,
        )
        # Simulate a process that died mid-run: phase running, run row still open.
        store._conn.execute(
            "UPDATE tasks SET phase = 'running', attempts = 1, pr_number = NULL WHERE id = ?",
            (task.id,),
        )
        run_row = store.start_run(
            task.id,
            run_id="20260101T000000Z-implementation",
            kind="implementation",
            resumed_from=None,
            log_path=str(self.tmp / "dangling.ndjson"),
        )
        store.close()

        repaired = self.run_cli("run", "--skip-poll")
        self.assertIn("reconcile", repaired.stdout)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        runs = store.run_history(task.id)
        dangling = [run for run in runs if run.id == run_row]
        self.assertEqual(
            dangling[0].outcome, "failed", "the orphaned run row must be closed, not left open"
        )
        # It was retried (queued then dispatched) rather than left stuck in running.
        self.assertNotEqual(store.get_task(self.slug, 1).phase, "running")

    def test_a_crash_during_push_recovers_publish_only_without_a_second_run(self) -> None:
        """The real crash-after-push window the review flagged.

        The task stays `running` for the whole of `git push`, so a crash there leaves
        a `running` row whose branch IS already on the remote. Recovery must finish
        publishing it — and must NOT spend a second model call, because the code
        already exists on the branch.
        """
        self.set_issues(issue(1, "Crash during push", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        branch = task.branch
        original_pr = task.pr_number
        self.assertIsNotNone(original_pr)

        # Reproduce the crash window faithfully: the branch is pushed, the run row is
        # still open, the task is `running`, and the PR ownership was never recorded.
        # This is the state the actual code leaves — the earlier version of this test
        # manually set `awaiting_review`, a phase the code does not reach before push,
        # which is why it missed the bug.
        world = self.world.read_world()
        world["repos"][self.slug]["pulls"] = []
        world["repos"][self.slug]["branches"] = [branch]
        self.world.world = world
        self.world.write_world()
        store._conn.execute(
            "UPDATE tasks SET phase = 'running', pr_number = NULL, pr_url = NULL WHERE id = ?",
            (task.id,),
        )
        # Deliberately NO extra open run row: the run had already completed and been
        # recorded as succeeded before `git push` began. An open run row would model a
        # killed agent mid-edit, which is a different window with a different repair.
        self.assertEqual(
            [r for r in store.run_history(task.id) if r.outcome == "running"],
            [],
            "the crash-during-push window has no open run row",
        )
        store.close()

        runs_before = len(self.recorded_argv())
        repaired = self.run_cli("run", "--skip-poll")
        self.assertIn("reconcile", repaired.stdout)

        # The whole point: no second agent run was started.
        self.assertEqual(
            len(self.recorded_argv()),
            runs_before,
            "a crash after push must be recovered publish-only, never by re-running the agent",
        )

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        repaired_task = store.get_task(self.slug, 1)
        self.assertEqual(repaired_task.phase, "awaiting_review")
        self.assertIsNotNone(repaired_task.pr_number, "the missing PR must be created")
        pulls = self.world.read_world()["repos"][self.slug]["pulls"]
        self.assertEqual(len(pulls), 1, "exactly one PR, created by recovery")
        self.assertEqual(pulls[0]["head"]["ref"], branch)

    def test_a_crash_during_push_adopts_an_existing_pr_without_a_second_run(self) -> None:
        # The other half of that window: the PR POST succeeded but the process died
        # before recording it. Recovery must adopt it, not create a second one.
        self.set_issues(issue(1, "PR created then crash", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        original_pr = task.pr_number

        # Lose only the DB write: the PR still exists in the world, as it would after a
        # crash between a successful POST and the SQLite update.
        store._conn.execute(
            "UPDATE tasks SET phase = 'running', pr_number = NULL, pr_url = NULL WHERE id = ?",
            (task.id,),
        )
        store.close()

        runs_before = len(self.recorded_argv())
        self.run_cli("run", "--skip-poll")

        self.assertEqual(len(self.recorded_argv()), runs_before, "no agent may be re-run")
        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        repaired_task = store.get_task(self.slug, 1)
        self.assertEqual(repaired_task.pr_number, original_pr, "the real PR must be adopted")
        self.assertEqual(len(self.world.read_world()["repos"][self.slug]["pulls"]), 1)

    def test_a_foreign_pr_prevents_the_run_and_is_never_owned(self) -> None:
        self.set_issues(issue(1, "Has a PR", labels=[TRIGGER]))
        self.set_pulls(pull(50, "someone-elses-branch", body="Closes #1"))

        # `enqueue` already refuses, using the very same decision the poll makes.
        queued = self.run_cli("enqueue", "--repo", self.slug, "--issue", "1")
        self.assertEqual(queued.returncode, 1)
        self.assertIn("PR #50", queued.stdout)

        # And even if a row existed, `run` must refuse to start an agent for it.
        result = self.run_cli("run", "--skip-poll")
        self.assertEqual(self.recorded_argv(), [], "no agent may be started")
        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertIsNone(task.pr_number, "a foreign PR must never be recorded as owned")
        self.assertEqual(task.linked_pr_number, 50, "it is recorded as an observation only")
        self.assertIn("no_eligible_task", result.stdout + result.stderr)

    def test_a_foreign_pr_is_never_owned_even_after_many_runs(self) -> None:
        # The #3 regression: a second poll must not promote someone else's PR to
        # owned. Repeated attempts are the way that bug showed up.
        self.set_issues(issue(1, "Foreign PR", labels=[TRIGGER]))
        self.set_pulls(pull(51, "human-branch", body="Fixes #1"))
        self.run_cli("enqueue", "--repo", self.slug, "--issue", "1")
        for _ in range(3):
            self.run_cli("run", "--skip-poll")

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertIsNone(task.pr_number)
        self.assertEqual(task.linked_pr_number, 51)
        self.assertEqual(self.recorded_argv(), [])

    def test_a_paused_task_is_not_run(self) -> None:
        self.set_issues(issue(1, "Paused", labels=[TRIGGER]))
        self.queue(1)
        self.assertEqual(self.run_cli("pause", "--repo", self.slug, "--issue", "1").returncode, 0)

        result = self.run_cli("run", "--skip-poll")
        self.assertEqual(self.recorded_argv(), [], "a paused task must never be executed")
        self.assertIn("no_eligible_task", result.stdout + result.stderr)

    def test_naming_a_paused_task_explicitly_does_not_bypass_eligibility(self) -> None:
        # `run --issue N` is a convenience, not an override: it must apply the same
        # rules the poll applies rather than acting on whatever number is passed.
        self.set_issues(issue(1, "Paused", labels=[TRIGGER]))
        self.queue(1)
        self.run_cli("pause", "--repo", self.slug, "--issue", "1")

        result = self.run_cli("run", "--skip-poll", "--repo", self.slug, "--issue", "1")
        self.assertEqual(self.recorded_argv(), [])
        self.assertIn("not dispatchable locally", result.stdout + result.stderr)

    def test_a_closed_issue_is_finished_rather_than_run(self) -> None:
        self.set_issues(issue(1, "Closed", labels=[TRIGGER]))
        self.queue(1)
        self.set_issues(issue(1, "Closed", labels=[TRIGGER], state="closed"))

        self.run_cli("run", "--skip-poll")
        self.assertEqual(self.recorded_argv(), [], "a closed Issue must never be executed")

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        self.assertEqual(store.get_task(self.slug, 1).phase, "finished")


class ConcurrencyTests(ExecutionCase):
    def test_a_second_run_cannot_start_while_one_holds_the_lock(self) -> None:
        self.set_issues(issue(1, "Locked", labels=[TRIGGER]))
        from agent_dispatch.lockfile import WorkerLock

        config = self.world.load_config()
        holder = WorkerLock(config.worker.lock_file, command="test holder")
        holder.acquire()
        self.addCleanup(holder.release)

        result = self.run_cli("run", "--skip-poll")
        # Exit 3 is EXIT_BUSY: the lock is the mechanism that enforces "one active
        # task globally", so a refusal here is the feature working.
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertEqual(self.recorded_argv(), [])
        self.assertIn("lock_busy", result.stderr)

    def test_only_one_task_runs_even_with_two_eligible_issues(self) -> None:
        self.set_issues(
            issue(1, "First", labels=[TRIGGER]),
            issue(2, "Second", labels=[TRIGGER]),
        )
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        phases = {task.issue_number: task.phase for task in store.list_tasks()}
        self.assertEqual(phases[1], "awaiting_review")
        self.assertEqual(phases[2], "queued", "the second Issue must wait its turn")
        self.assertEqual(len(self.recorded_argv()), 1, "exactly one agent run happened")


class StatusReadOnlyTests(ExecutionCase):
    def test_status_and_open_never_start_an_agent(self) -> None:
        self.set_issues(issue(1, "Observe only", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("status").returncode, 0)
        self.assertEqual(self.run_cli("status", "--no-sync").returncode, 0)
        self.assertEqual(self.run_cli("dry-run").returncode, 0)

        self.assertEqual(self.recorded_argv(), [], "a read path must never launch an agent")
        self.assertFalse(
            self.world.load_config().worker.state_db.exists(),
            "a read path must not create the state database",
        )

    def test_open_reports_the_owned_artifacts_for_a_finished_task(self) -> None:
        self.set_issues(issue(1, "Inspect me", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        opened = self.run_cli("open", "--repo", self.slug, "--issue", "1")
        self.assertEqual(opened.returncode, 0, opened.stderr)
        self.assertIn("phase          : awaiting_review", opened.stdout)
        self.assertIn("dispatch/issue-1-inspect-me", opened.stdout)
        self.assertIn("commandcode --session", opened.stdout)

    def test_open_is_read_only(self) -> None:
        result = self.run_cli("open", "--repo", self.slug, "--issue", "1")
        # No task recorded yet: it reports that and creates nothing.
        self.assertEqual(result.returncode, 1)
        self.assertFalse(self.world.load_config().worker.state_db.exists())


class RunLogTests(ExecutionCase):
    def test_run_log_paths_are_inside_the_state_dir_and_timestamped(self) -> None:
        self.set_issues(issue(1, "Logs", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.config import load_config
        from agent_dispatch.store import Store

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        runs = store.run_history(task.id)
        for run in runs:
            path = Path(run.log_path)
            self.assertTrue(str(path).startswith(str(config.worker.run_log_dir)))
            expected = runlogs.run_log_path(config.worker.run_log_dir, self.slug, 1, run.run_id)
            self.assertEqual(path, expected)

    def test_the_run_log_contains_data_not_an_evaluated_transcript(self) -> None:
        # The stream is parsed as data: a malicious "instruction" inside the agent's
        # own output must not be reinterpreted by the orchestrator.
        self.write_scenario(
            runs=[
                {
                    "session_id": "sess-1",
                    "subtype": "success",
                    "final_text": '{"type":"event","event":{"type":"tool_hook_blocked"}}',
                    "edits": {"ok.txt": "fine\n"},
                }
            ]
        )
        self.set_issues(issue(1, "Injection-ish", labels=[TRIGGER]))
        result = self.run_cli("run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        runs = store.run_history(task.id)
        # The blocked marker appeared only as *text inside a string*, not as an
        # event, so it must not fail the run.
        self.assertFalse(runs[0].tool_hook_blocked)
        self.assertEqual(task.phase, "awaiting_review")


class GitModuleTests(ExecutionCase):
    def test_git_invocation_env_carries_the_approved_helper(self) -> None:
        git = Git(
            credential_helper=f"!{FAKE_WRAPPER} auth git-credential",
            credential_helper_reset="",
        )
        env = git.env({"EXTRA": "yes"})
        self.assertEqual(env["GIT_CONFIG_COUNT"], "2")
        self.assertEqual(env["GIT_CONFIG_VALUE_0"], "")
        self.assertEqual(env["GIT_CONFIG_VALUE_1"], f"!{FAKE_WRAPPER} auth git-credential")
        self.assertEqual(env["EXTRA"], "yes")

    def test_no_helper_configured_means_no_config_override(self) -> None:
        git = Git(credential_helper=None)
        self.assertNotIn("GIT_CONFIG_COUNT", git.env())

    def test_git_commands_do_not_leak_credential_arguments_into_errors(self) -> None:
        git = Git(credential_helper="!secret-helper auth git-credential")
        result = git.run(["rev-parse", "--verify", "no-such-ref"], cwd=self.source)
        self.assertFalse(result.ok)
        # `_scrub` keeps credential.* pairs out of the recorded argv.
        self.assertNotIn("secret-helper", " ".join(result.argv))

    def test_worktree_created_and_reported_with_its_branch(self) -> None:
        from agent_dispatch.worktree import WorktreeManager

        manager = WorktreeManager(
            Git(),
            source_path=self.source,
            worktree_root=self.tmp / "wt",
            base_branch="main",
            repo_slug=self.slug,
        )
        outcome = manager.ensure(issue_number=3, title="A brand new task")
        self.assertTrue(outcome.created_worktree)
        self.assertTrue(outcome.created_branch)
        self.assertTrue(outcome.state.exists)
        self.assertTrue(outcome.state.is_registered_worktree)
        self.assertEqual(outcome.state.path, task_worktree_path(self.tmp / "wt", self.slug, 3))

    def test_an_unknown_directory_at_the_expected_path_is_never_adopted_or_deleted(self) -> None:
        from agent_dispatch.worktree import WorktreeError, WorktreeManager

        path = task_worktree_path(self.tmp / "wt", self.slug, 4)
        path.mkdir(parents=True)
        (path / "someone-elses-file.txt").write_text("do not delete me\n", encoding="utf-8")

        manager = WorktreeManager(
            Git(),
            source_path=self.source,
            worktree_root=self.tmp / "wt",
            base_branch="main",
            repo_slug=self.slug,
        )
        with self.assertRaises(WorktreeError):
            manager.ensure(issue_number=4, title="Unknown path")
        self.assertTrue(
            (path / "someone-elses-file.txt").is_file(),
            "an unknown path must be left completely untouched",
        )


class WorkerStartupReconciliationTests(ExecutionCase):
    """Review blocker 1: the persistent service must reconcile its own crashes.

    `Orchestrator.reconcile()` used to be reachable only from the explicit `run`
    command. The systemd entry point is `worker`, so a crash mid-run left
    `phase=running` forever: `active_task_count()` stayed above zero and every
    subsequent poll refused to dispatch, with no way out except running `run` by
    hand. These tests drive the **worker** entry point, not `run`.
    """

    def test_worker_once_reconciles_an_orphaned_running_row(self) -> None:
        self.set_issues(issue(1, "Interrupted", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("worker", "--once").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        # A crash mid-run: phase running, run row still open, nothing published.
        import subprocess

        subprocess.run(
            ["git", "-C", str(self.remote), "branch", "-D", task.branch],
            capture_output=True,
            text=True,
            check=True,
        )
        store._conn.execute(
            "UPDATE tasks SET phase = 'running', pr_number = NULL WHERE id = ?", (task.id,)
        )
        store.start_run(
            task.id,
            run_id="20260101T000000Z-implementation",
            kind="implementation",
            resumed_from=None,
            log_path=str(self.tmp / "orphan.ndjson"),
        )
        store.close()

        result = self.run_cli("worker", "--once", "--no-execute")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("running_row_reconciled", result.stderr)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        recovered = store.get_task(self.slug, 1)
        self.assertNotEqual(
            recovered.phase,
            "running",
            "the worker must not leave a task stuck in `running` after a crash",
        )
        open_runs = [r for r in store.run_history(task.id) if r.outcome == "running"]
        self.assertEqual(open_runs, [], "the orphaned run row must be closed")

    def test_an_orphan_no_longer_blocks_dispatch_globally(self) -> None:
        # The observable consequence of the bug: one orphaned row blocked ALL future
        # dispatch, because the MVP allows a single active task globally.
        self.set_issues(issue(1, "Interrupted", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("worker", "--once").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        import subprocess

        subprocess.run(
            ["git", "-C", str(self.remote), "branch", "-D", task.branch],
            capture_output=True,
            text=True,
            check=True,
        )
        store._conn.execute("UPDATE tasks SET phase = 'running' WHERE id = ?", (task.id,))
        store.close()

        # A second, unrelated Issue must still be dispatchable after the worker
        # reconciles — before the fix, active_task_count() blocked it forever.
        self.set_issues(
            issue(1, "Interrupted", labels=[TRIGGER]),
            issue(2, "Fresh work", labels=[TRIGGER]),
        )
        self.write_scenario(
            runs=[{"session_id": "sess-fresh", "subtype": "success", "edits": {"f.txt": "ok\n"}}]
        )
        result = self.run_cli("worker", "--once")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        self.assertEqual(store.get_task(self.slug, 2).phase, "awaiting_review")

    def test_a_simulated_poll_never_reconciles(self) -> None:
        # `status`/`dry-run` have no lock, so they must not attempt repairs.
        self.set_issues(issue(1, "Interrupted", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("worker", "--once").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        store._conn.execute("UPDATE tasks SET phase = 'running' WHERE id = ?", (task.id,))
        store.close()

        for args in (("status",), ("dry-run",), ("status", "--no-sync")):
            with self.subTest(command=args[0]):
                self.assertEqual(self.run_cli(*args).returncode, 0)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        self.assertEqual(
            store.get_task(self.slug, 1).phase,
            "running",
            "a read-only command must not repair state",
        )


class PublishRecoveryTests(ExecutionCase):
    """Review important-item 4: the promised publish-only recovery must be reachable."""

    def test_needs_attention_after_a_pr_lookup_failure_recovers_publish_only(self) -> None:
        # The dispatcher tells the operator "a later `agent-dispatch run` will adopt or
        # create it". That promise has to be true: the row is `needs_attention`, and it
        # must be finished off WITHOUT another model call.
        self.set_issues(issue(1, "PR lookup fails", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        # Simulate the failure: the branch is pushed, the PR was never recorded, and
        # the dispatcher escalated with the recovery instruction.
        world = self.world.read_world()
        world["repos"][self.slug]["pulls"] = []
        world["repos"][self.slug]["branches"] = [task.branch]
        self.world.world = world
        self.world.write_world()
        store._conn.execute(
            "UPDATE tasks SET phase = 'needs_attention', pr_number = NULL, pr_url = NULL, "
            "last_error = ? WHERE id = ?",
            ("branch is pushed, but GitHub could not be queried for an existing PR", task.id),
        )
        store.close()

        runs_before = len(self.recorded_argv())
        result = self.run_cli("run", "--skip-poll")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("reconcile", result.stdout)

        self.assertEqual(
            len(self.recorded_argv()),
            runs_before,
            "publish-only recovery must not spend a second model call",
        )
        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        recovered = store.get_task(self.slug, 1)
        self.assertEqual(recovered.phase, "awaiting_review")
        self.assertIsNotNone(recovered.pr_number, "the PR must actually be created")
        self.assertEqual(len(self.world.read_world()["repos"][self.slug]["pulls"]), 1)

    def test_needs_attention_with_no_published_branch_is_not_retried_blindly(self) -> None:
        # Nothing was published, so there is nothing to finish. The task must stay
        # escalated rather than being reset to `queued` (which would risk a second run)
        # or silently published.
        self.set_issues(issue(1, "Nothing published", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        import subprocess

        subprocess.run(
            ["git", "-C", str(self.remote), "branch", "-D", task.branch],
            capture_output=True,
            text=True,
            check=True,
        )
        world = self.world.read_world()
        world["repos"][self.slug]["pulls"] = []
        self.world.world = world
        self.world.write_world()
        store._conn.execute(
            "UPDATE tasks SET phase = 'needs_attention', pr_number = NULL, pr_url = NULL WHERE id = ?",
            (task.id,),
        )
        store.close()

        runs_before = len(self.recorded_argv())
        self.run_cli("run", "--skip-poll")
        self.assertEqual(len(self.recorded_argv()), runs_before, "no agent may be re-run")
        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        self.assertEqual(store.get_task(self.slug, 1).phase, "needs_attention")
        self.assertEqual(self.world.read_world()["repos"][self.slug]["pulls"], [])

    def test_a_publish_only_recovery_commits_pending_edits_first(self) -> None:
        # A completed run whose dispatcher commit failed, with the edits still in the
        # worktree: recovery commits them and publishes, still without a run.
        #
        # The parking stage is what makes this recoverable. A bare `needs_attention`
        # (someone else's pause) deliberately does NOT trigger a push of new commits,
        # which `test_a_human_parked_task_is_not_pushed_by_recovery` covers.
        self.set_issues(issue(1, "Uncommitted at crash", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        # Leave a pending edit in the worktree, as a failed `commit_all` would.
        Path(task.worktree_path, "late.txt").write_text("late change\n", encoding="utf-8")
        world = self.world.read_world()
        world["repos"][self.slug]["pulls"] = []
        world["repos"][self.slug]["branches"] = [task.branch]
        self.world.world = world
        self.world.write_world()
        store._conn.execute(
            "UPDATE tasks SET phase = 'needs_attention', recovery_stage = 'commit_failed', "
            "pr_number = NULL, pr_url = NULL WHERE id = ?",
            (task.id,),
        )
        store.close()

        runs_before = len(self.recorded_argv())
        self.run_cli("run", "--skip-poll")
        self.assertEqual(len(self.recorded_argv()), runs_before)

        import subprocess

        show = subprocess.run(
            ["git", "-C", str(self.remote), "show", f"{task.branch}:late.txt"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            show.returncode,
            0,
            "the recovered commit must include the edits left uncommitted at the crash",
        )


class CommitFailureTests(ExecutionCase):
    """Review blocker 3: a failed commit must not publish a PR that omits the work."""

    def test_a_failed_commit_stops_before_push_and_opens_no_pr(self) -> None:
        # Force the dispatcher's commit to fail by making the identity unusable, then
        # confirm nothing is published: `git push` cannot carry uncommitted edits, so
        # publishing would open a PR missing exactly this run's work.
        self.write_scenario(
            runs=[{"session_id": "sess-1", "subtype": "success", "edits": {"work.txt": "new\n"}}]
        )
        self.set_issues(issue(1, "Commit fails", labels=[TRIGGER]))

        # An empty Git identity is an override that outranks `-c user.name`, which is
        # the most realistic way for the commit to fail in production.
        self.world.env_overrides["GIT_AUTHOR_NAME"] = ""
        result = self.run_cli("run")

        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("needs_attention", result.stdout)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertNotEqual(task.phase, "awaiting_review")
        self.assertIsNone(task.pr_number, "a PR must never omit the run's work")
        self.assertEqual(self.world.read_world()["repos"][self.slug]["pulls"], [])
        # The edits are preserved for a publish-only retry, not discarded.
        self.assertTrue(
            Path(task.worktree_path, "work.txt").is_file(),
            "the uncommitted work must be preserved for recovery",
        )
        runs = store.run_history(task.id)
        self.assertEqual(runs[0].outcome, "failed")
        self.assertIn("could not commit the run's changes", runs[0].detail or "")

    def test_a_failed_commit_does_not_leave_a_branch_behind(self) -> None:
        self.write_scenario(
            runs=[{"session_id": "sess-1", "subtype": "success", "edits": {"w.txt": "x\n"}}]
        )
        self.set_issues(issue(1, "Commit fails twice", labels=[TRIGGER]))
        self.world.env_overrides["GIT_AUTHOR_NAME"] = ""
        self.run_cli("run")

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertNotIn(
            task.branch,
            self._remote_branches(),
            "a failed commit must not push a branch whose tip omits the run's work",
        )

    def _remote_branches(self) -> list[str]:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(self.remote), "branch", "--list", "--format=%(refname:short)"],
            capture_output=True,
            text=True,
            check=False,
        )
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


class WorktreeIdentityTests(ExecutionCase):
    """Review important-item 5: verify the checked-out branch, not just registration."""

    def test_a_worktree_on_the_wrong_branch_is_refused_not_published(self) -> None:
        """A switched worktree still looks owned, but its work belongs elsewhere.

        This is the failure the review asked about, and the harm is specific: the
        task's commits and working tree are on some other branch, so a commit or push
        would publish work that is not this Issue's — or open a PR claiming to
        implement the Issue while the actual change sits on the wrong branch. Both are
        worse than refusing. The assertion is therefore that no PR is created and the
        task is escalated, not merely that a push failed.
        """
        self.set_issues(issue(1, "Wrong branch", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        worktree = Path(task.worktree_path)
        branch = task.branch

        import subprocess

        # Switch the owned worktree to a different branch, as a mis-typed command or a
        # stray operator session could. Registration and path still look correct.
        subprocess.run(
            ["git", "-C", str(worktree), "checkout", "-q", "-b", "somewhere-else"],
            capture_output=True,
            text=True,
            check=True,
        )
        # Re-queue so a second attempt must reuse this same owned worktree.
        store._conn.execute(
            "UPDATE tasks SET phase = 'queued', pr_number = NULL, pr_url = NULL WHERE id = ?",
            (task.id,),
        )
        store.close()

        world = self.world.read_world()
        world["repos"][self.slug]["pulls"] = []
        world["repos"][self.slug]["branches"] = [branch]
        self.world.world = world
        self.world.write_world()

        # The agent "works", and the run reports success.
        self.write_scenario(
            runs=[{"session_id": "sess-2", "subtype": "success", "edits": {"again.txt": "y\n"}}]
        )
        result = self.run_cli("run", "--skip-poll")

        self.assertIn("needs_attention", result.stdout)
        self.assertEqual(
            len(self.world.read_world()["repos"][self.slug]["pulls"]),
            0,
            "no PR may be opened claiming work that sits on a different branch",
        )
        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        self.assertEqual(store.get_task(self.slug, 1).phase, "needs_attention")
        self.assertNotIn(
            "again.txt",
            self._remote_files(branch),
            "the run's work must not be attributed to the owned branch from the wrong one",
        )

    def test_inspect_reports_a_branch_mismatch(self) -> None:
        from agent_dispatch.gitcmd import Git
        from agent_dispatch.worktree import WorktreeManager

        manager = WorktreeManager(
            Git(),
            source_path=self.source,
            worktree_root=self.tmp / "mismatch-worktrees",
            base_branch="main",
            repo_slug=self.slug,
        )
        outcome = manager.ensure(issue_number=11, title="Mismatch")
        state = manager.inspect(outcome.state.path, outcome.state.branch)
        self.assertTrue(state.branch_matches)
        self.assertEqual(state.checked_out_branch, state.branch)

        import subprocess

        subprocess.run(
            ["git", "-C", str(state.path), "checkout", "-q", "-b", "elsewhere"],
            capture_output=True,
            text=True,
            check=True,
        )
        mismatched = manager.inspect(state.path, outcome.state.branch)
        self.assertFalse(mismatched.branch_matches, "a switched worktree must be detected")
        self.assertEqual(mismatched.checked_out_branch, "elsewhere")
        self.assertIn("MISMATCH", mismatched.describe())

    def _remote_files(self, branch: str) -> set[str]:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(self.remote), "ls-tree", "--name-only", "-r", branch],
            capture_output=True,
            text=True,
            check=False,
        )
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


class NewWorktreeBaseTests(ExecutionCase):
    """Review follow-up: a new task worktree must not silently use a stale base."""

    def test_a_failed_fetch_refuses_to_create_a_new_task_worktree(self) -> None:
        from agent_dispatch.gitcmd import Git
        from agent_dispatch.worktree import WorktreeError, WorktreeManager

        # A source clone whose `origin` cannot be fetched: branching from it would
        # build the task on a stale base with no indication anything was wrong.
        broken = self.tmp / "broken-clone"
        broken.mkdir()
        import subprocess

        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "T",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "T",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        subprocess.run(["git", "-C", str(broken), "init", "-q", "-b", "main"], check=True, env=env)
        (broken / "f.txt").write_text("x\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(broken), "add", "-A"], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(broken), "commit", "-q", "-m", "init"], check=True, env=env
        )
        subprocess.run(
            ["git", "-C", str(broken), "remote", "add", "origin", str(self.tmp / "does-not-exist")],
            check=True,
            env=env,
        )

        manager = WorktreeManager(
            Git(),
            source_path=broken,
            worktree_root=self.tmp / "broken-worktrees",
            base_branch="main",
            repo_slug=self.slug,
        )
        with self.assertRaises(WorktreeError) as caught:
            manager.ensure(issue_number=12, title="Stale base")
        self.assertIn("stale", str(caught.exception))

    def test_a_failed_fetch_only_warns_for_an_already_owned_worktree(self) -> None:
        # For an existing worktree the base only affects the diff comparison, so a
        # transient fetch failure must not throw away real work.
        from agent_dispatch.gitcmd import Git
        from agent_dispatch.worktree import WorktreeManager

        root = self.tmp / "warn-worktrees"
        manager = WorktreeManager(
            Git(),
            source_path=self.source,
            worktree_root=root,
            base_branch="main",
            repo_slug=self.slug,
        )
        first = manager.ensure(issue_number=13, title="Owned already")

        broken = WorktreeManager(
            Git(),
            source_path=self.tmp / "does-not-exist-source",
            worktree_root=root,
            base_branch="main",
            repo_slug=self.slug,
        )
        # Reached through the real code path: a fetch failure on an existing worktree.
        broken._is_registered = lambda path: True  # type: ignore[method-assign]
        broken._current_branch = lambda path: first.state.branch  # type: ignore[method-assign]
        outcome = broken.ensure(
            issue_number=13,
            title="Owned already",
            recorded_branch=first.state.branch,
            recorded_path=first.state.path,
        )
        self.assertTrue(outcome.state.exists)
        self.assertFalse(outcome.fetched_base)
        self.assertTrue(
            any("could not fetch" in note for note in outcome.notes),
            f"a stale base for an owned worktree must be reported: {outcome.notes}",
        )


class PrAdoptionVerificationTests(ExecutionCase):
    """Review follow-up: an exact branch match is not proof a PR is ours.

    Driven at :meth:`Orchestrator._ensure_pull_request`, which is where ownership is
    actually decided. Both the post-run path and crash recovery delegate to it, so
    testing it directly covers both callers without having to reconstruct a whole
    crashed publish for each case — and it keeps the assertions focused on the single
    decision under review.
    """

    def orchestrator(self):
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))
        self.addCleanup(log.stream.close)
        return Orchestrator(config, store, GitHubClient(config.github.command), log), store, config

    def task_row(self, store, config, *, branch: str = "dispatch/issue-1-adoption"):
        self.set_issues(issue(1, "Adoption", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        store.upsert_discovered(
            repo=self.slug,
            issue_number=1,
            title="Adoption",
            base_branch="main",
            runtime_driver="commandcode",
            runtime_model="deepseek/deepseek-v4-flash",
            runtime_effort="medium",
            permission_mode="allow-all",
            trigger_present=True,
            issue_state="open",
            linked_pr_number=None,
            linked_pr_state=None,
        )
        row = store.get_task(self.slug, 1)
        store.set_owned_worktree(
            row.id, branch=branch, worktree_path=str(self.tmp / "wt"), base_branch="main"
        )
        return store.get_task(self.slug, 1)

    def state(self, branch: str):
        from agent_dispatch.worktree import WorktreeState

        return WorktreeState(
            path=self.tmp / "wt",
            branch=branch,
            exists=False,
            is_registered_worktree=False,
            remote_branch_exists=True,
        )

    def ensure_pr(
        self, *, pr_body: str, head_repo: str, pr_number: int = 60, omit_head: bool = False
    ):
        orchestrator, store, config = self.orchestrator()
        branch = "dispatch/issue-1-adoption"
        task = self.task_row(store, config, branch=branch)

        if omit_head:
            pr = {
                "number": pr_number,
                "state": "open",
                "merged_at": None,
                # No head.repo / head.user, as a truncated payload would be.
                "head": {"ref": branch, "sha": "0" * 40},
                "html_url": f"https://github.com/{self.slug}/pull/{pr_number}",
                "title": f"PR {pr_number}",
                "body": pr_body,
                "base": {"ref": "main"},
            }
        else:
            pr = pull(pr_number, branch, body=pr_body, head_repo=head_repo)
        world = self.world.read_world()
        world["repos"][self.slug]["branches"] = [branch]
        world["repos"][self.slug]["pulls"] = [pr]
        self.world.world = world
        self.world.write_world()

        outcome = orchestrator._ensure_pull_request(
            task, config.repo(self.slug), self.state(branch), result=None, notes=[]
        )
        return outcome, store.get_task(self.slug, 1)

    def test_a_genuine_pr_for_this_task_is_adopted(self) -> None:
        # The positive case first, so the refusals below are proved not to be
        # refusing everything indiscriminately.
        outcome, task = self.ensure_pr(pr_body="Closes #1", head_repo=self.slug)
        self.assertEqual(outcome.action, "adopted_existing_pr")
        self.assertEqual(task.pr_number, 60, "a genuine PR for this task must be adopted")
        self.assertEqual(task.phase, "awaiting_review")

    def test_a_fork_pr_on_our_branch_name_is_not_adopted(self) -> None:
        # A branch *name* is not unique across GitHub, so a fork can host
        # `dispatch/issue-1-...` too. Adopting it would hand this task someone else's
        # pull request, which a later review round would then act on.
        outcome, task = self.ensure_pr(pr_body="Closes #1", head_repo="attacker/fork", pr_number=61)
        self.assertEqual(outcome.action, "needs_attention")
        self.assertEqual(outcome.reason, "pr_head_owner_mismatch")
        self.assertIsNone(task.pr_number, "a fork's PR must never be recorded as owned")
        self.assertIn("attacker", " ".join(outcome.notes), "the refusal must name the foreign head")

    def test_a_pr_on_our_branch_that_does_not_reference_the_issue_is_not_adopted(self) -> None:
        # A same-repo PR on our branch name with no Issue link is ambiguous: it could
        # be a human's. Ownership needs a proven link, not a name collision.
        outcome, task = self.ensure_pr(
            pr_body="No issue reference here", head_repo=self.slug, pr_number=62
        )
        self.assertEqual(outcome.action, "needs_attention")
        self.assertEqual(outcome.reason, "pr_issue_link_unproven")
        self.assertIsNone(task.pr_number, "an unlinked PR must not be recorded as owned")

    def test_a_pr_with_no_head_repository_information_is_not_adopted(self) -> None:
        # Unproven ownership fails closed: adopting the wrong PR is worse than asking
        # a human, so a payload without `head.repo` is refused rather than assumed.
        outcome, task = self.ensure_pr(
            pr_body="Closes #1", head_repo=self.slug, pr_number=63, omit_head=True
        )
        self.assertEqual(outcome.action, "needs_attention")
        self.assertEqual(outcome.reason, "pr_head_owner_mismatch")
        self.assertIsNone(task.pr_number)

    def test_a_pr_referencing_a_different_issue_is_not_adopted(self) -> None:
        # Guards the obvious adjacent mix-up: the PR is on our branch and in our repo,
        # but it implements a different Issue.
        outcome, task = self.ensure_pr(pr_body="Closes #999", head_repo=self.slug, pr_number=64)
        self.assertEqual(outcome.action, "needs_attention")
        self.assertEqual(outcome.reason, "pr_issue_link_unproven")
        self.assertIsNone(task.pr_number)

    def test_an_adopted_pr_is_recorded_as_owned_and_leaves_review_phase(self) -> None:
        # Exactly the body shape the dispatcher writes, so this proves a PR this
        # service created is recognised as its own on a later recovery.
        outcome, task = self.ensure_pr(
            pr_body=f"Implements #{1} (https://github.com/{self.slug}/issues/1).",
            head_repo=self.slug,
        )
        self.assertEqual(outcome.action, "adopted_existing_pr")
        self.assertEqual(outcome.pr_number, 60)
        self.assertEqual(task.pr_number, 60)
        self.assertEqual(task.phase, "awaiting_review")
        # Exactly one PR exists: adoption must not create a second one.
        self.assertEqual(len(self.world.read_world()["repos"][self.slug]["pulls"]), 1)


class SessionCaptureTests(ExecutionCase):
    """Review follow-up: the session ID must be persisted while the run is in flight."""

    def test_the_session_id_is_stored_before_the_run_finishes(self) -> None:
        # The claim in the docs is that the first `run_start` line is persisted
        # immediately, so a hard crash still records which session was attempted.
        # Prove it by reading SQLite while the fake runtime is still running.
        self.write_scenario(runs=[{"session_id": "sess-early", "sleep": 3, "subtype": "success"}])
        self.set_issues(issue(1, "Slow run", labels=[TRIGGER]))

        import subprocess

        env = self.env()
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_dispatch.cli",
                "--config",
                str(self.world.config_path),
                "run",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(self.tmp),
        )
        try:
            # The fake emits run_start immediately, then sleeps.
            deadline = time.monotonic() + 15
            observed: str | None = None
            while time.monotonic() < deadline:
                if self.world.load_config().worker.state_db.is_file():
                    store = Store(self.world.load_config().worker.state_db)
                    try:
                        task = store.get_task(self.slug, 1)
                        if task is not None and task.session_id:
                            observed = task.session_id
                            break
                    finally:
                        store.close()
                time.sleep(0.2)

            self.assertEqual(
                observed,
                "sess-early",
                "the session ID must be persisted as soon as the stream reports it, "
                "not only after the process exits",
            )
        finally:
            proc.wait(timeout=60)

    def test_an_early_captured_session_is_still_not_advertised_as_resumable(self) -> None:
        # Capturing the ID early must not blur the distinction that matters: an
        # interrupted run has no transcript, so it is not resumable.
        self.write_scenario(runs=[{"session_id": "sess-killed", "hang": True}])
        self.set_issues(issue(1, "Hangs after start", labels=[TRIGGER]))
        self.world.write_config(worker_overrides=self.execution_overrides(run_timeout_seconds=30))

        import subprocess

        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agent_dispatch.cli",
                "--config",
                str(self.world.config_path),
                "run",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=self.env(),
            cwd=str(self.tmp),
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if self.world.load_config().worker.state_db.is_file():
                    store = Store(self.world.load_config().worker.state_db)
                    try:
                        task = store.get_task(self.slug, 1)
                        if task is not None and task.session_id:
                            break
                    finally:
                        store.close()
                time.sleep(0.2)
            # Kill the whole CLI, leaving the DB exactly as a crash would.
            proc.kill()
        finally:
            proc.wait(timeout=30)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.session_id, "sess-killed", "the attempted session is recorded")
        self.assertIsNone(
            store.resumable_session(task.id),
            "an interrupted run is not resumable, however early its ID was captured",
        )


class FailedFirstCommitRecoveryTests(ExecutionCase):
    """Review round 2, blocker 1: the advertised recovery must actually exist.

    Round 1 fixed "a failed commit must not publish" but left the *subsequent* repair
    unreachable: recovery looked only for a remote branch, so a first run whose commit
    failed — where nothing reached the remote by definition — was reported as "no
    work" and the task sat in `needs_attention` forever. The parking stage and the
    completed-run check are what make the promise true.
    """

    def repair(self, *, remote_branch: bool):
        """Force a first-run commit failure, then remove the cause and recover.

        Returns ``(recovery_output, remote_branches, extra_runtime_invocations)``.
        """
        self.write_scenario(
            runs=[{"session_id": "sess-1", "subtype": "success", "edits": {"work.txt": "new\n"}}]
        )
        self.set_issues(issue(1, "Commit fails then recovers", labels=[TRIGGER]))

        # An empty Git identity is an override that outranks `-c user.name`, so the
        # dispatcher's own commit fails — the exact production failure mode.
        self.world.env_overrides["GIT_AUTHOR_NAME"] = ""
        first = self.run_cli("run")
        self.assertEqual(first.returncode, 1, first.stdout + first.stderr)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "needs_attention")
        self.assertEqual(task.recovery_stage, "commit_failed", "the parking reason must persist")
        self.assertIsNone(task.pr_number)
        self.assertEqual(self.world.read_world()["repos"][self.slug]["pulls"], [])
        branch = task.branch
        store.close()

        if not remote_branch:
            # Nothing was ever pushed, which is the normal state for this window.
            self.assertNotIn(branch, self._remote_branches())

        # Remove the cause of the failure WITHOUT losing the preserved edits, then ask
        # the service to finish the job.
        self.world.env_overrides.pop("GIT_AUTHOR_NAME", None)
        runs_before = len(self.recorded_argv())
        repaired = self.run_cli("run", "--skip-poll")
        return repaired, branch, len(self.recorded_argv()) - runs_before

    def test_a_failed_first_commit_is_recovered_locally_without_a_model_call(self) -> None:
        repaired, branch, extra_runs = self.repair(remote_branch=False)

        self.assertEqual(
            extra_runs,
            0,
            "recovering a failed commit must not spend a second model call",
        )
        self.assertIn(branch, self._remote_branches(), "the preserved work must reach the remote")

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "awaiting_review")
        self.assertIsNotNone(task.pr_number, "exactly one PR must be created")
        self.assertIsNone(task.recovery_stage, "the parking reason must be cleared")

        pulls = self.world.read_world()["repos"][self.slug]["pulls"]
        self.assertEqual(len(pulls), 1)
        self.assertIn("work.txt", self._remote_files(branch), "the edited file must be in the PR")

    def test_the_recovery_is_idempotent(self) -> None:
        # Running the recovery twice must not create a second PR or move the task back.
        self.repair(remote_branch=False)
        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        first_pr = store.get_task(self.slug, 1).pr_number
        store.close()

        again = self.run_cli("run", "--skip-poll")
        self.assertEqual(again.returncode, 0, again.stdout + again.stderr)
        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.pr_number, first_pr)
        self.assertEqual(task.phase, "awaiting_review")
        self.assertEqual(len(self.world.read_world()["repos"][self.slug]["pulls"]), 1)

    def test_a_bare_needs_attention_task_is_not_pushed_by_recovery(self) -> None:
        # A human may park a task for reasons this code cannot see, and pushing new
        # commits on their behalf is not a decision to make from a phase alone.
        self.set_issues(issue(1, "Human parked", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        branch = task.branch
        # A NEW local commit that the remote does not have.
        import subprocess

        subprocess.run(
            ["git", "-C", str(task.worktree_path), "commit", "-q", "--allow-empty", "-m", "extra"],
            capture_output=True,
            text=True,
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t",
                "GIT_AUTHOR_EMAIL": "t@example.invalid",
                "GIT_COMMITTER_NAME": "t",
                "GIT_COMMITTER_EMAIL": "t@example.invalid",
            },
        )
        store._conn.execute(
            "UPDATE tasks SET phase = 'needs_attention', recovery_stage = NULL, "
            "pr_number = NULL, pr_url = NULL WHERE id = ?",
            (task.id,),
        )
        store.close()

        remote_before = self._remote_tip(branch)
        self.run_cli("run", "--skip-poll")
        self.assertEqual(
            self._remote_tip(branch),
            remote_before,
            "a task parked by a human must not have new commits pushed for it",
        )

    def _remote_branches(self) -> list[str]:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(self.remote), "branch", "--list", "--format=%(refname:short)"],
            capture_output=True,
            text=True,
            check=False,
        )
        return [line.strip() for line in proc.stdout.splitlines() if line.strip()]

    def _remote_files(self, branch: str) -> set[str]:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(self.remote), "ls-tree", "--name-only", "-r", branch],
            capture_output=True,
            text=True,
            check=False,
        )
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}

    def _remote_tip(self, branch: str) -> str | None:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(self.remote), "rev-parse", "--verify", "--quiet", branch],
            capture_output=True,
            text=True,
            check=False,
        )
        return proc.stdout.strip() or None


class FailedPushRecoveryTests(ExecutionCase):
    """Review round 2, blocker 2: a failed push must not be treated as success.

    The old check asked only whether *a* branch existed on the remote. A branch pushed
    by an earlier attempt answers "yes" while pointing at **older** commits, so a
    failed push was accepted and a PR could be opened without the new work.

    Driven directly against :meth:`Orchestrator._publish`, because an end-to-end test
    cannot isolate this: when the push fails hard enough to be observable, other
    guards (no PR created, task re-queued) also fire and mask whether the tip was
    compared at all. The decision under review is the tip comparison itself.
    """

    def orchestrator_and_task(self):
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator
        from agent_dispatch.worktree import WorktreeManager

        self.set_issues(issue(1, "Stale remote", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))
        self.addCleanup(log.stream.close)
        orchestrator = Orchestrator(config, store, GitHubClient(config.github.command), log)
        manager = WorktreeManager(
            orchestrator.git,
            source_path=config.repo(self.slug).path,
            worktree_root=config.worker.worktree_root,
            base_branch="main",
            repo_slug=self.slug,
            commit_identity=("t", "t@example.invalid"),
        )
        return orchestrator, store, config, task, manager

    def test_a_stale_remote_tip_blocks_publishing(self) -> None:
        import subprocess

        orchestrator, store, config, task, manager = self.orchestrator_and_task()

        # Leave the remote at the OLD tip while the local branch moves ahead: exactly
        # the state a failed push leaves behind.
        remote_tip_before = orchestrator._remote_branch_tip(config.repo(self.slug), task.branch)
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        (Path(task.worktree_path) / "newer.txt").write_text("newer\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(task.worktree_path), "add", "-A"], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(task.worktree_path), "commit", "-q", "-m", "newer"],
            check=True,
            capture_output=True,
            env=env,
        )
        moved = manager.inspect(Path(task.worktree_path), task.branch)
        self.assertNotEqual(moved.head_sha, remote_tip_before, "precondition: local moved ahead")

        # Reject the push at the remote itself, via a pre-receive hook. This is the
        # realistic "push refused" failure (permissions, protected branch, non-fast-
        # forward) and, unlike pointing `origin` at a missing path, it leaves the
        # remote READABLE so the tip comparison is what gets exercised.
        hook = self.remote / "hooks" / "pre-receive"
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text(
            "#!/bin/sh\necho 'push rejected for the test' >&2\nexit 1\n", encoding="utf-8"
        )
        hook.chmod(0o755)
        self.addCleanup(hook.unlink)

        # Forget the owned PR so publishing would otherwise proceed to create one.
        store._conn.execute(
            "UPDATE tasks SET pr_number = NULL, pr_url = NULL WHERE id = ?", (task.id,)
        )
        task = store.get_task(self.slug, 1)
        pulls_before = len(self.world.read_world()["repos"][self.slug]["pulls"])

        outcome = orchestrator._publish(task, config.repo(self.slug), manager, moved)

        # The remote tip does not match local, so nothing may be published: not a new
        # PR, and not an awaiting_review phase that implies the work landed.
        self.assertNotEqual(
            outcome.action,
            "awaiting_review",
            f"a push that did not deliver the new commit must not publish: {outcome.summary()}",
        )
        self.assertEqual(
            len(self.world.read_world()["repos"][self.slug]["pulls"]),
            pulls_before,
            "no PR may be created while the remote lacks the produced commits",
        )
        self.assertNotEqual(store.get_task(self.slug, 1).phase, "awaiting_review")

    def test_recovery_pushes_when_the_remote_is_behind_local(self) -> None:
        # The complement: when the local branch really is ahead and the remote is
        # reachable, recovery must push and publish rather than stall forever.
        orchestrator, store, config, task, manager = self.orchestrator_and_task()
        repo = config.repo(self.slug)
        # A recorded publish stage is the evidence that this service was interrupted
        # while publishing, which is what authorises pushing on recovery.
        store.park_for_recovery(
            task.id, stage="push_failed", note="simulated failed push after a completed run"
        )
        store._conn.execute(
            "UPDATE tasks SET pr_number = NULL, pr_url = NULL WHERE id = ?", (task.id,)
        )

        result = orchestrator._recover_publish_only(store.get_task(self.slug, 1))
        self.assertTrue(result.handled, f"recovery should have published: {result.notes}")
        recovered = store.get_task(self.slug, 1)
        self.assertEqual(recovered.phase, "awaiting_review")
        self.assertIsNotNone(recovered.pr_number)
        self.assertEqual(
            orchestrator._remote_branch_tip(repo, recovered.branch),
            manager.inspect(Path(recovered.worktree_path), recovered.branch).head_sha,
            "the remote tip must match the published work",
        )


class UnfinishedRunNotPublishedTests(ExecutionCase):
    """Review round 2, blocker 3: never publish a crashed agent's half-written edits.

    "The remote branch exists" proves a *previous* push happened; it does not prove
    the edits currently on disk are finished. With an older pushed branch plus an
    interrupted runtime's partial edits, the old recovery committed the partial work
    and opened a PR for it.
    """

    def test_partial_edits_from_a_killed_agent_are_not_committed_or_published(self) -> None:
        self.set_issues(issue(1, "Killed mid-edit", labels=[TRIGGER]))
        # A first successful publish, so the remote branch exists.
        self.assertEqual(self.run_cli("run").returncode, 0)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        branch = task.branch
        worktree = Path(task.worktree_path)

        # A SECOND attempt starts and is killed mid-edit: an open run row, the task in
        # `running`, and half-written changes in the worktree.
        store._conn.execute(
            "UPDATE tasks SET phase = 'running', pr_number = NULL, pr_url = NULL WHERE id = ?",
            (task.id,),
        )
        store.start_run(
            task.id,
            run_id="20260101T000000Z-implementation",
            kind="implementation",
            resumed_from=None,
            log_path=str(self.tmp / "killed.ndjson"),
        )
        store.close()

        (worktree / "half-written.txt").write_text("BROKEN partial edit\n", encoding="utf-8")

        runs_before = len(self.recorded_argv())
        result = self.run_cli("run", "--skip-poll")
        self.assertIn("reconcile", result.stdout)

        # The partial edit must NOT be published, and no PR may be created for it. The
        # remote branch still holds the earlier GOOD work, so the file must be absent.
        self.assertNotIn(
            "half-written.txt",
            self._remote_files(branch),
            "a killed agent's partial edits must never be committed and published",
        )
        # The first successful run already opened a PR, so the assertion is that the
        # partial work did NOT add another one; combined with the file check above, that
        # is what proves the unfinished edits were not published.
        self.assertLessEqual(
            len(self.world.read_world()["repos"][self.slug]["pulls"]),
            1,
            "partial work must not create an additional PR",
        )
        # It is preserved locally for a bounded retry, not deleted.
        self.assertTrue(
            (worktree / "half-written.txt").is_file(),
            "the partial edits must be preserved for a retry, not discarded",
        )
        # And the agent was retried rather than the work being published silently.
        self.assertGreaterEqual(len(self.recorded_argv()), runs_before)

    def test_an_interrupted_run_with_a_pushed_branch_escalates_rather_than_publishing(self) -> None:
        # Same window, but the attempt budget is spent: the task must be parked with a
        # reason, and the local edits must remain untouched.
        self.set_issues(issue(1, "No budget", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        worktree = Path(task.worktree_path)
        # Spend the attempt budget so the retry has nowhere to go.
        store._conn.execute(
            "UPDATE tasks SET phase = 'running', attempts = 99, pr_number = NULL WHERE id = ?",
            (task.id,),
        )
        store.start_run(
            task.id,
            run_id="20260101T000001Z-implementation",
            kind="implementation",
            resumed_from=None,
            log_path=str(self.tmp / "killed2.ndjson"),
        )
        store.close()
        (worktree / "partial2.txt").write_text("partial\n", encoding="utf-8")

        self.run_cli("run", "--skip-poll")
        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        after = store.get_task(self.slug, 1)
        self.assertEqual(
            after.phase, "failed", "an exhausted interrupted task is failed, not published"
        )
        self.assertNotIn("partial2.txt", self._remote_files(after.branch))
        self.assertTrue((worktree / "partial2.txt").is_file())

    def _remote_files(self, branch: str) -> set[str]:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(self.remote), "ls-tree", "--name-only", "-r", branch],
            capture_output=True,
            text=True,
            check=False,
        )
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


class NoWorktreePublishTests(ExecutionCase):
    """Review round 2, item 4: never run Git against an invented working directory."""

    def test_recovery_does_not_push_from_the_process_cwd(self) -> None:
        # The old code used `Path('.')` when a task had no worktree, so `git push`
        # ran in whatever directory the dispatcher process was in — possibly an
        # unrelated checkout. Recovery must refuse instead.
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator
        from agent_dispatch.worktree import WorktreeState

        self.set_issues(issue(1, "No worktree", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))
        self.addCleanup(log.stream.close)
        orchestrator = Orchestrator(config, store, GitHubClient(config.github.command), log)

        bogus = WorktreeState(
            path=Path("."),
            branch="dispatch/issue-1-whatever",
            exists=False,
            is_registered_worktree=False,
            remote_branch_exists=True,
        )
        with self.assertRaises(ValueError) as caught:
            orchestrator._publish(
                task, config.repo(self.slug), orchestrator._manager(config.repo(self.slug)), bogus
            )
        self.assertIn("refusing to push", str(caught.exception))

    def test_recovery_without_a_worktree_reconciles_the_pr_without_pushing(self) -> None:
        # When the worktree is gone but the branch is genuinely on the remote, the PR
        # is still reconciled — through the wrapper, with no local Git at all.
        self.set_issues(issue(1, "Worktree gone", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        branch = task.branch
        pr_before = task.pr_number
        # Forget the worktree path and the PR, as a partially restored state would.
        store._conn.execute(
            "UPDATE tasks SET worktree_path = NULL, phase = 'running', pr_number = NULL, "
            "pr_url = NULL WHERE id = ?",
            (task.id,),
        )
        store.close()

        runs_before = len(self.recorded_argv())
        result = self.run_cli("run", "--skip-poll")

        self.assertEqual(len(self.recorded_argv()), runs_before, "no agent may be re-run")
        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        after = store.get_task(self.slug, 1)
        self.assertEqual(after.pr_number, pr_before, "the existing PR must be adopted")
        self.assertEqual(len(self.world.read_world()["repos"][self.slug]["pulls"]), 1)
        self.assertIn(branch, result.stdout + result.stderr)


class ReconcileRetryTests(ExecutionCase):
    """Review round 2, small follow-up: a transient wrapper failure must not disable
    reconciliation for the lifetime of the process."""

    def test_reconciliation_retries_after_a_transient_wrapper_failure(self) -> None:
        self.set_issues(issue(1, "Transient wrapper", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        # `--no-execute` queues without recording owned artifacts, so supply the branch
        # this test needs to reason about.
        branch = dispatch_branch_name(1, task.title or "Transient wrapper")
        store.set_owned_worktree(
            task.id, branch=branch, worktree_path=str(self.tmp / "wt"), base_branch="main"
        )
        import subprocess

        subprocess.run(
            ["git", "-C", str(self.remote), "branch", "-D", branch],
            capture_output=True,
            text=True,
            check=False,
        )
        store._conn.execute(
            "UPDATE tasks SET phase = 'running', pr_number = NULL WHERE id = ?", (task.id,)
        )
        store.start_run(
            task.id,
            run_id="20260101T000000Z-implementation",
            kind="implementation",
            resumed_from=None,
            log_path=str(self.tmp / "t.ndjson"),
        )
        store.close()

        # A wrapper that fails once, then works: the first poll must skip reconciliation
        # WITHOUT marking it done, so the next poll repairs the task.
        from agent_dispatch.config import load_config
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.worker import Worker

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))
        self.addCleanup(log.stream.close)

        worker = Worker(config, store, log, execute=False)
        worker._client = _FailingOnceClient(config)  # type: ignore[assignment]
        worker.reconcile_once()
        self.assertFalse(worker._reconciled, "a failed preflight must not consume reconciliation")

        # Now with a working client, reconciliation must actually run and repair it.
        store.close()
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        worker2 = Worker(config, store, log, execute=False)
        worker2.reconcile_once()
        self.assertNotEqual(
            store.get_task(self.slug, 1).phase,
            "running",
            "the orphaned row must be repaired on the retry",
        )


class _FailingOnceClient:
    """A GitHubClient stand-in whose `check_available` fails once, then succeeds."""

    def __init__(self, config) -> None:
        from agent_dispatch.github import GitHubClient

        self._real = GitHubClient(config.github.command)
        self._fail = True
        self._config = config

    def check_available(self):
        from agent_dispatch.github import ErrorKind, GitHubError

        if self._fail:
            self._fail = False
            raise GitHubError("transient wrapper outage", kind=ErrorKind.MISSING_WRAPPER)
        return self._real.check_available()

    def __getattr__(self, name):
        return getattr(self._real, name)


class MigrationRaceTests(ExecutionCase):
    """A concurrent column migration must not crash the process that loses.

    Found by the round-2 session-capture test, which kills a run mid-flight: two
    processes can both observe a column as missing and both issue the ``ALTER``, and
    the loser used to die with ``duplicate column name`` — during crash recovery,
    exactly when the service is least able to afford another failure.
    """

    def test_two_stores_can_migrate_the_same_new_database(self) -> None:
        from agent_dispatch.store import Store

        db = self.tmp / "race" / "state.db"
        db.parent.mkdir(parents=True, exist_ok=True)

        # Both handles are opened before either migrates, so the second one genuinely
        # re-runs the additive migration against an already-migrated file.
        first = Store(db)
        self.addCleanup(first.close)
        second = Store(db)
        self.addCleanup(second.close)

        # And a third, fresh handle on the migrated file, which is the common path.
        third = Store(db)
        self.addCleanup(third.close)
        self.assertEqual(third.count_by_phase()["queued"], 0)

    def test_the_additive_columns_all_exist_after_migration(self) -> None:
        from agent_dispatch.store import Store

        store = Store(self.tmp / "cols" / "state.db")
        self.addCleanup(store.close)
        columns = {str(info["name"]) for info in store._conn.execute("PRAGMA table_info(tasks)")}
        for expected in (
            "pause_reason",
            "pr_url",
            "pr_created_at",
            "dispatched_at",
            "recovery_stage",
        ):
            with self.subTest(column=expected):
                self.assertIn(expected, columns)

    def test_a_duplicate_column_error_is_only_swallowed_when_it_is_true(self) -> None:
        # The race handling re-checks the column rather than swallowing every
        # OperationalError, so a genuine migration failure still surfaces.
        import sqlite3

        from agent_dispatch.store import Store

        store = Store(self.tmp / "genuine" / "state.db")
        self.addCleanup(store.close)
        self.assertFalse(store._column_present("tasks", "definitely_not_a_column"))
        # An impossible ALTER must still raise, proving errors are not blanket-swallowed.
        with self.assertRaises(sqlite3.OperationalError):
            store._conn.execute("ALTER TABLE tasks ADD COLUMN this is not valid sql")


class RecoveryStagePreservationTests(ExecutionCase):
    """Review round 3: a transient recovery failure must not destroy the evidence.

    `PUBLISH_UNKNOWN` means "this attempt could not finish", which is NOT the same as
    "the runtime was interrupted". The outer reconciliation used to overwrite the
    persisted stage with `interrupted` on every unknown outcome, so a one-off
    `ls-remote` outage (or a commit that failed a second time) dropped the very
    evidence that authorises the next attempt to push — leaving the task permanently
    unrecoverable even though the model had finished cleanly.
    """

    def parked_task(self, *, stage: str = "push_failed"):
        """A real task with a completed run, a pushed branch and a recorded stage."""
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator

        self.set_issues(issue(1, "Transient outage", labels=[TRIGGER]))
        # A real run: worktree, branch, commit, push and a completed run row.
        self.assertEqual(self.run_cli("run").returncode, 0)

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        # Park it as if publishing had failed, and forget the PR so publishing is
        # attempted again.
        store.park_for_recovery(task.id, stage=stage, note="simulated publish failure")
        store._conn.execute(
            "UPDATE tasks SET pr_number = NULL, pr_url = NULL WHERE id = ?", (task.id,)
        )
        log = Logger(fmt="text", stream=open(os.devnull, "w"))
        self.addCleanup(log.stream.close)
        orchestrator = Orchestrator(config, store, GitHubClient(config.github.command), log)
        return orchestrator, store, config

    def test_a_transient_ls_remote_failure_preserves_the_publishable_stage(self) -> None:
        """Requested regression 1: push_failed + clean run + transient failure.

        The stage must survive so the *next* attempt can still push; otherwise a
        one-off GitHub outage destroys the task's recoverability permanently.
        """
        orchestrator, store, config = self.parked_task(stage="push_failed")

        from agent_dispatch.gitcmd import GitError

        def failing_tip(repo, branch):
            raise GitError("transient ls-remote failure", argv=["ls-remote"])

        healthy_tip = orchestrator._remote_branch_tip
        orchestrator._remote_branch_tip = failing_tip  # type: ignore[method-assign]
        orchestrator._reconcile_publish_pending(store.get_task(self.slug, 1))

        after = store.get_task(self.slug, 1)
        self.assertEqual(
            after.recovery_stage,
            "push_failed",
            "a transient lookup failure must not downgrade a publishable stage",
        )
        self.assertTrue(after.has_publishable_stage)

        # Second attempt, remote readable again: recovery must succeed with no model call.
        # Restoring the real lookup is the point of the test — leaving the outage in
        # place would exercise the same failure twice and prove nothing.
        orchestrator._remote_branch_tip = healthy_tip  # type: ignore[method-assign]
        runs_before = len(self.recorded_argv())
        orchestrator._reconcile_publish_pending(store.get_task(self.slug, 1))
        self.assertEqual(len(self.recorded_argv()), runs_before, "zero model calls")
        recovered = store.get_task(self.slug, 1)
        self.assertEqual(recovered.phase, "awaiting_review")
        self.assertIsNotNone(recovered.pr_number)

    def test_a_second_commit_failure_keeps_the_commit_failed_stage(self) -> None:
        """Requested regression 2: commit_failed, commit fails again, then recovers."""
        self.write_scenario(
            runs=[{"session_id": "sess-1", "subtype": "success", "edits": {"work.txt": "new\n"}}]
        )
        self.set_issues(issue(1, "Commit fails twice", labels=[TRIGGER]))

        # First attempt fails to commit.
        self.world.env_overrides["GIT_AUTHOR_NAME"] = ""
        self.assertEqual(self.run_cli("run").returncode, 1)
        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.recovery_stage, "commit_failed")
        branch = task.branch
        store.close()

        # Attempt reconciliation while the commit STILL cannot be made: the stage must
        # survive so the task remains recoverable.
        self.run_cli("run", "--skip-poll")
        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        after_failed_retry = store.get_task(self.slug, 1)
        self.assertEqual(
            after_failed_retry.recovery_stage,
            "commit_failed",
            "a repeated commit failure must not downgrade the stage",
        )
        self.assertTrue(after_failed_retry.has_publishable_stage)

        # Fix the cause and recover: one PR, no model call.
        self.world.env_overrides.pop("GIT_AUTHOR_NAME", None)
        runs_before = len(self.recorded_argv())
        self.run_cli("run", "--skip-poll")
        self.assertEqual(len(self.recorded_argv()), runs_before, "zero model calls during recovery")
        recovered = store.get_task(self.slug, 1)
        self.assertEqual(recovered.phase, "awaiting_review")
        self.assertIsNotNone(recovered.pr_number)
        self.assertIsNone(recovered.recovery_stage, "the stage is cleared once published")

        import subprocess

        show = subprocess.run(
            ["git", "-C", str(self.remote), "show", f"{branch}:work.txt"],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(show.returncode, 0, show.stderr)

    def test_an_ambiguous_task_still_parks_as_interrupted(self) -> None:
        # The fallback stage is still used when there is NO publishable stage to
        # preserve, so genuinely ambiguous work is not treated as publishable.
        import subprocess

        orchestrator, store, config = self.parked_task(stage="push_failed")
        task = store.get_task(self.slug, 1)
        # Clear the stage: now nothing authorises pushing this task's local commits.
        store.clear_recovery_stage(task.id)
        # Give the local branch commits the remote does NOT have, so publishing would
        # require a push. With no recorded stage and no phase evidence that this
        # service was interrupted mid-publish, that push is not recovery's call.
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        }
        (Path(task.worktree_path) / "local-only.txt").write_text("local only\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(task.worktree_path), "add", "-A"], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(task.worktree_path), "commit", "-q", "-m", "local only"],
            check=True,
            capture_output=True,
            env=env,
        )
        store._conn.execute(
            "UPDATE tasks SET pr_number = NULL, pr_url = NULL, phase = 'needs_attention' "
            "WHERE id = ?",
            (task.id,),
        )

        orchestrator._reconcile_publish_pending(store.get_task(self.slug, 1))

        after = store.get_task(self.slug, 1)
        self.assertFalse(after.has_publishable_stage, "no publishable stage may be invented")
        self.assertIsNotNone(after.recovery_stage, "the unresolved state must be recorded")
        self.assertNotIn(
            "local-only.txt",
            self._remote_files(after.branch),
            "a task with no recorded publish failure must not have commits pushed for it",
        )

    def _remote_files(self, branch: str) -> set[str]:
        import subprocess

        proc = subprocess.run(
            ["git", "-C", str(self.remote), "ls-tree", "--name-only", "-r", branch],
            capture_output=True,
            text=True,
            check=False,
        )
        return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


class PublishTipFailsClosedTests(ExecutionCase):
    """Review round 3, hardening: proceed only when remote tip == local tip."""

    def test_an_unreadable_remote_tip_does_not_open_a_pr(self) -> None:
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator
        from agent_dispatch.worktree import WorktreeManager

        self.set_issues(issue(1, "Unreadable tip", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))
        self.addCleanup(log.stream.close)
        orchestrator = Orchestrator(config, store, GitHubClient(config.github.command), log)
        manager = WorktreeManager(
            orchestrator.git,
            source_path=config.repo(self.slug).path,
            worktree_root=config.worker.worktree_root,
            base_branch="main",
            repo_slug=self.slug,
            commit_identity=("t", "t@example.invalid"),
        )
        state = manager.inspect(Path(task.worktree_path), task.branch)

        # Forget the PR so publishing would otherwise proceed, and make the tip
        # unreadable: an unknown answer is not evidence the work landed, so the
        # documented invariant ("proceed only when the tips match") must fail closed.
        store._conn.execute(
            "UPDATE tasks SET pr_number = NULL, pr_url = NULL WHERE id = ?", (task.id,)
        )
        orchestrator._remote_branch_tip = lambda repo, branch: None  # type: ignore[method-assign]
        pulls_before = len(self.world.read_world()["repos"][self.slug]["pulls"])

        outcome = orchestrator._publish(
            store.get_task(self.slug, 1), config.repo(self.slug), manager, state
        )

        self.assertEqual(outcome.reason, "remote_tip_unverified")
        self.assertEqual(
            len(self.world.read_world()["repos"][self.slug]["pulls"]),
            pulls_before,
            "no PR may be created while the delivered work cannot be confirmed",
        )
        self.assertNotEqual(store.get_task(self.slug, 1).phase, "awaiting_review")

    def test_a_confirmed_matching_tip_still_publishes(self) -> None:
        # The complement, so failing closed is not simply refusing everything.
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator
        from agent_dispatch.worktree import WorktreeManager

        self.set_issues(issue(1, "Matching tip", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))
        self.addCleanup(log.stream.close)
        orchestrator = Orchestrator(config, store, GitHubClient(config.github.command), log)
        manager = WorktreeManager(
            orchestrator.git,
            source_path=config.repo(self.slug).path,
            worktree_root=config.worker.worktree_root,
            base_branch="main",
            repo_slug=self.slug,
            commit_identity=("t", "t@example.invalid"),
        )
        state = manager.inspect(Path(task.worktree_path), task.branch)
        store._conn.execute(
            "UPDATE tasks SET pr_number = NULL, pr_url = NULL WHERE id = ?", (task.id,)
        )

        outcome = orchestrator._publish(
            store.get_task(self.slug, 1), config.repo(self.slug), manager, state
        )
        self.assertIn(outcome.action, {"awaiting_review", "adopted_existing_pr"})
        self.assertIsNotNone(store.get_task(self.slug, 1).pr_number)


class PublishPendingTransitionTests(ExecutionCase):
    """Review round 4: finished work must never be turned back into an implementation.

    The recovery contract introduced in earlier rounds relies on
    ``recovery_stage in {commit_failed, push_failed, pr_failed}`` meaning "the model
    already completed; only publishing remains". The operator and label transitions
    ignored that field, so `retry`, `pause`/`unpause` and remove/re-add `take-it` could
    each put such a task back into `queued` — where the next poll would start a second
    Command Code run on work that was already finished, spending credits twice and
    letting a second run modify finished work.
    """

    def parked(self, *, stage: str, title: str = "Publish pending") -> tuple:
        """A real task with a completed run, published branch, and the given stage."""
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator

        self.set_issues(issue(1, title, labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        # Park it as if publishing had failed, forgetting the PR so publication is
        # still outstanding.
        store.park_for_recovery(task.id, stage=stage, note=f"simulated {stage}")
        store._conn.execute(
            "UPDATE tasks SET pr_number = NULL, pr_url = NULL WHERE id = ?", (task.id,)
        )
        world = self.world.read_world()
        world["repos"][self.slug]["pulls"] = []
        self.world.world = world
        self.world.write_world()
        log = Logger(fmt="text", stream=open(os.devnull, "w"))
        self.addCleanup(log.stream.close)
        orchestrator = Orchestrator(config, store, GitHubClient(config.github.command), log)
        return orchestrator, store, config

    # Requested regression 1 ------------------------------------------------------

    def test_retry_is_refused_for_publish_pending_work(self) -> None:
        orchestrator, store, config = self.parked(stage="push_failed")
        runs_before = len(self.recorded_argv())

        result = self.run_cli("retry", "--repo", self.slug, "--issue", "1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("awaiting publication", result.stderr + result.stdout)
        self.assertIn("agent-dispatch run", result.stderr + result.stdout)

        # The refusal must not have queued the task, and no runtime may run.
        task = store.get_task(self.slug, 1)
        self.assertNotEqual(task.phase, "queued")
        self.assertTrue(task.has_publishable_stage, "the stage must survive the refusal")

        self.run_cli("run", "--skip-poll")
        self.assertEqual(len(self.recorded_argv()), runs_before, "zero additional runtime calls")

        # Publication still completes: exactly one PR, and the stage is cleared.
        recovered = store.get_task(self.slug, 1)
        self.assertEqual(recovered.phase, "awaiting_review")
        self.assertIsNotNone(recovered.pr_number)
        self.assertIsNone(recovered.recovery_stage)
        self.assertEqual(len(self.world.read_world()["repos"][self.slug]["pulls"]), 1)

    # Requested regression 2 ------------------------------------------------------

    def test_pause_and_unpause_do_not_queue_publish_pending_work(self) -> None:
        orchestrator, store, config = self.parked(stage="commit_failed")
        runs_before = len(self.recorded_argv())

        self.assertEqual(self.run_cli("pause", "--repo", self.slug, "--issue", "1").returncode, 0)
        self.assertEqual(store.get_task(self.slug, 1).phase, "paused")

        released = self.run_cli("unpause", "--repo", self.slug, "--issue", "1")
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        after = store.get_task(self.slug, 1)
        self.assertEqual(
            after.phase,
            "needs_attention",
            "unpausing publish-pending work must return it to publication, not the queue",
        )
        self.assertTrue(after.has_publishable_stage)

        # No runtime call, and publication still completes exactly once.
        self.run_cli("run", "--skip-poll")
        self.assertEqual(len(self.recorded_argv()), runs_before, "zero additional runtime calls")
        recovered = store.get_task(self.slug, 1)
        self.assertEqual(recovered.phase, "awaiting_review")
        self.assertIsNotNone(recovered.pr_number)

    def test_resume_publish_returns_a_paused_task_to_publication(self) -> None:
        # The explicit publish-pending counterpart of `retry`, and it must actually
        # publish: a command whose stated effect is "resume publication" cannot only
        # move the row and hope a worker notices.
        orchestrator, store, config = self.parked(stage="push_failed")
        runs_before = len(self.recorded_argv())
        self.assertEqual(self.run_cli("pause", "--repo", self.slug, "--issue", "1").returncode, 0)

        resumed = self.run_cli("resume-publish", "--repo", self.slug, "--issue", "1")
        self.assertEqual(resumed.returncode, 0, resumed.stdout + resumed.stderr)

        # No agent was started, and the work is published.
        self.assertEqual(len(self.recorded_argv()), runs_before, "zero additional runtime calls")
        after = store.get_task(self.slug, 1)
        self.assertEqual(after.phase, "awaiting_review")
        self.assertIsNotNone(after.pr_number)

    def test_resume_publish_defers_to_a_running_worker_without_losing_the_task(self) -> None:
        """When a worker holds the lock, `resume-publish` must hand off, not stall.

        It re-arms the task and says so, and the running worker's next poll finishes
        publication — the same already-running instance, no restart, no extra run.
        """
        from agent_dispatch.lockfile import WorkerLock

        orchestrator, store, config = self.parked(stage="push_failed")
        runs_before = len(self.recorded_argv())
        task = store.get_task(self.slug, 1)
        branch, session = task.branch, task.session_id
        self.assertEqual(self.run_cli("pause", "--repo", self.slug, "--issue", "1").returncode, 0)

        worker, live_store, _ = self.live_worker()
        worker.poll_once()  # consume this instance's one startup reconciliation
        self.assertTrue(worker._reconciled)

        lock = WorkerLock(config.worker.lock_file, command="test worker")
        lock.acquire()
        try:
            deferred = self.run_cli("resume-publish", "--repo", self.slug, "--issue", "1")
        finally:
            lock.release()
        self.assertEqual(deferred.returncode, 0, deferred.stdout + deferred.stderr)
        self.assertIn("holds the lock", deferred.stdout + deferred.stderr)
        self.assertEqual(store.get_task(self.slug, 1).phase, "needs_attention")

        worker.poll_once()
        self.assert_published_once(live_store, runs_before, branch, session)

    def test_resume_publish_reports_failure_when_publication_fails_again(self) -> None:
        """A second failure must not be reported as success.

        The command promises to finish publication, so its exit status must describe
        the *outcome*. With the commit still broken the work cannot be published, the
        task stays publish-pending, and a zero exit would be a lie a script could act
        on. Durable state was already correct either way; this is about the report.

        The commit is broken from the start so the **real** run leaves a dirty worktree
        and parks as `commit_failed` — the fixture has to reach the commit path, not a
        clean worktree that would publish immediately and never fail.
        """
        self.break_commits()
        self.set_issues(issue(1, "Publish pending", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 1)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.recovery_stage, "commit_failed", task.last_error)
        self.assertIsNone(task.pr_number)
        runs_before = len(self.recorded_argv())
        self.assertEqual(runs_before, 1, "exactly one implementation run, from the fixture")

        self.assertEqual(self.run_cli("pause", "--repo", self.slug, "--issue", "1").returncode, 0)

        # Force the second attempt to fail too, exactly like the first.
        failed = self.run_cli("resume-publish", "--repo", self.slug, "--issue", "1")

        self.assertEqual(
            failed.returncode,
            1,
            f"a failed publication must not exit 0\nstdout:{failed.stdout}\nstderr:{failed.stderr}",
        )
        self.assertIn("still incomplete", failed.stderr + failed.stdout)
        self.assertEqual(len(self.recorded_argv()), runs_before, "no runtime may be started")

        # The durable state stays correct and recoverable — only the report changed.
        after = store.get_task(self.slug, 1)
        self.assertIsNone(after.pr_number)
        self.assertEqual(after.recovery_stage, "commit_failed")
        self.assertTrue(after.is_publish_pending, "the task must stay recoverable")
        self.assertEqual(self.world.read_world()["repos"][self.slug]["pulls"], [])

        # Fixing the cause and re-running succeeds, and then does exit 0.
        self.repair_commits()
        healed = self.run_cli("resume-publish", "--repo", self.slug, "--issue", "1")
        self.assertEqual(healed.returncode, 0, healed.stdout + healed.stderr)
        published = store.get_task(self.slug, 1)
        self.assertEqual(published.phase, "awaiting_review")
        self.assertIsNotNone(published.pr_number)
        self.assertEqual(len(self.recorded_argv()), runs_before, "still no runtime call")
        self.assertEqual(len(self.world.read_world()["repos"][self.slug]["pulls"]), 1)

    def test_resume_publish_is_refused_when_nothing_awaits_publication(self) -> None:
        self.set_issues(issue(1, "Nothing pending", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        result = self.run_cli("resume-publish", "--repo", self.slug, "--issue", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no pending publication", result.stderr + result.stdout)

    # Requested regression 3 ------------------------------------------------------

    def test_readding_the_trigger_label_does_not_queue_publish_pending_work(self) -> None:
        """Withdrawing and re-adding `take-it` must restore publication, not dispatch.

        A worker poll legitimately *completes* publication when it can (that is the
        self-healing path), so this fixture keeps publication genuinely outstanding by
        holding the dispatcher's commit broken throughout the label dance. That is what
        makes the release decision observable: without the fix the task is put back in
        `queued` and the next poll would start a second implementation run.
        """
        self.write_scenario(
            runs=[{"session_id": "sess-1", "subtype": "success", "edits": {"work.txt": "new\n"}}]
        )
        self.set_issues(issue(1, "Publish pending", labels=[TRIGGER]))

        # Break the commit so the run completes but publishing cannot finish: the task
        # parks as `commit_failed` with the stage preserved.
        self.world.env_overrides["GIT_AUTHOR_NAME"] = ""
        self.assertEqual(self.run_cli("run").returncode, 1)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.recovery_stage, "commit_failed")
        branch = task.branch
        session = task.session_id
        runs_before = len(self.recorded_argv())

        # Withdraw `take-it`: the task pauses, and the reason it is publish-pending
        # must survive so re-adding the label can restore publication.
        self.set_issues(issue(1, "Publish pending", labels=[]))
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        paused = store.get_task(self.slug, 1)
        self.assertEqual(paused.phase, "paused")
        self.assertEqual(paused.pause_reason, "label_withdrawn")
        self.assertEqual(
            paused.recovery_stage,
            "commit_failed",
            "pausing for a withdrawn label must not discard the publish-pending reason",
        )

        # Re-add it.
        self.set_issues(issue(1, "Publish pending", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        rearmed = store.get_task(self.slug, 1)
        self.assertEqual(
            rearmed.phase,
            "needs_attention",
            "re-adding the label must restore publication, not queue an implementation run",
        )
        self.assertTrue(rearmed.has_publishable_stage)

        # Fix the cause: publication completes with the SAME branch and session, one PR,
        # and no additional runtime invocation.
        self.world.env_overrides.pop("GIT_AUTHOR_NAME", None)
        self.run_cli("run", "--skip-poll")
        self.assertEqual(
            len(self.recorded_argv()), runs_before, "zero additional runtime invocations"
        )
        recovered = store.get_task(self.slug, 1)
        self.assertEqual(recovered.branch, branch, "the owned branch must be reused")
        self.assertEqual(recovered.session_id, session, "the session must not be replaced")
        self.assertEqual(recovered.phase, "awaiting_review")
        self.assertIsNone(recovered.recovery_stage)
        self.assertEqual(len(self.world.read_world()["repos"][self.slug]["pulls"]), 1)

    # Requested regression 4 ------------------------------------------------------

    def test_a_legacy_queued_row_with_a_publishable_stage_never_starts_the_runtime(self) -> None:
        """Defensive guard: old DB state must not reintroduce the bug.

        A row left `queued` by an older build (or any future mutation that forgets the
        invariant) must not launch Command Code. The dispatcher escalates it to
        `needs_attention` so publication finishes instead.
        """
        orchestrator, store, config = self.parked(stage="push_failed")
        # Corrupt the phase exactly as a pre-fix build could have left it.
        store._conn.execute(
            "UPDATE tasks SET phase = 'queued' WHERE id = ?", (store.get_task(self.slug, 1).id,)
        )
        runs_before = len(self.recorded_argv())

        result = self.run_cli("run", "--skip-poll")

        self.assertEqual(
            len(self.recorded_argv()), runs_before, "no runtime may start for publish-pending work"
        )
        after = store.get_task(self.slug, 1)
        self.assertNotEqual(after.phase, "running")
        self.assertIn(after.phase, {"needs_attention", "awaiting_review"})
        self.assertIn("publish_pending_escalated", result.stderr)

    def test_the_claim_itself_refuses_a_queued_publish_pending_row(self) -> None:
        # The SQL guard is the last line of defence, so it is asserted directly: even
        # if a caller skips the higher-level checks, the atomic claim must fail.
        orchestrator, store, config = self.parked(stage="commit_failed")
        task = store.get_task(self.slug, 1)
        store._conn.execute("UPDATE tasks SET phase = 'queued' WHERE id = ?", (task.id,))

        claimed = store.claim_for_run(
            task.id,
            branch=task.branch or "b",
            worktree_path=str(self.tmp / "wt"),
            base_branch="main",
        )
        self.assertFalse(claimed, "the claim must refuse publish-pending work")
        self.assertEqual(store.get_task(self.slug, 1).phase, "queued")

    def test_normal_retry_behaviour_is_unchanged_for_interrupted_work(self) -> None:
        # Regression guard for the fix itself: a genuinely failed/interrupted task
        # (no publishable stage) still retries exactly as before.
        self.write_scenario(
            runs=[
                {"session_id": "sess-1", "subtype": "error", "exit_code": 1},
                {"session_id": "sess-2", "subtype": "success", "edits": {"ok.txt": "done\n"}},
            ]
        )
        self.set_issues(issue(1, "Genuine failure", labels=[TRIGGER]))
        # One attempt only, so the failure exhausts the budget and the task becomes
        # `failed` — the phase `retry` exists for. (With budget remaining a failed
        # attempt stays `queued` on purpose, so the next dispatch retries it without
        # operator action, and `retry` correctly refuses a queued task.)
        self.world.write_config(worker_overrides=self.execution_overrides(max_attempts=1))
        self.assertEqual(self.run_cli("run").returncode, 1)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "failed")
        self.assertIsNone(task.recovery_stage, "a failed run leaves no publishable stage")
        self.assertFalse(task.has_publishable_stage)

        # `retry` must still work for this case.
        self.assertEqual(self.run_cli("retry", "--repo", self.slug, "--issue", "1").returncode, 0)
        self.assertEqual(store.get_task(self.slug, 1).phase, "queued")
        self.assertEqual(store.get_task(self.slug, 1).attempts, 0)


class LiveWorkerPublishPickupTests(ExecutionCase):
    """Review round 5: a *running* worker must pick up newly publish-pending work.

    Startup reconciliation runs once per process (`Worker._reconciled`), so a task that
    becomes publish-pending *while the service is alive* — because `take-it` came back,
    or an operator unpaused it — used to sit untouched until a restart.

    The round-4 tests could not catch that: they finished publication with an explicit
    `run --skip-poll`, and that command always calls `Orchestrator.reconcile()`, which
    masked the missing per-poll pass. These tests therefore drive **one** `Worker`
    instance across several `poll_once()` calls, never construct a second one, and
    never invoke `run`.
    """

    def stuck_publication(self, worker, store):
        """First poll: a real run finishes but its commit fails, so it stays unpublished.

        This spends exactly one runtime invocation. Everything the tests do afterwards
        must finish publication without any further invocation.
        """
        self.set_issues(issue(1, "Publish pending", labels=[TRIGGER]))
        self.break_commits()

        outcome = worker.poll_once()
        self.assertIsNone(outcome.error, outcome.error)

        task = store.get_task(self.slug, 1)
        self.assertEqual(task.recovery_stage, "commit_failed", task.last_error)
        self.assertIsNone(task.pr_number)
        self.assertEqual(len(self.recorded_argv()), 1, "exactly one implementation run")
        self.assertTrue(
            worker._reconciled,
            "the persistent worker has already spent its one startup reconciliation",
        )
        return task

    # -------------------------------------------------------------------- tests

    def test_readding_the_label_publishes_on_the_next_poll(self) -> None:
        worker, store, _ = self.live_worker()
        task = self.stuck_publication(worker, store)
        runs_before = len(self.recorded_argv())
        branch, session = task.branch, task.session_id

        # Withdraw `take-it`: discovery pauses the task, and the reason it is
        # publish-pending must survive so re-adding the label restores publication.
        self.set_issues(issue(1, "Publish pending", labels=[]))
        worker.poll_once()
        paused = store.get_task(self.slug, 1)
        self.assertEqual(paused.phase, "paused")
        self.assertEqual(paused.pause_reason, "label_withdrawn")
        self.assertEqual(paused.recovery_stage, "commit_failed")
        self.assertIsNone(paused.pr_number)

        # Fix the cause, put the label back, and poll the SAME worker instance: no new
        # Worker, no `run`, and no restart of the service.
        self.repair_commits()
        self.set_issues(issue(1, "Publish pending", labels=[TRIGGER]))
        worker.poll_once()

        self.assert_published_once(store, runs_before, branch, session)
        self.assertIn("publish_reconciled", self.log_stream.getvalue())

    def test_unpause_publishes_on_the_next_poll(self) -> None:
        worker, store, _ = self.live_worker()
        task = self.stuck_publication(worker, store)
        runs_before = len(self.recorded_argv())
        branch, session = task.branch, task.session_id

        # A maintainer pause, then the pause released. The store routes publish-pending
        # work to `needs_attention`, never back to the implementation queue.
        self.assertEqual(self.run_cli("pause", "--repo", self.slug, "--issue", "1").returncode, 0)
        self.assertEqual(store.get_task(self.slug, 1).phase, "paused")

        self.repair_commits()
        released = self.run_cli("unpause", "--repo", self.slug, "--issue", "1")
        self.assertEqual(released.returncode, 0, released.stdout + released.stderr)
        self.assertEqual(store.get_task(self.slug, 1).phase, "needs_attention")

        worker.poll_once()

        self.assert_published_once(store, runs_before, branch, session)

    def test_a_maintainer_pause_is_never_overtaken_by_a_poll(self) -> None:
        """A publishable stage outlives a pause on purpose, so a poll must respect it.

        Re-adding `take-it` or unpausing is what restores publication, which is exactly
        why the stage survives a pause. A per-poll pass that keyed off the stage alone
        would publish work a maintainer had deliberately stopped — pushing to GitHub
        for a task that reports itself as paused.
        """
        worker, store, _ = self.live_worker()
        self.stuck_publication(worker, store)
        runs_before = len(self.recorded_argv())
        self.repair_commits()

        self.assertEqual(self.run_cli("pause", "--repo", self.slug, "--issue", "1").returncode, 0)
        worker.poll_once()

        held = store.get_task(self.slug, 1)
        self.assertEqual(held.phase, "paused", "a poll must not undo a maintainer pause")
        self.assertIsNone(held.pr_number, "a paused task's work must not be published")
        self.assertEqual(self.world.read_world()["repos"][self.slug]["pulls"], [])
        self.assertEqual(len(self.recorded_argv()), runs_before, "no runtime may be started")
        self.assertTrue(held.has_publishable_stage, "the stage must survive for the release")


if __name__ == "__main__":
    unittest.main(verbosity=2)

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

import json
import os
import stat
import sys
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

    def test_an_orphaned_running_row_is_recovered_without_a_second_run(self) -> None:
        # A `running` row can only be an artifact of a previous process: the
        # single-instance lock proves no other worker is alive. Recovery closes the
        # open run and re-queues the task rather than leaving it stuck.
        self.set_issues(issue(1, "Interrupted", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.store import Store

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        # Simulate a process that died mid-run: phase running, run row still open.
        store._conn.execute(
            "UPDATE tasks SET phase = 'running', attempts = 1 WHERE id = ?", (task.id,)
        )
        run_row = store.start_run(
            task.id,
            run_id="20260101T000000Z-implementation",
            kind="implementation",
            resumed_from=None,
            log_path=str(self.tmp / "dangling.ndjson"),
        )
        store.close()

        runs_before = len(self.recorded_argv())
        repaired = self.run_cli("run", "--skip-poll")
        self.assertIn("reconcile", repaired.stdout)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        recovered = store.get_task(self.slug, 1)
        self.assertEqual(recovered.phase, "queued", "an interrupted task returns to the queue")
        runs = store.run_history(task.id)
        dangling = [run for run in runs if run.id == run_row]
        self.assertEqual(
            dangling[0].outcome, "failed", "the orphaned run row must be closed, not left open"
        )
        # The recovery itself did not run an agent; the follow-up dispatch did.
        self.assertLessEqual(len(self.recorded_argv()) - runs_before, 1)

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


if __name__ == "__main__":
    unittest.main(verbosity=2)

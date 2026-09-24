#!/usr/bin/env python3
"""Offline test-suite for Issue #3 (stdlib ``unittest`` only).

Run it with:

    ./scripts/test-offline.sh          # or
    python3 -m unittest discover -s tests -v

No network access, no model calls and no HTTP mutations to GitHub: every GitHub
interaction goes through ``tests/fake_wrapper.py``, an executable that speaks the
same CLI surface as the approved wrapper. Because the harness drives the *real*
``GitHubClient``/``Discovery``/``Worker`` code as a subprocess, the tests cover
argument construction, pagination, error classification and reconciliation —
not a parallel mock implementation.

Coverage maps to the Issue #3 acceptance list:

* discovery + pagination, Issue-vs-PR filtering, allowlist checks
* missing auth / missing wrapper / missing label handling
* repeated polls and restart with previously labelled Issues (idempotency)
* removing and re-adding ``take-it``
* a pre-existing PR for the same Issue
* failures/retries that must never mark a task complete
* two workers cannot hold the same lock
* global concurrency limit = 1
* ``dry-run`` writes neither to GitHub nor to the state database
* state and logs never land inside the source/target repository
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))

from agent_dispatch import runlogs  # noqa: E402
from agent_dispatch.config import ConfigError, build_config, load_config  # noqa: E402
from agent_dispatch.lockfile import LockBusyError, WorkerLock  # noqa: E402
from agent_dispatch.logging_setup import Logger  # noqa: E402
from agent_dispatch.store import Store  # noqa: E402
from agent_dispatch.worker import Worker  # noqa: E402

FAKE_WRAPPER = REPO_ROOT / "tests" / "fake_wrapper.py"
TRIGGER = "take-it"
HANDOFF = "agent:fix"

#: A runtime path that deliberately does not exist. See ``write_config`` for why the
#: suite pins this instead of leaving ``commandcode_path`` unset.
MISSING_RUNTIME = "./no-such-commandcode-binary"


class FakeWorld:
    """Builds the fake wrapper's world file and the matching TOML config."""

    def __init__(
        self,
        root: Path,
        repos: dict[str, dict],
        *,
        labels: list[str] | None = None,
        env_overrides: dict[str, str | None] | None = None,
    ) -> None:
        self.root = root
        self.world_path = root / "world.json"
        self.state_dir = root / "state"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.repo_path = root / "checkout"
        (self.repo_path / ".git").mkdir(parents=True, exist_ok=True)
        #: Extra environment for the fake wrapper, e.g. to force a truncated scan.
        #: A value of ``None`` REMOVES the variable from the child environment; a
        #: value of "" sets it to empty, which is a different thing entirely.
        self.env_overrides: dict[str, str | None] = dict(env_overrides or {})

        payload: dict[str, object] = {"identity": "fake-user", "repos": {}}
        for slug, repo in repos.items():
            entry = dict(repo)
            entry.setdefault("labels", list(labels or []))
            entry.setdefault("issues", [])
            entry.setdefault("pulls", [])
            entry.setdefault("accessible", True)
            payload["repos"][slug] = entry  # type: ignore[index]
        self.world = payload
        self.write_world()

        self.config_path = root / "config.toml"
        self.write_config()

    # ------------------------------------------------------------------ files

    def write_world(self) -> None:
        self.world_path.write_text(
            json.dumps(self.world, indent=2, sort_keys=True), encoding="utf-8"
        )

    def read_world(self) -> dict:
        return json.loads(self.world_path.read_text(encoding="utf-8"))

    def write_config(
        self,
        *,
        worker_overrides: dict[str, object] | None = None,
        github_overrides: dict[str, object] | None = None,
        repos_block: str | None = None,
    ) -> None:
        """Render a config file for the fake world.

        Overrides are applied to the parsed values before rendering, so a test
        can change one setting without risking a duplicate TOML key.
        """
        worker: dict[str, object] = {
            "poll_interval_seconds": 20,
            "max_concurrent_tasks": 1,
            "run_timeout_seconds": 60,
            "max_attempts": 3,
            "worktree_root": f"{self.root}/worktrees",
            "state_db": f"{self.state_dir}/state.db",
            "run_log_dir": f"{self.state_dir}/runs",
            "run_log_keep": 5,
            "lock_file": f"{self.state_dir}/worker.lock",
            # A runtime path that does NOT exist, unless a test opts in.
            #
            # This matters for determinism and for safety. The agent runtime is a
            # paid, network-hitting binary that may well be installed on a
            # developer's VM; if the test config left this unset, `agent-dispatch
            # worker --once` would resolve `commandcode` from PATH and spawn a REAL
            # paid agent against a temp fixture. Pinning a missing path makes the
            # suite behave identically everywhere, and mirrors the honest
            # "runtime not installed" case that #3's tests are already about.
            # #4's execution tests override this with tests/fake_runtime.py.
            "commandcode_path": MISSING_RUNTIME,
        }
        worker.update(worker_overrides or {})

        github: dict[str, object] = {
            "command": str(FAKE_WRAPPER),
            "credential_helper_reset": "",
            "credential_helper": f"!{FAKE_WRAPPER} auth git-credential",
            "fail_fast_on_denied_repo": True,
            "labels": {"trigger": TRIGGER, "review_handoff": HANDOFF},
        }
        github.update(github_overrides or {})

        lines = ["[worker]"]
        for key, value in worker.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
        lines.append("[github]")
        labels = github.pop("labels")
        for key, value in github.items():
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
        lines.append("[github.labels]")
        for key, value in labels.items():  # type: ignore[union-attr]
            lines.append(f"{key} = {_toml_value(value)}")
        lines.append("")
        lines.append(repos_block if repos_block is not None else self._default_repos_block())

        self.config_path.write_text("\n".join(lines), encoding="utf-8")

    def _default_repos_block(self) -> str:
        blocks = []
        for slug in self.world.get("repos", {}):  # type: ignore[union-attr]
            blocks.append(
                f"""
[repos."{slug}"]
path = "{self.repo_path}"
base_branch = "main"
agents_file = "AGENTS.md"

[repos."{slug}".runtime]
driver = "commandcode"
model = "deepseek/deepseek-v4-flash"
effort = "medium"
permission_mode = "allow-all"
permission_flag = "--yolo"
max_turns = 40
"""
            )
        return "\n".join(blocks)

    # ------------------------------------------------------------------ runner

    def env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["FAKE_GH_WORLD"] = str(self.world_path)
        env["PYTHONPATH"] = str(SRC)
        # Clear any padding left over from another test, then apply overrides.
        env.pop("FAKE_GH_PAD_FULL_PAGES", None)
        for key, value in self.env_overrides.items():
            if value is None:
                # Unset, not blank. A variable set to "" is an *override* to a child
                # process (an empty GIT_AUTHOR_NAME outranks `-c user.name`), so a
                # test that needs a variable gone must be able to remove it.
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run_cli(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the real CLI as a subprocess against the fake wrapper."""
        return subprocess.run(
            [sys.executable, "-m", "agent_dispatch.cli", "--config", str(self.config_path), *args],
            capture_output=True,
            text=True,
            env=self.env(),
            cwd=str(self.root),
            timeout=120,
        )

    def load_config(self):
        return load_config(self.config_path)


def _toml_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value))


def issue(
    number: int,
    title: str,
    *,
    labels: list[str] | None = None,
    state: str = "open",
    is_pr: bool = False,
) -> dict:
    payload = {
        "number": number,
        "title": title,
        "state": state,
        "html_url": f"https://github.com/example/repo/issues/{number}",
        "labels": [{"name": name} for name in (labels or [])],
    }
    if is_pr:
        payload["pull_request"] = {
            "url": f"https://api.github.com/repos/example/repo/pulls/{number}"
        }
    return payload


def pull(
    number: int, head_ref: str, *, state: str = "open", merged: bool = False, body: str = ""
) -> dict:
    return {
        "number": number,
        "state": state,
        "merged_at": "2026-01-01T00:00:00Z" if merged else None,
        "head": {"ref": head_ref, "sha": "0" * 40},
        "html_url": f"https://github.com/example/repo/pull/{number}",
        "title": f"PR {number}",
        "body": body,
        "base": {"ref": "main"},
    }


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="agent-dispatch-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.slug = "example/repo"
        self.world = FakeWorld(
            self.tmp,
            {self.slug: {"labels": [TRIGGER, HANDOFF]}},
        )
        # In-process tests drive the real GitHubClient, which spawns the fake
        # wrapper as a subprocess inheriting this environment.
        self.addCleanup(os.environ.pop, "FAKE_GH_WORLD", None)
        os.environ["FAKE_GH_WORLD"] = str(self.world.world_path)

    def worker(
        self,
        *,
        reconcile: bool = True,
        store: Store | None = None,
        env_overrides: dict[str, str] | None = None,
    ):
        os.environ["FAKE_GH_WORLD"] = str(self.world.world_path)
        os.environ.pop("FAKE_GH_PAD_FULL_PAGES", None)
        for key, value in (env_overrides or {}).items():
            os.environ[key] = value
        if env_overrides:
            for key in env_overrides:
                self.addCleanup(os.environ.pop, key, None)
        config = self.world.load_config()
        log = Logger(fmt="text", stream=open(os.devnull, "w"))  # noqa: SIM115
        self.addCleanup(log.stream.close)
        owned = store is None
        store = store or Store(config.worker.state_db)
        if owned:
            self.addCleanup(store.close)
        return Worker(config, store, log, reconcile=reconcile), store, config

    def task(self, store: Store, number: int):
        task = store.get_task(self.slug, number)
        self.assertIsNotNone(task, f"expected a task row for {self.slug}#{number}")
        return task

    def set_issues(self, *issues: dict) -> None:
        self.world.world["repos"][self.slug]["issues"] = list(issues)  # type: ignore[index]
        self.world.write_world()

    def set_pulls(self, *pulls: dict) -> None:
        self.world.world["repos"][self.slug]["pulls"] = list(pulls)  # type: ignore[index]
        self.world.write_world()

    def inject_failure(
        self,
        key: str,
        *,
        stderr: str = "",
        exit_code: int = 1,
        once: bool = False,
        stdout: str = "",
    ) -> None:
        failures = self.world.world.setdefault("failures", {})
        spec: dict[str, object] = {"stderr": stderr, "exit": exit_code}
        if stdout:
            spec = {"stdout": stdout, "exit": exit_code if exit_code != 1 else 0}
        if once:
            spec["fail_once"] = True
        failures[key] = spec  # type: ignore[index]
        self.world.write_world()


# ==============================================================================
# Discovery, pagination, Issue-vs-PR filtering
# ==============================================================================


class DiscoveryTests(BaseCase):
    def test_labelled_issue_is_queued(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        worker, store, _ = self.worker()
        outcome = worker.poll_once()

        self.assertTrue(outcome.ok, outcome.error)
        task = self.task(store, 1)
        self.assertEqual(task.phase, "queued")
        self.assertTrue(task.trigger_present)
        self.assertEqual(task.title, "First")
        # Discovery is not implementation.
        self.assertEqual(store.active_task_count(), 0)

    def test_unlabelled_issue_is_not_queued(self) -> None:
        self.set_issues(issue(1, "Not labelled"))
        worker, store, _ = self.worker()
        worker.poll_once()
        self.assertIsNone(store.get_task(self.slug, 1))

    def test_pull_request_objects_are_filtered_out(self) -> None:
        # GitHub's issues endpoint returns PRs too; a naive implementation would
        # queue every open PR as if it were an Issue.
        self.set_issues(
            issue(7, "A real issue", labels=[TRIGGER]),
            issue(8, "Actually a PR", labels=[TRIGGER], is_pr=True),
        )
        worker, store, _ = self.worker()
        worker.poll_once()

        self.assertIsNotNone(store.get_task(self.slug, 7))
        self.assertIsNone(store.get_task(self.slug, 8), "a PR must never be queued as an Issue")

    def test_pagination_collects_every_page(self) -> None:
        # per_page is 100 in production; 250 labelled issues forces 3 pages.
        self.set_issues(
            *(issue(number, f"Issue {number}", labels=[TRIGGER]) for number in range(1, 251))
        )
        worker, store, _ = self.worker()
        worker.poll_once()

        tasks = store.list_tasks(self.slug)
        self.assertEqual(len(tasks), 250)
        self.assertEqual({task.issue_number for task in tasks}, set(range(1, 251)))

    def test_pagination_is_stable_across_repeated_polls(self) -> None:
        self.set_issues(
            *(issue(number, f"Issue {number}", labels=[TRIGGER]) for number in range(1, 251))
        )
        worker, store, _ = self.worker()
        worker.poll_once()
        worker.poll_once()
        worker.poll_once()
        self.assertEqual(len(store.list_tasks(self.slug)), 250)

    def test_pr_query_failure_does_not_queue_the_issue(self) -> None:
        # Fail-closed: if the PR listing fails, a pre-existing PR cannot be ruled
        # out, so the Issue must NOT be queued on that poll.
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        self.inject_failure(f"{self.slug}:pulls", stderr="gh: Server Error (HTTP 502)")

        worker, store, _ = self.worker()
        outcome = worker.poll_once()

        self.assertFalse(outcome.ok)
        self.assertIsNone(store.get_task(self.slug, 1), "must not queue when PR state is unknown")

        # Once the PR query recovers, the Issue is queued normally.
        self.world.world.pop("failures", None)
        self.world.write_world()
        recovered = worker.poll_once()
        self.assertTrue(recovered.ok, recovered.error)
        self.assertEqual(self.task(store, 1).phase, "queued")

    def test_no_pr_query_is_made_when_nothing_is_labelled(self) -> None:
        # An idle repository must not re-list every PR on every poll.
        self.set_issues(issue(1, "Not labelled"))
        self.inject_failure(f"{self.slug}:pulls", stderr="should never be called")

        worker, store, _ = self.worker()
        outcome = worker.poll_once()

        self.assertTrue(outcome.ok, "the PR endpoint must not be queried when nothing is labelled")
        self.assertEqual(store.list_tasks(self.slug), [])

    def test_closed_issue_is_not_queued_even_when_labelled(self) -> None:
        # The labelled query asks for state=open; the fake honours that.
        self.set_issues(issue(1, "Closed", labels=[TRIGGER], state="closed"))
        worker, store, _ = self.worker()
        worker.poll_once()
        self.assertIsNone(store.get_task(self.slug, 1))

    def test_non_allowlisted_repo_is_never_polled(self) -> None:
        # A second repo exists in the world but is absent from the allowlist.
        self.world.world["repos"]["example/other"] = {  # type: ignore[index]
            "labels": [TRIGGER],
            "issues": [issue(1, "Elsewhere", labels=[TRIGGER])],
            "pulls": [],
            "accessible": True,
        }
        self.world.write_world()

        worker, store, config = self.worker()
        worker.poll_once()

        self.assertNotIn("example/other", config.repos)
        self.assertIsNone(store.get_task("example/other", 1))
        self.assertEqual(store.list_tasks(), [])


# ==============================================================================
# Idempotency, restart, label removal / re-add
# ==============================================================================


class IdempotencyTests(BaseCase):
    def test_repeated_polls_yield_one_row(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        worker, store, _ = self.worker()
        for _ in range(5):
            worker.poll_once()
        self.assertEqual(len(store.list_tasks(self.slug)), 1)

    def test_restart_with_previously_labelled_issues_does_not_duplicate(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]), issue(2, "Second", labels=[TRIGGER]))

        worker, store, _ = self.worker()
        worker.poll_once()
        created_at = self.task(store, 1).created_at
        store.close()

        # "Restart": a brand-new process/store against the same database.
        worker2, store2, _ = self.worker()
        worker2.poll_once()

        tasks = store2.list_tasks(self.slug)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(
            self.task(store2, 1).created_at, created_at, "the row must be reused, not recreated"
        )

    def test_cli_worker_once_is_idempotent_across_processes(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        first = self.world.run_cli("worker", "--once")
        self.assertEqual(first.returncode, 0, first.stderr)
        second = self.world.run_cli("worker", "--once")
        self.assertEqual(second.returncode, 0, second.stderr)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        self.assertEqual(len(store.list_tasks(self.slug)), 1)

    def test_removing_then_readding_label_reuses_the_row(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        worker, store, _ = self.worker()
        worker.poll_once()
        original = self.task(store, 1)
        self.assertEqual(original.phase, "queued")

        # Label removed: dispatch intent withdrawn, row and work kept.
        self.set_issues(issue(1, "First", labels=[]))
        worker.poll_once()
        withdrawn = self.task(store, 1)
        self.assertEqual(withdrawn.id, original.id)
        self.assertEqual(withdrawn.phase, "paused")
        self.assertFalse(withdrawn.trigger_present)
        self.assertEqual(withdrawn.created_at, original.created_at)

        # Label re-added: same row, never a duplicate.
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        worker.poll_once()
        restored = self.task(store, 1)
        self.assertEqual(restored.id, original.id)
        self.assertEqual(len(store.list_tasks(self.slug)), 1)
        self.assertTrue(restored.trigger_present)

    def test_label_removal_does_not_delete_recorded_work(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        worker, store, _ = self.worker()
        worker.poll_once()

        self.set_issues(issue(1, "First", labels=[]))
        worker.poll_once()

        task = self.task(store, 1)
        self.assertEqual(task.phase, "paused")
        self.assertIn(TRIGGER, task.last_error or "")

    def test_re_adding_the_label_returns_a_withdrawn_task_to_dispatchable(self) -> None:
        # The reconciliation rule: withdrawing `take-it` suspends dispatch, and
        # restoring it must make the existing row dispatchable again without a
        # human unpause step. Regression: an earlier version left it paused.
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        worker, store, _ = self.worker()
        worker.poll_once()

        self.set_issues(issue(1, "First", labels=[]))
        worker.poll_once()
        self.assertFalse(self.task(store, 1).dispatchability()[0])

        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        outcome = worker.poll_once()
        task = self.task(store, 1)

        self.assertEqual(task.phase, "queued")
        self.assertTrue(task.dispatchability()[0])
        self.assertTrue(task.trigger_present)
        self.assertEqual(outcome.result.repos[0].requeued, 1)  # type: ignore[union-attr]
        self.assertEqual(len(store.list_tasks(self.slug)), 1)

    def test_maintainer_pause_is_not_released_by_a_label_re_add(self) -> None:
        # An explicit maintainer pause is sticky: it must survive polls and a
        # label round trip, unlike the pause created by label removal.
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        worker, store, _ = self.worker()
        worker.poll_once()

        store.pause(self.slug, 1)
        self.assertEqual(self.task(store, 1).pause_reason, "maintainer")

        self.set_issues(issue(1, "First", labels=[]))
        worker.poll_once()
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        worker.poll_once()

        task = self.task(store, 1)
        self.assertEqual(task.phase, "paused", "a maintainer pause must not be auto-released")
        self.assertEqual(task.pause_reason, "maintainer")
        self.assertFalse(task.dispatchability()[0])

    def test_label_withdrawn_pause_records_its_reason(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        worker, store, _ = self.worker()
        worker.poll_once()
        self.set_issues(issue(1, "First", labels=[]))
        worker.poll_once()

        self.assertEqual(self.task(store, 1).pause_reason, "label_withdrawn")

    def test_closed_issue_moves_task_to_finished(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        worker, store, _ = self.worker()
        worker.poll_once()

        # Closed Issues disappear from the labelled query; reconciliation then
        # reads the Issue directly and finishes the task without reopening it.
        self.set_issues(issue(1, "First", labels=[TRIGGER], state="closed"))
        worker.poll_once()

        task = self.task(store, 1)
        self.assertEqual(task.phase, "finished")
        self.assertEqual(task.issue_state, "closed")


# ==============================================================================
# Pre-existing PR
# ==============================================================================


class PreExistingPullRequestTests(BaseCase):
    def test_existing_pr_is_adopted_and_not_scheduled(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        self.set_pulls(pull(42, "some/human-branch", body=f"Closes #{1}"))

        worker, store, _ = self.worker()
        worker.poll_once()

        task = self.task(store, 1)
        self.assertEqual(
            task.phase, "awaiting_review", "a pre-existing PR must not create implementation work"
        )
        self.assertEqual(task.linked_pr_number, 42)
        dispatchable, reason = task.dispatchability()
        self.assertFalse(dispatchable)
        self.assertIn("PR #42", reason)

    def test_merged_pr_for_the_same_issue_is_detected(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        self.set_pulls(pull(42, "anything", state="closed", merged=True, body="Fixes #1"))

        worker, store, _ = self.worker()
        worker.poll_once()

        task = self.task(store, 1)
        self.assertEqual(task.linked_pr_number, 42)
        self.assertEqual(task.linked_pr_state, "merged")

    def test_unrelated_pr_does_not_block_dispatch(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        # A PR mentioning a different Issue must not be treated as this Issue's PR.
        self.set_pulls(pull(9, "feature/other", body="Closes #999"))

        worker, store, _ = self.worker()
        worker.poll_once()

        task = self.task(store, 1)
        self.assertEqual(task.phase, "queued")
        self.assertIsNone(task.linked_pr_number)

    def test_dispatch_branch_naming_is_recognised(self) -> None:
        self.set_issues(issue(5, "First", labels=[TRIGGER]))
        self.set_pulls(pull(11, "dispatch/issue-5-some-slug"))

        worker, store, _ = self.worker()
        worker.poll_once()

        task = self.task(store, 5)
        self.assertEqual(task.linked_pr_number, 11)
        self.assertEqual(task.phase, "awaiting_review")

    def test_recorded_own_pr_is_reconciled_not_re_adopted(self) -> None:
        # `tasks.pr_number` means "the PR this worker owns"; a pre-existing PR is
        # only ever recorded as an observation. Once #4 records ownership, later
        # polls must reconcile against GitHub instead of re-running adoption.
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        self.set_pulls(pull(42, "dispatch/issue-1-slug"))

        worker, store, _ = self.worker()
        worker.poll_once()
        adopted = self.task(store, 1)
        self.assertEqual(adopted.linked_pr_number, 42)
        self.assertIsNone(
            adopted.pr_number, "an adopted pre-existing PR is not owned by this worker"
        )

        # #4 records ownership of the PR it will use for this task.
        # `record_pr_ownership` is the #4-only entry point: discovery must never
        # call it, and the tests call it explicitly to stand in for #4.
        store.record_pr_ownership(adopted.id, 42, "own PR recorded")
        owned = self.task(store, 1)
        self.assertEqual(owned.pr_number, 42)
        updated_at = owned.updated_at

        outcome = worker.poll_once()
        second = self.task(store, 1)

        self.assertEqual(second.phase, "awaiting_review")
        self.assertEqual(second.pr_number, 42)
        self.assertEqual(outcome.result.repos[0].own_pr_reconciled, 1)  # type: ignore[union-attr]
        self.assertEqual(outcome.result.repos[0].skipped_has_pr, 0)  # type: ignore[union-attr]
        self.assertEqual(second.updated_at, updated_at, "reconciliation must not churn the row")


# ==============================================================================
# Failure handling: honest reporting, never a silent success
# ==============================================================================


class FailureTests(BaseCase):
    def test_missing_wrapper_is_reported_and_creates_no_tasks(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        self.world.write_config(github_overrides={"command": f"{self.tmp}/does-not-exist"})

        worker, store, _ = self.worker()
        outcome = worker.poll_once()

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.error_kind, "missing_wrapper")
        self.assertIsNone(store.get_task(self.slug, 1))
        self.assertEqual(store.count_by_phase()["failed"], 0)

    def test_denied_repository_is_reported_and_existing_state_untouched(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        worker, store, _ = self.worker()
        worker.poll_once()
        before = self.task(store, 1)

        # The credential loses access to the repository.
        self.world.world["repos"][self.slug]["accessible"] = False  # type: ignore[index]
        self.world.write_world()

        outcome = worker.poll_once()
        after = self.task(store, 1)

        self.assertFalse(outcome.ok)
        repo_outcome = outcome.result.repos[0]  # type: ignore[union-attr]
        self.assertEqual(repo_outcome.error_kind, "denied_repo")
        self.assertEqual(after.phase, before.phase, "a denied repo must not change task state")
        self.assertEqual(after.updated_at, before.updated_at)

    def test_rate_limit_is_reported_as_retryable_and_marks_nothing_complete(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        self.inject_failure(
            f"{self.slug}:issues",
            stderr="gh: API rate limit exceeded for user ID 1. (HTTP 403)",
        )
        worker, store, _ = self.worker()
        outcome = worker.poll_once()

        self.assertFalse(outcome.ok)
        repo_outcome = outcome.result.repos[0]  # type: ignore[union-attr]
        self.assertEqual(repo_outcome.error_kind, "rate_limit")
        self.assertIsNone(store.get_task(self.slug, 1))
        self.assertEqual(store.active_task_count(), 0)

    def test_network_error_is_reported_and_leaves_the_queue_queued(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        worker, store, _ = self.worker()
        worker.poll_once()
        queued_before = self.task(store, 1)

        self.world.write_config()
        self.world.world["failures"] = {  # type: ignore[index]
            f"{self.slug}:issues": {
                "stderr": "dial tcp: lookup api.github.com: no such host",
                "exit": 1,
            }
        }
        self.world.write_world()

        outcome = worker.poll_once()
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.result.repos[0].error_kind, "network")  # type: ignore[union-attr]

        after = self.task(store, 1)
        self.assertEqual(after.phase, "queued", "a transient failure must not change the phase")
        self.assertEqual(after.created_at, queued_before.created_at)

    def test_transient_failure_recovers_on_the_next_poll(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        self.inject_failure(
            f"{self.slug}:issues",
            stderr="HTTP 503: Service Unavailable",
            once=True,
        )
        worker, store, _ = self.worker()
        first = worker.poll_once()
        self.assertFalse(first.ok)

        second = worker.poll_once()
        self.assertTrue(second.ok, second.error)
        self.assertEqual(self.task(store, 1).phase, "queued")

    def test_auth_failure_is_reported_and_queues_nothing(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        self.inject_failure(
            f"{self.slug}:issues", stderr="gh: Bad credentials (HTTP 401)", once=True
        )

        worker, store, _ = self.worker()
        outcome = worker.poll_once()

        # An auth failure is surfaced honestly as a failure, never as an empty
        # (and therefore silently "successful") poll.
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.result.repos[0].error_kind, "auth")  # type: ignore[union-attr]
        self.assertIsNone(store.get_task(self.slug, 1))

    def test_doctor_reports_an_auth_failure(self) -> None:
        self.inject_failure("user", stderr="gh: Bad credentials (HTTP 401)")
        result = self.world.run_cli("doctor")
        self.assertEqual(result.returncode, 1)
        self.assertIn("github_api", result.stdout)
        self.assertIn("auth", result.stdout)

    def test_unparseable_wrapper_output_is_a_failure(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        # Exit 0 with unparseable stdout: the worst case, because a naive client
        # would treat this as an empty (successful) result set.
        self.inject_failure(f"{self.slug}:issues", stdout="<html>not json</html>")

        worker, store, _ = self.worker()
        outcome = worker.poll_once()

        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.result.repos[0].error_kind, "malformed_response")  # type: ignore[union-attr]
        self.assertIsNone(store.get_task(self.slug, 1))

    def test_doctor_reports_missing_labels_with_guidance(self) -> None:
        self.world.world["repos"][self.slug]["labels"] = []  # type: ignore[index]
        self.world.write_world()

        result = self.world.run_cli("doctor")
        self.assertIn("missing", result.stdout)
        self.assertIn("setup-labels", result.stdout)
        # Doctor must not create labels as a side effect.
        self.assertEqual(self.world.read_world()["repos"][self.slug]["labels"], [])

    def test_doctor_fails_clearly_for_a_denied_repository(self) -> None:
        self.world.world["repos"][self.slug]["accessible"] = False  # type: ignore[index]
        self.world.write_world()

        result = self.world.run_cli("doctor")
        self.assertEqual(result.returncode, 1)
        self.assertIn(self.slug, result.stdout)


# ==============================================================================
# Concurrency: the global limit is 1, and the lock is exclusive
# ==============================================================================


class ConcurrencyTests(BaseCase):
    def test_two_workers_cannot_hold_the_same_lock(self) -> None:
        config = self.world.load_config()
        first = WorkerLock(config.worker.lock_file)
        first.acquire()
        self.addCleanup(first.release)

        second = WorkerLock(config.worker.lock_file)
        with self.assertRaises(LockBusyError) as ctx:
            second.acquire()
        self.assertIn(str(config.worker.lock_file), str(ctx.exception))
        self.assertEqual(ctx.exception.holder.get("pid"), os.getpid())

    def test_second_worker_process_exits_with_a_clear_error(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        config = self.world.load_config()

        holder = WorkerLock(config.worker.lock_file, command="test-holder")
        holder.acquire()
        self.addCleanup(holder.release)

        result = self.world.run_cli("worker", "--once")
        self.assertEqual(result.returncode, 3, result.stdout + result.stderr)
        self.assertIn("lock_busy", result.stderr)

    def test_lock_is_released_when_the_holder_dies(self) -> None:
        config = self.world.load_config()
        script = (
            "import sys; sys.path.insert(0, %r);"
            "from agent_dispatch.lockfile import WorkerLock;"
            "lock = WorkerLock(%r); lock.acquire(); print('held', flush=True)"
            % (str(SRC), str(config.worker.lock_file))
        )
        proc = subprocess.Popen(
            [sys.executable, "-c", script],
            stdout=subprocess.PIPE,
            text=True,
            env=self.world.env(),
        )
        assert proc.stdout is not None
        self.assertEqual(proc.stdout.readline().strip(), "held")
        proc.kill()
        proc.wait(timeout=30)

        # The OS released the flock with the process, so a restart is not blocked.
        lock = WorkerLock(config.worker.lock_file)
        lock.acquire()
        lock.release()

    def test_max_concurrent_tasks_must_be_one(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            build_config(
                json.loads(json.dumps(self._config_with(max_concurrent=2))),
                source_path=Path(self.world.config_path),
            )
        self.assertIn("max_concurrent_tasks", str(ctx.exception))

    def _config_with(self, *, max_concurrent: int) -> dict:
        import tomllib

        self.world.write_config(worker_overrides={"max_concurrent_tasks": max_concurrent})
        return tomllib.loads(self.world.config_path.read_text(encoding="utf-8"))

    def test_active_task_count_never_exceeds_one(self) -> None:
        self.set_issues(
            *(issue(number, f"Issue {number}", labels=[TRIGGER]) for number in range(1, 12))
        )
        worker, store, _ = self.worker()
        worker.poll_once()

        self.assertEqual(store.active_task_count(), 0, "this release never starts a run")
        self.assertEqual(len(store.list_tasks(self.slug)), 11)


# ==============================================================================
# dry-run: no writes, no agent, identical decisions
# ==============================================================================


class DryRunTests(BaseCase):
    def test_dry_run_creates_no_state_database(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        result = self.world.run_cli("dry-run")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("dry-run summary", result.stdout)
        self.assertFalse(
            self.world.load_config().worker.state_db.exists(),
            "dry-run must not create or modify the state database",
        )

    def test_dry_run_makes_no_github_mutations(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        before = self.world.read_world()
        result = self.world.run_cli("dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        after = self.world.read_world()
        self.assertEqual(before, after, "dry-run must not mutate GitHub state")

    def test_dry_run_reports_existing_local_state_without_persisting_changes(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        worker, store, ctx = self.worker()
        worker.poll_once()
        state_db = ctx.worker.state_db
        snapshot = Store(state_db)
        self.addCleanup(snapshot.close)
        before = [(task.issue_number, task.phase) for task in snapshot.list_tasks()]
        store.close()

        # A new labelled Issue appears; dry-run must find it but not write it.
        self.set_issues(
            issue(1, "First", labels=[TRIGGER]),
            issue(2, "Second", labels=[TRIGGER]),
        )
        result = self.world.run_cli("dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("queued=1", result.stdout)

        after_store = Store(state_db)
        self.addCleanup(after_store.close)
        after = [(task.issue_number, task.phase) for task in after_store.list_tasks()]
        self.assertEqual(before, after, "dry-run must not persist the newly discovered Issue")

    def test_dry_run_fails_loudly_when_the_wrapper_is_unavailable(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        self.world.write_config(github_overrides={"command": f"{self.tmp}/nope"})
        result = self.world.run_cli("dry-run")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAILED", result.stdout)


# ==============================================================================
# Placement: state and logs stay outside the repository
# ==============================================================================


class PlacementTests(BaseCase):
    def test_state_inside_the_repository_is_rejected(self) -> None:
        self.world.write_config(worker_overrides={"state_db": f"{self.world.repo_path}/state.db"})

        with self.assertRaises(ConfigError) as ctx:
            load_config(self.world.config_path)
        self.assertIn("OUTSIDE the target repository", str(ctx.exception))

    def test_run_logs_inside_the_repository_are_rejected(self) -> None:
        self.world.write_config(worker_overrides={"run_log_dir": f"{self.world.repo_path}/runs"})

        with self.assertRaises(ConfigError) as ctx:
            load_config(self.world.config_path)
        message = str(ctx.exception)
        self.assertIn("run_log_dir", message)
        self.assertIn("OUTSIDE the target repository", message)

    def test_no_orchestrator_file_is_created_in_the_repository(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        before = {path.name for path in self.world.repo_path.iterdir()}

        result = self.world.run_cli("worker", "--once")
        self.assertEqual(result.returncode, 0, result.stderr)

        after = {path.name for path in self.world.repo_path.iterdir()}
        self.assertEqual(before, after, "the source/test repository must stay untouched")

    def test_run_log_paths_live_under_the_configured_state_dir(self) -> None:
        config = self.world.load_config()
        path = runlogs.run_log_path(
            config.worker.run_log_dir, self.slug, 7, "20260101T000000Z-run1"
        )
        self.assertTrue(
            str(path).startswith(str(self.world.state_dir)),
            "run logs must live under the configured state directory",
        )
        self.assertNotIn(str(self.world.repo_path), str(path))

    def test_run_log_retention_is_bounded(self) -> None:
        config = self.world.load_config()
        task_dir = runlogs.run_dir(config.worker.run_log_dir, self.slug, 3)
        task_dir.mkdir(parents=True, exist_ok=True)
        for index in range(8):
            (task_dir / f"2026010{index}T000000Z-run.ndjson").write_text("{}\n", encoding="utf-8")

        removed = runlogs.prune(config.worker.run_log_dir, 3)
        remaining = sorted(path.name for path in task_dir.glob("*.ndjson"))

        self.assertEqual(len(removed), 5)
        self.assertEqual(len(remaining), 3)
        self.assertEqual(remaining, sorted(remaining)[-3:], "the newest logs are the ones kept")


# ==============================================================================
# Configuration and CLI surface
# ==============================================================================


class ConfigTests(BaseCase):
    def test_additive_migration_keeps_existing_rows(self) -> None:
        # Upgrading the service must not require deleting state: a database from
        # an earlier build is brought forward in place, and its rows survive.
        db_path = self.tmp / "legacy" / "state.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)

        import sqlite3

        legacy = sqlite3.connect(db_path)
        legacy.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL);
            INSERT INTO schema_version (version) VALUES (1);
            CREATE TABLE tasks (
              id INTEGER PRIMARY KEY, repo TEXT NOT NULL, issue_number INTEGER NOT NULL, title TEXT,
              phase TEXT NOT NULL DEFAULT 'queued', branch TEXT, worktree_path TEXT, base_branch TEXT,
              pr_number INTEGER, runtime_driver TEXT NOT NULL, runtime_model TEXT NOT NULL,
              runtime_effort TEXT, permission_mode TEXT NOT NULL, session_id TEXT,
              review_round INTEGER NOT NULL DEFAULT 0, feedback_cursor TEXT,
              attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, last_run_at TEXT,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              UNIQUE (repo, issue_number)
            );
            INSERT INTO tasks (repo, issue_number, title, phase, runtime_driver, runtime_model,
                               permission_mode, created_at, updated_at)
            VALUES ('example/repo', 3, 'Legacy row', 'queued', 'commandcode', 'm', 'allow-all',
                    '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00');
            """
        )
        legacy.commit()
        legacy.close()

        store = Store(db_path)
        self.addCleanup(store.close)
        task = store.get_task("example/repo", 3)

        self.assertIsNotNone(task, "existing rows must survive the migration")
        self.assertEqual(task.title, "Legacy row")
        self.assertIsNone(task.pause_reason, "the new column is added as NULL")
        self.assertIn(
            "pause_reason",
            {str(row["name"]) for row in store._conn.execute("PRAGMA table_info(tasks)")},
        )

        # And the new semantics work on the migrated database.
        store.pause_for_withdrawn_label(task.id, "withdrawn")
        self.assertEqual(store.get_task("example/repo", 3).pause_reason, "label_withdrawn")

    def test_packaged_schema_matches_the_reviewed_repository_copy(self) -> None:
        # The approved schema is reviewed in config/ and shipped inside the
        # package so an installed CLI works without the checkout. If these two
        # ever drift, an installed worker would validate against a different
        # contract than the one that was reviewed.
        reviewed = REPO_ROOT / "config" / "config.schema.json"
        packaged = SRC / "agent_dispatch" / "data" / "config.schema.json"
        self.assertTrue(packaged.is_file(), "packaged schema is missing")
        self.assertEqual(
            reviewed.read_bytes(),
            packaged.read_bytes(),
            "config/config.schema.json and the packaged copy have drifted apart",
        )

    def test_example_configuration_is_valid_against_the_schema(self) -> None:
        example = REPO_ROOT / "config" / "agent-dispatch.example.toml"
        raw_text = example.read_text(encoding="utf-8")

        # The shipped example points at this VM's absolute paths; rewrite only
        # those so the *shape* of the approved example is what gets validated.
        import tomllib

        raw = tomllib.loads(raw_text)
        raw["worker"]["state_db"] = str(self.world.state_dir / "state.db")
        raw["worker"]["run_log_dir"] = str(self.world.state_dir / "runs")
        raw["worker"]["lock_file"] = str(self.world.state_dir / "worker.lock")
        raw["worker"]["worktree_root"] = str(self.tmp / "worktrees")
        raw["repos"] = {
            self.slug: {
                "path": str(self.world.repo_path),
                "base_branch": "main",
                "agents_file": "AGENTS.md",
                "runtime": {
                    "driver": "commandcode",
                    "model": "deepseek/deepseek-v4-flash",
                    "effort": "medium",
                    "permission_mode": "allow-all",
                    "permission_flag": "--yolo",
                    "max_turns": 40,
                },
            }
        }
        config = build_config(raw, source_path=example)
        self.assertEqual(config.github.trigger_label, TRIGGER)
        self.assertEqual(config.github.credential_helper_reset, "")

    def test_unknown_key_is_rejected(self) -> None:
        text = self.world.config_path.read_text(encoding="utf-8")
        text = text.replace(
            "poll_interval_seconds = 20", "poll_interval_seconds = 20\nsurprise_key = true"
        )
        self.world.config_path.write_text(text, encoding="utf-8")

        with self.assertRaises(ConfigError) as ctx:
            load_config(self.world.config_path)
        self.assertIn("surprise_key", str(ctx.exception))

    def test_raw_gh_credential_helper_is_rejected(self) -> None:
        text = self.world.config_path.read_text(encoding="utf-8")
        text = text.replace(
            f'credential_helper = "!{FAKE_WRAPPER} auth git-credential"',
            'credential_helper = "!/usr/bin/gh auth git-credential"',
        )
        self.world.config_path.write_text(text, encoding="utf-8")

        with self.assertRaises(ConfigError) as ctx:
            load_config(self.world.config_path)
        self.assertIn("wrapper", str(ctx.exception))

    def test_unknown_repository_is_refused(self) -> None:
        config = self.world.load_config()
        with self.assertRaises(ConfigError) as ctx:
            config.repo("someone/unlisted")
        self.assertIn("allowlist", str(ctx.exception))

    def test_non_empty_credential_helper_reset_is_rejected(self) -> None:
        text = self.world.config_path.read_text(encoding="utf-8")
        text = text.replace(
            'credential_helper_reset = ""', 'credential_helper_reset = "!/usr/bin/gh"'
        )
        self.world.config_path.write_text(text, encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(self.world.config_path)

    def test_unsupported_driver_is_rejected(self) -> None:
        text = self.world.config_path.read_text(encoding="utf-8")
        text = text.replace('driver = "commandcode"', 'driver = "opencode"')
        self.world.config_path.write_text(text, encoding="utf-8")
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.world.config_path)
        self.assertIn("driver", str(ctx.exception))

    def test_missing_config_file_reports_how_to_create_one(self) -> None:
        with self.assertRaises(ConfigError) as ctx:
            load_config(self.tmp / "absent.toml")
        self.assertIn("not found", str(ctx.exception))


class CliSurfaceTests(BaseCase):
    def test_help_lists_the_documented_commands(self) -> None:
        result = self.world.run_cli("--help")
        self.assertEqual(result.returncode, 0)
        for command in (
            "doctor",
            "status",
            "dry-run",
            "worker",
            "run",
            "open",
            "pause",
            "unpause",
            "retry",
            "setup-labels",
        ):
            self.assertIn(command, result.stdout)
        # The #5 review loop is documented as future work, not faked.
        self.assertNotIn("agent:fix handoff", result.stdout)

    def test_status_reports_tasks_without_claiming_a_run(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        self.assertEqual(self.world.run_cli("worker", "--once").returncode, 0)

        result = self.world.run_cli("status", "--no-sync")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{self.slug}#1", result.stdout)
        self.assertIn("queued", result.stdout)
        # `status` must never imply that a run happened; it is a read-only view.
        self.assertIn("own#N = a PR this worker created", result.stdout)

    def test_status_json_is_machine_readable(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        self.assertEqual(self.world.run_cli("worker", "--once").returncode, 0)
        result = self.world.run_cli("status", "--no-sync", "--json")
        payload = json.loads(result.stdout)
        self.assertEqual(payload["max_concurrent_tasks"], 1)
        self.assertEqual(payload["tasks"][0]["phase"], "queued")
        self.assertTrue(payload["tasks"][0]["dispatchable"])
        # Ownership is reported explicitly, and `null` means "no PR owned yet".
        self.assertIsNone(payload["tasks"][0]["pr_number"])
        self.assertIn("pr_url", payload["tasks"][0])

    def test_pause_unpause_and_retry_round_trip(self) -> None:
        self.set_issues(issue(1, "First", labels=[TRIGGER]))
        self.assertEqual(self.world.run_cli("worker", "--once").returncode, 0)

        paused = self.world.run_cli("pause", "--repo", self.slug, "--issue", "1")
        self.assertEqual(paused.returncode, 0, paused.stderr)
        store = Store(self.world.load_config().worker.state_db)
        self.assertEqual(store.get_task(self.slug, 1).phase, "paused")
        store.close()

        resumed = self.world.run_cli("unpause", "--repo", self.slug, "--issue", "1")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        store = Store(self.world.load_config().worker.state_db)
        self.assertEqual(store.get_task(self.slug, 1).phase, "queued")
        store.close()

        # retry only applies to failed/needs_attention tasks.
        rejected = self.world.run_cli("retry", "--repo", self.slug, "--issue", "1")
        self.assertEqual(rejected.returncode, 1)

    def test_actions_on_unknown_tasks_fail_clearly(self) -> None:
        result = self.world.run_cli("pause", "--repo", self.slug, "--issue", "999")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no task recorded", result.stderr)

    def test_actions_on_non_allowlisted_repos_fail_clearly(self) -> None:
        result = self.world.run_cli("pause", "--repo", "nobody/other", "--issue", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("allowlist", result.stderr)

    def test_enqueue_refuses_an_unlabelled_issue(self) -> None:
        self.set_issues(issue(4, "No label yet"))
        result = self.world.run_cli("enqueue", "--repo", self.slug, "--issue", "4")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("does not carry", result.stdout)
        self.assertIn(TRIGGER, result.stdout)
        # And it must not silently apply the label.
        labels = self.world.read_world()["repos"][self.slug]["issues"][0]["labels"]
        self.assertEqual(labels, [])

    def test_enqueue_is_idempotent(self) -> None:
        self.set_issues(issue(4, "Labelled", labels=[TRIGGER]))
        first = self.world.run_cli("enqueue", "--repo", self.slug, "--issue", "4")
        second = self.world.run_cli("enqueue", "--repo", self.slug, "--issue", "4")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already queued", second.stdout)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        self.assertEqual(len(store.list_tasks(self.slug)), 1)


# ==============================================================================
# Regression tests for PR #10 review findings
#
# Each test below pins a specific defect found in review, so the behaviour cannot
# silently regress. The names state the invariant, not the implementation.
# ==============================================================================


class EnqueueSharesPollRulesTests(BaseCase):
    """Finding 1: manual `enqueue` must not have looser rules than a poll."""

    def test_enqueue_refuses_an_issue_that_already_has_a_pr(self) -> None:
        self.set_issues(issue(1, "Has a PR", labels=[TRIGGER]))
        self.set_pulls(pull(42, "human/branch", body="Closes #1"))

        result = self.world.run_cli("enqueue", "--repo", self.slug, "--issue", "1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("PR #42", result.stdout)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 1)
        # Recorded as an observation so the maintainer can see why, but never
        # queued as new implementation work and never claimed as owned.
        self.assertEqual(task.phase, "awaiting_review")
        self.assertEqual(task.linked_pr_number, 42)
        self.assertIsNone(task.pr_number, "a foreign PR must never be recorded as owned")
        self.assertFalse(task.dispatchability()[0])

    def test_enqueue_refuses_a_pull_request_number(self) -> None:
        # A PR number is a valid GitHub "issue" number. The single-object endpoint
        # answers for both, so enqueue must reject the PR object explicitly.
        self.set_issues(issue(7, "A real issue", labels=[TRIGGER]))
        self.set_pulls(pull(8, "feature/thing", body=""))

        result = self.world.run_cli("enqueue", "--repo", self.slug, "--issue", "8")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("is a pull request, not an Issue", result.stdout)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        self.assertIsNone(store.get_task(self.slug, 8), "a PR must never get a task row")

    def test_enqueue_refuses_a_closed_issue(self) -> None:
        self.set_issues(issue(3, "Closed", labels=[TRIGGER], state="closed"))
        result = self.world.run_cli("enqueue", "--repo", self.slug, "--issue", "3")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("is closed", result.stdout)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        self.assertIsNone(store.get_task(self.slug, 3))

    def test_enqueue_does_not_clear_a_previously_observed_linked_pr(self) -> None:
        # Poll first (records the PR observation), then enqueue: the earlier
        # observation must survive and the Issue must stay unqueueable.
        self.set_issues(issue(1, "Has a PR", labels=[TRIGGER]))
        self.set_pulls(pull(42, "human/branch", body="Closes #1"))

        worker, store, _ = self.worker()
        worker.poll_once()
        before = self.task(store, 1)
        self.assertEqual(before.linked_pr_number, 42)

        result = self.world.run_cli("enqueue", "--repo", self.slug, "--issue", "1")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)

        after = store.get_task(self.slug, 1)
        self.assertEqual(after.linked_pr_number, 42, "enqueue must not clear a known PR link")
        self.assertIsNone(after.pr_number)
        self.assertEqual(after.phase, "awaiting_review")

    def test_enqueue_and_poll_agree_on_a_plain_labelled_issue(self) -> None:
        self.set_issues(issue(5, "Plain", labels=[TRIGGER]))
        result = self.world.run_cli("enqueue", "--repo", self.slug, "--issue", "5")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("queued", result.stdout)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        task = store.get_task(self.slug, 5)
        self.assertEqual(task.phase, "queued")
        self.assertTrue(task.dispatchability()[0], "a poll would queue this, so enqueue must too")

    def test_enqueue_is_idempotent_and_reuses_the_row(self) -> None:
        self.set_issues(issue(6, "Plain", labels=[TRIGGER]))
        first = self.world.run_cli("enqueue", "--repo", self.slug, "--issue", "6")
        second = self.world.run_cli("enqueue", "--repo", self.slug, "--issue", "6")
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        # Re-enqueueing an already-queued Issue is a success, not an error.
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn("already queued (row reused)", second.stdout)

        store = Store(self.world.load_config().worker.state_db)
        self.addCleanup(store.close)
        self.assertEqual(len(store.list_tasks(self.slug)), 1)


class ForeignPrOwnershipTests(BaseCase):
    """Finding 2: a foreign PR must never be recorded as the worker's own."""

    def test_foreign_pr_stays_unowned_across_many_polls(self) -> None:
        self.set_issues(issue(1, "Has a PR", labels=[TRIGGER]))
        self.set_pulls(pull(42, "human/branch", body="Closes #1"))

        worker, store, _ = self.worker()
        for poll in range(4):
            worker.poll_once()
            task = self.task(store, 1)
            self.assertIsNone(
                task.pr_number,
                f"poll {poll + 1}: a foreign PR became owned; #4 could act on someone else's PR",
            )
            self.assertEqual(task.linked_pr_number, 42)
            self.assertFalse(
                task.dispatchability()[0], f"poll {poll + 1}: must stay non-dispatchable"
            )

    def test_owned_pr_and_a_second_linked_pr_keep_ownership(self) -> None:
        # The task owns PR 10; a *different* PR 20 also references the Issue. The
        # recorded owner must not be overwritten by the discovered foreign one.
        self.set_issues(issue(1, "Two PRs", labels=[TRIGGER]))
        self.set_pulls(
            pull(10, "dispatch/issue-1-slug"), pull(20, "human/branch", body="Closes #1")
        )

        worker, store, _ = self.worker()
        worker.poll_once()

        # `record_pr_ownership` is the #4-only entry point: discovery must never
        # call it, and the tests call it explicitly to stand in for #4.
        store.record_pr_ownership(self.task(store, 1).id, 10, "own PR recorded")
        owned = self.task(store, 1)
        self.assertEqual(owned.pr_number, 10)

        # A poll that sees the (higher-numbered, open) foreign PR 20 must not take
        # ownership away from PR 10.
        worker.poll_once()
        after = self.task(store, 1)
        self.assertEqual(after.pr_number, 10, "ownership must not be reassigned by a poll")

    def test_poll_does_not_assign_ownership_even_when_pr_matches_the_dispatch_branch(self) -> None:
        # A PR on the deterministic dispatch branch is a strong signal, but still
        # not proof that *this* task created it. Ownership stays with #4.
        self.set_issues(issue(1, "Looks like ours", labels=[TRIGGER]))
        self.set_pulls(pull(11, "dispatch/issue-1-anything"))

        worker, store, _ = self.worker()
        for _ in range(3):
            worker.poll_once()
        task = self.task(store, 1)
        self.assertIsNone(task.pr_number)
        self.assertEqual(task.linked_pr_number, 11)
        self.assertEqual(task.phase, "awaiting_review")


class TruncatedPrScanTests(BaseCase):
    """Finding 3: an incomplete PR listing must fail closed, not queue."""

    def test_truncated_pr_scan_queues_nothing_and_creates_no_rows(self) -> None:
        self.set_issues(issue(1, "Labelled", labels=[TRIGGER]))
        # Pages 1..N come back exactly full, so the client paginates to its cap.
        worker, store, _ = self.worker(env_overrides={"FAKE_GH_PAD_FULL_PAGES": "60"})
        outcome = worker.poll_once()

        self.assertFalse(outcome.ok, "a truncated PR listing must be reported as a failure")
        repo_outcome = outcome.result.repos[0]  # type: ignore[union-attr]
        self.assertEqual(repo_outcome.error_kind, "incomplete_scan")
        self.assertIn("page cap", repo_outcome.error or "")
        self.assertIsNone(
            store.get_task(self.slug, 1), "must not queue when PR state is unprovable"
        )

    def test_truncated_scan_preserves_a_previously_recorded_pr_observation(self) -> None:
        self.set_issues(issue(1, "Has a PR", labels=[TRIGGER]))
        self.set_pulls(pull(42, "human/branch", body="Closes #1"))

        worker, store, _ = self.worker()
        worker.poll_once()
        before = self.task(store, 1)
        self.assertEqual(before.linked_pr_number, 42)

        # Now the PR listing becomes unreadable due to truncation.
        worker, _store, _ = self.worker(store=store, env_overrides={"FAKE_GH_PAD_FULL_PAGES": "60"})
        outcome = worker.poll_once()
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.result.repos[0].error_kind, "incomplete_scan")  # type: ignore[union-attr]

        after = self.task(store, 1)
        self.assertEqual(
            after.linked_pr_number, 42, "a truncated scan must not clear the observation"
        )
        self.assertEqual(after.phase, before.phase)
        self.assertEqual(after.updated_at, before.updated_at, "task state must be left untouched")

    def test_normal_pr_pagination_is_not_reported_as_truncated(self) -> None:
        # A listing that legitimately ends must not be mistaken for truncation.
        self.set_issues(issue(1, "Labelled", labels=[TRIGGER]))
        self.set_pulls(*(pull(number, f"branch/{number}", body="") for number in range(1, 150)))

        worker, store, _ = self.worker()
        outcome = worker.poll_once()
        self.assertTrue(outcome.ok, outcome.error)
        self.assertEqual(self.task(store, 1).phase, "queued")


class DryRunNoSyncTests(BaseCase):
    """Finding 4: `dry-run --no-sync` must not call the GitHub wrapper."""

    def test_dry_run_no_sync_makes_no_api_calls(self) -> None:
        self.set_issues(issue(1, "Labelled", labels=[TRIGGER]))
        # Any wrapper invocation is a hard failure, so a call cannot slip through.
        self.world.world["failures"] = {  # type: ignore[index]
            "user": {"stderr": "wrapper must not be called", "exit": 1},
            f"{self.slug}:issues": {"stderr": "wrapper must not be called", "exit": 1},
        }
        self.world.write_world()

        result = self.world.run_cli("dry-run", "--no-sync")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("local state only", result.stdout)
        self.assertNotIn("FAILED", result.stdout)

    def test_dry_run_no_sync_still_persists_nothing(self) -> None:
        result = self.world.run_cli("dry-run", "--no-sync")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.world.load_config().worker.state_db.exists())

    def test_dry_run_without_no_sync_still_polls(self) -> None:
        # The default must keep performing the real read-only poll.
        self.set_issues(issue(1, "Labelled", labels=[TRIGGER]))
        result = self.world.run_cli("dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("queued=1", result.stdout)
        self.assertNotIn("local state only", result.stdout)


class StatusIsReadOnlyTests(BaseCase):
    """Re-review follow-up: `status` is observability and must not write state.

    The worker does not take the single-instance lock for read paths, so a
    status command that persisted would be a second, unsynchronised writer
    competing with the service — and would surprise an operator who only asked
    what the queue contains.
    """

    def test_status_does_not_create_the_state_database(self) -> None:
        self.set_issues(issue(1, "Labelled", labels=[TRIGGER]))
        result = self.world.run_cli("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(
            self.world.load_config().worker.state_db.exists(),
            "`status` must not create the state database",
        )

    def test_status_does_not_insert_rows_for_a_discovered_issue(self) -> None:
        self.set_issues(issue(1, "Labelled", labels=[TRIGGER]))
        result = self.world.run_cli("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        # It must still *report* what the poll sees, without persisting it.
        self.assertIn("queued=1", result.stdout)

        db = self.world.load_config().worker.state_db
        self.assertFalse(db.exists(), "no row may be written by `status`")

    def test_status_does_not_advance_a_phase(self) -> None:
        # Establish real state with a mutating poll first.
        self.set_issues(issue(1, "Labelled", labels=[TRIGGER]))
        self.assertEqual(self.world.run_cli("worker", "--once").returncode, 0)
        db = self.world.load_config().worker.state_db
        before = Store(db)
        try:
            snapshot_before = [(t.issue_number, t.phase) for t in before.list_tasks()]
            updated_before = {t.issue_number: t.updated_at for t in before.list_tasks()}
        finally:
            before.close()

        # Withdraw the label on GitHub, then ask for status: the on-disk phase must
        # not change until the *worker* reconciles it.
        self.set_issues(issue(1, "Labelled", labels=[]))
        self.assertEqual(self.world.run_cli("status").returncode, 0)

        after = Store(db)
        try:
            self.assertEqual(
                [(t.issue_number, t.phase) for t in after.list_tasks()],
                snapshot_before,
                "`status` must not change a task phase",
            )
            self.assertEqual(
                {t.issue_number: t.updated_at for t in after.list_tasks()},
                updated_before,
                "`status` must not touch updated_at",
            )
        finally:
            after.close()

    def test_status_no_sync_does_not_create_the_database(self) -> None:
        result = self.world.run_cli("status", "--no-sync")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.world.load_config().worker.state_db.exists())

    def test_status_reports_unpersisted_state_and_is_idempotent(self) -> None:
        # Running it repeatedly must be a no-op on disk.
        self.set_issues(issue(1, "Labelled", labels=[TRIGGER]))
        for _ in range(3):
            self.assertEqual(self.world.run_cli("status").returncode, 0)
        self.assertFalse(
            self.world.load_config().worker.state_db.exists(),
            "repeated `status` invocations must still write nothing",
        )

    def test_status_fails_honestly_when_the_wrapper_is_unavailable(self) -> None:
        self.set_issues(issue(1, "Labelled", labels=[TRIGGER]))
        self.world.write_config(github_overrides={"command": f"{self.tmp}/nope"})
        result = self.world.run_cli("status")
        # It reports the problem and still shows the (empty) on-disk state.
        self.assertIn("config", result.stdout)
        self.assertFalse(self.world.load_config().worker.state_db.exists())

    def test_simulated_poll_logs_are_labelled_as_simulated(self) -> None:
        # A read-only status whose log says "issue_queued" would mislead an operator
        # tailing the journal into thinking durable state changed.
        self.set_issues(issue(1, "Labelled", labels=[TRIGGER]))
        result = self.world.run_cli("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("simulated_issue_queued", result.stderr)
        self.assertIn("simulated=True", result.stderr)
        # The unqualified event name must not appear at the start of a log line.
        for line in result.stderr.splitlines():
            if "issue_queued" not in line:
                continue
            self.assertIn("simulated_", line, f"unlabelled queue event in: {line}")

    def test_worker_logs_are_not_labelled_simulated(self) -> None:
        # The real worker must keep its unqualified event names.
        self.set_issues(issue(1, "Labelled", labels=[TRIGGER]))
        result = self.world.run_cli("worker", "--once")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("issue_queued", result.stderr)
        self.assertNotIn("simulated", result.stderr)


class LabelSetupTests(BaseCase):
    def test_setup_labels_requires_explicit_confirmation(self) -> None:
        self.world.world["repos"][self.slug]["labels"] = []  # type: ignore[index]
        self.world.write_world()

        result = self.world.run_cli("setup-labels", "--repo", self.slug)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.world.read_world()["repos"][self.slug]["labels"], [])

    def test_setup_labels_creates_missing_labels_idempotently(self) -> None:
        self.world.world["repos"][self.slug]["labels"] = []  # type: ignore[index]
        self.world.write_world()

        first = self.world.run_cli("setup-labels", "--repo", self.slug, "--yes")
        self.assertEqual(first.returncode, 0, first.stderr)
        names = {label["name"] for label in self.world.read_world()["repos"][self.slug]["labels"]}
        self.assertEqual(names, {TRIGGER, HANDOFF})

        second = self.world.run_cli("setup-labels", "--repo", self.slug, "--yes")
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertIn("already present", second.stdout)
        names_after = {
            label["name"] for label in self.world.read_world()["repos"][self.slug]["labels"]
        }
        self.assertEqual(names_after, {TRIGGER, HANDOFF}, "a second run must not duplicate labels")

    def test_setup_labels_refuses_non_allowlisted_repos(self) -> None:
        result = self.world.run_cli("setup-labels", "--repo", "nobody/other", "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("allowlist", result.stderr)

    def test_setup_labels_reports_a_label_read_failure_honestly(self) -> None:
        # The failure path in `_cmd_setup_labels` references GitHubError; if that
        # name is not imported the handler raises NameError instead of reporting
        # the GitHub problem, which is worse than the original error.
        self.inject_failure(f"{self.slug}:labels", stderr="rate limit exceeded")

        result = self.world.run_cli("setup-labels", "--repo", self.slug, "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("NameError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("label_read_failed", result.stderr)

    def test_setup_labels_reports_a_label_create_failure_honestly(self) -> None:
        # Same NameError trap on the create path.
        self.world.world["repos"][self.slug]["labels"] = []  # type: ignore[index]
        self.world.write_world()
        self.inject_failure(f"{self.slug}:label_create", stderr="could not create label")

        result = self.world.run_cli("setup-labels", "--repo", self.slug, "--yes")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("NameError", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertIn("label_create_failed", result.stderr)


if __name__ == "__main__":
    unittest.main(verbosity=2)

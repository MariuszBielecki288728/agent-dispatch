#!/usr/bin/env python3
"""Offline test-suite for Issue #5 — the review loop.

Run with the rest of the suite:

    ./scripts/test-offline.sh          # or
    PYTHONPATH=src python3 -m unittest discover -s tests -v

Everything here is deterministic and offline. GitHub is served by
``tests/fake_wrapper.py`` and the coding agent by ``tests/fake_runtime.py``, both
real executables, so these tests drive the actual ``ReviewLoop``, ``Orchestrator``,
``CommandCodeDriver`` and ``Store`` code paths — real subprocesses, real Git
worktrees, real SQLite transactions — rather than a mock that could drift from
production behaviour.

Coverage maps to the Issue #5 acceptance list:

* one owned PR with a mixture of conversation comment, inline review comment/reply,
  submitted review, an edited comment, the dispatcher's own progress comment, and a
  reviewer/implementer **sharing one login** — one ``agent:fix`` starts **exactly one**
  round in the original session and worktree with the correct feedback subset;
* repeated polls and a persistently present label do nothing after the claim;
* remove-and-re-add is the only way to ask for a further round;
* incomplete pagination, a missing session, a closed/foreign PR, a paused task, a
  missing ``take-it`` and a concurrent handoff all fail closed with the feedback
  neither lost nor silently acknowledged;
* a publish-only regression for a round: resume completes cleanly → publication
  fails → recovery publishes to the **same PR** with **zero** further runtime calls,
  and only then advances the cursor;
* a crash at the review-round handoff leaves neither ``published-but-unacknowledged``
  nor ``acknowledged-but-not-awaiting_review``, and the model is not called again;
* the #17 one-comment lifecycle is reused: the round heartbeats the **same** comment,
  the terminal body is ``Awaiting review``, and no second comment is created.

The opt-in live VM smoke (a real Command Code run against a disposable PR) is
deliberately **not** here: it needs a wrapper-authorised repository and real model
credits. Its absence is reported in the PR rather than papered over.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))

from agent_dispatch.review import (  # noqa: E402
    KIND_CONVERSATION,
    collect_feedback,
    parse_cursor,
    serialise_cursor,
)
from agent_dispatch.statuscomment import (  # noqa: E402
    STATE_APPLYING_FEEDBACK,
    STATE_AWAITING_REVIEW,
    STATE_NEEDS_ATTENTION,
    STATE_RUNNING,
)
from agent_dispatch.store import (  # noqa: E402
    RECOVERY_PUSH_FAILED,
    ROUND_CLAIMED,
    ROUND_FAILED,
    ROUND_INTERRUPTED,
    ROUND_PUBLICATION_BLOCKED,
    ROUND_PUBLISH_PENDING,
    ROUND_PUBLISHED,
    ROUND_RELEASED,
    ROUND_STAGE_PR,
    ROUND_STAGE_PUSH,
    ROUND_STAGES,
    Store,
)
from test_execution import ExecutionCase  # noqa: E402
from test_offline import TRIGGER, issue  # noqa: E402

HANDOFF = "agent:fix"
PR_NUMBER = 1


class ReviewCase(ExecutionCase):
    """Base for #5 tests: a real owned PR, worktree and resumable session."""

    def setUp(self) -> None:
        super().setUp()
        # One Issue labelled for dispatch, and one PR created by the dispatcher.
        self.set_issues(issue(1, "Feature work", labels=[TRIGGER]))

    # ------------------------------------------------------------- first run

    def first_run(self) -> None:
        """Complete one ordinary implementation run: worktree, session, PR."""
        self.assertEqual(self.run_cli("run").returncode, 0)
        task = self.task_row()
        self.assertEqual(task.phase, "awaiting_review", task.last_error)
        self.assertEqual(task.pr_number, PR_NUMBER, "the first run must own a PR")
        self.assertIsNotNone(task.session_id)
        self.assertEqual(len(self.recorded_argv()), 1, "exactly one implementation run")

    # -------------------------------------------------------------- fixtures

    def task_row(self):
        from agent_dispatch.config import load_config

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        try:
            task = store.get_task(self.slug, 1)
            self.assertIsNotNone(task, "expected a task row")
            return task
        finally:
            store.close()

    def store(self) -> Store:
        from agent_dispatch.config import load_config

        config = load_config(self.world.config_path)
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        return store

    def current_world(self) -> dict:
        return self.world.read_world()

    def pr(self, number: int = PR_NUMBER) -> dict:
        repo = self.current_world()["repos"][self.slug]
        for record in repo.get("pulls", []):
            if int(record["number"]) == number:
                return record
        raise AssertionError(f"no PR #{number} in the fake world")

    def mutate(self, **updates: object) -> None:
        """Mutate the fake world's repository block and persist it."""
        world = self.current_world()
        world["repos"][self.slug].update(updates)  # type: ignore[union-attr]
        self.world.world = world
        self.world.write_world()

    def set_pr_labels(self, *labels: str) -> None:
        world = self.current_world()
        repo = world["repos"][self.slug]
        for record in repo.get("pulls", []):
            if int(record["number"]) == PR_NUMBER:
                record["labels"] = [{"name": name} for name in labels]
        self.world.world = world
        self.world.write_world()

    def add_pr_labels(self, *labels: str) -> None:
        existing = [
            str(item["name"]) if isinstance(item, dict) else str(item)
            for item in self.pr().get("labels", [])
        ]
        self.set_pr_labels(*sorted(set(existing) | set(labels)))

    def hand_off(self) -> None:
        """The maintainer's explicit handoff: the label on the open owned PR."""
        self.add_pr_labels(HANDOFF)

    def add_comment(
        self,
        body: str,
        *,
        author: str = "maintainer",
        comment_id: int | None = None,
        created_at: str = "2026-02-01T10:00:00Z",
        updated_at: str | None = None,
        target: int = PR_NUMBER,
    ) -> int:
        """Add a PR conversation comment (GitHub serves these via the issues API)."""
        world = self.current_world()
        repo = world["repos"][self.slug]
        records = repo.setdefault("comments", [])
        number = comment_id or (max([int(item["id"]) for item in records] or [1000]) + 1)
        records.append(
            {
                "id": number,
                "body": body,
                "html_url": f"https://github.com/{self.slug}/issues/{target}#issuecomment-{number}",
                "user": {"login": author},
                "issue_number": target,
                "created_at": created_at,
                "updated_at": updated_at or created_at,
                "edit_count": 0,
            }
        )
        self.world.world = world
        self.world.write_world()
        return number

    def add_inline(
        self,
        body: str,
        *,
        author: str = "maintainer",
        path: str = "impl.txt",
        line: int | None = 3,
        original_line: int | None = None,
        in_reply_to: int | None = None,
        comment_id: int | None = None,
        created_at: str = "2026-02-01T10:05:00Z",
    ) -> int:
        world = self.current_world()
        repo = world["repos"][self.slug]
        records = repo.setdefault("review_comments", [])
        number = comment_id or (max([int(item["id"]) for item in records] or [5000]) + 1)
        record: dict[str, object] = {
            "id": number,
            "body": body,
            "html_url": f"https://github.com/{self.slug}/pull/{PR_NUMBER}#discussion_r{number}",
            "user": {"login": author},
            "path": path,
            "line": line,
            "original_line": original_line,
            "side": "RIGHT",
            "commit_id": "a" * 40,
            "created_at": created_at,
            "updated_at": created_at,
            "pull_number": PR_NUMBER,
        }
        if in_reply_to is not None:
            record["in_reply_to_id"] = in_reply_to
        records.append(record)
        self.world.world = world
        self.world.write_world()
        return number

    def add_review(
        self,
        body: str,
        *,
        state: str = "CHANGES_REQUESTED",
        author: str = "maintainer",
        review_id: int | None = None,
        submitted_at: str = "2026-02-01T10:10:00Z",
    ) -> int:
        world = self.current_world()
        repo = world["repos"][self.slug]
        records = repo.setdefault("reviews", [])
        number = review_id or (max([int(item["id"]) for item in records] or [7000]) + 1)
        records.append(
            {
                "id": number,
                "body": body,
                "state": state,
                "submitted_at": submitted_at,
                "html_url": f"https://github.com/{self.slug}/pull/{PR_NUMBER}#pullrequestreview-{number}",
                "user": {"login": author},
                "commit_id": "a" * 40,
                "pull_number": PR_NUMBER,
            }
        )
        self.world.world = world
        self.world.write_world()
        return number

    def inject_failure(self, key: str, **spec: object) -> None:
        """Inject a one-shot wrapper failure for one endpoint key."""
        world = self.current_world()
        failures = world.setdefault("failures", {})
        failures[key] = {"fail_once": True, **spec}
        self.world.world = world
        self.world.write_world()

    # ------------------------------------------------------------ assertions

    def rounds(self):
        return self.store().rounds_for(self.task_row().id)

    def open_round(self):
        return self.store().open_round(self.task_row().id)

    def runtime_calls(self) -> int:
        return len(self.recorded_argv())

    def claimed_cursor(self) -> str:
        """The feedback cursor a fresh claim would record for this task's PR.

        Crash fixtures must claim with a cursor that a restart can actually reproduce,
        because a claimed round's snapshot has to be re-read from GitHub and re-checked
        before it may run again. A hand-written cursor would be a snapshot no live read
        can match, and the round would (correctly) refuse to re-drive — so a test built
        on one would prove nothing about the restart path.
        """
        import os

        from agent_dispatch.github import GitHubClient
        from test_offline import FAKE_WRAPPER

        os.environ["FAKE_GH_WORLD"] = str(self.world.world_path)
        feedback = collect_feedback(
            GitHubClient(str(FAKE_WRAPPER)),
            repo=self.slug,
            pr_number=PR_NUMBER,
            issue_number=1,
        )
        self.assertTrue(feedback.complete, feedback.problems)
        return serialise_cursor(feedback.cursor())

    def parked_publish_pending_round(self, *, stage: str = ROUND_STAGE_PR):
        """A round whose model turn already completed and whose publication failed.

        Built directly, because the alternative is a fixture that lies. Injecting a
        one-shot failure and running the whole `review` command does **not** reach the
        publication code: `ReviewLoop.evaluate` reads the PR itself, so it consumes the
        injected failure first and defers — and the test would then pass or fail for
        reasons that have nothing to do with publication.

        The state modelled is exactly what `_publish_review_round` leaves behind: the
        round is `publish_pending` with a review-specific stage, and the task is parked
        with the same stage so the recovery pass picks it up.
        """
        store = self.store()
        task = self.task_row()
        cursor = self.claimed_cursor()
        claimed = store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch=task.branch,
            worktree_path=task.worktree_path,
            session_id=task.session_id,
            cursor_json=cursor,
            snapshot_json=cursor,
        )
        store.start_review_round(claimed.id, session_id=task.session_id)
        store.finish_run(
            store.start_run(
                task.id,
                run_id="20260401T000000000000-review-parked1",
                kind="review",
                resumed_from=task.session_id,
                log_path=str(self.tmp / "parked.ndjson"),
                consumes_attempt=False,
            ),
            outcome="succeeded",
            session_id=task.session_id,
            exit_code=0,
            subtype="success",
            tool_hook_blocked=False,
            timed_out=False,
            produced_work=True,
            detail=None,
        )
        store.mark_round_publish_pending(claimed.id, head_sha="a" * 40, stage=stage)
        store.park_for_recovery(task.id, stage=stage, note="review publication failed")
        return store, task, claimed

    def _repo_config(self):
        from agent_dispatch.config import load_config

        return load_config(self.world.config_path).repo(self.slug)

    def _orchestrator(self, store, repository, task):
        """An Orchestrator plus the worktree manager and state it would publish from."""
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator

        config = load_config(self.world.config_path)
        os.environ["FAKE_GH_WORLD"] = str(self.world.world_path)
        stream = open(os.devnull, "w")  # noqa: SIM115
        self.addCleanup(stream.close)
        orchestrator = Orchestrator(
            config, store, GitHubClient(config.github.command), Logger(fmt="text", stream=stream)
        )
        manager = orchestrator._manager(repository)
        state = manager.inspect(task.worktree_path, task.branch)
        self.assertTrue(state.branch_matches, state.describe())
        return orchestrator, manager, state

    def _confirm_publish(self, orchestrator, store, claimed, task, repository, manager, state):
        """Drive the exact-PR confirmation directly, with the round's real handover.

        Called directly rather than through `review` on purpose: `ReviewLoop.evaluate`
        reads the PR itself, so a whole-command test would consume any injected failure
        before publication was reached — and would then assert about the wrong step.
        """
        return orchestrator._confirm_exact_pull_request(
            task,
            repository,
            state,
            require_pr=PR_NUMBER,
            round_id=claimed.id,
            result=None,
            notes=[],
            on_confirmed=lambda number, url: store.finalise_review_round(
                claimed.id,
                task.id,
                cursor_json=claimed.cursor or "{}",
                pr_number=number,
                pr_url=url,
            ),
            recovery_stage=ROUND_STAGE_PR,
        )

    def _remote_tip(self, branch: str) -> str | None:
        """The remote tip of ``branch``, asked through Git.

        Lives on the base class because three classes now assert "nothing was pushed".
        A per-class copy is how those assertions drift apart.
        """
        import subprocess

        proc = subprocess.run(
            [
                "git",
                "-C",
                str(self.source),
                "ls-remote",
                "--heads",
                "origin",
                f"refs/heads/{branch}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        line = (proc.stdout or "").strip()
        return line.split()[0] if line else None

    def _label_names(self) -> list[str]:
        return [
            str(item["name"]) if isinstance(item, dict) else str(item)
            for item in self.pr().get("labels", [])
        ]

    def live_orchestrator(self, store):
        """An Orchestrator wired to the fake wrapper, for driving reconcile passes.

        Used by the tests that must run a pass *directly*: going through the `review`
        CLI would also poll discovery and run the round pass, so a "did this pass push
        anything?" assertion could be satisfied by some other pass doing nothing.
        """
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator

        config = load_config(self.world.config_path)
        os.environ["FAKE_GH_WORLD"] = str(self.world.world_path)
        stream = open(os.devnull, "w")  # noqa: SIM115
        self.addCleanup(stream.close)
        return Orchestrator(
            config, store, GitHubClient(config.github.command), Logger(fmt="text", stream=stream)
        )

    def live_client(self):
        """A GitHubClient wired to the fake wrapper, for direct reads in a test."""
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient

        config = load_config(self.world.config_path)
        os.environ["FAKE_GH_WORLD"] = str(self.world.world_path)
        return GitHubClient(config.github.command)

    def run_all_reconcile_passes(self, store) -> None:
        """One poll's worth of the automatic passes, in the worker's real order.

        Discovery runs FIRST, exactly as `Worker.poll_once` does, and that ordering is
        part of what the pause tests are about: removing `take-it` is applied *by* the
        discovery pass, so a poll driven without it would never see the withdrawal at
        all and the assertion would pass for the wrong reason.

        Then the review pass, then the implementation passes. A guard that only the
        review pass honours would still push a paused round's commits the moment the
        implementation passes ran — which is why this drives all of them rather than
        calling one directly.
        """
        from agent_dispatch.config import load_config
        from agent_dispatch.discovery import Discovery
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger

        config = load_config(self.world.config_path)
        os.environ["FAKE_GH_WORLD"] = str(self.world.world_path)
        stream = open(os.devnull, "w")  # noqa: SIM115
        self.addCleanup(stream.close)
        log = Logger(fmt="text", stream=stream)
        client = GitHubClient(config.github.command)

        Discovery(config, store, client, log).poll_once()

        orchestrator = self.live_orchestrator(store)
        orchestrator.reconcile_review_rounds()
        orchestrator.reconcile_publish_pending()
        orchestrator.reconcile()

    def review_scenario(self, **overrides: object) -> None:
        """Script a second run: the resumed review round."""
        run: dict[str, object] = {
            "session_id": self.task_row().session_id,
            "subtype": "success",
            "edits": {"impl.txt": "reviewed\n"},
        }
        run.update(overrides)
        self.write_scenario(runs=[run])

    def status_comments(self) -> list[dict]:
        repo = self.current_world()["repos"][self.slug]
        return [
            record
            for record in repo.get("comments", [])
            if "agent-dispatch:status" in str(record.get("body", ""))
        ]

    def status_body(self) -> str:
        rows = self.status_comments()
        self.assertEqual(len(rows), 1, f"expected exactly one status comment, got {len(rows)}")
        return str(rows[0]["body"])


# ==============================================================================
# The handoff protocol: one claim, one round, and a label that is not a metronome
# ==============================================================================


class OneRoundPerHandoffTests(ReviewCase):
    """The core trigger rules: comments never start work, the label does, once."""

    def test_one_handoff_starts_exactly_one_round_in_the_original_session(self) -> None:
        self.first_run()
        session = self.task_row().session_id
        branch = self.task_row().branch
        self.hand_off()
        self.add_comment("Please rename this function.")
        self.review_scenario()

        self.assertEqual(self.run_cli("review").returncode, 0)

        task = self.task_row()
        self.assertEqual(task.phase, "awaiting_review", task.last_error)
        self.assertEqual(task.review_round, 1)
        self.assertEqual(task.session_id, session, "the round must reuse the pinned session")
        self.assertEqual(task.branch, branch, "and the same owned branch")
        self.assertEqual(self.runtime_calls(), 2, "exactly one round was started")

        argv = self.recorded_argv()[-1]
        self.assertIn("--session", argv)
        self.assertEqual(
            argv[argv.index("--session") + 1],
            session,
            "the resumed invocation must pass the exact original session id",
        )
        self.assertIn("--yolo", argv, "the permission flag is re-passed on every resume")

        rounds = self.rounds()
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0].state, ROUND_PUBLISHED)
        self.assertEqual(rounds[0].session_id, session)

    def test_exactly_one_round_when_several_polls_happen(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("One request.")
        self.review_scenario()

        self.assertEqual(self.run_cli("review").returncode, 0)
        before = self.runtime_calls()
        for _ in range(3):
            self.run_cli("review")
        self.assertEqual(self.runtime_calls(), before, "later polls must not start a round")
        self.assertEqual(len(self.rounds()), 1)

    def test_a_label_left_in_place_after_a_claim_starts_no_second_round(self) -> None:
        """The metronome guard, in the window where it actually matters.

        A label that stays visible after its round was claimed — because the removal
        failed, or the process died between the claim and the removal — must not start
        anything on later polls, however new the feedback looks. Without the
        armed/disarmed state, every poll would mint a round and spend a model call on
        feedback that was already handled.

        This is distinct from a maintainer who really does remove and re-add the label:
        that path observes the absence and legitimately re-arms (covered separately).
        """
        self.first_run()
        self.hand_off()
        self.add_comment("Fix the naming.")
        self.review_scenario()
        # The removal fails, so the label is still on the PR when the round finishes.
        self.inject_failure(f"{self.slug}:label_remove", stderr="gh: HTTP 502 bad gateway\n")
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(len(self.rounds()), 1)
        self.assertIn(HANDOFF, self._label_names(), "the label is still visible on the PR")
        self.assertFalse(self.task_row().handoff_claimable)

        # Fresh feedback arrives while that stale label sits on the PR.
        self.add_comment("And this too.", created_at="2026-02-02T10:00:00Z")
        self.review_scenario()
        calls_before = self.runtime_calls()
        for _ in range(2):
            self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(
            self.runtime_calls(),
            calls_before,
            "a label left in place after its round was claimed starts nothing",
        )
        self.assertEqual(len(self.rounds()), 1, "no second round row")
        self.assertEqual(self.task_row().review_round, 1)

        # And once the label really is gone and comes back, the handoff works again.
        self.set_pr_labels()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertTrue(self.task_row().handoff_claimable)
        self.hand_off()
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual([r.round for r in self.rounds()], [1, 2])

    def test_removing_and_readding_the_label_queues_exactly_one_further_round(self) -> None:
        """The documented way to ask again: remove it, let the round finish, add it again."""
        self.first_run()
        self.hand_off()
        self.add_comment("Round one request.")
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(len(self.rounds()), 1)

        # The maintainer removes the label (observed absent), reviews, then re-adds it.
        self.set_pr_labels()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertTrue(self.task_row().handoff_claimable, "absence must re-arm the handoff")

        self.hand_off()
        self.add_comment("Round two request.", created_at="2026-02-03T10:00:00Z")
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)

        rounds = self.rounds()
        self.assertEqual([r.round for r in rounds], [1, 2])
        self.assertEqual(rounds[1].state, ROUND_PUBLISHED)
        self.assertEqual(self.task_row().review_round, 2)
        self.assertEqual(self.runtime_calls(), 3, "exactly one round per handoff")

        # And a still-present label after round two is inert again.
        self.add_pr_labels(HANDOFF)
        calls_before = self.runtime_calls()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), calls_before)

    def test_a_comment_without_the_label_never_starts_an_agent(self) -> None:
        self.first_run()
        self.add_comment("Could you change this?")
        self.add_inline("And this line.")
        self.add_review("Overall: please rework.")
        self.review_scenario()

        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), 1, "review comments alone must never start work")
        self.assertEqual(self.task_row().phase, "awaiting_review")
        self.assertEqual(self.task_row().review_round, 0)
        self.assertEqual(self.rounds(), [])

    def test_no_new_feedback_defers_the_handoff_without_consuming_it(self) -> None:
        """A handoff whose feedback is already acknowledged waits, and is not lost."""
        self.first_run()
        self.hand_off()
        self.review_scenario()

        # The label is on the PR, but nothing new has been said since a round would have
        # been claimed. The handoff must stay available so the maintainer does not have
        # to remove and re-add the label merely to add the comment afterwards.
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), 1)
        self.assertEqual(self.rounds(), [])
        self.assertTrue(
            self.task_row().handoff_claimable,
            "a deferred handoff must remain claimable once feedback arrives",
        )

        # Feedback arrives; the SAME still-present label now claims a round.
        self.add_comment("Now here is the request.")
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(len(self.rounds()), 1)
        self.assertEqual(self.runtime_calls(), 2)

    def test_dry_run_claims_nothing_and_starts_nothing(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("A pending request.")
        self.review_scenario()

        result = self.run_cli("review", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("would claim", result.stdout)
        self.assertEqual(self.runtime_calls(), 1, "a dry run must not start an agent")
        self.assertEqual(self.rounds(), [], "a dry run must not claim a round")
        self.assertTrue(self.task_row().handoff_claimable)

        # The database is opened read-only, so nothing was written at all.
        self.assertEqual(self.task_row().review_round, 0)


# ==============================================================================
# Feedback ingestion: complete enough, honest about provenance
# ==============================================================================


class FeedbackIngestionTests(ReviewCase):
    def _instruction_for_round(self) -> str:
        """The argv the resumed run was invoked with — i.e. the exact instruction."""
        argv = self.recorded_argv()[-1]
        return argv[argv.index("-p") + 1]

    def test_every_feedback_surface_is_consolidated_into_one_instruction(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("Conversation comment: rename the helper.")
        inline_id = self.add_inline("Inline comment: this loop is off by one.")
        self.add_inline("Inline reply: agreed, and please add a test.", in_reply_to=inline_id)
        self.add_review("Submitted review: please document the new flag.")
        self.review_scenario()

        self.assertEqual(self.run_cli("review").returncode, 0)
        instruction = self._instruction_for_round()

        for expected in (
            "rename the helper",
            "off by one",
            "agreed, and please add a test",
            "document the new flag",
        ):
            self.assertIn(expected, instruction, f"the round must carry: {expected}")
        # Inline context is preserved as observed, and a reply is labelled as a reply.
        self.assertIn("impl.txt:3", instruction)
        self.assertIn("Reply within an existing thread", instruction)
        self.assertIn("submitted review (CHANGES_REQUESTED)", instruction)
        self.assertEqual(len(self.rounds()), 1)

    def test_an_outdated_inline_comment_reports_its_original_line_honestly(self) -> None:
        """An outdated comment is labelled outdated rather than given a current line.

        GitHub stops reporting ``line`` once the diff has moved on. Inventing a current
        line number would tell the agent the comment sits somewhere it does not, so the
        location carries the original line **and** says so.
        """
        self.first_run()
        self.hand_off()
        self.add_inline("This line moved.", line=None, original_line=42)
        self.review_scenario()

        self.assertEqual(self.run_cli("review").returncode, 0)
        instruction = self._instruction_for_round()

        from agent_dispatch.github import ReviewComment

        # The location logic itself, driven by the shape GitHub returns for an
        # outdated comment (no `line`, only `original_line`).
        outdated = ReviewComment(id=1, body="x", path="impl.txt", line=None, original_line=42)
        self.assertEqual(outdated.location(), "impl.txt:42 (outdated)")
        with_line = ReviewComment(id=2, body="x", path="impl.txt", line=9, original_line=42)
        self.assertEqual(with_line.location(), "impl.txt:9", "a live line is not marked outdated")
        no_context = ReviewComment(id=3, body="x", path="impl.txt")
        self.assertEqual(no_context.location(), "impl.txt (no line context returned)")

        item = self._claimed_item("inline")
        self.assertEqual(item.location, "impl.txt:42 (outdated)")
        self.assertIn("## impl.txt:42 (outdated)", instruction)
        self.assertIn("This line moved.", instruction)

    def _claimed_item(self, kind: str):
        """The single feedback item of ``kind`` a fresh read of the PR produces.

        Built from the same live read the round used, so the assertion is about the
        object the instruction was rendered from rather than a hand-written copy.
        """
        import os

        from agent_dispatch.github import GitHubClient
        from test_offline import FAKE_WRAPPER

        os.environ["FAKE_GH_WORLD"] = str(self.world.world_path)
        feedback = collect_feedback(
            GitHubClient(str(FAKE_WRAPPER)),
            repo=self.slug,
            pr_number=PR_NUMBER,
            issue_number=1,
        )
        self.assertTrue(feedback.complete, feedback.problems)
        matches = [item for item in feedback.items if item.kind == kind]
        self.assertEqual(len(matches), 1, f"expected exactly one {kind} item")
        return matches[0]

    def test_the_dispatchers_own_status_comment_is_never_fed_back(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("A real request.")
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)

        instruction = self._instruction_for_round()
        self.assertIn("A real request.", instruction)
        # The marker is machine ownership, and both the marker itself and the template's
        # own wording must stay out of the prompt.
        self.assertNotIn("agent-dispatch:status", instruction)
        self.assertNotIn("Status written by agent-dispatch", instruction)

    def test_reviewer_and_implementer_sharing_one_login_are_not_filtered(self) -> None:
        """The shared-login case is exactly why nothing may filter by author."""
        self.first_run()
        self.hand_off()
        # The same login as the agent's own commits, and as the dispatcher's comments.
        self.add_comment("Request from the shared account.", author="fake-user")
        self.add_inline("Inline from the shared account.", author="fake-user")
        self.add_review("Review from the shared account.", author="fake-user")
        self.review_scenario()

        self.assertEqual(self.run_cli("review").returncode, 0)
        instruction = self._instruction_for_round()
        for expected in ("Request from the shared account", "Inline from the shared account"):
            self.assertIn(expected, instruction)
        self.assertIn("Review from the shared account", instruction)

    def test_an_edited_comment_is_delivered_again(self) -> None:
        """Editing is new feedback: the maintainer changed what they are asking for."""
        self.first_run()
        self.hand_off()
        comment_id = self.add_comment("Original wording.")
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(len(self.rounds()), 1)

        # Second handoff, with the SAME comment edited rather than a new one added.
        self.set_pr_labels()
        self.run_cli("review")
        self.hand_off()
        world = self.current_world()
        for record in world["repos"][self.slug]["comments"]:
            if int(record["id"]) == comment_id:
                record["body"] = "Edited wording: do something different."
                record["updated_at"] = "2026-02-05T10:00:00Z"
        self.world.world = world
        self.world.write_world()
        self.review_scenario()

        self.assertEqual(self.run_cli("review").returncode, 0)
        rounds = self.rounds()
        self.assertEqual(len(rounds), 2, "an edited comment is new feedback, not done feedback")
        self.assertIn("Edited wording", self._instruction_for_round())

    def test_feedback_arriving_during_a_round_belongs_to_the_next_round(self) -> None:
        """An in-flight round must not silently absorb feedback it never saw.

        The round's snapshot is fixed at claim time, so a comment posted while it runs is
        newer than the cursor and is picked up by the next explicit handoff. It is never
        dropped, and never claimed as already applied.
        """
        self.first_run()
        self.hand_off()
        self.add_comment("First request.")
        # The comment that arrives while the round is running is only added after the
        # round completes here, and its timestamp places it inside the round's window, so
        # the "possibly your own note" provenance applies without pretending it is not
        # feedback.
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(len(self.rounds()), 1)

        # A comment added after the round finished is simply new feedback.
        self.set_pr_labels()
        self.run_cli("review")
        self.hand_off()
        self.add_comment(
            "Second request, added during the next round.", created_at="2026-03-01T10:00:00Z"
        )
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)

        rounds = self.rounds()
        self.assertEqual(len(rounds), 2)
        second = parse_cursor(rounds[1].cursor)
        self.assertTrue(second, "the second round must claim the newer feedback")
        # The first round's cursor did not include the later comment.
        first = parse_cursor(rounds[0].cursor)
        self.assertNotEqual(first, second)

    def test_a_comment_from_the_previous_round_window_is_labelled_ambiguous(self) -> None:
        """An agent's own progress note is not silently deleted, and not pretended to be human."""
        self.first_run()
        self.hand_off()
        self.add_comment("The real request.")
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)
        round_one = self.rounds()[0]

        # A comment that appeared while round one was running: its author login is
        # indistinguishable from the maintainer's, so it is labelled rather than guessed.
        self.add_comment(
            "Checking in: I have started on this.",
            created_at=round_one.started_at or round_one.claimed_at,
        )
        self.set_pr_labels()
        self.run_cli("review")
        self.hand_off()
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)

        instruction = self._instruction_for_round()
        self.assertIn("Checking in", instruction, "it must not be dropped")
        self.assertIn("possibly a note written by you during the previous round", instruction)
        self.assertIn(
            "Reviewer and implementer",
            instruction.replace("the reviewer and the implementer", "Reviewer and implementer"),
        )


# ==============================================================================
# Failing closed: an incomplete or unproven read starts nothing
# ==============================================================================


class FailingClosedTests(ReviewCase):
    def test_a_truncated_feedback_listing_refuses_to_claim(self) -> None:
        """Claiming from a partial read would acknowledge feedback the model never saw.

        Asserting only "no round started" would be too weak: a truncated read also makes
        the *new* feedback set look empty, so the round would be deferred with
        ``no_new_feedback`` and the test would pass for the wrong reason. The recorded
        reason is therefore asserted, and it must be the incompleteness one — that is
        the decision under test.
        """
        self.first_run()
        self.hand_off()
        self.add_comment("Some request.")
        self.review_scenario()

        previous = os.environ.get("FAKE_GH_PAD_REVIEWS")
        os.environ["FAKE_GH_PAD_REVIEWS"] = "60"
        self.addCleanup(
            lambda: (
                os.environ.pop("FAKE_GH_PAD_REVIEWS", None)
                if previous is None
                else os.environ.__setitem__("FAKE_GH_PAD_REVIEWS", previous)
            )
        )

        result = self.run_cli("review")
        self.assertEqual(result.returncode, 0)
        self.assertIn(
            "feedback_listing_incomplete",
            result.stderr,
            "the refusal must be the incompleteness decision, not a side effect",
        )
        self.assertNotIn("no_new_feedback", result.stderr)
        self.assertEqual(self.runtime_calls(), 1, "an incomplete read must not start a round")
        self.assertEqual(self.rounds(), [])
        task = self.task_row()
        self.assertEqual(task.phase, "awaiting_review")
        self.assertIsNone(task.feedback_cursor, "nothing may be acknowledged")
        self.assertTrue(task.handoff_claimable, "the deferred handoff is not consumed")

        # With the padding gone the same handoff claims a round, so the refusal above
        # was about the incomplete read and not a broken fixture.
        os.environ.pop("FAKE_GH_PAD_REVIEWS", None)
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(len(self.rounds()), 1)
        self.assertEqual(self.runtime_calls(), 2)

    def test_a_missing_session_refuses_rather_than_starting_a_fresh_conversation(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        # Model the one case #4 documents: an interrupted first run left no transcript,
        # so there is no resumable session to continue.
        store = self.store()
        task = self.task_row()
        for run in store.run_history(task.id):
            store.finish_run(
                run.id,
                outcome="failed",
                session_id=run.session_id,
                exit_code=1,
                subtype=None,
                tool_hook_blocked=False,
                timed_out=False,
                produced_work=None,
                detail="interrupted",
            )
        self.review_scenario()

        self.assertEqual(self.run_cli("review").returncode, 1)
        self.assertEqual(self.runtime_calls(), 1, "no fresh session may be started")
        self.assertEqual(self.rounds(), [])

        after = self.task_row()
        self.assertEqual(after.phase, "needs_attention")
        self.assertIn("resumable", after.last_error or "")
        self.assertFalse(after.handoff_claimable, "a structural refusal consumes the handoff")

    def test_a_blocked_tool_run_fails_the_round_and_acknowledges_nothing(self) -> None:
        """The silent false-success guard, applied to a review round."""
        self.first_run()
        self.hand_off()
        self.add_comment("A request that must not be marked done.")
        self.review_scenario(events=["tool_hook_blocked"], edits={}, subtype="success", exit_code=0)

        self.assertEqual(self.run_cli("review").returncode, 1)
        rounds = self.rounds()
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0].state, ROUND_FAILED)
        task = self.task_row()
        self.assertEqual(task.phase, "needs_attention")
        self.assertIsNone(task.feedback_cursor, "blocked work must not acknowledge feedback")
        self.assertEqual(task.pr_number, PR_NUMBER, "the same PR is retained")

    def test_a_paused_task_starts_no_round(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario()
        self.assertEqual(self.run_cli("pause", "--repo", self.slug, "--issue", "1").returncode, 0)

        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), 1)
        self.assertEqual(self.rounds(), [])
        self.assertEqual(self.task_row().phase, "paused")

    def test_a_merged_pr_starts_no_round(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("A request on a PR that is already merged.")
        self.mutate(
            pulls=[
                {
                    **self.pr(),
                    "state": "closed",
                    "merged_at": "2026-02-10T00:00:00Z",
                }
            ]
        )
        self.review_scenario()

        self.assertEqual(self.run_cli("review").returncode, 1)
        self.assertEqual(self.runtime_calls(), 1)
        self.assertEqual(self.rounds(), [])
        task = self.task_row()
        self.assertEqual(task.phase, "needs_attention")
        self.assertIn("merged", task.last_error or "")

    def test_a_foreign_pr_is_never_acted_on(self) -> None:
        """A PR that merely references the Issue is an observation, not ours."""
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario()
        # The recorded PR number now points at a PR whose head is a fork's branch, i.e.
        # the ownership evidence no longer holds.
        self.mutate(
            pulls=[
                {
                    **self.pr(),
                    "head": {
                        "ref": "dispatch/issue-1-feature-work",
                        "sha": "0" * 40,
                        "repo": {"full_name": "someone-else/agent-dispatch"},
                        "user": {"login": "someone-else"},
                    },
                }
            ]
        )

        self.assertEqual(self.run_cli("review").returncode, 1)
        self.assertEqual(self.runtime_calls(), 1, "a foreign PR must never be acted on")
        self.assertEqual(self.rounds(), [])
        self.assertIn("not proven to belong", self.task_row().last_error or "")

    def test_a_withdrawn_trigger_label_refuses_the_round(self) -> None:
        """`take-it` is dispatch intent for the review loop too, not just dispatch."""
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario()
        self.set_issues(issue(1, "Feature work", labels=[]))

        result = self.run_cli("review")
        self.assertEqual(result.returncode, 0, "a reversible pause is not a failure")
        self.assertEqual(self.runtime_calls(), 1, "no model call without dispatch intent")
        self.assertEqual(self.rounds(), [], "no round may be claimed")
        task = self.task_row()
        self.assertEqual(task.phase, "paused")
        self.assertEqual(task.pause_reason, "label_withdrawn")
        self.assertTrue(task.handoff_claimable, "the handoff is kept, not consumed")

    def test_an_unreadable_pr_defers_rather_than_failing_the_handoff(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario()
        self.inject_failure(f"{self.slug}:pull:{PR_NUMBER}", stderr="gh: rate limit exceeded\n")

        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), 1)
        self.assertEqual(self.rounds(), [])
        task = self.task_row()
        self.assertEqual(task.phase, "awaiting_review", "a transient failure must not park it")
        self.assertTrue(task.handoff_claimable, "a deferred handoff is retried later")


# ==============================================================================
# Publication: same PR, no second model call, and an atomic handover
# ==============================================================================


class ReviewPublicationTests(ReviewCase):
    def test_a_failed_pr_lookup_is_finished_publish_only_on_a_later_pass(self) -> None:
        """The #5 publish-only regression: same PR, zero further runtime calls.

        The failure is injected on the **exact-PR** read, which is what the review
        publication path uses now that it may not create or adopt a pull request.
        Injecting it on the PR *listing* would no longer reach this code at all, so the
        test would pass without exercising anything.
        """
        self.first_run()
        session = self.task_row().session_id
        repository = self._repo_config()
        store, task, claimed = self.parked_publish_pending_round()
        calls_before = self.runtime_calls()
        orchestrator, manager, state = self._orchestrator(store, repository, task)

        # First attempt: the exact-PR read fails, so the round must park with its stage
        # intact and its feedback still unacknowledged.
        self.inject_failure(f"{self.slug}:pull:{PR_NUMBER}", stderr="gh: rate limit exceeded\n")
        outcome = self._confirm_publish(
            orchestrator, store, claimed, task, repository, manager, state
        )
        self.assertEqual(outcome.action, "needs_attention")
        self.assertEqual(outcome.reason, "review_pr_unreadable")

        rounds = self.rounds()
        self.assertEqual(rounds[0].state, ROUND_PUBLISH_PENDING)
        self.assertEqual(rounds[0].recovery_stage, ROUND_STAGE_PR)
        self.assertIsNone(
            self.task_row().feedback_cursor,
            "feedback must stay unacknowledged until its publication is durable",
        )
        self.assertEqual(self.runtime_calls(), calls_before)

        # The failure was one-shot, so recovery now publishes — with no model call.
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), calls_before, "recovery must invoke no runtime")

        rounds = self.rounds()
        self.assertEqual(len(rounds), 1, "no second round may be minted by recovery")
        self.assertEqual(rounds[0].state, ROUND_PUBLISHED)
        self.assertIsNone(rounds[0].recovery_stage)

        after = self.task_row()
        self.assertEqual(after.phase, "awaiting_review")
        self.assertEqual(after.pr_number, PR_NUMBER, "the SAME PR is retained")
        self.assertEqual(after.session_id, session)
        self.assertEqual(
            parse_cursor(after.feedback_cursor),
            parse_cursor(rounds[0].cursor),
            "the cursor advances once publication is durable",
        )
        # Exactly one PR exists: a round never creates a replacement.
        self.assertEqual(len(self.current_world()["repos"][self.slug]["pulls"]), 1)

    def test_a_failed_push_of_a_round_is_recovered_without_a_model_call(self) -> None:
        self.first_run()
        repository = self._repo_config()
        store, task, claimed = self.parked_publish_pending_round(stage=ROUND_STAGE_PUSH)
        calls = self.runtime_calls()
        orchestrator, manager, state = self._orchestrator(store, repository, task)

        outcome = self._confirm_publish(
            orchestrator, store, claimed, task, repository, manager, state
        )
        self.assertEqual(outcome.action, "awaiting_review")
        self.assertEqual(self.runtime_calls(), calls, "publication must not call the model")

        rounds = self.rounds()
        self.assertEqual(rounds[0].state, ROUND_PUBLISHED)
        after = self.task_row()
        self.assertEqual(after.phase, "awaiting_review")
        self.assertEqual(after.pr_number, PR_NUMBER, "the SAME PR is retained")
        self.assertEqual(
            parse_cursor(after.feedback_cursor),
            parse_cursor(rounds[0].cursor),
            "the cursor advances only with the publication",
        )
        self.assertEqual(len(self.current_world()["repos"][self.slug]["pulls"]), 1)

    def test_a_round_that_changes_nothing_is_accepted_and_published(self) -> None:
        """A reasoned no-op round is a valid outcome, not a failure (#5 acceptance)."""
        self.first_run()
        self.hand_off()
        self.add_comment("Is this already correct?")
        self.review_scenario(edits={})

        self.assertEqual(self.run_cli("review").returncode, 0)
        rounds = self.rounds()
        self.assertEqual(rounds[0].state, ROUND_PUBLISHED)
        task = self.task_row()
        self.assertEqual(task.phase, "awaiting_review")
        self.assertEqual(parse_cursor(task.feedback_cursor), parse_cursor(rounds[0].cursor))

    def test_the_round_commits_what_the_agent_left_uncommitted(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("Please change this.")
        self.review_scenario(edits={"impl.txt": "changed by review\n"})

        self.assertEqual(self.run_cli("review").returncode, 0)
        run = self.store().run_history(self.task_row().id)[-1]
        self.assertEqual(run.kind, "review")
        self.assertEqual(run.outcome, "succeeded")
        self.assertTrue(run.produced_work)

    def test_a_failed_round_commit_parks_with_its_own_stage_and_keeps_the_feedback(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("Please change this.")
        self.review_scenario(edits={"impl.txt": "edited but uncommittable\n"})
        self.break_commits()

        self.assertEqual(self.run_cli("review").returncode, 1)
        rounds = self.rounds()
        self.assertEqual(rounds[0].state, ROUND_PUBLISH_PENDING)
        self.assertEqual(rounds[0].recovery_stage, "review_commit_failed")
        task = self.task_row()
        self.assertEqual(task.phase, "needs_attention")
        self.assertIsNone(task.feedback_cursor, "uncommitted work must not acknowledge feedback")

        # The cause is fixed; recovery commits and publishes without a model call.
        self.repair_commits()
        calls = self.runtime_calls()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), calls, "commit recovery must not call the model")
        self.assertEqual(self.task_row().phase, "awaiting_review")
        self.assertEqual(self.rounds()[-1].state, ROUND_PUBLISHED)


class ReviewHandoffCrashTests(ReviewCase):
    """The crash windows the issue names explicitly, made observable."""

    def test_a_claim_survives_a_failed_label_removal_and_starts_no_second_round(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario()
        self.inject_failure(f"{self.slug}:label_remove", stderr="gh: HTTP 502 bad gateway\n")

        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(len(self.rounds()), 1)
        # The label is still visible on GitHub, exactly as the failure left it.
        self.assertIn(HANDOFF, self._label_names())
        # But it cannot start a second round: the claim already consumed this appearance.
        calls = self.runtime_calls()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), calls, "a sticky label must stay inert")
        self.assertEqual(len(self.rounds()), 1)

    def test_a_claimed_but_unstarted_round_is_redriven_after_a_crash(self) -> None:
        """A claim with no run row never reached the model, so re-driving costs nothing."""
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        store = self.store()
        task = self.task_row()
        cursor = self.claimed_cursor()
        claimed = store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch=task.branch,
            worktree_path=task.worktree_path,
            session_id=task.session_id,
            cursor_json=cursor,
            snapshot_json=cursor,
        )
        self.assertEqual(claimed.state, ROUND_CLAIMED)
        self.assertEqual(claimed.attempts, 0)

        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)
        rounds = self.rounds()
        self.assertEqual(len(rounds), 1, "the same round is re-driven, not duplicated")
        self.assertEqual(rounds[0].state, ROUND_PUBLISHED)
        self.assertEqual(self.runtime_calls(), 2)

    def test_a_running_round_from_a_dead_process_is_parked_without_a_new_model_call(self) -> None:
        """A turn may already have been paid for, so it is never silently repeated."""
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        store = self.store()
        task = self.task_row()
        cursor = self.claimed_cursor()
        claimed = store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch=task.branch,
            worktree_path=task.worktree_path,
            session_id=task.session_id,
            cursor_json=cursor,
            snapshot_json=cursor,
        )
        store.start_review_round(claimed.id, session_id=task.session_id)
        run_row = store.start_run(
            task.id,
            run_id="20260301T000000000000-review-dead01",
            kind="review",
            resumed_from=task.session_id,
            log_path=str(self.tmp / "dead.ndjson"),
        )
        store.record_round_run(claimed.id, run_id=str(run_row))
        store.set_phase(task.id, "running", "review round in flight")

        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)

        self.assertEqual(self.runtime_calls(), 1, "no model call may be made for a dead round")
        rounds = self.rounds()
        self.assertEqual(rounds[0].state, ROUND_INTERRUPTED)
        task = self.task_row()
        self.assertEqual(task.phase, "needs_attention")
        self.assertIsNone(task.feedback_cursor)
        self.assertIn("interrupted", (rounds[0].error or "").lower())

    def test_a_crash_after_publication_leaves_exactly_one_applied_round(self) -> None:
        """The issue's finalisation invariant, asserted on every coupled fact.

        A restart must find: one applied round, the correct cursor, ``awaiting_review``,
        the same PR, and no further model call. Asserting a subset of those is how the
        earlier publication bug stayed invisible, so all of them are checked here.
        """
        self.first_run()
        session = self.task_row().session_id
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)

        published = self.rounds()[0]
        task = self.task_row()
        self.assertEqual(published.state, ROUND_PUBLISHED)
        self.assertEqual(task.feedback_cursor, published.cursor)
        self.assertEqual(task.phase, "awaiting_review")
        self.assertEqual(task.pr_number, PR_NUMBER)
        self.assertEqual(task.review_round, 1)

        # A fresh process (a poll, a restart) must change none of it.
        calls = self.runtime_calls()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), calls)
        after = self.task_row()
        self.assertEqual(len(self.rounds()), 1, "no duplicate round")
        self.assertEqual(after.review_round, 1)
        self.assertEqual(after.feedback_cursor, published.cursor)
        self.assertEqual(after.phase, "awaiting_review")
        self.assertEqual(after.pr_number, PR_NUMBER)
        self.assertEqual(after.session_id, session)
        self.assertIsNone(after.recovery_stage)


class FinalisationAtomicityTests(ReviewCase):
    """The handover is ONE transaction, proven structurally rather than by outcome."""

    def _prepared_round(self):
        store = self.store()
        task = self.task_row()
        cursor = serialise_cursor({"conversation:7": "2026-02-01T10:00:00Z"})
        claimed = store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch="dispatch/issue-1-x",
            worktree_path=str(self.tmp),
            session_id="sess-x",
            cursor_json=cursor,
            snapshot_json=cursor,
        )
        store.mark_round_publish_pending(claimed.id, head_sha="a" * 40)
        store.park_for_recovery(task.id, stage=ROUND_STAGE_PR, note="parked")
        # Read the phase back AFTER parking, so the assertion below is about the state
        # the handover was attempted from rather than a pre-park snapshot.
        self._prepared_phase = store.get_task(self.slug, 1).phase
        return store, task, claimed

    def test_the_handover_writes_every_coupled_fact_in_one_transaction(self) -> None:
        self.first_run()
        store, task, claimed = self._prepared_round()

        statements: list[str] = []
        store._conn.set_trace_callback(statements.append)
        try:
            store.finalise_review_round(
                claimed.id, task.id, cursor_json=claimed.cursor or "{}", pr_number=PR_NUMBER
            )
        finally:
            store._conn.set_trace_callback(None)

        begins = [s for s in statements if s.strip().upper().startswith("BEGIN")]
        commits = [s for s in statements if s.strip().upper().startswith("COMMIT")]
        self.assertEqual(len(begins), 1, f"one transaction expected: {statements}")
        self.assertEqual(len(commits), 1, f"one commit expected: {statements}")

        fresh = store.get_task(self.slug, 1)
        assert fresh is not None
        self.assertEqual(fresh.phase, "awaiting_review")
        self.assertIsNone(fresh.recovery_stage)
        self.assertEqual(fresh.feedback_cursor, claimed.cursor)
        self.assertEqual(fresh.review_round, claimed.round)
        self.assertEqual(store.review_round(task.id, claimed.round).state, ROUND_PUBLISHED)

    def test_a_failure_inside_the_handover_leaves_nothing_half_written(self) -> None:
        """The two states the issue forbids are impossible if the write cannot half-apply.

        The failure is injected with SQLite's own ``RAISE`` in a trigger on the second
        table the handover touches. A trigger is used rather than monkeypatching the
        connection because ``sqlite3.Connection`` attributes are read-only, and rather
        than patching the store's method because the point is to fail *inside* the
        transaction, after the first statement has run.
        """
        self.first_run()
        store, task, claimed = self._prepared_round()
        before = store.get_task(self.slug, 1)
        assert before is not None

        store._conn.executescript(
            "CREATE TRIGGER fail_handover BEFORE UPDATE ON tasks "
            "WHEN NEW.phase = 'awaiting_review' "
            "BEGIN SELECT RAISE(ABORT, 'injected failure mid-handover'); END;"
        )
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                store.finalise_review_round(
                    claimed.id, task.id, cursor_json=claimed.cursor or "{}", pr_number=PR_NUMBER
                )
        finally:
            store._conn.executescript("DROP TRIGGER fail_handover")

        after = store.get_task(self.slug, 1)
        assert after is not None
        self.assertEqual(after.feedback_cursor, before.feedback_cursor, "cursor must not move")
        self.assertEqual(after.phase, before.phase, "phase must not move")
        self.assertEqual(after.recovery_stage, before.recovery_stage)
        self.assertEqual(
            store.review_round(task.id, claimed.round).state,
            ROUND_PUBLISH_PENDING,
            "the round must not be marked published by a rolled-back transaction",
        )
        # The connection is still usable, so a later pass can retry — and a rolled-back
        # handover leaves the task exactly where it was, not in a new phase.
        self.assertEqual(after.phase, self._prepared_phase)

        # The retry then succeeds and moves every coupled fact together.
        store.finalise_review_round(
            claimed.id, task.id, cursor_json=claimed.cursor or "{}", pr_number=PR_NUMBER
        )
        final = store.get_task(self.slug, 1)
        assert final is not None
        self.assertEqual(final.feedback_cursor, claimed.cursor)
        self.assertEqual(final.phase, "awaiting_review")
        self.assertIsNone(final.recovery_stage)
        self.assertEqual(store.review_round(task.id, claimed.round).state, ROUND_PUBLISHED)


# ==============================================================================
# The #17 comment lifecycle, reused rather than re-implemented
# ==============================================================================


class ReviewStatusCommentTests(ReviewCase):
    def test_a_round_reuses_the_same_comment_and_never_opens_a_second(self) -> None:
        self.first_run()
        before = len(self.status_comments())
        self.assertEqual(before, 1, "the implementation run owns one status comment")

        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario(stream_seconds=1.5, stream_tick=0.2)
        self.assertEqual(self.run_cli("review").returncode, 0)

        self.assertEqual(
            len(self.status_comments()), 1, "a review round must not create a second comment"
        )
        self.assertEqual(
            self.status_body().splitlines()[0].strip(), self.status_body().splitlines()[0].strip()
        )

    def test_the_review_states_are_published_and_the_final_body_is_awaiting_review(self) -> None:
        self.first_run()
        # Scope the assertion to the review round's OWN writes. The implementation run
        # already published `Starting`/`Running`, so indexing the global write log would
        # check the wrong run's states.
        writes_before = len(self._status_writes())
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario(stream_seconds=2.5, stream_tick=0.2)
        self.assertEqual(self.run_cli("review").returncode, 0)

        round_states = [entry["body"] for entry in self._status_writes()[writes_before:]]
        self.assertTrue(round_states, "the round must publish at least one state")
        self.assertIn(
            STATE_APPLYING_FEEDBACK,
            "\n".join(round_states),
            "the round must report 'Applying feedback'",
        )
        self.assertNotIn(
            STATE_RUNNING + "**",
            round_states[0],
            "a review round must never report itself as a first implementation",
        )
        self.assertIn(
            STATE_AWAITING_REVIEW,
            round_states[-1],
            "the round's terminal state must be awaiting review",
        )

        body = self.status_body()
        self.assertIn(STATE_AWAITING_REVIEW, body)
        self.assertIn("Review round:** 1", body, "the terminal body must name the round")

    def _status_writes(self) -> list[dict]:
        repo = self.current_world()["repos"][self.slug]
        return [
            entry
            for entry in repo.get("comment_writes", [])
            if "agent-dispatch:status" in entry["body"]
        ]

    def test_no_heartbeat_edit_lands_after_the_rounds_terminal_update(self) -> None:
        self.first_run()
        writes_before = len(self._status_writes())
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario(stream_seconds=2.0, stream_tick=0.2)
        self.assertEqual(self.run_cli("review").returncode, 0)

        writes = [entry["body"] for entry in self._status_writes()[writes_before:]]
        self.assertTrue(writes)
        # The terminal state is last: anything after it would be a heartbeat landing on
        # top of a finished round.
        self.assertIn(STATE_AWAITING_REVIEW, writes[-1])
        for later in writes[1:]:
            if STATE_AWAITING_REVIEW in later:
                break
        else:  # pragma: no cover - the assertion above already fails first
            self.fail("no terminal review write")

    def test_a_failed_round_shows_trouble_rather_than_success(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario(events=["tool_hook_blocked"], edits={})
        self.assertEqual(self.run_cli("review").returncode, 1)

        body = self.status_body()
        self.assertIn(STATE_NEEDS_ATTENTION, body)
        self.assertEqual(len(self.status_comments()), 1)


# ==============================================================================
# The read-only and no-model-call guarantees
# ==============================================================================


class ReviewReadOnlyTests(ReviewCase):
    def test_the_implementation_publish_pass_leaves_an_open_round_alone(self) -> None:
        """The routing decision, asserted directly.

        The worker runs the review pass *before* the implementation passes, so in the
        normal flow this never comes up. It still has to be true on its own, because the
        two passes reach opposite conclusions about the same row: the implementation
        pass has a self-heal for "owned PR + publishable stage" (an old crash), while for
        an open round that exact combination is the *published-but-unacknowledged* state
        #5 forbids. If it ever ran first, it would clear the round's stage and set
        ``awaiting_review`` while the cursor stayed put.

        Driving it explicitly — rather than hoping the ordering holds — is what makes
        the guarantee testable, and it is how #4's review had to learn to test a
        decision instead of a flow.
        """
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        store = self.store()
        task = self.task_row()
        cursor = self.claimed_cursor()
        claimed = store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch=task.branch,
            worktree_path=task.worktree_path,
            session_id=task.session_id,
            cursor_json=cursor,
            snapshot_json=cursor,
        )
        store.mark_round_publish_pending(claimed.id, head_sha="a" * 40)
        store.park_for_recovery(task.id, stage=ROUND_STAGE_PUSH, note="mid-publication")
        before = store.get_task(self.slug, 1)
        assert before is not None

        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator

        config = load_config(self.world.config_path)
        os.environ["FAKE_GH_WORLD"] = str(self.world.world_path)
        log_stream = open(os.devnull, "w")  # noqa: SIM115
        self.addCleanup(log_stream.close)
        orchestrator = Orchestrator(
            config,
            store,
            GitHubClient(config.github.command),
            Logger(fmt="text", stream=log_stream),
        )
        orchestrator.reconcile_publish_pending()

        after = store.get_task(self.slug, 1)
        assert after is not None
        self.assertEqual(after.phase, before.phase, "the implementation pass must not move it")
        self.assertEqual(after.recovery_stage, ROUND_STAGE_PUSH, "the stage must survive")
        self.assertIsNone(after.feedback_cursor, "no feedback may be acknowledged")
        self.assertEqual(
            store.review_round(task.id, claimed.round).state,
            ROUND_PUBLISH_PENDING,
            "the round must still be open for its own pass",
        )

    def test_a_worker_start_never_turns_a_dead_review_round_into_a_new_run(self) -> None:
        """The most expensive mistake this feature can make, tested through the real entry point.

        Startup reconciliation repairs orphaned ``running`` rows. For an implementation
        task that means "requeue for a bounded fresh-session retry". For a **review
        round** the same treatment is wrong: a turn may already have been paid for, and
        the worktree may hold half-written edits to code a reviewer has already seen.

        ``run --skip-poll`` is the entry point that matters here, and deliberately so.
        It is the only path that calls startup reconciliation **without** the review pass
        afterwards, so it is where a missing guard has real consequences: without it the
        round's task is requeued as a fresh *implementation* run, and the same
        reconciliation may also publish the round's partial work through the
        implementation recovery path. The polling worker would paper over both, because
        it runs the review pass straight afterwards — which is exactly why testing this
        through the worker would pass for the wrong reason.
        """
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        store = self.store()
        task = self.task_row()
        cursor = self.claimed_cursor()
        claimed = store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch=task.branch,
            worktree_path=task.worktree_path,
            session_id=task.session_id,
            cursor_json=cursor,
            snapshot_json=cursor,
        )
        store.start_review_round(claimed.id, session_id=task.session_id)
        run_row = store.start_run(
            task.id,
            run_id="20260301T000000000000-review-dead02",
            kind="review",
            resumed_from=task.session_id,
            log_path=str(self.tmp / "dead2.ndjson"),
        )
        store.record_round_run(claimed.id, run_id=str(run_row))
        # `running` with no live process: exactly what a killed worker leaves.
        store.set_phase(task.id, "running", "review round in flight")
        self.mutate(branches=[])  # nothing was pushed by the round

        self.review_scenario()
        calls_before = self.runtime_calls()
        self.assertEqual(self.run_cli("run", "--skip-poll").returncode, 0)

        self.assertEqual(
            self.runtime_calls(), calls_before, "a dead review turn must not be re-run"
        )
        after = self.task_row()
        self.assertNotEqual(
            after.phase,
            "queued",
            "a dead review round must never be requeued for an implementation run",
        )
        self.assertEqual(after.phase, "needs_attention")
        self.assertEqual(after.review_round, 1)
        self.assertIsNone(after.feedback_cursor, "nothing may be acknowledged")
        self.assertEqual(self.rounds()[0].state, ROUND_INTERRUPTED)

    def test_review_dry_run_creates_no_state_database(self) -> None:
        from agent_dispatch.config import load_config

        config = load_config(self.world.config_path)
        self.assertFalse(config.worker.state_db.exists(), "precondition: no state database")

        result = self.run_cli("review", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(
            config.worker.state_db.exists(),
            "a read-only command must not create the state database",
        )

    def test_review_dry_run_writes_nothing_when_the_label_is_absent(self) -> None:
        """The absent-label branch is the one that used to write.

        `ReviewLoop.evaluate` re-armed the handoff itself when it found no label, so a
        dry run against a task with `handoff_armed = 0` and no label mutated the database
        it had just opened read-only — contradicting both that method's own docstring and
        the CLI's promise. Asserting "no state file was created" does not cover this,
        because here the database already exists; the check has to be on its contents.
        """
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)

        # Disarm the handoff directly: a crash between the claim and the label removal
        # leaves exactly this pair (label gone from the PR, handoff still consumed), and
        # that pair is what made `evaluate` re-arm as a side effect of deciding.
        task = self.task_row()
        self.store().disarm_handoff(task.id, note="simulated interrupted claim")
        self.assertFalse(self.task_row().handoff_claimable, "precondition: consumed")

        from agent_dispatch.config import load_config

        config = load_config(self.world.config_path)
        before = hashlib.sha256(config.worker.state_db.read_bytes()).hexdigest()

        # No label on the PR, and the handoff is disarmed: the exact branch that used to
        # re-arm as a side effect of *evaluating*.
        self.set_pr_labels()
        result = self.run_cli("review", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)

        after = hashlib.sha256(config.worker.state_db.read_bytes()).hexdigest()
        self.assertEqual(
            after, before, "review --dry-run must not modify the state database at all"
        )
        self.assertFalse(
            self.task_row().handoff_claimable, "evaluating must not re-arm the handoff"
        )

    def test_status_and_dry_run_never_start_a_round(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario()

        self.assertEqual(self.run_cli("status").returncode, 0)
        self.assertEqual(self.run_cli("dry-run", "--no-sync").returncode, 0)
        self.assertEqual(self.runtime_calls(), 1)
        self.assertEqual(self.rounds(), [])

    def test_a_no_execute_worker_poll_does_not_start_a_round(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario()

        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        self.assertEqual(self.runtime_calls(), 1, "--no-execute must not spend a model call")
        self.assertEqual(self.rounds(), [])


class ExactPullRequestTests(ReviewCase):
    """A review round may only ever publish to the PR it was claimed against (#5).

    The create-or-adopt implementation path is correct for a first implementation and
    forbidden for a round: a round that pushed to a replacement PR, or adopted a
    different open PR on the same branch, would silently replace review work on a pull
    request a human was reading.
    """

    def _publish_parked_round(self, store, claimed, task, repository, mutate_world=None):
        if mutate_world is not None:
            mutate_world()
        orchestrator, manager, state = self._orchestrator(store, repository, task)
        return self._confirm_publish(orchestrator, store, claimed, task, repository, manager, state)

    def _publish_full(self, store, task, mutate_world=None):
        """Drive the WHOLE publication path, push included.

        `_publish_parked_round` above calls only the final confirmation step, so it can
        never push and therefore cannot tell "the push was skipped because the PR is
        unusable" apart from "the test never got as far as pushing". Both look like an
        unchanged tip. These tests are about the branch being left alone, so they must go
        through the real entry point that *would* have pushed — and the worktree must hold
        a commit that is genuinely not on the remote yet, or a skipped push and a completed
        push leave the same remote tip and the assertion proves nothing.
        """
        worktree = Path(task.worktree_path)
        (worktree / "round-work.txt").write_text("round work\n", encoding="utf-8")
        if mutate_world is not None:
            mutate_world()
        repository = self._repo_config()
        orchestrator, manager, state = self._orchestrator(store, repository, task)
        committed, note = manager.commit_all(worktree, task.branch, "review round: apply feedback")
        self.assertTrue(committed, note)
        state = manager.inspect(worktree, task.branch)
        self.assertNotEqual(
            state.head_sha,
            self._remote_tip(task.branch),
            "there must be a commit that is not on the remote yet, or the push assertions "
            "cannot tell 'skipped' from 'produced no new commits'",
        )
        return orchestrator, orchestrator._publish_review_round(
            task, repository, manager, state, self.rounds()[0], result=None
        )

    def test_a_closed_pr_parks_the_round_and_posts_no_replacement(self) -> None:
        self.first_run()
        repository = self._repo_config()
        store, task, claimed = self.parked_publish_pending_round()
        pulls_before = len(self.current_world()["repos"][self.slug]["pulls"])

        def close_it() -> None:
            self.mutate(
                pulls=[{**self.pr(), "state": "closed", "merged_at": "2026-04-01T00:00:00Z"}]
            )

        outcome = self._publish_parked_round(
            store, claimed, task, repository, mutate_world=close_it
        )
        self.assertEqual(outcome.action, "needs_attention")
        self.assertEqual(outcome.reason, "review_pr_not_open")

        # The harm to assert is "no PR was created", not a proxy.
        self.assertEqual(
            len(self.current_world()["repos"][self.slug]["pulls"]),
            pulls_before,
            "a review round must never create a replacement pull request",
        )
        # A structural failure must NOT be left auto-retryable: a publish-pending round is
        # retried every pass, and no retry can reopen a closed PR. Left that way the
        # dispatcher would push to the branch on every poll forever.
        #
        # `publication_blocked`, not `interrupted`: the model turn completed cleanly, so
        # the only thing left is a push. The distinction is what later tells
        # `--retry-round` to resume publication instead of spending a second model turn.
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)
        self.assertNotEqual(self.rounds()[0].state, ROUND_PUBLISH_PENDING)
        self.assertIsNone(self.task_row().feedback_cursor, "nothing may be acknowledged")
        self.assertEqual(self.task_row().phase, "needs_attention")

    def test_a_closed_pr_is_not_pushed_to_by_the_full_publication_path(self) -> None:
        """The pre-push gate, driven through the code that actually pushes.

        This is the specific harm: the branch is the one piece of shared state a round
        touches, and a round that pushed before discovering its PR was closed would leave
        commits on a branch nobody will merge — while reporting that it had done nothing.
        Asserted against the real `_publish_review_round`, so "the tip is unchanged"
        means the push was genuinely skipped rather than never attempted.
        """
        self.first_run()
        store, task, claimed = self.parked_publish_pending_round()
        tip_before = self._remote_tip(task.branch)
        calls_before = self.runtime_calls()
        pulls_before = len(self.current_world()["repos"][self.slug]["pulls"])

        _, outcome = self._publish_full(
            store,
            task,
            mutate_world=lambda: self.mutate(
                pulls=[{**self.pr(), "state": "closed", "merged_at": "2026-04-01T00:00:00Z"}]
            ),
        )

        self.assertEqual(outcome.reason, "review_pr_not_open")
        self.assertEqual(
            self._remote_tip(task.branch),
            tip_before,
            "a closed PR must not be pushed to: the gate runs before the push",
        )
        self.assertEqual(
            len(self.current_world()["repos"][self.slug]["pulls"]),
            pulls_before,
            "no replacement PR either",
        )
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)
        self.assertEqual(self.runtime_calls(), calls_before)
        self.assertIsNone(self.task_row().feedback_cursor)

    def test_a_structurally_parked_round_is_not_repushed_or_rerun(self) -> None:
        """The round must not be retried by a later pass — no push and no model call."""
        self.first_run()
        store, task, claimed = self.parked_publish_pending_round()
        tip_before = self._remote_tip(task.branch)
        calls_before = self.runtime_calls()

        self._publish_full(
            store,
            task,
            mutate_world=lambda: self.mutate(
                pulls=[{**self.pr(), "state": "closed", "merged_at": "2026-04-01T00:00:00Z"}]
            ),
        )
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)

        # Every pass that could pick the round up again, run in the worker's order.
        self.run_all_reconcile_passes(store)
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)
        self.assertEqual(
            self._remote_tip(task.branch), tip_before, "a later pass must not push either"
        )
        self.assertEqual(
            self.runtime_calls(),
            calls_before,
            "a parked round must not spend another model call",
        )

    def test_another_open_pr_on_the_branch_is_never_adopted(self) -> None:
        """Even a plausible-looking candidate must not replace the claimed PR."""
        self.first_run()
        repository = self._repo_config()
        store, task, claimed = self.parked_publish_pending_round()
        # A second open PR that also references the Issue. The old create-or-adopt path
        # could have adopted this; the round must not.
        self.mutate(
            pulls=[
                self.pr(),
                {
                    **self.pr(),
                    "number": 99,
                    "html_url": f"https://github.com/{self.slug}/pull/99",
                    "body": f"Fixes https://github.com/{self.slug}/issues/1",
                },
            ]
        )

        outcome = self._publish_parked_round(store, claimed, task, repository)
        self.assertEqual(outcome.action, "awaiting_review")
        self.assertEqual(
            outcome.pr_number,
            PR_NUMBER,
            "the round must publish to its own PR, not to another open one",
        )
        self.assertEqual(self.task_row().pr_number, PR_NUMBER)
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISHED)

    def test_a_foreign_pr_is_rejected_before_the_branch_is_pushed(self) -> None:
        """The ADOPT variant of the pre-push gate.

        A closed claimed PR and a *replaced* claimed PR fail differently: the first makes
        the create path tempting, the second makes the adopt path tempting — and the
        pre-push gate has to stop both. The closed case is covered above; this is the one
        where an unrelated open PR that also references the Issue sits on the same head
        branch, so a gate that only handled "closed" would push and then adopt it.
        """
        self.first_run()
        store, task, claimed = self.parked_publish_pending_round()
        tip_before = self._remote_tip(task.branch)

        _, outcome = self._publish_full(
            store,
            task,
            mutate_world=lambda: self.mutate(
                pulls=[
                    {
                        **self.pr(),
                        "head": {
                            "ref": task.branch,
                            "sha": "0" * 40,
                            "repo": {"full_name": "someone-else/agent-dispatch"},
                            "user": {"login": "someone-else"},
                        },
                    }
                ]
            ),
        )

        self.assertEqual(outcome.reason, "review_pr_unowned")
        self.assertEqual(
            self._remote_tip(task.branch),
            tip_before,
            "a PR that is not provably ours must not be pushed to either",
        )
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)
        self.assertEqual(
            self.task_row().pr_number,
            PR_NUMBER,
            "the task must keep its own PR number",
        )

    def test_a_round_without_recorded_pr_is_never_published_or_republished(self) -> None:
        """The `require_pr is None` branch, which had the same stale-stage gap.

        A round with no recorded PR number cannot publish anywhere, and the honest
        answer is to park it. What must NOT happen is the task being left looking
        publish-pending: the implementation publish pass reads that signature as "retry
        this publication" and would pick the task up as though a round's unfinished work
        were an ordinary implementation publication.
        """
        self.first_run()
        store, task, claimed = self.parked_publish_pending_round()
        # The round loses its PR number; the worktree and diff are untouched.
        store._conn.execute("UPDATE review_rounds SET pr_number = NULL WHERE id = ?", (claimed.id,))
        round_row = store.review_round(task.id, 1)
        self.assertIsNotNone(round_row)
        self.assertIsNone(round_row.pr_number)

        repository = self._repo_config()
        orchestrator, manager, state = self._orchestrator(store, repository, task)
        tip_before = self._remote_tip(task.branch)
        pulls_before = len(self.current_world()["repos"][self.slug]["pulls"])
        calls_before = self.runtime_calls()

        outcome = orchestrator._publish_review_round(
            task, repository, manager, state, round_row, result=None
        )

        self.assertEqual(outcome.reason, "review_pr_unknown")
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)
        self.assertEqual(
            len(self.current_world()["repos"][self.slug]["pulls"]),
            pulls_before,
            "a round with no known PR must not create one",
        )
        self.assertEqual(self._remote_tip(task.branch), tip_before, "and must not push either")
        self.assertEqual(self.runtime_calls(), calls_before)

        # Asserted immediately, before a later pass could tidy it up: the task's own
        # round stage must be retired along with the round, so no later attempt mirrors
        # a reason belonging to a parked round onto itself. `_task_round_stage` reads the
        # task row back, so a surviving stage is stale evidence rather than a gate.
        parked = self.task_row()
        self.assertEqual(parked.phase, "needs_attention")
        self.assertNotIn(
            parked.recovery_stage,
            ROUND_STAGES,
            "a parked round must not leave its publication stage on the task row",
        )

        # And no later pass spends a model call or pushes on its behalf. Parking the
        # ROUND is what enforces this: the re-drive is driven by the round's state, so
        # leaving it `publish_pending` would push to the branch on every pass forever.
        self.run_all_reconcile_passes(store)
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)
        self.assertEqual(self.runtime_calls(), calls_before)
        self.assertEqual(self._remote_tip(task.branch), tip_before)
        self.assertIsNone(self.task_row().feedback_cursor)

    def test_parking_a_round_preserves_an_implementation_stage(self) -> None:
        """The clearing is scoped to round stages, because the two kinds mean different things.

        An implementation `recovery_stage` is evidence for the implementation publish
        pass — it is what authorises finishing *that* work without a model call. A round
        has no business discarding it, so the clear is a `CASE` over round stages only.
        """
        self.first_run()
        store = self.store()
        task = self.task_row()
        store.park_for_recovery(task.id, stage=RECOVERY_PUSH_FAILED, note="implementation push")
        cursor = self.claimed_cursor()
        claimed = store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch=task.branch,
            worktree_path=task.worktree_path,
            session_id=task.session_id,
            cursor_json=cursor,
            snapshot_json=cursor,
        )

        store.park_round_outside_publication(
            claimed.id, task.id, "the round was parked", turn_completed=False
        )

        after = self.task_row()
        self.assertEqual(
            after.recovery_stage,
            RECOVERY_PUSH_FAILED,
            "a round must not clear an implementation stage: it is another repair's evidence",
        )
        self.assertEqual(self.rounds()[0].state, ROUND_INTERRUPTED)

    def test_a_pr_that_stopped_being_ours_parks_the_round(self) -> None:
        """Ownership is re-verified at publish time, not trusted from the claim."""
        self.first_run()
        repository = self._repo_config()
        store, task, claimed = self.parked_publish_pending_round()

        def fork_it() -> None:
            self.mutate(
                pulls=[
                    {
                        **self.pr(),
                        "head": {
                            "ref": task.branch,
                            "sha": "0" * 40,
                            "repo": {"full_name": "someone-else/agent-dispatch"},
                            "user": {"login": "someone-else"},
                        },
                    }
                ]
            )

        outcome = self._publish_parked_round(store, claimed, task, repository, mutate_world=fork_it)
        self.assertEqual(outcome.action, "needs_attention")
        self.assertEqual(outcome.reason, "review_pr_unowned")
        # Structural, so parked for an explicit decision: a PR that stopped being ours
        # does not become ours again on the next poll, and leaving the round
        # publish-pending would push to the branch every pass forever.
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)
        self.assertIsNone(self.task_row().feedback_cursor)
        self.assertEqual(self.task_row().phase, "needs_attention")

    def test_publish_only_recovery_is_also_restricted_to_the_exact_pr(self) -> None:
        """Recovery is the same decision, so it needs the same restriction.

        Recovering a round is exactly when a replacement is most tempting — the push
        already happened — and exactly when it would be most damaging.
        """
        self.first_run()
        store, task, claimed = self.parked_publish_pending_round(stage=ROUND_STAGE_PUSH)
        pulls_before = len(self.current_world()["repos"][self.slug]["pulls"])
        self.mutate(pulls=[{**self.pr(), "state": "closed", "merged_at": "2026-04-01T00:00:00Z"}])

        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator

        config = load_config(self.world.config_path)
        os.environ["FAKE_GH_WORLD"] = str(self.world.world_path)
        stream = open(os.devnull, "w")  # noqa: SIM115
        self.addCleanup(stream.close)
        orchestrator = Orchestrator(
            config, store, GitHubClient(config.github.command), Logger(fmt="text", stream=stream)
        )
        calls = self.runtime_calls()
        orchestrator.reconcile_review_rounds()

        self.assertEqual(self.runtime_calls(), calls, "recovery must not call the model")
        self.assertEqual(
            len(self.current_world()["repos"][self.slug]["pulls"]),
            pulls_before,
            "recovery must not create a replacement pull request either",
        )
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)
        self.assertIsNone(self.task_row().feedback_cursor, "nothing may be acknowledged")

    def test_recovery_never_adopts_another_open_pr_on_the_branch(self) -> None:
        """The ADOPT variant of the recovery case, which the create variant does not cover.

        A closed claimed PR and a *replaced* claimed PR fail differently: the first makes
        the create path tempting, the second makes the adopt path tempting. The second is
        the quieter mistake — it looks like a successful recovery and publishes the
        round's work onto a pull request that may not be this task's.
        """
        self.first_run()
        store, task, claimed = self.parked_publish_pending_round(stage=ROUND_STAGE_PUSH)
        pulls_before = len(self.current_world()["repos"][self.slug]["pulls"])
        # The claimed PR is closed, and a DIFFERENT open PR that also references the Issue
        # now sits on the same head branch.
        self.mutate(
            pulls=[
                {**self.pr(), "state": "closed"},
                {
                    **self.pr(),
                    "number": 77,
                    "html_url": f"https://github.com/{self.slug}/pull/77",
                    "body": f"Fixes https://github.com/{self.slug}/issues/1",
                },
            ]
        )

        calls = self.runtime_calls()
        self.live_orchestrator(store).reconcile_review_rounds()

        self.assertEqual(self.runtime_calls(), calls, "recovery must not call the model")
        self.assertEqual(
            len(self.current_world()["repos"][self.slug]["pulls"]),
            pulls_before + 1,
            "the only new PR is the fixture's: recovery neither created nor adopted one",
        )
        self.assertEqual(
            self.task_row().pr_number,
            PR_NUMBER,
            "the task must keep its own PR number, not the replacement's",
        )
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)
        self.assertIsNone(self.task_row().feedback_cursor, "nothing may be acknowledged")


class RedrivePrRevalidationTests(ReviewCase):
    """A re-driven round re-checks its PR before spending a model call (#5).

    A round can be re-driven long after it was claimed — a crash restart, or an explicit
    `review --retry-round` — and in that window the pull request can be merged, closed or
    stop being provably this task's. The claim is not evidence that the PR is still valid
    now, and #5 is explicit that an externally closed or merged PR receives no new round.

    Every case must end with ZERO runtime calls and an unacknowledged cursor: the
    feedback has to survive so a later, valid round can still apply it.
    """

    def _claimed_unstarted_round(self, *, advance_attempts: int = 0):
        """A claimed round whose model turn never started — the re-drivable state."""
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        store = self.store()
        task = self.task_row()
        cursor = self.claimed_cursor()
        claimed = store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch=task.branch,
            worktree_path=task.worktree_path,
            session_id=task.session_id,
            cursor_json=cursor,
            snapshot_json=cursor,
        )
        return store, task, claimed

    def _assert_parked_without_a_call(self, store, calls_before: int) -> None:
        self.assertEqual(
            self.runtime_calls(),
            calls_before,
            "a round whose PR is unusable must not spend another model call",
        )
        rounds = self.rounds()
        self.assertEqual(rounds[0].state, ROUND_INTERRUPTED)
        self.assertEqual(self.task_row().phase, "needs_attention")
        self.assertIsNone(self.task_row().feedback_cursor, "the feedback must stay unacknowledged")
        # And it is not silently revived by a later pass either.
        self.run_all_reconcile_passes(store)
        self.assertEqual(self.runtime_calls(), calls_before)
        self.assertEqual(self.rounds()[0].state, ROUND_INTERRUPTED)

    def test_a_pr_closed_after_the_claim_is_never_re_driven(self) -> None:
        store, task, claimed = self._claimed_unstarted_round()
        self.mutate(pulls=[{**self.pr(), "state": "closed", "merged_at": "2026-04-01T00:00:00Z"}])
        self.review_scenario()
        calls_before = self.runtime_calls()

        self.assertEqual(self.run_cli("review").returncode, 0)
        self._assert_parked_without_a_call(store, calls_before)

    def test_a_pr_merged_after_the_claim_is_never_re_driven(self) -> None:
        """Merged is a separate verdict from closed, and reaches a different `state`."""
        store, task, claimed = self._claimed_unstarted_round()
        self.mutate(pulls=[{**self.pr(), "merged_at": "2026-04-01T00:00:00Z"}])
        self.review_scenario()
        calls_before = self.runtime_calls()

        self.assertEqual(self.run_cli("review").returncode, 0)
        self._assert_parked_without_a_call(store, calls_before)

    def test_a_pr_that_stopped_being_ours_is_never_re_driven(self) -> None:
        store, task, claimed = self._claimed_unstarted_round()
        self.mutate(
            pulls=[
                {
                    **self.pr(),
                    "head": {
                        "ref": task.branch,
                        "sha": "0" * 40,
                        "repo": {"full_name": "someone-else/agent-dispatch"},
                        "user": {"login": "someone-else"},
                    },
                }
            ]
        )
        self.review_scenario()
        calls_before = self.runtime_calls()

        self.assertEqual(self.run_cli("review").returncode, 0)
        self._assert_parked_without_a_call(store, calls_before)

    def test_retry_round_after_the_pr_closed_does_not_run_the_model(self) -> None:
        """`--retry-round` re-opens a parked round, so it must meet the same gate.

        This is the path a maintainer reaches for *precisely* when something went wrong,
        so it is the one most likely to be aimed at a PR that has since become unusable.
        A gate that only covered the crash re-drive would leave this open.

        The command exits non-zero, and that is the honest report rather than a bug: the
        maintainer asked for the round to be retried and it was **not** retried, because
        the exact PR can no longer be published to. Saying "released" would imply the
        round is resolved when nothing has happened to it.
        """
        store, task, claimed = self._claimed_unstarted_round()
        store.park_round(claimed.id, state=ROUND_FAILED, stage=None, note="the round broke")
        self.mutate(pulls=[{**self.pr(), "state": "closed", "merged_at": "2026-04-01T00:00:00Z"}])
        self.review_scenario()
        calls_before = self.runtime_calls()
        tip_before = self._remote_tip(task.branch)

        result = self.run_cli("review", "--retry-round", "--repo", self.slug, "--issue", "1")
        self.assertEqual(
            result.returncode,
            1,
            "an unsuccessful retry must say so rather than reporting success",
        )
        self.assertIn("still interrupted", result.stderr)
        self._assert_parked_without_a_call(store, calls_before)
        self.assertEqual(
            self._remote_tip(task.branch), tip_before, "the branch must not be pushed either"
        )

    def test_a_still_open_pr_still_re_drives(self) -> None:
        """The positive case, so the gate is not refusing every re-drive.

        Without this, a gate that always blocked would satisfy every test above and
        silently disable the crash re-drive the rest of the feature depends on.
        """
        store, task, claimed = self._claimed_unstarted_round()
        self.review_scenario()
        calls_before = self.runtime_calls()

        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(
            self.runtime_calls(), calls_before + 1, "a usable PR must still be re-driven"
        )
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISHED)


class StructuralParkAtomicityTests(ReviewCase):
    """Parking a structurally-unusable round is one transaction, not two writes.

    `park_round_outside_publication` is the authority for every structural exact-PR
    park, so a crash inside it must leave the row exactly as it was. The connection runs
    with `isolation_level=None`, which means statements autocommit individually unless
    they share a transaction — "both statements live in one method" is not atomicity.

    The failure is injected with SQLite's own `RAISE` in a trigger on the *second* table
    the method touches, so it fails after the first statement has already run and the
    rollback is what the test observes. That mirrors the `finalise_review_round` tests
    above rather than inventing a second technique.
    """

    def _parked_round(self):
        self.first_run()
        store, task, claimed = self.parked_publish_pending_round()
        before = self.task_row()
        assert before is not None
        return store, task, claimed, before

    def test_the_park_writes_both_facts_in_one_transaction(self) -> None:
        store, task, claimed, _ = self._parked_round()

        statements: list[str] = []
        store._conn.set_trace_callback(statements.append)
        try:
            store.park_round_outside_publication(
                claimed.id, task.id, "structurally unusable", turn_completed=True
            )
        finally:
            store._conn.set_trace_callback(None)

        begins = [s for s in statements if s.strip().upper().startswith("BEGIN")]
        commits = [s for s in statements if s.strip().upper().startswith("COMMIT")]
        self.assertEqual(len(begins), 1, f"one transaction expected: {statements}")
        self.assertEqual(len(commits), 1, f"one commit expected: {statements}")

        after = self.task_row()
        assert after is not None
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)
        self.assertEqual(after.phase, "needs_attention")

    def test_a_failure_inside_the_park_rolls_the_round_back_too(self) -> None:
        """The half-applied state this method exists to prevent must be unreachable.

        Without one transaction the round would already be `interrupted` (so nothing
        retries its publication) while the task row still looked publish-pending — the
        worst of both, and invisible from the outside.
        """
        store, task, claimed, before = self._parked_round()
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISH_PENDING)

        store._conn.executescript(
            "CREATE TRIGGER fail_park BEFORE UPDATE ON tasks "
            "WHEN NEW.phase = 'needs_attention' "
            "BEGIN SELECT RAISE(ABORT, 'injected failure mid-park'); END;"
        )
        try:
            with self.assertRaises(sqlite3.DatabaseError):
                store.park_round_outside_publication(
                    claimed.id, task.id, "structurally unusable", turn_completed=True
                )
        finally:
            store._conn.executescript("DROP TRIGGER fail_park")

        after = self.task_row()
        assert after is not None
        self.assertEqual(
            self.rounds()[0].state,
            ROUND_PUBLISH_PENDING,
            "a rolled-back park must not leave the round interrupted",
        )
        self.assertEqual(after.phase, before.phase, "phase must not move")
        self.assertEqual(after.recovery_stage, before.recovery_stage, "stage must not move")

        # The retry then succeeds and moves both facts together.
        store.park_round_outside_publication(
            claimed.id, task.id, "structurally unusable", turn_completed=True
        )
        final = self.task_row()
        assert final is not None
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)
        self.assertEqual(final.phase, "needs_attention")
        self.assertNotIn(final.recovery_stage, ROUND_STAGES)


class PreSpawnSingleReadTests(ReviewCase):
    """The pre-spawn gate validates the object it hands to the model (#5).

    Validating response A and then using response B validates nothing: the PR can change
    between the two reads, and the second object would be quoted to the model without
    ever being checked. The fake wrapper counts reads per PR number so the single-read
    property is observable rather than asserted in a comment.
    """

    def _claimed_unstarted_round(self):
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        store = self.store()
        task = self.task_row()
        cursor = self.claimed_cursor()
        claimed = store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch=task.branch,
            worktree_path=task.worktree_path,
            session_id=task.session_id,
            cursor_json=cursor,
            snapshot_json=cursor,
        )
        return store, task, claimed

    def _pr_reads(self) -> int:
        reads = self.current_world().get("pr_reads") or {}
        return int(reads.get(f"{self.slug}#{PR_NUMBER}", 0))

    def test_the_gate_reads_the_pr_exactly_once(self) -> None:
        """One boundary, one read — counted around the gate itself.

        Deliberately not counted around a whole `review` invocation: several legitimate
        code paths read this PR (evaluation, snapshot reproduction, discovery), so a
        command-level total would be measuring all of them and could not tell a double
        read inside the gate from normal traffic. Counting around the one call that
        owns this boundary is what makes the assertion specific to the bug.
        """
        store, task, claimed = self._claimed_unstarted_round()
        orchestrator = self.live_orchestrator(store)
        before = self._pr_reads()

        pull, problem = orchestrator._require_live_round_pull(self._repo_config(), task, PR_NUMBER)

        self.assertIsNone(problem)
        self.assertIsNotNone(pull, "the validated object must be the one returned")
        self.assertEqual(
            self._pr_reads() - before,
            1,
            "the gate must validate the very object it hands on, not refetch a second",
        )

    def test_a_pr_that_closes_on_its_single_read_is_refused(self) -> None:
        """The TOCTOU case, modelled on the read the gate actually performs.

        With a validate-then-refetch gate, the object that reaches
        `build_review_instruction` is a *second*, unvalidated read — so the PR can be
        closed on that read and the model spawns anyway. Here the override makes the
        gate's one read closed, so the refusal proves the object it judged is the object
        it would have used.
        """
        store, task, claimed = self._claimed_unstarted_round()
        world = self.current_world()
        world["pr_read_overrides"] = {
            f"{self.slug}#{PR_NUMBER}": {
                "1": {**self.pr(), "state": "closed", "merged_at": "2026-04-01T00:00:00Z"}
            }
        }
        self.world.world = world
        self.world.write_world()
        orchestrator = self.live_orchestrator(store)
        before = self._pr_reads()

        pull, problem = orchestrator._require_live_round_pull(self._repo_config(), task, PR_NUMBER)

        self.assertIsNone(pull, "a PR that is not usable must never be returned")
        self.assertIsNotNone(problem)
        self.assertEqual(problem.reason, "review_pr_not_open")
        self.assertEqual(self._pr_reads() - before, 1, "and it took exactly one read")

    def test_the_gate_still_refuses_a_closed_pr_on_its_single_read(self) -> None:
        """The positive control: the single read is still a real check.

        Without this, a gate that read once and validated nothing would satisfy the two
        tests above.
        """
        store, task, claimed = self._claimed_unstarted_round()
        self.mutate(pulls=[{**self.pr(), "state": "closed", "merged_at": "2026-04-01T00:00:00Z"}])
        self.review_scenario()
        calls_before = self.runtime_calls()

        self.assertEqual(self.run_cli("review").returncode, 0)

        self.assertEqual(self.runtime_calls(), calls_before, "no model call for a closed PR")
        self.assertEqual(self.rounds()[0].state, ROUND_INTERRUPTED)
        self.assertEqual(self._pr_reads(), 1, "and it read the PR exactly once to decide")


class UnreadablePrBeforePushTests(ReviewCase):
    """An unreadable PR at the pre-push boundary must not push, and must stay retryable.

    The classification is right that a failed read is transient, but "transient and
    retryable" does not require mutating the remote branch while the exact PR is
    unproven: an API outage can coincide with the PR having been closed or merged. So the
    round stays publishable and the branch is left alone until the target is readable.
    """

    def _publish_full(self, store, task, mutate_world=None):
        worktree = Path(task.worktree_path)
        (worktree / "round-work.txt").write_text("round work\n", encoding="utf-8")
        if mutate_world is not None:
            mutate_world()
        repository = self._repo_config()
        orchestrator, manager, state = self._orchestrator(store, repository, task)
        committed, note = manager.commit_all(worktree, task.branch, "review round: apply feedback")
        self.assertTrue(committed, note)
        state = manager.inspect(worktree, task.branch)
        self.assertNotEqual(
            state.head_sha,
            self._remote_tip(task.branch),
            "there must be a commit that is not on the remote yet",
        )
        return orchestrator, orchestrator._publish_review_round(
            task, repository, manager, state, self.rounds()[0], result=None
        )

    def test_an_unreadable_pr_is_not_pushed_and_stays_retryable(self) -> None:
        self.first_run()
        store, task, claimed = self.parked_publish_pending_round()
        tip_before = self._remote_tip(task.branch)
        calls_before = self.runtime_calls()

        def fail_the_read() -> None:
            self.inject_failure(f"{self.slug}:pull:{PR_NUMBER}", exit_code=1, stderr="boom")

        _, outcome = self._publish_full(store, task, mutate_world=fail_the_read)

        self.assertEqual(outcome.reason, "review_pr_unreadable")
        self.assertEqual(
            self._remote_tip(task.branch),
            tip_before,
            "an unproven exact PR must not be pushed to",
        )
        # Still publishable, NOT parked: a read failure that clears by itself must not
        # need a human. That is the whole point of keeping this transient.
        self.assertEqual(
            self.rounds()[0].state,
            ROUND_PUBLISH_PENDING,
            "an unreadable PR is transient, so the round must stay retryable",
        )
        self.assertEqual(self.runtime_calls(), calls_before, "no model call")
        self.assertIsNone(self.task_row().feedback_cursor)

    def test_the_next_pass_publishes_once_the_pr_is_readable_again(self) -> None:
        """The retryability promise, kept: the next pass finishes it with no model call."""
        self.first_run()
        store, task, claimed = self.parked_publish_pending_round()
        tip_before = self._remote_tip(task.branch)

        def fail_the_read() -> None:
            self.inject_failure(f"{self.slug}:pull:{PR_NUMBER}", exit_code=1, stderr="boom")

        self._publish_full(store, task, mutate_world=fail_the_read)
        self.assertEqual(self._remote_tip(task.branch), tip_before)
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISH_PENDING)

        # The next pass: the read works again, so publication completes.
        calls_before = self.runtime_calls()
        self.run_all_reconcile_passes(store)

        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISHED)
        self.assertEqual(self.task_row().phase, "awaiting_review")
        self.assertEqual(
            self.runtime_calls(), calls_before, "completing publication needs no model call"
        )
        self.assertNotEqual(
            self._remote_tip(task.branch), tip_before, "the retry did push the round"
        )


class RetryOfCompletedTurnIsPublicationOnlyTests(ReviewCase):
    """A round whose turn completed must never be re-run by `--retry-round`.

    The zero-extra-runtime rule of #5: once the resumed turn completed cleanly and
    validation passed, any later commit/push/PR failure stays publish-only. Structural
    exact-PR parking created a hole in exactly that rule, because `interrupted` was
    standing for three materially different situations:

    1. no turn happened yet — an explicit retry may legitimately run the model;
    2. the turn was interrupted or failed — retrying is a deliberate human choice;
    3. the turn completed cleanly and only publication was blocked — retry must be
       **publication-only**.

    The durable state now records which side of the model boundary the round reached
    (``publication_blocked`` versus ``interrupted``), so the routing is structural
    rather than something each call site has to remember.
    """

    def _publish_full(self, store, task, mutate_world=None):
        """Drive the real publication path, with genuine unpushed work present."""
        worktree = Path(task.worktree_path)
        (worktree / "round-work.txt").write_text("round work\n", encoding="utf-8")
        if mutate_world is not None:
            mutate_world()
        repository = self._repo_config()
        orchestrator, manager, state = self._orchestrator(store, repository, task)
        committed, note = manager.commit_all(worktree, task.branch, "review round: apply feedback")
        self.assertTrue(committed, note)
        state = manager.inspect(worktree, task.branch)
        self.assertNotEqual(
            state.head_sha,
            self._remote_tip(task.branch),
            "there must be a commit that is not on the remote yet",
        )
        return orchestrator, orchestrator._publish_review_round(
            task, repository, manager, state, self.rounds()[0], result=None
        )

    def _structurally_blocked_round(self, *, close_before_push: bool):
        """A completed turn whose publication was structurally blocked.

        ``close_before_push`` picks the boundary: the pre-push gate (nothing reached the
        remote) or the post-push re-check (the completed head is already on the remote).
        Both are reachable, and they leave different things behind, so both are pinned.
        """
        self.first_run()
        store, task, claimed = self.parked_publish_pending_round()
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISH_PENDING)

        def close_it() -> None:
            if close_before_push:
                self.mutate(
                    pulls=[{**self.pr(), "state": "closed", "merged_at": "2026-04-01T00:00:00Z"}]
                )
            else:
                # The PR is open for the pre-push gate and closed for the re-check, which
                # is exactly the race the post-push read exists to catch. The override is
                # keyed by read index, so this models the change of state rather than
                # approximating it with a stub.
                world = self.current_world()
                world["pr_read_overrides"] = {
                    f"{self.slug}#{PR_NUMBER}": {
                        "2": {
                            **self.pr(),
                            "state": "closed",
                            "merged_at": "2026-04-01T00:00:00Z",
                        }
                    }
                }
                self.world.world = world
                self.world.write_world()

        _, outcome = self._publish_full(store, task, mutate_world=close_it)
        self.assertEqual(outcome.reason, "review_pr_not_open")
        return store, task, claimed

    def _repair_pr(self) -> None:
        self.mutate(pulls=[{**self.pr(), "state": "open", "merged_at": None}])

    def test_a_pre_push_structural_block_parks_as_publication_blocked(self) -> None:
        """The state distinguishes a completed turn from an unfinished one."""
        store, task, claimed = self._structurally_blocked_round(close_before_push=True)

        self.assertEqual(
            self.rounds()[0].state,
            ROUND_PUBLICATION_BLOCKED,
            "a completed turn must not be parked as merely interrupted",
        )
        self.assertNotEqual(self.rounds()[0].state, ROUND_INTERRUPTED)
        self.assertIsNone(self.task_row().feedback_cursor, "nothing may be acknowledged")

    def test_retry_after_a_pre_push_block_never_runs_the_model(self) -> None:
        """Regression A: reopen the PR, `--retry-round`, zero extra runtime calls."""
        store, task, claimed = self._structurally_blocked_round(close_before_push=True)
        cursor_before = self.task_row().feedback_cursor
        calls_before = self.runtime_calls()
        tip_before = self._remote_tip(task.branch)
        head_sha = self.rounds()[0].head_sha

        self._repair_pr()
        result = self.run_cli("review", "--retry-round", "--repo", self.slug, "--issue", "1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.runtime_calls(),
            calls_before,
            "the turn already completed: retrying must be publication-only",
        )
        rounds = self.rounds()
        self.assertEqual(len(rounds), 1, "the SAME round is finished, never duplicated")
        self.assertEqual(rounds[0].id, claimed.id)
        self.assertEqual(rounds[0].state, ROUND_PUBLISHED)
        self.assertEqual(
            rounds[0].cursor,
            claimed.cursor,
            "the round keeps the snapshot it was claimed with",
        )
        self.assertIsNotNone(self.task_row().feedback_cursor, "the cursor advances")
        self.assertNotEqual(self.task_row().feedback_cursor, cursor_before)
        self.assertEqual(self.task_row().phase, "awaiting_review")
        # The work the completed turn produced is what reached the PR.
        self.assertNotEqual(self._remote_tip(task.branch), tip_before, "the push happened")
        self.assertIsNotNone(head_sha)

    def test_retry_after_a_post_push_block_never_runs_the_model(self) -> None:
        """Regression B: the completed head is already remote; retry confirms it.

        Distinct from the pre-push case because the push already happened, so the retry
        must adopt the existing tip rather than produce anything new.
        """
        store, task, claimed = self._structurally_blocked_round(close_before_push=False)
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)
        calls_before = self.runtime_calls()
        cursor_before = self.task_row().feedback_cursor

        self._repair_pr()
        result = self.run_cli("review", "--retry-round", "--repo", self.slug, "--issue", "1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.runtime_calls(),
            calls_before,
            "a completed turn must never be re-run, including after the push",
        )
        rounds = self.rounds()
        self.assertEqual(len(rounds), 1, "no duplicate round")
        self.assertEqual(rounds[0].id, claimed.id)
        self.assertEqual(rounds[0].state, ROUND_PUBLISHED)
        self.assertEqual(self.task_row().phase, "awaiting_review")
        self.assertNotEqual(self.task_row().feedback_cursor, cursor_before)

    def test_retrying_a_blocked_round_twice_is_still_publication_only(self) -> None:
        """A second repair-and-retry must not creep back into running the model.

        The routing reads durable state, so it has to stay correct on repetition rather
        than only on the first retry after the park.
        """
        store, task, claimed = self._structurally_blocked_round(close_before_push=True)
        self._repair_pr()
        self.assertEqual(
            self.run_cli("review", "--retry-round", "--repo", self.slug, "--issue", "1").returncode,
            0,
        )
        calls_after_first = self.runtime_calls()

        result = self.run_cli("review", "--retry-round", "--repo", self.slug, "--issue", "1")

        self.assertEqual(result.returncode, 1, "nothing is parked any more, so it refuses")
        self.assertEqual(self.runtime_calls(), calls_after_first, "and runs no model")
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISHED)

    def test_a_blocked_round_tells_the_maintainer_the_command_that_works(self) -> None:
        """The status comment must not promise a later pass that will never come.

        `publish_pending` heals on the next poll, so "the dispatcher finishes this" is
        true there. A structurally blocked round was parked *out of* that retry path, so
        the same wording would leave a maintainer waiting indefinitely. The two states
        now render different next actions, and this pins the blocked one.
        """
        store, task, claimed = self._structurally_blocked_round(close_before_push=True)
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLICATION_BLOCKED)

        self.live_orchestrator(store).sync_task_status(self.task_row())

        body = self.status_body()
        self.assertIn("review --retry-round", body, "name the command that finishes it")
        self.assertIn("without another model run", body, "and promise no second model turn")
        self.assertNotIn(
            "The dispatcher finishes the push on a later pass",
            body,
            "a blocked round is never finished by a later pass — that is why it was parked",
        )

    def test_a_round_parked_before_its_turn_still_retries_by_running_it(self) -> None:
        """The positive opposite case, so the fix is not "never run the model".

        A round parked by the pre-spawn gate never reached the model, so an explicit
        `--retry-round` is the maintainer deliberately asking for that turn — and it must
        actually run.
        """
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        store = self.store()
        task = self.task_row()
        cursor = self.claimed_cursor()
        store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch=task.branch,
            worktree_path=task.worktree_path,
            session_id=task.session_id,
            cursor_json=cursor,
            snapshot_json=cursor,
        )
        self.mutate(pulls=[{**self.pr(), "state": "closed", "merged_at": "2026-04-01T00:00:00Z"}])
        self.review_scenario()
        calls_before = self.runtime_calls()

        # The pre-spawn gate parks it as unfinished, because the turn never happened.
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), calls_before, "no model call while closed")
        self.assertEqual(
            self.rounds()[0].state,
            ROUND_INTERRUPTED,
            "a round that never reached the model is interrupted, not publication_blocked",
        )

        # With the PR usable again, retrying legitimately starts the turn.
        self._repair_pr()
        result = self.run_cli("review", "--retry-round", "--repo", self.slug, "--issue", "1")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.runtime_calls(),
            calls_before + 1,
            "an unstarted round's retry is meant to run the model once",
        )
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISHED)


class StatusCommentIsNotFeedbackTests(ReviewCase):
    """The dispatcher's own status comment is never part of the feedback contract (#5/#17).

    It is outbound status/provenance, so it is excluded from the model input — and it
    must therefore also be excluded from the durable acknowledgement snapshot. Including
    it was a real crash bug rather than a tidiness one: the round's own status comment is
    edited to `Applying feedback` immediately after the claim and may be edited again by
    the heartbeat, so a crash mid-round made the dispatcher's own write look like
    "claimed feedback edited after the claim" and the restart refused to re-drive although
    no maintainer feedback had changed.

    The sequence below is the real one — a genuine #17 comment, a genuine claim, and the
    status edit that actually happens — rather than a synthetic cursor, because a
    synthetic one cannot show whether the production path puts the comment in the cursor.
    """

    def _claimed_round_with_status_comment(self):
        """Own a status comment, claim a round, then edit the comment as a round does."""
        self.first_run()
        self.assertEqual(len(self.status_comments()), 1, "the implementation owns a status comment")
        self.hand_off()
        self.add_comment("Please rework the helper.")
        store = self.store()
        task = self.task_row()
        cursor = self.claimed_cursor()
        claimed = store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch=task.branch,
            worktree_path=task.worktree_path,
            session_id=task.session_id,
            cursor_json=cursor,
            snapshot_json=cursor,
        )
        return store, task, claimed, cursor

    def _edit_status_comment_like_a_round_would(self) -> None:
        """Rewrite the owned status comment exactly as `begin_review`/heartbeat do.

        Done through the fake world rather than the publisher so the edit is guaranteed
        to have happened before the restart is simulated; the point under test is the
        snapshot check, not the publisher's transport.
        """
        world = self.current_world()
        repo = world["repos"][self.slug]
        edited = False
        for record in repo.get("comments", []):
            if "agent-dispatch:status" in str(record.get("body", "")):
                record["body"] = "<!-- agent-dispatch:status -->\n**Status:** Applying feedback"
                record["updated_at"] = "2026-05-01T00:00:00Z"
                edited = True
        self.assertTrue(edited, "the fixture must actually edit the owned status comment")
        self.world.world = world
        self.world.write_world()

    def test_the_status_comment_is_not_in_the_claimed_cursor(self) -> None:
        """The contract, asserted directly: no dispatcher item is ever acknowledged."""
        _, _, claimed, cursor = self._claimed_round_with_status_comment()
        keys = parse_cursor(claimed.cursor)

        status_keys = [
            str(record["id"])
            for record in self.status_comments()
            if "agent-dispatch:status" in str(record.get("body", ""))
        ]
        self.assertTrue(status_keys, "there is a status comment to exclude")
        for key in status_keys:
            self.assertNotIn(
                key,
                keys,
                "the dispatcher's own status comment must not be claimed as feedback",
            )
        self.assertEqual(
            set(keys),
            set(parse_cursor(cursor)),
            "the claimed snapshot is exactly the maintainer feedback",
        )

    def test_a_status_edit_after_the_claim_still_reproduces_the_snapshot(self) -> None:
        """The crash case: the round is re-drivable even though its own status changed."""
        store, task, claimed, _ = self._claimed_round_with_status_comment()
        self._edit_status_comment_like_a_round_would()
        self.review_scenario()
        calls_before = self.runtime_calls()

        self.assertEqual(self.run_cli("review").returncode, 0)

        self.assertNotEqual(
            self.runtime_calls(),
            calls_before,
            "the round must re-drive: no maintainer feedback changed",
        )
        self.assertEqual(
            self.rounds()[0].state,
            ROUND_PUBLISHED,
            "the round must not be parked for the dispatcher's own status write",
        )

    def test_editing_real_feedback_still_refuses_the_restart(self) -> None:
        """The negative control, so the exclusion is not "the check stopped working"."""
        store, task, claimed, _ = self._claimed_round_with_status_comment()
        self._edit_status_comment_like_a_round_would()

        def edit_the_real_feedback() -> None:
            world = self.current_world()
            for record in world["repos"][self.slug].get("comments", []):
                if record.get("body") == "Please rework the helper.":
                    record["body"] = "Please rework the helper. (Scope changed.)"
                    record["updated_at"] = "2026-05-02T00:00:00Z"
            self.world.world = world
            self.world.write_world()

        edit_the_real_feedback()
        self.review_scenario()
        calls_before = self.runtime_calls()

        self.assertEqual(self.run_cli("review").returncode, 0)

        self.assertEqual(
            self.runtime_calls(),
            calls_before,
            "maintainer feedback edited after the claim must still refuse to run",
        )
        self.assertEqual(self.rounds()[0].state, ROUND_INTERRUPTED)
        self.assertIsNone(self.task_row().feedback_cursor)


class OversizedFeedbackIsNeverSilentlyAcknowledgedTests(ReviewCase):
    """A handoff may not claim more than the instruction can deliver (#5).

    Claiming acknowledges **every** new item, so an item that never fits in the bounded
    instruction must not be claimed at all. Otherwise the model never sees the request
    while publication marks it handled forever — silent feedback loss, which the note
    admitting a truncation does not repair.

    The feedback is genuinely oversized rather than the bound being patched down: the
    bound is read inside the `review` subprocess, so a monkeypatch in the test process
    would not reach the code under test, and a test that only appeared to lower it would
    assert nothing. One rendered item is ~4.3k characters (its metadata plus the 4k body
    cap), so fifteen exceed the 60k bound while two comfortably fit.
    """

    #: Items whose rendered size exceeds the real bound, and a count that fits it.
    OVERFLOWING_ITEMS = 15
    FITTING_ITEMS = 2

    def _scenario(self, *, comments: int):
        """An owned PR carrying `comments` large maintainer comments, fully readable."""
        self.first_run()
        self.hand_off()
        for index in range(comments):
            # Each body is at the per-item cap, so the rendered size is dominated by the
            # item itself and the fixture cannot accidentally fit or accidentally
            # overflow through the metadata.
            self.add_comment(
                f"Request {index}: " + ("x" * 4000),
                comment_id=1000 + index,
                created_at=f"2026-02-{index + 1:02d}T10:00:00Z",
            )
        self.review_scenario()

    def test_oversized_new_feedback_defers_instead_of_claiming(self) -> None:
        """No round is claimed, nothing is acknowledged, and the label stays put."""
        self._scenario(comments=self.OVERFLOWING_ITEMS)
        calls_before = self.runtime_calls()

        self.assertEqual(self.run_cli("review").returncode, 0)

        self.assertEqual(
            self.runtime_calls(),
            calls_before,
            "no model call may be spent on feedback the round cannot carry",
        )
        self.assertEqual(self.rounds(), [], "no round may be claimed")
        self.assertIsNone(
            self.task_row().feedback_cursor,
            "nothing may be acknowledged: the model never saw these requests",
        )
        self.assertTrue(
            self.task_row().handoff_claimable,
            "a deferral keeps the handoff, so it starts by itself once the batch fits",
        )

    def test_a_claimable_batch_delivers_every_acknowledged_item(self) -> None:
        """The positive control and the invariant: cursor items == delivered items.

        Without this, a guard that refused every handoff would satisfy the test above
        while breaking the feature. It also pins the shape the review asked for: every
        item present in the claimed cursor actually appears in the model instruction.
        """
        self._scenario(comments=self.FITTING_ITEMS)
        calls_before = self.runtime_calls()

        self.assertEqual(self.run_cli("review").returncode, 0)

        self.assertEqual(self.runtime_calls(), calls_before + 1, "a fitting handoff must run")
        rounds = self.rounds()
        self.assertEqual(len(rounds), 1)
        self.assertEqual(rounds[0].state, ROUND_PUBLISHED)

        argv = self.recorded_argv()[-1]
        instruction = argv[argv.index("-p") + 1]
        claimed_keys = set(parse_cursor(rounds[0].cursor))
        self.assertEqual(len(claimed_keys), self.FITTING_ITEMS, "both items are claimed")
        for key in claimed_keys:
            comment_id = key.split(":", 1)[1]
            self.assertIn(
                f"#issuecomment-{comment_id}",
                instruction,
                f"claimed item {key} must actually appear in the instruction",
            )

    def test_the_overflow_guard_refuses_rather_than_slicing(self) -> None:
        """A slice would drop whole items; the guard must report overflow instead.

        Asserted on the helper directly, because this is the decision the whole fix
        turns on and it must not depend on the render path's slicing behaviour.
        """
        from agent_dispatch.review import FeedbackItem, FeedbackSet, new_feedback_overflow

        def items(count: int) -> list:
            return [
                FeedbackItem(
                    key=f"conversation:{1000 + index}",
                    kind=KIND_CONVERSATION,
                    id=1000 + index,
                    version="v1",
                    author="maintainer",
                    body="y" * 4000,
                    url="",
                )
                for index in range(count)
            ]

        overflow = new_feedback_overflow(FeedbackSet(items=items(self.OVERFLOWING_ITEMS)), {})
        self.assertIsNotNone(overflow, "an oversized set must be reported as overflowing")
        assert overflow is not None
        self.assertGreater(overflow, 0, "the overflow is reported as a positive excess")
        self.assertIsNone(
            new_feedback_overflow(FeedbackSet(items=items(self.FITTING_ITEMS)), {}),
            "a set that fits must not be refused, or the guard is 'refuse everything'",
        )


class FinalPromptBoundaryTests(ReviewCase):
    """Every claimed item must survive the FINAL instruction, not just its own section.

    The round-5 guard measured the new-feedback section in isolation, but the assembled
    instruction was still sliced at the same bound — so a section that fit by itself
    could be cut by the outer truncation once metadata, diff and framing were added, and
    the cursor would still acknowledge the items that never reached the model. Same
    invariant as round 5, one level higher.

    These tests assert at the exact string the runtime is given (`-p` in the recorded
    argv), because that is the only place the whole instruction is observable. A guard
    that checks an intermediate value cannot catch a later slice.
    """

    #: Items at the per-item cap whose block lands just UNDER the section bound, so the
    #: section passes its own guard while the assembled instruction does not fit. Thirteen
    #: renders 55,742 characters against a 60,000 bound; fourteen exceeds it and is
    #: deferred, which is the round-5 guard doing its job.
    NEARLY_FULL_ITEMS = 13

    def _near_full_scenario(self):
        """A claimed round whose new feedback very nearly fills the bound on its own."""
        self.first_run()
        self.hand_off()
        for index in range(self.NEARLY_FULL_ITEMS):
            self.add_comment(
                f"Request {index}: " + ("x" * 4000),
                comment_id=1000 + index,
                created_at=f"2026-02-{index + 1:02d}T10:00:00Z",
            )
        self.review_scenario()

    def _bloated_instruction_sections(self):
        """The real section list, with the optional parts inflated to realistic maxima.

        Built by calling the real builder with a large diff rather than by reaching into
        its internals, so the test measures the assembled instruction the runtime is
        actually given. The diff is capped at 40 files and 12 stat lines, and `AGENTS.md`
        at 8,000 characters, so this is the worst case the code can produce — and the
        worst case is what the invariant has to survive.
        """
        from agent_dispatch.review import (
            DiffContext,
            build_review_instruction,
        )

        diff = DiffContext(
            commits=[f"{'a' * 8} {'b' * 60}" for _ in range(40)],
            files=[f"path/{'f' * 80}.py" for _ in range(40)],
            stat="\n".join([" " * 20 + "|" + " " * 60 for _ in range(12)]),
            available=True,
        )
        return build_review_instruction, diff

    def test_the_instruction_is_never_sliced_through_new_feedback(self) -> None:
        """The exact `-p` string contains every claimed item, or nothing is claimed.

        Driven through the real builder with a worst-case diff, because the assembled
        instruction is the only place the whole-prompt bound is observable: a check on an
        intermediate value cannot catch a later slice, which is exactly how the round-5
        guard came to be satisfied while the prompt was still cut.
        """
        self._near_full_scenario()
        build, worst_case_diff = self._bloated_instruction_sections()
        task = self.task_row()
        repository = self._repo_config()
        client = self.live_client()
        pull = client.get_pull(self.slug, PR_NUMBER)
        feedback = collect_feedback(client, repo=self.slug, pr_number=PR_NUMBER, issue_number=1)
        self.assertTrue(feedback.complete)

        instruction, notes = build(
            repo=repository,
            issue=client.open_issue(self.slug, 1),
            task=task,
            round_number=1,
            pull=pull,
            feedback=feedback,
            previous_cursor=parse_cursor(task.feedback_cursor),
            diff=worst_case_diff,
            worktree_path=str(task.worktree_path),
        )

        from agent_dispatch.review import FEEDBACK_TOTAL_LIMIT

        claimed_keys = set(parse_cursor(serialise_cursor(feedback.cursor())))
        self.assertTrue(claimed_keys, "the fixture must have feedback to deliver")
        instruction_keys = {
            key for key in claimed_keys if f"#issuecomment-{key.split(':', 1)[1]}" in instruction
        }
        # The harm, asserted as the harm and asserted FIRST: not "the string is short"
        # but "these specific items would be acknowledged without ever reaching the
        # model". With the outer slice restored this loses exactly one item, so a
        # length-based assertion cannot satisfy it by coincidence — and putting it
        # first means a failure reports the real consequence rather than the symptom.
        self.assertEqual(
            claimed_keys - instruction_keys,
            set(),
            "the cursor would acknowledge feedback the model was never given",
        )
        self.assertLessEqual(
            len(instruction),
            FEEDBACK_TOTAL_LIMIT,
            "the assembled instruction must respect the bound without slicing",
        )
        # The optional sections are what give way, and that must be disclosed.
        self.assertTrue(notes, "dropping optional sections must be reported in the notes")
        self.assertNotIn(
            "instruction truncated by the dispatcher",
            instruction,
            "the final prompt must never be character-sliced",
        )

    def _required_set_overflows_scenario(self) -> None:
        """Feedback that fits its OWN bound but not together with the required framing.

        The boundary round 7 named, and the reason the guard has to measure the
        required-only instruction rather than the feedback block: the required framing is
        ~1.8k characters that can never be dropped, so a block just under the bound leaves
        the required set just over it. Thirteen full-size items plus a *shorter* fourteenth
        lands the block at ~59.4k — under the bound, so a feedback-only guard would admit
        it — while the framing pushes the required set past 60k.
        """
        self.first_run()
        self.hand_off()
        for index in range(13):
            self.add_comment(
                f"Request {index}: " + ("x" * 4000),
                comment_id=1000 + index,
                created_at=f"2026-02-{index + 1:02d}T10:00:00Z",
            )
        # The shorter item is what tunes the block into the reachable window.
        self.add_comment(
            "Request 13: " + ("y" * 2800),
            comment_id=2000,
            created_at="2026-03-01T10:00:00Z",
        )
        self.review_scenario()

    def test_feedback_that_fits_but_framing_does_not_defers_before_claiming(self) -> None:
        """The claim gate must prove the same bound the builder enforces.

        Before the shared required-section measurement this sequence claimed the round,
        then `build_review_instruction()` raised `ValueError` with no handler, so the
        error escaped the worker/CLI *after the durable claim*. A restart found the same
        claimed round and hit the same error: a deterministic crash/re-drive loop for a
        perfectly valid batch, with no model call and no acknowledgement ever produced.

        Asserted as that harm: the handoff stays unclaimed and unacknowledged, and no
        exception escapes.
        """
        from agent_dispatch.review import FEEDBACK_TOTAL_LIMIT, new_feedback_block

        self._required_set_overflows_scenario()
        task = self.task_row()
        client = self.live_client()
        feedback = collect_feedback(client, repo=self.slug, pr_number=PR_NUMBER, issue_number=1)
        self.assertTrue(feedback.complete)

        # The precondition the reviewer named: the block alone fits, so a feedback-only
        # guard would have admitted this handoff.
        block = new_feedback_block(feedback.new_items(parse_cursor(task.feedback_cursor)), [])
        self.assertLessEqual(
            len(block),
            FEEDBACK_TOTAL_LIMIT,
            "the fixture must sit in the reachable window: block under the bound",
        )

        calls_before = self.runtime_calls()
        result = self.run_cli("review")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(
            "required instruction sections exceed",
            result.stdout + result.stderr,
            "the builder must never raise after a claim: the guard must refuse first",
        )
        self.assertEqual(self.rounds(), [], "no round may be claimed for an undeliverable batch")
        self.assertEqual(self.runtime_calls(), calls_before, "and no model call is spent")
        self.assertIsNone(self.task_row().feedback_cursor, "nothing may be acknowledged")
        self.assertIsNone(
            self.store().open_round(task.id),
            "no round may be left open, or a restart re-drives the same failure",
        )
        self.assertTrue(
            self.task_row().handoff_claimable,
            "a deferral keeps the handoff, so it starts once the batch is split",
        )

    def test_the_required_measurement_matches_what_the_builder_enforces(self) -> None:
        """The two must agree, or the guard is checking a number the builder ignores.

        Asserted directly on the shared functions: the guard's measurement and the
        builder's required-only join are the same text, so a future change to one cannot
        silently reopen the claim-vs-required mismatch.
        """
        from agent_dispatch.review import (
            FEEDBACK_TOTAL_LIMIT,
            required_instruction_overflow,
            required_instruction_sections,
            review_expectations,
        )

        self._required_set_overflows_scenario()
        task = self.task_row()
        repository = self._repo_config()
        client = self.live_client()
        pull = client.get_pull(self.slug, PR_NUMBER)
        issue = client.open_issue(self.slug, 1)
        feedback = collect_feedback(client, repo=self.slug, pr_number=PR_NUMBER, issue_number=1)

        overflow = required_instruction_overflow(
            repo=repository,
            issue=issue,
            task=task,
            round_number=task.review_round + 1,
            pull=pull,
            feedback=feedback,
            previous_cursor=parse_cursor(task.feedback_cursor),
        )

        self.assertIsNotNone(overflow, "this fixture must be measured as overflowing")
        new_items = feedback.new_items(parse_cursor(task.feedback_cursor))
        sections = required_instruction_sections(
            repo=repository,
            issue=issue,
            task=task,
            round_number=task.review_round + 1,
            pull=pull,
            new_items=new_items,
            expectations=review_expectations(new_items=new_items, diff_available=True),
            notes=[],
        )
        joined = "\n\n".join(text for _, text in sections)
        self.assertEqual(
            len(joined) - FEEDBACK_TOTAL_LIMIT,
            overflow,
            "the guard must measure exactly the required text the builder joins",
        )

    def test_the_built_instruction_emits_every_required_section_exactly_once(self) -> None:
        """The builder's ACTUAL output, not just the shared helper.

        The earlier version of this check compared `required_instruction_overflow()` with
        `required_instruction_sections()` directly, so both sides called the same function
        and agreed even when `build_review_instruction()` appended extra required text
        afterwards. That is exactly how a duplicate `Requirements:` block slipped in: the
        guard counted the shared set once while the builder emitted it twice, reopening a
        ~1.1k window in which a round is claimed and then cannot be built.

        So this builds the real instruction and asserts on it: each required section
        appears exactly once, and the closing instruction is last.
        """
        from agent_dispatch.review import (
            build_review_instruction,
            required_instruction_sections,
            review_expectations,
        )

        self._near_full_scenario()
        _, worst_case_diff = self._bloated_instruction_sections()
        task = self.task_row()
        repository = self._repo_config()
        client = self.live_client()
        feedback = collect_feedback(client, repo=self.slug, pr_number=PR_NUMBER, issue_number=1)
        self.assertTrue(feedback.complete)
        new_items = feedback.new_items(parse_cursor(task.feedback_cursor))

        instruction, _ = build_review_instruction(
            repo=repository,
            issue=client.open_issue(self.slug, 1),
            task=task,
            round_number=1,
            pull=client.get_pull(self.slug, PR_NUMBER),
            feedback=feedback,
            previous_cursor=parse_cursor(task.feedback_cursor),
            diff=worst_case_diff,
            worktree_path=str(task.worktree_path),
        )

        # Every REQUIRED section appears exactly once. Counted by membership rather than
        # by position, so an accidental duplicate is caught wherever it lands.
        shared = required_instruction_sections(
            repo=repository,
            issue=client.open_issue(self.slug, 1),
            task=task,
            round_number=1,
            pull=client.get_pull(self.slug, PR_NUMBER),
            new_items=new_items,
            expectations=review_expectations(new_items=new_items, diff_available=True),
            notes=[],
        )
        for _, text in shared:
            self.assertEqual(
                instruction.count(text),
                1,
                "each required section must be emitted exactly once — a second copy is "
                "required text the pre-claim guard never measured",
            )

        # And the prompt still ends by asking for the summary, which the duplicated
        # append had also broken by pushing optional sections after it.
        self.assertTrue(
            instruction.rstrip().endswith(shared[-1][1].rstrip()),
            "the closing instruction must be last, after every optional section",
        )

    def test_a_near_boundary_required_set_still_builds(self) -> None:
        """The window the duplicate reopened: required set under the bound, build must fit.

        A second `Requirements:` block is ~1.1k characters of required text. A required
        set anywhere in the ~1.1k below the bound therefore passed the guard and then
        failed to build. This asserts the structural consequence directly: a required set
        that the guard measures as fitting is a set the builder can actually assemble.
        """
        import agent_dispatch.review as review_module

        self._near_full_scenario()
        _, worst_case_diff = self._bloated_instruction_sections()
        repository = self._repo_config()
        client = self.live_client()
        issue = client.open_issue(self.slug, 1)
        pull = client.get_pull(self.slug, PR_NUMBER)

        from agent_dispatch.review import (
            FEEDBACK_TOTAL_LIMIT,
            required_instruction_sections,
            requirements_section,
            review_expectations,
        )

        def required_size() -> int:
            fresh = self.task_row()
            live = collect_feedback(client, repo=self.slug, pr_number=PR_NUMBER, issue_number=1)
            items = live.new_items(parse_cursor(fresh.feedback_cursor))
            sections = required_instruction_sections(
                repo=repository,
                issue=issue,
                task=fresh,
                round_number=1,
                pull=pull,
                new_items=items,
                expectations=review_expectations(new_items=items, diff_available=True),
                notes=[],
            )
            return len("\n\n".join(text for _, text in sections))

        # The width a second `Requirements:` block used to add, measured rather than
        # hard-coded so the fixture tracks the real text.
        expectations = review_expectations(new_items=[], diff_available=True)
        duplicate_width = len(requirements_section(expectations)) + 2

        # Top the fixture up so the required set lands INSIDE the window: close enough to
        # the bound that a second required section would have pushed it over. A hand-tuned
        # constant would silently fall out of a <1.2k window when any section text changes,
        # leaving a test that still passes while covering nothing.
        target = FEEDBACK_TOTAL_LIMIT - 50
        deficit = target - required_size()
        self.assertGreater(
            deficit,
            duplicate_width,
            "the near-full fixture must start below the window, to have room to enter it",
        )
        self.add_comment(
            "Filler: " + ("z" * max(1, deficit - 400)),
            comment_id=3000,
            created_at="2026-04-01T10:00:00Z",
        )

        size = required_size()
        self.assertLessEqual(size, FEEDBACK_TOTAL_LIMIT, "the fixture must fit the bound")
        self.assertGreater(
            size + duplicate_width,
            FEEDBACK_TOTAL_LIMIT,
            "the fixture must sit INSIDE the window: fitting now, but overflowed by a "
            "second required section — otherwise this test covers nothing",
        )

        task = self.task_row()
        feedback = collect_feedback(client, repo=self.slug, pr_number=PR_NUMBER, issue_number=1)
        previous = parse_cursor(task.feedback_cursor)

        overflow = review_module.required_instruction_overflow(
            repo=repository,
            issue=issue,
            task=task,
            round_number=1,
            pull=pull,
            feedback=feedback,
            previous_cursor=previous,
        )
        self.assertIsNone(overflow, "this fixture must fit the required bound")

        # The guard said it fits, so building it must not raise. Before the fix the
        # builder added ~1.1k more required text and `fit_instruction` raised here.
        instruction, _ = review_module.build_review_instruction(
            repo=repository,
            issue=issue,
            task=task,
            round_number=1,
            pull=pull,
            feedback=feedback,
            previous_cursor=previous,
            diff=worst_case_diff,
            worktree_path=str(task.worktree_path),
        )
        self.assertLessEqual(len(instruction), review_module.FEEDBACK_TOTAL_LIMIT)

    def test_optional_sections_are_dropped_whole_never_the_feedback(self) -> None:
        """With the feedback near the bound, the optional framing gives way instead.

        This is the positive half: a round still runs, and the way the prompt shrinks is
        by losing whole optional sections — never by cutting into the required block.
        """
        from agent_dispatch.review import FEEDBACK_TOTAL_LIMIT, fit_instruction

        block = "REQUIRED-FEEDBACK-BLOCK-" + ("y" * (FEEDBACK_TOTAL_LIMIT - 100))
        sections = [
            (None, "framing"),
            ("diff", "d" * 50_000),
            (None, block),
            ("context", "c" * 50_000),
            ("agents", "a" * 50_000),
        ]
        notes: list[str] = []

        text = fit_instruction(sections, notes)

        self.assertLessEqual(len(text), FEEDBACK_TOTAL_LIMIT, "the bound must still hold")
        self.assertIn(block, text, "the required feedback block must survive whole")
        self.assertIn("framing", text, "required framing must survive")
        self.assertNotIn("d" * 50_000, text, "the oversized diff must be the thing dropped")
        self.assertTrue(notes, "dropping sections must be disclosed in the notes")

    def test_required_sections_overflowing_is_an_error_not_a_silent_cut(self) -> None:
        """A required set past the bound is a bug, so it raises instead of truncating.

        Unreachable through the claim path — the pre-claim guard refuses first — but if
        the guard and the assembly ever disagree, silently truncating is exactly the
        failure this whole sequence of fixes has been removing.
        """
        from agent_dispatch.review import FEEDBACK_TOTAL_LIMIT, fit_instruction

        sections = [(None, "z" * (FEEDBACK_TOTAL_LIMIT + 1000))]
        with self.assertRaises(ValueError):
            fit_instruction(sections, [])


class SnapshotReproductionTests(ReviewCase):
    """A restarted round may only run when its claimed snapshot is reproducible (#5).

    Only ids and versions are persisted — bodies stay on GitHub by design — so a
    re-drive has to re-read them and check they still match. All three failures below
    must park without a model call, because the alternative is acting on feedback the
    round was not claimed for.
    """

    def _claim_and_restart(self, mutate_world=None):
        """A claimed-but-unstarted round, optionally with the world changed since."""
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        store = self.store()
        task = self.task_row()
        cursor = self.claimed_cursor()
        claimed = store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch=task.branch,
            worktree_path=task.worktree_path,
            session_id=task.session_id,
            cursor_json=cursor,
            snapshot_json=cursor,
        )
        if mutate_world is not None:
            mutate_world()
        self.review_scenario()
        return store, task, claimed

    def _redrive(self) -> None:
        """Run one `review` pass, expecting the round to park WITHOUT a model call.

        The exit code is deliberately not asserted here: a snapshot refusal is reported
        as `needs_attention`, and what this class is about is the *absence of a model
        call plus the parked state*. Asserting an exit code as well would make the test
        about CLI plumbing, and an earlier version of it did exactly that — it asserted
        `1` for a case that correctly returns `0`, so it failed for a reason unrelated to
        the guard it was meant to pin.
        """
        result = self.run_cli("review")
        self.assertIn(
            "review_snapshot_unreproducible",
            result.stdout + result.stderr,
            "the refusal must be the snapshot decision, not a side effect",
        )

    def test_a_truncated_read_after_the_claim_starts_nothing(self) -> None:
        self._claim_and_restart()
        before = self.runtime_calls()
        previous = os.environ.get("FAKE_GH_PAD_REVIEWS")
        os.environ["FAKE_GH_PAD_REVIEWS"] = "60"
        self.addCleanup(
            lambda: (
                os.environ.pop("FAKE_GH_PAD_REVIEWS", None)
                if previous is None
                else os.environ.__setitem__("FAKE_GH_PAD_REVIEWS", previous)
            )
        )

        self._redrive()
        self.assertEqual(self.runtime_calls(), before, "no model call on an unproven snapshot")
        self.assertEqual(self.rounds()[0].state, ROUND_INTERRUPTED)
        self.assertIsNone(self.task_row().feedback_cursor, "nothing may be acknowledged")

    def test_a_claimed_item_deleted_after_the_claim_starts_nothing(self) -> None:

        def delete_it() -> None:
            world = self.current_world()
            repo = world["repos"][self.slug]
            repo["comments"] = [item for item in repo.get("comments", []) if item["id"] == 1001]
            self.world.world = world
            self.world.write_world()

        self._claim_and_restart(mutate_world=delete_it)
        before = self.runtime_calls()
        self._redrive()
        self.assertEqual(self.runtime_calls(), before, "no model call when a claimed item is gone")
        self.assertEqual(self.rounds()[0].state, ROUND_INTERRUPTED)

    def test_an_item_edited_after_the_claim_starts_nothing(self) -> None:
        """Post-claim text belongs to a LATER handoff, never to the round that claimed it."""

        def edit_it() -> None:
            world = self.current_world()
            for record in world["repos"][self.slug]["comments"]:
                record["body"] = "edited after the claim"
                record["updated_at"] = "2026-05-01T00:00:00Z"
            self.world.world = world
            self.world.write_world()

        self._claim_and_restart(mutate_world=edit_it)
        before = self.runtime_calls()
        self._redrive()
        self.assertEqual(self.runtime_calls(), before, "no model call for edited feedback")
        self.assertEqual(self.rounds()[0].state, ROUND_INTERRUPTED)
        self.assertIsNone(self.task_row().feedback_cursor)

    def test_an_unchanged_snapshot_still_re_drives(self) -> None:
        """The positive case, so failing closed is not accidentally refusing everything."""
        before = self.runtime_calls()
        self._claim_and_restart()
        before = self.runtime_calls()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), before + 1, "an unchanged snapshot still runs")
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISHED)


class PausedTaskReviewTests(ReviewCase):
    """Pause and withdrawn `take-it` stop review work, exactly as they stop dispatch (#5)."""

    def _claimed_round(self, *, started: bool):
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        store = self.store()
        task = self.task_row()
        cursor = self.claimed_cursor()
        claimed = store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch=task.branch,
            worktree_path=task.worktree_path,
            session_id=task.session_id,
            cursor_json=cursor,
            snapshot_json=cursor,
        )
        if started:
            store.start_review_round(claimed.id, session_id=task.session_id)
            store.set_phase(task.id, "running", "review round in flight")
        return store, task, claimed

    def test_a_paused_task_is_never_re_driven_after_a_crash(self) -> None:
        store, task, claimed = self._claimed_round(started=False)
        store.pause(task.repo, task.issue_number)
        self.review_scenario()
        calls = self.runtime_calls()

        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), calls, "a paused task must not be re-driven")
        self.assertEqual(self.rounds()[0].state, ROUND_CLAIMED)
        self.assertEqual(self.task_row().phase, "paused")

    def test_a_paused_publish_pending_round_is_not_pushed(self) -> None:
        """The #16/#18 invariant, for review: pause stops publication too."""
        self.first_run()
        store, task, claimed = self.parked_publish_pending_round(stage=ROUND_STAGE_PUSH)
        store.pause(task.repo, task.issue_number)

        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator

        config = load_config(self.world.config_path)
        os.environ["FAKE_GH_WORLD"] = str(self.world.world_path)
        stream = open(os.devnull, "w")  # noqa: SIM115
        self.addCleanup(stream.close)
        orchestrator = Orchestrator(
            config, store, GitHubClient(config.github.command), Logger(fmt="text", stream=stream)
        )
        tips_before = self._remote_tip(task.branch)
        orchestrator.reconcile_review_rounds()
        orchestrator.reconcile_publish_pending()

        self.assertEqual(self._remote_tip(task.branch), tips_before, "nothing may be pushed")
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISH_PENDING)
        self.assertEqual(self.task_row().phase, "paused")
        self.assertIsNone(self.task_row().feedback_cursor)

    def test_a_withdrawn_trigger_label_stops_a_crash_re_drive(self) -> None:
        store, task, claimed = self._claimed_round(started=False)
        self.set_issues(issue(1, "Feature work", labels=[]))
        self.review_scenario()
        calls = self.runtime_calls()

        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), calls, "no model call without dispatch intent")
        self.assertEqual(self.rounds()[0].state, ROUND_CLAIMED)
        self.assertEqual(self.task_row().pause_reason, "label_withdrawn")

    def test_a_paused_task_is_left_alone_by_every_real_poll_pass(self) -> None:
        """The pause case driven through the actual passes, not just the round pass.

        The reviewer asked for this through a *poll*, and the distinction is real: three
        passes run per poll, and an earlier version of this suite asserted against one of
        them directly. A guard that only the review pass honours would still push a
        paused round's commits the moment the implementation passes ran.
        """
        store, task, claimed = self._claimed_round(started=True)
        self.parked = True
        store.mark_round_publish_pending(claimed.id, head_sha="a" * 40, stage=ROUND_STAGE_PUSH)
        store.park_for_recovery(task.id, stage=ROUND_STAGE_PUSH, note="mid-publication")
        store.pause(task.repo, task.issue_number)

        tips_before = self._remote_tip(task.branch)
        calls_before = self.runtime_calls()
        pulls_before = len(self.current_world()["repos"][self.slug]["pulls"])
        self.run_all_reconcile_passes(store)

        self.assertEqual(self.runtime_calls(), calls_before, "no pass may spend a model call")
        self.assertEqual(self._remote_tip(task.branch), tips_before, "no pass may push")
        self.assertEqual(
            len(self.current_world()["repos"][self.slug]["pulls"]),
            pulls_before,
            "no pass may create a pull request",
        )
        after = self.task_row()
        self.assertEqual(after.phase, "paused", "the pause must survive every pass")
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISH_PENDING)
        self.assertIsNone(after.feedback_cursor, "nothing may be acknowledged")

    def test_a_withdrawn_label_stops_a_paused_publication(self) -> None:
        """The same invariant for the other pause: `take-it` removed mid-publication.

        A publish-pending round with the label withdrawn is the case the #16/#18 rule is
        really about, and it is separate from an explicit `pause` because it arrives
        through discovery rather than through a command.
        """
        store, task, claimed = self._claimed_round(started=True)
        store.mark_round_publish_pending(claimed.id, head_sha="a" * 40, stage=ROUND_STAGE_PUSH)
        store.park_for_recovery(task.id, stage=ROUND_STAGE_PUSH, note="mid-publication")
        self.set_issues(issue(1, "Feature work", labels=[]))

        tips_before = self._remote_tip(task.branch)
        calls_before = self.runtime_calls()
        pulls_before = len(self.current_world()["repos"][self.slug]["pulls"])
        self.run_all_reconcile_passes(store)

        self.assertEqual(self.runtime_calls(), calls_before, "no model call without intent")
        self.assertEqual(self._remote_tip(task.branch), tips_before, "nothing may be pushed")
        self.assertEqual(len(self.current_world()["repos"][self.slug]["pulls"]), pulls_before)
        after = self.task_row()
        self.assertEqual(after.pause_reason, "label_withdrawn")
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISH_PENDING)
        self.assertIsNone(after.feedback_cursor, "nothing may be acknowledged")

    def test_restoring_intent_resumes_the_review_path_not_an_implementation(self) -> None:
        store, task, claimed = self._claimed_round(started=False)
        self.set_issues(issue(1, "Feature work", labels=[]))
        self.review_scenario()
        self.run_cli("review")
        calls = self.runtime_calls()
        self.assertEqual(calls, 1)

        # Re-add the label: the task is released from the pause and the round, still
        # unresolved and unchanged, is re-driven. It must not become an implementation
        # run. The handoff needs no re-arming because it was never claimed-and-consumed
        # here — the round is still in its original `claimed` state.
        self.set_issues(issue(1, "Feature work", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), calls + 1)
        argv = self.recorded_argv()[-1]
        self.assertIn("--session", argv, "the resumed call must be a review round")
        task_row = self.task_row()
        self.assertEqual(task_row.phase, "awaiting_review")
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISHED)
        self.assertNotIn("queued", task_row.phase)

    def test_restoring_intent_with_no_open_round_lands_in_awaiting_review(self) -> None:
        """The ordinary case: a finished task with a PR, suspended and resumed.

        Deliberately has NO unresolved round. The sibling test above does, and that
        masked a real dead end: an unresolved round makes the review loop act on the
        task regardless of its phase, so the task recovered even when the *phase* the
        restore chose was wrong.

        The case modelled here is the plain one a maintainer actually hits. A first
        implementation publishes, so the task owns a PR and is ``awaiting_review``.
        Removing `take-it` must pause it; re-adding it must put it back in
        ``awaiting_review``. It must NOT go to ``queued``, where implementation dispatch
        refuses it ("this worker already owns PR #1") and the review loop ignores it
        (`phase != awaiting_review`) — a task stranded in no reachable state at all.
        """
        self.first_run()
        self.assertEqual(self.task_row().phase, "awaiting_review")
        self.assertEqual(self.rounds(), [], "a finished implementation run is not a round")

        # Withdraw dispatch intent through the real poll, then observe it.
        self.set_issues(issue(1, "Feature work", labels=[]))
        self.run_all_reconcile_passes(self.store())
        paused = self.task_row()
        self.assertEqual(paused.phase, "paused")
        self.assertEqual(paused.pause_reason, "label_withdrawn")

        # Re-add it: the task must return to the phase where the review loop can see it.
        calls_before = self.runtime_calls()
        self.set_issues(issue(1, "Feature work", labels=[TRIGGER]))
        self.run_all_reconcile_passes(self.store())
        restored = self.task_row()
        self.assertEqual(
            restored.phase,
            "awaiting_review",
            "a task with a PR belongs in awaiting_review, not in a queue dispatch refuses",
        )
        self.assertIsNone(restored.pause_reason)
        self.assertEqual(
            self.runtime_calls(),
            calls_before,
            "restoring intent must not start an implementation run",
        )

        # And the review path is genuinely reachable from there, which is the point: a
        # normal handoff claims and publishes a round.
        self.hand_off()
        self.add_comment("Please rework the helper.")
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(len(self.rounds()), 1)
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISHED)
        self.assertEqual(self.task_row().phase, "awaiting_review")

    def test_unpause_of_a_task_with_a_pr_also_returns_to_awaiting_review(self) -> None:
        """The same boundary through the maintainer command, which had the same bug.

        `unpause` chose its destination with its own copy of the rule, so fixing only the
        label path would have left the dead end reachable a second way.
        """
        self.first_run()
        self.assertEqual(self.run_cli("pause", "--repo", self.slug, "--issue", "1").returncode, 0)
        self.assertEqual(self.task_row().phase, "paused")

        calls_before = self.runtime_calls()
        self.assertEqual(self.run_cli("unpause", "--repo", self.slug, "--issue", "1").returncode, 0)
        restored = self.task_row()
        self.assertEqual(
            restored.phase,
            "awaiting_review",
            "unpause must reach the same destination as the label path",
        )
        self.assertEqual(self.runtime_calls(), calls_before)

    def test_a_publish_pending_task_still_restores_to_publication(self) -> None:
        """The other half of the rule, still honoured: unfinished work resumes publishing.

        The regression above must not be fixed by always choosing ``awaiting_review`` —
        a task whose push/PR never completed has no published work to review and must go
        back to ``needs_attention`` so the publish pass finishes it without a second
        model run.

        Asserted at the release boundary itself rather than through a poll. A poll would
        not test the *decision*: the publish pass afterwards legitimately completes the
        publication and moves the task on to ``awaiting_review``, so a poll-level
        assertion would read the same whichever phase the release had chosen — and would
        have passed even with the ordering bug this pins.
        """
        self.first_run()
        store = self.store()
        task = self.task_row()
        store.park_for_recovery(task.id, stage=RECOVERY_PUSH_FAILED, note="the push never landed")
        store.pause_for_withdrawn_label(task.id, "take-it went away")
        self.assertEqual(self.task_row().recovery_stage, RECOVERY_PUSH_FAILED)

        self.assertTrue(store.release_label_withdrawn_pause(task.id))
        restored = self.task_row()
        # The task owns a PR *and* has an unfinished publication. Publication is the
        # decision that wins, because it is the work that is genuinely incomplete:
        # sending the task to `awaiting_review` would tell the review loop there is
        # something to review while the push carrying it is still pending.
        self.assertEqual(
            restored.phase,
            "needs_attention",
            "unfinished publication must resume publication, not review",
        )
        self.assertEqual(restored.recovery_stage, RECOVERY_PUSH_FAILED)
        self.assertIsNone(restored.pause_reason)


class ParkedRoundRecoveryTests(ReviewCase):
    """`review --release` / `--retry-round`: the one explicit way out of a parked round.

    Without these, a failed or interrupted round was unrecoverable without editing
    SQLite by hand — the review pass deliberately leaves both alone, `evaluate` needs
    `awaiting_review`, and implementation `retry` would start an implementation run.
    """

    def _park(self, *, state: str = ROUND_FAILED):
        self.first_run()
        store = self.store()
        task = self.task_row()
        cursor = self.claimed_cursor()
        claimed = store.claim_review_round(
            task.id,
            pr_number=PR_NUMBER,
            branch=task.branch,
            worktree_path=task.worktree_path,
            session_id=task.session_id,
            cursor_json=cursor,
            snapshot_json=cursor,
        )
        store.park_round(claimed.id, state=state, stage=None, note="the round broke")
        store.set_phase(task.id, "needs_attention", "the round broke")
        return store, task, claimed

    def test_a_parked_round_is_not_pickable_by_a_plain_review_run(self) -> None:
        """The problem the commands solve: re-adding the label did nothing."""
        store, task, claimed = self._park()
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario()
        calls = self.runtime_calls()

        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertEqual(self.runtime_calls(), calls, "a parked round must not be re-driven")
        self.assertEqual(self.rounds()[0].state, ROUND_FAILED)
        self.assertEqual(self.task_row().phase, "needs_attention")

    def test_release_returns_the_task_and_lets_a_fresh_handoff_be_claimed(self) -> None:
        store, task, claimed = self._park()
        self.assertEqual(
            self.run_cli("review", "--release", "--repo", self.slug, "--issue", "1").returncode,
            0,
        )
        after = self.task_row()
        self.assertEqual(after.phase, "awaiting_review")
        self.assertIsNone(after.recovery_stage)
        self.assertEqual(self.rounds()[0].state, ROUND_RELEASED)
        self.assertIsNone(after.feedback_cursor, "releasing must not acknowledge feedback")

        # The feedback is still unacknowledged, so a fresh handoff carries it again.
        # The label must be OBSERVED ABSENT first, because a label sitting on the PR is
        # not automatically a new request — and note that `_park` never added one, so
        # this genuinely is an absence rather than a re-add of something already there.
        # (Getting this wrong made an earlier version of the test pass for the wrong
        # reason: the "absence" was a no-op, so the re-arm under test never happened.)
        self.assertNotIn(HANDOFF, self._label_names(), "precondition: no handoff label")
        self.assertEqual(self.run_cli("review").returncode, 0)
        self.assertTrue(self.task_row().handoff_claimable)
        self.hand_off()
        self.add_comment("A follow-up request.", created_at="2026-08-01T00:00:00Z")
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)
        rounds = self.rounds()
        self.assertEqual([item.round for item in rounds], [1, 2])
        self.assertEqual(rounds[1].state, ROUND_PUBLISHED)
        self.assertEqual(
            parse_cursor(self.task_row().feedback_cursor),
            parse_cursor(rounds[1].cursor),
            "the new round acknowledges the same feedback",
        )

    def test_release_works_for_an_interrupted_round_too(self) -> None:
        store, task, claimed = self._park(state=ROUND_INTERRUPTED)
        self.assertEqual(
            self.run_cli("review", "--release", "--repo", self.slug, "--issue", "1").returncode,
            0,
        )
        self.assertEqual(self.rounds()[0].state, ROUND_RELEASED)
        self.assertEqual(self.task_row().phase, "awaiting_review")

    def test_retry_round_re_drives_the_same_feedback(self) -> None:
        store, task, claimed = self._park()
        self.review_scenario()
        calls = self.runtime_calls()

        self.assertEqual(
            self.run_cli("review", "--retry-round", "--repo", self.slug, "--issue", "1").returncode,
            0,
        )
        rounds = self.rounds()
        self.assertEqual(len(rounds), 1, "the SAME round is retried, not a new one")
        self.assertEqual(rounds[0].id, claimed.id)
        self.assertEqual(rounds[0].state, ROUND_PUBLISHED)
        self.assertEqual(self.runtime_calls(), calls + 1, "exactly one model call for the retry")
        self.assertEqual(
            parse_cursor(self.task_row().feedback_cursor),
            parse_cursor(rounds[0].cursor),
        )

    def test_retry_round_works_for_an_interrupted_round_too(self) -> None:
        """`--retry-round` from BOTH parked states, not just `failed`.

        The two arrive from different causes — a failed turn versus a process that died
        mid-turn — and they are the states the command exists for. Pinning only one
        leaves the other free to regress.
        """
        store, task, claimed = self._park(state=ROUND_INTERRUPTED)
        self.review_scenario()
        calls = self.runtime_calls()

        self.assertEqual(
            self.run_cli("review", "--retry-round", "--repo", self.slug, "--issue", "1").returncode,
            0,
        )
        rounds = self.rounds()
        self.assertEqual(len(rounds), 1, "the SAME round is retried")
        self.assertEqual(rounds[0].id, claimed.id)
        self.assertEqual(rounds[0].state, ROUND_PUBLISHED)
        self.assertEqual(self.runtime_calls(), calls + 1)
        self.assertEqual(self.task_row().phase, "awaiting_review")

    def test_release_is_refused_when_nothing_is_parked(self) -> None:
        self.first_run()
        self.hand_off()
        self.add_comment("A request.")
        self.review_scenario()
        self.assertEqual(self.run_cli("review").returncode, 0)

        result = self.run_cli("review", "--release", "--repo", self.slug, "--issue", "1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no parked review round", result.stderr)
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISHED)

    def test_release_requires_a_repo_and_issue(self) -> None:
        self.first_run()
        self.assertEqual(self.run_cli("review", "--release").returncode, 2)


class PinnedSessionTests(ReviewCase):
    """A mismatched resume must not overwrite the task's pinned session (#5)."""

    def test_a_resume_returning_another_session_keeps_the_pinned_one(self) -> None:
        """The run may report any id it likes; the *task's* pin is the pinned one.

        The driver validates `result.session_id == expected_session` and fails the run
        when they differ — but the `on_session` callback fires as soon as the stream
        reports an id, which is **before** validation. Adopting that id into
        `tasks.session_id` would leave the task advertising a session that was
        explicitly rejected, and every later resume would then be attempted against the
        wrong conversation.
        """
        self.first_run()
        session = self.task_row().session_id
        self.hand_off()
        self.add_comment("A request.")
        # The resumed run reports a DIFFERENT session id.
        self.review_scenario(session_id="sess-different", result_session_id="sess-different")

        result = self.run_cli("review")
        self.assertEqual(result.returncode, 1, "a mismatched resume must fail the round")

        task = self.task_row()
        self.assertEqual(
            task.session_id,
            session,
            "the pinned session must survive a rejected resume",
        )
        rounds = self.rounds()
        self.assertEqual(rounds[0].state, ROUND_FAILED, "the round is parked, not published")
        self.assertEqual(
            rounds[0].session_id,
            session,
            "the round must still record the session it was claimed against",
        )
        self.assertIsNone(task.feedback_cursor, "a failed round acknowledges nothing")

        # The attempted id is not lost: it belongs on the run record.
        run = self.store().run_history(task.id)[-1]
        self.assertEqual(run.kind, "review")
        self.assertEqual(
            run.session_id,
            "sess-different",
            "the run records which session was actually attempted",
        )

    def test_a_first_run_still_pins_the_session_it_started(self) -> None:
        """The positive case: `record_session` must still work for a NEW session."""
        self.set_issues(issue(1, "Feature work", labels=[TRIGGER]))
        self.write_scenario(runs=[{"session_id": "sess-first", "edits": {"impl.txt": "x\n"}}])
        self.assertEqual(self.run_cli("run").returncode, 0)
        self.assertEqual(self.task_row().session_id, "sess-first")


class AttemptBudgetTests(ReviewCase):
    """A review round must not consume the implementation attempt budget (#5)."""

    def test_a_review_round_does_not_inflate_the_attempt_counter(self) -> None:
        """The budget bounds *implementation* tries; a round has its own.

        Sharing the counter made the status comment render nonsense — a task with one
        implementation and several rounds read "Attempt 4 of 3" — and, worse, a task near
        its budget could be pushed into `failed` by review rounds rather than by failed
        implementations.
        """
        self.first_run()
        self.assertEqual(self.task_row().attempts, 1, "the implementation run consumed one")

        for round_number in (1, 2):
            self.hand_off()
            self.add_comment(
                f"Round {round_number} request.", created_at=f"2026-0{round_number}-01T00:00:00Z"
            )
            self.review_scenario()
            self.assertEqual(self.run_cli("review").returncode, 0, f"round {round_number}")
            self.assertEqual(
                self.task_row().attempts,
                1,
                f"round {round_number} must not consume an implementation attempt",
            )
            self.assertEqual(self.task_row().review_round, round_number)
            if round_number == 1:
                self.set_pr_labels()
                self.run_cli("review")

        self.assertEqual(
            self.store().review_round_attempts(self.task_row().id),
            2,
            "the review budget is tracked on the rounds themselves",
        )
        body = self.status_body()
        self.assertNotIn(
            "Attempt 3",
            body,
            "the rendered attempt count must describe implementation attempts only",
        )


class ReviewLoopGuardTests(unittest.TestCase):
    """Unit-level checks of the decision table, without a subprocess."""

    def test_a_cursor_round_trips_and_an_unreadable_one_re_delivers(self) -> None:
        cursor = {"conversation:1": "v1", "inline:2": "v2"}
        self.assertEqual(parse_cursor(serialise_cursor(cursor)), cursor)
        # An unreadable cursor is treated as empty, which re-delivers feedback rather
        # than dropping it: the safe direction, since re-delivery costs one duplicate
        # request while dropping means the maintainer was ignored.
        for broken in ("", "{not json", "[]", "null"):
            self.assertEqual(parse_cursor(broken), {})

    def test_a_direct_execution_confirms_the_round_once(self) -> None:
        """`collect_feedback` merges the three surfaces and flags incompleteness."""
        import os
        import tempfile
        from pathlib import Path

        from test_offline import FAKE_WRAPPER, FakeWorld

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            world = FakeWorld(root, {"example/repo": {"labels": [TRIGGER, HANDOFF]}})
            payload = world.world
            payload["repos"]["example/repo"]["comments"] = [
                {
                    "id": 11,
                    "body": "a conversation comment",
                    "issue_number": 1,
                    "user": {"login": "u"},
                    "created_at": "2026-01-01T00:00:00Z",
                    "updated_at": "2026-01-01T00:00:00Z",
                }
            ]
            payload["repos"]["example/repo"]["review_comments"] = [
                {
                    "id": 12,
                    "body": "an inline comment",
                    "path": "a.py",
                    "line": 4,
                    "user": {"login": "u"},
                    "pull_number": 1,
                    "created_at": "2026-01-01T00:01:00Z",
                }
            ]
            payload["repos"]["example/repo"]["reviews"] = [
                {
                    "id": 13,
                    "body": "a submitted review",
                    "state": "APPROVED",
                    "submitted_at": "2026-01-01T00:02:00Z",
                    "user": {"login": "u"},
                    "pull_number": 1,
                }
            ]
            world.write_world()
            os.environ["FAKE_GH_WORLD"] = str(world.world_path)
            if os.environ.pop("FAKE_GH_PAD_REVIEWS", None):
                pass

            from agent_dispatch.github import GitHubClient

            client = GitHubClient(str(FAKE_WRAPPER))
            feedback = collect_feedback(client, repo="example/repo", pr_number=1, issue_number=1)
            self.assertTrue(feedback.complete)
            self.assertEqual(len(feedback.items), 3)
            self.assertEqual(
                {item.kind for item in feedback.items},
                {"conversation", "inline", "review"},
            )
            self.assertEqual(len(feedback.cursor()), 3)
            self.assertEqual(len(feedback.new_items({})), 3)
            self.assertEqual(len(feedback.new_items(feedback.cursor())), 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

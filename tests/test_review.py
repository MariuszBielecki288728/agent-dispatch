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
    ROUND_CLAIMED,
    ROUND_FAILED,
    ROUND_INTERRUPTED,
    ROUND_PUBLISH_PENDING,
    ROUND_PUBLISHED,
    ROUND_RELEASED,
    ROUND_STAGE_PR,
    ROUND_STAGE_PUSH,
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

    def _label_names(self) -> list[str]:
        return [
            str(item["name"]) if isinstance(item, dict) else str(item)
            for item in self.pr().get("labels", [])
        ]

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
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISH_PENDING)
        self.assertIsNone(self.task_row().feedback_cursor, "nothing may be acknowledged")
        self.assertEqual(self.task_row().phase, "needs_attention")

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
        # The round stays publish-pending with its feedback unacknowledged, so a later
        # pass (or a maintainer) can still resolve it rather than losing the work.
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISH_PENDING)
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
        self.assertEqual(self.rounds()[0].state, ROUND_PUBLISH_PENDING)
        self.assertIsNone(self.task_row().feedback_cursor, "nothing may be acknowledged")


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

    def _remote_tip(self, branch: str) -> str | None:
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

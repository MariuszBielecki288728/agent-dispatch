#!/usr/bin/env python3
"""Offline test-suite for Issue #17 — one editable Issue status comment.

Run with the rest of the suite:

    ./scripts/test-offline.sh          # or
    PYTHONPATH=src python3 -m unittest discover -s tests -v

Everything here is deterministic and offline. GitHub is served by
``tests/fake_wrapper.py`` and the coding agent by ``tests/fake_runtime.py``, both
real executables, so the tests drive the real ``StatusPublisher``, ``RunHeartbeat``,
``Orchestrator`` and CLI code paths.

Coverage maps to the Issue #17 acceptance list:

* one ``take-it`` Issue produces exactly **one** status comment, created at actual
  start, and heartbeats **edit that comment's id** rather than POSTing new ones;
* updates happen *during* a blocked/live runtime run, not only when the main polling
  loop resumes;
* correct pinned model/effort and non-invented timestamps appear;
* a clean run that publishes shows ``Awaiting review`` with the verified owned PR
  link, and no heartbeat edits follow;
* forced ``commit_failed`` / ``push_failed`` / ``pr_failed`` show publication trouble
  rather than success, and the **same long-lived worker** later completes publish-only
  recovery into the **same comment** with zero extra runtime calls;
* a manual ``pause`` leaves the comment ``Paused`` and no poll pushes anything until
  publication is legitimately restored;
* a crash between comment creation and recording its id is recovered by marker scan,
  with exactly one comment total;
* an edit timeout/denial leaves the run's outcome correct, warns locally, and creates
  no duplicate and no second credential;
* a stale ``Running`` left by a killed process is repaired at startup against durable
  state, never reported as a completed task;
* ``status`` and ``dry-run`` stay read-only: no runtime, no comment created or edited.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from dataclasses import replace
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TESTS_DIR.parent
SRC = REPO_ROOT / "src"
sys.path.insert(0, str(SRC))

from agent_dispatch.github import (  # noqa: E402
    ErrorKind,
    GitHubError,
    IssueComment,
)
from agent_dispatch.logging_setup import Logger  # noqa: E402
from agent_dispatch.statuscomment import (  # noqa: E402
    MARKER_TEMPLATE,
    STATE_AWAITING_REVIEW,
    STATE_FAILED,
    STATE_INTERRUPTED,
    STATE_NEEDS_ATTENTION,
    STATE_PAUSED,
    STATE_PUBLISHING,
    STATE_QUEUED,
    STATE_RECOVERING,
    STATE_RUNNING,
    STATE_STARTING,
    STATUS_TIMEOUT_SECONDS,
    RunHeartbeat,
    StatusPublisher,
    StatusView,
    contains_marker,
    describe_task,
    elapsed_seconds,
    format_elapsed,
    format_utc,
    marker_for,
    render_comment,
    task_view,
)
from agent_dispatch.store import PAUSE_MAINTAINER, Store  # noqa: E402
from test_execution import ExecutionCase  # noqa: E402
from test_offline import TRIGGER, issue  # noqa: E402


#: The comment state a test can read back from the fake world.
def comments_of(world: dict, slug: str, number: int) -> list[dict]:
    repo = world["repos"][slug]
    return [
        record
        for record in repo.get("comments", [])
        if int(record.get("issue_number", number)) == number
    ]


def writes_of(world: dict, slug: str) -> list[dict]:
    """Every comment write (create or edit) the fake wrapper actually saw.

    This is what makes "heartbeats edit, they do not POST" checkable, and what lets a
    test assert which states really reached GitHub.
    """
    return list(world["repos"][slug].get("comment_writes", []))


class StatusCommentCase(ExecutionCase):
    """Base for #17 tests: a real run whose status comment is served by the fake."""

    def status_comments(self, number: int = 1) -> list[dict]:
        return comments_of(self.world.read_world(), self.slug, number)

    def status_comment_body(self, number: int = 1) -> str:
        rows = self.status_comments(number)
        self.assertTrue(rows, "expected a status comment on the Issue")
        return str(rows[0]["body"])

    def status_writes(self) -> list[dict]:
        return writes_of(self.world.read_world(), self.slug)

    def status_edits(self) -> list[dict]:
        return [entry for entry in self.status_writes() if entry["kind"] == "edit"]

    def status_creates(self) -> list[dict]:
        return [entry for entry in self.status_writes() if entry["kind"] == "create"]

    def published_states(self) -> list[str]:
        """The state label of every comment write, in order."""
        states: list[str] = []
        for entry in self.status_writes():
            body = entry["body"]
            if "**Status:** " in body:
                states.append(body.split("**Status:** ")[1].split("\n")[0].strip())
        return states

    def store_rows(self, config) -> Store:
        store = Store(config.worker.state_db)
        self.addCleanup(store.close)
        return store

    def assert_one_comment(self, number: int = 1, why: str = "") -> dict:
        rows = self.status_comments(number)
        self.assertEqual(
            len(rows),
            1,
            f"exactly one status comment must exist on the Issue{': ' + why if why else ''} "
            f"(found {len(rows)})",
        )
        return rows[0]


# ==============================================================================
# Pure rendering: what a comment may and may not say
# ==============================================================================


class MarkerTests(unittest.TestCase):
    def test_marker_is_scoped_to_one_repo_and_issue(self) -> None:
        # Ownership is proven by this exact string, so a marker for another repository
        # or another Issue must never match.
        mine = marker_for("owner/repo", 17)
        self.assertTrue(contains_marker(f"hello\n{mine}\nworld", "owner/repo", 17))
        self.assertFalse(contains_marker(mine, "owner/repo", 18))
        self.assertFalse(contains_marker(mine, "owner/other", 17))
        self.assertFalse(contains_marker(mine, "owner/other", 18))

    def test_marker_shape_is_stable_and_non_sensitive(self) -> None:
        # Documented literally, because the recovery scan depends on it surviving
        # exactly, and because it must carry nothing but repo + Issue number.
        self.assertEqual(
            MARKER_TEMPLATE.format(repo="a/b", issue=3),
            '<!-- agent-dispatch:status repo="a/b" issue=3 -->',
        )

    def test_a_similar_but_different_marker_is_not_adopted(self) -> None:
        # A human writing something that merely looks like a status comment (or a
        # marker with extra text) must not become ours.
        text = '<!-- agent-dispatch:status repo="owner/repo" issue=17 extra -->'
        self.assertFalse(contains_marker(text, "owner/repo", 17))


class TimeFormattingTests(unittest.TestCase):
    def test_utc_rendering_is_unambiguous(self) -> None:
        self.assertEqual(format_utc("2026-09-25T00:00:00+00:00"), "2026-09-25 00:00")
        # A non-UTC offset is converted, not printed as if it were UTC.
        self.assertEqual(format_utc("2026-09-25T02:00:00+02:00"), "2026-09-25 00:00")

    def test_unobserved_timestamps_render_as_nothing(self) -> None:
        # `None` is what makes every caller omit the line: an invented timestamp is
        # worse than an absent one.
        self.assertIsNone(format_utc(None))
        self.assertIsNone(format_utc(""))
        self.assertIsNone(format_utc("not a timestamp"))

    def test_elapsed_is_never_negative(self) -> None:
        self.assertEqual(
            elapsed_seconds("2026-09-25T00:00:00+00:00", "2026-09-25T00:10:00+00:00"), 600
        )
        # A backwards clock reports 0 rather than an impossible elapsed time.
        self.assertEqual(
            elapsed_seconds("2026-09-25T00:10:00+00:00", "2026-09-25T00:00:00+00:00"), 0
        )
        self.assertEqual(format_elapsed(600), "10 min")
        self.assertEqual(format_elapsed(42), "42 s")
        self.assertIsNone(format_elapsed(None))


class RenderTests(unittest.TestCase):
    def _view(self, **overrides: object) -> StatusView:
        base: dict[str, object] = {
            "repo": "owner/repo",
            "issue_number": 17,
            "state": STATE_RUNNING,
            "model": "deepseek/deepseek-v4-flash",
            "effort": "medium",
            "attempt": 1,
            "max_attempts": 3,
            "run_started_at": "2026-09-25T00:00:00+00:00",
            "last_checked_at": "2026-09-25T00:10:00+00:00",
            "elapsed_seconds": 600,
            "process_alive": True,
        }
        base.update(overrides)
        return StatusView(**base)  # type: ignore[arg-type]

    def test_running_comment_shows_pinned_identity_and_truthful_liveness(self) -> None:
        body = render_comment(self._view())
        self.assertIn(marker_for("owner/repo", 17), body)
        self.assertIn(STATE_RUNNING, body)
        self.assertIn("deepseek/deepseek-v4-flash", body)
        self.assertIn("medium", body)
        self.assertIn("2026-09-25 00:00 UTC", body)
        self.assertIn("2026-09-25 00:10 UTC", body)
        self.assertIn("Elapsed:** 10 min", body)
        self.assertIn("Command Code process:** alive", body)
        # A live PID is not proof of progress, and the comment must say so rather
        # than implying tokens are being generated.
        self.assertIn("not a claim that the model is generating tokens", body)

    def test_never_invents_progress_tokens_cost_or_eta(self) -> None:
        # The only permitted mentions are explicit disclaimers; no figure is ever
        # invented, because a status comment must not guess at progress.
        body = render_comment(self._view()).lower()
        for forbidden in ("eta", "percent", "cost", "remaining", "tokens used", "progress:"):
            with self.subTest(word=forbidden):
                self.assertNotIn(forbidden, body)
        # And when it does mention tokens, it is to deny claiming them.
        self.assertIn("not a claim that the model is generating tokens", body)

    def test_last_event_time_is_shown_only_when_actually_observed(self) -> None:
        without = render_comment(self._view())
        self.assertNotIn("Last observed runtime event", without)

        with_event = render_comment(
            self._view(last_event_at="2026-09-25T00:07:00+00:00", events_observed=42)
        )
        self.assertIn("Last observed runtime event:** 2026-09-25 00:07 UTC", with_event)
        self.assertIn("42 events observed", with_event)
        # Distinct from the heartbeat time: the whole point of reporting both is that
        # a heartbeat is the dispatcher's clock, not the runtime's activity.
        self.assertIn("Last checked:** 2026-09-25 00:10 UTC", with_event)

    def test_process_not_started_is_stated_not_implied(self) -> None:
        body = render_comment(self._view(state=STATE_STARTING, process_alive=False))
        self.assertIn(STATE_STARTING, body)
        self.assertIn("not started yet", body)
        # `Starting` must never claim the process is alive.
        self.assertNotIn("process:** alive", body)

    def test_awaiting_review_needs_a_verified_pr_link(self) -> None:
        body = render_comment(
            self._view(
                state=STATE_AWAITING_REVIEW,
                process_alive=None,
                pr_number=42,
                pr_url="https://github.com/owner/repo/pull/42",
                run_started_at=None,
                last_checked_at=None,
                elapsed_seconds=None,
            )
        )
        self.assertIn(STATE_AWAITING_REVIEW, body)
        self.assertIn("#42 (https://github.com/owner/repo/pull/42)", body)
        self.assertIn("created by this task", body)
        # A finished task must not carry live liveness fields.
        self.assertNotIn("Command Code process", body)
        self.assertNotIn("Elapsed", body)

    def test_terminal_states_omit_every_liveness_field(self) -> None:
        for state in (STATE_FAILED, STATE_NEEDS_ATTENTION, STATE_PAUSED, STATE_QUEUED):
            with self.subTest(state=state):
                body = render_comment(self._view(state=state, process_alive=None))
                self.assertIn(state, body)
                self.assertNotIn("Command Code process", body)
                self.assertNotIn("Last checked", body)
                self.assertNotIn("Elapsed", body)

    def test_footer_scopes_comment_edits_to_the_pull_request(self) -> None:
        body = render_comment(self._view())
        self.assertIn("never starts agent work by itself", body)


# ==============================================================================
# State derivation from durable rows
# ==============================================================================


class DescribeTaskTests(unittest.TestCase):
    """`describe_task` is the single derivation used by every writer."""

    def _task(self, **overrides: object):
        from dataclasses import replace

        base = _bare_task()
        return replace(base, **overrides)  # type: ignore[arg-type]

    def test_queued_and_terminal_phases(self) -> None:
        self.assertEqual(describe_task(self._task(phase="queued"))[0], STATE_QUEUED)
        self.assertEqual(
            describe_task(self._task(phase="paused", pause_reason=PAUSE_MAINTAINER))[0],
            STATE_PAUSED,
        )
        self.assertEqual(
            describe_task(self._task(phase="paused", pause_reason="label_withdrawn"))[0],
            STATE_PAUSED,
        )

    def test_maintainer_and_label_pauses_give_different_next_actions(self) -> None:
        maintainer = describe_task(self._task(phase="paused", pause_reason=PAUSE_MAINTAINER))[1]
        withdrawn = describe_task(self._task(phase="paused", pause_reason="label_withdrawn"))[1]
        self.assertIn("unpause", maintainer or "")
        self.assertIn("Re-adding the label", withdrawn or "")
        self.assertNotEqual(maintainer, withdrawn)

    def test_publish_pending_is_recovering_not_queued(self) -> None:
        # The merged #16 invariant: finished work awaiting publication is never an
        # implementation task, so its comment must not read like one.
        for stage in ("commit_failed", "push_failed", "pr_failed"):
            with self.subTest(stage=stage):
                state, action = describe_task(
                    self._task(phase="needs_attention", recovery_stage=stage)
                )
                self.assertEqual(state, STATE_RECOVERING)
                self.assertIn("no further model run", action or "")

    def test_interrupted_stage_explains_the_refusal(self) -> None:
        # `interrupted` is deliberately NOT a publishable stage, so it must not be
        # reported as "Recovering publication": these edits are half-written and are
        # not going to be published without a human.
        state, action = describe_task(
            self._task(phase="needs_attention", recovery_stage="interrupted")
        )
        self.assertEqual(state, STATE_INTERRUPTED)
        self.assertIn("deliberately not published", action or "")

    def test_a_failed_attempt_within_budget_is_not_reported_as_merely_queued(self) -> None:
        # A failed run inside the attempt budget leaves the task `queued` by design, so
        # the phase alone is truthful but uninformative: reporting only "Queued" would
        # hide that a run just failed.
        state, action = describe_task(self._task(phase="queued", attempts=1))
        self.assertEqual(state, STATE_FAILED)
        self.assertIn("still within", action or "")

    def test_an_owned_pr_wins_over_a_stale_queued_phase(self) -> None:
        # What a remove-then-re-add of the trigger label leaves behind. `queued` here
        # would be actively misleading: this worker's own PR already exists, so no
        # further implementation run will happen for it.
        state, _ = describe_task(self._task(phase="queued", pr_number=99, attempts=1))
        self.assertEqual(state, STATE_AWAITING_REVIEW)

    def test_an_orphaned_running_row_is_never_reported_as_running(self) -> None:
        # The state this whole feature must not lie about: with one writer holding the
        # lock and synchronous dispatch, a `running` row seen by a state derivation has
        # no live process behind it.
        state, action = describe_task(self._task(phase="running"))
        self.assertEqual(state, STATE_INTERRUPTED)
        self.assertNotEqual(state, STATE_RUNNING)
        self.assertIn("not running now", action or "")

    def test_awaiting_review_without_a_pr_is_attention_not_success(self) -> None:
        state, _ = describe_task(self._task(phase="awaiting_review", pr_number=None))
        self.assertEqual(state, STATE_NEEDS_ATTENTION)
        state, _ = describe_task(self._task(phase="awaiting_review", pr_number=7))
        self.assertEqual(state, STATE_AWAITING_REVIEW)

    def test_failed_phase_names_the_recovery_command(self) -> None:
        state, action = describe_task(self._task(phase="failed"))
        self.assertEqual(state, STATE_FAILED)
        self.assertIn("agent-dispatch retry", action or "")


def _bare_task(**overrides: object):
    """A minimal Task with every field the status logic reads."""
    from dataclasses import replace

    from agent_dispatch.store import Task

    base = Task(
        id=1,
        repo="owner/repo",
        issue_number=17,
        title="A title",
        phase="queued",
        branch="dispatch/issue-17-x",
        worktree_path="/tmp/wt",
        base_branch="main",
        pr_number=None,
        runtime_driver="commandcode",
        runtime_model="deepseek/deepseek-v4-pro",
        runtime_effort="high",
        permission_mode="allow-all",
        session_id="sess-1",
        review_round=0,
        feedback_cursor=None,
        attempts=0,
        last_error="operator-visible detail that must never reach a public comment",
        last_run_at="2026-09-25T00:00:00+00:00",
        pause_reason=None,
        created_at="2026-09-25T00:00:00+00:00",
        updated_at="2026-09-25T00:00:00+00:00",
        trigger_present=True,
        issue_state="open",
        linked_pr_number=None,
        linked_pr_state=None,
        observed_at="2026-09-25T00:00:00+00:00",
        pr_url=None,
        pr_created_at=None,
        dispatched_at=None,
        recovery_stage=None,
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def _owned_task(**overrides: object):
    """A task that has already been claimed, for publisher-level tests.

    `_bare_task` describes a queued row; the publisher-level tests here need a task
    whose phase is `running`, because that is what an owned status comment belongs to.
    """
    return _bare_task(phase="running", **overrides)


def _owned_store_task(store: Store, **overrides: object):
    """A real task row in ``store``, plus the matching ``Task`` object.

    Publisher-level tests need a genuine row rather than a detached object: the
    ``status_comments`` table has a foreign key to ``tasks``, so recording ownership
    intent (which is what makes the publisher own a comment) requires one to exist.
    """
    from dataclasses import replace

    store.upsert_discovered(
        repo="owner/repo",
        issue_number=1,
        title="A task",
        base_branch="main",
        runtime_driver="commandcode",
        runtime_model="model-x",
        runtime_effort="high",
        permission_mode="allow-all",
        trigger_present=True,
        issue_state="open",
        linked_pr_number=None,
        linked_pr_state=None,
    )
    task = store.get_task("owner/repo", 1)
    assert task is not None
    return replace(task, phase="running", **overrides)  # type: ignore[arg-type]


class TaskViewTests(unittest.TestCase):
    def test_view_uses_the_pinned_identity_from_the_row(self) -> None:
        # `runtime_model` is written at claim time, so a later config change cannot
        # make the comment describe a model that never ran this task.
        view = task_view(_bare_task(runtime_model="pinned/model", runtime_effort="low"))
        self.assertEqual(view.model, "pinned/model")
        self.assertEqual(view.effort, "low")

    def test_view_never_carries_operator_text(self) -> None:
        view = task_view(_bare_task())
        body = render_comment(view)
        self.assertNotIn("operator-visible detail", body)
        # And there is no field for it, so no future caller can pass one through.
        self.assertFalse(hasattr(view, "last_error"))
        self.assertFalse(hasattr(view, "body"))
        self.assertFalse(hasattr(view, "detail"))


# ==============================================================================
# End-to-end: one comment, created at start, edited for the terminal state
# ==============================================================================


class SingleCommentLifecycleTests(StatusCommentCase):
    def test_one_comment_created_at_start_and_finalised_after_publishing(self) -> None:
        self.set_issues(issue(1, "Status comment", labels=[TRIGGER]))
        result = self.run_cli("run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        comment = self.assert_one_comment(why="a clean run publishes exactly one status")

        # The body was edited in place to the terminal state, with the verified PR.
        world = self.world.read_world()
        pr = world["repos"][self.slug]["pulls"][0]
        body = self.status_comment_body()
        self.assertIn(STATE_AWAITING_REVIEW, body)
        self.assertIn(f"#{pr['number']}", body)
        self.assertIn(pr["html_url"], body)
        self.assertIn("created by this task", body)
        self.assertEqual(comment["id"], self.status_comments()[0]["id"], "the same comment object")

    def test_the_comment_carries_pinned_model_and_effort_and_real_timestamps(self) -> None:
        self.set_issues(issue(1, "Identity", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        body = self.status_comment_body()
        self.assertIn("deepseek/deepseek-v4-flash", body)
        self.assertIn("medium", body)
        # A real UTC timestamp that was actually observed, not a placeholder.
        self.assertRegex(body, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC")

    def test_every_state_the_maintainer_watches_for_is_actually_published(self) -> None:
        # A single terminal edit would mean nobody could see progress, so the write
        # history itself is asserted rather than only the final body.
        #
        # The run must be genuinely in flight: `Running` describes a live subprocess,
        # so a run that finishes instantly may legitimately go straight from `Starting`
        # to the terminal state — the 300s heartbeat interval never comes due. Keeping
        # the run alive across at least one injected interval is what makes this
        # assertion about the implementation rather than about machine speed.
        self.set_issues(issue(1, "States", labels=[TRIGGER]))
        self.write_scenario(
            runs=[
                {
                    "session_id": "sess-1",
                    "subtype": "success",
                    "stream_seconds": 2.5,
                    "stream_tick": 0.2,
                    "edits": {"impl.txt": "done\n"},
                }
            ]
        )
        self.assertEqual(self.run_cli("run").returncode, 0)
        states = self.published_states()
        for expected in (STATE_STARTING, STATE_RUNNING, STATE_PUBLISHING, STATE_AWAITING_REVIEW):
            with self.subTest(state=expected):
                self.assertIn(expected, states, f"states published were {states}")

    def test_publishing_never_claims_the_runtime_has_not_started(self) -> None:
        # `Publishing` means the runtime finished cleanly and publication is in
        # flight. Rendering the process block there said "not started yet", the
        # opposite of the truth, so the liveness fields are scoped to Starting/Running.
        self.set_issues(issue(1, "Publishing render", labels=[TRIGGER]))
        from agent_dispatch.config import load_config
        from agent_dispatch.statuscomment import (
            STATE_PUBLISHING,
        )
        from agent_dispatch.statuscomment import (
            task_view as build_view,
        )

        store = self.store_rows(load_config(self.world.config_path))
        task = store.get_task(self.slug, 1)
        self.assertIsNone(task, "no task row exists before a poll")

        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        task = store.get_task(self.slug, 1)
        self.assertIsNotNone(task)

        # The exact view the orchestrator builds for the publication window.
        view = build_view(
            task,
            state=STATE_PUBLISHING,
            process_alive=False,
            run_started_at="2026-09-25T00:00:00+00:00",
        )
        body = render_comment(view)
        self.assertIn(STATE_PUBLISHING, body)
        self.assertNotIn(
            "not started yet",
            body,
            "Publishing must not claim the runtime never started",
        )
        self.assertNotIn("Command Code process", body, "no process line while publishing")
        self.assertNotIn("Last observed runtime event", body)

    def test_publishing_is_not_claimed_as_awaiting_review(self) -> None:
        # Ordering matters: Publishing must precede Awaiting review, and the run must
        # never jump straight to the terminal state (which would hide a failed publish).
        self.set_issues(issue(1, "Order", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        states = self.published_states()
        self.assertLess(
            states.index(STATE_PUBLISHING),
            states.index(STATE_AWAITING_REVIEW),
            f"Publishing must be published before Awaiting review (saw {states})",
        )

    def test_only_one_comment_is_ever_created(self) -> None:
        self.set_issues(issue(1, "One create", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        self.assertEqual(
            len(self.status_creates()), 1, "exactly one comment may ever be POSTed per task"
        )

    def test_no_heartbeat_edits_after_the_terminal_update(self) -> None:
        self.set_issues(issue(1, "Quiet", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        before = len(self.status_edits())

        # Several polls with nothing running must not touch the comment at all: the
        # rendered body is unchanged, so no GitHub write happens.
        for _ in range(3):
            self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        self.assertEqual(
            len(self.status_edits()),
            before,
            "an awaiting_review task must not keep editing its comment",
        )
        self.assert_one_comment()

    def test_no_run_log_or_transcript_ever_reaches_the_comment(self) -> None:
        self.set_issues(issue(1, "No leakage", labels=[TRIGGER]))
        self.write_scenario(
            runs=[
                {
                    "session_id": "sess-1",
                    "subtype": "success",
                    "edits": {"impl.txt": "done\n"},
                    "final_text": "SECRET-AGENT-FINAL-TEXT /home/private/path",
                }
            ]
        )
        self.assertEqual(self.run_cli("run").returncode, 0)
        body = self.status_comment_body()
        self.assertNotIn("SECRET-AGENT-FINAL-TEXT", body)
        self.assertNotIn("/home/private", body)
        self.assertNotIn(".ndjson", body)
        self.assertNotIn("state.db", body)


# ==============================================================================
# Heartbeat: fires while the runtime is genuinely in flight
# ==============================================================================


class HeartbeatDuringRunTests(StatusCommentCase):
    def execution_overrides(self, **extra: object) -> dict[str, object]:
        # A short interval INJECTED FOR TESTS ONLY; production defaults to 300s. The
        # test must never sleep five real minutes.
        overrides = super().execution_overrides(status_heartbeat_seconds=1, **extra)
        return overrides

    def test_heartbeats_edit_the_same_comment_while_the_run_is_alive(self) -> None:
        # `stream_seconds` keeps the fake runtime genuinely in flight and emitting, so
        # the driver is blocked reading the stream. A heartbeat driven from the polling
        # loop could not fire here — which is precisely what this proves.
        self.set_issues(issue(1, "Live run", labels=[TRIGGER]))
        self.write_scenario(
            runs=[
                {
                    "session_id": "sess-1",
                    "subtype": "success",
                    "stream_seconds": 3.5,
                    "stream_tick": 0.2,
                    "edits": {"impl.txt": "done\n"},
                }
            ]
        )
        self.assertEqual(self.run_cli("run").returncode, 0)

        self.assert_one_comment(why="heartbeats must EDIT, never POST a new comment")
        bodies = [entry["body"] for entry in self.status_edits()]
        running = [body for body in bodies if f"**Status:** {STATE_RUNNING}" in body]
        self.assertGreaterEqual(
            len(running),
            2,
            f"expected at least two Running edits during a 3.5s live run (saw {len(running)})",
        )
        # Every write went to the one comment id.
        comment_id = self.status_comments()[0]["id"]
        self.assertTrue(
            all(entry["comment_id"] == comment_id for entry in self.status_edits()),
            "every heartbeat must edit the single owned comment",
        )

    def test_the_heartbeat_reports_only_really_observed_event_times(self) -> None:
        self.set_issues(issue(1, "Events", labels=[TRIGGER]))
        self.write_scenario(
            runs=[
                {
                    "session_id": "sess-1",
                    "subtype": "success",
                    "stream_seconds": 2.5,
                    "stream_tick": 0.2,
                    "edits": {"impl.txt": "done\n"},
                }
            ]
        )
        self.assertEqual(self.run_cli("run").returncode, 0)
        running_bodies = [
            entry["body"]
            for entry in self.status_edits()
            if f"**Status:** {STATE_RUNNING}" in entry["body"]
        ]
        self.assertTrue(
            any("Last observed runtime event" in body for body in running_bodies),
            "the last observed event time must be shown when events really were observed",
        )
        for body in running_bodies:
            if "Last observed runtime event" in body:
                # And it must be paired with the heartbeat's own clock, kept distinct.
                self.assertIn("Last checked", body)
                self.assertRegex(body, r"events observed")

    def test_starting_is_published_before_any_process_is_claimed_alive(self) -> None:
        self.set_issues(issue(1, "Starting", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        writes = self.status_writes()
        self.assertTrue(writes, "expected the status comment to be written")
        # The FIRST write is the one that creates the comment, and it must already say
        # Starting: creating it with `Running` would claim a model that has not started.
        first = writes[0]
        self.assertEqual(first["kind"], "create", "the first write must create the comment")
        self.assertIn(STATE_STARTING, first["body"])
        self.assertIn("not started yet", first["body"])
        self.assertNotIn("process:** alive", first["body"])

    def test_a_blocked_tool_run_still_updates_its_status_to_failure(self) -> None:
        # The false-success case: the runtime reports subtype=success and exits 0 while
        # every tool call was refused. The comment must not show a success.
        self.set_issues(issue(1, "Blocked", labels=[TRIGGER]))
        self.write_scenario(
            runs=[
                {
                    "session_id": "sess-1",
                    "subtype": "success",
                    "exit_code": 0,
                    "events": ["tool_hook_blocked"],
                }
            ]
        )
        self.run_cli("run")
        self.assert_one_comment()
        body = self.status_comment_body()
        self.assertNotIn(STATE_AWAITING_REVIEW, body)
        self.assertNotIn(STATE_PUBLISHING, body)
        self.assertTrue(
            STATE_FAILED in body or STATE_NEEDS_ATTENTION in body,
            f"a blocked run must not report success (body was:\n{body})",
        )


# ==============================================================================
# Publication lifecycle: trouble is shown as trouble, then repaired in place
# ==============================================================================


class PublicationLifecycleTests(StatusCommentCase):
    def _run_to_publication_trouble(self, *, fail: str) -> tuple[Store, dict]:
        """Drive one real run, forcing publication to fail at ``fail``."""
        self.set_issues(issue(1, "Publication", labels=[TRIGGER]))
        if fail == "commit":
            self.break_commits()
        else:
            self.inject_failure(
                {
                    "push": f"{self.slug}:pull_create",  # unused; clarity
                }.get(fail, "")
                or f"{self.slug}:pull_create",
                stderr="gh: Server Error (HTTP 502)",
            )
        self.run_cli("run")
        from agent_dispatch.config import load_config

        store = self.store_rows(load_config(self.world.config_path))
        return store, {}

    def test_commit_failure_shows_trouble_then_the_same_comment_becomes_awaiting_review(
        self,
    ) -> None:
        # Reproduces the merged #16 lifecycle: a clean runtime completion whose commit
        # fails. The comment must show publication trouble, not success, and the SAME
        # long-lived worker must later finish publication into the SAME comment with no
        # additional model invocation.
        self.set_issues(issue(1, "Commit trouble", labels=[TRIGGER]))
        self.break_commits()
        self.assertEqual(self.run_cli("run").returncode, 1)

        comment = self.assert_one_comment()
        stuck = self.status_comment_body()
        self.assertNotIn(STATE_AWAITING_REVIEW, stuck)
        self.assertIn(STATE_RECOVERING, stuck)
        self.assertIn("could not be committed", stuck)

        from agent_dispatch.config import load_config

        store = self.store_rows(load_config(self.world.config_path))
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.recovery_stage, "commit_failed")

        runs_before = len(self.recorded_argv())
        self.repair_commits()

        # A single persistent worker, polled — not a fresh process, which would hide a
        # missing per-poll pass behind startup reconciliation.
        worker, store, _ = self.live_worker()
        worker.poll_once()

        self.assert_one_comment(why="recovery must edit the same comment")
        self.assertEqual(
            self.status_comments()[0]["id"], comment["id"], "the same comment id must be reused"
        )
        recovered = self.status_comment_body()
        self.assertIn(STATE_AWAITING_REVIEW, recovered)
        world = self.world.read_world()
        pulls = world["repos"][self.slug]["pulls"]
        self.assertEqual(len(pulls), 1, "exactly one PR")
        self.assertIn(pulls[0]["html_url"], recovered)
        self.assertEqual(
            len(self.recorded_argv()), runs_before, "publish-only recovery must not run the model"
        )

    def test_pr_creation_failure_shows_recovery_then_completes_in_place(self) -> None:
        self.set_issues(issue(1, "PR trouble", labels=[TRIGGER]))
        self.inject_failure(
            f"{self.slug}:pull_create", stderr="gh: Server Error (HTTP 502)", once=True
        )
        self.run_cli("run")

        self.assert_one_comment()
        stuck = self.status_comment_body()
        self.assertNotIn(STATE_AWAITING_REVIEW, stuck)
        self.assertIn(STATE_RECOVERING, stuck)

        runs_before = len(self.recorded_argv())
        worker, store, _ = self.live_worker()
        worker.poll_once()

        self.assert_one_comment()
        recovered = self.status_comment_body()
        self.assertIn(STATE_AWAITING_REVIEW, recovered)
        self.assertEqual(len(self.world.read_world()["repos"][self.slug]["pulls"]), 1)
        self.assertEqual(len(self.recorded_argv()), runs_before, "zero additional runtime calls")

    def test_a_failed_run_shows_failure_and_no_pr_is_claimed(self) -> None:
        self.set_issues(issue(1, "Failed run", labels=[TRIGGER]))
        self.write_scenario(runs=[{"session_id": "sess-1", "subtype": "error", "exit_code": 1}])
        self.run_cli("run")
        body = self.status_comment_body()
        self.assertNotIn(STATE_AWAITING_REVIEW, body)
        self.assertNotIn("Pull request", body)
        self.assertIn(STATE_FAILED, body)

    def test_a_timed_out_run_is_reported_as_a_failure_not_a_success(self) -> None:
        # The approved schema FLOORS run_timeout_seconds at 30s (a deliberate safety
        # minimum), and weakening that floor for a test would be testing a different
        # system. This therefore waits the real 30s once, through the real worker
        # watchdog, which is the only way to prove the end-to-end claim: a killed run
        # must never be reported as a success.
        self.set_issues(issue(1, "Timeout", labels=[TRIGGER]))
        self.write_scenario(runs=[{"session_id": "sess-1", "hang": True}])
        result = self.run_cli("worker", "--once", "--timeout", "30")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

        body = self.status_comment_body()
        self.assertNotIn(STATE_AWAITING_REVIEW, body)
        self.assertNotIn("Pull request", body)
        self.assertIn(STATE_FAILED, body)

        from agent_dispatch.config import load_config

        store = self.store_rows(load_config(self.world.config_path))
        runs = store.run_history(store.get_task(self.slug, 1).id)
        self.assertTrue(runs[-1].timed_out, "the deadline must be recorded as a timeout")
        self.assertEqual(runs[-1].outcome, "failed")
        self.assertIsNone(store.get_task(self.slug, 1).pr_number, "no PR for a killed run")

    def test_a_run_that_produces_no_work_is_attention_not_success(self) -> None:
        self.set_issues(issue(1, "No work", labels=[TRIGGER]))
        self.write_scenario(runs=[{"session_id": "sess-1", "subtype": "success"}])
        self.run_cli("run")
        body = self.status_comment_body()
        self.assertNotIn(STATE_AWAITING_REVIEW, body)
        self.assertIn(STATE_NEEDS_ATTENTION, body)


# ==============================================================================
# Pause, operator transitions and stale-run repair
# ==============================================================================


class PauseAndTransitionTests(StatusCommentCase):
    def test_pause_is_shown_and_a_poll_does_not_publish_anything(self) -> None:
        # A maintainer pause must be reflected as Paused and must not be overwritten by
        # heartbeat or automatic publish reconciliation.
        self.set_issues(issue(1, "Pause me", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        self.assertEqual(self.run_cli("pause", "--repo", self.slug, "--issue", "1").returncode, 0)
        body = self.status_comment_body()
        self.assertIn(STATE_PAUSED, body)
        self.assertIn("Paused by the maintainer", body)

        edits_before = len(self.status_edits())
        pulls_before = len(self.world.read_world()["repos"][self.slug]["pulls"])
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)

        self.assertIn(STATE_PAUSED, self.status_comment_body(), "pause must survive a poll")
        self.assertEqual(len(self.status_edits()), edits_before, "a paused task is not re-edited")
        self.assertEqual(
            len(self.world.read_world()["repos"][self.slug]["pulls"]),
            pulls_before,
            "no poll may push or open anything for a paused task",
        )

    def test_an_unchanged_body_is_recognised_across_a_minute_boundary(self) -> None:
        # The dedupe that makes "a poll with nothing running costs no GitHub write"
        # true is a comparison of the rendered body against the persisted one. That
        # only works if the body is a function of durable state: rendering the CURRENT
        # time into a non-live status made the body differ on any poll that crossed a
        # minute boundary, so an idle comment was re-edited on every such poll.
        #
        # The render clock is moved instead of sleeping, so this is decided by the
        # code rather than by when the test happens to run.
        self.set_issues(issue(1, "Stable body", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.config import load_config
        from agent_dispatch.statuscomment import task_view

        store = self.store_rows(load_config(self.world.config_path))
        task = store.get_task(self.slug, 1)

        first, second = (task_view(task, now=now) for now in self._two_different_minutes())
        self.assertEqual(
            render_comment(first),
            render_comment(second),
            "a non-live body must not change just because the clock advanced",
        )

        # And the live case must still move: a heartbeat has to report fresh liveness.
        live_first = task_view(task, state=STATE_RUNNING, now="2026-09-25T00:00:00+00:00")
        live_second = task_view(task, state=STATE_RUNNING, now="2026-09-25T00:01:00+00:00")
        self.assertNotEqual(render_comment(live_first), render_comment(live_second))

    @staticmethod
    def _two_different_minutes() -> tuple[str, str]:
        """Two ISO timestamps that render to different minutes."""
        return ("2026-09-25T00:00:00+00:00", "2026-09-25T00:01:00+00:00")

    def test_label_withdrawal_pauses_the_comment_and_readding_requeues_it(self) -> None:
        self.set_issues(issue(1, "Withdrawn", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        self.assertIn(STATE_AWAITING_REVIEW, self.status_comment_body())

        # Withdraw the label, then re-add it: the comment follows the durable state.
        self.set_issues(issue(1, "Withdrawn"))
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        self.assertIn(STATE_PAUSED, self.status_comment_body())

        self.set_issues(issue(1, "Withdrawn", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        # A published task stays awaiting review after its label returns.
        self.assertIn(STATE_AWAITING_REVIEW, self.status_comment_body())
        self.assert_one_comment(why="no second comment may be created by a label cycle")

    def test_retry_of_a_failed_task_updates_the_comment_via_the_cli(self) -> None:
        # `retry` is correctly REFUSED for a task still inside its attempt budget (a
        # failed run within budget stays queued by design), so this fixture uses
        # max_attempts=1: the failure exhausts the budget and lands in `failed`, which
        # is exactly the state `retry` exists to reschedule.
        self.world.write_config(worker_overrides=self.execution_overrides(max_attempts=1))
        self.set_issues(issue(1, "Retry", labels=[TRIGGER]))
        self.write_scenario(
            runs=[
                {"session_id": "sess-1", "subtype": "error", "exit_code": 1},
                {"session_id": "sess-2", "subtype": "success", "edits": {"impl.txt": "ok\n"}},
            ]
        )
        self.run_cli("run")
        from agent_dispatch.config import load_config

        self.assertEqual(
            self.store_rows(load_config(self.world.config_path)).get_task(self.slug, 1).phase,
            "failed",
            "an exhausted budget must land the task in failed so retry applies",
        )
        self.assertIn(STATE_FAILED, self.status_comment_body())

        result = self.run_cli("retry", "--repo", self.slug, "--issue", "1")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(STATE_QUEUED, self.status_comment_body())
        self.assert_one_comment()

    def test_a_stale_running_comment_is_repaired_at_startup_and_never_shows_success(
        self,
    ) -> None:
        # A killed worker leaves a fresh-looking Running status. The next start must
        # re-derive it from durable state, say a prior heartbeat went stale, and must
        # NOT mark the task successful on the strength of an orphaned running row.
        self.set_issues(issue(1, "Crashed", labels=[TRIGGER]))
        self.write_scenario(
            runs=[
                {
                    "session_id": "sess-1",
                    "subtype": "success",
                    "edits": {"impl.txt": "done\n"},
                }
            ]
        )
        self.assertEqual(self.run_cli("run").returncode, 0)

        from agent_dispatch.config import load_config

        config = load_config(self.world.config_path)
        store = self.store_rows(config)
        task = store.get_task(self.slug, 1)
        # Model a REAL crash. Two things must be true, and setting `phase='running'`
        # alone models neither: there must be an OPEN run row (which is what makes the
        # next startup close it as interrupted), and the newest run must be that
        # interrupted one — otherwise the task is legitimately just re-queued for a
        # retry, and "Queued" would be the truthful answer rather than "Interrupted".
        store._conn.execute(
            "UPDATE tasks SET phase = 'running', pr_number = NULL, recovery_stage = NULL, "
            "branch = 'dispatch/issue-1-crashed', worktree_path = NULL, attempts = 0 "
            "WHERE id = ?",
            (task.id,),
        )
        store.start_run(
            task.id,
            run_id="20260101T000000Z-implementation",
            kind="implementation",
            resumed_from=None,
            log_path=str(self.tmp / "crashed.ndjson"),
        )
        store.record_status_published(
            repo=self.slug,
            issue_number=1,
            comment_id=self.status_comments()[0]["id"],
            comment_url="",
            state=STATE_RUNNING,
            body="stale body claiming a live run",
            task_id=task.id,
        )
        store.close()

        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)

        body = self.status_comment_body()
        self.assertNotIn(STATE_AWAITING_REVIEW, body, "an orphaned row is not a success")
        self.assertIn(STATE_INTERRUPTED, body)
        self.assertIn("previous heartbeat was stale", body)
        self.assert_one_comment()


# ==============================================================================
# Crash between create and recording the id
# ==============================================================================


class MarkerRecoveryTests(StatusCommentCase):
    def test_a_crash_after_creation_is_recovered_by_marker_with_one_comment_total(self) -> None:
        # The window the marker exists for: GitHub created the comment, SQLite has the
        # intent row with comment_id NULL, and the process died.
        self.set_issues(issue(1, "Crash window", labels=[TRIGGER]))

        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.statuscomment import StatusPublisher

        config = load_config(self.world.config_path)
        store = self.store_rows(config)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))
        self.addCleanup(log.stream.close)
        client = GitHubClient(config.github.command)

        # A real task row must exist: a claim is what authorises a status comment.
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)

        # Create the comment "for real" through the wrapper, exactly as a crashed run
        # would have left it, but record only the intent (no comment_id).
        first = StatusPublisher(client=client, store=store, log=log, repo=self.slug, issue_number=1)
        task = store.get_task(self.slug, 1)
        self.assertIsNotNone(task, "the worker poll must have queued the task")
        self.assertTrue(first.begin(task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00"))
        self.assertIsNotNone(first.comment_id)
        created_id = first.comment_id
        store._conn.execute(
            "UPDATE status_comments SET comment_id = NULL, comment_url = NULL, last_body = NULL"
        )
        self.assertEqual(len(self.status_comments()), 1)

        # A fresh publisher in a fresh process must find it by marker and reuse it.
        second = StatusPublisher(
            client=client, store=store, log=log, repo=self.slug, issue_number=1
        )
        self.assertTrue(second.begin(task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00"))
        self.assertEqual(second.comment_id, created_id, "the marked comment must be adopted")
        self.assert_one_comment(why="the recovery must not create a second comment")
        self.assertNotEqual(
            len(self.status_comments()),
            2,
            "a crash after creation must never produce two status threads",
        )

    def test_another_persons_similar_comment_is_never_edited(self) -> None:
        # Ownership is proven by the exact marker, never by resemblance.
        self.set_issues(issue(1, "Foreign", labels=[TRIGGER]))
        world = self.world.read_world()
        world["repos"][self.slug]["comments"] = [
            {
                "id": 500,
                "body": "### agent-dispatch · task status\nStatus: Running\n(looks similar!)",
                "html_url": "https://github.com/example/repo/issues/1#issuecomment-500",
                "issue_number": 1,
            }
        ]
        self.world.world = world
        self.world.write_world()

        self.assertEqual(self.run_cli("run").returncode, 0)
        comments = self.status_comments()
        self.assertEqual(len(comments), 2, "ours is created alongside the human's")
        foreign = [record for record in comments if record["id"] == 500][0]
        self.assertEqual(
            foreign["body"],
            "### agent-dispatch · task status\nStatus: Running\n(looks similar!)",
            "another person's comment must never be edited",
        )

    def test_a_truncated_comment_scan_refuses_to_create(self) -> None:
        # "No marker found" is unprovable when the listing was cut short, so nothing may
        # be created: a duplicate status thread is the failure mode this prevents.
        self.set_issues(issue(1, "Truncated", labels=[TRIGGER]))
        # Only the COMMENT listing is padded. Padding the PR listing too would trip the
        # discovery path's fail-closed rule, the task would never be queued, and the
        # behaviour under test would never be reached.
        self.world.env_overrides["FAKE_GH_PAD_COMMENTS"] = "60"
        self.world.write_config(worker_overrides=self.execution_overrides())

        self.run_cli("run")
        self.assertEqual(
            self.status_comments(),
            [],
            "a truncated comment scan must not create a status comment",
        )
        # And the run itself still succeeded: status reporting is non-fatal.
        from agent_dispatch.config import load_config

        store = self.store_rows(load_config(self.world.config_path))
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "awaiting_review")
        self.assertIsNotNone(task.pr_number)
        self.assertIsNone(store.status_comment(self.slug, 1).comment_id)

    def test_two_marked_comments_fail_closed_rather_than_picking_one(self) -> None:
        # Two exact markers means ownership is genuinely ambiguous, and the Issue text
        # requires ambiguity to be *reported* rather than resolved by guessing. Picking
        # one arbitrarily could edit a comment that is not ours, so nothing is edited
        # and nothing is created until an operator leaves exactly one.
        self.set_issues(issue(1, "Ambiguous", labels=[TRIGGER]))
        marker = marker_for(self.slug, 1)
        world = self.world.read_world()
        world["repos"][self.slug]["comments"] = [
            {
                "id": 700,
                "body": f"{marker}\nfirst",
                "html_url": "https://github.com/example/repo/issues/1#c700",
                "issue_number": 1,
            },
            {
                "id": 701,
                "body": f"{marker}\nsecond",
                "html_url": "https://github.com/example/repo/issues/1#c701",
                "issue_number": 1,
            },
        ]
        self.world.world = world
        self.world.write_world()

        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.statuscomment import StatusPublisher

        config = load_config(self.world.config_path)
        store = self.store_rows(config)
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        stream = __import__("io").StringIO()
        log = Logger(fmt="text", stream=stream)
        publisher = StatusPublisher(
            client=GitHubClient(config.github.command),
            store=store,
            log=log,
            repo=self.slug,
            issue_number=1,
        )
        task = store.get_task(self.slug, 1)
        self.assertIsNotNone(task, "the worker poll must have queued the task")
        self.assertFalse(
            publisher.begin(task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00"),
            "an ambiguous scan must publish nothing",
        )
        self.assertIsNone(publisher.comment_id, "no comment may be adopted on a guess")
        self.assertEqual(len(self.status_comments()), 2, "no third comment is created")
        self.assertIn("status_comment_ambiguous", stream.getvalue())

        # Nothing was edited either: both comments are untouched.
        bodies = [
            entry["body"] for entry in self.world.read_world()["repos"][self.slug]["comments"]
        ]
        self.assertEqual(sorted(bodies), sorted([f"{marker}\nfirst", f"{marker}\nsecond"]))

    def test_ambiguity_resolved_by_an_operator_is_adopted_again(self) -> None:
        # The recovery path for the fail-closed case: once exactly one marked comment
        # remains, normal ownership resumes without creating anything.
        self.set_issues(issue(1, "Ambiguous then fixed", labels=[TRIGGER]))
        marker = marker_for(self.slug, 1)
        world = self.world.read_world()
        world["repos"][self.slug]["comments"] = [
            {
                "id": 700,
                "body": f"{marker}\nfirst",
                "html_url": "https://github.com/example/repo/issues/1#c700",
                "issue_number": 1,
            },
            {
                "id": 701,
                "body": f"{marker}\nsecond",
                "html_url": "https://github.com/example/repo/issues/1#c701",
                "issue_number": 1,
            },
        ]
        self.world.world = world
        self.world.write_world()
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)

        # An operator deletes one of the two duplicates.
        world = self.world.read_world()
        world["repos"][self.slug]["comments"] = [
            entry for entry in world["repos"][self.slug]["comments"] if entry["id"] == 700
        ]
        self.world.world = world
        self.world.write_world()

        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.statuscomment import StatusPublisher

        config = load_config(self.world.config_path)
        store = self.store_rows(config)
        publisher = StatusPublisher(
            client=GitHubClient(config.github.command),
            store=store,
            log=Logger(fmt="text", stream=__import__("io").StringIO()),
            repo=self.slug,
            issue_number=1,
        )
        task = store.get_task(self.slug, 1)
        self.assertTrue(
            publisher.begin(task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00")
        )
        self.assertEqual(publisher.comment_id, 700, "the surviving comment is adopted")


# ==============================================================================
# Non-fatal delivery: denial, timeout, rate limit
# ==============================================================================


class NonFatalDeliveryTests(StatusCommentCase):
    def test_a_denied_comment_edit_does_not_change_the_task_result_or_duplicate(self) -> None:
        # The first edit is denied; the run itself must be unaffected.
        self.set_issues(issue(1, "Denied", labels=[TRIGGER]))
        self.inject_failure(
            f"{self.slug}:comment_edit",
            stderr="gh: Resource not accessible by personal access token (HTTP 403)",
            once=True,
        )
        result = self.run_cli("run")
        self.assertEqual(result.returncode, 0, "a status write failure must not fail the run")

        from agent_dispatch.config import load_config

        store = self.store_rows(load_config(self.world.config_path))
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "awaiting_review")
        self.assertIsNotNone(task.pr_number)
        self.assertEqual(task.attempts, 1, "the attempt budget is untouched by a status failure")
        self.assert_one_comment(why="a denied edit must not cause a duplicate")

    def test_a_denied_comment_creation_is_reported_locally_and_the_run_still_succeeds(self) -> None:
        self.set_issues(issue(1, "Denied create", labels=[TRIGGER]))
        self.inject_failure(
            f"{self.slug}:comment_create",
            stderr="gh: Resource not accessible by personal access token (HTTP 403)",
        )
        result = self.run_cli("run")
        self.assertEqual(result.returncode, 0, "the run's outcome is what matters")
        self.assertIn("status_comment_create_failed", result.stderr)

        from agent_dispatch.config import load_config

        store = self.store_rows(load_config(self.world.config_path))
        self.assertIsNone(store.status_comment(self.slug, 1).comment_id)
        self.assertIn("did not reach GitHub", store.status_comment(self.slug, 1).last_error or "")
        # No fallback to another identity: the same wrapper was used throughout.
        self.assertNotIn("GH_TOKEN", result.stderr)

        # The status is still truthful and never claims a live run.
        self.assertNotIn(STATE_RUNNING, store.status_comment(self.slug, 1).last_state or "")

    def test_a_rate_limited_heartbeat_edit_is_warned_and_retried_once(self) -> None:
        self.set_issues(issue(1, "Rate limited", labels=[TRIGGER]))
        self.world.write_config(
            worker_overrides=self.execution_overrides(status_heartbeat_seconds=1)
        )
        self.inject_failure(
            f"{self.slug}:comment_edit",
            stderr="gh: API rate limit exceeded (HTTP 403)",
            once=True,
        )
        self.write_scenario(
            runs=[
                {
                    "session_id": "sess-1",
                    "subtype": "success",
                    "stream_seconds": 2.5,
                    "stream_tick": 0.2,
                    "edits": {"impl.txt": "done\n"},
                }
            ]
        )
        result = self.run_cli("run")
        self.assertEqual(result.returncode, 0)
        self.assertIn("status_comment_edit_failed", result.stderr)
        # The retry is at the next interval on the SAME comment, never a new one.
        self.assert_one_comment()
        self.assertIn(STATE_AWAITING_REVIEW, self.status_comment_body())

    def test_status_client_timeout_is_shorter_than_the_general_wrapper_timeout(self) -> None:
        # A status update still waiting when the run ends is worthless, and this bound is
        # what keeps an in-flight edit from delaying a terminal update indefinitely.
        self.assertLess(STATUS_TIMEOUT_SECONDS, 60.0)

    def test_a_transient_edit_failure_never_creates_a_second_comment(self) -> None:
        # The duplicate bug the review found. A PATCH can fail transiently (rate limit,
        # timeout, network) while the marker listing still SUCCEEDS and returns the very
        # comment we own. Because `found.id == self._comment_id`, the old code returned
        # nothing and then fell through to `_create()`, POSTing a second marked comment.
        #
        # A terminal/per-poll `sync_from_task()` passes `allow_create=True`, so it is
        # exactly this path that was vulnerable — the heartbeat uses
        # `allow_create=False`, which is why the existing denial tests missed it.
        self.set_issues(issue(1, "Transient edit", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        created = self.assert_one_comment()
        self.assertIn(STATE_AWAITING_REVIEW, self.status_comment_body())

        # An operator transition changes the durable state, so the next sync genuinely
        # has a different body to write — and that write is made to fail. (A poll on
        # unchanged state would be skipped by the body comparison and never reach the
        # edit at all, which is why the failure has to be paired with a real change.)
        #
        # `inject_failure` persists the in-memory world, so it must be refreshed from
        # disk first: otherwise it writes back a snapshot taken before the run created
        # the comment and silently deletes it.
        self.world.world = self.world.read_world()
        self.inject_failure(
            f"{self.slug}:comment_edit",
            stderr="gh: API rate limit exceeded (HTTP 403)",
            once=True,
        )
        result = self.run_cli("pause", "--repo", self.slug, "--issue", "1")
        self.assertEqual(result.returncode, 0, "a status failure must not fail the command")
        self.assertIn("status_comment_edit_failed", result.stderr)

        # The failure must not have produced a second comment.
        after = self.status_comments()
        self.assertEqual(len(after), 1, "a failed edit must never create a duplicate")
        self.assertEqual(after[0]["id"], created["id"], "the owned comment is unchanged")

        # The next pass edits that same id — the retry is a later edit, never a create.
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        self.assertEqual(len(self.status_comments()), 1, "still exactly one comment")
        self.assertTrue(
            all(entry["comment_id"] == created["id"] for entry in self.status_edits()),
            "every write must target the single owned comment",
        )
        self.assertIn(STATE_PAUSED, self.status_comment_body(), "the retry applied the change")

    def test_a_failed_edit_on_a_truncated_rescan_still_never_creates(self) -> None:
        # The same bug reached through the other guard: the edit fails AND the rescan
        # cannot prove what exists. Creation must stay refused, because "I could not
        # read the list" is not evidence that no comment exists.
        self.set_issues(issue(1, "Truncated retry", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        created = self.assert_one_comment()

        # `inject_failure` persists the in-memory world, so it must be refreshed from
        # disk first — otherwise it would write back a snapshot taken before the run
        # created the comment and silently delete it.
        self.world.world = self.world.read_world()
        self.inject_failure(
            f"{self.slug}:comment_edit",
            stderr="gh: API rate limit exceeded (HTTP 403)",
            once=True,
        )
        # Pad only the COMMENT listing so the rescan hits the page cap.
        self.world.env_overrides["FAKE_GH_PAD_COMMENTS"] = "60"
        self.addCleanup(self.world.env_overrides.pop, "FAKE_GH_PAD_COMMENTS", None)

        self.run_cli("pause", "--repo", self.slug, "--issue", "1")
        self.assertEqual(len(self.status_comments()), 1, "no duplicate on an unprovable scan")
        self.assertEqual(self.status_comments()[0]["id"], created["id"])

    def test_a_deleted_comment_is_re_resolved_by_marker_rather_than_duplicated(self) -> None:
        self.set_issues(issue(1, "Deleted", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        created = self.assert_one_comment()

        # A maintainer deletes the comment. The recorded id now 404s on edit.
        world = self.world.read_world()
        world["repos"][self.slug]["comments"] = []
        self.world.world = world
        self.world.write_world()

        # A later poll re-derives the status; the edit fails, the re-scan finds nothing,
        # and a replacement is created exactly once (the body changed, so an edit was
        # genuinely required).
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        after = self.status_comments()
        self.assertLessEqual(
            len(after), 1, "the dispatcher must converge on one comment, not accumulate"
        )
        if after:
            self.assertNotEqual(after[0]["id"], created["id"])


# ==============================================================================
# Read-only discipline
# ==============================================================================


class ReadOnlyTests(StatusCommentCase):
    def test_status_never_creates_or_edits_a_comment(self) -> None:
        self.set_issues(issue(1, "Read only", labels=[TRIGGER]))
        # The Issue is labelled but nothing has ever been claimed.
        result = self.run_cli("status")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.status_comments(), [], "`status` must not create a status comment")
        self.assertEqual(self.status_edits(), [], "`status` must not edit any comment")

    def test_dry_run_never_creates_a_comment_or_starts_a_runtime(self) -> None:
        self.set_issues(issue(1, "Dry run", labels=[TRIGGER]))
        result = self.run_cli("dry-run")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.status_comments(), [], "`dry-run` must not create a comment")
        self.assertEqual(self.recorded_argv(), [], "`dry-run` must not start a runtime")

    def test_status_and_dry_run_do_not_edit_an_existing_comment(self) -> None:
        self.set_issues(issue(1, "Existing", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        self.assert_one_comment()
        edits_before = len(self.status_edits())

        self.assertEqual(self.run_cli("status").returncode, 0)
        self.assertEqual(self.run_cli("dry-run").returncode, 0)
        self.assertEqual(
            len(self.status_edits()), edits_before, "read paths must not write to GitHub"
        )

    def test_status_shows_the_comment_it_owns_without_resolving_it(self) -> None:
        self.set_issues(issue(1, "Shown", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("run").returncode, 0)
        comment = self.assert_one_comment()

        result = self.run_cli("status")
        self.assertEqual(result.returncode, 0)
        self.assertIn(f"status comment: #{comment['id']}", result.stdout)
        self.assertIn(STATE_AWAITING_REVIEW, result.stdout)

        payload = json.loads(self.run_cli("status", "--json").stdout)
        entry = payload["tasks"][0]
        self.assertEqual(entry["status_comment_id"], comment["id"])
        self.assertEqual(entry["status_comment_state"], STATE_AWAITING_REVIEW)

        opened = self.run_cli("open", "--repo", self.slug, "--issue", "1")
        self.assertEqual(opened.returncode, 0)
        self.assertIn(f"status comment : #{comment['id']}", opened.stdout)
        self.assertIn(f"comment state  : {STATE_AWAITING_REVIEW}", opened.stdout)

    def test_open_on_a_task_without_a_comment_says_so(self) -> None:
        self.set_issues(issue(1, "Never claimed", labels=[TRIGGER]))
        self.run_cli("worker", "--once", "--no-execute")
        result = self.run_cli("open", "--repo", self.slug, "--issue", "1")
        self.assertEqual(result.returncode, 0)
        self.assertNotIn("status comment :", result.stdout)


# ==============================================================================
# Unit-level guards on the heartbeat thread
# ==============================================================================


class _RecordingPublisher:
    """A publisher stand-in that records views instead of writing to GitHub."""

    def __init__(self, *, comment_id: int | None = 1) -> None:
        self.views: list[StatusView] = []
        self.comment_id = comment_id
        self.terminal = False

    def publish(self, view: StatusView, *, allow_create: bool, background: bool = False) -> bool:
        # Mirrors the real publisher's ordering guarantee so the heartbeat tests
        # exercise it: a background write is refused once terminal publication began.
        if background and self.terminal:
            return False
        self.views.append(view)
        return True

    def begin_terminal(self) -> None:
        self.terminal = True


class _BlockingStatusClient:
    """A deterministic status client that can hold a write open on demand.

    Used to prove the heartbeat/terminal ordering WITHOUT sleeping: the test controls
    exactly when the in-flight heartbeat write completes, so the race is reproduced
    rather than hoped for. It also counts calls, which is what makes "a background tick
    is one PATCH and no scan" assertable.
    """

    def __init__(self) -> None:
        self.comment_scan_truncated = False
        self.entered = __import__("threading").Event()
        self.release = __import__("threading").Event()
        self.writes: list[str] = []
        #: Call counters, so a test can assert the shape of the transport usage.
        self.scans = 0
        self.edit_attempts = 0
        self.creates = 0
        self._block_next_edit = False
        self._fail_next_edit = False
        self._comment_id = 1

    def block_next_edit(self) -> None:
        self._block_next_edit = True

    def fail_next_edit(self) -> None:
        """Make the next PATCH fail the way a rate limit or timeout would."""
        self._fail_next_edit = True

    # -- GitHubClient surface used by StatusPublisher -------------------------

    def list_issue_comments(self, slug, issue_number):
        self.scans += 1
        return [
            IssueComment(
                id=self._comment_id,
                body=marker_for(slug, issue_number),
                url=f"https://github.com/{slug}/issues/{issue_number}#c1",
            )
        ]

    def edit_issue_comment(self, slug, comment_id, body):
        self.edit_attempts += 1
        if self._fail_next_edit:
            self._fail_next_edit = False
            raise GitHubError("gh: API rate limit exceeded (HTTP 403)", kind=ErrorKind.RATE_LIMIT)
        if self._block_next_edit:
            self._block_next_edit = False
            # Announce that the write is in flight, then wait for the test.
            self.entered.set()
            self.release.wait(timeout=30)
        self.writes.append(body)
        return IssueComment(
            id=comment_id,
            body=body,
            url=f"https://github.com/{slug}/issues/1#c{comment_id}",
        )

    def create_issue_comment(self, slug, issue_number, body):
        self.creates += 1
        self.writes.append(body)
        return IssueComment(
            id=self._comment_id,
            body=body,
            url=f"https://github.com/{slug}/issues/{issue_number}#c{self._comment_id}",
        )


class HeartbeatThreadTests(unittest.TestCase):
    def _heartbeat(self, publisher, **overrides: object) -> RunHeartbeat:
        base: dict[str, object] = {
            "base_view": StatusView(
                repo="owner/repo",
                issue_number=1,
                state=STATE_STARTING,
                model="m",
                attempt=1,
                run_started_at="2026-09-25T00:00:00+00:00",
                process_alive=False,
            ),
            "interval_seconds": 0.05,
            "log": __import__("agent_dispatch.logging_setup", fromlist=["Logger"]).Logger(
                fmt="text", stream=__import__("io").StringIO()
            ),
            "poll_seconds": 0.01,
        }
        base.update(overrides)
        return RunHeartbeat(publisher, **base)  # type: ignore[arg-type]

    def test_no_tick_is_published_before_the_process_exists(self) -> None:
        # Claiming a running model before its process exists is the lie this guard
        # prevents.
        publisher = _RecordingPublisher()
        heartbeat = self._heartbeat(publisher)
        heartbeat.start()
        try:
            __import__("time").sleep(0.2)
            self.assertEqual(publisher.views, [], "no tick before spawn confirmation")
            self.assertFalse(heartbeat.running_published)
        finally:
            heartbeat.stop_and_join()

    def test_the_first_tick_does_not_wait_for_the_poll_interval(self) -> None:
        # The regression CI caught, and the reason it only appeared there: the loop
        # slept a whole poll interval BEFORE its first check, so the first `Running`
        # tick was delayed by up to `_poll`. A run that finished inside that window
        # was stopped before any tick, and the comment went straight from `Starting`
        # to the terminal state without ever reporting `Running`.
        #
        # A deliberately huge poll interval makes the ordering decidable rather than
        # a matter of machine speed: if the first tick still waited for a poll,
        # nothing at all would be published for the whole 30s, far longer than this
        # test is prepared to wait.
        publisher = _RecordingPublisher()
        heartbeat = self._heartbeat(publisher, poll_seconds=30.0)
        heartbeat.note_spawned()
        heartbeat.start()
        try:
            deadline = __import__("time").monotonic() + 2.0
            while not publisher.views and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.01)
        finally:
            self.assertTrue(heartbeat.stop_and_join(), "the thread must be confirmed finished")
        self.assertTrue(
            publisher.views,
            "the first Running tick must follow the spawn, not the poll interval",
        )
        self.assertEqual(publisher.views[0].state, STATE_RUNNING)
        self.assertTrue(heartbeat.running_published)

    def test_a_tick_reports_running_with_process_alive(self) -> None:
        publisher = _RecordingPublisher()
        heartbeat = self._heartbeat(publisher)
        heartbeat.note_spawned()
        heartbeat.start()
        try:
            deadline = __import__("time").monotonic() + 2.0
            while not publisher.views and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.01)
        finally:
            heartbeat.stop_and_join()
        self.assertTrue(publisher.views, "a spawned process must produce a Running tick")
        view = publisher.views[0]
        self.assertEqual(view.state, STATE_RUNNING)
        self.assertTrue(view.process_alive)

    def test_events_are_counted_and_timestamped_but_never_rendered(self) -> None:
        publisher = _RecordingPublisher()
        heartbeat = self._heartbeat(publisher)
        heartbeat.note_spawned()
        heartbeat.note_event({"type": "event", "event": {"type": "tool_completed"}})
        heartbeat.note_event({"secret": "MUST-NOT-BE-RENDERED"})
        heartbeat.start()
        try:
            deadline = __import__("time").monotonic() + 2.0
            while not publisher.views and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.01)
        finally:
            heartbeat.stop_and_join()
        view = publisher.views[0]
        self.assertEqual(heartbeat.events_observed, 2)
        self.assertIsNotNone(view.last_event_at)
        rendered = render_comment(view)
        self.assertNotIn("MUST-NOT-BE-RENDERED", rendered)

    def test_stop_and_join_prevents_any_further_write(self) -> None:
        publisher = _RecordingPublisher()
        heartbeat = self._heartbeat(publisher)
        heartbeat.note_spawned()
        heartbeat.start()
        try:
            deadline = __import__("time").monotonic() + 2.0
            while not publisher.views and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.01)
        finally:
            self.assertTrue(heartbeat.stop_and_join(), "the thread must be confirmed finished")
        count = len(publisher.views)
        __import__("time").sleep(0.25)
        self.assertEqual(len(publisher.views), count, "a stopped heartbeat must not write again")

    def test_no_comment_means_no_tick_and_no_scan(self) -> None:
        # The heartbeat must never create; without an owned comment it is silent.
        publisher = _RecordingPublisher(comment_id=None)
        heartbeat = self._heartbeat(publisher)
        heartbeat.note_spawned()
        heartbeat.start()
        try:
            __import__("time").sleep(0.25)
        finally:
            heartbeat.stop_and_join()
        self.assertEqual(publisher.views, [], "no owned comment means no heartbeat edits")

    def test_elapsed_is_derived_from_the_real_clock_not_a_constant(self) -> None:
        publisher = _RecordingPublisher()
        heartbeat = self._heartbeat(publisher)
        heartbeat.note_spawned()
        heartbeat.start()
        try:
            deadline = __import__("time").monotonic() + 2.0
            while not publisher.views and __import__("time").monotonic() < deadline:
                __import__("time").sleep(0.01)
        finally:
            heartbeat.stop_and_join()
        view = publisher.views[0]
        # The base view starts in 2026-09-25 while "now" is real, so the difference is
        # enormous — the point is only that it was computed from two real timestamps.
        self.assertIsNotNone(view.elapsed_seconds)
        self.assertGreaterEqual(view.elapsed_seconds or -1, 0)

    def test_a_heartbeat_cannot_write_after_terminal_publication_begins(self) -> None:
        # The round-2 blocker, made deterministic. `stop_and_join()` is ALLOWED to time
        # out while a heartbeat is still inside the transport (a failed PATCH plus a
        # marker re-scan plus a second PATCH is several sequential bounded calls), so the
        # ordering must not rest on the join succeeding.
        #
        # The invariant that holds is: the terminal write is always LAST. An in-flight
        # heartbeat write is allowed to finish (it began before terminal publication),
        # and `begin_terminal()` waits for it on the shared lock — so it can never land
        # *after* the terminal state. Every heartbeat that starts later is refused.
        import threading
        from dataclasses import replace

        client = _BlockingStatusClient()
        store = Store.in_memory()
        self.addCleanup(store.close)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))  # noqa: SIM115
        self.addCleanup(log.stream.close)
        publisher = StatusPublisher(
            client=client, store=store, log=log, repo="owner/repo", issue_number=1
        )
        task = _owned_store_task(store)
        self.assertTrue(
            publisher.begin(task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00")
        )
        heartbeat = RunHeartbeat(
            publisher,
            base_view=publisher.heartbeat_view(
                task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00"
            ),
            interval_seconds=0.01,
            log=log,
            poll_seconds=0.005,
        )
        heartbeat.note_spawned()
        client.block_next_edit()
        heartbeat.start()
        self.assertTrue(client.entered.wait(timeout=10), "the heartbeat must reach the transport")

        # The runtime stops while that write is still open. The join is given a
        # deliberately short budget, so it takes the TIMEOUT path the review identified
        # as uncovered rather than the happy path.
        self.assertFalse(
            heartbeat.stop_and_join(timeout=0.01),
            "the heartbeat is still inside the transport — the case that made relying "
            "on the join unsound",
        )

        # Terminal publication begins while the heartbeat write is in flight. The call
        # blocks on the shared lock, which is exactly what orders the two writes.
        terminal_started = threading.Event()

        def begin_terminal() -> None:
            publisher.begin_terminal()
            terminal_started.set()

        starter = threading.Thread(target=begin_terminal, daemon=True)
        starter.start()
        self.assertFalse(
            terminal_started.wait(timeout=0.3),
            "begin_terminal must wait for the in-flight heartbeat write, not race it",
        )

        # Release the heartbeat. Its write completes, and THEN the terminal flag is set.
        client.release.set()
        self.assertTrue(
            terminal_started.wait(timeout=10),
            "begin_terminal must proceed once the in-flight write finishes",
        )
        starter.join(timeout=5)

        # The owning thread now publishes the terminal state. It must be the LAST write.
        self.assertTrue(
            publisher.publish(
                replace(
                    publisher.heartbeat_view(
                        task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00"
                    ),
                    state=STATE_AWAITING_REVIEW,
                ),
                allow_create=False,
            )
        )
        self.assertIn(STATE_AWAITING_REVIEW, client.writes[-1])

        # And no heartbeat may land after it, however many ticks it attempts.
        writes_after_terminal = len(client.writes)
        self.assertTrue(heartbeat.running_published)
        __import__("time").sleep(0.4)
        self.assertEqual(
            len(client.writes),
            writes_after_terminal,
            "no heartbeat write may land after the terminal state",
        )
        self.assertNotIn(
            STATE_RUNNING,
            client.writes[-1],
            "the last body must be the terminal state, never Running",
        )

    def test_the_run_lifecycle_shares_one_publisher_instance(self) -> None:
        # The other half of the round-2 blocker, asserted directly rather than through a
        # timing scenario. A happy-path ordering test cannot catch this: by the time
        # `_finish` runs, the heartbeat has already been joined, so a second publisher
        # there changes nothing observable. The defect IS the second publisher — the
        # terminal write would take a lock unrelated to the heartbeat's, making the
        # documented "queues behind the publisher lock" guarantee false whenever a
        # heartbeat outlives its join budget.
        #
        # Source-level assertion so it holds for the real subprocess path too (a
        # monkeypatch cannot reach a child process, which is why an in-process spy
        # would silently pass here).
        source = (SRC / "agent_dispatch" / "orchestrator.py").read_text(encoding="utf-8")
        self.assertNotIn(
            "publisher=self._publisher(",
            source,
            "the run lifecycle must pass its ONE publisher into _finish; constructing a "
            "second one gives the terminal write an unrelated lock",
        )
        # The final durable sync inside `_finish` must reuse that same publisher.
        self.assertIn("self.sync_task_status(fresh, publisher=publisher)", source)

    def test_a_background_write_only_patches_and_never_scans(self) -> None:
        # Round-3 blocker. `background=True` originally only checked the terminal flag
        # and then called `_publish_locked`, so a heartbeat tick whose PATCH failed still
        # ran a full paginated marker scan and could issue a SECOND patch. That made one
        # tick a multi-call sequence, so:
        #   * it could outlive its join budget, and
        #   * `begin_terminal()` — which waits on this lock — could block behind it,
        #     stalling the commit/push/PR path that runs immediately afterwards.
        # A background write must therefore be exactly one known-id PATCH.
        client = _BlockingStatusClient()
        store = Store.in_memory()
        self.addCleanup(store.close)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))  # noqa: SIM115
        self.addCleanup(log.stream.close)
        publisher = StatusPublisher(
            client=client, store=store, log=log, repo="owner/repo", issue_number=1
        )
        task = _owned_store_task(store)
        view = publisher.heartbeat_view(task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00")
        self.assertTrue(
            publisher.begin(task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00")
        )

        # Make the PATCH fail, which is what previously triggered the recovery path.
        client.fail_next_edit()
        scans_before = client.scans
        edits_before = client.edit_attempts
        self.assertFalse(
            publisher.publish(
                replace(view, state=STATE_RUNNING), allow_create=False, background=True
            ),
            "a failed heartbeat PATCH reports no write",
        )
        self.assertEqual(
            client.edit_attempts - edits_before,
            1,
            "a heartbeat tick must make exactly ONE edit attempt",
        )
        self.assertEqual(
            client.scans - scans_before,
            0,
            "a heartbeat tick must never scan the Issue for markers",
        )
        self.assertEqual(client.creates, 0, "a heartbeat tick must never create")

    def test_a_background_write_without_an_owned_comment_does_not_resolve(self) -> None:
        # Ownership resolution is the owning thread's job. A tick with no known id must
        # stay silent rather than scanning to discover one.
        client = _BlockingStatusClient()
        store = Store.in_memory()
        self.addCleanup(store.close)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))  # noqa: SIM115
        self.addCleanup(log.stream.close)
        publisher = StatusPublisher(
            client=client, store=store, log=log, repo="owner/repo", issue_number=1
        )
        task = _owned_store_task(store)
        view = publisher.heartbeat_view(task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00")
        scans_before = client.scans
        self.assertFalse(
            publisher.publish(
                replace(view, state=STATE_RUNNING), allow_create=False, background=True
            ),
            "no owned comment means no heartbeat write",
        )
        self.assertEqual(client.scans - scans_before, 0, "and no scan to find one")
        self.assertEqual(client.edit_attempts, 0)
        self.assertEqual(client.creates, 0)

    def test_terminal_publication_does_not_wait_behind_a_heartbeat_scan(self) -> None:
        # The harm the round-3 blocker described: `begin_terminal()` waits on the shared
        # lock, and `_finish()` waits for `begin_terminal()` BEFORE the commit/push/PR
        # path. With a scan allowed in a tick, that wait could cover several sequential
        # wrapper calls. With a one-PATCH tick it covers at most one bounded call.
        client = _BlockingStatusClient()
        store = Store.in_memory()
        self.addCleanup(store.close)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))  # noqa: SIM115
        self.addCleanup(log.stream.close)
        publisher = StatusPublisher(
            client=client, store=store, log=log, repo="owner/repo", issue_number=1
        )
        task = _owned_store_task(store)
        view = publisher.heartbeat_view(task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00")
        self.assertTrue(
            publisher.begin(task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00")
        )

        # A failing tick, then terminal publication. The failure must not have queued up
        # a scan, so the terminal call returns promptly. Scans are counted from HERE:
        # `begin()` legitimately scans once to resolve ownership, and this assertion is
        # about the heartbeat, not the start path.
        scans_after_begin = client.scans
        client.fail_next_edit()
        publisher.publish(replace(view, state=STATE_RUNNING), allow_create=False, background=True)
        self.assertEqual(
            client.scans - scans_after_begin, 0, "no scan may be triggered by a heartbeat"
        )

        started = __import__("time").monotonic()
        publisher.begin_terminal()
        self.assertTrue(
            publisher.publish(replace(view, state=STATE_AWAITING_REVIEW), allow_create=False),
            "terminal publication must still succeed after a failed heartbeat tick",
        )
        self.assertLess(
            __import__("time").monotonic() - started,
            5.0,
            "terminal publication must not wait behind a heartbeat recovery chain",
        )
        self.assertIn(STATE_AWAITING_REVIEW, client.writes[-1])

    def test_a_background_write_is_refused_once_terminal_begins(self) -> None:
        # The unit-level form of the same invariant: the decision is taken under the
        # publisher's own lock, so it holds regardless of thread timing.
        from dataclasses import replace

        client = _BlockingStatusClient()
        store = Store.in_memory()
        self.addCleanup(store.close)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))  # noqa: SIM115
        self.addCleanup(log.stream.close)
        publisher = StatusPublisher(
            client=client, store=store, log=log, repo="owner/repo", issue_number=1
        )
        task = _owned_store_task(store)
        view = publisher.heartbeat_view(task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00")
        self.assertTrue(
            publisher.begin(task, attempt=1, run_started_at="2026-09-25T00:00:00+00:00")
        )

        publisher.begin_terminal()
        before = len(client.writes)
        self.assertFalse(
            publisher.publish(
                replace(view, state=STATE_RUNNING), allow_create=False, background=True
            ),
            "a background write must be refused once terminal publication began",
        )
        self.assertEqual(len(client.writes), before, "and it must not reach GitHub")

        # The owning thread must still be able to publish the terminal state itself.
        self.assertTrue(
            publisher.publish(replace(view, state=STATE_AWAITING_REVIEW), allow_create=False),
            "the owning thread must still publish the terminal state",
        )
        self.assertIn(STATE_AWAITING_REVIEW, client.writes[-1])


class StaleIdentityTests(StatusCommentCase):
    def _repos_block(self, model: str, effort: str) -> str:
        return f"""
[repos."{self.slug}"]
path = "{self.world.repo_path}"
base_branch = "main"
agents_file = "AGENTS.md"

[repos."{self.slug}".runtime]
driver = "commandcode"
model = "{model}"
effort = "{effort}"
permission_mode = "allow-all"
permission_flag = "--yolo"
max_turns = 40
"""

    def test_the_comment_shows_the_model_actually_invoked_after_a_config_change(self) -> None:
        # The review's finding 3. `task` is loaded during discovery; the claim then
        # pins the CURRENT runtime identity into SQLite and the driver invokes it. If
        # the status were rendered from the pre-claim snapshot, a config change between
        # discovery and dispatch would make the comment name a model that never ran —
        # exactly what the pinned identity exists to prevent.
        #
        # Asserting only the FINAL body would not catch this: the terminal sync
        # re-reads the durable row and is correct either way. The vulnerable renders
        # are the LIVE ones (`Starting`, `Running`) written during the run, so the
        # whole write history is checked.
        self.set_issues(issue(1, "Identity change", labels=[TRIGGER]))

        # Discovery happens at the OLD identity...
        self.world.write_config(
            worker_overrides=self.execution_overrides(),
            repos_block=self._repos_block("model-at-discovery", "low"),
        )
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)

        # ...then the config changes before the task is dispatched. The run is kept
        # alive so a live `Running` render really happens.
        self.world.write_config(
            worker_overrides=self.execution_overrides(),
            repos_block=self._repos_block("model-at-dispatch", "high"),
        )
        self.write_scenario(
            runs=[
                {
                    "session_id": "sess-1",
                    "subtype": "success",
                    "stream_seconds": 1.5,
                    "stream_tick": 0.2,
                    "edits": {"impl.txt": "done\n"},
                }
            ]
        )
        self.assertEqual(self.run_cli("run").returncode, 0)

        writes = self.status_writes()
        self.assertTrue(writes, "expected status writes")
        for entry in writes:
            with self.subTest(kind=entry["kind"]):
                self.assertNotIn(
                    "model-at-discovery",
                    entry["body"],
                    "no write may name a model that never ran this task",
                )
                self.assertIn("model-at-dispatch", entry["body"])

        body = self.status_comment_body()
        self.assertIn("model-at-dispatch", body, "the comment must name the invoked model")
        self.assertIn("high", body)

        # The durable row agrees with the comment, so a later sync cannot flip it.
        from agent_dispatch.config import load_config

        store = self.store_rows(load_config(self.world.config_path))
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.runtime_model, "model-at-dispatch")
        self.assertEqual(task.runtime_effort, "high")

    def test_a_spawn_failure_reports_failure_not_the_pre_claim_state(self) -> None:
        # The review's finding 4a. When the runtime preflight passes but the spawn then
        # fails, `_run_once` records the task as `failed` — but the caller used to sync
        # the status from the object it held BEFORE that transition, so the comment
        # could describe the task's old `queued` state while the database already knew
        # it had failed. The sync must re-read the row.
        self.set_issues(issue(1, "Spawn fails", labels=[TRIGGER]))
        # A runtime path that cannot be executed: preflight passes (the config is
        # valid) and the spawn itself is what fails.
        self.world.write_config(
            worker_overrides=self.execution_overrides(
                commandcode_path=str(self.tmp / "no-such-runtime")
            )
        )

        result = self.run_cli("run")
        self.assertNotEqual(result.returncode, 0, "a spawn failure is not a success")

        from agent_dispatch.config import load_config

        store = self.store_rows(load_config(self.world.config_path))
        task = store.get_task(self.slug, 1)
        self.assertEqual(task.phase, "failed", "the durable row records the failure")

        # The comment must agree with the durable row rather than describe `queued`.
        self.assertIn(STATE_FAILED, self.status_comment_body())
        self.assertNotIn(STATE_QUEUED, self.status_comment_body())
        self.assert_one_comment(why="a failed spawn still owns exactly one comment")


class PublicationFinalisationTests(StatusCommentCase):
    def test_recording_an_owned_pr_is_atomic_with_the_phase_and_stage(self) -> None:
        # Round-3 review: recording the PR, clearing the recovery stage and entering
        # `awaiting_review` were three autocommitted statements, which left two crash
        # windows. This asserts the durable invariants those windows violated, using the
        # single operation both the create and adopt paths now call.
        from agent_dispatch.config import load_config
        from agent_dispatch.store import Store as _Store

        self.set_issues(issue(1, "Atomic finalise", labels=[TRIGGER]))
        config = load_config(self.world.config_path)
        store = _Store(config.worker.state_db)
        self.addCleanup(store.close)
        store.upsert_discovered(
            repo=self.slug,
            issue_number=1,
            title="A task",
            base_branch="main",
            runtime_driver="commandcode",
            runtime_model="m",
            runtime_effort="high",
            permission_mode="allow-all",
            trigger_present=True,
            issue_state="open",
            linked_pr_number=None,
            linked_pr_state=None,
        )
        task = store.get_task(self.slug, 1)
        assert task is not None

        # Park it the way a failed publication would: needs_attention + a stage.
        store.park_for_recovery(task.id, stage="push_failed", note="boom")

        store.finalise_publication(task.id, pr_number=42, pr_url="https://example/42")

        after = store.get_task(self.slug, 1)
        assert after is not None
        # The three facts are true together, so neither crash window can be observed.
        self.assertEqual(after.pr_number, 42)
        self.assertIsNone(after.recovery_stage, "a stale stage must not survive")
        self.assertEqual(after.phase, "awaiting_review")
        self.assertFalse(after.is_publish_pending, "no lingering publish-pending state")

        # And the rendered state is the verified PR, not "Recovering publication".
        from agent_dispatch.statuscomment import task_view as build_view

        body = render_comment(build_view(after))
        self.assertIn(STATE_AWAITING_REVIEW, body)
        self.assertNotIn(STATE_RECOVERING, body)

    def test_finalisation_is_one_statement_so_it_cannot_half_apply(self) -> None:
        # The crash windows are BETWEEN statements, so a test that runs to completion
        # can never observe them — with three separate writes the previous test passes
        # either way. What makes them impossible is that the three facts are written by
        # ONE statement, so that is what is asserted here.
        source = (SRC / "agent_dispatch" / "store.py").read_text(encoding="utf-8")
        start = source.index("    def finalise_publication(")
        end = source.index("    # ----", start)
        body = source[start:end]
        self.assertEqual(
            body.count("self._conn.execute("),
            1,
            "finalise_publication must write the PR, the stage and the phase in a "
            "single statement; separate statements leave crash windows",
        )
        for column in ("pr_number = ?", "recovery_stage = NULL", "phase = 'awaiting_review'"):
            self.assertIn(column, body, f"{column} must be part of that one statement")

    def test_the_two_finalisation_call_sites_share_the_atomic_operation(self) -> None:
        # Both the create path and the adopt path must use it; a single leftover
        # three-write sequence would reintroduce the window on that path. The stale-stage
        # repair in `_reconcile_publish_pending` uses it too, so three call sites.
        source = (SRC / "agent_dispatch" / "orchestrator.py").read_text(encoding="utf-8")
        self.assertEqual(
            source.count("self.store.finalise_publication("),
            3,
            "the create path, the adopt path and the stale-stage repair must all "
            "finalise atomically",
        )
        self.assertNotIn(
            "self.store.record_owned_pr(",
            source,
            "no call site may write PR ownership outside finalise_publication",
        )
        # `clear_recovery_stage` alone is still legitimate on the handled-recovery path,
        # which leaves the phase as it found it. What must NOT come back is the crash
        # window: a stage clear immediately followed by a phase write.
        self.assertNotIn(
            "self.store.clear_recovery_stage(task.id)\n            self.store.set_phase(",
            source,
            "clearing the stage and setting the phase as separate writes is the crash "
            "window this fix removed",
        )

    def test_a_stale_stage_beside_an_owned_pr_is_repaired(self) -> None:
        # Rows already written by the old three-write sequence must self-heal: the
        # reconcile pass returns early when a PR is owned, which is exactly what used to
        # stop the stale stage from ever being cleared.
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.orchestrator import Orchestrator
        from agent_dispatch.store import Store as _Store

        self.set_issues(issue(1, "Stale stage", labels=[TRIGGER]))
        config = load_config(self.world.config_path)
        store = _Store(config.worker.state_db)
        self.addCleanup(store.close)
        store.upsert_discovered(
            repo=self.slug,
            issue_number=1,
            title="A task",
            base_branch="main",
            runtime_driver="commandcode",
            runtime_model="m",
            runtime_effort="high",
            permission_mode="allow-all",
            trigger_present=True,
            issue_state="open",
            linked_pr_number=None,
            linked_pr_state=None,
        )
        task = store.get_task(self.slug, 1)
        assert task is not None
        # Exactly the crash-window-1 row: an owned PR AND a publishable stage.
        store.record_owned_pr(task.id, pr_number=9, pr_url="https://example/9")
        store.park_for_recovery(task.id, stage="push_failed", note="crashed mid-finalise")
        stale = store.get_task(self.slug, 1)
        assert stale is not None
        self.assertTrue(stale.is_publish_pending, "precondition: the row is inconsistent")

        log = Logger(fmt="text", stream=open(os.devnull, "w"))  # noqa: SIM115
        self.addCleanup(log.stream.close)
        orchestrator = Orchestrator(config, store, GitHubClient(config.github.command), log)

        # `reconcile()` is the real entry point for a stale publish-pending row (it runs
        # once at startup and on `resume-publish`), not the status-sync pass.
        notes = orchestrator.reconcile()

        healed = store.get_task(self.slug, 1)
        assert healed is not None
        # ALL THREE facts must be restored, not just the stage. Clearing only the stage
        # would leave the *other* inconsistent state from the same crash (owned PR + no
        # stage + `needs_attention`), which is wrong for anything gating on a real
        # `awaiting_review` — exactly the gap this repair previously had.
        self.assertEqual(healed.pr_number, 9, "the owned PR must be kept")
        self.assertIsNone(healed.recovery_stage, "the stale stage must be cleared")
        self.assertEqual(
            healed.phase,
            "awaiting_review",
            "publication completed, so the phase must be normalised too",
        )
        self.assertFalse(healed.is_publish_pending)
        self.assertTrue(
            any("stale recovery stage" in note for note in notes),
            f"the repair must be reported, saw {notes}",
        )

        # And the rendered status agrees.
        from agent_dispatch.statuscomment import task_view as build_view

        body = render_comment(build_view(healed))
        self.assertIn(STATE_AWAITING_REVIEW, body)
        self.assertNotIn(STATE_RECOVERING, body)

    def test_an_owned_pr_without_a_stage_is_not_touched(self) -> None:
        # The repair is deliberately limited to the exact crash signature (owned PR AND
        # a publishable stage). A `needs_attention` row with an owned PR but no stage may
        # be a legitimate later intervention, so it must NOT be normalised — a broad
        # "needs_attention + pr_number => awaiting_review" rule would erase that.
        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.orchestrator import Orchestrator
        from agent_dispatch.store import Store as _Store

        self.set_issues(issue(1, "Deliberate intervention", labels=[TRIGGER]))
        config = load_config(self.world.config_path)
        store = _Store(config.worker.state_db)
        self.addCleanup(store.close)
        store.upsert_discovered(
            repo=self.slug,
            issue_number=1,
            title="A task",
            base_branch="main",
            runtime_driver="commandcode",
            runtime_model="m",
            runtime_effort="high",
            permission_mode="allow-all",
            trigger_present=True,
            issue_state="open",
            linked_pr_number=None,
            linked_pr_state=None,
        )
        task = store.get_task(self.slug, 1)
        assert task is not None
        # An owned PR with NO publishable stage: not the crash signature.
        store.record_owned_pr(task.id, pr_number=9, pr_url="https://example/9")
        store.set_phase(task.id, "needs_attention", "an operator parked this later")

        log = Logger(fmt="text", stream=open(os.devnull, "w"))  # noqa: SIM115
        self.addCleanup(log.stream.close)
        orchestrator = Orchestrator(config, store, GitHubClient(config.github.command), log)
        orchestrator.reconcile()

        after = store.get_task(self.slug, 1)
        assert after is not None
        self.assertEqual(
            after.phase,
            "needs_attention",
            "a deliberate intervention must not be normalised away",
        )
        self.assertEqual(after.pr_number, 9)


class PreStatusSchemaTests(StatusCommentCase):
    def _drop_status_table(self) -> None:
        """Model a database written by the merged #16 build (no status_comments)."""
        from agent_dispatch.config import load_config

        config = load_config(self.world.config_path)
        store = self.store_rows(config)
        store._conn.execute("DROP TABLE IF EXISTS status_comments")
        store.close()

    def test_read_only_commands_work_against_a_pre_17_database(self) -> None:
        # Read-only stores deliberately do not migrate, so an upgraded database has no
        # `status_comments` table until a write path opens it. `status`, `open` and
        # `dry-run` must still work — and must not create it, because that would make a
        # read command a writer.
        self.set_issues(issue(1, "Upgrade path", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        self._drop_status_table()

        for argv in (
            ("status",),
            ("open", "--repo", self.slug, "--issue", "1"),
            ("dry-run",),
        ):
            with self.subTest(command=argv[0]):
                result = self.run_cli(*argv)
                self.assertEqual(
                    result.returncode,
                    0,
                    f"`{argv[0]}` must survive a pre-#17 database:\n{result.stderr}",
                )
                self.assertNotIn("no such table", result.stderr)

        # Still absent: reading must not have added it.
        from agent_dispatch.config import load_config
        from agent_dispatch.store import Store

        config = load_config(self.world.config_path)
        store = Store.open_read_only(config.worker.state_db)
        try:
            self.assertFalse(
                store._table_present("status_comments"),
                "a read-only command must not create the table",
            )
            self.assertIsNone(store.status_comment(self.slug, 1))
        finally:
            store.close()

    def test_a_write_path_adds_the_table_afterwards(self) -> None:
        self.set_issues(issue(1, "Recreate", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)
        self._drop_status_table()

        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)

        from agent_dispatch.config import load_config

        store = self.store_rows(load_config(self.world.config_path))
        self.assertTrue(
            store._table_present("status_comments"),
            "the next write-path open must create the table normally",
        )


class PublisherGuardTests(StatusCommentCase):
    def test_publish_without_an_owned_comment_does_not_create(self) -> None:
        # Only the owning start path may create; a heartbeat-style or synchronisation
        # publish with no resolved id must be a silent no-op instead.
        self.set_issues(issue(1, "No create", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)

        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger

        config = load_config(self.world.config_path)
        store = self.store_rows(config)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))
        self.addCleanup(log.stream.close)
        publisher = StatusPublisher(
            client=GitHubClient(config.github.command),
            store=store,
            log=log,
            repo=self.slug,
            issue_number=1,
        )
        view = task_view(store.get_task(self.slug, 1), state=STATE_RUNNING, process_alive=True)
        self.assertFalse(publisher.publish(view, allow_create=False))
        self.assertEqual(self.status_comments(), [])

    def test_sync_is_a_noop_for_a_task_that_never_owned_a_comment(self) -> None:
        self.set_issues(issue(1, "Never owned", labels=[TRIGGER]))
        self.assertEqual(self.run_cli("worker", "--once", "--no-execute").returncode, 0)

        from agent_dispatch.config import load_config
        from agent_dispatch.github import GitHubClient
        from agent_dispatch.logging_setup import Logger
        from agent_dispatch.orchestrator import Orchestrator

        config = load_config(self.world.config_path)
        store = self.store_rows(config)
        log = Logger(fmt="text", stream=open(os.devnull, "w"))
        self.addCleanup(log.stream.close)
        orchestrator = Orchestrator(config, store, GitHubClient(config.github.command), log)
        task = store.get_task(self.slug, 1)
        self.assertFalse(orchestrator.sync_task_status(task))
        self.assertEqual(self.status_comments(), [], "nothing is created for an unclaimed task")
        self.assertEqual(orchestrator.sync_status_comments(), [])


if __name__ == "__main__":  # pragma: no cover - run via the suite
    unittest.main()

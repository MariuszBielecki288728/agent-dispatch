#!/usr/bin/env python3
"""A fake Command Code runtime, used only by the offline test-suite.

Not part of the service. It exists so the tests exercise the real
:class:`~agent_dispatch.runtime.CommandCodeDriver` — the real argv construction,
the real process spawn, the real NDJSON parsing and the real validation rules —
instead of a mock that could drift from production behaviour.

The scenario is read from the JSON file named by ``FAKE_RUNTIME_SCENARIO``, which
describes what each successive invocation should emit and do. Because the fake is
a real executable, the tests can prove things a stub cannot:

* ``tool_hook_blocked`` is detected **even when** the run reports
  ``subtype=success`` and exits 0 — the silent false-success case (§2.2);
* a session ID is captured from the first line of the stream;
* an interrupted run leaves no transcript and a fresh session starts on retry;
* a timeout really kills the process group, leaving no orphan holding the worktree.

Scenario shape::

    {
      "runs": [
        {
          "session_id": "sess-1",            # omitted => a generated id
          "subtype": "success",              # result subtype
          "exit_code": 0,
          "events": ["tool_hook_blocked"],   # extra event kinds to emit
          "edits": {"file.txt": "content"},  # files to write into the cwd
          "commit": false,                   # git add/commit the working tree
          "sleep": 0,                        # seconds to sleep before starting
          "stream_seconds": 0,               # seconds to stay in flight AFTER run_start
          "stream_tick": 0.1,                # interval between streamed events
          "hang": false,                     # never finish (for timeout tests)
          "result_session_id": null          # override the id in the result line
        }
      ],
      "record_argv": "path"                  # append the argv to this file
    }

``stream_seconds`` is what makes a status-heartbeat test meaningful: the process is
really alive and really emitting for that long, so a heartbeat that only fires from
the polling loop — rather than while the driver is blocked on the stream — cannot
pass. The events it emits are ordinary ``tool_completed`` records, which is also how
a test can show the last *observed event* time is distinct from the heartbeat time.

No network access, no model credits, no GitHub mutation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path


def die(message: str, code: int = 2) -> None:
    print(message, file=sys.stderr)
    sys.exit(code)


def load_scenario() -> dict:
    path = os.environ.get("FAKE_RUNTIME_SCENARIO")
    if not path:
        die("FAKE_RUNTIME_SCENARIO is not set")
    scenario_path = Path(path)
    if not scenario_path.is_file():
        die(f"FAKE_RUNTIME_SCENARIO does not exist: {scenario_path}")
    try:
        return json.loads(scenario_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        die(f"cannot read FAKE_RUNTIME_SCENARIO: {exc}")


def load_state(scenario: dict) -> dict:
    """Per-invocation state, stored beside the scenario so it survives processes."""
    state_path = Path(str(scenario.get("_path", "")) + ".state")
    if state_path.is_file():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"invocations": 0}
    return {"invocations": 0}


def save_state(scenario: dict, state: dict) -> None:
    state_path = Path(str(scenario.get("_path", "")) + ".state")
    try:
        state_path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass


def emit(payload: dict) -> None:
    """Write one NDJSON line and flush immediately.

    Flushing matters: a test that kills the fake mid-run must still see the lines
    it emitted before dying, exactly as a real runtime would have produced them.
    """
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def record_argv(scenario: dict) -> None:
    target = scenario.get("record_argv")
    if not target:
        return
    try:
        with open(str(target), "a", encoding="utf-8") as handle:
            handle.write(json.dumps(sys.argv[1:]) + "\n")
    except OSError:
        pass


def apply_edits(run: dict) -> None:
    """Write the configured files into the current working directory."""
    for relative, content in (run.get("edits") or {}).items():
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts:
            die(f"fake runtime: refusing to write outside the worktree: {relative}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")


def maybe_commit(run: dict) -> None:
    """Optionally commit the working tree, modelling an agent that commits its work."""
    if not run.get("commit"):
        return
    env = dict(os.environ)
    for command in (
        ["git", "add", "-A"],
        [
            "git",
            "-c",
            "user.email=fake@example.invalid",
            "-c",
            "user.name=Fake Agent",
            "commit",
            "-m",
            run.get("commit_message", "apply changes"),
        ],
    ):
        subprocess.run(command, capture_output=True, text=True, env=env, check=False)


def main(argv: list[str]) -> int:
    scenario = load_scenario()
    scenario["_path"] = os.environ.get("FAKE_RUNTIME_SCENARIO", "")
    record_argv(scenario)

    # `--help`/`--version` are answered without consuming a scripted run, so a
    # capability probe cannot desynchronise a test's scenario.
    if "--version" in argv or "version" in argv[:1]:
        print("fake commandcode 0.0.0")
        return 0
    if "--help" in argv:
        print("fake commandcode — offline test double")
        return 0

    state = load_state(scenario)
    index = int(state.get("invocations", 0))
    state["invocations"] = index + 1
    save_state(scenario, state)

    runs = scenario.get("runs") or []
    if not runs:
        die("fake runtime: scenario has no runs")
    # Past the last scripted run, repeat the final one. A test that forgets a
    # scenario still gets a deterministic, inspectable result rather than an
    # unexplained crash.
    run = runs[index] if index < len(runs) else runs[-1]

    session_id = run.get("session_id") or f"sess-{uuid.uuid4().hex[:8]}"
    requested = _flag_value(argv, "--session")

    if run.get("hang"):
        # Emit the start line (so the session ID is captured) then sleep well past
        # any test timeout: this is what exercises the watchdog's kill path.
        emit({"type": "run_start", "sessionId": session_id})
        while True:
            time.sleep(1)

    sleep_for = float(run.get("sleep") or 0)
    if sleep_for:
        time.sleep(sleep_for)

    emit({"type": "run_start", "sessionId": session_id, "requestedSession": requested})

    # A run that stays in flight, emitting events as it goes. This happens BEFORE the
    # scripted `events` list so a scenario can put a blocked-tool event after a period
    # of ordinary activity, exactly as a real run would.
    stream_seconds = float(run.get("stream_seconds") or 0)
    if stream_seconds > 0:
        tick = max(float(run.get("stream_tick") or 0.1), 0.01)
        deadline = time.monotonic() + stream_seconds
        index = 0
        while time.monotonic() < deadline:
            emit(
                {
                    "type": "event",
                    "event": {
                        "type": "tool_completed",
                        "toolName": f"stream-step-{index}",
                    },
                }
            )
            index += 1
            time.sleep(min(tick, max(deadline - time.monotonic(), 0.0)))

    for kind in run.get("events") or []:
        if kind == "tool_hook_blocked":
            # The real shape: nested under "event". Emitted BEFORE the result line,
            # exactly as observed on the VM.
            emit(
                {
                    "type": "event",
                    "event": {
                        "type": "tool_hook_blocked",
                        "toolName": run.get("blocked_tool", "write_file"),
                        "hookOutput": (
                            'Error: Tool "write_file" requires permissions. Use --yolo '
                            "(or --dangerously-skip-permissions) to enable file writes and shell "
                            "commands in print mode."
                        ),
                    },
                }
            )
        elif kind == "tool_completed":
            emit(
                {
                    "type": "event",
                    "event": {"type": "tool_completed", "toolName": run.get("tool", "write_file")},
                }
            )
        else:
            emit({"type": "event", "event": {"type": kind}})

    # File mutation only happens for a run that is not blocked, so a blocked run
    # really does leave an untouched worktree — the point of the false-success case.
    if "tool_hook_blocked" not in (run.get("events") or []):
        apply_edits(run)
        maybe_commit(run)

    result_session = run.get("result_session_id", session_id)
    emit(
        {
            "type": "result",
            "subtype": run.get("subtype", "success"),
            "stopReason": run.get("stop_reason", "end_turn"),
            "sessionId": result_session,
            "finalText": run.get("final_text", "Fake run complete."),
            "usage": run.get(
                "usage",
                {
                    "inputTokens": 100,
                    "outputTokens": 50,
                    "cacheReadTokens": 10,
                    "cacheWriteTokens": 5,
                },
            ),
            "durationMs": int(run.get("duration_ms", 1234)),
        }
    )

    stderr_text = run.get("stderr")
    if stderr_text:
        print(str(stderr_text), file=sys.stderr)

    return int(run.get("exit_code", 0))


def _flag_value(argv: list[str], flag: str) -> str | None:
    for index, token in enumerate(argv):
        if token == flag and index + 1 < len(argv):
            return argv[index + 1]
    return None


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""The Command Code runtime driver.

The smallest boundary that covers Issues #4–#6 (``docs/architecture.md`` §3):

```
start(task) -> RunResult      # new session; returns session_id + outcome
resume(task, instruction) -> RunResult
status(task) -> RuntimeStatus
stop(task) -> None
```

Only :class:`CommandCodeDriver` is implemented — a future runtime means one new
adapter, not a provider-plugin framework.

Correctness rules taken from the verified ground truth (§2.1–§2.3.1), each of
which a naive implementation gets wrong:

**`--yolo` is the only working mutation unlock** and is re-passed on *every*
invocation including a resume, because a permission grant does not persist inside
a resumed session. ``--permission-mode yolo`` and ``--tools-all`` were both
observed to leave writes blocked. The flag string comes from config, so the
effective grant is auditable rather than implied.

**`tool_hook_blocked` is a hard failure.** A fully blocked run still reports
``subtype=success``, ``stopReason=end_turn``, exit code 0 and no ``isError``
anywhere. The only in-band signal is that NDJSON event, so it is scanned for on
every run and any occurrence fails the run — this is the single most important
rule in the design (§2.2, §8).

**An interrupted first run is not resumable.** The transcript is written on clean
completion; a killed run leaves only ``.meta.json``/``.checkpoints.jsonl`` and
``--session <id>`` then fails outright. So an early session ID is captured (it
arrives on line 1) and recorded, but a retry after an interruption starts a
*fresh* session and says so, instead of advertising a resume that cannot work.

**The session ID is treated as data and validated.** On a resume the returned ID
must equal the pinned one, otherwise the run is failed rather than silently
starting a new conversation under the old task row.

Streams are parsed as **data**, never evaluated: the agent's own output can
contain anything, including text shaped like an instruction to the orchestrator.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Sequence

from .config import RuntimeConfig
from .runlogs import run_dir, run_log_path
from .util import utcnow_iso

#: Exit code Command Code uses when the turn cap is hit (per ``--help``). Distinct
#: from a normal failure (1), and treated as a bounded failure rather than success.
EXIT_TURN_CAP = 8

#: The NDJSON event that proves a tool call was refused for lack of permission.
#: Its presence means the run may have written nothing while still reporting
#: success, so it fails the run.
TOOL_HOOK_BLOCKED_EVENT = "tool_hook_blocked"


class RuntimeSpawnError(Exception):
    """The runtime could not be started at all (missing binary, spawn failure)."""


@dataclass
class Usage:
    """Token accounting, when the runtime reports it."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    @classmethod
    def from_raw(cls, raw: Mapping[str, Any]) -> "Usage":
        def pick(key: str) -> int | None:
            value = raw.get(key)
            return int(value) if isinstance(value, (int, float)) else None

        return cls(
            input_tokens=pick("inputTokens"),
            output_tokens=pick("outputTokens"),
            cache_read_tokens=pick("cacheReadTokens"),
            cache_write_tokens=pick("cacheWriteTokens"),
        )

    def summary(self) -> str:
        parts = [
            f"{name}={value}"
            for name, value in (
                ("in", self.input_tokens),
                ("out", self.output_tokens),
                ("cache_read", self.cache_read_tokens),
                ("cache_write", self.cache_write_tokens),
            )
            if value is not None
        ]
        return " ".join(parts) or "unavailable"


@dataclass
class RunValidation:
    """Why a run was accepted or rejected. Every check below is evidence-based."""

    ok: bool
    problems: list[str] = field(default_factory=list)
    checks: dict[str, bool] = field(default_factory=dict)

    def summary(self) -> str:
        if self.ok:
            return "all checks passed"
        return "; ".join(self.problems)


@dataclass
class RunResult:
    """Everything one runtime invocation produced, as observed — not assumed."""

    run_id: str
    session_id: str | None
    exit_code: int | None
    subtype: str | None
    stop_reason: str | None
    final_text: str
    usage: Usage | None
    duration_ms: int | None
    tool_hook_blocked: bool
    #: Session this run was *asked* to continue, when it was a resume.
    resumed_from: str | None
    log_path: Path | None
    validation: RunValidation
    started_at: str
    finished_at: str
    timed_out: bool = False
    spawn_error: str | None = None
    #: Set when an ``on_session`` callback raised. The run outcome is unaffected —
    #: the agent's work is still valid — but the caller is told, so it can report
    #: that the session ID was not persisted rather than leaving it unexplained.
    session_callback_error: str | None = None
    events_seen: int = 0
    result_lines: int = 0

    @property
    def ok(self) -> bool:
        return self.validation.ok

    @property
    def fresh_session_started(self) -> bool:
        """True when this run began a new session rather than continuing one."""
        return self.resumed_from is None

    def summary_fields(self) -> dict[str, Any]:
        """Compact, log-safe description. Never includes transcript contents."""
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "resumed_from": self.resumed_from,
            "exit_code": self.exit_code,
            "subtype": self.subtype,
            "stop_reason": self.stop_reason,
            "duration_ms": self.duration_ms,
            "tool_hook_blocked": self.tool_hook_blocked,
            "events": self.events_seen,
            "usage": self.usage.summary() if self.usage else "unavailable",
            "log": str(self.log_path) if self.log_path else None,
        }


@dataclass
class RuntimeStatus:
    """What is known about a task's session without spending a model call."""

    driver: str
    session_id: str | None
    resumable: bool
    note: str


def parse_ndjson_line(line: str) -> dict[str, Any] | None:
    """Parse one NDJSON line as data, or return ``None`` when it is not an object.

    Anything unparseable is skipped rather than failing the run: the runtime may
    emit non-JSON noise, and the *validation* of what was captured is what decides
    the outcome. Streaming this way also means a truncated final line does not
    destroy the earlier evidence.
    """
    text = line.strip()
    if not text or not text.startswith("{"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def is_tool_hook_blocked(event: Mapping[str, Any]) -> bool:
    """Whether one parsed NDJSON record is a blocked-tool event.

    The event arrives nested (``{"type":"event","event":{"type":"tool_hook_blocked"}}``)
    but the observed shape has varied, so the marker is looked for both nested and
    at the top level. A false positive costs one failed run; a false negative
    records success for work that was never written.
    """
    if event.get("type") == TOOL_HOOK_BLOCKED_EVENT:
        return True
    nested = event.get("event")
    if isinstance(nested, Mapping) and nested.get("type") == TOOL_HOOK_BLOCKED_EVENT:
        return True
    return False


class CommandCodeDriver:
    """Invoke Command Code for one task and classify the result honestly."""

    def __init__(
        self,
        runtime: RuntimeConfig,
        *,
        run_log_dir: Path | str,
        repo: str,
        issue_number: int,
        binary: str | None = None,
        timeout_seconds: float = 3600.0,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.runtime = runtime
        self.run_log_dir = Path(run_log_dir)
        self.repo = repo
        self.issue_number = issue_number
        self.binary = binary or "commandcode"
        self.timeout_seconds = timeout_seconds
        self.env = dict(env or {})

    def check_available(self) -> Path:
        """Resolve the runtime binary, refusing clearly when it is not installed.

        The orchestrator calls this before claiming anything, so a missing runtime
        is reported as a configuration fault instead of failing the first task.
        """
        resolved = shutil.which(self.binary) if not os.path.isabs(self.binary) else None
        path = Path(self.binary) if os.path.isabs(self.binary) else Path(resolved or "")
        if not resolved and not (path.is_file() and os.access(path, os.X_OK)):
            raise RuntimeSpawnError(
                f"runtime binary {self.binary!r} was not found or is not executable; install the "
                "runtime or set worker.commandcode_path in the configuration"
            )
        return path

    # ------------------------------------------------------------------ argv

    def build_argv(self, instruction: str, *, session_id: str | None = None) -> list[str]:
        """The exact command line, with the pinned identity re-passed every time.

        ``--yolo`` and the pinned ``--model``/``--effort`` appear on every
        invocation including resumes: permission grants do not persist, and
        re-passing the model is what makes "a config change silently moves an
        existing session onto another model" structurally impossible.
        """
        argv = [self.binary, "-p", instruction]
        if session_id:
            argv += ["--session", session_id]
        argv += ["--model", self.runtime.model]
        if self.runtime.effort:
            argv += ["--effort", self.runtime.effort]
        argv += [self.runtime.permission_flag]
        argv += ["--output-format", "json"]
        argv += ["--max-turns", str(self.runtime.max_turns)]
        argv += ["--skip-onboarding", "--no-auto-update"]
        return argv

    # ------------------------------------------------------------------- run

    def run(
        self,
        *,
        worktree: Path | str,
        instruction: str,
        run_id: str,
        session_id: str | None = None,
        on_session: Callable[[str], None] | None = None,
    ) -> RunResult:
        """Execute one invocation, streaming NDJSON to a log outside every repo.

        The log is written by this process (not by a shell redirect inside the
        worktree), which is what keeps raw transcripts out of the repository where
        ``git add -A`` could sweep them into a PR.

        ``on_session`` is invoked **once**, the first time the stream reveals a
        session ID (it arrives on the first line). Returning the ID only in the
        result would lose it in a hard crash — exactly the case where knowing which
        session was attempted matters — so the caller persists it from the callback
        while the run is still in flight.
        """
        worktree = Path(worktree)
        log_path = run_log_path(self.run_log_dir, self.repo, self.issue_number, run_id)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        started_at = utcnow_iso()
        argv = self.build_argv(instruction, session_id=session_id)
        env = dict(os.environ)
        env.update(self.env)
        # stderr is written straight to a sidecar file rather than a pipe. Two
        # pipes read sequentially can deadlock (a child that fills the stderr
        # buffer blocks before it finishes stdout, and the reader is still waiting
        # on stdout), and a deadlocked run would hang the whole worker.
        stderr_path = _stderr_path(log_path)

        state = _StreamState()
        deadline_hit = False

        # New process group: a hung agent and anything it spawned are killed
        # together. Killing only the direct child would leave a grandchild holding
        # the worktree, which is exactly the state that corrupts a later run.
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(worktree),
                stdout=subprocess.PIPE,
                stderr=stderr_path.open("w", encoding="utf-8"),
                text=True,
                env=env,
                start_new_session=True,
            )
        except FileNotFoundError as exc:
            raise RuntimeSpawnError(
                f"runtime binary {self.binary!r} was not found on PATH; set "
                "worker.commandcode_path or install the runtime"
            ) from exc
        except OSError as exc:  # pragma: no cover - environment specific
            raise RuntimeSpawnError(f"could not start runtime {self.binary!r}: {exc}") from exc

        def on_deadline() -> None:
            # The flag is set only by this callback, so the timeout verdict cannot
            # be a race against a run that finished on its own at the same instant.
            nonlocal deadline_hit
            deadline_hit = True
            self._kill_group(proc)

        watchdog = threading.Timer(self.timeout_seconds, on_deadline)
        watchdog.daemon = True
        watchdog.start()
        log_write_error: str | None = None
        session_callback_error: str | None = None

        try:
            with log_path.open("w", encoding="utf-8") as log_file:
                for line in _iter_stdout(proc):
                    if not deadline_hit:
                        # After the deadline the remaining output is a killed
                        # process's death rattle; the log stops at the evidence we
                        # actually needed to classify the run.
                        log_file.write(line if line.endswith("\n") else line + "\n")
                    event = parse_ndjson_line(line)
                    if event is not None:
                        had_session = state.session_id is not None
                        state.observe(event)
                        if on_session and state.session_id and not had_session:
                            # Persist the ID the moment it appears, so a crash after
                            # this point still records which session was attempted.
                            # This does NOT make the run resumable: resumability is
                            # decided separately, from whether the run completed.
                            try:
                                on_session(state.session_id)
                            except Exception as exc:
                                # Never fatal, but never swallowed either: the caller
                                # is told so it can report that the session could not
                                # be persisted, instead of the ID quietly going missing.
                                session_callback_error = str(exc)
                log_file.flush()
        except OSError as exc:
            log_write_error = f"could not write the run log {log_path}: {exc}"
        finally:
            watchdog.cancel()

        exit_code = _finish_process(proc)
        if proc.stderr is not None:  # pragma: no cover - closed with the process
            proc.stderr.close()
        finished_at = utcnow_iso()

        result = RunResult(
            run_id=run_id,
            session_id=state.session_id,
            exit_code=exit_code,
            subtype=state.subtype,
            stop_reason=state.stop_reason,
            final_text=state.final_text,
            usage=state.usage,
            duration_ms=state.duration_ms,
            tool_hook_blocked=state.tool_hook_blocked,
            resumed_from=session_id,
            log_path=log_path,
            validation=RunValidation(ok=False),
            started_at=started_at,
            finished_at=finished_at,
            timed_out=deadline_hit,
            spawn_error=log_write_error,
            session_callback_error=session_callback_error,
            events_seen=state.events_seen,
            result_lines=state.result_lines,
        )
        result.validation = validate_run(result, expected_session=session_id)
        return result

    def status(self, session_id: str | None) -> RuntimeStatus:
        """Report resumability **without** spending a model call.

        The honest answer for an interrupted first run is "not resumable": a
        transcript exists only after a clean completion, so this never promises a
        resume that would fail (§2.3.1).
        """
        if not session_id:
            return RuntimeStatus(
                driver=self.runtime.driver,
                session_id=None,
                resumable=False,
                note="no session has been started for this task",
            )
        return RuntimeStatus(
            driver=self.runtime.driver,
            session_id=session_id,
            resumable=True,
            note="a completed run recorded this session; a resume re-passes the pinned model/effort/flag",
        )

    def stop(self, process: subprocess.Popen[str] | None) -> None:
        """Terminate a live run's whole process group, escalating to SIGKILL."""
        if process is None:
            return
        self._kill_group(process, escalate=True)

    def _kill_group(self, process: "subprocess.Popen[str]", *, escalate: bool = False) -> None:
        """Kill the process group so no grandchild keeps holding the worktree."""
        if process.poll() is not None:
            return
        if not _signal_group(process, signal.SIGKILL if not escalate else signal.SIGTERM):
            return
        if not escalate:
            return
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                return
            time.sleep(0.2)
        _signal_group(process, signal.SIGKILL)


class _StreamState:
    """Accumulates the facts a validation decision needs, as they stream in."""

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.subtype: str | None = None
        self.stop_reason: str | None = None
        self.final_text: str = ""
        self.usage: Usage | None = None
        self.duration_ms: int | None = None
        self.tool_hook_blocked = False
        self.events_seen = 0
        self.result_lines = 0

    def observe(self, event: Mapping[str, Any]) -> None:
        self.events_seen += 1

        # The session ID arrives on the first line (`run_start`); capturing it
        # immediately is what makes an interrupted run traceable even though it
        # will not be resumable.
        candidate = event.get("sessionId") or event.get("session_id")
        if isinstance(candidate, str) and candidate and self.session_id is None:
            self.session_id = candidate

        if is_tool_hook_blocked(event):
            self.tool_hook_blocked = True

        if event.get("type") == "result":
            self.result_lines += 1
            subtype = event.get("subtype")
            if isinstance(subtype, str):
                self.subtype = subtype
            stop_reason = event.get("stopReason")
            if isinstance(stop_reason, str):
                self.stop_reason = stop_reason
            text = event.get("finalText")
            if isinstance(text, str):
                self.final_text = text
            raw_usage = event.get("usage")
            if isinstance(raw_usage, Mapping):
                self.usage = Usage.from_raw(raw_usage)
            duration = event.get("durationMs")
            if isinstance(duration, (int, float)):
                self.duration_ms = int(duration)
            # A result line may repeat the session ID; a later, different ID would
            # mean the runtime switched sessions, which validation rejects.
            result_session = event.get("sessionId")
            if isinstance(result_session, str) and result_session:
                self.session_id = result_session if self.session_id is None else self.session_id


def validate_run(result: RunResult, *, expected_session: str | None) -> RunValidation:
    """Decide whether a run may be treated as a success. Every failure is named.

    The checks mirror §8's mandatory post-run validation. Rule 5
    (``tool_hook_blocked``) is the false-success guard and fails the run *even
    when* the process exited 0 and reported ``subtype=success``.
    """
    problems: list[str] = []
    checks: dict[str, bool] = {}

    if result.spawn_error:
        checks["spawned"] = False
        problems.append(result.spawn_error)
    else:
        checks["spawned"] = True

    if result.timed_out:
        checks["completed_in_time"] = False
        problems.append(
            f"run exceeded the {_format_seconds(result.duration_ms)} wall-clock timeout and was killed"
        )
    else:
        checks["completed_in_time"] = True

    exit_ok = result.exit_code == 0
    checks["exit_code_zero"] = exit_ok
    if not exit_ok and not result.timed_out:
        if result.exit_code == EXIT_TURN_CAP:
            problems.append(
                f"turn cap reached (exit {EXIT_TURN_CAP}); the run ended mid-task and counts as a "
                "bounded failure, not a success"
            )
        else:
            problems.append(f"runtime exited with code {result.exit_code}")

    subtype_ok = result.subtype == "success"
    checks["subtype_success"] = subtype_ok
    if not subtype_ok:
        problems.append(
            "no completed result with subtype=success in the stream"
            + (f" (saw subtype={result.subtype!r})" if result.subtype else "")
        )

    session_ok = bool(result.session_id)
    checks["session_id_captured"] = session_ok
    if not session_ok:
        problems.append("no session_id was emitted; the task cannot be pinned or later resumed")

    matched = True
    if expected_session is not None:
        matched = result.session_id == expected_session
    checks["session_id_matches"] = matched
    if not matched:
        problems.append(
            f"resume returned session_id {result.session_id!r}, which does not match the pinned "
            f"{expected_session!r}; refusing to treat this as the same conversation"
        )

    not_blocked = not result.tool_hook_blocked
    checks["no_blocked_tool_events"] = not_blocked
    if not not_blocked:
        # Called out separately because this is the case the runtime reports as a
        # success: silent false success is the highest-risk observed failure mode.
        problems.append(
            f"the stream contains {TOOL_HOOK_BLOCKED_EVENT} events, so at least one tool call was "
            "refused; the runtime still reports subtype=success and exit 0, but the work was not "
            "performed"
        )

    return RunValidation(ok=not problems, problems=problems, checks=checks)


def _signal_group(process: "subprocess.Popen[str]", sig: int) -> bool:
    """Signal the process group, returning whether the signal was delivered."""
    try:
        os.killpg(os.getpgid(process.pid), sig)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _iter_stdout(proc: "subprocess.Popen[str]") -> Iterator[str]:
    """Yield stdout lines until EOF.

    A plain blocking read is safe here **because** the watchdog timer kills the
    process group at the wall-clock deadline: a hung run with no output closes
    its pipe when it is killed, which ends this loop. A truncated final line is
    still handed to the parser, so a killed run keeps whatever evidence it
    emitted before dying.
    """
    assert proc.stdout is not None
    for line in proc.stdout:
        yield line


def _finish_process(proc: "subprocess.Popen[str]") -> int | None:
    """Reap the process, killing the group if it outlives its stdout pipe."""
    try:
        return proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        _signal_group(proc, signal.SIGKILL)
        return proc.wait(timeout=10)


def _stderr_path(log_path: Path) -> Path:
    """Sidecar file holding the runtime's stderr for one run.

    Deliberately not the ``.ndjson`` file: that stream is parsed as data, and
    mixing free-form stderr into it would make a failed run look like a malformed
    one. The file is bounded by the runtime itself, not by this module.
    """
    return log_path.with_suffix(".stderr.txt")


def _format_seconds(duration_ms: int | None) -> str:
    if duration_ms is None:
        return "unknown-length"
    return f"{duration_ms / 1000:.0f}s"


def next_run_id(prefix: str = "run", *, now: "datetime | None" = None) -> str:
    """A unique, sortable run id: ``<UTC stamp to microseconds>-<prefix>-<nonce>``.

    Two properties are load-bearing:

    * **Sortable by name.** :func:`~agent_dispatch.runlogs.prune` keeps the newest
      logs by name comparison, so the timestamp must come first and be fixed-width.
    * **Unique even within one microsecond.** ``run_id`` is part of the ``runs``
      uniqueness constraint *and* of the log filename. Second resolution (the first
      version of this function) let two rapid runs collide, which both violated the
      constraint and silently overwrote the earlier run's log — destroying the
      evidence for the run that actually needed explaining. The random suffix makes
      that impossible rather than merely unlikely.
    """
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%S%f")
    return f"{stamp}-{prefix}-{uuid.uuid4().hex[:6]}"


def ensure_run_dir(run_log_dir: Path | str, repo: str, issue_number: int) -> Path:
    path = run_dir(Path(run_log_dir), repo, issue_number)
    path.mkdir(parents=True, exist_ok=True)
    return path


def redact_argv(argv: Sequence[str]) -> list[str]:
    """A copy of an argv with the (potentially huge) prompt replaced by a marker.

    Used for log lines only. The instruction can embed Issue text, and a log line
    is not the place for untrusted content copied verbatim.
    """
    redacted: list[str] = []
    index = 0
    while index < len(argv):
        part = argv[index]
        if part in {"-p", "--print"} and index + 1 < len(argv):
            redacted += [part, f"<instruction {len(argv[index + 1])} chars>"]
            index += 2
            continue
        redacted.append(part)
        index += 1
    return redacted

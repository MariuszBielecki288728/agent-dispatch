# agent-dispatch

Autonomous, evidence-driven AI agent dispatch and review orchestration service for development VMs.

Poll configured GitHub repositories for Issues labelled `take-it`, implement each one with a coding agent in a dedicated Git worktree, open exactly one PR, and — only after an explicit `agent:fix` handoff — resume the *same* agent session to address review feedback.

Built for a single trusted personal development VM. Deliberately small: **simplicity over production-grade multitenancy.**

---

## Status

| Stage | Issue | State |
|---|---|---|
| Feasibility spike (runtime, GitHub access, VM) | [#1](https://github.com/MariuszBielecki288728/agent-dispatch/issues/1) | ✅ Closed — see [`docs/feasibility.md`](docs/feasibility.md) |
| Architecture and task state contract | [#2](https://github.com/MariuszBielecki288728/agent-dispatch/issues/2) | ✅ Merged — see [`docs/architecture.md`](docs/architecture.md) |
| MVP foundation: service, polling, queue | #3 | ✅ Merged — see [`docs/operations.md`](docs/operations.md) |
| Developer tooling: uv, Ruff, pre-commit | #14 | ✅ Implemented — see [`docs/operations.md` §2](docs/operations.md) |
| MVP execution: worktree, session, PR | [#4](https://github.com/MariuszBielecki288728/agent-dispatch/issues/4) | ✅ Implemented — see [`docs/operations.md` §0–§12](docs/operations.md) |
| Review loop: feedback collection, handoff | [#5](https://github.com/MariuszBielecki288728/agent-dispatch/issues/5) | ✅ Implemented — see [`docs/operations.md` §16](docs/operations.md) |
| Operational release and pilot | #6 | Planned |

**Implemented today: the full one-task loop, Issue → PR → review rounds.**
One installable Python package, one CLI entry point, one polling loop, one SQLite
database, one single-instance lock, and **no third-party dependencies** (stdlib
`tomllib`/`sqlite3`/`fcntl`). A queued `take-it` Issue is implemented by Command
Code in a task-owned Git worktree, validated, committed, pushed, and opened as
exactly one PR owned by that task. Adding the `agent:fix` label to that PR resumes
the **same session** for one consolidated batch of feedback.

```
poll -> queue -> claim (conditional SQL) -> worktree + branch
     -> Command Code run (pinned model/effort/--yolo, bounded turns, wall-clock cap)
     -> validate the stream -> push -> one PR -> awaiting_review

review PR, add `agent:fix` to the PR
     -> claim one round + snapshot its feedback -> clear the label
     -> resume the SAME session in the SAME worktree -> validate -> push to the SAME PR
     -> acknowledge that feedback -> awaiting_review
```

What it will **not** do, on purpose:

- **`subtype=success` is not trusted.** A run whose tool calls were refused still
  reports success with exit 0. The `tool_hook_blocked` event is scanned for and any
  occurrence fails the task — the single most important correctness rule here.
- **It never merges, approves, or closes anything.**
- **It never claims a PR it did not create.** A PR that merely references the Issue
  is recorded as an observation (`obs#N`), never as owned (`own#N`).
- **It never resumes an interrupted run.** Command Code writes a transcript only on
  clean completion, so a retry starts a *fresh* session in the same worktree with
  the existing edits preserved, and says so. For the same reason a review handoff is
  **refused** when there is no cleanly completed session, rather than quietly
  starting a different conversation.

**The review loop is implemented and needs an explicit handoff.** Review comments
alone never start an agent work; the `agent:fix` label on an open PR this task owns
does. See [§16 of the operations guide](docs/operations.md) for the exact workflow,
including how to ask for a second round.

**`status`, `dry-run` and `open` never write and never start an agent.** They read a
read-only snapshot, and `status`/`dry-run` simulate the poll in scratch memory, so
asking what the queue contains cannot change it or spend a model call. `worker` and
`run` are the only commands that do either.

```bash
uv sync --locked                         # one locked environment, no third-party runtime deps
mkdir -p ~/.local/bin
ln -sf "$PWD/.venv/bin/agent-dispatch" ~/.local/bin/agent-dispatch

agent-dispatch doctor      # wrapper, allowlist, labels, state placement, runtime, systemd
agent-dispatch dry-run     # one poll that writes nothing to disk or GitHub
agent-dispatch run         # execute one queued task through to exactly one PR
agent-dispatch open --repo owner/name --issue 12   # branch, worktree, session, PR
```

**uv owns the environment.** The same tool and the same committed `uv.lock`
produce the environment; `uv run` is never used by the service unit, so a restart
cannot sync or download packages.

`uv sync --locked --no-dev` is an **alternative for a dedicated deployment
checkout, not a follow-up step** — it writes the same `.venv` and removes Ruff and
pre-commit, which would break the commit hook if run in a checkout you develop in.
See **[`docs/operations.md`](docs/operations.md)** for the mode table, install,
configuration, the `systemd --user` unit, pause/unpause/retry semantics, log
locations, the migration recipe from the pre-uv virtualenv, and the release's
explicit limitations.

---

## Documentation

- **[`docs/operations.md`](docs/operations.md)** — install, configure, run, day-to-day commands and the explicit limits of the current release.
- **[`docs/architecture.md`](docs/architecture.md)** — the design: component boundaries, worktree layout, task state contract, trigger/review protocol, failure handling, and tradeoffs.
- **[`docs/feasibility.md`](docs/feasibility.md)** — the on-VM evidence spike that this design is built on.
- **[`config/agent-dispatch.example.toml`](config/agent-dispatch.example.toml)** — sample configuration.
- **[`config/config.schema.json`](config/config.schema.json)** — configuration schema (the reviewed source of truth; an identical copy ships inside the package so an installed CLI validates without the checkout, and the test-suite asserts the two never diverge).

---

## Development

One locked environment provides Ruff and pre-commit; there is no second,
independently versioned tool copy to drift out of sync.

```bash
uv sync --locked                          # create/update the environment
uv run --no-sync ruff check .             # lint
uv run --no-sync ruff format --check .    # verify formatting
uv run --no-sync ruff format .            # apply fixes locally
uv run --no-sync pre-commit install       # install the git hook
uv run --no-sync pre-commit run --all-files

PYTHON=.venv/bin/python ./scripts/test-offline.sh        # deterministic suite
PYTHON=.venv/bin/python ./scripts/smoke-runtime.sh --mock
```

`--no-sync` runs the already-locked environment and never resolves or downloads
anything; a missing environment fails loudly rather than falling back to a system
Ruff/Python. The pre-commit hooks use `language: system` and call the locked Ruff
through `uv run --no-sync`, so they cannot disagree with CI. No hook runs a model,
spends credits, or touches GitHub.

CI runs the same commands after `uv sync --locked`, so a dependency change without
a refreshed `uv.lock` is rejected rather than silently resolving something
different. The offline suite drives the real `GitHubClient`/`Discovery`/`Worker`
code against tests/fake_wrapper.py, an executable that speaks the same CLI surface
as the approved wrapper. It covers discovery and pagination, Issue-vs-PR filtering,
allowlist enforcement, missing auth/wrapper/label handling, repeated polls,
restarts with previously labelled Issues, `take-it` removal and re-add, a
pre-existing PR for the same Issue, failure/retry paths that must never mark a
task complete, a truncated PR listing failing closed, `enqueue` sharing the poll's
exact rules, single-instance lock contention, the global concurrency limit of
one, `dry-run` writing nothing, and state/log placement outside every repository.

---

## Trigger protocol at a glance

**`take-it`** on an Issue expresses dispatch intent. It stays on the Issue for the whole active lifetime and is not a mirror of local status.

Removing it suspends dispatch but keeps the task row, branch and worktree; re-adding it makes the **same** row dispatchable again. An explicit maintainer `pause` is separate and is never released automatically.

**`agent:fix`** on a PR is an explicit handoff. It starts **exactly one** consolidated
follow-up round that resumes the task's existing session on the existing branch and
pushes to the same PR. The label must be on an **open PR this task owns**.

> PR review comments alone **never** start an agent. Reviewer and implementer may
> share the same GitHub account, so role is never inferred from login and the
> agent's own replies never re-trigger work.

> **One label, one round.** The round and its feedback snapshot are recorded before
the label is removed, so a crash cannot lose the handoff. A label that is simply **left
in place is not a standing request for more rounds** — remove it and add it again for
another round. Without that rule every poll would start a round against feedback that
was already applied.

`take-it` and `agent:fix` do not appear automatically: create them deliberately,
and only in an allowlisted repository, with
`agent-dispatch setup-labels --repo owner/name --yes`. Until then the trigger
protocol is inert by construction — a safe default, not a bug.

---

## Runtime

**Command Code CLI is the initial implementation, not a permanent provider lock-in.** It is the default because the maintainer currently subscribes to it. Switching runtime later is configuration plus one small adapter; selecting a different supported model *within* Command Code is normally just a config change. Existing tasks stay pinned to the runtime, model, and session recorded at start, and a config change never silently migrates a running or resumable session.

The current release does **not** invoke the runtime. The pinned
`runtime.driver`/`model`/`effort`/`permission_mode` are validated and stored on each
task row at discovery time, so that #4 inherits an explicit, auditable contract
rather than an implied one. An unsupported driver is a startup error.

---

## Headless permissions — read before configuring

Unattended file mutation in Command Code's non-interactive `-p` mode requires its broad allow-all flag. Verified on this VM (v1.64.0):

| Flags | Headless writes |
|---|---|
| `--yolo` | **allowed** ✅ |
| `--permission-mode yolo` | blocked ❌ |
| `--tools-all` | blocked ❌ |
| `--session <id>` resumed without `--yolo` | blocked ❌ |

The permission grant does **not** persist across `--session` resume, so the flag is re-passed on every invocation.

A blocked run still reports `subtype: "success"` with exit code 0 and no error flag, so the orchestrator must scan the NDJSON stream for `tool_hook_blocked` events and treat any occurrence as a hard failure. See [`docs/architecture.md` §2](docs/architecture.md).

**Interrupted runs cannot be resumed.** The session ID is emitted on the stream's first line, but the transcript is only written on clean completion — a run killed by timeout, `SIGKILL`, or even `SIGINT` leaves no transcript, and `--session <id>` then fails with *"neither an existing .jsonl transcript nor a known session-id prefix"*. Bounded retries therefore start a **fresh** session while preserving any uncommitted edits.

**Raw run logs never live inside a worktree.** They are written to the private state directory, because a log created in the worktree could be swept into a commit by `git add -A` and would dirty the tree the ownership check relies on.

---

## Security posture — stated plainly

- **`--yolo` is a trust choice, not isolation.** It grants the agent unrestricted shell and file access as your user. The maintainer has accepted this on this trusted personal VM. Do not describe it as a sandbox.
- **A Git worktree is Git isolation only.** It is not a filesystem, process, network, or credential boundary. An agent with unrestricted shell access can reach files outside its worktree and the same user's credentials. These are consciously accepted risks, not guarantees the orchestrator can prevent.
- **All GitHub access goes through a configurable wrapper command** (on this VM: `gh-craftlypse`), which holds a repository-scoped credential. Never substitute raw `gh`, run `gh auth login`, or export `GH_TOKEN` on this VM.
- **Credential-helper ordering is explicit, because `credential.helper` is multi-valued.** A bare `-c credential.https://github.com.helper=!<cmd>` *appends* to the inherited list; an earlier ambient helper is then queried first and stops the chain once it returns credentials. The correct form **resets the list, then sets the wrapper**:

  ```bash
  git -c credential.https://github.com.helper= \
      -c credential.https://github.com.helper="!<wrapper> auth git-credential" <command>
  ```

  A per-invocation `-c` covers only that process, so managed worktrees also get this reset-then-wrapper sequence written into their **repo-local** config, which agent-issued Git inherits. On this VM the inherited order is the unapproved raw `gh` helper first and the wrapper second; the raw helper currently returns nothing, so the chain happens to fall through — that is incidental, not guaranteed.
- **Operational guardrails:** repository allowlist only, no automatic merge, no Issue closure, no reviewer approval, no touching unrelated repositories, one task at a time, bounded run timeout and retries, no token or raw-log dumping.
- **State and logs live outside every target repository**, enforced by configuration validation rather than convention: a log written inside a worktree could be swept into a commit by `git add -A`. `test-offline.sh` asserts the checkout is left untouched.
- **Failed GitHub access is reported honestly.** An inaccessible repository, a missing wrapper, an auth failure, a rate limit or a network error is surfaced with its cause; it can never mark an Issue as completed, and the service never retries with another identity or model.

---

## Development VM

Developed on and for the same Ubuntu 24.04 VM used for other work. VS Code connects over Remote SSH, and the worker runs as a `systemd --user` service — **it must keep working with no VS Code window open.**

Headless CLI sessions do not appear as native VS Code Chat sessions. Inspecting or resuming a task session is done by opening the task worktree in VS Code and using the integrated terminal.

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
| MVP foundation: service, polling, queue | #3 | ✅ Implemented — see [`docs/operations.md`](docs/operations.md) |
| MVP execution: worktree, session, PR | #4 | Planned |
| Review loop: feedback collection, handoff | #5 | Planned |
| Operational release and pilot | #6 | Planned |

**Implemented today: the service queues work; it does not execute it.**
There is one installable Python package with one CLI entry point, one polling
loop, one SQLite database, one single-instance lock, and **no third-party
dependencies** (stdlib `tomllib`/`sqlite3`/`fcntl`). It discovers Issues labelled
`take-it` in allowlisted repositories through the configured `gh-craftlypse`
wrapper and maintains an idempotent durable queue.

**Nothing here starts an agent.** No `commandcode` invocation exists in the code
base yet, and no task is ever promoted to `running`; discovery is not
implementation. Branch/worktree creation, sessions, pushes and PRs are #4, and
the `agent:fix` review loop is #5. Those are documented as future work rather
than stubbed.

```bash
python3 -m venv ~/.local/share/agent-dispatch/venv
~/.local/share/agent-dispatch/venv/bin/pip install .
agent-dispatch doctor      # wrapper, allowlist, labels, state placement, systemd
agent-dispatch dry-run     # one poll that writes nothing to disk or GitHub
```

See **[`docs/operations.md`](docs/operations.md)** for install, configuration,
the `systemd --user` unit, pause/unpause/retry semantics, log locations and the
release's explicit limitations.

---

## Documentation

- **[`docs/operations.md`](docs/operations.md)** — install, configure, run, day-to-day commands and the explicit limits of the current release.
- **[`docs/architecture.md`](docs/architecture.md)** — the design: component boundaries, worktree layout, task state contract, trigger/review protocol, failure handling, and tradeoffs.
- **[`docs/feasibility.md`](docs/feasibility.md)** — the on-VM evidence spike that this design is built on.
- **[`config/agent-dispatch.example.toml`](config/agent-dispatch.example.toml)** — sample configuration.
- **[`config/config.schema.json`](config/config.schema.json)** — configuration schema (the reviewed source of truth; an identical copy ships inside the package so an installed CLI validates without the checkout, and the test-suite asserts the two never diverge).

---

## Development

```bash
./scripts/test-offline.sh      # deterministic suite: no network, no model credits, no GitHub mutation
./scripts/smoke-runtime.sh --mock
```

The offline suite drives the real `GitHubClient`/`Discovery`/`Worker` code against
tests/fake_wrapper.py`, an executable that speaks the same CLI surface as the
approved wrapper. It covers discovery and pagination, Issue-vs-PR filtering,
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

**`agent:fix`** on a PR is an explicit handoff. It queues **exactly one** consolidated follow-up round that resumes the task's existing session on the existing branch. This is #5 scope and is **not** implemented yet.

> PR review comments alone **never** start an agent. Reviewer and implementer may share the same GitHub account, so role is never inferred from login and the agent's own replies never re-trigger work.

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

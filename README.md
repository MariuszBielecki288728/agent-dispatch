# agent-dispatch

Autonomous, evidence-driven AI agent dispatch and review orchestration service for development VMs.

Poll configured GitHub repositories for Issues labelled `take-it`, implement each one with a coding agent in a dedicated Git worktree, open exactly one PR, and — only after an explicit `agent:fix` handoff — resume the *same* agent session to address review feedback.

Built for a single trusted personal development VM. Deliberately small: **simplicity over production-grade multitenancy.**

---

## Status

| Stage | Issue | State |
|---|---|---|
| Feasibility spike (runtime, GitHub access, VM) | [#1](https://github.com/MariuszBielecki288728/agent-dispatch/issues/1) | ✅ Closed — see [`docs/feasibility.md`](docs/feasibility.md) |
| Architecture and task state contract | [#2](https://github.com/MariuszBielecki288728/agent-dispatch/issues/2) | 📐 Design — see [`docs/architecture.md`](docs/architecture.md) |
| MVP foundation: service, polling, queue | #3 | Planned |
| MVP execution: worktree, session, PR | #4 | Planned |
| Review loop: feedback collection, handoff | #5 | Planned |
| Operational release and pilot | #6 | Planned |

**No service code exists yet.** Issue #2 is design-only by explicit maintainer scope.

---

## Documentation

- **[`docs/architecture.md`](docs/architecture.md)** — the design: component boundaries, worktree layout, task state contract, trigger/review protocol, failure handling, and tradeoffs.
- **[`docs/feasibility.md`](docs/feasibility.md)** — the on-VM evidence spike that this design is built on.
- **[`config/agent-dispatch.example.toml`](config/agent-dispatch.example.toml)** — sample configuration.
- **[`config/config.schema.json`](config/config.schema.json)** — configuration schema.

---

## Trigger protocol at a glance

**`take-it`** on an Issue expresses dispatch intent. It stays on the Issue for the whole active lifetime and is not a mirror of local status.

**`agent:fix`** on a PR is an explicit handoff. It queues **exactly one** consolidated follow-up round that resumes the task's existing session on the existing branch.

> PR review comments alone **never** start an agent. Reviewer and implementer may share the same GitHub account, so role is never inferred from login and the agent's own replies never re-trigger work.

---

## Runtime

**Command Code CLI is the initial implementation, not a permanent provider lock-in.** It is the default because the maintainer currently subscribes to it. Switching runtime later is configuration plus one small adapter; selecting a different supported model *within* Command Code is normally just a config change. Existing tasks stay pinned to the runtime, model, and session recorded at start, and a config change never silently migrates a running or resumable session.

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

---

## Security posture — stated plainly

- **`--yolo` is a trust choice, not isolation.** It grants the agent unrestricted shell and file access as your user. The maintainer has accepted this on this trusted personal VM. Do not describe it as a sandbox.
- **A Git worktree is Git isolation only.** It is not a filesystem, process, network, or credential boundary. An agent with unrestricted shell access can reach files outside its worktree and the same user's credentials. These are consciously accepted risks, not guarantees the orchestrator can prevent.
- **All GitHub access goes through a configurable wrapper command** (on this VM: `gh-craftlypse`), which holds a repository-scoped credential. Never substitute raw `gh`, run `gh auth login`, or export `GH_TOKEN` on this VM. Worktrees pass an explicit Git credential helper per invocation so an ambient, unapproved helper is never picked up.
- **Operational guardrails:** repository allowlist only, no automatic merge, no Issue closure, no reviewer approval, no touching unrelated repositories, one task at a time, bounded run timeout and retries, no token or raw-log dumping.

---

## Development VM

Developed on and for the same Ubuntu 24.04 VM used for other work. VS Code connects over Remote SSH, and the worker runs as a `systemd --user` service — **it must keep working with no VS Code window open.**

Headless CLI sessions do not appear as native VS Code Chat sessions. Inspecting or resuming a task session is done by opening the task worktree in VS Code and using the integrated terminal.

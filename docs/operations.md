# agent-dispatch — operations (Issues #3–#4, #17)

Operator guide for the MVP loop: an installable CLI, one polling worker, one
SQLite queue, and **one Command Code run per task in a task-owned Git worktree,
ending in exactly one pull request**. The `agent:fix` review loop is Issue #5 and
is not implemented here (§11).

---

## 0. What actually happens to a `take-it` Issue

```
poll finds a labelled Issue            -> task row, phase `queued`
claim (conditional SQL transition)     -> phase `running`, branch + worktree recorded
                                       -> ONE status comment on the Issue (§13)
Command Code runs in the worktree      -> session ID pinned to the row
                                       -> comment edited in place every 5 min
run validated                          -> blocked tools / bad session / timeout all FAIL
changes committed if the agent left them uncommitted
branch pushed with the approved credential helper
one PR created (or an existing one adopted)  -> phase `awaiting_review`
                                       -> same comment becomes `Awaiting review`
```

Three things a reader should not assume:

* **`--yolo` is a trust choice, not a sandbox.** The agent runs as your user with
  broad file and shell access. A worktree is Git isolation only — see §12.
* **A `subtype=success` exit is not proof of work.** A run whose tool calls were
  refused still reports success with exit 0. `agent-dispatch` scans for the
  `tool_hook_blocked` event and fails the run (§9).
* **An interrupted run is not resumable.** Command Code writes a session
  transcript only on a clean completion, so a retry starts a *fresh* session in the
  same worktree with the existing edits preserved (§7).

---

## 1. Install

The service has **no third-party dependencies** (stdlib `tomllib`, `sqlite3`,
`fcntl`). The project is managed with **uv**: the same tool installs both the
development environment and the deployable runtime, so there is no second
toolchain and no ad-hoc `pip install`.

Two things are pinned, and they are not the same thing:

| What | Pinned by | Guarantee |
|---|---|---|
| Package/tool versions (Ruff, pre-commit) | `uv.lock` | exact resolved versions; `--locked` refuses any drift |
| Python minor version | `.python-version` (`3.12`) | uv selects 3.12 locally; CI asserts the resolved version |

`uv.lock` pins **packages** resolved against `requires-python = ">=3.11"` — it does
**not** by itself pin the Python minor version. `.python-version` does that, and uv
honours it natively (no CI-specific plumbing needed). CI runs an explicit check
that the resolved interpreter matches the pin, so a drift fails the build instead
of going unnoticed.

On this VM uv reuses the system 3.12.3 rather than downloading an interpreter, so
the pin costs nothing.

### Which sync mode to use

```bash
uv sync --locked           # developer environment (includes Ruff/pre-commit)
uv sync --locked --no-dev  # runtime-only environment (deployment)
```

`--locked` refuses to resolve anything not already recorded in the committed
`uv.lock`, so a deployment can never silently pick up a different Ruff or
library version. The project is installed into `.venv` as an editable install,
and `.venv/bin/agent-dispatch` is the CLI.

> **These are alternative modes, not sequential setup steps.** Both write the
> **same `.venv`**. Running `--no-dev` after a developer sync *removes* Ruff and
> pre-commit from that environment, so an already-installed commit hook stops
> working and `uv run --no-sync ruff …` fails with `Failed to spawn: ruff`.
> Pick one mode per checkout — never both in the same one.

| Checkout | Sync mode | Hooks |
|---|---|---|
| **Shared development/deployment checkout** (the default here) | `uv sync --locked` | install and keep them |
| **Dedicated deployment checkout** | `uv sync --locked --no-dev` | do not install or expect them |

For the shared checkout, a bare `uv sync --locked` is the correct choice: the dev
group costs one environment and keeps the hook commands usable, while the service
still needs no `uv` on its execution path because systemd invokes the installed
executable directly.

### Declared testing unit

```bash
systemctl --user restart agent-dispatch.service
./scripts/install-service.sh --status
```

### A checkout dedicated to deployment

Use this only when the source checkout is **not** used for development. Install
the runtime environment without the dev group:

```bash
uv sync --locked --no-dev
# -> .venv/bin/agent-dispatch, and no Ruff/pre-commit present
```

Do **not** install the commit hook in that checkout — the hook invokes the locked
Ruff through `uv run --no-sync`, which `--no-dev` deliberately removes. Keep it a
pure runtime environment.

`./scripts/install-service.sh --install` resolves the executable on `PATH`. Link
it once and the unit needs no further edits:

```bash
mkdir -p ~/.local/bin
ln -sf ~/code/agent-dispatch/.venv/bin/agent-dispatch ~/.local/bin/agent-dispatch
```

### Migrating from the pre-uv virtualenv (Issue #3 layout)

The old layout installed into `~/.local/share/agent-dispatch/venv` with `pip`.
Upgrading does **not** require touching configuration, the SQLite queue or logs —
all of those live outside the environment.

**First, update the checkout with the approved wrapper-backed Git procedure.**
A bare `git pull` is not sufficient on this VM: the ambient credential-helper
chain queries the **unapproved** raw `gh` helper before `gh-craftlypse`, and that
ordering is incidental rather than guaranteed (see
[`docs/architecture.md` §2.4](architecture.md)). Reset the inherited helper list,
then set the configured wrapper — the wrapper's own `github.command` and
`github.credential_helper_reset`/`credential_helper` values are the source of
truth:

```bash
cd ~/code/agent-dispatch
set +H   # '!' would otherwise trigger bash history expansion
git -c credential.https://github.com.helper= \
    -c credential.https://github.com.helper='!/home/craftlypse/.local/bin/gh-craftlypse auth git-credential' \
    pull --ff-only
```

Then rebuild the environment and restart the service:

```bash
uv sync --locked                                # keep Ruff/pre-commit usable here
mkdir -p ~/.local/bin
ln -sf "$PWD/.venv/bin/agent-dispatch" ~/.local/bin/agent-dispatch
systemctl --user restart agent-dispatch.service # unit keeps using the same ~/.local/bin path
```

Confirm before removing anything:

```bash
~/.local/bin/agent-dispatch doctor
~/.local/bin/agent-dispatch status --no-sync
uv run --no-sync ruff --version                 # proves the dev tooling still works
```

Only after that has been verified, the superseded environment may be deleted:

```bash
rm -rf ~/.local/share/agent-dispatch/venv
```

`worker.state_db`, `worker.run_log_dir` and `worker.lock_file` are unchanged by
this migration, so the existing task rows and log history are preserved. If the
service is not installed as a unit, run it from the checkout instead — there is
deliberately **no timer**, because the worker polls forever on its own interval.

`uv run` is **not** used in the service unit: `uv run` can sync/download packages
when the environment does not match the lock, and a service restart must not
silently mutate the deployment.

---

## 2. Development tooling (Ruff + pre-commit)

Ruff and pre-commit come from the locked dev group; there is no second,
independently versioned copy of either.

```bash
uv sync --locked                        # required before the commands below
uv run --no-sync ruff check .           # lint
uv run --no-sync ruff format --check .  # verify formatting
uv run --no-sync ruff format .          # apply formatting locally
```

`--no-sync` is deliberate: it runs the already-locked environment and never
silently resolves or downloads anything. If the environment is missing the
command fails loudly instead of falling back to a system Ruff/Python.

Formatting is delegated to `ruff format` (line length 100); E501 is not enforced
separately because the formatter cannot wrap long string/URL literals, and manual
splits there would be noise rather than clarity.

### Commit hooks

```bash
uv sync --locked                          # prerequisite
uv run --no-sync pre-commit install       # install the git hook
uv run --no-sync pre-commit run --all-files
```

The hooks are `language: system` and call the locked Ruff through
`uv run --no-sync`, so pre-commit downloads **no** tool environment of its own and
the commit hook cannot disagree with CI. The hooks are:

| Hook | Purpose |
|---|---|
| `ruff-check` | lint staged Python files |
| `ruff-format-check` | verify formatting; never rewrites your staged tree |
| `no-merge-conflict-markers` | block an unresolved conflict marker |

No hook runs a model, spends credits, or touches GitHub — a commit must stay
local, offline and fast.

---

## 3. Configure

```bash
mkdir -p ~/.config/agent-dispatch
cp config/agent-dispatch.example.toml ~/.config/agent-dispatch/config.toml
$EDITOR ~/.config/agent-dispatch/config.toml
```

Config is loaded from `~/.config/agent-dispatch/config.toml` (then
`~/agent-dispatch/config.toml`), or from `--config PATH`. It is validated against
the reviewed `config/config.schema.json`; an unknown key, an unlisted repository
or an unsupported driver is a **startup error**, never a silent fallback.

Two placements are enforced, not merely recommended, because violating them would
corrupt future runs:

| Setting | Must live | Why |
|---|---|---|
| `worker.state_db`, `worker.run_log_dir`, `worker.lock_file` | outside every target repository | a log written inside a worktree can be swept into a commit by `git add -A`, and would dirty the tree the ownership check relies on |
| `worker.worktree_root` | outside every target repository | the maintainer's normal checkout is never touched |

### Required `gh-craftlypse` access

All GitHub access goes through the wrapper named by `github.command`. On this VM
that is `/home/craftlypse/.local/bin/gh-craftlypse`, and its credential is limited
to selected repositories.

```bash
agent-dispatch doctor
```

`doctor` verifies the wrapper exists and is executable, that it authenticates, and
that **each allowlisted repository is actually readable with that credential**.
Never run `gh auth login`, never substitute raw `gh`, and never export `GH_TOKEN`
on this VM. A repository outside the credential's permitted set fails fast with an
explicit message — the service will **not** retry with another identity.

`doctor` cannot verify token *scopes*; they are not observable through the
wrapper. That limitation is reported as such instead of being claimed.

---

## 4. Create the labels intentionally

`take-it` and `agent:fix` did not exist in this repository. The service never
creates labels as a side effect of polling; creation is an explicit,
maintainer-invoked, idempotent action:

```bash
agent-dispatch setup-labels --repo MariuszBielecki288728/agent-dispatch --yes
```

Without `--yes` the command only prints what it would do. Re-running it is safe:
existing labels are reported as `already present`. Only repositories in the
allowlist are accepted, and nothing outside them is ever modified.

`doctor` reports missing labels with this exact command as guidance.

---

## 5. Run the worker

### Foreground (no systemd required)

```bash
agent-dispatch worker                      # polls every worker.poll_interval_seconds
agent-dispatch worker --interval 60        # override the interval
agent-dispatch worker --once               # a single poll, then exit
agent-dispatch worker --once --no-execute  # diagnose the queue without running an agent
```

**`worker` executes an eligible task.** That is the point of this release: the
poll both discovers work and dispatches it. Two ways to keep it queue-only:

* `--no-execute` — discover, queue and report, but start no agent;
* `dry-run` / `status` — read-only, and they never execute at all.

A missing `commandcode` binary is reported once as `dispatch_unavailable` and
**touches no task state**; it is a configuration fault, not a task failure, so a
queued Issue is not consumed by it.

### One task at a time, globally

The MVP allows **one active task across all repositories**. Two mechanisms enforce
it, and only both together are sufficient:

1. the single-instance `flock` — a second worker (or `agent-dispatch run`) exits 3
   (`EXIT_BUSY`) rather than starting a competing agent;
2. the claim itself — a conditional SQL `UPDATE ... WHERE phase = 'queued'`, so
   even within one process only one task can move to `running`.

### `run` — execute one task now

```bash
agent-dispatch run                                   # poll, then execute the oldest eligible task
agent-dispatch run --skip-poll                       # use the existing queue state
agent-dispatch run --repo owner/name --issue 12      # target one task (still re-validated)
```

`run` takes the same lock as the worker, polls first unless `--skip-poll` is given,
repairs crash-interrupted state, and then dispatches **at most one** task.
`--repo/--issue` selects a task; it does not bypass eligibility — a paused, failed,
closed or already-owned task is refused with its real reason.

Exit codes: `0` success or a legitimate refusal with a reason, `1` a failed run,
`2` usage error, `3` the lock is held by another process.

### `open` — inspect a task by hand

```bash
agent-dispatch open --repo owner/name --issue 12
```

Prints the phase, owned branch/worktree, owned PR vs. observed PR, pinned
runtime, attempt count, session ID, whether that session is resumable, and each
recorded run with its outcome and log path. It is read-only (it opens the state
database without write access), so it can never create or migrate it.

To inspect or resume a session interactively, copy the two lines it prints:

```bash
cd <worktree_path>
commandcode --session <session_id>
```

Do this in the integrated terminal of a VS Code Remote SSH window if you want the
session in a GUI; no native VS Code Chat handoff is claimed, and the CLI session
is the supported interface. **Do not resume a session that `open` reports as not
resumable** — an interrupted run has no transcript and the resume will fail with
`neither an existing .jsonl transcript nor a known session-id prefix`.

### systemd --user (recommended; no VS Code window needed)

```bash
./scripts/install-service.sh --install
./scripts/install-service.sh --status
./scripts/install-service.sh --uninstall
```

The installer renders the unit with the real binary and config paths, enables and
starts it, and **checks `Linger`** rather than assuming it:

```bash
loginctl show-user "$USER" -p Linger
# Linger=no  ->  sudo loginctl enable-linger $USER
```

There is deliberately **no timer**. The worker polls forever on its own interval;
adding a systemd timer would introduce a second, competing scheduling mechanism.

### Stop

```bash
systemctl --user stop agent-dispatch.service      # systemd
# or Ctrl-C / SIGTERM in the foreground
```

`SIGTERM` and `SIGINT` are handled: the current poll finishes, the lock is
released, and the process exits 0.

---

## 6. Day-to-day commands

```bash
agent-dispatch status                 # poll, then show every task and why it is/isn't dispatchable
agent-dispatch status --no-sync       # local state only (no API calls)
agent-dispatch status --json          # machine-readable
agent-dispatch dry-run                # one poll that writes NOTHING to disk or GitHub
agent-dispatch run                    # execute one queued task through to a PR
agent-dispatch run --repo owner/name --issue 12
agent-dispatch open --repo owner/name --issue 12
agent-dispatch enqueue --repo owner/name --issue 12
agent-dispatch pause   --repo owner/name --issue 12
agent-dispatch unpause --repo owner/name --issue 12
agent-dispatch retry   --repo owner/name --issue 12
agent-dispatch resume-publish --repo owner/name --issue 12
agent-dispatch prune-logs [--dry-run]
```

**`dry-run` is the safe way to see what the worker would do.** It mirrors the real
state database into scratch memory, performs the same GitHub reads and the same
decisions, prints the result, and persists nothing — it will not even create
`state.db`. It never starts an agent.

`dry-run --no-sync` keeps the "persist nothing" guarantee and **makes no API calls
at all**: it reports local state only and skips the poll. Use it when GitHub is
unreachable or you deliberately want to avoid touching the API.

**`status` is observability, and never writes.** It does not create or migrate the
state database, and it does not become a second, unsynchronised writer competing
with the worker that owns the single-instance lock (which `status` does not take).
Neither `status` nor `dry-run` can start an agent: they poll against a **scratch
copy** of the database, and the only object that can execute a task is the
orchestrator holding a writable store.

| Command | Reads | Makes API calls | Persists | Runs an agent |
|---|---|---|---|---|
| `status --no-sync` | on-disk state directly | no | nothing | no |
| `status` | on-disk state, then a poll against a scratch copy | yes (read-only) | nothing | no |
| `dry-run --no-sync` | on-disk state | no | nothing | no |
| `dry-run` | on-disk state, then a poll against a scratch copy | yes (read-only) | nothing | no |
| `open` | on-disk state (read-only connection) | no | nothing | no |
| `worker --once --no-execute` | — | yes | **yes** | no |
| `enqueue` / `pause` / `unpause` / `retry` | — | yes (`enqueue` only) | **yes** | no |
| `resume-publish` | — | yes | **yes** | no |
| `worker` | — | yes | **yes** | **yes** |
| `run` | — | yes | **yes** | **yes** |

So `status` and `dry-run` show what the queue *would* look like; the on-disk state
they report only advances when the worker (or an explicit `enqueue`/`pause`/
`unpause`/`retry`) runs. If a sync is incomplete, `status` says so and falls back to
showing on-disk state rather than presenting a partial poll as authoritative.

**`enqueue` is not a shortcut around the rules, and it never executes.** It
addresses one Issue by number and applies exactly the same decision the poll
applies, so it refuses a PR number, a closed Issue, an unlabelled Issue, or an Issue
that already has a PR. Re-running it on an already-queued Issue is a success that
reuses the existing row. It is the safe way to put a task in the queue ahead of the
next poll, or to test eligibility without spending a model call.

### Pause semantics (important)

There are two distinct reasons a task can be `paused`, and they behave differently
on purpose:

| Reason | Set by | Released by |
|---|---|---|
| `maintainer` | `agent-dispatch pause` | `agent-dispatch unpause` (sticky across polls and restarts) |
| `label_withdrawn` | removing `take-it` from the Issue | re-adding `take-it`, automatically on the next poll |

So removing `take-it` suspends dispatch but keeps the row, branch and worktree;
restoring the label makes the **same** row dispatchable again. An explicit
maintainer `pause` is never released automatically.

`retry` applies only to `failed`/`needs_attention` tasks; on a `queued` task it is
refused with a clear message rather than silently doing nothing.

`resume-publish` is the publish-pending counterpart of `retry`: for a task whose
model run finished but whose push/PR did not, and which was then paused. It finishes
that publication — never starting an agent — and refuses a task with nothing pending.
A live worker holds the single-instance lock for the whole time it runs, so when one
is up the command re-arms the task and hands off, reporting that the worker will
publish it on its next poll. That hand-off is safe because the worker finishes
publish-pending work on every poll; it does not depend on a restart.

### Publication is finalised in one statement

Recording the owned PR, clearing `recovery_stage` and entering `awaiting_review` are
written by **one** SQL statement (`Store.finalise_publication`), on both the create
and adopt paths. These three facts are only true together, and the connection is in
autocommit mode, so writing them separately left two crash windows:

* crash after the PR was recorded but before the stage was cleared — the reconcile
  pass returns early when a PR is owned, so the stale stage was never cleared, and the
  status renderer reads the stage *before* the PR: the comment would say
  "Recovering publication" indefinitely for a task whose PR already existed;
* crash after the stage was cleared but before the phase changed — an owned PR beside
  `needs_attention`, which is not a real `awaiting_review` for anything that gates on
  the phase.

Rows left inconsistent by an older build are **repaired on reconciliation**: a task
with an owned PR *and* a publishable stage is the exact old crash signature, so the
whole publication result is normalised in one statement — the owned PR is kept, the
stale stage is cleared and the phase becomes `awaiting_review`. Clearing only the stage
would leave the *other* inconsistent state (an owned PR beside `needs_attention`),
which is still wrong for anything gating on a real `awaiting_review`.

The repair is deliberately narrow: it applies only to that combination. A
`needs_attention` row with an owned PR but **no** publishable stage is left alone,
because that can be a legitimate later intervention rather than this crash signature.

**Its exit status describes the outcome, not the attempt.** The command exits `0`
only when the work is actually published (an owned PR exists) or when a live worker
holds the lock and has explicitly accepted responsibility for the next poll. If the
commit, push or PR fails *again*, the task stays publish-pending and the command exits
`1` with `publish_incomplete` and the remaining `recovery_stage` — a script that
treats `0` as "published" would otherwise act on a lie. The durable task state is
unchanged and still recoverable either way; only the report differs.

---

## 7. What `status` means

Phases: `queued`, `running`, `awaiting_review`, `paused`, `failed`,
`needs_attention`, `finished`. (`feedback_queued` arrives with #5.)

A task is **dispatchable** only when the Issue is open, it still carries `take-it`,
no relevant PR already exists for it, this worker does not already own a PR for it,
and its phase is `queued`. Otherwise `status` prints the specific blocker.

That check is a cheap **local** pre-check for reporting. It is deliberately not what
authorizes a run: ranking a task and starting it are different decisions, and a
`status` snapshot can be a whole poll interval stale. Immediately before claiming,
the orchestrator re-reads the Issue and the PR list from GitHub, then performs the
claim as a conditional SQL transition. A label that was withdrawn in between loses
the race rather than starting an agent.

### Observed PR vs. owned PR

Two different facts are recorded separately, and conflating them would let a review
round act on someone else's pull request:

| Column | Meaning | Written by |
|---|---|---|
| `observed_state.linked_pr_number` | a PR that *already exists* for this Issue — a human's PR, or an earlier one | discovery, every poll |
| `tasks.pr_number` | the PR **this worker's own run created** (or adopted on its own branch) | only the #4 publish workflow |

Discovery **never** writes `tasks.pr_number`, not even when the PR sits on a
`dispatch/issue-N-...` branch — a branch name is a strong hint, not proof of
ownership. `status` therefore prints `own#N` for an owned PR and `obs#N` for an
observed one, so the distinction is visible at a glance. An Issue with an observed
PR is recorded as `awaiting_review` and stays non-dispatchable.

### Ownership of branches and worktrees

`tasks.branch` and `tasks.worktree_path` are the owned artifacts. They are recorded
before a run starts and reused afterwards, so a restart finds the *existing* branch
instead of deriving a new name. Layout:

```
<worktree_root>/<owner>__<name>/issue-<N>/     branch: dispatch/issue-<N>-<kebab-title>
<run_log_dir>/<owner>__<name>/issue-<N>/<run-id>.ndjson
```

The worktree's **checked-out branch is verified against the recorded one** on every
reuse, not merely its path and registration. A registered worktree that has been
switched to another branch still looks owned while its commits and working tree
belong elsewhere, so a mismatch is refused and escalated to `needs_attention`
rather than silently committed or published.

A directory that exists at the expected path but is **not** a registered Git
worktree of the configured source is never adopted and never deleted: the task moves
to `needs_attention` and says so. "Dirty" is not used as a proxy for anything — a
clean worktree may contain successful commits, and a dirty one is expected after an
interrupted run.

### New worktrees require a fresh base

A **new** task worktree fails clearly if `origin/<base_branch>` cannot be fetched,
because branching from a stale local base would build the task on old code with no
indication anything was wrong. For an **already-owned** worktree the base only
affects the diff comparison, so a transient fetch failure is reported as a warning
rather than discarding existing work.

---

## 8. Logs and state

| What | Where |
|---|---|
| State database | `worker.state_db` (default `~/.local/state/agent-dispatch/state.db`) |
| Raw NDJSON run logs | `worker.run_log_dir`, retained to `worker.run_log_keep` files per task |
| Runtime stderr per run | `<run-id>.stderr.txt` beside the NDJSON file |
| Worker lock | `worker.lock_file` (pid + start time + command; never credentials) |
| Service logs | `journalctl --user -u agent-dispatch -f` |

`--log-format json` emits one JSON object per line for journald/CI capture.

Run logs live **outside** every repository and worktree by design — a log written
inside the worktree could be swept into a commit by `git add -A` and would leak a
raw agent transcript into the PR. The offline suite asserts this placement after a
real run. Log *contents* are never printed to CI, pasted into GitHub, or echoed by
`status`/`open`; those report paths only.

Stderr goes to a **sidecar file**, not into the NDJSON stream: that stream is parsed
as data, and mixing free-form stderr into it would make a failed run look like a
malformed one.

---

## 9. How a run is judged (and what fails it)

A run is accepted **only if every one** of these holds:

| Check | Why it exists |
|---|---|
| The process spawned and exited `0` | a missing binary refuses before the task is claimed; exit `8` is the turn cap and is a bounded failure |
| A `result` line with `subtype=success` exists | a truncated or crashed stream must not read as success |
| A non-empty `session_id` was captured | without it the task cannot be pinned or later resumed |
| On a resume, the returned ID **equals** the pinned one | otherwise the task's conversation is not the one that ran |
| **Zero `tool_hook_blocked` events** | the false-success guard — see below |
| The run finished inside the wall-clock timeout | the watchdog kills the process group at the deadline |
| The worktree is still on the branch this task owns | a switched worktree looks owned while holding someone else's work |
| Any uncommitted changes **can be committed** | `git push` cannot carry them, so a failed commit must stop before publishing rather than open a PR that omits the run's work |

Produced work is evaluated separately, and deliberately does **not** require a
dirty tree: commits count, uncommitted edits count, and a successful run that
changed nothing is recorded as such and escalated to `needs_attention` rather than
opening an empty PR.

The session ID is persisted **the moment the stream reports it**, via a callback
while the run is still in flight — not only after the process exits. Without that, a
hard crash would leave no record of which session was attempted, which is exactly
the case where that answer is needed. It does **not** make the run resumable: an
interrupted first run still has no transcript.

### The silent false-success hazard

A run whose tool calls were **all refused** still reports:

```json
{"type":"result","subtype":"success","stopReason":"end_turn","sessionId":"…"}
```

with exit code 0 and no `isError` anywhere in the stream. The only reliable signal
is a dedicated event:

```json
{"type":"event","event":{"type":"tool_hook_blocked","toolName":"write_file"}}
```

`agent-dispatch` scans every stream for it and fails the run — regardless of the
exit code or the reported subtype. `run` prints each check's verdict, so the reason
is visible rather than inferred from an exit code:

```
result: owner/name#12: failed — run_failed
  check spawned: pass
  check completed_in_time: pass
  check exit_code_zero: pass
  check subtype_success: pass
  check session_id_captured: pass
  check session_id_matches: pass
  check no_blocked_tool_events: FAIL
  run log: ~/.local/state/agent-dispatch/runs/owner__name/issue-12/<run-id>.ndjson
```

### Retries

`worker.max_attempts` bounds retries (default 3). A failed attempt within budget
leaves the task `queued` so the next dispatch retries it; when the budget is spent
the task becomes `failed` and `agent-dispatch retry` is required to restore it.

A retry **preserves** whatever the previous attempt wrote and starts a **fresh**
session in the same owned worktree. It is never presented as continuing the
interrupted conversation, because an interrupted first run has no transcript to
continue. The new session ID is pinned to the task.

---

## 10. Behaviour when GitHub is unavailable

Every GitHub problem is reported honestly, and none of them can mark an Issue as
completed:

| Condition | Behaviour |
|---|---|
| Wrapper missing / not executable | `poll_failed` with `missing_wrapper`; no state is touched |
| Repository not in the credential's permitted set | reported as `denied_repo`; existing task rows are left exactly as they were |
| Auth failure, rate limit, network error, timeout | reported with its kind; the task keeps its phase and is retried on the next poll |
| Wrapper returns unparseable output | treated as a hard error, not as an empty (silently successful) result |
| PR listing truncated (page cap reached) | reported as `incomplete_scan`; **nothing is queued** and no observation is cleared, because a pre-existing PR may sit on a page that was never read |
| Issue or PR deleted mid-poll | the task is left unchanged and reported; nothing is guessed |
| Runtime binary missing | `dispatch_unavailable` once, per poll; **no task is claimed**, so a queued Issue is not consumed by a configuration fault |
| Push failed | the branch is re-checked on the remote before the failure is believed, so an already-pushed branch (a crash window) is not reported as a push failure |
| Dispatcher's own commit failed | the run is still recorded as successful, and the reason is logged; the branch then has no new commits, so the produced-work evaluation is what decides whether a PR is opened |
| PR lookup failed after a push | the branch is kept, the task becomes `needs_attention`, and the next `run` adopts or creates the PR without re-running the agent |
| PR creation failed | recorded as a recoverable intent; the next `run` adopts the PR if GitHub did create it |

A failed poll never marks a task `finished`, and the worker never silently
switches identity or model.

The "cannot prove a negative" rule is applied consistently: an unreadable or
incomplete PR listing is never reported as "no PR exists", because that is what
would schedule a duplicate implementation of work someone already did.

---

## 11. Crash recovery and intent-before-action

The orchestrator records a side effect as **intended** *before* attempting it and
**confirmed** only after it succeeded (`operations` table).

Reconciliation runs **once at startup, under the single-instance lock**, from both
entry points: the persistent `worker` (the systemd path) and the explicit `run`.
That matters — without it a crash during a run would leave `phase=running`
forever, and because the MVP allows one active task globally, *every* future poll
would refuse to dispatch until someone ran `run` by hand. It is not gated on
`--no-execute`, since repairing state is not executing and that is the flag an
operator would reach for to unstick a task safely.

What happens to an interrupted task depends on **evidence, not on its phase**. Two
pieces of persisted evidence decide it: whether a run actually **completed**, and
the **parking stage** recorded when the dispatcher gave up (`tasks.recovery_stage`).

| Evidence | Repair |
|---|---|
| A completed, validated run, and the branch is on the remote | **Publish-only**: commit anything still uncommitted, push, adopt or create the PR. **No second agent run** |
| A completed run whose commit or push failed, so nothing reached the remote | **Publish-only from local commits**: commit, push, then create the PR. No model call |
| The newest run never completed (killed mid-edit) | Bounded fresh-session retry. Edits are **preserved and deliberately not committed**: partial work must not be published as though it were finished |
| No completed run and no recorded stage | Escalate. There is no evidence of finished work, so none is invented |
| Cannot determine whether the branch was published | `needs_attention`, with the reason. Never guessed, because guessing "absent" is how a stale row spends a second model call |

A bare `needs_attention` phase is deliberately **not** enough to trigger a push: it is
also where a maintainer parks a task for reasons the dispatcher cannot see, and
pushing new commits on their behalf is not a decision to make from a phase alone.

**The parking reason survives a failed recovery attempt.** A transient failure during
recovery — an `ls-remote` outage, or a commit that failed again — leaves
the recorded stage untouched, so the *next* healthy attempt can still publish. If that
stage were overwritten with `interrupted` on every unsuccessful attempt, one momentary
network problem would permanently destroy the task's recoverability. The dispatcher
re-parks as `interrupted` only when there is no publishable stage to preserve, which is
the honest answer for genuinely ambiguous work.

### Finished work is never queued for another implementation run

A task whose model run completed and only publishing failed (`commit_failed`,
`push_failed`, `pr_failed`) must never be turned back into an implementation queue
entry: the work is done, and a second Command Code run would spend credits twice and
could modify work that is already finished. Every path that could do that is closed:

| Action | Behaviour for publish-pending work |
|---|---|
| `retry` | **Refused**, with the reason and the action that does help. Retrying means "run the implementation again", which is exactly wrong here |
| `unpause` | Returns the task to `needs_attention` — i.e. "carry on with publication" — not to `queued` |
| Re-adding `take-it` after a label-withdrawn pause | Restores it to `needs_attention`, not `queued`. The label restores *dispatch intent*, but this task does not need another implementation run |
| `resume-publish` | Finishes publication for a paused publish-pending task — the counterpart of `retry`, and it actually publishes. When a live worker holds the lock it re-arms the task and hands off, because that worker finishes publication on its next poll |
| Any row left `queued` by an older build | The dispatcher **escalates** it and starts no runtime, and the atomic claim additionally refuses it in SQL |

The normal retry behaviour for a genuinely interrupted or failed runtime (no
publishable stage) is unchanged, and passing `resume-publish` a task with nothing
pending is refused rather than silently doing nothing.

The "branch is on the remote" check compares **tips**, not existence. A branch pushed
by an earlier attempt still exists while pointing at older commits, so an existence
check would accept a failed push and open a PR without the new work. Publishing
proceeds **only when the remote tip equals the owned worktree's tip**; a mismatch — or
an unreadable remote tip, since an unknown answer is not evidence the work landed —
makes nothing get published and parks the task with both SHAs recorded.

Reconciliation runs **once at startup, under the single-instance lock**, from both
entry points: the persistent `worker` (the systemd path) and the explicit `run`.
That matters — without it a crash during a run would leave `phase=running`
forever, and because the MVP allows one active task globally, *every* future poll
would refuse to dispatch until someone ran `run` by hand. It is not gated on
`--no-execute`, since repairing state is not executing and that is the flag an
operator would reach for to unstick a task safely. A transient wrapper outage at
startup does **not** consume the one reconciliation attempt, so a recovered wrapper
still repairs the task on the next poll.

**Publish-pending recovery also runs on every poll, not only at startup.** Startup
reconciliation happens once per process, but a task can become publish-pending
*while the service is alive*: re-adding `take-it` to a withdrawn task, a manual
`unpause`, or `resume-publish`. If recovery only ran at startup, those tasks would
sit untouched until the service happened to restart. The per-poll pass is
deliberately narrow — it visits only tasks already known to be publish-pending in an
`awaiting_review`/`needs_attention` phase — so it never re-checks published state for
every task the way the startup pass does, and it never starts a runtime.

**A `paused` task is never published by a poll.** The publishable stage deliberately
survives a `pause` so that unpausing can restore publication, which means a paused
task also looks publish-pending. Excluding it is what stops an automatic pass from
silently undoing a maintainer's `pause` — and from pushing to GitHub for a task that
reports itself as paused. Releasing the pause (`unpause`, re-adding `take-it`, or
`resume-publish`) is what restores publication.

| Crash point | Reconciliation on the next `worker` start or `run` |
|---|---|
| Process died mid-run, nothing published | the orphaned `running` row is closed as `failed`; the worktree is inspected and left untouched; the partial edits are **preserved but not committed**; the task returns to `queued` (or `failed` when the budget is spent) |
| Process died during `git push` | publish-only recovery: the run had completed, so the PR is created or adopted and no agent runs |
| Process died after `git push`, before the PR | adopt or create the PR; no agent runs |
| A failed push left an older branch on the remote | the tip comparison refuses to publish, and the task is parked so the mismatch is visible |
| Recovery itself hit a transient failure | the parking reason is kept, so the next attempt can still publish; the task is not downgraded to an unrecoverable state |
| PR created, DB write lost | adopt by exact head-branch match, verified head repository and Issue link |
| PR lookup or creation failed | `needs_attention` with the branch preserved; the next start finishes it publish-only |
| The dispatcher's own commit failed | `needs_attention` **before** any push, so no PR can omit the run's work; the next start commits the preserved edits, pushes and opens the PR — no model call |
| Duplicate poll | one task row per `(repo, Issue)`; a task not in `queued` is never dispatched |
| Owned worktree on the wrong branch | refused and escalated: work is never committed or published from a branch this task does not own |
| PR merged or closed externally | no new rounds; the task ends via the normal reconciliation rules |
| Owned worktree with uncommitted edits | **preserved** and reported; committed only when a completed run produced them |
| A task parked by a maintainer | left alone. Recovery does not push commits or open PRs for it |
| A task that becomes publish-pending while the service is running | finished by the next poll of the **same** worker process — no restart, no second agent run |

An orphaned `running` row needs no PID liveness check precisely *because* of the
single-instance lock: while this process holds the lock, any `running` row it finds
belongs to a previous process, not a concurrent one.

Nothing is ever deleted to "clean up". Removing a worktree or branch is an explicit
operator action, not a side effect of a task ending.

---

## 12. Credentials and security posture

### Commits the dispatcher makes itself

When the agent leaves its work uncommitted, the dispatcher commits it so the branch
can be pushed. That commit is made with an **explicit** identity
(`worker.commit_identity_name`/`commit_identity_email`, applied as `git -c`), never
the ambient `user.email`:

```toml
commit_identity_name = "agent-dispatch"
commit_identity_email = "agent-dispatch@localhost"
```

This is not cosmetic. A machine with no configured Git identity fails the commit
with exit 128 (`Author identity unknown`), so the finished work never reaches the
branch — and because a development VM usually *does* have `user.email` set, the
failure appears only somewhere else, such as CI. Point the email at your GitHub
noreply address if you want those commits attributed to you; leaving it as the
default keeps the honesty that they were made by the service, not by you.

### The credential ordering is not optional

`credential.helper` is **multi-valued**, and a bare
`-c credential.<url>.helper=!<cmd>` **appends** to the inherited list instead of
replacing it. An ambient helper is then queried first and can answer with a
different credential.

**Measured on this VM (2026-09-25): the ambient `/usr/bin/gh auth git-credential`
currently *does* return a real credential for `github.com`.** The reset entry is
therefore load-bearing, not defensive. It was verified with an instrumented decoy
helper, and the control case (no reset) confirmed the decoy is otherwise called
first.

`agent-dispatch` applies the reset-then-wrapper pair two ways:

1. **`GIT_CONFIG_COUNT`/`KEY_n`/`VALUE_n` in the environment** of every
   orchestrator Git command *and* of the agent subprocess. These variables are
   inherited by children, so Git the agent spawns itself also resolves the approved
   wrapper. This is the primary mechanism.
2. Optionally, the repository's **local** config
   (`worker.write_repo_local_credentials = true`). Off by default, because a
   worktree shares its clone's Git common config — which is why provisioning points
   at an orchestrator-managed source clone rather than your working checkout.

To verify the effective chain for yourself:

```bash
cd <owned_worktree>
GIT_CONFIG_COUNT=2 \
GIT_CONFIG_KEY_0=credential.https://github.com.helper GIT_CONFIG_VALUE_0= \
GIT_CONFIG_KEY_1=credential.https://github.com.helper \
GIT_CONFIG_VALUE_1='!/home/craftlypse/.local/bin/gh-craftlypse auth git-credential' \
  git config --get-all credential.https://github.com.helper
# -> an empty line first (the reset), then the wrapper
```

> **Do not run bare `git credential fill` to check this.** It prints the resolved
> token to your terminal and into your shell transcript. The command above never
> reveals a credential.

### Stated plainly

* **`--yolo` grants the agent unrestricted shell and file access as your user.**
  This is a deliberate trust choice on a single-user VM, not isolation.
* **A Git worktree is not a sandbox.** It is not a filesystem, process, network or
  credential boundary. An unrestricted agent can read and write outside it.
* **The wrapper is an operational guardrail, not a hard boundary.** The
  `gh-craftlypse` credential is scoped to selected repositories and the wrapper
  injects `GH_TOKEN` only into its own child, but an agent with a shell could
  attempt to bypass it. A deny from the wrapper is a **visible failure**; the
  service never switches identity to get around it.
* **Issue and PR text is untrusted task content.** The instruction fences it and
  labels it as data, but that does not make it an authorization boundary: the
  permission flags come from configuration, and anyone who can open an Issue can
  put text in front of the agent.
* **The agent's own summary is quoted, not verified.** The PR body labels it
  explicitly as unverified task output; only the checks the orchestrator observed
  itself are asserted as facts.

---

## 13. The Issue status comment (one comment, edited in place)

While a task runs, `agent-dispatch` keeps **one status comment on the source
Issue** current, so progress is visible on GitHub without SSH access to the VM.
It looks like this (content is illustrative):

```markdown
### agent-dispatch · task status
- **Status:** Running
- **Runtime:** Command Code · model `<pinned model>` · thinking effort `<pinned effort>`
- **Attempt:** 1 of 3
- **Run started:** 2026-09-25 00:00 UTC
- **Last checked:** 2026-09-25 00:10 UTC
- **Elapsed:** 10 min
- **Command Code process:** alive — task still running. This reports the subprocess
  only; it is not a claim that the model is generating tokens, and no progress
  estimate is available.
- **Last observed runtime event:** 2026-09-25 00:07 UTC (12 events observed)
- **Pull request:** #42 (<url>) — created by this task
```

**It is one comment, edited — not a new comment per heartbeat.** A comment edit is
not a promise of a GitHub notification, and this is a visibility surface, not an
alerting channel.

### States

| State | Means |
|---|---|
| `Queued` | claimed or re-armed; no process yet |
| `Starting` | claim taken and the comment exists, but no process has been observed |
| `Running` | the Command Code subprocess has been observed alive |
| `Publishing` | the runtime finished cleanly; commit/push/PR has **not** succeeded yet |
| `Awaiting review` | an **owned** PR exists and was verified |
| `Recovering publication` | a publish-only failure is being finished off — no model call |
| `Needs attention` | publication could not complete and needs a human |
| `Paused` | a maintainer paused it; never overwritten by a heartbeat or auto-recovery |
| `Failed` / `Interrupted` / `Finished` | terminal |

`Publishing` is deliberately not `Awaiting review`: a clean model result is not a
pull request, and the maintainer is told which of the two is actually true. The
publish-only recovery paths from §11 (`commit_failed`, `push_failed`, `pr_failed`)
surface as `Recovering publication` / `Needs attention`, and converge on the
**same** comment when a later poll finishes them — with no new model call.

### What it will not say

* **A live PID is not progress.** "Process is alive" describes the subprocess only.
  Elapsed time and the last *observed* stream event are reported instead of a
  percentage, a token count or an ETA — none of which the dispatcher can observe.
* **The last-event line appears only when an event was actually observed.** An
  unobserved timestamp is omitted entirely rather than printed as a placeholder:
  an invented timestamp is worse than an absent one.
* **Terminal states carry no liveness fields at all**, so a finished task cannot be
  mistaken for a running one.
* **No transcripts, prompts, secrets or filesystem paths** reach a public Issue.

### Delivery is best-effort by contract

A slow, timed-out, rate-limited or denied GitHub write is logged as a warning
(`status_comment_unexpected_error`, `status_comment_not_created`) and is otherwise
inert: it never interrupts, fails, restarts or extends the run, never changes the
task's phase, claim, attempt budget or run validation, and never switches identity
to get around a denial. A later poll or reconciliation retries it without creating
a duplicate.

The heartbeat runs on **one bounded in-process thread per live run**. It starts
when the subprocess is first observed and wakes on the spawn signal — not on a
sleep — so the first `Running` update is immediate rather than delayed by up to
one poll. (Without that, a run shorter than the poll interval could finish before
any tick, and the comment would jump `Starting` → `Publishing` without ever
reporting `Running`.) It is a thread rather than a poll-time hook because
`CommandCodeDriver.run()` blocks reading the NDJSON stream: a heartbeat tied to
the worker's polling loop would not run while the agent is actually working. The
thread writes to GitHub only and never touches SQLite, so the worker's database
connection stays owned by one thread.

**"No heartbeat lands after a terminal state" is structural, not a timeout.** One
`StatusPublisher` is shared by the whole run lifecycle — `Starting`, the heartbeat,
`Publishing` and the terminal sync — so they all take the *same* lock. When the run
stops, the owning thread marks terminal publication as begun *under that lock*
before its first terminal write. A join is deliberately not trusted for this: it is
allowed to time out while a heartbeat is still inside the transport (a failed PATCH,
a marker re-scan and a second PATCH are several sequential bounded calls), so relying
on it would leave a window in which `Running` could overwrite `Awaiting review`. Any
heartbeat write that starts after the flag is set is refused, and a heartbeat tick is
a **strictly narrower operation than a foreground write**: it may only PATCH the
comment id it already holds. It never resolves ownership, never scans the Issue,
never adopts and never creates — those stay on the owning thread, and a later poll
already has that path. Two reasons for the narrowing: a scan is a paginated
multi-call sequence, so allowing it inside a tick would let one tick outlive its join
budget *and* make the owning thread's terminal write block behind it — and that write
sits immediately before commit/push/PR.

### Ownership survives a crash between create and record

Three things keep the comment from being duplicated or hijacked:

1. The comment carries a machine marker scoped to the exact repo and Issue:
   `<!-- agent-dispatch:status repo="owner/name" issue=17 -->`.
2. The `status_comments` row is written **before** the create call, so the intent
   survives a crash inside that window.
3. On resolution the Issue's comments are scanned for that exact marker, and the
   owning comment is reused.

Marker matching is exact, so a comment that merely *looks* like a status comment is
never adopted, and another person's comment is never edited or deleted. Two further
rules keep that honest:

* **Ambiguity fails closed.** If *more than one* exact marker exists, ownership is
  genuinely ambiguous, so nothing is edited and nothing is created — the dispatcher
  logs `status_comment_ambiguous` and waits for an operator to delete the extra
  comment. Picking one arbitrarily could edit a comment that is not ours, and would
  compound the duplicate problem this design exists to prevent.
* **An unprovable scan never creates.** If the comment listing comes back
  **truncated**, "no marker found" cannot be proven, so nothing is created and a
  warning is logged rather than risking a duplicate.
* **A failed edit never creates either.** A rate limit, timeout or network error
  fails identically to a deleted comment, so a failed edit triggers a re-scan; if
  that scan finds a marked comment, it is adopted and retried and creation stays
  refused. Creating is only safe once a **complete** scan proves no marked comment
  exists.

### Configuration

```toml
[worker]
status_heartbeat_seconds = 300   # 5 minutes; lower only for testing
```

The initial `Starting` and the terminal updates happen immediately and never wait
for this interval. Nothing is written while a task is merely `awaiting_review`, and
a poll that changes nothing costs no GitHub write at all.

That last guarantee depends on the body being a function of **durable state only**.
A non-live status therefore takes its `Status updated` stamp from the task row
rather than from the moment it was rendered: rendering the current time made the
body differ on any poll that crossed a minute boundary, so the "body unchanged, skip
the write" comparison could not recognise an idle comment and re-edited it on every
such poll.

`status`, `dry-run` and `open` stay **read-only**: they display the comment row but
never create or edit a comment, and never start a runtime.

Read-only stores deliberately do not migrate, so an **upgraded database has no
`status_comments` table until a write path opens it**. Reads treat an absent table
as "this build has published no status comments yet" rather than failing, so
`status` / `dry-run` / `open` work immediately after an upgrade and the first
worker or `enqueue` run creates the table normally. A read command never adds it.

---

## 14. Verifying this release

Everything below is available after `uv sync --locked`. The scripts accept a
`PYTHON=` override so the suite can run on the uv-managed interpreter, which is
what CI does:

```bash
uv sync --locked                       # prerequisite: one locked environment
uv run --no-sync ruff check .          # lint
uv run --no-sync ruff format --check . # formatting
uv run --no-sync pre-commit run --all-files

PYTHON=.venv/bin/python ./scripts/test-offline.sh   # 280 tests, no network, no credits
PYTHON=.venv/bin/python ./scripts/smoke-runtime.sh --mock

agent-dispatch doctor          # live capability report for this VM
agent-dispatch status --no-sync    # on-disk state only, no API calls, writes nothing
agent-dispatch dry-run         # live read-only poll
agent-dispatch dry-run --no-sync   # local state only, no API calls
agent-dispatch status          # read-only snapshot + simulated poll
```

The suite covers the #4 execution path end to end with **no model credits and no
GitHub mutation**: `tests/fake_wrapper.py` serves GitHub and `tests/fake_runtime.py`
is a real executable standing in for `commandcode`, so the tests drive the real
`CommandCodeDriver`, worktree creation, NDJSON parsing and process kill rather than
a mock. The cases most worth knowing about:

| Test | Proves |
|---|---|
| `BlockedRunTests` | a `tool_hook_blocked` stream with `subtype=success` and exit 0 **fails** the task and opens no PR |
| `TimeoutTests` | a hung run is killed at the deadline, recorded as a timeout, and leaves no live process |
| `RetryTests` | an interrupted run's edits are preserved and the retry starts a **fresh** session |
| `IdempotencyTests` | restart adopts the existing PR/branch instead of duplicating work |
| `EligibilityTests` | a withdrawn label, a paused task or a foreign PR prevents the run — even when named explicitly; a crash after push recovers **publish-only** |
| `WorkerStartupReconciliationTests` | the `worker` entry point (not just `run`) repairs an orphaned `running` row, and an orphan no longer blocks dispatch globally |
| `PublishRecoveryTests` | a `needs_attention` row with a pushed branch is finished off without a second model call |
| `FailedFirstCommitRecoveryTests` | a first run whose commit failed is recovered locally — commit, push, one PR, zero extra runtime invocations — and a maintainer-parked task is left alone |
| `FailedPushRecoveryTests` | a stale remote tip blocks publishing, and a genuine publish stage pushes and matches tips |
| `UnfinishedRunNotPublishedTests` | a killed agent's partial edits are never committed or published, and are preserved for retry |
| `NoWorktreePublishTests` | Git is never run against an invented working directory; the PR is reconciled through the wrapper instead |
| `ReconcileRetryTests` | a transient wrapper outage does not consume the single reconciliation attempt |
| `RecoveryStagePreservationTests` | a second failure during recovery keeps the publishable stage, so the task stays recoverable and the retry succeeds with zero model calls |
| `PublishTipFailsClosedTests` | an unreadable remote tip opens no PR, while a confirmed matching tip still publishes |
| `PublishPendingTransitionTests` | `retry`, pause/unpause, `resume-publish` and re-adding `take-it` never queue an implementation run for finished work, a legacy `queued` row starts no runtime, and `resume-publish` exits non-zero when publication fails again |
| `LiveWorkerPublishPickupTests` | one already-running `Worker` finishes publication on its next poll after `take-it` returns or an `unpause` — zero extra runtime calls, exactly one PR — and a maintainer `pause` is never overtaken by a poll |
| `MigrationRaceTests` | two processes can migrate the same new database without the loser crashing |
| `CommitFailureTests` | a failed commit stops before push and opens no PR, preserving the edits |
| `WorktreeIdentityTests` | a worktree switched to another branch is detected and never published from |
| `PrAdoptionVerificationTests` | a fork's PR, an unlinked PR and a short payload are refused; a genuine PR is adopted |
| `SessionCaptureTests` | the session ID reaches SQLite while the run is still in flight, and is still not advertised as resumable |
| `CredentialEnvironmentMockedTests` | the agent inherits the reset-then-wrapper ordering, and repo-local config is opt-in |
| `StatusReadOnlyTests` | `status`/`dry-run`/`open` start no agent and create no state |
| `SingleCommentLifecycleTests` | one comment per task is created at the real start, heartbeats **edit its ID** and never POST another, and no heartbeat edit follows the terminal update |
| `HeartbeatDuringRunTests` | heartbeats land **while the runtime is blocked**, not merely when the polling loop resumes |
| `PublicationLifecycleTests` | clean completion → forced `commit_failed`/`push_failed`/`pr_failed` → trouble shown rather than success → the same long-lived worker finishes it on a later poll → the same comment becomes `Awaiting review` with zero extra model calls |
| `PauseAndTransitionTests` | a manual `pause` stays `Paused` and is never overtaken; `unpause`/re-added `take-it`/`resume-publish` restore publication |
| `MarkerRecoveryTests` | a crash between comment creation and recording its ID reuses the marked comment, ending with exactly one |
| `NonFatalDeliveryTests` | an edit timeout / rate limit / denied write leaves the run result correct, logs a warning, and creates no duplicate |
| `ReadOnlyTests` | `status`/`dry-run` never create or edit a comment and never start a runtime |

`test-offline.sh` ends by asserting that no state, lock or run-log artefact was
created inside the checkout; `test_the_run_log_lives_outside_the_worktree` asserts
the same for a real run's worktree.

Two failure modes are worth exercising deliberately, because a tooling gate that
cannot fail is not a gate:

```bash
# 1. A malformed file must be rejected.
printf 'def broken(\n  x =\n' > src/agent_dispatch/_probe.py
uv run --no-sync ruff check .            # fails
rm src/agent_dispatch/_probe.py

# 2. A dependency change without a refreshed lock must be rejected.
#    (Edit [dependency-groups] in pyproject.toml, then:)
uv sync --locked                         # fails: lock is out of date
uv lock                                  # refresh it deliberately
```

---

## 15. Not in this release

Documented so nothing here is mistaken for a working feature. These belong to
Issues #5/#6 and are **not implemented, not stubbed and not faked**:

- the `agent:fix` review handoff, feedback collection, grouping and cursors (#5);
- same-session follow-up rounds after review feedback, and re-arming the label (#5);
- multi-repo concurrency above one (configuration rejects it), distributed leases,
  a broker, webhooks, a dashboard, or a provider-plugin system (#6);
- automatic merge, automatic approval, Issue closure, or deletion of unknown
  worktrees — permanently out of scope, not deferred.

Also deliberately absent, because the alternative would be a false claim:

- **No native VS Code Chat session handoff.** Inspect and resume CLI sessions in
  the integrated terminal of a Remote SSH window; that is the supported interface.
- **No container or OS sandbox.** `--yolo` plus a worktree is a trust choice on a
  single-user VM, and §12 says so plainly.
- **No live end-to-end VM run is claimed by this document.** The offline suite
  proves the orchestration logic against fakes; an actual Command Code run, real
  push and real PR are an explicit, opt-in operator action
  (`agent-dispatch run`) and are only "observed" when someone observes them.

# agent-dispatch — operations (Issue #3)

Operator guide for the MVP foundation: an installable CLI, one polling worker, one
SQLite queue. **This release queues work; it does not run an agent, create
worktrees, push branches or open PRs** (Issues #4/#5).

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
```

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
agent-dispatch status                 # poll, then show every task and why it is/ isn't dispatchable
agent-dispatch status --no-sync       # local state only (no API calls)
agent-dispatch status --json          # machine-readable
agent-dispatch dry-run                # one poll that writes NOTHING to disk or GitHub
agent-dispatch enqueue --repo owner/name --issue 12
agent-dispatch pause   --repo owner/name --issue 12
agent-dispatch unpause --repo owner/name --issue 12
agent-dispatch retry   --repo owner/name --issue 12
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

| Command | Reads | Makes API calls | Persists |
|---|---|---|---|
| `status --no-sync` | on-disk state directly | no | nothing |
| `status` | on-disk state, then a poll against a scratch copy | yes (read-only) | nothing |
| `dry-run --no-sync` | on-disk state | no | nothing |
| `dry-run` | on-disk state, then a poll against a scratch copy | yes (read-only) | nothing |
| `worker` | — | yes | **yes** — the only command that does |

So `status` and `dry-run` show what the queue *would* look like; the on-disk state
they report only advances when the worker (or an explicit `enqueue`/`pause`/
`unpause`/`retry`) runs. If a sync is incomplete, `status` says so and falls back to
showing on-disk state rather than presenting a partial poll as authoritative.

**`enqueue` is not a shortcut around the rules.** It addresses one Issue by number
and applies exactly the same decision the poll applies, so it refuses a PR number,
a closed Issue, an unlabelled Issue, or an Issue that already has a PR. Re-running
it on an already-queued Issue is a success that reuses the existing row.

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

---

## 7. What `status` means

Phases this release can produce: `queued`, `paused`, `finished`, `needs_attention`.

**No task is promoted to `running`.** Discovery and queueing are not evidence that
anything was implemented; `status` states this explicitly rather than implying an
agent ran. `running`, `awaiting_review` and `feedback_queued` become reachable in
#4/#5.

A task is **dispatchable** only when all of these hold: the Issue is open, it
still carries `take-it`, no relevant PR already exists for it, and its phase is
`queued`. Otherwise `status` prints the specific blocker.

### Observed PR vs. owned PR

Two different facts are recorded separately, and conflating them would be a real
bug once #4 runs agents:

| Column | Meaning | Written by |
|---|---|---|
| `observed_state.linked_pr_number` | a PR that *already exists* for this Issue — a human's PR, or an earlier one | discovery, every poll |
| `tasks.pr_number` | the PR **this worker's own run created** | only the #4 PR workflow |

Discovery **never** writes `tasks.pr_number`, not even when the PR sits on a
`dispatch/issue-N-...` branch. A branch name is a strong hint, not proof of
ownership, and a review round acting on a PR this worker did not create is exactly
the failure this separation prevents. An Issue with an observed PR is recorded as
`awaiting_review` and stays non-dispatchable.

---

## 8. Logs and state

| What | Where |
|---|---|
| State database | `worker.state_db` (default `~/.local/state/agent-dispatch/state.db`) |
| Run logs (NDJSON, written by #4) | `worker.run_log_dir`, retained to `worker.run_log_keep` files per task |
| Worker lock | `worker.lock_file` (pid + start time + command; never credentials) |
| Service logs | `journalctl --user -u agent-dispatch -f` |

`--log-format json` emits one JSON object per line for journald/CI capture.

Run logs live **outside** every repository and worktree by design, and their
contents are never printed to CI or pasted into GitHub. `status` reports log
*paths*, never contents.

---

## 9. Behaviour when GitHub is unavailable

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

A failed poll never marks a task `finished`, and the worker never silently
switches identity or model.

The "cannot prove a negative" rule is applied consistently: an unreadable or
incomplete PR listing is never reported as "no PR exists", because that is what
would schedule a duplicate implementation of work someone already did.

---

## 10. Verifying this release

Everything below is available after `uv sync --locked`. The scripts accept a
`PYTHON=` override so the suite can run on the uv-managed interpreter, which is
what CI does:

```bash
uv sync --locked                       # prerequisite: one locked environment
uv run --no-sync ruff check .          # lint
uv run --no-sync ruff format --check . # formatting
uv run --no-sync pre-commit run --all-files

PYTHON=.venv/bin/python ./scripts/test-offline.sh   # 92 tests, no network, no credits
PYTHON=.venv/bin/python ./scripts/smoke-runtime.sh --mock

agent-dispatch doctor          # live capability report for this VM
agent-dispatch status --no-sync    # on-disk state only, no API calls, writes nothing
agent-dispatch dry-run         # live read-only poll
agent-dispatch dry-run --no-sync   # local state only, no API calls
agent-dispatch status          # read-only snapshot + simulated poll
```

`test-offline.sh` ends by asserting that no state, lock or run-log artefact was
created inside the checkout.

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

## 11. Not in this release

Documented so nothing here is mistaken for a working feature. These belong to
Issues #4/#5/#6 and are **not implemented, not stubbed and not faked**:

- starting a coding agent at all (no `commandcode` invocation exists in this code)
- worktree creation, branch creation, `git push`, PR creation
- session IDs, resume, run-log streaming and interrupted-run recovery
- `agent:fix` review rounds, feedback collection and cursors
- multi-repo concurrency above one (configuration rejects it), distributed leases,
  a broker, webhooks, a dashboard, or a plugin system
- Git credential-helper injection into worktrees (#4 scope; config keys exist and
  are validated, but no worktree is provisioned here)

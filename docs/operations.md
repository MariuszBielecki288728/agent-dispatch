# agent-dispatch — operations (Issue #3)

Operator guide for the MVP foundation: an installable CLI, one polling worker, one
SQLite queue. **This release queues work; it does not run an agent, create
worktrees, push branches or open PRs** (Issues #4/#5).

---

## 1. Install

The service has **no third-party dependencies** (stdlib `tomllib`, `sqlite3`,
`fcntl`). The VM's system Python has no `pip`, so install into a virtualenv:

```bash
python3 -m venv ~/.local/share/agent-dispatch/venv
~/.local/share/agent-dispatch/venv/bin/pip install .
mkdir -p ~/.local/bin
ln -sf ~/.local/share/agent-dispatch/venv/bin/agent-dispatch ~/.local/bin/agent-dispatch
```

Alternatively, run it straight from a checkout with `PYTHONPATH=src python3 -m agent_dispatch`.

---

## 2. Configure

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

## 3. Create the labels intentionally

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

## 4. Run the worker

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

## 5. Day-to-day commands

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

## 6. What `status` means

Phases this release can produce: `queued`, `paused`, `finished`, `needs_attention`.

**No task is promoted to `running`.** Discovery and queueing are not evidence that
anything was implemented; `status` states this explicitly rather than implying an
agent ran. `running`, `awaiting_review` and `feedback_queued` become reachable in
#4/#5.

A task is **dispatchable** only when all of these hold: the Issue is open, it
still carries `take-it`, no relevant PR already exists for it, and its phase is
`queued`. Otherwise `status` prints the specific blocker.

---

## 7. Logs and state

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

## 8. Behaviour when GitHub is unavailable

Every GitHub problem is reported honestly, and none of them can mark an Issue as
completed:

| Condition | Behaviour |
|---|---|
| Wrapper missing / not executable | `poll_failed` with `missing_wrapper`; no state is touched |
| Repository not in the credential's permitted set | reported as `denied_repo`; existing task rows are left exactly as they were |
| Auth failure, rate limit, network error, timeout | reported with its kind; the task keeps its phase and is retried on the next poll |
| Wrapper returns unparseable output | treated as a hard error, not as an empty (silently successful) result |
| Issue or PR deleted mid-poll | the task is left unchanged and reported; nothing is guessed |

A failed poll never marks a task `finished`, and the worker never silently
switches identity or model.

---

## 9. Verifying this release

```bash
./scripts/test-offline.sh      # 65 tests, deterministic, no network, no model credits
./scripts/smoke-runtime.sh --mock
agent-dispatch doctor          # live capability report for this VM
agent-dispatch dry-run         # live read-only poll
```

`test-offline.sh` ends by asserting that no state, lock or run-log artefact was
created inside the checkout.

---

## 10. Not in this release

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

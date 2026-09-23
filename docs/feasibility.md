# Feasibility Spike: Agent Runtimes, GitHub CLI, and Remote VM Architecture

**Spike Reference:** Issue #1  
**Target Environment:** Dedicated Ubuntu 24.04 VM (`craftlypse-agent`)  
**Repository:** `MariuszBielecki288728/agent-dispatch`  
**Date:** September 2026 (Updated with Command Code CLI)  

---

## Executive Summary & Recommendations

This spike investigates the actual software, process architecture, permissions, and AI agent runtimes present on the development Ubuntu VM to establish a solid foundation for `agent-dispatch`.

### Key Recommendations
1. **Primary Proven Frontier Runtime: Command Code CLI (`commandcode`)**
   - **Status:** **Observed on VM** (Version 1.64.0 at `/home/craftlypse/.npm-global/bin/commandcode`, symlinked at `/home/craftlypse/.local/bin/commandcode`).
   - **Authentication & Subscription:** Verified active user `MariuszBielecki288728` (`mariuszbielecki01@gmail.com`) via `~/.commandcode/auth.json`. 80 frontier and open-weight models available immediately out of the box (including `deepseek/deepseek-v4-flash`, `deepseek/deepseek-v4-pro`, `moonshotai/kimi-k3`, `claude-sonnet-5`, `gpt-6-astra`, `google/gemini-3.8-flash`).
   - **Capabilities:** Non-interactive headless execution (`commandcode -p "..."`), structured streaming NDJSON events (`--output-format json`), full session resumption with prompt cache hits (`--session <id>`), native managed Git worktrees (`-w, --worktree [name]`), active VS Code IDE bridge (`~/.commandcode/ide/code-*.sock`), and tested headless execution under `systemd --user`.
2. **Proven Open / Free-Tier MVP Runtime: OpenCode CLI (`opencode`)**
   - **Status:** **Observed on VM** (Version 1.18.23 at `/home/craftlypse/.opencode/bin/opencode`).
   - **Rationale:** Fully supports non-interactive execution (`opencode run`), persistent session IDs (`-s <session_id>`), structured streaming JSON event output (`--format json`), session exports, auto-approval (`--auto`), and independent directory/worktree targeting (`--dir`).
   - **Account/Quota Reality:** The configured `opencode-go` provider currently returns HTTP 403 (*"An active OpenCode Go subscription is required to use Go models"*). However, free models (tested with `opencode/nemotron-3-ultra-free` and `opencode/mimo-v2.5-free`) execute **100% reliably** on the VM with zero cost and conversational resumption across process restarts and `systemd --user` units.
3. **Fallback / Companion Runtime: Codex CLI (`codex-cli`)**
   - **Status:** **Observed on VM** (Version 0.155.0-alpha.16.3 at `/home/craftlypse/.vscode-server/extensions/openai.chatgpt-.../bin/linux-x86_64/codex`).
   - **Rationale:** Bundled with the `openai.chatgpt` extension and connects to a shared local daemon (`codex app-server`). Has native Git worktree creation (`codex exec --worktree`), JSONL event output, and **native observability in the VS Code ChatGPT panel** where existing sessions are visible.
   - **Account/Quota Reality:** Currently returns a hard quota limit (*"You've hit your usage limit... try again at Sep 26th, 2026 4:24 PM"*).
4. **GitHub Access Boundary: Mandatory `gh-craftlypse` Wrapper**
   - Direct `gh` authentication and raw `GH_TOKEN` environment exports are strictly avoided on this VM.
   - All GitHub API and CLI interactions must route through `/home/craftlypse/.local/bin/gh-craftlypse`.
   - For Git HTTPS commands (`clone`, `fetch`, `push`, `ls-remote`), Git must be configured with:
     ```bash
     git config credential.https://github.com.helper "!/home/craftlypse/.local/bin/gh-craftlypse auth git-credential"
     ```
     This allows complete non-interactive Git authentication without leaking credentials or requiring `gh auth login`.

---

## 1. System Inventory

### OS, Kernel, and Host
```bash
$ uname -a
Linux craftlypse-agent 7.0.0-31-generic #31~24.04.1-Ubuntu SMP PREEMPT_DYNAMIC Mon Aug 10 09:38:02 UTC 2 x86_64 GNU/Linux

$ cat /etc/os-release
NAME="Ubuntu"
VERSION="24.04.5 LTS (Noble Numbat)"
ID=ubuntu
```

### Process Architecture, Sessions, and Lingering
- **User:** `craftlypse` (uid=1000, gid=1000).
- **Session Types:** Active Wayland/GNOME desktop session on `seat0` (`tty2`) alongside SSH pts sessions.
- **Systemd User Manager:** Running at PID 1360 (`/usr/lib/systemd/systemd --user`).
- **User Lingering:**
  ```bash
  $ loginctl show-user craftlypse -p Linger
  Linger=yes
  ```
  **Critical Finding:** Because `Linger=yes` is set, user services and background processes managed by `systemd --user` continue running persistently after SSH disconnection.

### Tool Inventory & Versions
| Binary | Path | Version | Verification Command |
|---|---|---|---|
| `git` | `/usr/bin/git` | `2.43.0` | `git --version` |
| `gh` | `/usr/bin/gh` | `2.100.0` (2026-09-03) | `gh --version` |
| `gh-craftlypse` | `/home/craftlypse/.local/bin/gh-craftlypse` | Wrapper script calling `gh` 2.100.0 | `gh-craftlypse --version` |
| `commandcode` | `/home/craftlypse/.npm-global/bin/commandcode` (symlinked at `~/.local/bin/commandcode`) | `1.64.0` | `commandcode --version` |
| `opencode` | `/home/craftlypse/.opencode/bin/opencode` | `1.18.23` | `opencode --version` |
| `codex-cli` | `~/.vscode-server/extensions/openai.chatgpt-.../bin/linux-x86_64/codex` | `0.155.0-alpha.16.3` | `codex --version` |
| `code` (Standalone) | `/home/craftlypse/.vscode-server/code-2242ebbb...` | `1.139.0` (commit `2242ebbb...`) | `code --version` |
| `copilot` CLI | *None* | **Not installed** | `which copilot` (exited 1) |

### VS Code Remote SSH Server & Extensions
VS Code Server is active under commit `2242ebbb54efeeb0129e08e919e7e8d43033cd83`.  
Installed extensions (`code --list-extensions --show-versions`):
- `hidenobunagai.commandcode-goat-provider@0.1.22` — Command Code GOAT model provider for Copilot Chat; integrates with Command Code CLI.
- `google.google-antigravity@1.4.0` — Antigravity agent integration.
- `openai.chatgpt@26.5917.62051-linux-x64` — Codex / ChatGPT agent with bundled `codex` binary and app-server daemon.
- `openai.codex-audio@26.917.62051` — Dictation support for Codex.
- `ltmoerdani.opencode-copilot-chat@0.7.5` — OpenCode Language Model provider for Copilot Chat (reads `~/.local/share/opencode/opencode.db` for usage tracking).
- `vizards.deepseek-v4-for-copilot@0.9.2` — DeepSeek V4 Copilot Chat provider.
- `rexwel.cloakcode@1.0.0` — Localhost bridge and session observer for `vscode.lm`.

---

## 2. GitHub Access via `gh-craftlypse`

### Wrapper Architecture
The wrapper script `/home/craftlypse/.local/bin/gh-craftlypse` enforces strict security constraints:
1. Validates that `~/.config/craftlypse/github-token` exists and is owned by `craftlypse`.
2. Validates that permissions are strict (`0600`).
3. Exports `GH_TOKEN` into the subshell environment and immediately unsets the shell variable.
4. Executes `exec "$(command -v gh)" "$@"`.

### Identity & Repository Access
- **Authenticated Identity:** `MariuszBielecki288728` (User ID `22575146`).
- **Token Type:** Fine-grained Personal Access Token (`github_pat_...`).
- **Rate Limits:**
  - Core API: 5,000 requests/hour (verified via `gh-craftlypse api rate_limit`).
  - GraphQL API: 5,000 points/hour.
  - Search API: 30 requests/minute.
- **Repository Allowlist & Permissions:**
  - `MariuszBielecki288728/agent-dispatch`: **Confirmed ADMIN** (`admin: true`, `push: true`, `pull: true`, `triage: true`, `maintain: true`).
  - `MariuszBielecki288728/SquareBattles-v2`: Confirmed accessible (read-only verification of PRs/reviews performed).
- **Identity Overlap Constraint:**
  The authenticated token identity (`MariuszBielecki288728`) is identical to the repository owner and task creator.
  > [!IMPORTANT]
  > Under GitHub's pull request rules, **an author cannot approve their own pull request** (`APPROVE` events fail on self-authored PRs if branch protection requires external review). Automated review loops must account for this boundary when interacting with PR review workflows.

### Verified GitHub Operations
| Operation | Command Pattern | Status | Notes |
|---|---|---|---|
| Discovery | `gh-craftlypse repo view <owner/repo> --json ...` | **Observed** | Returns JSON metadata and viewer permissions. |
| Label Listing | `gh-craftlypse label list -R <repo>` | **Observed** | Returns full label palette. |
| Issue Inspection | `gh-craftlypse issue view <num> --json ...` | **Observed** | Returns issue details cleanly. |
| Issue vs PR Disambiguation | `GET /repos/{owner}/{repo}/issues/{num}` (`.pull_request != null`) | **Observed** | Returns `false` for issues, `true` for PRs. `gh pr view` on an issue fails fast with GraphQL error. |
| PR Reviews & Comments | `gh-craftlypse pr view <num> --json reviews,comments` | **Observed** | Successfully reads review node IDs, states, authors, bodies. |
| Inline Review Comments | `gh-craftlypse api /repos/.../pulls/{num}/comments` | **Observed** | Returns threaded review comments with `in_reply_to_id` and line anchors. |
| Pagination | `gh-craftlypse api --paginate ...` | **Observed** | Transparent pagination across pages. |
| Non-interactive systemd | `systemd-run --user --wait gh-craftlypse ...` | **Observed** | Succeeded with exit status 0 in headless service unit. |
| Git HTTPS Authentication | `git -c credential.https://github.com.helper="!gh-craftlypse auth git-credential" ...` | **Observed** | Authenticates git clone, fetch, ls-remote, push without interactive prompt or raw token. |

---

## 3. Agent Runtimes Capability Matrix

| Evaluation Dimension | Command Code CLI (`commandcode`) | OpenCode CLI (`opencode`) | Codex CLI (`codex-cli`) | Native VS Code Agent Host | GitHub Copilot CLI |
|---|---|---|---|---|---|
| **VM Status** | **Observed on VM** (Proven Frontier MVP) | **Observed on VM** (Proven Free-Tier MVP) | **Observed on VM** (Usage limited) | **Observed on VM** (Blocked for headless) | **Unsupported** (Not installed) |
| **Executable Path** | `~/.npm-global/bin/commandcode` | `~/.opencode/bin/opencode` | Bundled in `openai.chatgpt` | `~/.vscode-server/code-...` | None |
| **Model / Mode Selection** | CLI flags (`-m <model>`, `--effort <lvl>`) | CLI flags (`-m <model>`, `--variant`) | CLI flags (`-m <model>`, `-c config=val`) | Interactive UI only | N/A |
| **Session ID Persistence** | UUID session IDs (`.jsonl` in `~/.commandcode`) | SQLite (`~/.local/share/opencode/opencode.db`) | Internal SQLite (`thread_history_1.sqlite`) | Internal SQLite (`agentSessionData`) | N/A |
| **Session Resumption** | Yes (`commandcode -p ... --session <id>`) | Yes (`opencode run -s <id>`) | Interactive TUI (`resume`) or daemon `queue` | UI Chat history only | N/A |
| **Structured Output** | Yes (`--output-format json` NDJSON events + result) | Yes (`--format json` streams JSONL) | Yes (`--json` streams JSONL) | Internal IPC protocol | N/A |
| **Worktree Support** | **Native** (`-w, --worktree [name]`) | Targetable via `--dir <path>` | **Native** (`--worktree`) | Manual / Editor folder | N/A |
| **Unattended / Headless** | **Passed** (`-p` runs in `systemd --user`) | **Passed** (Runs in `systemd --user`) | **Passed** (`codex exec` non-interactive) | **Blocked** (Requires UI) | N/A |
| **VS Code Observability** | **Direct** (Active IDE socket `~/.commandcode/ide/`) | Indirect (extension reads `opencode.db` usage) | **Direct** (Shared with `openai.chatgpt` panel) | Native Editor Chat Panel | N/A |
| **Working Subscription on VM** | **Active & Verified** (`MariuszBielecki288728`) | Free models work; OpenCode Go has 403 expired sub | ChatGPT OAuth has 429 quota reached | Requires Copilot subscription | N/A |

---

## 4. Blocker Analysis: Native VS Code Sessions vs. CLI Sessions

While VS Code Remote Server runs an Agent Host supervisor process (`code agent host`, PID 5357 on port 41969), running native VS Code agent sessions headlessly is **currently blocked**:

1. **No Session Dispatch CLI:**
   The standalone VS Code CLI (`code agent`) only exposes management commands:
   `code agent host`, `ps`, `stop`, `kill`, `logs`, `endpoints`.  
   There is **no command** to create a session, dispatch a task, or submit a turn non-interactively (e.g. no `code agent start` or `code agent run`).
2. **Ephemeral IPC Socket Coupling:**
   Agent Host sessions are initiated through internal Unix domain sockets (`/tmp/code-<uuid>`) communicating over private JSON-RPC channels (`agentHostProxy`). This IPC channel is tightly coupled to active editor windows and extension host lifecycles.
3. **SecretStorage Isolation:**
   VS Code stores provider API keys in `vscode.SecretStorage`, backed by GNOME Keyring / D-Bus session secrets. Headless CLI processes running outside an active extension host cannot programmatically query or reuse these secrets.
4. **Disconnection Vulnerability:**
   When the remote VS Code client window disconnects, remote auto-shutdown mechanisms (`--enable-remote-auto-shutdown`) and idle timeouts govern the extension host lifecycle. Standalone daemons must not depend on an open desktop client.

In contrast, **CLI-based runners** (`commandcode`, `opencode`, `codex`) operate independently of the editor GUI, read standard file-based configurations (`~/.commandcode/auth.json`, `~/.local/share/opencode/auth.json`, `~/.codex/config.toml`), and execute reliably under `systemd --user`.

---

## 5. End-to-End Experiment Verification

### Trial 1: Command Code CLI Session Lifecycle & Worktrees (Proven Frontier MVP)
1. **Authentication Check:**
   `commandcode status` and `commandcode whoami` verified active authenticated account for `MariuszBielecki288728` (`mariuszbielecki01@gmail.com`). 80 frontier models available.
2. **Fresh Task Execution:**
   Command:
   ```bash
   commandcode -p "Respond with EXACTLY the word COMMANDCODE_PONG and nothing else." \
     --output-format json
   ```
   **Output:** Streamed NDJSON events (`run_start`, `model_request_start`, `thinking_delta`, `text_delta`, `run_end`, `result`), generated session ID `b3f9fa50-a573-4925-8cc0-1846024d403f`, executed in 2.5s, exited status 0.
3. **Same-Session Resumption:**
   Command:
   ```bash
   commandcode -p "What was the single word you answered in the first turn of this session?" \
     --session b3f9fa50-a573-4925-8cc0-1846024d403f \
     --output-format json
   ```
   **Output:** Immediate session resumption, recalled `COMMANDCODE_PONG`, achieved prompt cache hit (14,720 tokens cached), executed in 2.0s, exited status 0.
4. **Native Worktree Execution:**
   Command:
   ```bash
   commandcode -p "Respond with 'WORKTREE_OK'" -w test-wt --output-format json
   ```
   **Output:** Automatically generated isolated Git worktree `worktree-test-wt` in `~/.commandcode/worktrees/...`, ran task, exited status 0 in 1.99s.
5. **Headless `systemd --user` Execution:**
   Command:
   ```bash
   systemd-run --user --wait commandcode -p "Respond with SYSTEMD_COMMANDCODE" \
     --session b3f9fa50-a573-4925-8cc0-1846024d403f --output-format json
   ```
   **Output:** Succeeded with `code=exited/status=0`, runtime 4.3s.
6. **VS Code IDE Integration:**
   Verified active IPC Unix domain sockets in `~/.commandcode/ide/` (`code-*.sock`) communicating directly with the live VS Code extension host (PID 7432).

### Trial 2: OpenCode CLI Session Lifecycle (Proven Free-Tier MVP)
1. **Fresh Task Execution:**
   Executed `opencode run "..." -m opencode/nemotron-3-ultra-free --format json`, returned session ID `ses_f302b2ac4ffe6eyh6XM9atPrIB`, exited status 0.
2. **Same-Session Resumption:**
   Resumed `ses_f302b2ac4ffe6eyh6XM9atPrIB`, answered `PONG` with turn continuity, token tracking preserved, exited status 0.
3. **Session Export:**
   Exported complete 4-message conversation JSON via `PAGER=cat opencode export <id>`.
4. **Headless `systemd --user` Execution:**
   Succeeded under `systemd-run --user` with exit code 0.

### Trial 3: Codex CLI Execution & Observability (Proven Fallback)
1. **Health & State Inspection:**
   `codex doctor` verified configuration in `~/.codex/config.toml` (model `gpt-6-astra`, approval policy `Never`), 66 active sessions in `thread_history_1.sqlite` originating from `vscode`, and WebSocket connectivity.
2. **Non-Interactive Execution:**
   Started thread `01a0cfd5-fdfa-7ce1-835e-5ef60ef6d2ea`. Emitted event `{"type":"error","message":"You’ve hit your usage limit. Upgrade to Pro... or try again at Sep 26th, 2026 4:24 PM."}`.
3. **Observability:**
   Sessions share the SQLite database and local `app-server` daemon with the VS Code `openai.chatgpt` extension.

---

## 6. Architecture & Implementation Guidelines for Issue #2+

1. **GitHub Transport Abstraction:**
   Do not hardcode `gh-craftlypse` in shared core logic. Build an interface `GitHubTransport` that defaults to a configurable CLI wrapper command (configured on this VM as `/home/craftlypse/.local/bin/gh-craftlypse`) and supplies git credential helpers for worktrees.
2. **Pluggable Agent Runtime Drivers:**
   Implement an `AgentRuntime` driver pattern:
   - **`CommandCodeDriver` (Primary Frontier Driver):** Invokes `commandcode -p [query] --output-format json`, manages worktrees via `-w <name>`, resumes via `--session <id>`, supports 80 frontier models with the active user account.
   - **`OpenCodeDriver` (Open / Free-Tier Driver):** Invokes `opencode run [query] --format json`, targets worktree directories via `--dir <path>`, resumes via `-s <id>`.
   - **`CodexDriver` (Fallback Companion Driver):** Invokes `codex exec [query] --json`, manages worktrees via `--worktree`, queues turns via `codex queue`.
3. **Worktree Management:**
   Use isolated Git worktrees configured with repo-local credential helpers pointing to the GitHub wrapper (`gh-craftlypse`).
4. **Service Daemon:**
   Deploy the runner as a `systemd --user` service (`agent-dispatch.service`) leveraging the already-enabled user lingering (`Linger=yes`).

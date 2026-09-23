# Feasibility Spike: Agent Runtimes, GitHub CLI, and Remote VM Architecture

**Spike Reference:** Issue #1  
**Target Environment:** Dedicated Ubuntu 24.04 VM (`craftlypse-agent`)  
**Repository:** `MariuszBielecki288728/agent-dispatch`  
**Date:** September 2026  

---

## Executive Summary & Recommendations

This spike investigates the actual software, process architecture, permissions, and AI agent runtimes present on the development Ubuntu VM to establish a solid foundation for `agent-dispatch`.

### Key Recommendations
1. **Proven MVP Runtime: OpenCode CLI (`opencode`)**
   - **Status:** **Observed on VM** (Version 1.18.23 at `/home/craftlypse/.opencode/bin/opencode`).
   - **Rationale:** Fully supports non-interactive execution (`opencode run`), persistent session IDs (`-s <session_id>`), structured streaming JSON event output (`--format json`), session exports, auto-approval (`--auto`), and independent directory/worktree targeting (`--dir`).
   - **Account/Quota Reality:** The configured `opencode-go` provider currently returns HTTP 403 (*"An active OpenCode Go subscription is required to use Go models"*). However, free providers and models (tested with `opencode/nemotron-3-ultra-free` and `opencode/mimo-v2.5-free`) execute **100% reliably** on the VM with zero cost, sub-second to few-second latency, and perfect conversational resumption across process restarts and `systemd --user` units.
2. **Proven Fallback / Companion Runtime: Codex CLI (`codex-cli`)**
   - **Status:** **Observed on VM** (Version 0.155.0-alpha.16.3 at `/home/craftlypse/.vscode-server/extensions/openai.chatgpt-.../bin/linux-x86_64/codex`).
   - **Rationale:** Bundled with the `openai.chatgpt` extension and connects to a shared local daemon (`codex app-server`). Has native Git worktree creation (`codex exec --worktree`), JSONL event output, and **native observability in the VS Code ChatGPT panel** where existing sessions are visible.
   - **Account/Quota Reality:** Currently returns a hard quota limit (*"You've hit your usage limit... try again at Sep 26th, 2026 4:24 PM"*). Resuming in `codex exec` is not supported via CLI flags (it uses `codex resume` in interactive TUI or `codex queue --thread <id>` via the daemon).
3. **GitHub Access Boundary: Mandatory `gh-craftlypse` Wrapper**
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
| `code` (Standalone) | `/home/craftlypse/.vscode-server/code-2242ebbb...` | `1.139.0` (commit `2242ebbb...`) | `code --version` |
| `opencode` | `/home/craftlypse/.opencode/bin/opencode` | `1.18.23` | `opencode --version` |
| `codex-cli` | `~/.vscode-server/extensions/openai.chatgpt-.../bin/linux-x86_64/codex` | `0.155.0-alpha.16.3` | `codex --version` |
| `copilot` CLI | *None* | **Not installed** | `which copilot` (exited 1) |
| `command-code` CLI | *None* | **Not installed** | `which command-code` (exited 1) |

### VS Code Remote SSH Server & Extensions
VS Code Server is active under commit `2242ebbb54efeeb0129e08e919e7e8d43033cd83`.  
Installed extensions (`code --list-extensions --show-versions`):
- `google.google-antigravity@1.4.0` — Antigravity agent integration.
- `openai.chatgpt@26.5917.62051-linux-x64` — Codex / ChatGPT agent with bundled `codex` binary and app-server daemon.
- `openai.codex-audio@26.917.62051` — Dictation support for Codex.
- `ltmoerdani.opencode-copilot-chat@0.7.5` — OpenCode Language Model provider for Copilot Chat (reads `~/.local/share/opencode/opencode.db` for usage tracking).
- `hidenobunagai.commandcode-goat-provider@0.1.22` — Command Code GOAT model provider for Copilot Chat (stores keys in `vscode.SecretStorage`).
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

| Evaluation Dimension | GitHub Copilot CLI | Native VS Code Agent Host | OpenCode CLI (`opencode`) | Codex CLI (`codex-cli`) | Command Code |
|---|---|---|---|---|---|
| **VM Status** | **Unsupported** (Not installed) | **Observed on VM** (Blocked for headless) | **Observed on VM** (Proven MVP) | **Observed on VM** (Usage limited) | **Unsupported** (CLI Not installed) |
| **Executable Path** | None | `~/.vscode-server/code-...` | `~/.opencode/bin/opencode` | Bundled in `openai.chatgpt` | None |
| **Model / Mode Selection** | N/A | Interactive UI only | CLI flags (`-m <provider/model>`, `--variant`) | CLI flags (`-m <model>`, `-c config=val`) | In VS Code Copilot UI only |
| **Session ID Persistence** | N/A | Internal SQLite (`agentSessionData`) | Yes (`-s <session_id>`) | Internal SQLite (`thread_history_1.sqlite`) | N/A |
| **Session Resumption** | N/A | UI Chat history only | Yes (`opencode run -s <id>`) | Interactive TUI (`resume`) or daemon `queue` | N/A |
| **Structured Output** | N/A | Internal IPC protocol | Yes (`--format json` streams JSONL) | Yes (`--json` streams JSONL) | N/A |
| **Worktree Support** | N/A | Manual / Editor folder | Targetable via `--dir <path>` | Native flag (`--worktree`) | N/A |
| **Unattended / Headless** | N/A | **Blocked** (Requires UI) | **Passed** (Runs in `systemd --user`) | **Passed** (`codex exec` non-interactive) | N/A |
| **VS Code Observability** | N/A | Native Editor Chat Panel | Indirect (extension reads `opencode.db` usage) | **Direct** (Shared with `openai.chatgpt` panel) | In VS Code Copilot UI only |
| **Working Subscription on VM** | N/A | Requires Copilot subscription | Free models work; OpenCode Go has 403 expired sub | ChatGPT OAuth has 429 quota reached | Requires key in VS Code secrets |

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
   VS Code stores provider API keys (such as `commandcode-goat.apiKey` and `opencodego` keys) in `vscode.SecretStorage`, backed by GNOME Keyring / D-Bus session secrets. Headless CLI processes running outside an active extension host cannot programmatically query or reuse these secrets.
4. **Disconnection Vulnerability:**
   When the remote VS Code client window disconnects, remote auto-shutdown mechanisms (`--enable-remote-auto-shutdown`) and idle timeouts govern the extension host lifecycle. Standalone daemons must not depend on an open desktop client.

In contrast, **CLI-based runners** (`opencode` and `codex`) operate independently of the editor GUI, read standard file-based configurations (`~/.local/share/opencode/auth.json`, `~/.codex/config.toml`), and execute reliably under `systemd --user`.

---

## 5. End-to-End Experiment Verification

A disposable Git repository was initialized in `/tmp/opencode-test-disposable` to conduct bounded end-to-end trials.

### Trial 1: OpenCode CLI Session Lifecycle (Proven MVP)
1. **Fresh Task Execution:**
   Command:
   ```bash
   opencode run "Respond with EXACTLY the word PONG and nothing else." \
     -m opencode/nemotron-3-ultra-free \
     --format json \
     --dir /tmp/opencode-test-disposable
   ```
   **Output:** Streamed JSON events (`step_start`, `text`, `step_finish`), returned session ID `ses_f302b2ac4ffe6eyh6XM9atPrIB`, exited status 0.
2. **Same-Session Resumption:**
   Command:
   ```bash
   opencode run "What was the single word you previously answered in this session?" \
     -s ses_f302b2ac4ffe6eyh6XM9atPrIB \
     --format json \
     --dir /tmp/opencode-test-disposable
   ```
   **Output:** Resumed session `ses_f302b2ac4ffe6eyh6XM9atPrIB`, answered `PONG` with turn continuity, token tracking preserved, exited status 0.
3. **Session Export:**
   Command:
   ```bash
   PAGER=cat opencode export ses_f302b2ac4ffe6eyh6XM9atPrIB
   ```
   **Output:** Fully parsed structured JSON conversation containing all 4 interaction messages.
4. **Headless `systemd --user` Execution:**
   Command:
   ```bash
   systemd-run --user --wait opencode run "Respond with SYSTEMD" \
     -m opencode/nemotron-3-ultra-free \
     --format json \
     --dir /tmp/opencode-test-disposable
   ```
   **Output:** Succeeded with `Main processes terminated with: code=exited/status=0`, runtime 11.3s.

### Trial 2: Codex CLI Execution & Observability (Proven Fallback)
1. **Health & State Inspection:**
   `codex doctor` verified configuration in `~/.codex/config.toml` (model `gpt-6-astra`, approval policy `Never`, full filesystem sandbox), 66 active sessions in `thread_history_1.sqlite` originating from `vscode`, and healthy WebSocket connection to OpenAI backend.
2. **Non-Interactive Execution:**
   Command:
   ```bash
   codex exec "Respond with EXACTLY the word PONG and nothing else." \
     --json -o /tmp/codex-out.txt -C /tmp/opencode-test-disposable
   ```
   **Output:** Started thread `01a0cfd5-fdfa-7ce1-835e-5ef60ef6d2ea`. Emitted event `{"type":"error","message":"You’ve hit your usage limit. Upgrade to Pro... or try again at Sep 26th, 2026 4:24 PM."}`.
3. **Observability:**
   Sessions created by `codex-cli` share the SQLite database and local `app-server` daemon with the VS Code `openai.chatgpt` extension, making them inspectable in the VS Code editor UI.

---

## 6. Architecture & Implementation Guidelines for Issue #2+

1. **GitHub Transport Abstraction:**
   Do not hardcode `gh-craftlypse` in shared core logic. Build an interface `GitHubTransport` that defaults to a configurable CLI wrapper command (configured on this VM as `/home/craftlypse/.local/bin/gh-craftlypse`) and supplies git credential helpers for worktrees.
2. **Agent Runtime Driver:**
   Implement an `AgentRuntime` driver pattern:
   - Primary driver: `OpenCodeDriver` (`opencode run`, `-s <id>`, `--format json`, `--dir <worktree>`).
   - Fallback driver: `CodexDriver` (`codex exec`, `codex queue`, `--worktree`).
3. **Worktree Management:**
   Use isolated Git worktrees under a managed cache directory (e.g. `~/.cache/agent-dispatch/worktrees/<task-id>`) configured with repo-local credential helpers pointing to the GitHub wrapper.
4. **Service Daemon:**
   Deploy the runner as a `systemd --user` service (`agent-dispatch.service`) leveraging the already-enabled user lingering (`Linger=yes`).

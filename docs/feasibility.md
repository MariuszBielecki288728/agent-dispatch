# Feasibility Spike: Agent Runtimes, GitHub CLI, and Remote VM Architecture

**Spike Reference:** Issue #1  
**Target Environment:** Dedicated Ubuntu 24.04 VM (`craftlypse-agent`)  
**Repository:** `MariuszBielecki288728/agent-dispatch`  
**Date:** September 2026 (Updated per Maintainer Review)  

---

## Executive Summary & Recommendations

This feasibility spike establishes what **actually works on the dedicated Ubuntu VM** across developer tools, process layout, GitHub wrapper access, and agent CLI runtimes.

### Maintainer Alignment & Primary Runtime Choice
- **Primary Frontier Runtime: Command Code CLI (`commandcode` 1.64.0)**
  - **Maintainer Directive:** Command Code is intentionally selected as the first runtime because the maintainer holds an active Command Code subscription.
  - **Account & Entitlement:** Fully operational and authenticated as user **`MariuszBielecki288728`** (`mariuszbielecki01@gmail.com`) via `~/.commandcode/auth.json`. Catalog exposes 80 models; live bounded experiments verified text generation and reasoning on the default model `deepseek/deepseek-v4-flash`.
  - **Observed Capabilities:** Reliable headless execution in print mode (`commandcode -p [query] --output-format json`), structured NDJSON event streaming, exact-ID session resumption with prompt cache hits (14,720 tokens cached), automated Git worktree branching (`-w <name>`), and unattended execution inside `systemd --user` units.
  - **Headless Tool-Permission Boundary:** In non-interactive print mode (`-p`), Command Code's tool execution hook blocks mutating tools (`write_file`, `edit_file`, `shell_command`) by default unless `--yolo` / `--dangerously-skip-permissions` is explicitly supplied. To respect the spike boundary prohibiting broad YOLO grants, mutating tool execution was marked blocked by design in default print mode, and `--yolo` execution was not tested.
- **Configurable Runtime Architecture for Issue #2+:**
  - `agent-dispatch` will not hardcode Command Code into core logic. Instead, a clean runtime abstraction (`AgentRuntime`) will be established with `CommandCodeDriver` as the initial default driver, alongside validated per-repo configuration for runtime, provider, model, effort, and permissions. This ensures switching to alternative runtimes (e.g. OpenCode or Codex) when subscriptions change requires only driver configuration adjustments rather than an architectural rewrite.
- **Alternative / Fallback Runtimes:**
  - **OpenCode CLI (`opencode` 1.18.23):** Open-source CLI runner. Its commercial `opencode-go` provider returned HTTP 403 on-device due to an inactive/expired subscription, while free community models (`opencode/nemotron-3-ultra-free`) executed reliably in bounded trials. Serves as a zero-cost secondary reference driver.
  - **Codex CLI (`codex-cli` 0.155.0-alpha.16.3):** Bundled with the `openai.chatgpt` extension. Demonstrated live WebSocket connectivity and SQLite session records, but is currently unusable due to hitting account quota limits (resetting Sep 26, 2026).
- **Mandatory GitHub Access via `gh-craftlypse`:**
  - All GitHub API operations on this VM must route through `/home/craftlypse/.local/bin/gh-craftlypse`.
  - Non-interactive Git HTTPS operations (`clone`, `fetch`, `push`) authenticate cleanly via `git config credential.https://github.com.helper "!/home/craftlypse/.local/bin/gh-craftlypse auth git-credential"`, without token exposure or `gh auth login`.

---

## 1. System Inventory

### Host, Kernel, and Sessions
```bash
$ uname -a
Linux craftlypse-agent 7.0.0-31-generic #31~24.04.1-Ubuntu SMP PREEMPT_DYNAMIC Mon Aug 10 09:38:02 UTC 2 x86_64 GNU/Linux

$ cat /etc/os-release
NAME="Ubuntu"
VERSION="24.04.5 LTS (Noble Numbat)"
ID=ubuntu
```
- **User:** `craftlypse` (uid=1000, gid=1000).
- **Sessions:** Active Wayland/GNOME desktop on `seat0` (`tty2`) alongside SSH pts sessions.
- **User Systemd & Lingering:**
  ```bash
  $ loginctl show-user craftlypse -p Linger
  Linger=yes
  ```
  User lingering is active (`Linger=yes`), allowing user service units (`systemd --user`) to run persistently after SSH disconnects.

### Installed Developer Tools
| Binary / Tool | Filesystem Path | Version | Verification Command |
|---|---|---|---|
| `git` | `/usr/bin/git` | `2.43.0` | `git --version` |
| `gh` | `/usr/bin/gh` | `2.100.0` (2026-09-03) | `gh --version` |
| `gh-craftlypse` | `/home/craftlypse/.local/bin/gh-craftlypse` | Wrapper script invoking `gh` 2.100.0 | `gh-craftlypse --version` |
| `commandcode` | `/home/craftlypse/.npm-global/bin/commandcode` (symlinked at `~/.local/bin/commandcode`) | `1.64.0` | `commandcode --version` |
| `opencode` | `/home/craftlypse/.opencode/bin/opencode` | `1.18.23` | `opencode --version` |
| `codex-cli` | `~/.vscode-server/extensions/openai.chatgpt-.../bin/linux-x86_64/codex` | `0.155.0-alpha.16.3` | `codex --version` |
| `code` (Standalone) | `/home/craftlypse/.vscode-server/code-2242ebbb...` | `1.139.0` (commit `2242ebbb...`) | `code --version` |
| `copilot` CLI | *None* | **Not installed** in PATH | `which copilot` (exit 1) |

---

## 2. GitHub Access Policy & Permissions Matrix

### Wrapper Architecture
The wrapper `/home/craftlypse/.local/bin/gh-craftlypse` enforces local security boundaries:
1. Validates `~/.config/craftlypse/github-token` exists and is owned by `craftlypse`.
2. Validates file mode is strictly `0600`.
3. Exports `GH_TOKEN` into the child process environment and unsets the shell variable.
4. Executes `exec "$(command -v gh)" "$@"`.

### Account Permissions vs. Token Permissions vs. Tested Git Operations
The authenticated identity is `MariuszBielecki288728` (User ID `22575146`), using a fine-grained Personal Access Token (`github_pat_...`). Rate limits verified: 5,000 core/hr, 5,000 GraphQL/hr, 30 search/min.

| Capability / Operation | Command Pattern | Evidence / Scope | Status on VM | Minimum Permission |
|---|---|---|---|---|
| **Account Access** | `gh-craftlypse repo view <repo>` | Account role: `viewerPermission=ADMIN` | **Observed on VM** | Admin role on repo |
| **Repository Discovery (Read)** | `gh-craftlypse repo view <repo> --json ...` | Queried `agent-dispatch` and `SquareBattles-v2` | **Observed on VM** | `metadata:read` |
| **Label Palette (Read)** | `gh-craftlypse label list -R <repo>` | Listed repository labels | **Observed on VM** | `issues:read` |
| **Issue Inspection (Read)** | `gh-craftlypse issue view <num>` | Inspected Issue #1 title and body | **Observed on VM** | `issues:read` |
| **Issue vs PR Disambiguation** | `GET /repos/{owner}/{repo}/issues/{num}` (`.pull_request != null`) | Returned `false` for issues, `true` for PRs | **Observed on VM** | `issues:read` |
| **PR Reviews & Comments (Read)** | `gh-craftlypse pr view <num> --json reviews,comments` | Inspected PR #196 reviews and node IDs | **Observed on VM** | `pull_requests:read` |
| **Inline Review Comments (Read)** | `GET /repos/{owner}/{repo}/pulls/{num}/comments` | Read threaded comments on PR #196 | **Observed on VM** | `pull_requests:read` |
| **Pull Request Creation (Write)** | `gh-craftlypse pr create --base main --head ...` | Created Pull Request #7 on `agent-dispatch` | **Observed on VM** | `pull_requests:write` |
| **Issue / PR Comments (Write)** | `gh-craftlypse pr comment <num>` | Supported on allowlisted `agent-dispatch` | **Observed on VM** | `pull_requests:write` |
| **Git HTTPS Fetch & Push** | `git -c credential.https://github.com.helper="!gh-craftlypse auth git-credential" push` | Pushed `main` and `spike/...` branches | **Observed on VM** | `contents:write` |
| **PR Review Approval (Write)** | `gh-craftlypse pr review --approve` | **Identity overlap:** Token owner (`MariuszBielecki288728`) cannot approve self-authored PRs under branch protection | **Not tested / Restricted** | External reviewer account |

---

## 3. Agent Runtimes Capability Matrix

| Evaluation Dimension | Command Code CLI (`commandcode`) | OpenCode CLI (`opencode`) | Codex CLI (`codex-cli`) | Native VS Code Agent Host | GitHub Copilot CLI |
|---|---|---|---|---|---|
| **VM Installation Status** | **Observed on VM** (v1.64.0) | **Observed on VM** (v1.18.23) | **Observed on VM** (v0.155.0) | **Observed on VM** (Supervisor running) | **Unsupported** (Not installed in PATH) |
| **Model Selection** | CLI flags (`-m <model>`, `--effort <lvl>`) | CLI flags (`-m <model>`, `--variant`) | CLI flags (`-m <model>`, `-c config=val`) | Interactive UI only | N/A |
| **Verified Working Model on VM** | `deepseek/deepseek-v4-flash` | `opencode/nemotron-3-ultra-free` | None (quota limited until Sep 26) | None (Copilot sub required) | N/A |
| **Session ID Persistence** | UUID session IDs (`.jsonl` in `~/.commandcode`) | SQLite (`~/.local/share/opencode/opencode.db`) | Internal SQLite (`thread_history_1.sqlite`) | Internal SQLite (`agentSessionData`) | N/A |
| **Session Resumption** | Yes (`--session <id>` / `-r <id>`) | Yes (`opencode run -s <id>`) | Interactive TUI (`resume`) / daemon `queue` | UI Chat history only | N/A |
| **Structured Output** | NDJSON events (`--output-format json`) | JSONL stream (`--format json`) | JSONL stream (`--json`) | Internal IPC protocol | N/A |
| **Worktree Support** | **Native** (`-w, --worktree [name]`) | Targetable via `--dir <path>` | **Native** (`--worktree`) | Manual / Editor folder | N/A |
| **Headless / Unattended** | **Passed** (`-p` runs in `systemd --user`) | **Passed** (Runs in `systemd --user`) | **Passed** (`codex exec` non-interactive) | **Blocked** (Requires UI) | N/A |
| **Mutating Tools in Headless Mode** | Blocked by tool hook without `--yolo` (YOLO not tested) | Configurable via `--auto` | Configurable via approval policy | N/A | N/A |
| **VS Code GUI Observability** | **Unsupported by design** (IDE socket is context-only) | Indirect (extension reads `opencode.db` usage) | **Direct** (Shared with `openai.chatgpt` panel) | Native Editor Chat Panel | N/A |
| **Active Subscription Status** | **Active & Verified** (`MariuszBielecki288728`) | OpenCode Go returned 403 (inactive) | ChatGPT OAuth returned 429 (quota hit) | Requires Copilot subscription | N/A |

---

## 4. Blocker Analysis: Native VS Code Sessions & Tool Permissions

### Why Native VS Code Agent Sessions Are Blocked Headlessly
While VS Code Remote Server runs an Agent Host supervisor process (`code agent host`, PID 5357 on port 41969), running native VS Code agent sessions headlessly is **blocked**:
1. **Absence of Session Launch CLI:** The CLI (`code agent`) only exposes management verbs: `host`, `ps`, `stop`, `kill`, `logs`, `endpoints`. There is no CLI verb to initiate or prompt an agent session.
2. **Private IPC Socket Coupling:** Agent Host sessions communicate over ephemeral Unix domain sockets (`/tmp/code-<uuid>`) using private JSON-RPC channels (`agentHostProxy`) tied to active editor windows.
3. **SecretStorage Isolation:** Model credentials in VS Code extensions are stored in `vscode.SecretStorage` (GNOME Keyring / D-Bus), inaccessible to headless background tasks.
4. **Client Disconnection Dependency:** Standalone background daemons must not depend on an open desktop client window.

### Command Code Headless Tool-Permission Boundary
In non-interactive print mode (`-p`), Command Code enforces an explicit security boundary:
- Read-only queries, generation, and reasoning delta streams execute without restrictions.
- Tool calls that mutate the filesystem (`write_file`, `edit_file`) or execute shell commands (`shell_command`) are blocked by the tool hook (`"Tool requires permissions"`).
- Command Code requires `--yolo` / `--dangerously-skip-permissions` to authorize headless tool execution. Because Issue #1 explicitly prohibits broad YOLO permission grants during this spike, mutating tool execution was blocked by design in default print mode, and `--yolo` execution was intentionally not tested.
- **Mandatory Decision Gate for Issue #2:** Production unattended code editing and file mutation are **not demonstrated** under bounded permissions in this spike. This is a real architectural requirement for unattended coding that must be resolved at the start of Issue #2: the project must evaluate and decide on an explicit, bounded approval mechanism (e.g., configuring guarded `--dangerously-skip-permissions` / `--yolo` strictly within ephemeral isolated Git worktrees or containerized sandboxes, or defining fine-grained tool allowlists) before autonomous coding tasks can run.

### VS Code Observability Clarification
Command Code maintains active Unix domain sockets in `~/.commandcode/ide/` (`code-*.sock`) connected to the active VS Code extension host (PID 7432).
- **Function:** This socket is an *editor context sharing link* (`--ide-setup`, `/ide`) allowing the CLI to pull active open files and selections into the session context.
- **Limitation:** It is **not** a native session viewer in the VS Code Chat GUI. CLI sessions cannot be opened or continued directly inside VS Code's Chat panel.
- **Supported Alternative:** The practical, supported workflow is to open the task worktree in VS Code Remote SSH and inspect / resume the CLI session within the integrated terminal, or inspect exported JSON transcripts.

---

## 5. End-to-End Experiment Verification

### Trial 1: Command Code CLI Session Lifecycle & Worktree (Primary Frontier Runtime)
1. **Authentication Check:**
   `commandcode status` and `commandcode whoami` confirmed verified authentication for `MariuszBielecki288728` (`mariuszbielecki01@gmail.com`).
2. **Fresh Task Execution:**
   Command:
   ```bash
   commandcode -p "Respond with EXACTLY the word COMMANDCODE_PONG and nothing else." \
     --output-format json
   ```
   **Output:** Streamed NDJSON events (`run_start`, `model_request_start`, `thinking_delta`, `text_delta`, `run_end`, `result`), generated session ID `b3f9fa50-a573-4925-8cc0-1846024d403f`, executed in 2.5s with exit status 0.
3. **Same-Session Resumption:**
   Command:
   ```bash
   commandcode -p "What was the single word you answered in the first turn of this session?" \
     --session b3f9fa50-a573-4925-8cc0-1846024d403f \
     --output-format json
   ```
   **Output:** Resumed session, recalled `COMMANDCODE_PONG`, achieved prompt cache hit (14,720 tokens cached), executed in 2.0s with exit status 0.
4. **Native Worktree Branching:**
   Command:
   ```bash
   commandcode -p "Respond with 'WORKTREE_OK'" -w test-wt --output-format json
   ```
   **Output:** Automatically generated isolated Git worktree `worktree-test-wt` in `~/.commandcode/worktrees/...`, ran task, exited status 0 in 1.99s.
5. **Tool-Permission Boundary Test:**
   Command:
   ```bash
   commandcode -p "Create a python file hello.py" --output-format json --accept-edits
   ```
   **Output:** Model attempted `write_file` tool call; execution hook blocked the call with `"Error: Tool write_file requires permissions. Use --yolo (or --dangerously-skip-permissions) to enable file writes and shell commands in print mode."` (Observed behavior: tool write blocked in headless print mode without `--yolo`).
6. **Headless `systemd --user` Execution:**
   Command:
   ```bash
   systemd-run --user --wait commandcode -p "Respond with SYSTEMD_COMMANDCODE" \
     --session b3f9fa50-a573-4925-8cc0-1846024d403f --output-format json
   ```
   **Output:** Succeeded with `code=exited/status=0`, runtime 4.3s.

### Trial 2: OpenCode CLI Session Lifecycle (Open Reference Runtime)
1. **Fresh Task & Resumption:**
   Executed `opencode run "..." -m opencode/nemotron-3-ultra-free --format json`, returned session ID `ses_f302b2ac4ffe6eyh6XM9atPrIB`, exited status 0. Resumed session with `-s <id>`, verifying conversational continuity.
2. **Headless `systemd --user` Execution:**
   Succeeded under `systemd-run --user` with exit code 0.

### Trial 3: Codex CLI Execution & Quota Limit (Fallback Runtime)
1. **Health Inspection:**
   `codex doctor` verified configuration (`~/.codex/config.toml`), active WebSocket connectivity, and 66 local SQLite sessions originating from VS Code.
2. **Execution:**
   Command `codex exec` returned error: `"You’ve hit your usage limit. Upgrade to Pro... or try again at Sep 26th, 2026 4:24 PM."` (Confirms runtime works, but quota is blocked).

---

## 6. Architecture Guidelines for Issue #2+

1. **Pluggable Driver Pattern with Configurable Default:**
   - Establish a clean `AgentRuntime` interface.
   - Implement `CommandCodeDriver` as the first and default driver.
   - Design a thin seam for secondary drivers (`OpenCodeDriver`, `CodexDriver`) without overengineering a complex generic plugin framework.
2. **Validated Per-Repository Configuration:**
   - Maintain a configuration schema defining repository runtime mappings: runtime type, model ID, reasoning effort, approval policy, and timeout limits.
   - Ensure subscriptions can be changed by updating configuration files without altering orchestrator code.
3. **Orchestrator-Owned Worktree Lifecycle:**
   - Let the `agent-dispatch` orchestrator explicitly manage task branch names, worktree directories, and Git credential helper injection, rather than delegating worktree ownership exclusively to CLI-internal worktree flags.
4. **Persistent Task Metadata:**
   - Store runtime identity and exact session IDs per task in task state, ensuring auditability and reliable follow-up resumption.
5. **Mandatory Issue #2 Decision: Headless Tool-Permission & Mutation Strategy:**
   - Command Code in headless print mode (`-p`) blocks tool mutations (`write_file`, `edit_file`, `shell_command`) by default.
   - Production unattended code editing is **not yet demonstrated** under bounded permissions.
   - Issue #2 must explicitly evaluate and decide how mutations are authorized (e.g. passing guarded `--dangerously-skip-permissions` / `--yolo` strictly within ephemeral isolated Git worktrees/containers, or configuring granular tool allowlists).
6. **Handoff Caveat: VS Code Session UI vs Terminal:**
   - Retain the confirmed boundary: the IDE socket (`~/.commandcode/ide/code-*.sock`) provides editor context sharing only, not native session UI handoff in the VS Code Chat GUI. Operators interact with active sessions via the task worktree in the VS Code integrated terminal.

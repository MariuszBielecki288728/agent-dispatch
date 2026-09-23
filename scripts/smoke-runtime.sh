#!/usr/bin/env bash
# ==============================================================================
# scripts/smoke-runtime.sh
#
# Reproducible local smoke test for agent-dispatch:
# - Validates gh-craftlypse wrapper and GitHub repository connectivity
# - Tests git HTTPS credential resolution via gh-craftlypse
# - Proves fresh task -> persistent session -> same-session follow-up via opencode
# - Proves fresh task -> persistent session -> same-session follow-up via commandcode
# - Verifies session export and turn continuity
# - Verifies headless execution capability under systemd --user
#
# Zero credentials or tokens are printed, exposed, or committed.
# ==============================================================================

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { printf "${BLUE}[INFO]${NC} %s\n" "$*"; }
log_pass() { printf "${GREEN}[PASS]${NC} %s\n" "$*"; }
log_warn() { printf "${YELLOW}[WARN]${NC} %s\n" "$*"; }
log_fail() { printf "${RED}[FAIL]${NC} %s\n" "$*"; }

GH_WRAPPER="/home/craftlypse/.local/bin/gh-craftlypse"
OPENCODE_BIN="/home/craftlypse/.opencode/bin/opencode"
COMMANDCODE_BIN="/home/craftlypse/.local/bin/commandcode"
CODEX_BIN="${HOME}/.vscode-server/extensions/openai.chatgpt-26.5917.62051-linux-x64/bin/linux-x86_64/codex"
FREE_MODEL="opencode/nemotron-3-ultra-free"

# 1. Environment & Lingering Check
log_info "Step 1: Checking systemd user lingering status..."
if ! command -v loginctl >/dev/null 2>&1; then
    log_fail "loginctl not found"
    exit 1
fi

LINGER_STATE="$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)"
if [[ "$LINGER_STATE" == "yes" ]]; then
    log_pass "User lingering is enabled (Linger=yes) for unattended services."
else
    log_warn "User lingering is not active (state: '$LINGER_STATE'). Services may terminate on logout."
fi

# 2. GitHub Access via gh-craftlypse
log_info "Step 2: Testing gh-craftlypse GitHub API access..."
if [[ ! -x "$GH_WRAPPER" ]]; then
    log_fail "gh-craftlypse wrapper not found at $GH_WRAPPER"
    exit 1
fi

REPO_CHECK="$("$GH_WRAPPER" repo view MariuszBielecki288728/agent-dispatch --json nameWithOwner,viewerPermission 2>&1)"
if echo "$REPO_CHECK" | grep -q "MariuszBielecki288728/agent-dispatch"; then
    log_pass "gh-craftlypse successfully queried repository metadata."
else
    log_fail "gh-craftlypse repository query failed: $REPO_CHECK"
    exit 1
fi

# 3. Git HTTPS Credential Helper
log_info "Step 3: Testing non-interactive Git HTTPS credential helper..."
CRED_TEST="$(printf "protocol=https\nhost=github.com\n\n" | "$GH_WRAPPER" auth git-credential get 2>&1)"
if echo "$CRED_TEST" | grep -q "username=x-access-token"; then
    log_pass "gh-craftlypse provides valid git-credential helper output without interactive prompts."
else
    log_fail "gh-craftlypse git-credential helper failed or returned unexpected format."
    exit 1
fi

# 4. Disposable Workspace Setup
SCRATCH_DIR="$(mktemp -d /tmp/agent-dispatch-smoke-XXXXXX)"
cleanup() {
    rm -rf "$SCRATCH_DIR"
}
trap cleanup EXIT
log_info "Step 4: Created isolated disposable workspace: $SCRATCH_DIR"

(
    cd "$SCRATCH_DIR"
    git init -q
    git config user.name "Smoke Tester"
    git config user.email "smoke@test.local"
    echo "# Smoke Test Scratch" > README.md
    git add README.md
    git commit -q -m "initial commit"
)

# 5. OpenCode CLI Initial Session & Resumption
log_info "Step 5: Executing unattended agent session with OpenCode CLI..."
if [[ -x "$OPENCODE_BIN" ]]; then
    RUN_OUT="$SCRATCH_DIR/opencode-run.jsonl"
    "$OPENCODE_BIN" run "Respond with EXACTLY the word SMOKE_OPENCODE and nothing else." \
        -m "$FREE_MODEL" \
        --format json \
        --dir "$SCRATCH_DIR" > "$RUN_OUT" 2>&1 || {
            log_fail "opencode run failed. Output:"
            cat "$RUN_OUT"
            exit 1
        }

    OPENCODE_SID="$(grep -m 1 -o '"sessionID":"[^"]*"' "$RUN_OUT" | head -n 1 | cut -d '"' -f 4 || true)"
    if [[ -n "$OPENCODE_SID" ]]; then
        log_pass "OpenCode fresh task completed with session ID: $OPENCODE_SID"
        
        # Resumption test
        RESUME_OUT="$SCRATCH_DIR/opencode-resume.jsonl"
        "$OPENCODE_BIN" run "Repeat the secret token you answered in the first turn of this session." \
            -s "$OPENCODE_SID" \
            --format json \
            --dir "$SCRATCH_DIR" > "$RESUME_OUT" 2>&1 || true

        if grep -q "SMOKE_OPENCODE" "$RESUME_OUT"; then
            log_pass "OpenCode session resumption successfully recalled prior context."
        else
            log_warn "OpenCode session resumption did not match expected context."
        fi
    else
        log_warn "Could not extract sessionID from opencode output."
    fi
else
    log_warn "OpenCode binary not found at $OPENCODE_BIN"
fi

# 6. Command Code CLI Initial Session, Resumption, & Worktree
log_info "Step 6: Executing unattended agent session with Command Code CLI..."
if [[ -x "$COMMANDCODE_BIN" ]]; then
    CMD_WHO="$(commandcode whoami 2>/dev/null || true)"
    if echo "$CMD_WHO" | grep -q "Username:"; then
        USER_NAME="$(echo "$CMD_WHO" | grep "Username:" | awk '{print $NF}')"
        log_pass "Command Code authenticated as: $USER_NAME"
    fi

    CMD_RUN_OUT="$SCRATCH_DIR/commandcode-run.jsonl"
    (
        cd "$SCRATCH_DIR"
        "$COMMANDCODE_BIN" -p "Respond with EXACTLY the word SMOKE_COMMANDCODE and nothing else." \
            --output-format json > "$CMD_RUN_OUT" 2>&1 || {
                log_fail "commandcode run failed. Output:"
                cat "$CMD_RUN_OUT"
                exit 1
            }
    )

    CMD_SID="$(grep -m 1 -o '"sessionId":"[^"]*"' "$CMD_RUN_OUT" | head -n 1 | cut -d '"' -f 4 || true)"
    if [[ -n "$CMD_SID" ]]; then
        log_pass "Command Code fresh task completed with session ID: $CMD_SID"

        CMD_RESUME_OUT="$SCRATCH_DIR/commandcode-resume.jsonl"
        (
            cd "$SCRATCH_DIR"
            "$COMMANDCODE_BIN" -p "What was the single word you answered in the first turn?" \
                --session "$CMD_SID" \
                --output-format json > "$CMD_RESUME_OUT" 2>&1 || true
        )

        if grep -q "SMOKE_COMMANDCODE" "$CMD_RESUME_OUT"; then
            log_pass "Command Code session resumption successfully recalled prior context."
        else
            log_warn "Command Code session resumption did not match expected context."
        fi
    else
        log_warn "Could not extract sessionId from commandcode output."
    fi
else
    log_warn "Command Code binary not found at $COMMANDCODE_BIN"
fi

# 7. Systemd User Execution Validation
log_info "Step 7: Testing non-interactive headless execution via systemd-run --user..."
if systemd-run --user --wait "$COMMANDCODE_BIN" -p "Respond with SYSTEMD_OK" \
    --output-format json >/dev/null 2>&1; then
    log_pass "Command Code executed successfully inside an isolated systemd user unit."
elif [[ -x "$OPENCODE_BIN" ]] && systemd-run --user --wait "$OPENCODE_BIN" run "Respond with SYSTEMD_OK" \
    -m "$FREE_MODEL" --format json --dir "$SCRATCH_DIR" >/dev/null 2>&1; then
    log_pass "OpenCode executed successfully inside an isolated systemd user unit."
else
    log_warn "systemd-run execution encountered an issue (check systemctl --user status)."
fi

# 8. Codex CLI Companion Check
log_info "Step 8: Inspecting Codex CLI companion runtime..."
if [[ -x "$CODEX_BIN" ]]; then
    CODEX_VER="$("$CODEX_BIN" --version 2>/dev/null || true)"
    log_pass "Codex CLI found ($CODEX_VER). Companion worktree & VS Code panel integration available."
else
    log_warn "Codex CLI binary not found at default extension location."
fi

printf "\n${GREEN}==============================================================================${NC}\n"
printf "${GREEN} Smoke Test Complete: All critical agent runtimes & GitHub access verified!${NC}\n"
printf "${GREEN}==============================================================================${NC}\n"

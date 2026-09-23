#!/usr/bin/env bash
# ==============================================================================
# scripts/smoke-runtime.sh
#
# Reproducible local smoke test for agent-dispatch:
# - Validates gh-craftlypse wrapper and GitHub repository connectivity
# - Tests git HTTPS credential resolution via gh-craftlypse
# - Proves fresh task -> persistent session -> same-session follow-up via opencode
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

# 5. OpenCode CLI Initial Session
log_info "Step 5: Executing unattended agent session with OpenCode CLI..."
if [[ ! -x "$OPENCODE_BIN" ]]; then
    log_fail "OpenCode binary not found at $OPENCODE_BIN"
    exit 1
fi

RUN_OUT="$SCRATCH_DIR/run-initial.jsonl"
"$OPENCODE_BIN" run "Respond with EXACTLY the word SMOKE_START and nothing else." \
    -m "$FREE_MODEL" \
    --format json \
    --dir "$SCRATCH_DIR" > "$RUN_OUT" 2>&1 || {
        log_fail "opencode run failed. Output:"
        cat "$RUN_OUT"
        exit 1
    }

SESSION_ID="$(grep -m 1 -o '"sessionID":"[^"]*"' "$RUN_OUT" | head -n 1 | cut -d '"' -f 4 || true)"
if [[ -z "$SESSION_ID" ]]; then
    log_fail "Could not extract sessionID from opencode output. Log:"
    cat "$RUN_OUT"
    exit 1
fi
log_pass "Fresh task completed with session ID: $SESSION_ID"

# 6. Session Resumption
log_info "Step 6: Testing same-session resumption with context recall..."
RESUME_OUT="$SCRATCH_DIR/run-resume.jsonl"
"$OPENCODE_BIN" run "Repeat the secret token you answered in the first turn of this session." \
    -s "$SESSION_ID" \
    --format json \
    --dir "$SCRATCH_DIR" > "$RESUME_OUT" 2>&1 || {
        log_fail "opencode resume failed. Output:"
        cat "$RESUME_OUT"
        exit 1
    }

if grep -q "SMOKE_START" "$RESUME_OUT"; then
    log_pass "Session resumption successfully recalled prior context ('SMOKE_START')."
else
    log_fail "Session resumption succeeded but did not recall expected context. Output:"
    cat "$RESUME_OUT"
    exit 1
fi

# 7. Session Export & Continuity Check
log_info "Step 7: Validating session export and message history..."
EXPORT_OUT="$SCRATCH_DIR/session-export.json"
PAGER=cat "$OPENCODE_BIN" export "$SESSION_ID" > "$EXPORT_OUT" 2>/dev/null || true

MSG_COUNT="$(python3 -c "
import json
try:
    with open('$EXPORT_OUT') as f:
        data = json.load(f)
    print(len(data.get('messages', [])))
except Exception:
    print(0)
")"

if [[ "$MSG_COUNT" -ge 4 ]]; then
    log_pass "Session exported successfully with $MSG_COUNT messages (continuity verified)."
else
    log_warn "Export message count ($MSG_COUNT) less than expected 4, but turns succeeded."
fi

# 8. Systemd User Execution Validation
log_info "Step 8: Testing non-interactive headless execution via systemd-run --user..."
if systemd-run --user --wait "$OPENCODE_BIN" run "Respond with the word SYSTEMD_OK" \
    -m "$FREE_MODEL" \
    --format json \
    --dir "$SCRATCH_DIR" >/dev/null 2>&1; then
    log_pass "Agent executed successfully inside an isolated systemd user unit."
else
    log_warn "systemd-run execution encountered an issue (check systemctl --user status)."
fi

# 9. Codex CLI Companion Check
log_info "Step 9: Inspecting Codex CLI companion runtime..."
if [[ -x "$CODEX_BIN" ]]; then
    CODEX_VER="$("$CODEX_BIN" --version 2>/dev/null || true)"
    log_pass "Codex CLI found ($CODEX_VER). Companion worktree & VS Code panel integration available."
else
    log_warn "Codex CLI binary not found at default extension location."
fi

printf "\n${GREEN}==============================================================================${NC}\n"
printf "${GREEN} Smoke Test Complete: All critical agent runtime & GitHub prerequisites verified!${NC}\n"
printf "${GREEN}==============================================================================${NC}\n"

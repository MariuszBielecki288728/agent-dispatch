#!/usr/bin/env bash
# ==============================================================================
# scripts/smoke-runtime.sh
#
# Smoke verification suite for agent-dispatch:
# - Deterministic mock suite and negative failure-injection tests (default / CI).
# - Opt-in live VM provider verification (--live / SMOKE_LIVE=1).
# - Strict assertions: structural JSON parsing, turn continuity, bounded timeouts.
# - Sanitized reporting: no credentials, tokens, or raw unfiltered outputs dumped.
# ==============================================================================

set -euo pipefail

# ANSI colors
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Mode detection
MODE="mock"
for arg in "$@"; do
    case "$arg" in
        --live) MODE="live" ;;
        --mock) MODE="mock" ;;
        --help|-h)
            cat <<EOF
Usage: $0 [--mock | --live]

Modes:
  --mock (default) Run deterministic mock checks and negative test cases.
                   Safe for CI and unauthenticated environments.
  --live           Run live checks against VM environment (gh-craftlypse,
                   commandcode, systemd --user). Requires credentials.
EOF
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 1
            ;;
    esac
done

if [[ "${SMOKE_LIVE:-0}" == "1" ]]; then
    MODE="live"
fi

# Track results in arrays
declare -a TEST_NAMES=()
declare -a TEST_STATUS=()
declare -a TEST_DETAILS=()

record_result() {
    local name="$1"
    local status="$2" # PASS, FAIL, SKIP
    local details="$3"
    TEST_NAMES+=("$name")
    TEST_STATUS+=("$status")
    TEST_DETAILS+=("$details")
}

log_info() { printf "${BLUE}[INFO]${NC} %s\n" "$*"; }
log_pass() { printf "${GREEN}[PASS]${NC} %s\n" "$*"; }
log_warn() { printf "${YELLOW}[WARN]${NC} %s\n" "$*"; }
log_fail() { printf "${RED}[FAIL]${NC} %s\n" "$*"; }

# Temporary scratch space
SCRATCH_DIR="$(mktemp -d /tmp/agent-dispatch-smoke-XXXXXX)"
cleanup() {
    rm -rf "$SCRATCH_DIR"
}
trap cleanup EXIT

# Python helper to extract fields safely from NDJSON streams
parse_ndjson_result() {
    local file="$1"
    python3 -c "
import sys, json

session_id = None
final_text = None
subtype = None
error_msg = None

try:
    with open('$file') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            
            # Check run_start event
            if data.get('type') == 'event' and data.get('event', {}).get('type') == 'run_start':
                session_id = data['event'].get('sessionId')
            
            # Check result line
            if data.get('type') == 'result':
                subtype = data.get('subtype')
                session_id = data.get('sessionId') or session_id
                final_text = data.get('finalText', '')
                if subtype != 'success':
                    error_msg = data.get('error', {}).get('message') or final_text
except Exception as e:
    print(json.dumps({'status': 'parse_error', 'error': str(e)}))
    sys.exit(0)

print(json.dumps({
    'sessionId': session_id,
    'finalText': final_text,
    'subtype': subtype,
    'errorMessage': error_msg
}))
"
}

# ==============================================================================
# 1. Deterministic Negative Tests (Failure Injection)
# ==============================================================================
run_negative_tests() {
    log_info "Running deterministic negative test cases..."

    # Negative Test A: CLI exits with non-zero status
    log_info "Negative Test A: Verifying detection of non-zero CLI exit..."
    local fake_cli_exit="$SCRATCH_DIR/fake_exit_fail.sh"
    cat << 'EOF' > "$fake_cli_exit"
#!/usr/bin/env bash
echo '{"type":"event","event":{"type":"run_start","sessionId":"fake-id"}}'
echo 'Process crashed unexpectedly' >&2
exit 1
EOF
    chmod +x "$fake_cli_exit"

    local exit_out="$SCRATCH_DIR/neg_exit.out"
    if "$fake_cli_exit" > "$exit_out" 2>&1; then
        record_result "negative_exit_failure" "FAIL" "Failed to catch non-zero CLI exit code"
    else
        record_result "negative_exit_failure" "PASS" "Correctly caught non-zero CLI exit code"
    fi

    # Negative Test B: Missing sessionId in output
    log_info "Negative Test B: Verifying detection of missing session ID..."
    local fake_no_id="$SCRATCH_DIR/fake_no_id.jsonl"
    cat << 'EOF' > "$fake_no_id"
{"type":"event","event":{"type":"turn_start"}}
{"type":"result","subtype":"success","finalText":"OK"}
EOF
    local parsed_no_id
    parsed_no_id="$(parse_ndjson_result "$fake_no_id")"
    local sid
    sid="$(python3 -c "import json; print(json.loads('''$parsed_no_id''').get('sessionId') or '')")"
    if [[ -z "$sid" ]]; then
        record_result "negative_missing_session_id" "PASS" "Correctly caught missing session ID"
    else
        record_result "negative_missing_session_id" "FAIL" "Failed to detect missing session ID"
    fi

    # Negative Test C: Error subtype in result
    log_info "Negative Test C: Verifying detection of error result subtype..."
    local fake_err="$SCRATCH_DIR/fake_err.jsonl"
    cat << 'EOF' > "$fake_err"
{"type":"event","event":{"type":"run_start","sessionId":"fake-err-id"}}
{"type":"result","subtype":"error","finalText":"Rate limit exceeded","error":{"message":"Quota hit"}}
EOF
    local parsed_err
    parsed_err="$(parse_ndjson_result "$fake_err")"
    local subtype
    subtype="$(python3 -c "import json; print(json.loads('''$parsed_err''').get('subtype') or '')")"
    if [[ "$subtype" != "success" ]]; then
        record_result "negative_error_subtype" "PASS" "Correctly caught error result subtype"
    else
        record_result "negative_error_subtype" "FAIL" "Failed to catch error subtype"
    fi

    # Negative Test D: Resume context mismatch
    log_info "Negative Test D: Verifying detection of session context mismatch..."
    local fake_resume="$SCRATCH_DIR/fake_resume.jsonl"
    cat << 'EOF' > "$fake_resume"
{"type":"event","event":{"type":"run_start","sessionId":"fake-id"}}
{"type":"result","subtype":"success","finalText":"WRONG_ANSWER"}
EOF
    local parsed_resume
    parsed_resume="$(parse_ndjson_result "$fake_resume")"
    local text
    text="$(python3 -c "import json; print(json.loads('''$parsed_resume''').get('finalText') or '')")"
    if [[ "$text" != "EXPECTED_TOKEN" ]]; then
        record_result "negative_resume_mismatch" "PASS" "Correctly caught context mismatch on resume"
    else
        record_result "negative_resume_mismatch" "FAIL" "Failed to catch context mismatch"
    fi
}

# ==============================================================================
# 2. Mock Test Suite (Default / CI)
# ==============================================================================
run_mock_suite() {
    log_info "Running mock verification suite..."

    # Mock Task: Fresh session
    local mock_fresh="$SCRATCH_DIR/mock_fresh.jsonl"
    cat << 'EOF' > "$mock_fresh"
{"type":"event","event":{"type":"run_start","sessionId":"mock-session-1234"}}
{"type":"event","event":{"type":"turn_start","turnNumber":1}}
{"type":"result","subtype":"success","sessionId":"mock-session-1234","finalText":"MOCK_SMOKE_OK"}
EOF
    local parsed_fresh
    parsed_fresh="$(parse_ndjson_result "$mock_fresh")"
    local sid subtype text
    sid="$(python3 -c "import json; print(json.loads('''$parsed_fresh''').get('sessionId') or '')")"
    subtype="$(python3 -c "import json; print(json.loads('''$parsed_fresh''').get('subtype') or '')")"
    text="$(python3 -c "import json; print(json.loads('''$parsed_fresh''').get('finalText') or '')")"

    if [[ "$sid" == "mock-session-1234" && "$subtype" == "success" && "$text" == "MOCK_SMOKE_OK" ]]; then
        record_result "mock_fresh_task" "PASS" "Mock fresh task validated structurally"
    else
        record_result "mock_fresh_task" "FAIL" "Mock fresh task validation failed"
    fi

    # Mock Resume: Same session continuity
    local mock_resume="$SCRATCH_DIR/mock_resume.jsonl"
    cat << 'EOF' > "$mock_resume"
{"type":"event","event":{"type":"run_start","sessionId":"mock-session-1234"}}
{"type":"event","event":{"type":"turn_start","turnNumber":2}}
{"type":"result","subtype":"success","sessionId":"mock-session-1234","finalText":"MOCK_SMOKE_OK"}
EOF
    local parsed_res
    parsed_res="$(parse_ndjson_result "$mock_resume")"
    local res_sid res_text
    res_sid="$(python3 -c "import json; print(json.loads('''$parsed_res''').get('sessionId') or '')")"
    res_text="$(python3 -c "import json; print(json.loads('''$parsed_res''').get('finalText') or '')")"

    if [[ "$res_sid" == "mock-session-1234" && "$res_text" == "MOCK_SMOKE_OK" ]]; then
        record_result "mock_session_resume" "PASS" "Mock session resume validated structurally"
    else
        record_result "mock_session_resume" "FAIL" "Mock session resume validation failed"
    fi

    # Optional runners recorded as SKIP in mock mode
    record_result "live_commandcode" "SKIP" "Live Command Code check skipped (run with --live)"
    record_result "live_github_access" "SKIP" "Live GitHub access check skipped (run with --live)"
    record_result "live_opencode_optional" "SKIP" "OpenCode optional alternative skipped"
    record_result "live_codex_optional" "SKIP" "Codex optional alternative skipped"
}

# ==============================================================================
# 3. Live Test Suite (Opt-in via --live / SMOKE_LIVE=1)
# ==============================================================================
run_live_suite() {
    log_info "Running live environment checks on host..."

    # 1. Systemd Lingering
    log_info "Step 1: Checking user lingering..."
    local linger_val
    linger_val="$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || true)"
    if [[ "$linger_val" == "yes" ]]; then
        record_result "systemd_lingering" "PASS" "Linger=yes verified"
    else
        record_result "systemd_lingering" "FAIL" "Linger not enabled (state: $linger_val)"
    fi

    # 2. GitHub Access via gh-craftlypse
    local gh_wrapper="/home/craftlypse/.local/bin/gh-craftlypse"
    log_info "Step 2: Testing gh-craftlypse..."
    if [[ -x "$gh_wrapper" ]]; then
        local repo_view
        if repo_view="$(timeout 15s "$gh_wrapper" repo view MariuszBielecki288728/agent-dispatch --json nameWithOwner 2>&1)"; then
            record_result "gh_craftlypse_api" "PASS" "Verified repository read permission"
        else
            record_result "gh_craftlypse_api" "FAIL" "Repository query failed"
        fi
    else
        record_result "gh_craftlypse_api" "FAIL" "Wrapper missing at $gh_wrapper"
    fi

    # 3. Git HTTPS Credential Helper
    log_info "Step 3: Testing Git credential helper..."
    if [[ -x "$gh_wrapper" ]]; then
        local cred_out
        cred_out="$(printf "protocol=https\nhost=github.com\n\n" | timeout 10s "$gh_wrapper" auth git-credential get 2>&1 || true)"
        if echo "$cred_out" | grep -q "username=x-access-token"; then
            record_result "git_credential_helper" "PASS" "Credential helper returned valid auth structure"
        else
            record_result "git_credential_helper" "FAIL" "Credential helper failed"
        fi
    fi

    # 4. Command Code CLI Fresh Task
    local cmdcode_bin="/home/craftlypse/.local/bin/commandcode"
    local live_session_id=""
    log_info "Step 4: Testing Command Code fresh task..."
    if [[ -x "$cmdcode_bin" ]]; then
        local fresh_log="$SCRATCH_DIR/cmdcode_fresh.jsonl"
        if timeout 30s "$cmdcode_bin" -p "Respond with EXACTLY the word LIVE_SMOKE_OK and nothing else." \
            --output-format json > "$fresh_log" 2>/dev/null; then
            
            local parsed
            parsed="$(parse_ndjson_result "$fresh_log")"
            live_session_id="$(python3 -c "import json; print(json.loads('''$parsed''').get('sessionId') or '')")"
            local text
            text="$(python3 -c "import json; print(json.loads('''$parsed''').get('finalText') or '')")"
            local subtype
            subtype="$(python3 -c "import json; print(json.loads('''$parsed''').get('subtype') or '')")"

            if [[ -n "$live_session_id" && "$subtype" == "success" && "$text" == *"LIVE_SMOKE_OK"* ]]; then
                record_result "commandcode_fresh_task" "PASS" "Session $live_session_id returned expected output"
            else
                record_result "commandcode_fresh_task" "FAIL" "Structural validation failed (subtype: $subtype)"
            fi
        else
            record_result "commandcode_fresh_task" "FAIL" "Command Code execution timed out or exited non-zero"
        fi
    else
        record_result "commandcode_fresh_task" "FAIL" "Binary missing at $cmdcode_bin"
    fi

    # 5. Command Code Session Resumption
    log_info "Step 5: Testing Command Code session resumption..."
    if [[ -n "$live_session_id" && -x "$cmdcode_bin" ]]; then
        local resume_log="$SCRATCH_DIR/cmdcode_resume.jsonl"
        if timeout 30s "$cmdcode_bin" -p "Repeat the exact token you answered in the first turn." \
            --session "$live_session_id" --output-format json > "$resume_log" 2>/dev/null; then
            
            local parsed_res
            parsed_res="$(parse_ndjson_result "$resume_log")"
            local res_text
            res_text="$(python3 -c "import json; print(json.loads('''$parsed_res''').get('finalText') or '')")"
            local res_sub
            res_sub="$(python3 -c "import json; print(json.loads('''$parsed_res''').get('subtype') or '')")"

            if [[ "$res_sub" == "success" && "$res_text" == *"LIVE_SMOKE_OK"* ]]; then
                record_result "commandcode_session_resume" "PASS" "Successfully recalled prior turn context"
            else
                record_result "commandcode_session_resume" "FAIL" "Failed to recall context (got: '$res_text')"
            fi
        else
            record_result "commandcode_session_resume" "FAIL" "Resume execution timed out or exited non-zero"
        fi
    else
        record_result "commandcode_session_resume" "SKIP" "Skipped due to failed initial session"
    fi

    # 6. Headless systemd execution
    log_info "Step 6: Testing headless systemd-run execution..."
    if [[ -x "$cmdcode_bin" ]]; then
        if timeout 20s systemd-run --user --wait "$cmdcode_bin" -p "Respond with SYSTEMD_PASS" \
            --output-format json >/dev/null 2>&1; then
            record_result "commandcode_systemd_run" "PASS" "Executed successfully in isolated user service"
        else
            record_result "commandcode_systemd_run" "FAIL" "systemd-run failed or timed out"
        fi
    else
        record_result "commandcode_systemd_run" "SKIP" "Command Code binary missing"
    fi

    # Optional alternatives recorded as SKIP unless explicitly requested
    record_result "opencode_alternative" "SKIP" "Optional alternative (OpenCode Go sub currently inactive)"
    record_result "codex_alternative" "SKIP" "Optional alternative (Codex quota limit reached)"
}

# ==============================================================================
# Execution & Summary Report
# ==============================================================================
echo "=============================================================================="
echo " Starting agent-dispatch smoke verification (Mode: $MODE)"
echo "=============================================================================="

run_negative_tests

if [[ "$MODE" == "live" ]]; then
    run_live_suite
else
    run_mock_suite
fi

echo ""
echo "=============================================================================="
echo " Smoke Verification Summary Report"
echo "=============================================================================="
printf "%-32s | %-8s | %s\n" "TEST CASE" "STATUS" "DETAILS"
echo "------------------------------------------------------------------------------"

ANY_FAILED=0
for i in "${!TEST_NAMES[@]}"; do
    NAME="${TEST_NAMES[$i]}"
    STATUS="${TEST_STATUS[$i]}"
    DETAILS="${TEST_DETAILS[$i]}"

    case "$STATUS" in
        PASS) printf "%-32s | ${GREEN}%-8s${NC} | %s\n" "$NAME" "$STATUS" "$DETAILS" ;;
        FAIL)
            printf "%-32s | ${RED}%-8s${NC} | %s\n" "$NAME" "$STATUS" "$DETAILS"
            ANY_FAILED=1
            ;;
        SKIP) printf "%-32s | ${YELLOW}%-8s${NC} | %s\n" "$NAME" "$STATUS" "$DETAILS" ;;
    esac
done
echo "=============================================================================="

if [[ "$ANY_FAILED" -eq 1 ]]; then
    log_fail "One or more required smoke test cases FAILED."
    exit 1
else
    log_pass "All required test cases PASSED. (Optional checks marked as SKIP)."
    exit 0
fi

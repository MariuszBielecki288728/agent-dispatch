#!/usr/bin/env bash
# ==============================================================================
# scripts/smoke-runtime.sh
#
# Smoke verification suite for agent-dispatch:
# - Deterministic mock suite and negative failure-injection tests (default / CI).
# - Opt-in live VM provider verification (--live / SMOKE_LIVE=1).
# - Strict assertions: structural JSON parsing, turn continuity, bounded timeouts.
# - Sanitized reporting: no credentials, tokens, or raw unfiltered outputs dumped.
# - Safe parsing: zero code interpolation of untrusted model outputs into Python.
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

# ==============================================================================
# Shared Turn Validator & Data Parser
#
# Crucial Security Design:
# Untrusted model outputs and filenames are NEVER interpolated into executable
# Python code strings. Arguments are passed strictly as positional argv elements,
# and JSON fields are extracted purely by decoding data streams from stdin.
# ==============================================================================

validate_turn() {
    local log_file="$1"
    local exit_code="$2"
    local expected_session_id="${3:-}"
    local expected_token="${4:-}"

    python3 - "$log_file" "$exit_code" "$expected_session_id" "$expected_token" << 'PYEOF'
import sys, json, os

log_file = sys.argv[1]
try:
    exit_code = int(sys.argv[2])
except ValueError:
    exit_code = 1

expected_session_id = sys.argv[3] if len(sys.argv) > 3 else ""
expected_token = sys.argv[4] if len(sys.argv) > 4 else ""

if exit_code != 0:
    print(json.dumps({
        "valid": False,
        "reason": f"Process exited with non-zero code {exit_code}",
        "sessionId": None
    }))
    sys.exit(0)

if not os.path.exists(log_file):
    print(json.dumps({
        "valid": False,
        "reason": "Log file does not exist",
        "sessionId": None
    }))
    sys.exit(0)

session_id = None
final_text = None
subtype = None
has_result = False

try:
    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except Exception:
                continue
            if not isinstance(data, dict):
                continue

            # Check run_start event
            if data.get("type") == "event":
                evt = data.get("event")
                if isinstance(evt, dict) and evt.get("type") == "run_start":
                    sid = evt.get("sessionId")
                    if sid:
                        session_id = str(sid).strip()

            # Check result line
            elif data.get("type") == "result":
                has_result = True
                subtype = data.get("subtype")
                sid = data.get("sessionId")
                if sid:
                    session_id = str(sid).strip()
                ft = data.get("finalText")
                final_text = str(ft) if ft is not None else ""
except Exception as e:
    print(json.dumps({
        "valid": False,
        "reason": f"Failed to read log stream: {type(e).__name__}",
        "sessionId": None
    }))
    sys.exit(0)

if not has_result:
    print(json.dumps({
        "valid": False,
        "reason": "Missing completed result event in NDJSON stream",
        "sessionId": None
    }))
    sys.exit(0)

if subtype != "success":
    print(json.dumps({
        "valid": False,
        "reason": f"Result subtype is '{subtype}' (expected 'success')",
        "sessionId": session_id
    }))
    sys.exit(0)

if not session_id:
    print(json.dumps({
        "valid": False,
        "reason": "Missing or empty sessionId in NDJSON stream",
        "sessionId": None
    }))
    sys.exit(0)

if expected_session_id and session_id != expected_session_id:
    print(json.dumps({
        "valid": False,
        "reason": "Session ID mismatch between turns",
        "sessionId": session_id
    }))
    sys.exit(0)

if expected_token and expected_token not in (final_text or ""):
    print(json.dumps({
        "valid": False,
        "reason": "Response token mismatch",
        "sessionId": session_id
    }))
    sys.exit(0)

print(json.dumps({
    "valid": True,
    "reason": "Turn validation succeeded",
    "sessionId": session_id
}))
PYEOF
}

parse_validation_field() {
    local field="$1"
    python3 -c 'import sys, json; data = json.load(sys.stdin); val = data.get(sys.argv[1]); print(val if val is not None else "")' "$field"
}

# ==============================================================================
# 1. Deterministic Negative Tests (Failure Injection)
#
# All negative tests run through the exact production validator (validate_turn)
# ensuring acceptance logic cannot regress.
# ==============================================================================
run_negative_tests() {
    log_info "Running deterministic negative test cases through shared validator..."

    # Negative Test A: Non-zero process exit
    log_info "Negative Test A: Verifying detection of non-zero CLI exit..."
    local val_exit
    val_exit="$(validate_turn "/dev/null" 1 "" "")"
    local val_exit_valid val_exit_reason
    val_exit_valid="$(printf '%s' "$val_exit" | parse_validation_field "valid")"
    val_exit_reason="$(printf '%s' "$val_exit" | parse_validation_field "reason")"

    if [[ "$val_exit_valid" == "False" && "$val_exit_reason" == *"non-zero code"* ]]; then
        record_result "negative_exit_failure" "PASS" "Correctly caught non-zero CLI exit code"
    else
        record_result "negative_exit_failure" "FAIL" "Failed to detect non-zero exit code"
    fi

    # Negative Test B: Missing sessionId in stream
    log_info "Negative Test B: Verifying detection of missing session ID..."
    local fake_no_id="$SCRATCH_DIR/fake_no_id.jsonl"
    cat << 'EOF' > "$fake_no_id"
{"type":"event","event":{"type":"turn_start"}}
{"type":"result","subtype":"success","finalText":"OK"}
EOF
    local val_no_id
    val_no_id="$(validate_turn "$fake_no_id" 0 "" "")"
    local no_id_valid no_id_reason
    no_id_valid="$(printf '%s' "$val_no_id" | parse_validation_field "valid")"
    no_id_reason="$(printf '%s' "$val_no_id" | parse_validation_field "reason")"

    if [[ "$no_id_valid" == "False" && "$no_id_reason" == *"Missing or empty sessionId"* ]]; then
        record_result "negative_missing_session_id" "PASS" "Correctly caught missing session ID"
    else
        record_result "negative_missing_session_id" "FAIL" "Failed to catch missing session ID"
    fi

    # Negative Test C: Error subtype in result
    log_info "Negative Test C: Verifying detection of error result subtype..."
    local fake_err="$SCRATCH_DIR/fake_err.jsonl"
    cat << 'EOF' > "$fake_err"
{"type":"event","event":{"type":"run_start","sessionId":"fake-err-id"}}
{"type":"result","subtype":"error","sessionId":"fake-err-id","finalText":"Rate limit exceeded","error":{"message":"Quota hit"}}
EOF
    local val_err
    val_err="$(validate_turn "$fake_err" 0 "" "")"
    local err_valid err_reason
    err_valid="$(printf '%s' "$val_err" | parse_validation_field "valid")"
    err_reason="$(printf '%s' "$val_err" | parse_validation_field "reason")"

    if [[ "$err_valid" == "False" && "$err_reason" == *"Result subtype is 'error'"* ]]; then
        record_result "negative_error_subtype" "PASS" "Correctly caught error result subtype"
    else
        record_result "negative_error_subtype" "FAIL" "Failed to catch error result subtype"
    fi

    # Negative Test D: Session ID mismatch on resume
    log_info "Negative Test D: Verifying detection of session ID mismatch on resume..."
    local fake_id_mismatch="$SCRATCH_DIR/fake_id_mismatch.jsonl"
    cat << 'EOF' > "$fake_id_mismatch"
{"type":"event","event":{"type":"run_start","sessionId":"session-new-different"}}
{"type":"result","subtype":"success","sessionId":"session-new-different","finalText":"MOCK_SMOKE_OK"}
EOF
    local val_id_mis
    val_id_mis="$(validate_turn "$fake_id_mismatch" 0 "session-expected-original" "MOCK_SMOKE_OK")"
    local id_mis_valid id_mis_reason
    id_mis_valid="$(printf '%s' "$val_id_mis" | parse_validation_field "valid")"
    id_mis_reason="$(printf '%s' "$val_id_mis" | parse_validation_field "reason")"

    if [[ "$id_mis_valid" == "False" && "$id_mis_reason" == *"Session ID mismatch"* ]]; then
        record_result "negative_resume_id_mismatch" "PASS" "Correctly caught session ID mismatch on resume"
    else
        record_result "negative_resume_id_mismatch" "FAIL" "Failed to catch session ID mismatch"
    fi

    # Negative Test E: Token mismatch on resume
    log_info "Negative Test E: Verifying detection of response token mismatch..."
    local fake_token_mis="$SCRATCH_DIR/fake_token_mis.jsonl"
    cat << 'EOF' > "$fake_token_mis"
{"type":"event","event":{"type":"run_start","sessionId":"matching-session-id"}}
{"type":"result","subtype":"success","sessionId":"matching-session-id","finalText":"UNEXPECTED_ANSWER"}
EOF
    local val_tok_mis
    val_tok_mis="$(validate_turn "$fake_token_mis" 0 "matching-session-id" "EXPECTED_TOKEN")"
    local tok_mis_valid tok_mis_reason
    tok_mis_valid="$(printf '%s' "$val_tok_mis" | parse_validation_field "valid")"
    tok_mis_reason="$(printf '%s' "$val_tok_mis" | parse_validation_field "reason")"

    if [[ "$tok_mis_valid" == "False" && "$tok_mis_reason" == *"Response token mismatch"* ]]; then
        record_result "negative_token_mismatch" "PASS" "Correctly caught response token mismatch"
    else
        record_result "negative_token_mismatch" "FAIL" "Failed to catch response token mismatch"
    fi

    # Negative Test F: Truncated / malformed stream without result event
    log_info "Negative Test F: Verifying detection of truncated stream..."
    local fake_trunc="$SCRATCH_DIR/fake_trunc.jsonl"
    cat << 'EOF' > "$fake_trunc"
{"type":"event","event":{"type":"run_start","sessionId":"trunc-id"}}
{"type":"event","event":{"type":"turn_start","turnNumber":1}}
{"type":"event","event":{"type":"text_delta","text":"Process killed prematurely...
EOF
    local val_trunc
    val_trunc="$(validate_turn "$fake_trunc" 0 "" "")"
    local trunc_valid trunc_reason
    trunc_valid="$(printf '%s' "$val_trunc" | parse_validation_field "valid")"
    trunc_reason="$(printf '%s' "$val_trunc" | parse_validation_field "reason")"

    if [[ "$trunc_valid" == "False" && "$trunc_reason" == *"Missing completed result event"* ]]; then
        record_result "negative_truncated_incomplete" "PASS" "Correctly caught truncated stream"
    else
        record_result "negative_truncated_incomplete" "FAIL" "Failed to catch truncated stream"
    fi

    # Negative Test G: Hostile injection payload (quotes, backslashes, ''', code injection attempt)
    log_info "Negative Test G: Verifying safe parsing of hostile payload without code execution..."
    local canary_file="$SCRATCH_DIR/canary_pwned.txt"
    rm -f "$canary_file"
    local fake_hostile="$SCRATCH_DIR/fake_hostile.jsonl"

    python3 - "$fake_hostile" "$canary_file" << 'PYEOF'
import sys, json
fake_file = sys.argv[1]
canary = sys.argv[2]
with open(fake_file, "w", encoding="utf-8") as f:
    f.write(json.dumps({"type": "event", "event": {"type": "run_start", "sessionId": "hostile-sess-123"}}) + "\n")
    f.write(json.dumps({
        "type": "result",
        "subtype": "success",
        "sessionId": "hostile-sess-123",
        "finalText": f"Hostile: ''' + __import__('os').system('touch {canary}') + ''' \\ \" ' ; SAFE_CANARY_TOKEN"
    }) + "\n")
PYEOF

    local val_hostile
    val_hostile="$(validate_turn "$fake_hostile" 0 "hostile-sess-123" "SAFE_CANARY_TOKEN")"
    local host_valid host_reason
    host_valid="$(printf '%s' "$val_hostile" | parse_validation_field "valid")"
    host_reason="$(printf '%s' "$val_hostile" | parse_validation_field "reason")"

    if [[ -f "$canary_file" ]]; then
        record_result "negative_hostile_injection" "FAIL" "VULNERABILITY: Code injection executed during parsing!"
        rm -f "$canary_file"
    elif [[ "$host_valid" == "True" ]]; then
        record_result "negative_hostile_injection" "PASS" "Safely parsed hostile quotes/literals without code execution"
    else
        record_result "negative_hostile_injection" "FAIL" "Validator failed safe hostile parse: $host_reason"
    fi
}

# ==============================================================================
# 2. Mock Test Suite (Default / CI)
# ==============================================================================
run_mock_suite() {
    log_info "Running mock verification suite through shared validator..."

    # Mock Task: Fresh session
    local mock_fresh="$SCRATCH_DIR/mock_fresh.jsonl"
    cat << 'EOF' > "$mock_fresh"
{"type":"event","event":{"type":"run_start","sessionId":"mock-session-1234"}}
{"type":"event","event":{"type":"turn_start","turnNumber":1}}
{"type":"result","subtype":"success","sessionId":"mock-session-1234","finalText":"MOCK_SMOKE_OK"}
EOF
    local val_mock_fresh
    val_mock_fresh="$(validate_turn "$mock_fresh" 0 "" "MOCK_SMOKE_OK")"
    local fresh_valid fresh_reason
    fresh_valid="$(printf '%s' "$val_mock_fresh" | parse_validation_field "valid")"
    fresh_reason="$(printf '%s' "$val_mock_fresh" | parse_validation_field "reason")"

    if [[ "$fresh_valid" == "True" ]]; then
        record_result "mock_fresh_task" "PASS" "Mock fresh task validated structurally"
    else
        record_result "mock_fresh_task" "FAIL" "Mock fresh task validation failed: $fresh_reason"
    fi

    # Mock Resume: Same session continuity
    local mock_resume="$SCRATCH_DIR/mock_resume.jsonl"
    cat << 'EOF' > "$mock_resume"
{"type":"event","event":{"type":"run_start","sessionId":"mock-session-1234"}}
{"type":"event","event":{"type":"turn_start","turnNumber":2}}
{"type":"result","subtype":"success","sessionId":"mock-session-1234","finalText":"MOCK_SMOKE_OK"}
EOF
    local val_mock_resume
    val_mock_resume="$(validate_turn "$mock_resume" 0 "mock-session-1234" "MOCK_SMOKE_OK")"
    local res_valid res_reason
    res_valid="$(printf '%s' "$val_mock_resume" | parse_validation_field "valid")"
    res_reason="$(printf '%s' "$val_mock_resume" | parse_validation_field "reason")"

    if [[ "$res_valid" == "True" ]]; then
        record_result "mock_session_resume" "PASS" "Mock session resume validated structurally"
    else
        record_result "mock_session_resume" "FAIL" "Mock session resume validation failed: $res_reason"
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
    local fresh_passed=0
    log_info "Step 4: Testing Command Code fresh task..."
    if [[ -x "$cmdcode_bin" ]]; then
        local fresh_log="$SCRATCH_DIR/cmdcode_fresh.jsonl"
        local fresh_code=0
        timeout 30s "$cmdcode_bin" -p "Respond with EXACTLY the word LIVE_SMOKE_OK and nothing else." \
            --output-format json > "$fresh_log" 2>/dev/null || fresh_code=$?
        
        local val_fresh
        val_fresh="$(validate_turn "$fresh_log" "$fresh_code" "" "LIVE_SMOKE_OK")"
        local fresh_valid fresh_sid fresh_reason
        fresh_valid="$(printf '%s' "$val_fresh" | parse_validation_field "valid")"
        fresh_sid="$(printf '%s' "$val_fresh" | parse_validation_field "sessionId")"
        fresh_reason="$(printf '%s' "$val_fresh" | parse_validation_field "reason")"

        if [[ "$fresh_valid" == "True" && -n "$fresh_sid" ]]; then
            live_session_id="$fresh_sid"
            fresh_passed=1
            record_result "commandcode_fresh_task" "PASS" "Session $live_session_id returned expected output"
        else
            record_result "commandcode_fresh_task" "FAIL" "Turn validation failed: $fresh_reason"
        fi
    else
        record_result "commandcode_fresh_task" "FAIL" "Binary missing at $cmdcode_bin"
    fi

    # 5. Command Code Session Resumption (Assert exact session ID equality)
    log_info "Step 5: Testing Command Code session resumption..."
    if [[ "$fresh_passed" -eq 1 && -n "$live_session_id" && -x "$cmdcode_bin" ]]; then
        local resume_log="$SCRATCH_DIR/cmdcode_resume.jsonl"
        local resume_code=0
        timeout 30s "$cmdcode_bin" -p "Repeat the exact token you answered in the first turn." \
            --session "$live_session_id" --output-format json > "$resume_log" 2>/dev/null || resume_code=$?
        
        local val_resume
        val_resume="$(validate_turn "$resume_log" "$resume_code" "$live_session_id" "LIVE_SMOKE_OK")"
        local res_valid res_sid res_reason
        res_valid="$(printf '%s' "$val_resume" | parse_validation_field "valid")"
        res_sid="$(printf '%s' "$val_resume" | parse_validation_field "sessionId")"
        res_reason="$(printf '%s' "$val_resume" | parse_validation_field "reason")"

        if [[ "$res_valid" == "True" && "$res_sid" == "$live_session_id" ]]; then
            record_result "commandcode_session_resume" "PASS" "Successfully recalled context in session $live_session_id"
        else
            record_result "commandcode_session_resume" "FAIL" "Turn validation failed: $res_reason"
        fi
    else
        record_result "commandcode_session_resume" "FAIL" "Dependency failed: fresh session was not established"
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

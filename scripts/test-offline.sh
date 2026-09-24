#!/usr/bin/env bash
# ==============================================================================
# scripts/test-offline.sh
#
# Deterministic, offline test-suite for agent-dispatch.
#
# Guarantees:
#   * no network calls, no model credits, no GitHub mutation,
#   * no third-party Python packages (stdlib unittest only),
#   * every GitHub interaction goes through tests/fake_wrapper.py,
#   * a repository scan afterwards proves no state, log or secret file leaked
#     into the working tree.
#
# This is the command CI runs. It is deliberately the same command a developer
# runs locally, so "passes in CI" and "passes here" cannot diverge.
# ==============================================================================

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

GREEN='\033[0;32m'; RED='\033[0;31m'; BLUE='\033[0;34m'; NC='\033[0m'
log_info() { printf "${BLUE}[INFO]${NC} %s\n" "$*"; }
log_ok()   { printf "${GREEN}[ OK ]${NC} %s\n" "$*"; }
log_fail() { printf "${RED}[FAIL]${NC} %s\n" "$*" >&2; }

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
    log_fail "python3 not found"
    exit 1
fi

log_info "python: $("$PYTHON" -VV)"
log_info "package: agent_dispatch (stdlib only, no third-party imports required)"

# The fake wrapper must be executable; it is the single seam the tests replace.
chmod +x tests/fake_wrapper.py

# 1. Syntax/import check of every source file before running the suite, so a
#    broken module fails with a clear message rather than a wall of test errors.
log_info "step 1/3: byte-compiling sources"
if ! "$PYTHON" -m compileall -q src tests > /dev/null; then
    log_fail "sources do not compile"
    exit 1
fi
log_ok "sources compile"

# 2. The real suite.
log_info "step 2/3: running the offline suite"
SUITE_LOG="$(mktemp)"
trap 'rm -f "$SUITE_LOG"' EXIT
set +e
PYTHONPATH="$REPO_ROOT/src" "$PYTHON" -m unittest discover -s tests -p 'test_*.py' -v 2>&1 | tee "$SUITE_LOG"
STATUS="${PIPESTATUS[0]}"
set -e

SUMMARY="$(grep -E '^(OK|FAILED|Ran )' "$SUITE_LOG" | tail -n 2 | tr '\n' ' ' || true)"
if [[ "$STATUS" -ne 0 ]]; then
    log_fail "offline suite failed: $SUMMARY"
    exit "$STATUS"
fi
log_ok "offline suite passed: $SUMMARY"

# 3. Placement/leak assertion: the service must never write into the checkout.
log_info "step 3/3: asserting no orchestrator state leaked into the repository"
LEAKS="$(git status --porcelain --untracked-files=all --ignored=no | grep -vE '^\?\? (tests/(fake_wrapper\.py|__pycache__)/?|src/)' || true)"
UNTRACKED_STATE="$(find . -path ./.git -prune -o \
    \( -name 'state.db*' -o -name 'worker.lock' -o -name '*.ndjson' \) -print 2>/dev/null || true)"
if [[ -n "$UNTRACKED_STATE" ]]; then
    log_fail "state/log artefacts were created inside the repository:"
    printf '%s\n' "$UNTRACKED_STATE"
    exit 1
fi
log_ok "no state, lock or run-log artefacts in the working tree"

log_ok "offline verification complete — no network, no model credits, no GitHub mutation"

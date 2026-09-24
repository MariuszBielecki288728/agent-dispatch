#!/usr/bin/env bash
# ==============================================================================
# scripts/install-service.sh
#
# Install / upgrade / remove agent-dispatch as a systemd --user service, and
# print the log commands an operator actually needs.
#
# Design constraints honoured here:
#   * The worker must run with no VS Code window open, so it is a systemd --user
#     unit and the script proves Linger=yes instead of assuming it.
#   * There is NO timer: the worker polls forever on its own interval. Adding a
#     timer on top would schedule a second, competing mechanism.
#   * The service is installed for the current user only; nothing is written into
#     any target repository.
#   * The unit calls the uv-installed executable directly, never `uv run`, so a
#     restart cannot trigger a dependency sync. Upgrading is an explicit
#     `uv sync --locked --no-dev` followed by a restart (see docs/operations.md).
#
#   ./scripts/install-service.sh --install     # install + enable + start
#   ./scripts/install-service.sh --status      # unit + lock + last polls
#   ./scripts/install-service.sh --uninstall   # stop + disable + remove unit
# ==============================================================================

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_NAME="agent-dispatch.service"
UNIT_SRC="$REPO_ROOT/systemd/$UNIT_NAME"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT_DEST="$UNIT_DIR/$UNIT_NAME"
CONFIG_PATH="${AGENT_DISPATCH_CONFIG:-$HOME/.config/agent-dispatch/config.toml}"

RED='\033[0;31m'; GREEN='\033[0;32m'; BLUE='\033[0;34m'; YELLOW='\033[1;33m'; NC='\033[0m'
log_info() { printf "${BLUE}[INFO]${NC} %s\n" "$*"; }
log_ok()   { printf "${GREEN}[ OK ]${NC} %s\n" "$*"; }
log_warn() { printf "${YELLOW}[WARN]${NC} %s\n" "$*"; }
log_fail() { printf "${RED}[FAIL]${NC} %s\n" "$*" >&2; }

# Print the configured GitHub wrapper command (the approved credential helper),
# or nothing when it cannot be determined. The TOML is parsed by Python with the
# path passed as an argv element, so configuration text is never interpreted as
# shell code — the same rule cmd_status() follows.
resolve_wrapper() {
    [[ -f "$CONFIG_PATH" ]] || return 0
    python3 - "$CONFIG_PATH" <<'PY' 2>/dev/null || true
import sys, tomllib

try:
    with open(sys.argv[1], "rb") as fh:
        cfg = tomllib.load(fh)
except Exception:
    sys.exit(0)

command = cfg.get("github", {}).get("command", "")
if command:
    print(command)
PY
}

usage() {
    local wrapper
    wrapper="$(resolve_wrapper)"
    if [[ -z "$wrapper" ]]; then
        wrapper="<configured-wrapper>   # github.command in $CONFIG_PATH"
    fi

    cat <<EOF
Usage: $0 [--install | --status | --uninstall | --help]

  --install    Render the unit, enable and start agent-dispatch.service.
  --status     Show unit state, lock holder and recent poll log lines.
  --uninstall  Stop, disable and delete the user unit.

Canonical uv workflow (see docs/operations.md):

  # shared development/deployment checkout (the usual case): keep the dev group
  uv sync --locked
  mkdir -p ~/.local/bin
  ln -sf $REPO_ROOT/.venv/bin/agent-dispatch ~/.local/bin/agent-dispatch

  # upgrade an existing deployment (state, config and logs are untouched).
  # Update the checkout with the APPROVED wrapper-backed Git procedure first --
  # a bare 'git pull' does not guarantee the approved credential helper is used
  # on this VM (see docs/architecture.md 2.4). The reset entry is what stops an
  # ambient unapproved helper from being queried first:
  set +H   # '!' would otherwise trigger bash history expansion
  git -c credential.https://github.com.helper= \\
      -c credential.https://github.com.helper="!$wrapper auth git-credential" \\
      pull --ff-only
  uv sync --locked
  systemctl --user restart agent-dispatch.service

  # a DEDICATED deployment checkout (not used for development) may instead use
  # 'uv sync --locked --no-dev' -- but do not run that in a checkout where you
  # develop: it writes the same .venv and removes Ruff/pre-commit, breaking the
  # commit hook. Do not install the hook in a --no-dev checkout.

The unit invokes the installed executable directly and never 'uv run', so a
restart cannot trigger a dependency sync.

Environment:
  AGENT_DISPATCH_CONFIG  config path written into the unit (default: $CONFIG_PATH)
  UNIT_NAME              override the systemd unit name
EOF
}

require_systemctl() {
    if ! command -v systemctl >/dev/null 2>&1; then
        log_fail "systemctl not found: run the worker in the foreground instead:"
        log_fail "  agent-dispatch --config $CONFIG_PATH worker"
        exit 1
    fi
    if ! systemctl --user show-environment >/dev/null 2>&1; then
        log_fail "no systemd --user session is available in this shell."
        log_fail "Run from a real login session (or set XDG_RUNTIME_DIR) and retry."
        exit 1
    fi
}

check_linger() {
    local linger
    linger="$(loginctl show-user "$(id -un)" -p Linger --value 2>/dev/null || echo unknown)"
    if [[ "$linger" == "yes" ]]; then
        log_ok "Linger=yes — the worker survives logout (no VS Code window needed)"
    else
        log_warn "Linger is '$linger'. The worker will stop when your last session ends."
        log_warn "Enable it with:  sudo loginctl enable-linger $(id -un)"
    fi
}

cmd_install() {
    require_systemctl

    if [[ ! -f "$UNIT_SRC" ]]; then
        log_fail "unit template missing: $UNIT_SRC"
        exit 1
    fi

    local bin
    if ! bin="$(command -v agent-dispatch)"; then
        log_fail "agent-dispatch is not on PATH. Install it first (see docs/operations.md §1):"
        log_fail "  cd $REPO_ROOT"
        log_fail "  uv sync --locked --no-dev          # runtime-only environment"
        log_fail "  mkdir -p ~/.local/bin"
        log_fail "  ln -sf $REPO_ROOT/.venv/bin/agent-dispatch ~/.local/bin/agent-dispatch"
        exit 1
    fi

    # The unit must not invoke `uv run`: that can sync/download packages when the
    # environment does not match the lock, and a service restart must not mutate
    # the deployment. Point the unit at the installed executable instead.
    if [[ "$(basename "$bin")" == "uv" ]]; then
        log_fail "agent-dispatch resolved to 'uv'; the unit must call the installed executable."
        exit 1
    fi

    if [[ ! -f "$CONFIG_PATH" ]]; then
        log_warn "config not found at $CONFIG_PATH"
        log_warn "Create it from the approved example before starting:"
        log_warn "  mkdir -p ~/.config/agent-dispatch"
        log_warn "  cp $REPO_ROOT/config/agent-dispatch.example.toml $CONFIG_PATH"
    fi

    log_info "validating configuration before installing..."
    if ! "$bin" --config "$CONFIG_PATH" doctor --skip-github >/dev/null 2>&1; then
        log_warn "doctor reported problems; run it yourself to see them:"
        log_warn "  $bin --config $CONFIG_PATH doctor"
    fi

    mkdir -p "$UNIT_DIR"
    # Render the unit so the unit file matches the real interpreter/config paths,
    # rather than hardcoding this machine's layout in the repository copy.
    sed \
        -e "s|^ExecStart=.*|ExecStart=$bin --config $CONFIG_PATH worker|" \
        "$UNIT_SRC" > "$UNIT_DEST"
    log_ok "unit written to $UNIT_DEST"

    systemctl --user daemon-reload
    systemctl --user enable --now "$UNIT_NAME"
    log_ok "service enabled and started"

    check_linger

    echo
    log_info "verify with:"
    echo "  systemctl --user status $UNIT_NAME"
    echo "  journalctl --user -u $UNIT_NAME -f"
    echo "  $bin --config $CONFIG_PATH status --no-sync"
}

cmd_status() {
    require_systemctl
    systemctl --user status "$UNIT_NAME" --no-pager || true
    echo
    log_info "lock file:"
    # Read worker.lock_file from the TOML and expand ~ / $VARS for display only.
    # The expansion happens inside Python on a value passed as an argv element, so
    # configuration text is never interpreted as shell code.
    local lock_file
    if ! lock_file="$(python3 - "$CONFIG_PATH" <<'PY'
import os, sys, tomllib

try:
    with open(sys.argv[1], "rb") as fh:
        cfg = tomllib.load(fh)
except Exception:
    sys.exit(0)

raw = cfg.get("worker", {}).get("lock_file", "")
if raw:
    print(os.path.expanduser(os.path.expandvars(raw)))
PY
)"; then
        log_warn "could not read worker.lock_file from $CONFIG_PATH"
        return 0
    fi

    if [[ -z "$lock_file" ]]; then
        log_warn "worker.lock_file is not set in $CONFIG_PATH"
        return 0
    fi

    if [[ -f "$lock_file" ]]; then
        # The worker lock records only pid/timestamp/command — never credentials.
        cat "$lock_file"
    else
        log_warn "no lock file at $lock_file (worker not running?)"
    fi
    echo
    log_info "recent polls:"
    journalctl --user -u "$UNIT_NAME" --no-pager -n 20 || true
}

cmd_uninstall() {
    require_systemctl
    systemctl --user stop "$UNIT_NAME" 2>/dev/null || true
    systemctl --user disable "$UNIT_NAME" 2>/dev/null || true
    rm -f "$UNIT_DEST"
    systemctl --user daemon-reload
    log_ok "unit stopped, disabled and removed"
    log_info "state and logs were left in place; remove them explicitly if you want a clean slate:"
    echo "  ~/.local/state/agent-dispatch/"
}

case "${1:---help}" in
    --install)   cmd_install ;;
    --status)    cmd_status ;;
    --uninstall) cmd_uninstall ;;
    --help|-h)   usage ;;
    *)           usage; exit 2 ;;
esac

#!/usr/bin/env bash
set -euo pipefail

# ============================================================================
# HermesA2A v2 Installer
# ============================================================================
# Installs the hermes-a2a-v2 plugin into an existing Hermes Agent installation.
# Requires: Hermes Agent already installed, Python 3.10+, Git.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/emiltsoi/hermes-agent-a2a/main/install.sh | bash
#
# Or clone and run locally:
#   git clone https://github.com/emiltsoi/hermes-agent-a2a.git /tmp/hermes-a2a-v2
#   bash /tmp/hermes-a2a-v2/install.sh
# ============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$SCRIPT_DIR"

HERMES_HOME_DIR="${HERMES_HOME:-$HOME/.hermes}"
HERMES_PLUGINS_DIR="$HERMES_HOME_DIR/plugins"

# Resolve plugin install target
# Priority: HERMES_PLUGIN_DIR env > $HERMES_HOME/plugins/hermes-a2a-v2
if [[ -n "${HERMES_PLUGIN_DIR:-}" ]]; then
  TARGET_DIR="$HERMES_PLUGIN_DIR/hermes-a2a-v2"
elif [[ -n "${HERMES_PROFILE:-}" ]]; then
  TARGET_DIR="$HERMES_HOME_DIR/profiles/${HERMES_PROFILE}/plugins/hermes-a2a-v2"
else
  TARGET_DIR="$HERMES_PLUGINS_DIR/hermes-a2a-v2"
fi

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()  { echo -e "${CYAN}→${NC} $1"; }
log_ok()    { echo -e "${GREEN}✓${NC} $1"; }
log_warn()  { echo -e "${YELLOW}⚠${NC} $1"; }
log_error() { echo -e "${RED}✗${NC} $1"; }

# ============================================================================
# Prerequisites
# ============================================================================

check_prereqs() {
  log_info "Checking prerequisites..."

  if ! command -v git &>/dev/null; then
    log_error "Git not found. Install git first."
    exit 1
  fi
  log_ok "Git found"

  if ! command -v python3 &>/dev/null; then
    log_error "Python 3 not found. Install Python 3.10+ first."
    exit 1
  fi
  local py_ver
  py_ver=$(python3 --version 2>&1 | awk '{print $2}')
  log_ok "Python $py_ver found"

  # Check Hermes is installed
  if command -v hermes &>/dev/null; then
    log_ok "Hermes Agent found"
  else
    log_warn "Hermes command not found on PATH"
    log_info "  Ensure Hermes Agent is installed first: https://github.com/NousResearch/hermes-agent"
    log_info "  Or install: curl -fsSL https://raw.githubusercontent.com/NousResearch/hermes-agent/main/scripts/install.sh | bash"
  fi
}

# ============================================================================
# Vault creation
# ============================================================================

create_vault() {
  # VaultResolver expects: profiles/<profile>/a2a/vault.yaml
  # The install script creates for the "default" profile; users can adjust.
  local vault_dir="$HERMES_HOME_DIR/profiles/default/a2a"
  local vault_file="$vault_dir/vault.yaml"

  mkdir -p "$vault_dir"

  if [[ -f "$vault_file" ]] && grep -q "platforms:" "$vault_file" 2>/dev/null; then
    log_info "Vault already exists at $vault_file — skipping vault creation"
    return
  fi

  log_info "Creating vault at $vault_file..."
  cat > "$vault_file" <<'EOF'
# HermesA2A v2 — Vault
# Resolution: agent vault -> profile vault -> env vars -> explicit config (wins)
# Update bot_token and default_chat_id with your values.
# ${A2A_TELEGRAM_BOT_TOKEN} and ${A2A_OWNER_CHAT_ID} are resolved from env at runtime.

platforms:
  telegram:
    bot_token: "${A2A_TELEGRAM_BOT_TOKEN}"
    default_chat_id: "${A2A_OWNER_CHAT_ID}"

defaults:
  platform: telegram
  chat_type: dm
  chat_id_resolver: default_chat_id
EOF
  log_ok "Vault created at $vault_file"
  log_info "  Set A2A_TELEGRAM_BOT_TOKEN and A2A_OWNER_CHAT_ID env vars, or"
  log_info "  replace \${...} placeholders with real values directly in the vault file."
}

# ============================================================================
# Plugin symlink
# ============================================================================

install_plugin() {
  log_info "Installing hermes-a2a-v2 to $TARGET_DIR..."

  mkdir -p "$(dirname "$TARGET_DIR")"

  if [[ -L "$TARGET_DIR" ]]; then
    local current_target
    current_target="$(readlink "$TARGET_DIR")"
    if [[ "$current_target" != "$REPO_ROOT" ]]; then
      log_error "Existing symlink points elsewhere: $TARGET_DIR -> $current_target"
      log_info "  Remove it with: rm $TARGET_DIR"
      exit 1
    fi
    log_info "Plugin already symlinked to this checkout — skipping"
    return
  fi

  if [[ -e "$TARGET_DIR" ]]; then
    log_error "Path exists and is not a symlink: $TARGET_DIR"
    log_info "  Move it aside or remove it before re-running."
    exit 1
  fi

  ln -s "$REPO_ROOT" "$TARGET_DIR"
  log_ok "Symlinked hermes-a2a-v2 -> $TARGET_DIR"
}

# ============================================================================
# Enable plugin
# ============================================================================

enable_plugin() {
  local config_file="$HERMES_HOME_DIR/config.yaml"

  if [[ ! -f "$config_file" ]]; then
    log_warn "No config.yaml found at $config_file — skipping auto-enable"
    log_info "  Manually add 'hermes-a2a-v2' to the plugins.enabled list."
    return
  fi

  if grep -qE "^\s+-\s+hermes-a2a-v2" "$config_file" 2>/dev/null; then
    log_info "Plugin already enabled in config — skipping"
    return
  fi

  log_info "Enabling hermes-a2a-v2 in $config_file..."

  # Insert before the first enabled plugin entry, or at the start of plugins block
  if grep -q "^\s*plugins:" "$config_file" && grep -q "^\s*enabled:" "$config_file"; then
    # Use awk to insert after "enabled:" line
    awk '/^(\s*)enabled:/ && !done { print; print "      - hermes-a2a-v2"; done=1; next } {print}' \
      "$config_file" > "$config_file.tmp" && mv "$config_file.tmp" "$config_file"
    log_ok "Plugin enabled"
  else
    log_warn "Could not auto-insert — manually add '- hermes-a2a-v2' to plugins.enabled in $config_file"
  fi
}

# ============================================================================
# Verify
# ============================================================================

verify() {
  log_info "Verifying plugin package..."

  # Syntax-check all Python files (no import, just parse)
  local py_files
  py_files=$(find "$REPO_ROOT/src" -name "*.py" -type f)
  local fail_count=0
  for f in $py_files; do
    if ! python3 -m py_compile "$f" 2>/dev/null; then
      log_error "Syntax error in $f"
      fail_count=$((fail_count + 1))
    fi
  done

  if [[ $fail_count -eq 0 ]]; then
    log_ok "All Python files pass syntax check"
  else
    log_error "$fail_count file(s) failed syntax check"
    return 1
  fi

  # Verify the plugin package structure
  if [[ -f "$REPO_ROOT/src/plugin.py" ]] && [[ -f "$REPO_ROOT/src/__init__.py" ]]; then
    log_ok "Plugin package structure verified (src/plugin.py + src/__init__.py)"
  else
    log_error "Plugin package structure incomplete"
    return 1
  fi
}

# ============================================================================
# Main
# ============================================================================

main() {
  echo ""
  echo "┌─────────────────────────────────────────────────────────────┐"
  echo "│           HermesA2A v2 — Plugin Installer                  │"
  echo "└─────────────────────────────────────────────────────────────┘"
  echo ""

  check_prereqs
  install_plugin
  create_vault
  enable_plugin
  verify

  echo ""
  echo "┌─────────────────────────────────────────────────────────────┐"
  echo "│  Installation complete! Next steps:                        │"
  echo "├─────────────────────────────────────────────────────────────┤"
  echo "│  1. Set your bot token and chat ID:                       │"
  echo "     Option A — env vars (recommended):                      │"
  echo "       export A2A_TELEGRAM_BOT_TOKEN=\"123456789:ABC...\"    │"
  echo "       export A2A_OWNER_CHAT_ID=\"111222333\"               │"
  echo "                                                             │"
  echo "     Option B — direct vault values:                          │"
  echo "       $HERMES_HOME_DIR/profiles/default/a2a/vault.yaml      │"
  echo "                                                             │"
  echo "  2. Enable the plugin in config.yaml:                       │"
  echo "     plugins:                                                │"
  echo "       enabled:                                              │"
  echo "         - hermes-a2a-v2                                     │"
  echo "                                                             │"
  echo "  3. Restart Hermes:                                         │"
  echo "     hermes restart                                          │"
  echo "                                                             │"
  echo "  4. Start a DM with your bot on Telegram, then verify:     │"
  echo "     hermes plugins                                          │"
  echo "                                                             │"
  echo "  5. Read the full setup guide:                              │"
  echo "     cat QUICKSTART.md                                       │"
  echo "└─────────────────────────────────────────────────────────────┘"
  echo ""
}

main "$@"

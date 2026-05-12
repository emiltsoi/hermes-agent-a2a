#!/bin/bash
set -e

HERMES_HOME="${HERMES_HOME:-$HOME/.hermes}"
PLUGIN_DIR="$HERMES_HOME/plugins/hermes-agent-a2a"

echo "=== Hermes A2A Plugin Installer ==="
echo ""

# Step 1: HERMES_HOME is already set above (default ~/.hermes)
echo "[1/9] Using HERMES_HOME=$HERMES_HOME"

# Step 2: Check Python >= 3.11
echo "[2/9] Checking Python version..."
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
Pymajor=$(python3 -c 'import sys; print(sys.version_info.major)')
Pyminor=$(python3 -c 'import sys; print(sys.version_info.minor)')
if [ "$Pymajor" -lt 3 ] || [ "$Pymajor" -eq 3 ] && [ "$Pyminor" -lt 11 ]; then
    echo "ERROR: Python 3.11+ required, found $PYVER"
    exit 1
fi

# Prefer python3 over python
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found"
    exit 1
fi
echo "      Python $PYVER — OK"

# Step 3: Create $PLUGIN_DIR
echo "[3/9] Creating $PLUGIN_DIR..."
mkdir -p "$PLUGIN_DIR"

# Step 4: Detect install method
echo "[4/9] Detecting install method..."
if [ -f "$PLUGIN_DIR/pyproject.toml" ]; then
    echo "      Found existing install — updating via pip install -e ."
    pip install -e "$PLUGIN_DIR" --quiet
else
    echo "      No existing install — cloning repo + pip install -e ."
    REPO_URL="https://github.com/your-org/hermes-agent-a2a.git"
    echo "      NOTE: Set INSTALL_REPO_URL to override the repo URL."
    if [ -n "$INSTALL_REPO_URL" ]; then
        REPO_URL="$INSTALL_REPO_URL"
    fi
    git clone "$REPO_URL" "$PLUGIN_DIR" --quiet 2>/dev/null || {
        echo "ERROR: git clone failed. Set INSTALL_REPO_URL or manually place files in $PLUGIN_DIR"
        exit 1
    }
    pip install -e "$PLUGIN_DIR" --quiet
fi
echo "      Installed — OK"

# Step 5: Create vault directory
echo "[5/9] Creating vault directory..."
PROFILE="${HERMES_PROFILE:-default}"
VAULT_DIR="$HERMES_HOME/profiles/$PROFILE/a2a"
mkdir -p "$VAULT_DIR"
echo "      Vault dir: $VAULT_DIR"

# Step 6: Copy vault.yaml.example → vault.yaml if not exists
echo "[6/9] Configuring vault..."
EXAMPLE_SRC="$PLUGIN_DIR/vault.yaml.example"
VAULT="$VAULT_DIR/vault.yaml"
if [ -f "$VAULT" ]; then
    echo "      vault.yaml already exists — skipping copy"
else
    if [ -f "$EXAMPLE_SRC" ]; then
        cp "$EXAMPLE_SRC" "$VAULT"
        echo "      Copied vault.yaml.example → vault.yaml"
    else
        echo "WARNING: vault.yaml.example not found at $EXAMPLE_SRC"
        echo "         Create $VAULT manually."
    fi
fi

# Step 7: Prompt for values (if vault.yaml exists and has empty placeholders)
if [ -f "$VAULT" ]; then
    echo "[7/9] Collecting vault values (or press Enter to leave blank)..."
    echo ""

    echo -n "  Telegram bot token (leave blank to skip): "
    read -s TELEGRAM_BOT_TOKEN || true
    echo ""

    echo -n "  Telegram chat ID (leave blank to skip): "
    read -s TELEGRAM_CHAT_ID || true
    echo ""

    echo -n "  Agent name (e.g. britney, linda): "
    read AGENT_NAME || true
    echo ""

    # Step 8: Write values to vault.yaml
    echo "[8/9] Writing values to vault.yaml..."
    if [ -n "$TELEGRAM_BOT_TOKEN" ]; then
        sed -i "s/bot_token: \"\"/bot_token: \"$TELEGRAM_BOT_TOKEN\"/" "$VAULT"
    fi
    if [ -n "$TELEGRAM_CHAT_ID" ]; then
        sed -i "s/chat_id: \"\"/chat_id: \"$TELEGRAM_CHAT_ID\"/" "$VAULT"
    fi
    if [ -n "$AGENT_NAME" ]; then
        sed -i "s/name: \"\"/name: \"$AGENT_NAME\"/" "$VAULT"
    fi
    echo "      vault.yaml updated"
else
    echo "[7-8/9] Skipped — vault.yaml not present"
fi

# Step 9: Print env vars reminder
echo ""
echo "[9/9] Verification..."
cd "$PLUGIN_DIR" && python3 -c "from src.plugin import _get_version; print(f'Plugin OK — version {_get_version()}')" 2>/dev/null && echo "      Plugin loads — OK" || echo "      Plugin load check failed — check installation"

echo ""
echo "=== Install complete ==="
echo ""
echo "Add to your profile .env:"
echo "  HERMES_HOME=$HERMES_HOME"
echo "  HERMES_PROFILE=$PROFILE"
echo ""
echo "Then restart hermes-agent and run 'a2a_list' to test."

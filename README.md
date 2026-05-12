# hermes-agent-a2a

Hermes Agent A2A communication plugin — vault-based, fleet-agnostic, self-configuring.

## What This Plugin Does

This is **not** a standalone Telegram bot. It layers on top of the standard Hermes Telegram gateway platform.

| What | Where |
|------|-------|
| Telegram message receive/send | Standard Hermes Telegram platform (`gateway/platforms/telegram.py`) |
| Bot token + chat_id resolution | This plugin (vault → env → config) |
| Token validation on boot | This plugin |
| Webhook route bootstrap | This plugin |

**You need both:**
- `telegram` in `platforms.enabled` in your config (standard gateway)
- `hermes-a2a-v2` in `plugins.enabled` (this plugin)

**And the gateway session injection patch** — see `scripts/gateway-session-inject.patch`. This is a one-line change to `gateway/run.py` that allows webhook-sourced sessions to route to Telegram DMs without being blocked by the user allowlist. Required for A2A Telegram routing to work.

## Features

- **Telegram-only (v1)** — works with the standard Hermes Telegram gateway platform
- **Vault resolution chain**: agent vault → profile vault → env vars → explicit config
- **Auto-source bootstrap**: webhook routes automatically filled with `source` block from vault
- **Schema validation**: required fields, type checking, sensible defaults — fail-fast on misconfiguration
- **Boot-time token validation**: Telegram bot token validated on every startup via `getMe` API

## Installation

### Option A — Clone and run the installer (recommended)

```bash
git clone https://github.com/emiltsoi/hermes-agent-a2a.git /tmp/hermes-a2a-v2
bash /tmp/hermes-a2a-v2/install.sh
```

### Option B — Pip install from source

Requires Python 3.10+ and a virtual environment:

```bash
git clone https://github.com/emiltsoi/hermes-agent-a2a.git
cd hermes-agent-a2a
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

> **Note:** On Ubuntu/Debian with system Python, pass `--break-system-packages` or use a venv. The installer script handles this automatically.

## Quick Start

**1. Create a vault file:**

```bash
mkdir -p ~/.hermes/profiles/default/a2a
```

Create `~/.hermes/profiles/default/a2a/vault.yaml`:

```yaml
platforms:
  telegram:
    bot_token: "123456789:ABCdefGhIJKlmNoPQRsTUVwxYZ"
    default_chat_id: "111222333"

defaults:
  platform: telegram
  chat_type: dm
  chat_id_resolver: default_chat_id
```

Or set env vars instead:

```bash
export A2A_TELEGRAM_BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRsTUVwxYZ"
export A2A_OWNER_CHAT_ID="111222333"
```

**2. Enable the plugin** in `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-a2a-v2
```

**3. Restart and verify:**

```bash
hermes restart
hermes plugins
```

## Configuration

The plugin is configured via `plugin.yaml` in the plugin directory. Runtime configuration is resolved through the vault chain:

| Priority | Source | Example |
|----------|--------|---------|
| 1 | Agent vault | `profiles/<agent>/a2a/vault.yaml` |
| 2 | Profile vault | `profiles/<profile>/a2a/vault.yaml` |
| 3 | Environment variables | `A2A_TELEGRAM_BOT_TOKEN`, `A2A_OWNER_CHAT_ID` |
| 4 | Explicit config | `config.yaml` (last resort) |

Set `vault: none` in explicit config to skip vault resolution entirely and force explicit config only.

## Plugin Structure

```
hermes-agent-a2a/
├── plugin.yaml          # Plugin descriptor
├── pyproject.toml       # Package metadata
├── install.sh           # Interactive installer
├── QUICKSTART.md        # Setup guide
├── scripts/
│   └── gateway-session-inject.patch  # Gateway patch (apply before use)
├── src/
│   ├── __init__.py     # Package entry point
│   ├── plugin.py        # Plugin class + on_boot
│   ├── identity.py      # Vault resolution chain
│   ├── bootstrap.py     # Auto-source bootstrap
│   ├── schema.py        # Config schema + defaults
│   ├── validators.py    # Boot-time health checks
│   ├── platforms/
│   │   ├── __init__.py
│   │   ├── base.py      # Platform abstraction
│   │   └── telegram.py  # Telegram API handler
├── vault/               # Example vault (not installed)
│   └── test-vault.yaml
├── templates/
│   └── agent-config.yaml
└── tests/
    ├── conftest.py
    └── test_*.py        # identity, bootstrap, schema, validators, plugin
```

## Credits

Forked from `@iamagenius00/hermes-a2a` — [original repository](https://github.com/iamagenius00/hermes-a2a) (non-functional, now removed). Fixed and adapted for the Hermes fleet architecture.

## License

MIT

# HermesA2A v2 — Quick Start

**HermesA2A v2** is a Hermes Agent plugin that enables peer-to-peer A2A (Agent-to-Agent) communication between Hermes agents over Telegram, Discord, and Matrix — no third-party relay required.

---

## What You Need

| Requirement | Details |
|---|---|
| Hermes Agent | v0.3.0 or later |
| Python | 3.10+ |
| A Telegram bot | Create via [@BotFather](https://t.me/BotFather) |
| Bot token | From BotFather — looks like `123456789:ABCdef...` |

---

## Installation

### Option A — Clone and install (recommended for development)

```bash
git clone https://github.com/emiltsoi/hermes-agent-a2a.git /tmp/hermes-a2a-v2
bash /tmp/hermes-a2a-v2/install.sh
```

### Option B — One-liner (once the repo is public)

```bash
curl -fsSL https://raw.githubusercontent.com/emiltsoi/hermes-agent-a2a/main/install.sh | bash
```

The installer will:
1. Symlink the plugin into `~/.hermes/plugins/hermes-a2a-v2`
2. Create a vault entry at `~/.hermes/vault/agents.yaml`
3. Auto-enable the plugin in `~/.hermes/config.yaml`

---

## Configuration

### 1. Add your bot token to the vault

VaultResolver expects the vault at `profiles/<profile>/a2a/vault.yaml`. For the default profile:

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

Or use environment variables (recommended for production):

```bash
export A2A_TELEGRAM_BOT_TOKEN="123456789:ABCdefGhIJKlmNoPQRsTUVwxYZ"
export A2A_OWNER_CHAT_ID="111222333"
```

The vault file also supports `${ENV_VAR}` interpolation — the placeholders above are resolved from env at runtime.

> **Finding your chat ID:** Start a DM with your bot, then visit:
> `https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates`
> Look for `"id":111222333` in the JSON response — that's your numeric chat ID.

**2. Apply the gateway session injection patch (required for A2A Telegram routing)**

The plugin requires a small gateway patch so webhook-sourced A2A sessions can route to Telegram DMs without being blocked by the user allowlist. This is a one-time patch against your Hermes Agent installation:

```bash
# Run from your hermes-agent source directory
cd ~/.hermes/hermes-agent
patch -p1 < /path/to/hermes-a2a-v2/scripts/gateway-session-inject.patch
```

If your Hermes Agent is installed via pip rather than cloned source:

```bash
# Find the gateway source
python3 -c "import hermes_agent.gateway.run as m; print(m.__file__)"
# Then patch it directly
patch -p1 < /path/to/hermes-a2a-v2/scripts/gateway-session-inject.patch "$(python3 -c "import hermes_agent.gateway.run as m; print(m.__file__)")"
```

> **What this patch does:** Adds an allowlist bypass for sessions where `source.user_id` starts with `"webhook:"`. These sessions are HMAC-authenticated at the webhook level — the allowlist check is redundant and blocks A2A Telegram routing.

### 3. Configure the plugin

In `~/.hermes/config.yaml`:

```yaml
plugins:
  enabled:
    - hermes-a2a-v2
```

### 4. Restart Hermes

```bash
hermes restart
```

### 5. Verify

```bash
hermes plugins
```

You should see `hermes-a2a-v2` in the list.

---

## Agent Card Discovery

Each agent publishes its capabilities via an **Agent Card** — a JSON document at `/.well-known/agent.json` on the agent's webhook server.

To discover and call another agent:

```bash
hermes a2a discover --url https://your-agent.example.com
hermes a2a call <agent-name> --message "Hello from A2A"
```

---

## Architecture

```
Resolution priority (first valid wins):
  1. Agent-level vault   — profiles/<agent>/a2a/vault.yaml (highest priority)
  2. Profile-level vault — profiles/<profile>/a2a/vault.yaml
  3. Environment vars   — A2A_TELEGRAM_BOT_TOKEN, A2A_OWNER_CHAT_ID (deployment override)
  4. Explicit config    — bot_token in config.yaml (last resort)
```

Platforms: Telegram (primary)
Protocol: HTTP POST to agent webhooks

---

## Troubleshooting

**Bot not responding after install?**
1. Confirm vault has real values (not `${...}` placeholders):
   ```bash
   cat ~/.hermes/profiles/default/a2a/vault.yaml
   ```
2. Or confirm env vars are set:
   ```bash
   echo $A2A_TELEGRAM_BOT_TOKEN $A2A_OWNER_CHAT_ID
   ```
3. Check `hermes logs --level debug` for A2A identity errors

**A2A identity error: no valid bot token found?**
- Vault file missing or has unresolved `${...}` placeholders
- Set `A2A_TELEGRAM_BOT_TOKEN` env var, or replace placeholders with real values

**A2A identity error: bot_token appears to be an unresolved env var?**
- Vault file has `${A2A_TELEGRAM_BOT_TOKEN}` but the env var is not set
- Either set the env var, or replace `${A2A_TELEGRAM_BOT_TOKEN}` with the actual token

**A2A identity error: no default_chat_id found?**
- Set `A2A_OWNER_CHAT_ID` env var or add `default_chat_id` to vault

**Plugin not loading after restart?**
- Confirm plugin is in `plugins.enabled` in `config.yaml`
- Confirm bot token and chat ID are valid

**A2A calls failing between agents?**
- Confirm both agents have mutually reachable webhook URLs (not `localhost`)
- Check `gateway.log` for HTTP errors

---

## Uninstall

```bash
rm ~/.hermes/plugins/hermes-a2a-v2
# Remove vault: profiles/default/a2a/vault.yaml
# Remove from plugins.enabled in ~/.hermes/config.yaml
hermes restart
```

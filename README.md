# Hermes A2A Plugin

A self-contained A2A (Agent-to-Agent) protocol plugin for [Hermes Agent](https://github.com/your-org/hermes-agent). No fleet paths, no hardcoded identities, no gateway patches.

---

## What this plugin does

| Capability | Description |
|---|---|
| **A2A HTTP Server** | Runs an HTTP server (`/a2a/*`) on a configurable port. Agents discover each other via the A2A discovery protocol. |
| **Vault Identity** | Reads Telegram bot tokens, chat IDs, and agent names from `vault.yaml` via `VaultResolver`. Profile-relative — works for any profile. |
| **4 Tools** | `a2a_discover`, `a2a_list`, `a2a_call`, `a2a_telegram` — wired into Hermes Agent when the plugin is installed. |
| **Webhook Routing** | `pre_gateway_dispatch` hook intercepts Telegram updates and routes them to the A2A server when addressed to a known agent. |

---

## What Hermes Agent provides

When this plugin is installed, these tools become available to your agent automatically:

- **`a2a_discover`** — Probe a host for A2A protocol support
- **`a2a_list`** — List agent capabilities from the local A2A server
- **`a2a_call`** — Send a task to a remote agent via A2A (supports mode 2 and 3)
- **`a2a_telegram`** — Send a message via a Telegram bot using vault credentials

---

## Install

### Option A: Automated installer (recommended)

```bash
curl -sSL https://raw.githubusercontent.com/your-org/hermes-agent-a2a/main/install.sh | bash
```

### Option B: pip install from source

```bash
pip install -e /path/to/hermes-agent-a2a
```

### Option C: ClawHub

```bash
hermes plugin install hermes-agent-a2a
```

---

## Configure

The plugin stores identity in a **vault file** — no hardcoded tokens anywhere.

1. Copy the example vault:

   ```bash
   cp vault.yaml.example ~/.hermes/profiles/<profile>/a2a/vault.yaml
   ```

2. Edit `vault.yaml`:

   ```yaml
   telegram:
     bot_token: "your-bot-token-from-botfather"
     chat_id: "your-telegram-chat-id"

   agent:
     name: "britney"   # your agent's name
   ```

3. Set your profile in the Hermes Agent environment:

   ```bash
   HERMES_PROFILE=<profile>  # defaults to "default"
   ```

---

## Usage

### Discover a remote agent

```
a2a_discover host=agent.example.com port=8000
```

### List local agent capabilities

```
a2a_list
```

### Call a remote agent (mode 2: streaming)

```
a2a_call agent_id=agent-123 method=tasks/send mode=2 params={"message": {"role": "user", "content": "hello"}}
```

### Call a remote agent (mode 3: single response)

```
a2a_call agent_id=agent-123 method=tasks/send mode=3 params={"message": {"role": "user", "content": "hello"}}
```

### Send a Telegram message

```
a2a_telegram message="Hello from the A2A plugin!"
```

---

## No gateway patch

This plugin is **fully self-contained**. It does not modify the Hermes Agent gateway or core server. All A2A functionality lives in:

- `src/plugin.py` — plugin registration
- `src/server.py` — A2A HTTP server
- `src/tools.py` — 4 tool handlers
- `src/identity.py` — `VaultResolver` (vault.yaml-based identity)
- `src/hooks.py` — Telegram webhook hook
- `src/bootstrap.py` — capability bootstrap

Install, configure, restart — done.

# A2A v3 — Standalone Self-Contained Plugin

**Version:** 3.0.0
**Status:** SPEC — drafting
**Notion:** `35e29647-6d73-81e0-bdd7-e1f0a9022931`

---

## 1. Goals

- **Single plugin** exposing `a2a_telegram`, `a2a_call`, `a2a_discover`, `a2a_list` tools
- **Merges** fleet-local `a2a` v1 (tools, server, hooks) + `hermes-a2a-v2` v2 (vault/identity/bootstrap)
- **No gateway patch required** — plugin manages its own routing via `pre_llm_call` / `post_llm_call` hooks
- **Fleet-agnostic** — no hardcoded `/home/emil` paths; all paths derived from `HERMES_HOME` env
- **Installs from GitHub** as a standalone `pip install`able plugin via ClawHub or direct GitHub URL

---

## 2. What Exists Today

### v1: `~/.hermes/fleet/plugins/a2a/` (fleet-local, 476 lines `__init__.py`)
- `tools.py` (708 lines) — `handle_discover`, `handle_call`, `handle_list`, `handle_telegram`
  - Hardcoded paths: `/home/emil/.hermes/hermes-agent/venv/bin/python`, `/home/emil/.hermes/profiles`
  - Loads identities from NAS vault path + fleet config
- `server.py` (463 lines) — `ThreadingHTTPServer`, `TaskQueue`, `A2AServer`
  - No hardcoded paths — uses env vars throughout
- `schemas.py` (129 lines) — tool schemas (A2A_DISCOVER, A2A_CALL, etc.)
- `persistence.py` (110 lines) — `save_exchange`
- `security.py` (109 lines) — `RateLimiter`, `audit`, `filter_outbound`, `sanitize_inbound`
- `__init__.py` (476 lines) — registers tools + hooks + command; starts HTTP server; imports `a2a_core` from v2 (fails silently)

### v2: `~/.hermes/plugins/hermes-a2a-v2/src/` (GitHub, `2.0.1`)
- `identity.py` — `VaultResolver` with 3-tier resolution (agent vault → profile vault → env)
- `bootstrap.py` — `AutoSourceBootstrap` for webhook route auto-fill
- `validators.py` — `BootValidator` (token + chat_id checks)
- `schema.py` — config validation
- `plugin.py` — lifecycle hooks (`on_boot`, `on_shutdown`)
- `platforms/telegram.py` — `TelegramHandler` (send_message, get_me)
- **No tool implementations** — only infrastructure

---

## 3. Architecture

```
src/
├── __init__.py              # Empty — src/ is a namespace for package-dir
├── plugin.py                 # Hermes plugin: register(), on_boot(), hooks
├── server.py                 # A2A HTTP server (ported from v1, fleet-agnostic)
├── tools.py                  # Tool handlers (ported from v1, fleet-agnostic)
├── schemas.py                # Tool schemas (ported from v1)
├── identity.py               # VaultResolver (ported from v2)
├── bootstrap.py              # AutoSourceBootstrap (ported from v2)
├── validators.py            # BootValidator (ported from v2)
├── schema.py                 # Config validation (ported from v2)
├── persistence.py            # save_exchange (ported from v1)
├── security.py               # RateLimiter, audit, PII filter (ported from v1)
├── hooks.py                  # pre_llm_call, post_llm_call, pre_gateway_dispatch
├── _mode2_worker.py         # Mode 2 ephemeral worker subprocess
└── platforms/
    ├── __init__.py
    ├── base.py
    └── telegram.py           # TelegramHandler (ported from v2)
```

### Key Design Decisions

**Fleet-agnostic paths:** All `/home/emil` hardcoded paths replaced with `HERMES_HOME` env var derivation. `HERMES_HOME` is the canonical base for all profile, vault, and venv paths.

**Vault = identity source:** The v2 `VaultResolver` replaces v1's dual vault+config identity loading. Single resolution chain: agent vault → profile vault → env vars. No NAS dependency.

**Plugin is A2A-server AND tool provider:** Plugin registers HTTP server (port 8081 default) AND tool handlers. This is the complete stack — no separate plugin needed.

**No `a2a_core` module:** v1's attempt to share identity via `a2a_core` is removed. All shared state lives in the plugin's own modules.

**Mode 2 worker path:** Mode 2 spawns a subprocess using the Hermes venv python at `{hermes_home}/hermes-agent/venv/bin/python`. Derived from `HERMES_HOME`, not hardcoded.

---

## 4. Tool Specifications

### `a2a_telegram`
Send a Telegram DM to a mesh peer.
- `agent` (required): peer name — resolves `chat_id` from vault
- `message` (required): message text
- `cta` (optional): `reply` | `ack` | `nop` (default: `reply`)
- `ref` (optional): message ID to reply to
- **Behavior:** Looks up `telegram_bot_token` from vault/platform config; sends via Telegram Bot API; strips Mode 1 header from displayed text

### `a2a_discover`
Fetch another agent's capability card.
- `url` OR `name` (one required)
- **Behavior:** GET `/.well-known/agent.json` from target; returns `{agent_name, description, url, version, skills[], capabilities{}}`

### `a2a_call`
Send a task to a remote agent, await response.
- `url` OR `name` (one required)
- `message` (required): task description
- `worker_at` (optional): `caller` (Mode 2) | `target` (Mode 3) | omit (Mode 1 HTTP polling)
- `task_id` (optional)
- `intent` (optional): `consultation` | `action_request` | etc.
- `expected_action` (optional): `reply` | `forward` | `acknowledge`
- **Behavior:**
  - Mode 1 (default): POST to target's A2A server, poll until response
  - Mode 2: spawn local subprocess with target's profile
  - Mode 3: POST to target with `worker_at=target`; target runs local worker and returns result synchronously

### `a2a_list`
List all known agents and their A2A endpoints.
- No required args
- **Behavior:** Reads from vault + config; returns `[{name, url, auth_token, description}]`

---

## 5. Vault Structure

`profiles/<profile>/a2a/vault.yaml`:
```yaml
platforms:
  telegram:
    bot_token: "..."       # Telegram bot token
    default_chat_id: "..."  # Default recipient chat_id

defaults:
  platform: telegram
  chat_type: dm
  chat_id: "..."

# Agent registry (used by a2a_list, a2a_call name resolution)
agents:
  britney:
    a2a_url: "http://<host>:41812"
    auth_token: "..."
    description: "Principal SWE"
  linda:
    a2a_url: "http://<host>:41811"
    auth_token: "..."
    description: "Software Architect"
```

Vault resolution chain (unchanged from v2):
1. `HERMES_HOME/profiles/<agent>/a2a/vault.yaml` — agent's own vault
2. `HERMES_HOME/profiles/<profile>/a2a/vault.yaml` — current profile's vault
3. Environment variables: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `A2A_AGENT_NAME`, etc.

---

## 6. Install / Setup

```bash
# Option A: ClawHub
hermes plugins install https://github.com/emiltsoi/hermes-agent-a2a

# Option B: pip
pip install git+https://github.com/emiltsoi/hermes-agent-a2a.git

# Then set required env vars:
export TELEGRAM_BOT_TOKEN="..."
export TELEGRAM_CHAT_ID="..."        # your Telegram chat ID
export A2A_AGENT_NAME="linda"        # your agent name
export A2A_ENABLED=true
```

No gateway patch. No webhook route configuration required (plugin self-configures).

---

## 7. Hardcoded Path Replacements

| v1 location | v1 hardcoded path | v3 replacement |
|---|---|---|
| `tools.py:_handle_call_mode2` | `/home/emil/.hermes/hermes-agent/venv/bin/python` | `{hermes_home}/hermes-agent/venv/bin/python` |
| `tools.py:_handle_call_mode2` | `/home/emil/.hermes/profiles` | `{hermes_home}/profiles` |
| `tools.py:_load_identity_from_vault` | `/tmp/nas/emiltsoi/Agents/vault` | `VaultResolver` (env-derived) |
| `__init__.py:_find_hermes_a2a_v2_src` | Hardcoded candidate paths | Removed (no a2a_core) |
| `__init__.py:_build_v1_identities` | NAS vault path + fleet config | VaultResolver |

---

## 8. Not Included from v1

- `__init__.py`'s `_get_a2a_core` import logic — gone
- v1's hardcoded `_find_hermes_a2a_v2_src` path candidates — gone
- `a2a_core` registration attempt — gone
- Fleet config `a2a.agents[]` merge — replaced by vault-based registry

---

## 9. Testing Strategy

- Port existing tests from both v1 and v2
- New tests: fleet-agnostic path resolution, vault loading, Mode 2/3 with temp HERMES_HOME
- 39+ tests target
- No PII in test fixtures (chat ID `123456789` placeholder)

---

## 10. Version Bump

- `pyproject.toml`: `version = "3.0.0"`
- `plugin.yaml`: `version = "3.0.0"`
- GitHub tag: `v3.0.0`

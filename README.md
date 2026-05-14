# Hermes Agent A2A

`hermes-agent-a2a` is the A2A toolset plugin for Hermes Agent. It gives Hermes profiles a local A2A HTTP server plus outbound tools for Hermes fleet agents and external A2A-compatible agents.

## Capabilities

| Capability | Tools / Files | Purpose |
|---|---|---|
| Agent discovery | `a2a_discover` | Fetch an Agent Card by registry name or direct URL. Can auto-register external agents. |
| Protocol tasks | `a2a_send_protocol_task` | Send JSON-RPC `tasks/send` and poll `tasks/get`. |
| Hermes local workers | `a2a_run_local_agent_task` | Run another local Hermes profile as an ephemeral worker. |
| Hermes remote workers | `a2a_run_remote_agent_task` | Ask a remote Hermes A2A server to run its own ephemeral worker. |
| Session relay | `a2a_send_session_message` | Send one-way through Hermes gateway/session routing and return delivery status. |
| Registry | `~/.hermes/fleet/a2a/agents/<name>/identity.yaml` | Stores transport URLs and auth metadata. |
| Help | `a2a_help` | In-band help for protocol, workers, sessions, external agents, security, and troubleshooting. |

## Current toolset

The plugin registers the `a2a` toolset with these tools:

- `a2a_help`
- `a2a_discover`
- `a2a_list`
- `a2a_send_protocol_task`
- `a2a_cancel_protocol_task`
- `a2a_run_local_agent_task`
- `a2a_run_remote_agent_task`
- `a2a_send_session_message`

`a2a_send_session_message` is intentionally one-way: it delivers into the target Hermes session/gateway and returns relay status only. Use `a2a_send_protocol_task` when you need a pollable A2A task response.

## Install

### Clone into Hermes plugins

```bash
git clone https://github.com/emiltsoi/hermes-agent-a2a.git ~/.hermes/plugins/hermes-agent-a2a
python3 -m pip install -e ~/.hermes/plugins/hermes-agent-a2a
```

### Or run the installer

```bash
INSTALL_REPO_URL=https://github.com/emiltsoi/hermes-agent-a2a.git \
  bash <(curl -sSL https://raw.githubusercontent.com/emiltsoi/hermes-agent-a2a/main/install.sh)
```

## Profile configuration

A minimal profile config is provided at:

```text
templates/agent-config.yaml
```

Enable the plugin in your Hermes profile:

```yaml
plugins:
  enabled:
    - hermes-agent-a2a

a2a:
  enabled: true
  vault: auto
```

The `templates/` folder is still useful: it is the canonical minimal profile config template for new Hermes profiles using this plugin.

## Identity registry

Hermes fleet identities live under:

```text
~/.hermes/fleet/a2a/agents/<agent-name>/identity.yaml
```

Example external identity:

```yaml
id: external-demo
name: External Demo
external: true
transports:
  a2a_rpc:
    protocol: google-a2a
    url: https://external.example/a2a/rpc
    auth:
      type: api_key
      header: X-API-Key
      value_env: EXTERNAL_DEMO_A2A_KEY
  agent_card:
    protocol: google-a2a-agent-card
    url: https://external.example
    path: /.well-known/agent.json
    auth:
      type: api_key
      header: X-API-Key
      value_env: EXTERNAL_DEMO_A2A_KEY
```

Use environment variables for secrets. Do not store raw third-party API keys in identity files.

## External A2A agent onboarding

Start with discovery:

```text
a2a_discover(
  url="https://external.example",
  agent_card_path="/.well-known/agent.json",
  auth_type="api_key",
  auth_header="X-API-Key",
  auth_value="runtime-secret"
)
```

Auto-register the external agent:

```text
a2a_discover(
  url="https://external.example",
  agent_card_path="/.well-known/agent.json",
  auth_type="api_key",
  auth_header="X-API-Key",
  auth_value="runtime-secret",
  register=True,
  register_as="external-demo",
  rpc_url="https://external.example/a2a/rpc",
  auth_value_env="EXTERNAL_DEMO_A2A_KEY"
)
```

Then call by name:

```text
a2a_send_protocol_task(
  name="external-demo",
  message="Hello from Hermes"
)
```

## Hermes worker modes

Use protocol tasks for external A2A agents. Use worker tools only for Hermes-managed agents:

```text
a2a_run_local_agent_task(name="agent1", message="Work locally", timeout=300)
a2a_run_remote_agent_task(name="agent1", message="Work on your host", timeout=300)
```

## Hermes session routing requirement

`a2a_send_session_message` is a one-way Hermes session relay. It posts webhook text to the target Hermes agent; the target agent's `config.yaml` must route inbound webhook text into the desired platform/session.

Target profiles need plugin/toolset activation plus gateway webhook/session routing. Exact gateway keys may vary by Hermes version, but the required shape is:

```yaml
toolsets:
  - a2a

plugins:
  enabled:
    - hermes-agent-a2a

gateway:
  webhook:
    enabled: true
    target_session: default
    deliver_extra: true
```

The target identity also needs a webhook transport so peers know where to deliver:

```yaml
transports:
  hermes_webhook:
    url: https://target.example/hermes/webhook
    auth:
      type: hmac
      secret_env: TARGET_HERMES_WEBHOOK_SECRET
```

Telegram-backed session routing example:

```yaml
platforms:
  webhook:
    enabled: true
    extra:
      host: 0.0.0.0
      port: 8644
      secret: ${TARGET_HERMES_WEBHOOK_SECRET}
      routes:
        a2a_trigger:
          secret: ${TARGET_HERMES_WEBHOOK_SECRET}
          prompt: "{text}"
          deliver: telegram
          deliver_extra:
            chat_id: "<TELEGRAM_CHAT_ID>"
          target_session: "telegram:dm:<TELEGRAM_CHAT_ID>"
          source:
            platform: telegram
            chat_type: dm
            chat_id: "<TELEGRAM_CHAT_ID>"
            user_id: "<TELEGRAM_USER_ID>"
            user_name: "<TELEGRAM_DISPLAY_NAME>"
```

Without this routing, `a2a_send_session_message` may reach the webhook but not land in the intended Hermes session. Use `a2a_send_protocol_task` when you need a pollable A2A task result.

## Runtime environment

Common variables:

| Variable | Purpose |
|---|---|
| `HERMES_HOME` | Hermes root or profile path. Defaults to `~/.hermes`. |
| `A2A_AGENT_NAME` | Current agent/profile name. |
| `A2A_VAULT_PATH` | Fleet registry root. Defaults to `$HERMES_HOME/fleet` or root-derived equivalent. |
| `A2A_HOST` | A2A server bind host. Defaults to `127.0.0.1`. |
| `A2A_PORT` | A2A server port. Defaults to `8081`. |
| `A2A_AUTH_TOKEN` | Optional inbound bearer token for this server. |
| `A2A_REQUIRE_AUTH` | Set `true` to reject unauthenticated inbound requests. |

## Development checks

```bash
python3 -m py_compile hermes_agent_a2a/*.py
python3 -m pytest
```

## Repository layout

```text
hermes_agent_a2a/
  plugin.py       # plugin registration and server lifecycle
  server.py       # inbound A2A JSON-RPC server
  tools.py        # outbound tool handlers
  identity.py     # identity registry and transport normalization
  hooks.py        # Hermes gateway/LLM hooks
  security.py     # inbound filtering, redaction, audit, rate limiting
  persistence.py  # exchange persistence
  validators.py   # config validation helpers
templates/
  agent-config.yaml
```

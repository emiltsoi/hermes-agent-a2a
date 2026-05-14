# Quickstart — Hermes Agent A2A

This guide gets one Hermes profile running the `a2a` toolset and shows how to onboard an external A2A agent.

## 1. Install

```bash
git clone https://github.com/emiltsoi/hermes-agent-a2a.git ~/.hermes/plugins/hermes-agent-a2a
python3 -m pip install -e ~/.hermes/plugins/hermes-agent-a2a
```

## 2. Enable the plugin

Copy the template or add the same keys to your profile config:

```bash
mkdir -p ~/.hermes/profiles/default
cp ~/.hermes/plugins/hermes-agent-a2a/templates/agent-config.yaml \
   ~/.hermes/profiles/default/config.yaml
```

Minimal config:

```yaml
plugins:
  enabled:
    - hermes-agent-a2a

a2a:
  enabled: true
  vault: auto
```

## 3. Create or verify your local identity

Fleet identities are read from:

```text
~/.hermes/fleet/a2a/agents/<agent-name>/identity.yaml
```

For an external agent, copy the example and edit URLs/auth env var names:

```bash
mkdir -p ~/.hermes/fleet/a2a/agents/external-demo
cp ~/.hermes/plugins/hermes-agent-a2a/identity.yaml.example \
   ~/.hermes/fleet/a2a/agents/external-demo/identity.yaml
```

## 4. Restart Hermes Agent

Restart the Hermes profile that has `hermes-agent-a2a` enabled.

Useful environment variables:

```bash
export HERMES_HOME=~/.hermes/profiles/default
export A2A_AGENT_NAME=default
export A2A_VAULT_PATH=~/.hermes/fleet
export A2A_REQUIRE_AUTH=true
export A2A_AUTH_TOKEN='change-me'
```

## 5. Verify tools

Ask the agent or tool runner for:

```text
a2a_help(topic="overview")
a2a_list()
```

## 6. Discover an external A2A agent

```text
a2a_discover(
  url="https://external.example",
  agent_card_path="/.well-known/agent.json",
  auth_type="api_key",
  auth_header="X-API-Key",
  auth_value="runtime-secret"
)
```

## 7. Auto-register the external agent

Prefer env-var backed secrets:

```bash
export EXTERNAL_DEMO_A2A_KEY='runtime-secret'
```

Then register:

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

## 8. Send a protocol task

```text
a2a_send_protocol_task(
  name="external-demo",
  message="Hello from Hermes"
)
```

## 9. Hermes-only worker tools

Use these only for Hermes-managed agents, not generic external A2A agents:

```text
a2a_run_local_agent_task(name="agent1", message="Analyze this", timeout=300)
a2a_run_remote_agent_task(name="agent1", message="Analyze this on your host", timeout=300)
```

## 10. Configure session routing for `a2a_send_session_message`

`a2a_send_session_message` is a one-way Hermes session relay. The receiving Hermes profile must route inbound webhook text to a configured session/platform in its `config.yaml`.

Enable the plugin/toolset in the target profile:

```yaml
toolsets:
  - a2a

plugins:
  enabled:
    - hermes-agent-a2a
```

The target agent identity must also expose a webhook transport:

```yaml
transports:
  hermes_webhook:
    url: https://target.example/hermes/webhook
    auth:
      type: hmac
      secret_env: TARGET_HERMES_WEBHOOK_SECRET
```

Telegram-backed route example:

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

Without this routing, the webhook can receive the message but the target Hermes gateway may not deliver it into the desired session.

The receiving Hermes gateway must support authenticated webhook-to-session routing:

- `platforms.webhook.extra.routes.<route>.target_session`
- webhook source/session override through route `source`
- allowlist bypass for HMAC-authenticated `webhook:` sources

The A2A plugin owns fleet identity lookup, HMAC signing, A2A envelopes, cancellation, and sender-side Telegram echo. Hermes core should only provide generic webhook/session routing primitives.

## Troubleshooting

```text
a2a_help(topic="troubleshooting")
a2a_help(topic="security")
a2a_help(topic="external_requirements")
```

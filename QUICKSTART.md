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

## Troubleshooting

```text
a2a_help(topic="troubleshooting")
a2a_help(topic="security")
a2a_help(topic="external_requirements")
```

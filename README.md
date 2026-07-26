# Hermes Agent A2A

> **Status: Active**
>
> This plugin is the current Google A2A implementation for the Hermes fleet. It provides standard A2A operations: discovery, Agent Cards, JSON-RPC tasks, SSE streaming, push notifications, and local/remote Hermes worker tasks.
>
> Fleet session relay (the old `a2a_send_session_message` mesh feature) has moved to [hermes-mesh](https://github.com/emiltsoi/hermes-mesh).


`hermes-agent-a2a` is the A2A HTTP/JSON-RPC protocol plugin for Hermes fleet agents. It exposes a local A2A server, HMAC request signing, SSE streaming, push notifications, and fleet metrics — all Hermes-specific, not fleet-agnostic.

## Capabilities

| Capability | Tools / Files | Purpose |
|---|---|---|
| Agent discovery | `a2a_discover` | Fetch an Agent Card by registry name or direct URL. Can auto-register external agents. |
| **Registry announcement** | `a2a_announce` | Announce this agent to a shared A2A registry so other agents can discover it. Reads `A2A_REGISTRY_URL` env var. |
| Protocol tasks | `a2a_send_protocol_task` | Send JSON-RPC `SendMessage` and poll `GetTask`. |
| Hermes local workers | `a2a_run_local_agent_task` | Run another local Hermes profile as an ephemeral worker with Hermes A2A metadata. |
| Hermes remote workers | `a2a_run_remote_agent_task` | Ask a remote Hermes A2A server to run its own ephemeral worker. |
| Metrics | `a2a_get_metrics` | Get current A2A plugin metrics (uptime, task counts, queue depth). |
| SSE streaming | `SubscribeToTask` | Stream task state transitions via Server-Sent Events. Agent Card: `streaming: true`. |
| Push notifications | `POST /tasks/{id}/pushNotificationConfigs` | Register webhook URL for push delivery on task state changes. HMAC-SHA256 signed. Agent Card: `pushNotifications: true`. |
| Registry | `~/.hermes/fleet/a2a/agents/<name>/identity.yaml` | Stores transport URLs and auth metadata. |
| Help | `a2a_help` | In-band help for protocol, workers, sessions, external agents, security, and troubleshooting. |

## Current toolset

The plugin registers the `a2a` toolset with these tools:

- `a2a_help`
- `a2a_discover`
- `a2a_announce`
- `a2a_list`
- `a2a_send_protocol_task`
- `a2a_cancel_protocol_task`
- `a2a_run_local_agent_task`
- `a2a_run_remote_agent_task`
- `a2a_get_metrics`

`a2a_cancel_protocol_task` sends standard A2A `CancelTask` when `name` or `url` is provided. If called with only `task_id`, it attempts to cancel a locally registered Hermes worker subprocess.

## Install

### From PyPI (recommended)

```bash
python3 -m pip install hermes-agent-a2a
```

### From source

```bash
git clone https://github.com/emiltsoi/hermes-agent-a2a.git ~/.hermes/plugins/hermes-agent-a2a
python3 -m pip install -e ~/.hermes/plugins/hermes-agent-a2a
```

For development or custom branch installs, use the installer script:

```bash
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

Both worker tools return task-shaped results with Hermes metadata. Local workers use `route=worker`, `execution=local_subprocess`, and `isolation=local_profile`; remote workers use `execution=remote_subprocess` and `isolation=target_profile`.

### Requirements for `a2a_run_remote_agent_task`

**Shared filesystem (same `HERMES_HOME`):**
The target agent's A2A server process must have a `HERMES_HOME` environment variable that points to a filesystem accessible from the target machine — typically the same NFS-mounted home directory used by all fleet agents. The spawned worker runs on the target's filesystem using the target's `HERMES_HOME/profiles/{name}/` to locate the agent's profile and venv Python. If the target machine cannot reach that path (different user, different home, isolated machine), the spawn fails.

**Same path resolution on target:**
The target's profile directory must exist and be reachable at the path the target's `HERMES_HOME` resolves to. Cross-machine deployments where the caller and target have different filesystem layouts require a shared network mount (NFS, EFS, etc.) or a container image with a pre-mounted profile path.

These constraints do not apply to `a2a_send_protocol_task`, which communicates with external A2A agents over HTTP without spawning local workers.

## List registered agents

Use `a2a_list` to see all configured agents in the fleet registry:

```text
a2a_list()
```

Returns agent names, URLs, and descriptions. This is useful for verifying which external agents are available for protocol tasks.

## Cancel tasks

Use `a2a_cancel_protocol_task` to cancel running tasks:

For remote A2A agents:

```text
a2a_cancel_protocol_task(
  name="external-demo",
  task_id="task-123"
)
```

For local Hermes worker subprocesses:

```text
a2a_cancel_protocol_task(task_id="local-task-123")
```

When called with only `task_id`, it attempts to cancel a locally registered Hermes worker subprocess. When `name` or `url` is provided, it also sends a standard A2A `CancelTask` to the remote agent. The result includes `local_canceled` indicating whether local cancellation succeeded.

## Google A2A v1.0 Compliance

`hermes-agent-a2a` implements the [Google A2A](https://github.com/google/A2A) HTTP/JSON-RPC protocol specification (a2a.proto v1.0).

| Spec Item | Status | Details |
|-----------|--------|---------|
| JSON-RPC 2.0 | ✅ | All requests/responses conform to JSON-RPC 2.0 |
| Method names | ✅ | `SendMessage`, `GetTask`, `CancelTask`, `SubscribeToTask` per a2a.proto |
| AgentCard schema | ✅ | `AgentProvider`, `AgentSkill`, `AgentCapabilities`, `AgentInterface` per spec |
| Task state machine | ✅ | Canonical states: submitted, working, input_required, completed, failed, canceled, rejected |
| Role enum | ✅ | `Role.ROLE_USER = 1` (integer) per a2a.proto:245-252 |
| Parts oneof | ✅ | `parts: [{"text": "..."}]` without type wrapper per spec |
| Push notification REST | ✅ | `POST/GET/DELETE /tasks/{id}/pushNotificationConfigs` |
| SSE streaming | ✅ | `POST /message:stream` with Server-Sent Events |
| A2A-Version header | ✅ | All responses include `A2A-Version: 1.0` |
| Error codes | ✅ | `-32700`, `-32600`, `-32603`, `-38000` through `-38004` per spec |
| Idempotency keys | ✅ | 24h TTL, same-key/diff-payload returns `-38004` |
| SendMessageConfiguration | ✅ | `return_immediately`, `accepted_output_modes` accepted |

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
| `A2A_REGISTRY_URL` | Shared A2A registry URL for `a2a_announce`. Defaults to nothing (must be set to use announcement). |
| `A2A_REGISTRY_AUTH_TOKEN` | Bearer token for the shared A2A registry. |
**Metrics configuration:**

| Variable | Purpose |
|---|---|
| `A2A_METRICS_LOG_ENABLED` | Set `true` to enable periodic metrics logging. Defaults to `false`. |
| `A2A_METRICS_LOG_INTERVAL` | Interval in seconds between metrics log entries. Defaults to `300` (5 minutes). |
| `A2A_METRICS_COMMAND_ENABLED` | Set `true` to enable the `/a2a_metrics` (or `/a2a-metrics`) Telegram slash command. Defaults to `false`. |

**Using the `/a2a_metrics` (or `/a2a-metrics`) Telegram command:**

To enable the metrics command, set the environment variable:

```bash
export A2A_METRICS_COMMAND_ENABLED=true
```

Then restart the Hermes gateway. Once enabled, send `/a2a_metrics` or `/a2a-metrics` via Telegram to get formatted metrics:

```
📊 A2A Metrics

⏱️ Uptime: 1h 30m

🔗 Webhook
Attempts: 150
✅ Success: 142 (94.67%)
❌ Failed: 8

📋 Tasks
Received: 150
Completed: 142
Canceled: 5
Failed: 3

📬 Queue: 0 pending
```

Both command forms work — `/a2a_metrics` and `/a2a-metrics` — due to gateway-side normalization.

## Architecture

The A2A plugin runs within the Hermes gateway process:

```
Hermes Gateway Process
├── Main gateway loop
├── A2A Plugin (loaded into gateway)
│   ├── A2A HTTP Server Thread (handles inbound JSON-RPC requests)
│   ├── Hooks (pre/post LLM call interception)
│   └── Tool handlers (outbound A2A operations)
└── Other gateway components
```

**Important: Logging is gateway-side, not server-side.** All plugin logging (including A2A server logs) uses the gateway's logger configuration. Log destination (stdout, file, aggregation service) is controlled by the gateway's logging configuration, not by the A2A plugin.

## Development checks

```bash
python3 -m py_compile hermes_agent_a2a/*.py
python3 -m pytest
```

## Repository layout

```
hermes_agent_a2a/
  plugin.py           # plugin registration and server lifecycle
  server.py           # inbound A2A JSON-RPC server
  tools.py            # outbound tool handlers
  identity.py         # identity registry and transport normalization
  hooks.py            # Hermes gateway/LLM hooks
  security.py         # inbound filtering, redaction, audit, rate limiting
  persistence.py       # exchange persistence
  validators.py       # config validation helpers
  a2a_spec/
    __init__.py       # spec models re-export
    agent_card.py     # AgentCard, AgentProvider, AgentSkill, AgentCapabilities, AgentInterface
    tasks.py          # TaskState, SendMessageConfiguration, role enum, payload builders
    push.py           # push notification config models
    hermes_ext.py     # Hermes metadata extensions
templates/
  agent-config.yaml
```

# Hermes Agent A2A

`hermes-agent-a2a` is the A2A HTTP/JSON-RPC protocol plugin for Hermes fleet agents. It exposes a local A2A server, HMAC request signing, SSE streaming, push notifications, Telegram session routing with sender echo, and fleet metrics — all Hermes-specific, not fleet-agnostic.

## Capabilities

| Capability | Tools / Files | Purpose |
|---|---|---|
| Agent discovery | `a2a_discover` | Fetch an Agent Card by registry name or direct URL. Can auto-register external agents. |
| Protocol tasks | `a2a_send_protocol_task` | Send JSON-RPC `tasks/send` and poll `tasks/get`. |
| Hermes local workers | `a2a_run_local_agent_task` | Run another local Hermes profile as an ephemeral worker with Hermes A2A metadata. |
| Hermes remote workers | `a2a_run_remote_agent_task` | Ask a remote Hermes A2A server to run its own ephemeral worker. |
| Session relay | `a2a_send_session_message` | ⚠️ Requires Hermes gateway patches — see [README § Hermes gateway compatibility](#hermes-gateway-compatibility) |
| Metrics | `a2a_get_metrics` | Get current A2A plugin metrics (uptime, webhook stats, task counts, queue depth). |
| SSE streaming | `tasks/sendSubscribe` | Stream task state transitions via Server-Sent Events. Agent Card: `streaming: true`. |
| Push notifications | `tasks/pushNotification/subscribe` | Register webhook URL for push delivery on task state changes. HMAC-SHA256 signed. Agent Card: `pushNotifications: true`. |
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
- `a2a_get_metrics`

`a2a_send_session_message` is intentionally one-way: it delivers into the target Hermes session/gateway and returns an A2A-shaped delivery ACK, not a semantic reply. Use `a2a_send_protocol_task` when you need a pollable A2A task response.

`a2a_cancel_protocol_task` sends standard A2A `tasks/cancel` when `name` or `url` is provided. If called with only `task_id`, it attempts to cancel a locally registered Hermes worker subprocess.

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

Both worker tools return task-shaped results with Hermes metadata. Local workers use `route=worker`, `execution=local_subprocess`, and `isolation=local_profile`; remote workers use `execution=remote_subprocess` and `isolation=target_profile`.

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

When called with only `task_id`, it attempts to cancel a locally registered Hermes worker subprocess. When `name` or `url` is provided, it also sends a standard A2A `tasks/cancel` to the remote agent. The result includes `local_canceled` indicating whether local cancellation succeeded.

## Hermes session routing requirement

`a2a_send_session_message` is a one-way Hermes session relay. It posts webhook text to the target Hermes agent; the target agent's `config.yaml` must route inbound webhook text into the desired platform/session.

Target profiles need plugin/toolset activation plus gateway webhook/session routing:

```yaml
toolsets:
  - a2a

plugins:
  enabled:
    - hermes-agent-a2a
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

Without this routing, `a2a_send_session_message` may reach the webhook but not land in the intended Hermes session. The tool returns `state=completed`, `delivery=delivered`, `reply_expected=false`, and Hermes metadata with `route=session`, `delivery=one_way`. Use `a2a_send_protocol_task` when you need a pollable A2A task result.

### Hermes gateway compatibility

> **⚠️ Mode 4 (session relay) requires gateway patches**

The `a2a_send_session_message` tool (mode 4) requires Hermes gateway patches that are not present in the standard public `hermes-agent` codebase. Modes 1-3 (protocol tasks, local/remote workers) are self-contained and work without any gateway patches.

**Mode 4 requires these gateway patches:**

- `platforms.webhook.extra.routes.<route>.target_session` to bind the webhook event to an existing platform session.
- webhook-sourced session authorization after HMAC validation (webhook allowlist bypass for `webhook:` user IDs).
- webhook source/platform override when routing into another platform session (`_platform` parameter in `build_source()`).

**Minimal gateway changes needed (+8 lines):**
- `gateway/platforms/base.py`: +2 lines for `_platform` override in `build_source()`
- `gateway/run.py`: +6 lines for webhook allowlist bypass

The plugin owns A2A identity resolution, HMAC request signing, message envelope construction, and sender-side Telegram visibility echo. The gateway should only provide generic authenticated webhook-to-session routing.

### Recommended cleanup path for Hermes core patches

The clean long-term split is:

- Keep generic gateway primitives upstream: authenticated webhook routes, `target_session`, cross-platform delivery, source/session overrides, idempotency, and rate limiting.
- Rename private/core-facing arguments such as `_platform` to a public `platform_override` or route-level `source.platform`.
- Replace A2A-specific gateway logic such as `_load_a2a_agents()` and `_deliver_a2a()` with plugin-owned registry and protocol calls.
- Avoid A2A-specific payload flags in core webhook code. Prefer generic route modes such as `execution: agent_async`, `response_mode: none`, or `delivery: platform_session`.
- Keep cancellation, A2A JSON-RPC, and fleet identity semantics inside this plugin.

Until those gateway primitives are upstreamed, deployments using session relay need a Hermes build that includes the webhook `target_session` and HMAC-authenticated webhook-session routing behavior shown above.

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

**Webhook delivery configuration:**

| Variable | Purpose |
|---|---|
| `A2A_WEBHOOK_DELIVERY_RETRIES` | Number of retry attempts for failed webhook delivery. Defaults to `3`. |
| `A2A_WEBHOOK_DELIVERY_BACKOFF` | Base backoff in seconds for exponential backoff. Defaults to `1.0`. |
| `A2A_WEBHOOK_DELIVERY_TIMEOUT` | HTTP timeout in seconds for webhook delivery. Defaults to `10`. |
| `A2A_WEBHOOK_REACHABILITY_CHECK` | Set `true` to validate webhook reachability before delivery. Defaults to `false`. |
| `A2A_WEBHOOK_REACHABILITY_TIMEOUT` | Timeout in seconds for reachability check. Defaults to `5`. |
| `A2A_DISABLE_SENDER_ECHO` | Set `true` to disable sender-side Telegram echo. Defaults to `false`. |

**Metrics configuration:**

| Variable | Purpose |
|---|---|
| `A2A_METRICS_LOG_ENABLED` | Set `true` to enable periodic metrics logging. Defaults to `false`. |
| `A2A_METRICS_LOG_INTERVAL` | Interval in seconds between metrics log entries. Defaults to `300` (5 minutes). |
| `A2A_METRICS_COMMAND_ENABLED` | Set `true` to enable `/a2a_metrics` Telegram slash command. Defaults to `false`. |

**Using the `/a2a_metrics` Telegram command:**

To enable the `/a2a_metrics` command, set the environment variable:

```bash
export A2A_METRICS_COMMAND_ENABLED=true
```

Then restart the Hermes gateway. Once enabled, send `/a2a_metrics` via Telegram to get formatted metrics:

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

The command is detected by the webhook route and returns metrics directly without processing as a normal message.

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

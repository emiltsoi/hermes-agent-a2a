# Hermes Agent A2A

`hermes-agent-a2a` is the A2A HTTP/JSON-RPC protocol plugin for Hermes fleet agents. It exposes a local A2A server, HMAC request signing, SSE streaming, push notifications, session relay with gateway hook support, and fleet metrics — all Hermes-specific, not fleet-agnostic.

## Capabilities

| Capability | Tools / Files | Purpose |
|---|---|---|
| Agent discovery | `a2a_discover` | Fetch an Agent Card by registry name or direct URL. Can auto-register external agents. |
| **Registry announcement** | `a2a_announce` | Announce this agent to a shared A2A registry so other agents can discover it. Reads `A2A_REGISTRY_URL` env var. |
| Protocol tasks | `a2a_send_protocol_task` | Send JSON-RPC `SendMessage` and poll `GetTask`. |
| Hermes local workers | `a2a_run_local_agent_task` | Run another local Hermes profile as an ephemeral worker with Hermes A2A metadata. |
| Hermes remote workers | `a2a_run_remote_agent_task` | Ask a remote Hermes A2A server to run its own ephemeral worker. |
| Session relay | `a2a_send_session_message` | ⚠️ Requires Hermes gateway patches — see [README § Hermes gateway compatibility](#hermes-gateway-compatibility) |
| Metrics | `a2a_get_metrics` | Get current A2A plugin metrics (uptime, webhook stats, task counts, queue depth). |
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
- `a2a_send_session_message`
- `a2a_get_metrics`

`a2a_send_session_message` is intentionally one-way: it delivers into the target Hermes session/gateway and returns an A2A-shaped delivery ACK, not a semantic reply. Use `a2a_send_protocol_task` when you need a pollable A2A task response.

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

---

## The Mesh: Session-Aware Fleet Messaging

This is the main thing that makes Hermes fleets different from standard A2A.

**Standard A2A is orchestration:** one agent delegates a task to another, gets a result back, continues. The relationship is client → worker. Context doesn't persist between turns.

**Hermes mesh is teamwork:** agents hold conversations across sessions, preserve sender context (sender_name, message ID being replied to), and route replies through the mesh by convention. Britney can ask Linda a question mid-dispatch and get a threaded reply back — when both agents follow the mesh discipline documented below.

`a2a_send_session_message` is the mesh bridge. The envelope carries sender context — sender_name and the message ID being replied to — so the recipient's LLM sees exactly who asked and what they're responding to. Thread continuity within the mesh is preserved by agent discipline, not protocol enforcement: agents agree to route replies through `a2a_send_session_message` back to the sender. This is intentional — convention-based coordination lets agents exercise judgment rather than follow mechanical rules. The fleet's organic interactions (escalation instead of reflex-loop, context-aware routing) emerge from this flexibility.

**In a multi-owner or adversarial deployment, this model is insufficient.** A protocol-level mechanism would be needed. We're considering an optional hardwired mode (e.g., `X-Fleet-Hops` header with configurable threshold) for deployments that need protocol guarantees over organic flexibility.

This is not a webhook relay. It's a session-to-session handoff where the envelope does the routing work.

**What this enables:**
- Agents that work as a team, not just a delegation chain
- Cross-fleet coordination without either side needing to know internal topology
- Thread-preserving conversations between agents that outlive a single task
- Mesh discipline: domain routing, reply accountability, full context preserved

**Google A2A compatibility** is also built in — the plugin speaks standard A2A JSON-RPC for external agent onboarding. But the mesh session relay is what you can't get elsewhere.

---

### Mesh Discipline: The CTA Protocol

Every `a2a_send_session_message` carries a **CTA** (Call To Action) that tells the recipient what to do with it:

| Field | Values | Meaning |
|-------|--------|---------|
| `action` | `do` \| `info` | `do`: act on this message. `info`: log/acknowledge only |
| `reply` | `yes` \| `no` | `yes`: sender expects a reply. `no`: fire-and-forget |

**Combinations:**
- `action=do + reply=yes`: take action, then reply with result
- `action=do + reply=no`: act on it, no reply needed
- `action=info + reply=yes`: acknowledge and reply when done processing
- `action=info + reply=no`: acknowledge only, no reply needed

**Reply rule:** All A2A replies use `a2a_send_session_message` back to the originating peer — not a plain Telegram DM or platform DM. Plain DMs break thread continuity. The mesh handles the return path.

**Agent prompt for mesh discipline:**

> When you receive an A2A message:
> 1. Read the CTA — `action` tells you what to do, `reply` tells you whether to respond
> 2. If `reply=yes`: respond via `a2a_send_session_message` back to the sender, using the same message ID as a reference (`ref` field)
> 3. Never reply via plain Telegram DM or platform DM — always route through the mesh so the reply carries your full context and reaches the correct thread
> 4. If you need to route the message to another agent first (wrong domain), do so immediately — don't hold it
> 5. If the CTA is ambiguous, reply=yes is the safe default

**Example — Britney dispatches to Linda:**

```python
a2a_send_session_message(
    message="Linda, review PR #123 before I merge. Link: https://github.com/...",
    agent="linda",
    action="do",
    reply="yes"
)
# Linda's session receives it attributed to Britney.
# Linda's reply routes back through the mesh to Britney's session.
```

**Example — Linda acknowledges without replying:**

```python
a2a_send_session_message(
    message="Routing to Britney — she owns SWE dispatch.",
    agent="britney",
    action="info",
    reply="no"
)
# Britney receives the update; Linda has already forwarded.
```

---

### Hermes Gateway Compatibility

> **⚠️ Mode 4 (session relay) requires gateway patches**

The `a2a_send_session_message` tool (mode 4) requires Hermes gateway patches that are not present in the standard public `hermes-agent` codebase. Modes 1–3 (protocol tasks, local/remote workers) are self-contained and work without any gateway patches.

**Mode 4 requires these gateway patches:**

- `platforms.webhook.extra.routes.<route>.target_session` to bind the webhook event to an existing platform session.
- webhook-sourced session authorization after HMAC validation (webhook allowlist bypass for `webhook:` user IDs).
- webhook source/platform override when routing into another platform session (`_platform` parameter in `build_source()`).

**Minimal gateway changes needed (+8 lines):**
- `gateway/platforms/base.py`: +2 lines for `_platform` override in `build_source()`
- `gateway/run.py`: +6 lines for webhook allowlist bypass

The plugin owns A2A identity resolution, HMAC request signing, message envelope construction, and platform-independent session float via the `a2a:send` gateway hook. Drop a platform-specific hook handler (Telegram, Discord, etc.) to route floats to any channel. The gateway only needs to provide generic authenticated webhook-to-session routing.

### Recommended Cleanup Path for Hermes Core Patches

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
| `A2A_REGISTRY_URL` | Shared A2A registry URL for `a2a_announce`. Defaults to nothing (must be set to use announcement). |
| `A2A_REGISTRY_AUTH_TOKEN` | Bearer token for the shared A2A registry. |

**Webhook delivery configuration:**

| Variable | Purpose |
|---|---|
| `A2A_WEBHOOK_DELIVERY_RETRIES` | Number of retry attempts for failed webhook delivery. Defaults to `3`. |
| `A2A_WEBHOOK_DELIVERY_BACKOFF` | Base backoff in seconds for exponential backoff. Defaults to `1.0`. |
| `A2A_WEBHOOK_DELIVERY_TIMEOUT` | HTTP timeout in seconds for webhook delivery. Defaults to `10`. |
| `A2A_WEBHOOK_REACHABILITY_CHECK` | Set `true` to validate webhook reachability before delivery. Defaults to `false`. |
| `A2A_WEBHOOK_REACHABILITY_TIMEOUT` | Timeout in seconds for reachability check. Defaults to `5`. |
| `A2A_FLOAT_ENABLED` | Set `false` to disable the `a2a:send` gateway hook float. Defaults to `true`. |

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

```text
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

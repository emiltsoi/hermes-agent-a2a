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

## Use Cases

All of these patterns are powered by `a2a_send_session_message` — the session relay tool that delivers a message into a target agent's live conversation context with full thread continuity. No polling, no separate worker process, no context loss.

### Background agents that wake on schedule

You want an agent to do work while you're not watching — poll a feed, check a system, prepare a daily briefing. Most agent frameworks solve this with a separate daemon or polling loop.

The Hermes mesh approach: the agent's session *is* the ambient worker. A cron job fires → routes into the agent's live session via `a2a_send_session_message` → agent wakes with full context intact → acts → replies via the mesh.

```
Cron tick fires
     │
     ▼
Webhook hit (Telegram or any platform)
     │
     ▼
a2a_send_session_message → agent's live session
     │
     ▼
Agent session wakes. Full conversation history available.
Agent reads the A2A message, acts, replies.
     │
     ▼
Reply routes back through the mesh to the caller.
```

No separate worker daemon. No polling. The agent was sleeping — its session was idle. The schedule woke it via `a2a_send_session_message`. When it finishes, it goes back to sleep. The session persists so the next wake has full context from the previous run.

What this enables: daily digests compiled by 7am, monitoring agents that alert only on change, background research that accumulates context over days and delivers when ready.

### Specialist chain — humans curate, agents specialize

A complex task needs architecture thinking, domain discovery, and implementation planning. You could throw it all at one agent, but specialists are better.

The Hermes mesh approach: talk to three different agents in sequence via `a2a_send_session_message`, each building full context independently. When you reach execution, you have three expert perspectives — not one confused generalist.

```
You → a2a_send_session_message → Isa (Discovery)
     ← structured findings with full codebase context

You → a2a_send_session_message → Britney (Architecture)
     ← architecture proposal grounded in Isa's actual findings

You → a2a_send_session_message → Linda (Design Review)
     ← signed-off design with coupling and failure mode analysis

You → Merge all three perspectives → Claude Code executes with full specialist context
```

Each agent maintained a fully-persistent session. Isa's context is complete — she was inside the codebase, she knows what she found and what she dismissed. Britney responds to Isa's actual findings. Linda reviews the real architecture, not a paraphrase. All routing happens via `a2a_send_session_message` through the mesh — the user never leaves their own interface.

The human is the curator: deciding which specialist to consult, in what order, when to stop prep and start executing.

What this enables: multi-domain tasks handled by actual specialists rather than a single LLM acting as all of them, quality-gated workflows where each specialist signs off before the next stage, reduced hallucination because each specialist's claims are grounded in their own exploration.

### Specialist injection — agents loop in specialists mid-chain

During any relay chain, an agent can pull in a specialist via `a2a_send_session_message` without restarting or losing context. The chain pauses, the specialist responds, their output flows back in, the chain continues.

```
Britney → a2a_send_session_message → Linda (design review)
    │
    Linda detects a coupling issue that spans Isa's domain
    │
    Linda → a2a_send_session_message → Isa: "What's the import graph for module X?"
    Isa responds with the graph
    │
    Linda folds Isa's data into the review
    Linda → a2a_send_session_message → Britney: "Approved, with one routing change"
```

The human didn't know to call Isa — Linda did it because the mesh discipline says: wrong domain, route first. No context loss, no chain restart, no paraphrase. The specialist consultation is invisible to the caller.

What this enables: agents that self-correct by consulting the right specialist when they hit a domain boundary, chains that get smarter as they run without human intervention, context that flows through the right expert regardless of who initiated the chain.

### Parallel specialist prep — all at once, not one at a time

Same result as the specialist chain, but run in parallel instead of sequence. All three calls to `a2a_send_session_message` fire simultaneously — each agent works in isolation with a complete session, none waiting for the others.

```
You → a2a_send_session_message → Isa (discovery)    ─┐
You → a2a_send_session_message → Britney (arch)     ─┤
You → a2a_send_session_message → Linda (review)     ─┘
     All three act in parallel
     │
     ▼
You receive three independent, fully-contextual responses
Merge → Claude Code executes
```

Each agent had an uninterrupted, complete session. None of them know about the others until you merge the outputs. The context never got diluted by multitasking — every specialist worked in isolation and delivered a finished result.

What this enables: same quality as sequential specialist prep in a fraction of the time, agents that work at their own pace without blocking each other, human curator assembles the final output from complete specialist perspectives rather than watching a generalist try to do three things at once.

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

These constraints do not apply to `a2a_send_protocol_task`, which communicates with external A2A agents over HTTP without spawning local workers, or `a2a_send_session_message`, which delivers a message into the target's gateway session over HTTP — both work with any reachable agent regardless of filesystem layout.

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

**In a multi-owner or adversarial deployment, this model is insufficient.** A protocol-level mechanism would be needed. `X-Fleet-Hops` (for 1-1 task exchange) could address reflexive loops there; mesh multi-party discussions have no loop problem since each agent routes independently.

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

#### Hook Architecture

When `a2a_send_session_message` delivers a message, the plugin emits an `a2a:send` gateway hook on the **sender's** gateway. Any hook registered for `a2a:send` fires — fire-and-forget, never blocking delivery. The sender's gateway is the right place because the sender's profile owns the bot token and target chat IDs for outbound routing.

**Hook directory structure** — drop into `~/.hermes/hooks/<name>/`:

```
a2a-float/
├── HOOK.yaml      # manifest (name, events)
└── handler.py     # your platform-specific sender
```

**`HOOK.yaml`** — manifest declares the hook name and the events it subscribes to:

```yaml
name: a2a-float
description: Telegram float for outbound A2A session messages
events:
  - a2a:send
```

**`handler.py`** — `handle(event_type, context)` receives:

| Context key | Value |
|---|---|
| `agent` | sender agent name (e.g. `britney`) |
| `message` | padded message text with envelope prefix |
| `timestamp` | Unix timestamp of the send |
| `direction` | always `"outbound"` |

```python
# handler.py
import json, logging, os, urllib.request
from datetime import datetime

logger = logging.getLogger(__name__)
RULES_PATH = os.path.join(os.path.dirname(__file__), "rules.yaml")

def _in_hours_window(window: str) -> bool:
    if not window:
        return True
    try:
        start_str, end_str = window.split("-")
        sh, sm = int(start_str[:2]), int(start_str[3:])
        eh, em = int(end_str[:2]), int(end_str[3:])
        now = datetime.utcnow()
        cur_mins = now.hour * 60 + now.minute
        start_mins = sh * 60 + sm
        end_mins = eh * 60 + em
        if start_mins <= end_mins:
            return start_mins <= cur_mins <= end_mins
        else:  # window crosses midnight
            return cur_mins >= start_mins or cur_mins <= end_mins
    except Exception:
        return True

def _should_float(sender: str, context: dict) -> bool:
    try:
        import yaml
        with open(RULES_PATH, encoding="utf-8") as f:
            rules = yaml.safe_load(f) or {}
    except Exception:
        return True
    if not rules.get("float_all", True):
        return False
    allow_from = rules.get("allow_from", [])
    if allow_from and sender not in allow_from:
        return False
    if not _in_hours_window(rules.get("hours", "00:00-23:59")):
        return False
    action_filter = rules.get("action_filter", [])
    if action_filter:
        if context.get("action", "do") not in action_filter:
            return False
    return True

def _send_telegram(text: str) -> bool:
    # Supports multiple env var names for broad compatibility
    bot_token = os.getenv(
        "HERMES_TELEGRAM_BOT_TOKEN",
        os.getenv("A2A_TELEGRAM_BOT_TOKEN",
        os.getenv("TELEGRAM_BOT_TOKEN", ""))
    )
    chat_id = os.getenv(
        "HERMES_TELEGRAM_DEFAULT_CHAT_ID",
        os.getenv("A2A_TELEGRAM_DEFAULT_CHAT_ID",
        os.getenv("TELEGRAM_HOME_CHANNEL", ""))
    )
    if not bot_token or not chat_id:
        logger.warning("[a2a-float] bot or chat_id not set — suppressed")
        return False
    try:
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = json.dumps({
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": "HTML",
        }, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode()).get("ok", False)
    except Exception as exc:
        logger.warning("[a2a-float] Telegram send failed: %s", exc)
        return False

def handle(event_type: str, context: dict) -> None:
    if event_type != "a2a:send":
        return
    sender = context.get("agent", "unknown")
    message = context.get("message", "")
    if not message:
        return
    if _should_float(sender, context):
        _send_telegram(message)
```

**`rules.yaml`** — per-profile float rules:

```yaml
float_all: true          # false = disable globally
allow_from:             # empty = allow all senders
  - britney
  - linda
  - agent0
hours: "09:00-22:00"    # UTC window; empty = always on
action_filter:          # empty = all actions
  - do
  - info
```

**Required env vars** — add to `~/.hermes/profiles/<agent>/.env`:

```
TELEGRAM_BOT_TOKEN=<your bot token>
TELEGRAM_HOME_CHANNEL=<your chat ID>
```

**Hook discovery** — hooks are auto-discovered from `~/.hermes/hooks/<name>/` on gateway startup. One hook directory per event type. The gateway loads all hooks and fires them asynchronously on each matching event.

---

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

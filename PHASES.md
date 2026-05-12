# A2A v3 — Build Phases

**Notion task:** `35e29647-6d73-81e0-bdd7-e1f0a9022931`
**Repo:** `emiltsoi/hermes-agent-a2a`
**Target version:** 3.0.0

---

## Pre-Phase 0 — Fleet Safety

**This is not a code phase. It is a discipline phase.**

The fleet `a2a` plugin (`~/.hermes/plugins/a2a/`) is live and handling all fleet A2A traffic. v3 development must **never** interfere with it.

### Hard Rules

1. **Fleet Hermes Home is sacred.** `HERMES_HOME=/home/emil/.hermes` is never set by v3 dev scripts.
2. **v3 runs in isolation.** All Phase 1–3 testing uses `HERMES_HOME=/tmp/hermes-v3-dev`.
3. **v3 plugin dir != fleet plugin dir.** v3 → `~/.hermes/plugins/hermes-agent-a2a/`. Fleet → `~/.hermes/plugins/a2a/`. These must never be the same directory.
4. **Port isolation.** v3 A2A server defaults to port `8081`. Fleet uses `41800–41812`. No overlap.
5. **No fleet profile touches v3.** During Phases 1–3, `britney`, `linda`, `isa`, etc. profiles must not load v3. Install v3 into a **disposable test profile only** (`v3-test`, `v3-dev`).
6. **Phase 4 field test uses a dedicated profile.** Not a fleet profile. Not a production profile.

### Development Environment

```bash
# Safe dev shell — all v3 work happens here
export HERMES_HOME=/tmp/hermes-v3-dev
mkdir -p $HERMES_HOME
python -m pytest tests/ ...    # runs against isolated home
```

```bash
# Fleet shell — normal work, a2a plugin untouched
# HERMES_HOME=/home/emil/.hermes (default)
# No v3 plugin loaded
```

### How to Verify Fleet is Unaffected

```bash
# Fleet A2A still responding?
curl http://127.0.0.1:41800/health

# v3 not loaded in fleet profile?
grep -r "hermes-agent-a2a" ~/.hermes/config.yaml 2>/dev/null
# Expected: empty (not configured)

# v3 has its own home?
ls /tmp/hermes-v3-dev/plugins/
# Expected: hermes-agent-a2a only
```

If fleet A2A stops responding after a v3 change — **v3 broke fleet. Revert immediately.**

### Port Allocation

| Service | Port | Used by |
|---------|------|---------|
| Fleet A2A | 41800–41812 | Fleet agents |
| v3 A2A dev | 8081 | Phase 1–4 dev |
| v3 A2A prod (future) | configurable via `A2A_PORT` | Installed fleet-wide |

---

## Phase 1 — Skeleton & Identity

**Branch:** `phase/1-skeleton-identity`
**Goal:** Project structure + vault-based identity resolution. No tools.

### Fleet Safety Check (run before anything else)

```bash
curl -s http://127.0.0.1:41800/health && echo "FLEET OK"
# Must return {"status":"ok"...} before proceeding
```

If fleet A2A is down — **do not proceed.**

### What to build

```
src/
├── __init__.py
├── identity.py        # VaultResolver (from v2, HERMES_HOME-aware)
├── bootstrap.py        # AutoSourceBootstrap (from v2)
├── validators.py       # BootValidator (from v2)
├── schema.py           # validate_config, apply_defaults (from v2)
├── plugin.py           # register(), on_boot(), on_shutdown() — skeleton only
└── platforms/
    ├── __init__.py
    ├── base.py
    └── telegram.py     # TelegramHandler (from v2)

tests/
├── test_identity.py    # VaultResolver: 3-tier resolution
├── test_bootstrap.py   # AutoSourceBootstrap: route auto-fill
├── test_validators.py  # BootValidator: token/chat_id checks
└── test_schema.py      # validate_config, apply_defaults
```

### Vault resolver (`identity.py`)
Replace v2's hardcoded path candidates with HERMES_HOME derivation:

```python
# v2 had:
Path("/home/emil/.hermes/plugins/hermes-a2a-v2/src")

# v3:
hermes_home = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
```

`resolve_agent(name)` helper — looks up agent in `vault.yaml` agents registry:
```python
def resolve_agent(name: str) -> dict | None:
    # Reads profiles/<profile>/a2a/vault.yaml → agents[name]
    # Falls back to HERMES_HOME/profiles/<name>/a2a/vault.yaml
    # Returns {a2a_url, auth_token, description} or None
```

### Testable gates

| Gate | Criteria |
|------|----------|
| VaultResolver unit | Resolves agent from vault.yaml; falls back to profile vault; falls back to env |
| AutoSourceBootstrap | Fills missing `source:` in webhook routes from vault defaults |
| BootValidator | `validate()` passes with valid token+chat_id; raises with missing |
| `validate_config()` | Returns `(True, [])` for valid config; `(False, [errors])` for invalid |
| `apply_defaults()` | Injects `default_chat_id`; does not mutate original dict |
| `plugin.on_boot()` | No crash; returns None; logs "Phase 1 — identity only, no tools registered" |
| pytest | 39+ tests pass |

### Notion task name
`A2A v3 — Phase 1: Skeleton & Identity`

---

## Phase 2 — HTTP Server + Hooks

**Branch:** `phase/2-server-hooks`
**Goal:** Plugin starts, binds port, handles inbound JSON-RPC, hooks inject context into LLM calls.

### What to build

```
src/
├── server.py          # ThreadingHTTPServer + TaskQueue + A2AServer (from v1, fleet-agnostic)
├── hooks.py            # pre_llm_call, post_llm_call, pre_gateway_dispatch
├── persistence.py      # save_exchange, update_exchange
├── security.py         # RateLimiter, audit, filter_outbound, sanitize_inbound
└── _mode2_worker.py   # Mode 2 subprocess worker (fleet-agnostic paths)
```

### Fleet-agnostic path fixes (from v1)

| Location | v1 hardcoded | v3 replacement |
|---|---|---|
| `_mode2_worker.py` venv python | `/home/emil/.hermes/hermes-agent/venv/bin/python` | `{hermes_home}/hermes-agent/venv/bin/python` |
| `_handle_call_mode2` profile root | `/home/emil/.hermes/profiles` | `{hermes_home}/profiles` |
| `_handle_task_send_mode3` agent_home | `/home/emil/.hermes/profiles/{name}` | `{hermes_home}/profiles/{name}` |

### Hook behavior

`pre_llm_call`: if pending A2A task exists and agent is not mid-conversation, inject task context as user message with Mode 1 header.

`post_llm_call`: capture assistant response, write to task queue result, persist exchange.

`pre_gateway_dispatch`: for synthetic `[A2A trigger]` events, replace with real A2A task text.

### Server behavior

- Listens on `A2A_HOST:A2A_PORT` (default `127.0.0.1:8081`)
- `GET /.well-known/agent.json` → agent card
- `GET /health` → `{"status": "ok", "agent": ..., "version": ...}`
- `POST /` → JSON-RPC: `tasks/send`, `tasks/get`, `tasks/cancel`
- Auth via `A2A_AUTH_TOKEN` env var (HMAC Bearer)
- Rate limiting per IP

### Testable gates

| Gate | Criteria |
|------|----------|
| Server starts | Binds to `127.0.0.1:8081`; `GET /health` returns `200 {"status":"ok"}` |
| Agent card | `GET /.well-known/agent.json` returns `{name, url, version, capabilities}` |
| tasks/send accepted | POST valid JSON-RPC → `200 {"result":{"id":..., "status":{"state":"completed"...}}}` |
| tasks/send queues | POST sends task to queue; `GET /health` shows pending count |
| pre_llm_call inject | With pending task + idle agent, returns `{"context": "...[A2A..."}` |
| post_llm_call completes | With active task + assistant response, task queue marked complete |
| Rate limiting | >30 req/min from same IP → 429 |
| `pytest` | 39+ tests pass (Phase 1 + Phase 2 tests) |

### Notion task name
`A2A v3 — Phase 2: HTTP Server + Hooks`

---

## Phase 3 — Tool Handlers

**Branch:** `phase/3-tool-handlers`
**Goal:** All 4 tools wired to vault, fleet paths removed, no PII.

### What to build

```
src/
├── tools.py            # handle_discover, handle_call, handle_list, handle_telegram
└── schemas.py          # A2A_DISCOVER, A2A_CALL, A2A_LIST, A2A_TELEGRAM schemas
```

### Fixes per tool

**`a2a_discover`**
- URL resolution: use `resolve_agent(name)` from identity.py instead of `hermes_cli.config`
- Schema: `name` (required if no url), `url` (optional)

**`a2a_list`**
- Remove `hermes_cli.config` dependency entirely
- Read from `vault.yaml agents[]` via `VaultResolver` (expose `list_agents()`)
- Schema: no required args

**`a2a_call`**
- URL resolution: use `resolve_agent(name)` instead of `hermes_cli.config`
- Mode 2 subprocess path: `HERMES_HOME/hermes-agent/venv/bin/python`
- Schema: `name` OR `url`, `message`, `worker_at`, `task_id`, `intent`, `expected_action`

**`a2a_telegram`**
- Remove Emil's hardcoded chat ID `7945905361`
- Remove fleet path for caller identity (`hermes_home/fleet/a2a/agents/...`)
- Use `VaultResolver.resolve()` for bot token + chat_id
- Remove "echo to Emil" — user controls this via vault config if wanted
- Schema: `agent` (required), `message` (required), `cta`, `ref`

### Vault registry (`vault.yaml`)

```yaml
platforms:
  telegram:
    bot_token: "..."

defaults:
  chat_id: "..."
  platform: telegram
  chat_type: dm

agents:
  britney:
    a2a_url: "http://127.0.0.1:41812"
    auth_token: "..."
    description: "Principal SWE"
  linda:
    a2a_url: "http://127.0.0.1:41811"
    auth_token: "..."
    description: "Software Architect"
```

### Testable gates

| Gate | Criteria |
|------|----------|
| `a2a_discover` by name | With agent in vault: returns agent card. Unknown name → error |
| `a2a_discover` by url | Without name lookup: fetches card from URL |
| `a2a_list` | Returns vault agents list with names, URLs, descriptions |
| `a2a_list` empty | Returns `{"agents": [], "count": 0}` when vault has no agents |
| `a2a_call` Mode 1 | POSTs to resolved URL; returns task result or error |
| `a2a_call` Mode 2 | Spawns local worker with target profile; returns result |
| `a2a_call` by url | Direct URL call without name lookup |
| `a2a_telegram` | Sends to recipient's webhook_url; returns `{"status":"delivered"}` |
| `a2a_telegram` unknown agent | Returns error `Agent 'foo' not found` |
| No PII in test fixtures | No `7945905361` anywhere |
| No fleet paths in tools | No `/home/emil` strings in `tools.py` |
| `pytest` | 39+ tests pass |

### Notion task name
`A2A v3 — Phase 3: Tool Handlers`

---

## Phase 4 — Install & Publish

**Branch:** `phase/4-install-publish` (merged to `main`)
**Goal:** Shippable. Field tested. GitHub tag pushed.

### What to build

```
.
├── install.sh          # Interactive installer (tested against scratch HERMES_HOME)
├── QUICKSTART.md       # Setup guide — no gateway patch step needed
├── README.md           # Feature table, what this plugin does vs what Hermes does
├── pyproject.toml      # version 3.0.0
├── plugin.yaml         # version 3.0.0
└── .github/
    └── workflows/
        ├── ci.yml      # pytest on push
        └── release.yml # semver tag on GitHub Releases
```

### `install.sh` behavior

1. Detect `HERMES_HOME` (defaults to `~/.hermes`)
2. Check Python >= 3.11
3. Clone/pip-install the plugin into `HERMES_HOME/plugins/hermes-agent-a2a`
4. Create vault directory: `profiles/<profile>/a2a/`
5. Copy `vault.yaml.example` → `vault.yaml` (if not exists)
6. Prompt for `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `A2A_AGENT_NAME`
7. Write values to `vault.yaml`
8. Print env vars to add to profile `.env`
9. Verify: `python -c "from src.plugin import register; print('OK')"`

### Field test

Run install.sh against a **fresh `HERMES_HOME=/tmp/hermes-v3-test`**:
1. Install succeeds
2. `hermes-agent` starts without plugin crash
3. `GET /health` on A2A port returns `{"status":"ok"}`
4. Smoke test: POST `tasks/send` → task queued → `pre_llm_call` fires → `post_llm_call` completes

### `README.md` sections

1. **What this plugin does** — table: A2A HTTP server, vault identity, tool handlers, webhook routing
2. **What Hermes Agent provides** — A2A tools are built in when this plugin is installed
3. **Install** — pip / ClawHub / manual
4. **Configure** — vault.yaml structure
5. **Usage** — examples for each tool
6. **No gateway patch** — this plugin is self-contained

### GitHub release

- Tag: `v3.0.0` at `phase/4-install-publish` merge commit
- Release notes: highlight "self-contained plugin", "no gateway patch", "fleet-agnostic"
- Push to `origin/main`

### Testable gates

| Gate | Criteria |
|------|----------|
| install.sh runs cleanly | No errors, vault.yaml created, env vars prompted |
| Plugin loads without crash | hermes-agent starts; `on_boot()` logs success |
| A2A server responds | `GET /health` returns 200 |
| Vault identity loaded | `a2a_list` returns agents from vault |
| Smoke test | `tasks/send` round-trip works: enqueue → inject → complete |
| pytest | 39+ tests pass |
| install.sh verified step | `python -c "from src.plugin import register"` succeeds |
| GitHub push | `main` at `phase/4` merge; tag `v3.0.0` pushed |
| No PII in repo | No `7945905361`; no real tokens or chat IDs |

### Notion task name
`A2A v3 — Phase 4: Install & Publish`

---

## Phase Summary

| Phase | Branch | Focus | Key dependency |
|-------|--------|-------|---------------|
| 1 | `phase/1-skeleton-identity` | Structure + vault resolution | None |
| 2 | `phase/2-server-hooks` | HTTP server + LLM hooks | Phase 1 |
| 3 | `phase/3-tool-handlers` | All 4 tool handlers | Phase 2 |
| 4 | `phase/4-install-publish` | install.sh + publish | Phase 3 |

## Gate Protocol

1. Phase N branch open
2. Implement + write tests
3. `pytest` green
4. Britney reviews (Gate 1: Claude Code pre-gate)
5. Linda architect reviews (Gate 2)
6. Push to GitHub branch
7. Field test on fleet (smoke test)
8. Merge to `main`
9. Notion task marked Done
10. Next phase branch opened

## Hardcoded Path Audit

All `/home/emil` strings must be removed from `src/` before Phase 3 merge. Pattern:
```bash
grep -r "/home/emil" src/
grep -r "7945905361" src/ tests/
```

Both must return zero results.

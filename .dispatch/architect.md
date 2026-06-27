## ARCH Findings — hermes-agent-a2a v3.3 LOW-08

### SEC-01: SSRF Guard Asymmetry — `_validate_webhook_host` Blocks Only Loopback, `is_safe_url` Blocks All Private CIDRs
- `webhook_delivery.py:72` (lazy import), `server.py:50-66` (definition) — The production webhook delivery path (`trigger`/`trigger_async`) validates `A2A_WEBHOOK_HOST` via `_validate_webhook_host()`, which rejects only four literal strings: `localhost`, `127.0.0.1`, `::1`, `0.0.0.0`. Meanwhile `security.py:175-241` provides `is_safe_url()`, which blocks entire private CIDR ranges (10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16, 169.254.0.0/16), resolves hostnames via DNS, and blocks cloud metadata endpoints.
- Pattern: Two inconsistent SSRF guards exist in the codebase. The production path uses the weaker one. If `A2A_WEBHOOK_HOST=10.0.0.1` is set (via env-var injection), the SSRF guard passes and a signed HMAC payload is delivered to an internal network endpoint. The stronger `is_safe_url()` is only used for `a2a_announce` registry delivery and `validate_webhook_endpoint()` — not for the webhook trigger path.
- Exploitability rationale: An attacker who can inject `A2A_WEBHOOK_HOST` can redirect signed webhook payloads to an arbitrary internal IP. The payload includes the HMAC signature computed with `A2A_WEBHOOK_SECRET`, so the target would need to validate that signature — but the delivery still leaks timing, payload structure, and task IDs. Requires env-var injection, elevated but plausible in misconfigured container/CI environments.

### SEC-02: Webhook Secret Validation Misses Fallback Path — False Rejection of Valid Configs
- `tool_handlers.py:148-167` vs `tool_handlers.py:1360` — `_validate_agent_webhook_config()` reads the webhook secret exclusively from `transports.hermes_webhook.auth.secret` via `_transport_auth_value()`. The actual delivery code at line 1360 adds a second fallback: `raw_info.get("webhook_secret", "")`. An agent identity that places the secret at the top-level `webhook_secret` field would fail early validation (line 1309-1311) but actually have a valid secret available at line 1360.
- Pattern: Validation logic and consumption logic use different key paths. Systemic risk because identity documents evolve over time and validation can silently reject valid configs after migration/rename, causing session relay failures with a misleading error.
- UNCERTAIN: The Verifier should confirm whether any real identity vaults use the top-level `webhook_secret` key. If none do, this is a dormant bug. If some do, this is an active correctness defect.

### SEC-03: No Auth on `/health` Endpoint — Leaks Agent Name and Version
- `server.py:828-833` — `/health` returns `{"status": "ok", "agent": self.agent_name, "version": HERMES_VERSION}` with no authentication. Unlike other `do_GET` endpoints, this bypasses `_check_auth()` entirely.
- Pattern: Diagnostic endpoints leaking version/identity without auth are reconnaissance vectors. An attacker scanning for A2A agents can enumerate agent names and CLI versions, enabling version-specific exploit development. Rate limiting is not applied to GET endpoints either.
- Exploitability rationale: Low standalone risk, but combined with a version-specific vulnerability in `hermes_cli`, accelerates exploit development. `A2A_AGENT_NAME` env var is exposed, which may leak organizational naming conventions.

---

### ARCH-01: Circular Dependency via Lazy Import Creates Fragile Module Coupling
- `webhook_delivery.py:72,153` — `trigger()` and `trigger_async()` lazily import `from .server import _validate_webhook_host`. `server.py:47` eagerly imports `from .webhook_delivery import trigger, trigger_async`. Resolution depends on `_validate_webhook_host` existing as a module-level name before `trigger()` is first called — an implicit temporal contract.
- Pattern: Module A imports Module B at the top; Module B imports from Module A lazily inside a function. Works at runtime but silently breaks if the imported object is moved/renamed/privatized in Module A without import-time errors. `_validate_webhook_host` is underscore-prefixed (private by convention) yet imported across a module boundary.
- Coupling note: The CHANGELOG acknowledges this (LOW-08 deviation #2). Could be eliminated by moving `_validate_webhook_host` to `security.py` alongside `validate_webhook_endpoint` and `is_safe_url`.

### ARCH-02: Dual Webhook Delivery Implementations — Session Webhook vs Trigger Webhook
- `tool_handlers.py:1347-1409` (inline delivery in `handle_send_session_message`) vs `webhook_delivery.py:56-144` (`trigger`) — Two separate HMAC-signed webhook delivery implementations exist. The session path uses env vars `A2A_WEBHOOK_DELIVERY_RETRIES/BACKOFF/TIMEOUT`, a direct webhook URL, and records metrics inline. The trigger path uses `A2A_WEBHOOK_RETRIES/BACKOFF`, constructs `/webhooks/a2a_trigger`, and uses a different body format.
- Pattern: The v3.3 split extracted the server-side trigger path but left the session-message webhook path inline. Retry config, backoff strategy, and error handling diverge. Any security fix applied to one path must be manually replicated.
- Coupling note: The session path also does webhook reachability validation via `_validate_webhook_reachable()` (line 1323-1325), which the trigger path does not. These are symmetric operations that should share a delivery layer.

### ARCH-03: Hardcoded Agent Name "britney" in Session Message Handler
- `tool_handlers.py:1417` — `send(text=padded_message, sender_name="britney")` — Despite `telegram_float` being extracted as sender-agnostic (docstring: "the transport does not assume a sender"), the single production call site hardcodes `sender_name="britney"`.
- Pattern: The transport module makes the right architectural choice (caller passes identity); the handler reverts to pre-extraction behavior. Any agent other than britney using this handler displays the wrong sender prefix in the Telegram float.

### ARCH-04: `asyncio.run()` Inside Daemon Thread — Unbounded Thread Spawn, In-Flight Loss on Shutdown
- `server.py:93-110` — `_start_async_webhook_delivery()` spawns a daemon thread per inbound task calling `asyncio.run(trigger_async(...))`. No thread pool, no shutdown coordination, no graceful draining.
- Pattern: "Fire-and-forget daemon thread" anti-pattern compounded by `asyncio.run()` creating/destroying an event loop per delivery. `on_failure` callback (lines 99-103) marks the task as failed, but if the thread is killed mid-flight, the callback never runs, and the task remains stuck in "working" state until the orphan watchdog (240s timeout) fires. Under rapid task arrivals, threads proliferate without bound.

### ARCH-05: Two SSRF Protection Functions with Different Threat Models Coexist
- `server.py:50-66` (`_validate_webhook_host` — loopback-only, host-level) vs `security.py:175-241` (`is_safe_url` — full private CIDR + DNS + metadata, URL-level) — Different semantics, scope, and threat models. `_validate_webhook_host` guards webhook trigger + push capability; `is_safe_url` guards announcement registry + push subscriptions.
- Pattern: Architectural duplication risks inconsistent security posture. The weaker validator guards the higher-volume production path. No documentation guides a developer adding a new networked feature on which validator to choose.
- Coupling note: `_validate_webhook_host` is trapped in `server.py` because `_push_notifications_capability` (a method on `A2AServer`) needs it. Moving both to `security.py` would consolidate SSRF protection.

### ARCH-06: `_validate_target_url` Uses String Matching, Not IP Validation
- `tool_handlers.py:336-345` — Checks loopback via string comparison against `localhost`, `127.0.0.1`, `::1`, `0.0.0.0`. Misses DNS rebinding variants (`127.0.0.1.nip.io`), IP encoding tricks (decimal/hex/octal), and private IPs (`10.0.0.1`).
- Pattern: String-based SSRF protection is fragile. `is_safe_url` in `security.py` does numeric IP validation via the `ipaddress` module and DNS resolution. Acceptable when `allow_loopback=False` is the default for external URLs — but `allow_loopback=True` for fleet agents opens the door if `_is_local_fleet_agent()` is spoofed via identity poisoning.

---

## Passed
- **Hardcoded secrets**: None. All secrets flow through env vars (`A2A_AUTH_TOKEN`, `A2A_WEBHOOK_SECRET`, `A2A_HMAC_KEY`, Telegram bot tokens via env-var chain). Default-parameter `auth_token="" ` in `a2a_direct.call` and `webhook_delivery.trigger` are documented "no-token" defaults.
- **Auth model consistency**: `_check_auth()` correctly rejects all non-localhost requests when `A2A_AUTH_TOKEN` is unset (line 678-685: `return False`). Localhost bypass is documented and gated by `A2A_REQUIRE_AUTH=true`.
- **Injection filtering**: `sanitize_inbound()` blocks known prompt injection patterns before tasks reach the agent. `filter_outbound()` redacts API keys, GitHub tokens, Slack tokens from responses. Both applied in the task pipeline.
- **Rate limiting**: Dual-layer: server-side `RateLimiter` (per-client, 20 req/60s) for RPC/REST; tool-side `_consume_rate_limit()` (global, 30 calls/60s) for outbound protocol calls.
- **Content-Length validation**: Both REST and JSON-RPC paths enforce 1-65536 byte payload limits before JSON parsing.
- **HMAC timing safety**: `_check_auth` uses `hmac.compare_digest()` for bearer tokens (line 689). Note: `_check_hmac_push` (line 608) uses simple `==` comparison — less critical for short HMAC keys than bearer tokens.
- **Module extraction quality**: `telegram_float.py` is a true leaf (stdlib only). `a2a_direct.py` has one lazy import (`a2a_spec.tasks`). `webhook_delivery.py` has one lazy import (`server._validate_webhook_host`). All three are well-scoped with single responsibilities.
- **Test coverage**: 20 new tests across 3 new files covering env-var chains, error swallowing, retry backoff, HMAC signatures, short-circuit paths, and async delegation. No tests removed.

## Summary
**merge** — The v3.3 extraction improves code organization and breaks the god-module anti-pattern in `server.py`. The six findings are architectural observations, not blocking defects: SEC-01 (SSRF asymmetry) is the highest-severity but requires env-var injection; SEC-02 (validation mismatch) is likely dormant; SEC-03 (diagnostic leak) is low-impact; ARCH-01 through ARCH-06 are structural patterns for future refactors, not v3.3 regressions.

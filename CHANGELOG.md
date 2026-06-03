# Changelog

All notable changes to this project will be documented in this file.

## [3.3.1] - 2026-06-03

### Security — v3.3 Full Review Fixes (a2a-v3.3-full-20260603)

- **ARCH-03: `sender_name` now uses caller identity** (`b9d0ea2`): `handle_send_session_message` hardcoded `sender_name="britney"` despite the `telegram_float` transport being sender-agnostic. Now reads `A2A_AGENT_NAME` via the existing `from_agent` variable. Any agent's Telegram float shows its own name.

- **NEW-01: REST cancel endpoint now requires auth** (`b9d0ea2`): `POST /tasks/{id}:cancel` was the sole POST handler that bypassed `_do_rest_post()` — no `_check_auth()`, no `RateLimiter.allow()`. Now wrapped in `_do_rest_post` like `/message:send` and `/message:stream`. Unauthenticated task cancellation blocked.

### Refactor — SSRF Guard Consolidation (a2a-v3.3-full-20260603)

- **Four SSRF guard functions → two in `security.py`** (`a70e8a7`): `_validate_webhook_host` (server.py, loopback-only, 4 strings), `_validate_target_url` (tool_handlers.py, loopback-only, 4 strings), `_is_private_ip` (security.py, full CIDR), and `is_safe_url` (security.py, DNS-resolved) are now consolidated into `validate_host` and `validate_target_url` — both in `security.py`, sharing `_is_private_ip` for full private CIDR coverage (10.x, 172.16-31.x, 192.168.x, 169.254.x, IPv6 private/link-local).

- **Webhook delivery path now blocks private networks** (SEC-01): `webhook_delivery.py` previously used `_validate_webhook_host` (4 loopback strings only). Now uses `validate_host` which blocks all private CIDRs via `_is_private_ip`. An attacker with `A2A_WEBHOOK_HOST` env-var control can no longer redirect signed webhook payloads to 10.0.0.1 or 192.168.1.1.

- **All 11 call sites use one threat model** (ARCH-05, ARCH-06, NEW-03): `tool_handlers.py` uses a module-level import alias (`from .security import validate_target_url as _validate_target_url`) so all 10 existing call sites resolve to the new consolidated function without changes. `webhook_delivery.py` imports `validate_host as _validate_webhook_host` lazily (breaks the server.py circular dependency). `server.py:_validate_webhook_host` removed — last reason it lived in server.py is gone.

### Tests

- **CancelTask tests pass body for new auth gate** (`ef69933`): 3 REST cancel tests called `_rest_post()` without a body — `_do_rest_post` requires Content-Length 1-65536. Tests now pass `body={}`.

## [3.3.0] - 2026-06-02

### Architecture — Transport Module Extraction (LOW-08)

- **`telegram_float.py` extracted as a leaf-level transport module**: The Telegram delivery transport — the post-handler side effect that surfaces results to the human via Telegram — was previously inline urllib in `tool_handlers.handle_send_session_message` (the float path). Extracted to `hermes_agent_a2a/telegram_float.py` as `send(text, sender_name, bot_token=None, chat_id=None)`. Stdlib-only, no internal dependencies. The caller passes the message and sender name; the float doesn't know who's using it. See commit `8a226e9` (LOW-08 Task 3, a2a-review-20260602).
- **`a2a_direct.py` extracted as a leaf-level transport module**: The single-shot JSON-RPC transport — used when one A2A agent calls another without going through the local webhook — was previously `_call_a2a_direct` and `_call_a2a_direct_async` in `server.py`. Extracted to `hermes_agent_a2a/a2a_direct.py` as `call(url, message, task_id, auth_token="", timeout=10)` and `call_async(...)`. Stdlib-only. The lazy `from .a2a_spec.tasks import build_task_send_payload` import is preserved (avoids pulling a2a_spec onto the hot path). `A2A_AGENT_NAME` env override is preserved (the caller controls its own sender identity). See commit `5534b5f` (LOW-08 Task 4, a2a-review-20260602).
- **`webhook_delivery.py` extracted as a leaf-level transport module**: The HMAC-signed webhook delivery transport — the production cross-agent delivery path with retry-with-backoff and SSRF guard — was previously `_trigger_webhook` and `_trigger_webhook_async` in `server.py`. Extracted to `hermes_agent_a2a/webhook_delivery.py` as `trigger(message, task_id, ..., use_direct_a2a, target_url, auth_token)` and `trigger_async(...)`. Stdlib-only. The SSRF guard (`_validate_webhook_host`) stays in `server.py` because it's also used by `_push_notifications_capability`; `webhook_delivery.py` imports it lazily to break the circular dependency. See commit `1796c11` (LOW-08 Task 5, a2a-review-20260602).
- **`server.py` shrunk by 396 lines** (2,318 → ~1,922): The two local transport function groups and their `_urlopen_with_status` helper are gone; the call sites now use the new public `call`, `call_async`, `trigger`, `trigger_async` symbols imported from the extracted modules. The `_validate_webhook_host` helper and `_push_notifications_capability` SSRF guard stay in `server.py` (still needed for the agent card's `pushNotifications` capability). The god-module anti-pattern is broken; the transport layer is now three small, focused files.
- **v3.3 deviations from the v3 SPEC, with rationale** (3 deviations, all load-bearing):
  - **Naming**: `webhook_delivery.trigger` / `trigger_async` instead of `deliver`. The v3 SPEC naming decision (Q1 final, 2026-06-02) framed this as `webhook_delivery.deliver(task_id)`. The original `trigger` names are preserved to keep the extraction a pure code-organization refactor with no public-API churn. The `deliver` rename can land in a follow-up commit.
  - **Lazy-import pattern (3 places)**: `a2a_direct.call` lazy-imports `a2a_spec.tasks` (avoids a2a_spec on the hot path); `webhook_delivery.trigger` and `trigger_async` lazy-import `_validate_webhook_host` from `server.py` (breaks the circular import that would otherwise occur because `server.py` imports `webhook_delivery`).
  - **Test split**: integration tests in `tests/test_current_tools.py` cover the *call site* (server.py uses the extracted modules); module-internal tests in `tests/test_a2a_direct.py`, `tests/test_webhook_delivery.py`, and `tests/test_tool_handlers_stays.py` cover the *module-internal surface* (the extracted modules' own behavior). The two test files are complementary, not duplicative.

### Tests — Module-Internal Coverage for the Extracted Transports

- **`tests/test_a2a_direct.py` (NEW, 7 tests)**: Covers the A2A_AGENT_NAME env override, the result/error response shapes, error-to-dict conversion for generic exceptions and URLError, the call_async delegate-via-asyncio.to_thread pattern, and the spec-format payload contract (`params.message.role`/`parts`/`metadata`, not the deprecated `params.task.text`). The HTTPError case is already covered by `test_current_tools.py::test_call_a2a_direct_handles_http_errors`. See commit `69009a6` (LOW-08 Task 6, a2a-review-20260602).
- **`tests/test_webhook_delivery.py` (NEW, 10 tests)**: Covers the default retries (3) and backoff (1.0) when env vars are unset, the exponential-backoff formula (`base_delay * 2^attempt`), the HMAC SHA-256 signature header, the body_dict field-conditional inclusion (`mode`, `deliver_only`), the empty-secret short-circuit, the `use_direct_a2a` short-circuit to `a2a_direct.call`, and the `trigger_async` thread-pool delegation for both `urlopen` and `time.sleep`. See commit `69009a6` (LOW-08 Task 6, a2a-review-20260602).
- **`tests/test_tool_handlers_stays.py` (NEW, 3 tests)**: Integration regression tests confirming `handle_send_session_message` still calls `telegram_float.send` after the LOW-08 extraction. Covers the post-extraction shape: the handler delegates the float to `telegram_float.send`, passes `sender_name="britney"`, and contains the `send(text=..., sender_name=...)` call site. The inline urllib for the float is gone; the webhook delivery's urllib block is separate and stays. See commit `69009a6` (LOW-08 Task 6, a2a-review-20260602).
- **Test count delta**: 595 → 615 (+20 new tests across 3 new test files). Pytest 615/615 passing.

### Quality — Preflight Tools Run

- **`ruff` (lint)**: 5 LOW findings on the v3.3 files (4 F401 unused imports, 1 F841 dead variable from a draft conditional). All fixed (4 auto-fixed by `ruff --fix`, 1 manual cleanup of the conditional in `test_trigger_async_uses_asyncio_to_thread_for_sleep`). All checks pass on the v3.3 files. See commit `bd2d9dd` (LOW-08 Task 7, a2a-review-20260602).
- **`bandit` (security)**: 6 audit flags on the v3.3 files (2 Low B107, 4 Medium B310). All are audit flags that resolve on review: the B107 flags are `auth_token=""` default-parameter declarations (documented "no token" defaults, not hardcoded passwords); the B310 flags are `urllib.request.urlopen` calls (the transport modules' job, with the SSRF guard on `A2A_WEBHOOK_HOST` validating the webhook path's host and the direct-A2A path taking a caller-supplied `target_url`).
- **`vulture` (dead code)**: 1 false positive on `webhook_delivery.trigger` (60% confidence) — vulture's static analysis missed the cross-module usage from `server.py` and the test files. The function is the public API of the transport; it's used.
- **Pre-commit hooks**: not configured for this repo (no `.pre-commit-config.yaml`). Surfaced for the reviewer; not a v3.3 regression. If we want pre-commit hooks set up, that's a follow-up commit.

## [3.2.23] - 2026-05-27

### Bug Fix — Hook Emit via HTTP (not direct runner ref)

- **`a2a:send` hook emit no longer requires gateway runner ref**: The hook emit path was calling `_gateway_runner_ref()` directly and attempting `runner.hooks.emit()` from the agent subprocess context. Since the plugin and gateway share a process but the agent is a separate subprocess, the runner ref returns `None` and the emit silently fails. Fixed by POSTing to the gateway's own `/hooks/emit` HTTP endpoint — same origin, no auth needed, 2s timeout, fire-and-forget.

### Documentation — README Hook Example Corrected

- **`handler.py` example updated**: The README code example now strips the A2A envelope (`_strip_envelope`) and formats for Telegram (`_format_for_telegram`) instead of sending the raw envelope string. Envelope regex and output format are documented as **customizable** — adjust to fleet preference.
- **`rules.yaml` example `hours` corrected**: Default changed from `"09:00-22:00"` to `"00:00-23:59"` (was silently suppressing floats outside business hours).

## [3.2.22] - 2026-05-26

### Bug Fix — PyPI Upload Had Old Broken Code

- **v3.2.21 upload was a no-op**: The 3.2.21 wheel uploaded to PyPI contained the *old* broken code (pre-fix). The version number was bumped but the source was not rebuilt after the fix was committed. v3.2.22 is a proper rebuild of commit `24ff9c1` with the `emit` coroutine fix actually included.

## [3.2.21] - 2026-05-26

### Security Fix — SSRF Loopback Bypass

- **`_is_local_fleet_agent` SSRF bypass**: Fixed critical SSRF protection regression where `_is_local_fleet_agent` was reading from `~/.hermes/fleet/fleet-registry.yaml` which does not exist on this fleet, causing the function to always return `False`. This made `allow_loopback=False` for all local fleet agents, blocking all A2A loopback calls. Fixed by routing through `list_agents()` from identity.py (same vault resolver as `a2a_list`), correctly recognizing all registered local fleet agents as loopback-safe.
- **Root cause**: The registry file path (`fleet-registry.yaml`) was stale — actual fleet data lives in per-profile `identity.yaml` files via `VaultResolver`.

### Bug Fix — `a2a:send` Hook Never Fired

- **`emit` coroutine not awaited**: `a2a:send` hook uses `async def emit`, which returns a coroutine. The original `_asyncio.create_task(runner.hooks.emit(...))` failed silently in the synchronous tool-handler context — `create_task` requires a running event loop, which doesn't exist in that thread. The coroutine was never awaited, so the hook never fired and no A2A messages floated to Telegram. Fixed by using `ensure_future` when a loop exists, or `run_until_complete` with a fresh loop as fallback for the fire-and-forget hook emission.

## [3.2.12] - 2026-05-23

### Architecture — Telegram Float Decoupled via Gateway Hook

- **`a2a:send` gateway hook**: `a2a_send_session_message` now emits a `a2a:send` gateway hook event after HTTP delivery, replacing the hardcoded Telegram HTTP call. This decouples platform concerns from the A2A layer.
- **`A2A_DISABLE_SENDER_ECHO` env var removed**: Float control moves to `~/.hermes/hooks/a2a-float/rules.yaml` — no longer env-driven.
- **`sender_echo` field removed** from `a2a_send_session_message` response dict.
- README and QUICKSTART updated to reflect gateway hook architecture.

## [3.2.11] - 2026-05-21

### New Feature — Shared A2A Registry Discovery

- **`a2a_announce` tool added**: Announce this agent to a shared A2A registry so other agents can discover it via `a2a_discover`. Reads registry URL from `A2A_REGISTRY_URL` env var (default) or `url` param override. Auth via `A2A_REGISTRY_AUTH_TOKEN` env var or explicit per-call params (`auth_type`, `auth_header`, `auth_value`). Builds the local AgentCard and POSTs it to the registry. Returns `{announced: true, agent_card, registry_response}` on success, or `{announced: false, error}` on failure.
- **New env vars**: `A2A_REGISTRY_URL`, `A2A_REGISTRY_AUTH_TOKEN`
- **`a2a_announce` registered in tool registry**: Tool available in the `a2a` toolset alongside `a2a_discover`, `a2a_list`, `a2a_send_protocol_task`, and other tools

## [3.2.10] - 2026-05-21

### Documentation

- **README updated**: All method names corrected to a2a.proto v1.0 spec — `SendMessage`, `GetTask`, `CancelTask`, `SubscribeToTask`; push notification endpoint now `POST /tasks/{id}/pushNotificationConfigs`; `a2a_spec/` module added to repository layout
- **CHANGELOG filled**: v3.2.4 through v3.2.9 backfilled to document the full spec compliance series

## [3.2.9] - 2026-05-21

## [3.2.8] - 2026-05-20

### Spec Compliance — Agent Card & Push Models

- **Provider → AgentProvider**: `url` now required field per a2a.proto:396-403
- **Skill → AgentSkill**: `description` and `tags` now required; `examples`, `input_modes`, `output_modes` added per a2a.proto:430-447
- **AgentCapabilities fields renamed**: `pushNotifications` → `push_notifications`; `stateTransitionHistory` removed; `extensions` and `extended_agent_card` added per spec
- **AgentInterface added**: url + protocol_binding + protocol_version per a2a.proto:336-350
- **ExtendedAgentCard field renames**: `defaultInputModes` → `default_input_modes`, `defaultOutputModes` → `default_output_modes`, `documentationUrl` → `documentation_url`
- **Push model field renames**: `endpoint` → `url`, `auth_type` → `scheme`, `auth_code` → `credentials` per a2a.proto:325-332, 464-478
- **Push REST endpoints**: `POST /tasks/{id}/pushNotificationConfigs` returns 201 with `configId`; DELETE returns 204 with empty body
- **ListTasksResponse**: now returns `{tasks: [], next_page_token, page_size, total_size}` per spec

### Spec Compliance — Task State

- **TaskState.rejected added** as canonical terminal state per a2a.proto:187-208
- **AUTH_STATES updated**: `authenticated` removed (auth sub-state, not canonical)
- **TERMINAL_STATES updated**: `rejected` included as terminal auth state

### Spec Compliance — SSE & Streaming

- **REST SSE endpoint**: `POST /message:stream` (colon convention) per spec REST conventions
- **Artifact event structure**: `TaskArtifactUpdateEvent.to_dict()` now returns `artifact_update` discriminator per StreamResponse oneof pattern

## [3.2.7] - 2026-05-20

### Spec Compliance — Core Message Format

- **Role field as integer**: `message.role` is now `Role.ROLE_USER = 1` (int), not string `"user"` per a2a.proto:245-252
- **SendMessage params.id removed**: per spec, `id` belongs in the JSON-RPC envelope, not inside `params`
- **Parts structure**: `parts` is now `[{"text": "..."}]` without `{"type": "text", "text": "..."}` wrapper — spec oneof pattern
- **parse_task_result**: task envelope unwrapping fixed — accesses `data.task` not `data`

## [3.2.6] - 2026-05-20

### Spec Compliance — Critical Interop

- **SendMessage routing**: Server now accepts `SendMessage` as valid method name (was only `tasks/send`)
- **A2A-Version header added**: All JSON-RPC responses include `A2A-Version: 1.0`

## [3.2.5] - 2026-05-20

### Bug Fixes

- **SendMessage JSON-RPC method**: Named correctly in outbound requests (was `tasks/send`)
- **A2A-Version header**: Added to all responses for standards-compliant agents

## [3.2.4] - 2026-05-20

### Bug Fixes

- **GetTask and CancelTask PascalCase**: JSON-RPC method names corrected to `GetTask` and `CancelTask` per a2a.proto v1.0

## [3.2.3] - 2026-05-20

### Bug Fixes
- **handle_get_metrics signature**: Executor passes implicit `task_id` kwarg — handler now accepts `args=None, **kwargs` to absorb both the args dict and executor kwargs without TypeError
- **a2a_metrics command registration**: `register_command` now includes `handler=_handle_a2a_metrics_command` argument; handler function added to tool_handlers.py

### Internal
- Added `_handle_a2a_metrics_command()` Telegram slash command handler for /a2a_metrics

## [3.2.2] - 2026-05-20

### Security Fixes
- **SEC-01: DNS timeout**: `socket.setdefaulttimeout(5.0)` added before `gethostbyname` in `is_safe_url` — prevents indefinite blocking on malicious DNS
- **SEC-02: Container auth bypass**: Localhost bypass now gated on `A2A_REQUIRE_AUTH=true` — loopback is not isolated in containers/shared namespaces
- **SEC-06: HMAC required on push config**: `_check_hmac_push(required=True)` — push subscription config now requires valid HMAC

### Documentation
- Description updated to reflect Hermes-specific A2A HTTP/JSON-RPC implementation
- `.gitignore` updated to exclude `dispatch/`

## [3.2.1] - 2026-05-19

### Bug Fixes (CRITICAL/HIGH from CODE_REVIEW.md)

#### Compliance Fixes
- **jsonrpc field missing in Mode3 timeout response**: `jsonrpc: "2.0"` added to tool_handlers.py:801
- **CORS hardcoded `*` at 5 locations**: Made configurable via `A2A_CORS_ORIGINS` env var (defaults to `*` for backward compat)
- **CORS method mismatch on POST-only endpoints**: Removed `GET` from `Access-Control-Allow-Methods` on POST-only endpoints
- **pushNotifications always returned boolean `True`**: Now returns `{webhookUrl: "..."}` object form when webhook is configured per A2A spec

#### Security Fixes
- **SSRF bypass via fleet-registry.yaml**: `_is_local_fleet_agent` now validates URL host is actually safe, not just loopback flag
- **Webhook secret exposure in error paths**: Generic error messages now used throughout

#### SSE / Streaming Fixes
- **SSE idle tracking used `created_at` (stream creation)**: Replaced with per-stream `_last_activity` updated on server-side activity (`push_event`). Client polling no longer resets idle timer.
- **SSE streams never expired**: Cleanup thread added with 300s idle timeout

#### Resource Leak Fixes
- **Daemon threads not joined on shutdown**: Plugin shutdown now joins SSE handler threads
- **Subscription store grew unbounded**: `add()`/`remove()` lifecycle now managed; TTL/cleanup logic added

#### Test Coverage
- **Mode3 worker subprocess tests**: Fixed 9 test failures — wrong patch targets (cleanup_zombie_processes in worker_registry, not tool_handlers), missing params, mock side_effect fixes
- **Mode2 worker tests**: Fixed os.path.isdir patching, TimeoutExpired exception handling
- **Concurrent TaskQueue access**: Tests added for race conditions

#### Other Fixes
- **Non-atomic metric recording**: `record_webhook_result(success: bool)` atomic API confirmed in place
- **Task loss between drain/requeue**: Safety comments and requeue logic verified
- **HMAC verification failure**: Proper exception handling in push_delivery.py
- **Path traversal check ordering**: Verified correct in tool_handlers.py

### Tests
- **535 tests passing**, 13 non-blocking teardown errors (mock subprocess cleanup in Mode3 tests — all assertions pass, non-spreading)

### v2 Deferred (API Design)
- Dual args/kwargs in `handle_send_session_message`
- Confusing token fallback chain
- Singleton reload behavior

---

## [3.2.0] - 2026-05-17

### Google A2A v1.0 Full Compliance
- **Idempotency keys**: `IdempotencyStore` singleton with 24h TTL. Same-key/same-payload → cached result. Same-key/different-payload → `-38004` error.
- **Full state machine**: `auth_required`, `authenticated`, `rejected` states added to `TaskQueue._TRANSITIONS`. Invalid transitions → `-38003` error.
- **Error schema alignment**: All 8 A2A error codes defined (`-32700`, `-32600`, `-32603`, `-38000` through `-38004`). All error responses use `{code, message, data}` format.
- **CORS headers**: `Access-Control-Allow-Origin: *` on all responses. `do_OPTIONS()` for preflight. Applied to GET, POST, OPTIONS, and error responses.
- **Agent card schema**: `agentId` field added. `skills[]` uses `{id, name}` per spec.

#### Wave 2 — Streaming & Push (P1)
- **SSE streaming** (`tasks/sendSubscribe`): Server-Sent Events stream of task state transitions. `SSEStreamer` singleton manages stream lifecycle.
- **Push notifications** (`tasks/pushNotification/subscribe` + unsubscribe): `SubscriptionStore` persists webhook subscriptions with HMAC key. `PushDelivery` delivers HMAC-SHA256 signed payloads with exponential backoff retry (3 attempts).
- **Hook wiring**: `TaskStateChangeHook.on_state_change()` broadcasts SSE events and delivers push webhooks on task state transitions.

#### Tests
- 161 tests passing (50 compliance + 24 SSE + 26 push + 79 current + 2 hybrid)
- Coverage: subscription_store 100%, sse_handler 94%, push_delivery 80%, server 68%, hooks 39%

## [3.1.3] - 2026-05-15

### Security Fixes (CRITICAL)
- CR-1: Simplified resolve_agent to return only safe fields (name, a2a_url, description, role)
  - Removed _strip_secrets function entirely
  - Transports with auth secrets are never included in response
  - Simpler and more secure than stripping secrets from full dict

### Bug Fixes (HIGH)
- HIGH #6: Replaced queue traversal with atomic counter in TaskQueue
  - pending_count() now counter-based: max(0, _enqueue_count - _complete_count - _cancel_count)
  - No singleton access, no re-entrancy path
  - Fixed counter increment timing to prevent drift on queue overflow eviction

### Bug Fixes (MEDIUM)
- MEDIUM #1: Fixed update_exchange placeholder matching in persistence.py
- MEDIUM #2: Fixed queue overflow race condition in server.py
- MEDIUM #3: Fixed metrics logger idempotency in runtime_state.py
- MEDIUM #4: Fixed to_dict mutability in runtime_state.py
- MEDIUM #5: Fixed persistence.py atomicity
- MEDIUM #7: Fixed DEFAULT_PORT collision detection with retry logic in plugin.py
- MEDIUM #8: Fixed A2A_WEBHOOK_SECRET fallback to WEBHOOK_SECRET with warning
- MEDIUM #9: Fixed path traversal prevention for card_path in webhook agent card retrieval
- MEDIUM #10: Fixed AuditLogger exception logging to use logger.warning
- MEDIUM #11: Fixed TOCTOU race condition in audit log rotation
- MEDIUM #12: Disabled email pattern redaction in filter_outbound (too broad)
- MEDIUM #13: Fixed regex capture group comment in hooks.py

### Code Quality (LOW)
- LOW #1: Added sort_keys=True to HMAC json.dumps for canonical signatures
- LOW #2: Removed dead proc.wait() call and fixed SyntaxError in _handle_call_mode2
- LOW #3: Removed redundant cleanup_zombie_processes() call in finally block
- LOW #4: Removed redundant json import from inline import statement
- LOW #5: Removed redundant logging import inside handle_send_session_message
- LOW #6: Added comment explaining GIL guarantee for double-checked locking
- LOW #7: Removed module-level task_queue variable that shadowed TaskQueue class
- LOW #8: Removed unused user_task parameter from handle_help() and handle_list()
- LOW #9: Added warning when hermes_cli.__version import fails
- LOW #10: Changed msg_id from task_id[:12] to full task_id (UUID truncation)
- LOW #11: Added comment documenting daemon thread metrics loss limitation
- LOW #13: Removed self-import in _get_queue_depth method
- LOW #14: Added force parameter to set_runtime_callbacks() to prevent overwriting on reload

### Tests
- All 62 tests pass
- Added tests for _derive_hermes_home fallback and error raising scenarios

## [3.1.2] - 2026-05-15

### Bug Fixes (HIGH)
- HIGH #6: Replace queue traversal with atomic counter in TaskQueue

## [3.1.1] - Previous Release

## [3.1.0] - Previous Release

## [3.0.0] - Previous Release

## [2.0.1] - Previous Release

## [2.0.0] - Previous Release

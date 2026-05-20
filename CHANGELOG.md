# Changelog

All notable changes to this project will be documented in this file.

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

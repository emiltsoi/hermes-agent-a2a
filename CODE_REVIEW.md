# Code Review: hermes-agent-a2a

**Review Date:** 2026-05-19
**Reviewer:** Claude Code (via MiniMax-M2.7-highspeed endpoint)
**Commit:** 27bafce (docs: add fleet lessons from google-a2a project)

---

## Critical Issues

### 1. Hardcoded Development Path
**File:** `hermes_agent_a2a/_a2a_runner.py:3`
```python
sys.path.insert(0, '/home/emil/.hermes/plugins/hermes-agent-a2a')
```
- Committed Unix-only development artifact
- Will fail immediately on Windows or any other user's machine
- **Fix:** Remove this file or add to `.gitignore`

### 2. Missing `jsonrpc` Field in Mode3 Timeout Response
**File:** `hermes_agent_a2a/tool_handlers.py:790-794`
```python
return {
    "id": task_id,
    "status": {"state": "failed"},
    "artifacts": [{"parts": [{"type": "text", "text": f"Mode 3 worker timed out after {timeout}s"}], "index": 0}],
}
```
- Missing required `"jsonrpc": "2.0"` field per JSON-RPC spec
- **Fix:** Add `"jsonrpc": "2.0"` to the response dict

### 3. Static Method Should Be Decorated
**File:** `hermes_agent_a2a/persistence.py:41-47`
```python
def _payload_hash(payload: dict) -> str:  # Should be @staticmethod
```
- Called as `IdempotencyStore._payload_hash(payload)` at line 76
- Should be decorated with `@staticmethod`
- **Fix:** Add `@staticmethod` decorator

### 4. Non-Atomic Metric Recording
**File:** `hermes_agent_a2a/runtime_state.py:49-53` and `runtime_state.py:70-73`
- Division by zero guard is correct, but `record_webhook_attempt_and_success()` and `get_metrics()` are not atomic
- Between counter increments, another thread calling `get_metrics()` could get stale rates
- **Fix:** Consider atomic operations or holding lock across compound operations

---

## Security Issues

### 5. CORS Wide Open (`*`)
**File:** `hermes_agent_a2a/server.py:599`
```python
self.send_header("Access-Control-Allow-Origin", "*")
```
- Risky for production deployments
- **Fix:** Make configurable via environment variable (e.g., `A2A_CORS_ORIGINS`)

### 6. SSRF Bypass via `fleet-registry.yaml`
**File:** `hermes_agent_a2a/tool_handlers.py:77-89`
```python
def _is_local_fleet_agent(agent_name: str) -> bool:
    fleet_path = Path(os.environ.get("A2A_VAULT_PATH", str(Path.home() / ".hermes/fleet")))
    registry_path = fleet_path / "fleet-registry.yaml"
```
- If attacker can modify `fleet-registry.yaml`, they can redirect A2A calls to loopback addresses
- **Fix:** Validate that registry entries point to safe addresses; add audit logging

### 7. Webhook Secret Exposure in Error Paths
**File:** `hermes_agent_a2a/tool_handlers.py:1247-1249`
```python
webhook_secret = _transport_auth_value(hermes_webhook, "secret") or (raw_info.get("webhook_secret", "") if isinstance(raw_info, dict) else "")
if not webhook_secret:
    return {"error": f"Agent '{agent}' has no webhook_secret configured..."}
```
- Error messages reveal configuration state
- **Fix:** Use generic error messages in production

### 8. Path Traversal Check Placement
**File:** `hermes_agent_a2a/tool_handlers.py:582-584`
```python
# Prevent path traversal attacks
if ".." in card_path:
```
- Check happens after URL validation; misleading comment placement
- Flow is correct (URL validated first, then path checked), but comment suggests otherwise
- **Fix:** Clarify comment ordering

---

## Bugs & Correctness Issues

### 9. Counter Drift on Queue Overflow Eviction
**File:** `hermes_agent_a2a/server.py:178-190`
```python
while len(self._pending) > _TASK_CACHE_MAX:
    # ... eviction logic ...
    self._enqueue_count += 1  # Line 190
```
- Evicted tasks don't decrement any counter
- `pending_count()` at line 212: `max(0, self._enqueue_count - self._complete_count - self._cancel_count)` drifts over time
- **Fix:** Track evicted count separately or don't increment on eviction

### 10. Race Condition in TaskQueue._states Access
**File:** `hermes_agent_a2a/server.py:120-162`
- `get_status()` at lines 261-266 accesses `_states` directly without `_lock`
- `find_task_by_id()` at lines 291-298 doesn't use `_lock`
- While individual dict ops are atomic (GIL), compound ops are not
- **Fix:** Wrap with `_lock` in `get_status()` and `find_task_by_id()`

### 11. Task Loss Risk Between drain_pending and requeue_tasks
**File:** `hermes_agent_a2a/hooks.py:52-72`
```python
pending = queue.drain_pending()
# ... processing ...
if len(pending) > 1:
    queue.requeue_tasks(pending[1:])
```
- If process crashes between drain and requeue, tasks 1..N are lost
- **Fix:** Use atomic peek-and-requeue operation or write-ahead log

### 12. Non-Atomic Metric Recording
**File:** `hermes_agent_a2a/tool_handlers.py:1284-1294`
- Success recorded via `record_webhook_attempt_and_success()`, failure via `record_webhook_failure()` separately
- If code changes, these could get out of sync
- **Fix:** Consider single atomic method `record_webhook_result(success: bool)`

### 13. CORS Method Mismatch
**File:** `hermes_agent_a2a/server.py:599`
```python
self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
```
- Endpoint only accepts POST, but GET is advertised
- **Fix:** Remove GET from CORS headers

---

## Performance Concerns

### 14. SSE Polling Blocks Thread
**File:** `hermes_agent_a2a/server.py:689-717`
```python
while _time.time() < deadline:
    # ... poll every 100ms for up to 5 minutes ...
    _time.sleep(poll_interval)
```
- Holds connection open for up to 5 minutes per client
- **Fix:** Consider asyncio-based approach or reduce `max_wait`

### 15. Orphaned Task Watchdog Iterates All Pending
**File:** `hermes_agent_a2a/server.py:568-582`
```python
for task in list(task_queue._pending.values()):
```
- Copies full dict every 120 seconds (default `_RESPONSE_TIMEOUT`)
- With 1000 max pending tasks, could be slow
- **Fix:** Use indexed tracking by creation time; only check recent tasks

### 16. No HTTP Connection Pooling
**File:** `hermes_agent_a2a/tool_handlers.py:489-518`
- Each `_http_request` creates new connection
- **Fix:** Use `urllib3.PoolManager` or `requests.Session` for connection reuse

---

## Test Coverage Gaps

### 17. Mode3 Worker Untested
- Subprocess spawning, timeout handling, non-JSON output, worker errors
- **Fix:** Add tests in `tests/test_current_tools.py`

### 18. SSE Client Disconnect Untested
- Cleanup path at `server.py:652-659` never verified
- **Fix:** Add integration test for unclean disconnect

### 19. HMAC Verification Failure Untested
- Push delivery at `push_delivery.py:92-96` not covered
- **Fix:** Add test for invalid HMAC signature handling

### 20. Concurrent TaskQueue Access Untested
- No stress test for multi-threaded operations
- **Fix:** Add concurrent enqueue/dequeue/complete stress test

---

## A2A Spec Compliance Issues

### 21. tasks/pushNotification Method Misdirected
**File:** `hermes_agent_a2a/server.py:894-896`
```python
elif method == "tasks/pushNotification":
    result = self._handle_push_unsubscribe(params, rpc_id)
```
- Should be a different handler per A2A spec
- **Fix:** Implement proper `tasks/pushNotification` handler or reject with correct error

### 22. Agent Card Missing `provider` Field
**File:** `hermes_agent_a2a/server.py:1231-1259`
```python
def build_agent_card(self) -> dict:
    return {
        "name": self.agent_name,
        "agentId": self.agent_name,
        "description": self.agent_description,
        # Missing: "provider": {...}
    }
```
- Per Google A2A spec, Agent Card requires `provider` object
- **Fix:** Add provider object with at least `organization` field

### 23. pushNotifications Capabilities Format
**File:** `hermes_agent_a2a/server.py:1244-1249`
```python
"pushNotifications": True,
```
- Spec says `pushNotifications` can be an object with `webhookUrl` etc.
- **Fix:** Consider returning object form when webhook is configured

---

## Resource Leaks

### 24. Daemon Threads Not Joined on Shutdown
**File:** `hermes_agent_a2a/plugin.py:112-124`
- `server.shutdown()` may not wait for daemon threads (watchdog at `server.py:580`)
- **Fix:** Explicitly join daemon threads before shutdown completes

### 25. SSE Streams Never Expire
**File:** `hermes_agent_a2a/sse_handler.py`
- Unclean client disconnects leave streams in `_streams` and `_by_task` forever
- **Fix:** Add TTL/expiration for streams; periodic cleanup job

### 26. Subscription Store Grows Unbounded
**File:** `hermes_agent_a2a/subscription_store.py`
- Abandoned subscriptions never cleaned up
- **Fix:** Add expiration or periodic cleanup

---

## API Design Issues

### 27. Dual args/kwargs in handle_send_session_message
**File:** `hermes_agent_a2a/tool_handlers.py:1129`
```python
def handle_send_session_message(args: dict = None, **kwargs) -> dict:
    merged = dict(args) if args else {}
    merged.update(kwargs)
```
- Inconsistent with other handlers that use `_dict_args_handler` decorator
- **Fix:** Standardize on decorator-based argument handling

### 28. Confusing Token Fallback Chain
**File:** `hermes_agent_a2a/tool_handlers.py:383-391`
```python
"value": auth_value or auth_token or "",
"token": auth_token or auth_value or "",
```
- Hard to reason about which takes precedence
- **Fix:** Simplify; document precedence clearly

### 29. Singleton Reload Behavior
**File:** `hermes_agent_a2a/runtime_state.py:119-128`
```python
def __new__(cls) -> A2ARuntimeState:
    if cls._instance is None:
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._initialized = False
```
- After module reload, `_initialized=False` but instance is stale (old state)
- **Fix:** Reset `_instance = None` on reload, or properly reinitialize

---

## Summary Table

| Severity | Count | Key Issues |
|----------|-------|------------|
| **CRITICAL** | 4 | Hardcoded path, missing jsonrpc field, static method bug, non-atomic metrics |
| **SECURITY** | 4 | CORS open, SSRF bypass, webhook secret exposure, path traversal timing |
| **BUG** | 6 | Counter drift, race conditions, task loss risk, error inconsistency |
| **PERFORMANCE** | 3 | SSE polling, watchdog iteration, no connection pooling |
| **TEST GAPS** | 4 | mode3 untested, SSE disconnect untested, HMAC failure untested, concurrency untested |
| **A2A COMPLIANCE** | 3 | Wrong method handling, missing provider, capabilities format |
| **RESOURCE LEAK** | 3 | Daemon threads, SSE streams, subscription store |
| **API DESIGN** | 3 | Dual args/kwargs, confusing auth, singleton reload |

**Total Issues:** 30

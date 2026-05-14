# A2A Plugin Monitoring/Metrics Design

## Overview
Add monitoring and metrics for the A2A plugin to track queue depth, webhook success rates, and other operational metrics.

## Architecture

### 1. Metrics Storage
Store metrics in `A2ARuntimeState` singleton for thread-safe access across plugin components.

```python
class A2AMetrics:
    """Thread-safe metrics collector for A2A operations."""
    
    def __init__(self):
        self._lock = Lock()
        self._webhook_attempts = 0
        self._webhook_successes = 0
        self._webhook_failures = 0
        self._tasks_received = 0
        self._tasks_completed = 0
        self._tasks_canceled = 0
        self._tasks_failed = 0
        self._start_time = time.time()
    
    def record_webhook_attempt(self):
        with self._lock:
            self._webhook_attempts += 1
    
    def record_webhook_success(self):
        with self._lock:
            self._webhook_successes += 1
    
    def record_webhook_failure(self):
        with self._lock:
            self._webhook_failures += 1
    
    def record_task_received(self):
        with self._lock:
            self._tasks_received += 1
    
    def record_task_completed(self):
        with self._lock:
            self._tasks_completed += 1
    
    def record_task_canceled(self):
        with self._lock:
            self._tasks_canceled += 1
    
    def record_task_failed(self):
        with self._lock:
            self._tasks_failed += 1
    
    def get_metrics(self) -> dict:
        with self._lock:
            uptime = time.time() - self._start_time
            webhook_success_rate = (
                self._webhook_successes / self._webhook_attempts * 100
                if self._webhook_attempts > 0 else 0
            )
            return {
                "uptime_seconds": uptime,
                "webhook": {
                    "attempts": self._webhook_attempts,
                    "successes": self._webhook_successes,
                    "failures": self._webhook_failures,
                    "success_rate_percent": round(webhook_success_rate, 2),
                },
                "tasks": {
                    "received": self._tasks_received,
                    "completed": self._tasks_completed,
                    "canceled": self._tasks_canceled,
                    "failed": self._tasks_failed,
                },
                "queue": {
                    "pending_count": self._get_queue_depth(),
                },
            }
    
    def _get_queue_depth(self) -> int:
        from .runtime_state import get_runtime_state as get_state
        return get_state().get_task_queue().pending_count()
```

### 2. Tool: a2a_get_metrics
Expose metrics via a tool that the LLM can query directly.

```python
def handle_get_metrics() -> dict:
    """Get current A2A plugin metrics."""
    from .runtime_state import get_runtime_state as get_state
    state = get_state()
    return state.get_metrics().get_metrics()
```

### 3. Periodic Logging
Log metrics at regular intervals (configurable via env var).

```python
_METRICS_LOG_INTERVAL = int(os.getenv("A2A_METRICS_LOG_INTERVAL", "300"))  # 5 minutes

def _start_metrics_logger():
    """Start background thread to log metrics periodically."""
    if os.getenv("A2A_METRICS_LOG_ENABLED", "false").lower() != "true":
        return
    
    def log_metrics():
        while True:
            try:
                from .runtime_state import get_runtime_state as get_state
                metrics = get_state().get_metrics().get_metrics()
                logger.info("[A2A Metrics] %s", json.dumps(metrics))
                time.sleep(_METRICS_LOG_INTERVAL)
            except Exception as exc:
                logger.error("[A2A Metrics] Logger error: %s", exc)
                time.sleep(_METRICS_LOG_INTERVAL)
    
    threading.Thread(target=log_metrics, daemon=True).start()
```

### 4. Instrumentation Points
Add metric recording at key points in the code:

**server.py** - task queue operations:
- Record task received on enqueue
- Record task completed/canceled/failed

**tool_handlers.py** - webhook delivery:
- Record webhook attempt before delivery
- Record webhook success on successful delivery
- Record webhook failure on retry exhaustion

### 5. Telegram Slash Command (Optional)
Inject metrics as a Telegram slash command `/a2a_metrics` similar to `/lcm` from hermes-lcm.

**Challenge**: This requires Hermes gateway integration, not just plugin code.

**Options**:

**Option A: Gateway Customization (Recommended for hermes-lcm pattern)**
Add a custom command handler in the Hermes gateway's Telegram platform adapter:
```python
# In gateway/platforms/telegram.py (customization)
async def handle_a2a_metrics_command(update, context):
    """Handle /a2a_metrics command."""
    from hermes_agent_a2a.runtime_state import get_runtime_state
    metrics = get_runtime_state().get_metrics().get_metrics()
    response = format_metrics_for_telegram(metrics)
    await update.message.reply_text(response)
```

**Option B: Webhook-Backed Command (Plugin-Only)**
Register the command via the plugin's webhook endpoint:
- Send message to agent with special prefix: `/a2a_metrics`
- Tool handler detects command and returns metrics instead of processing as task
- Gateway routes response back to Telegram

```python
def handle_send_session_message(message, agent, ...):
    # Detect metrics command
    if message.strip().startswith("/a2a_metrics"):
        from .runtime_state import get_runtime_state as get_state
        metrics = get_state().get_metrics().get_metrics()
        return {
            "state": "completed",
            "response": format_metrics_for_telegram(metrics),
            "delivery": "command_response",
        }
    # ... normal processing
```

**Option C: Tool-Based Query (Simplest)**
LLM can query metrics via tool, then format for Telegram:
```
User: /a2a_metrics
LLM: [calls a2a_get_metrics tool]
LLM: [formats response and sends to Telegram]
```

### 6. Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `A2A_METRICS_LOG_ENABLED` | `false` | Enable periodic metrics logging |
| `A2A_METRICS_LOG_INTERVAL` | `300` | Logging interval in seconds |
| `A2A_METRICS_COMMAND_ENABLED` | `false` | Enable /a2a_metrics command (Option B) |

### 7. Implementation Priority

1. **Phase 1** (Core Metrics):
   - Create A2AMetrics class
   - Add to A2ARuntimeState
   - Instrument webhook delivery points
   - Instrument task queue operations
   - Add a2a_get_metrics tool

2. **Phase 2** (Logging):
   - Add periodic metrics logger
   - Add configuration via env vars

3. **Phase 3** (Telegram Command - Optional):
   - Implement Option B (webhook-backed command)
   - Add formatting for Telegram
   - Test command flow

## Example Output

### Tool Response (JSON)
```json
{
  "uptime_seconds": 3600,
  "webhook": {
    "attempts": 150,
    "successes": 142,
    "failures": 8,
    "success_rate_percent": 94.67
  },
  "tasks": {
    "received": 150,
    "completed": 142,
    "canceled": 5,
    "failed": 3
  },
  "queue": {
    "pending_count": 0
  }
}
```

### Telegram Formatted Response
```
📊 A2A Metrics

Uptime: 1h 0m

🔗 Webhook
Attempts: 150
✅ Success: 142 (94.67%)
❌ Failed: 8

📋 Tasks
Received: 150
Completed: 142
Canceled: 5
Failed: 3

📬 Queue
Pending: 0
```

## Testing

- Unit tests for metrics collection
- Integration tests for tool response
- Tests for periodic logging (mock time)
- Optional: Tests for Telegram command flow

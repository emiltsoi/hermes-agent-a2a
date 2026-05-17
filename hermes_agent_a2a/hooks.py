"""A2A LLM hooks — pre/post call interception and gateway dispatch.

These hooks bridge the A2A HTTP server (which queues inbound tasks) with
the Hermes LLM loop. They are registered in plugin.py on_boot().

Wave 2 additions:
- TaskStateChangeHook: fires on task state transitions, delivering SSE events
  and push notifications to subscribed clients.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import server as _server_module
from .persistence import save_exchange

if TYPE_CHECKING:
    from .sse_handler import SSEStreamer
    from .subscription_store import SubscriptionStore
    from .push_delivery import PushDelivery

logger = logging.getLogger(__name__)

_HERMES_HOME = os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))


def _get_task_queue():
    """Access the process-wide task queue from the singleton state."""
    from .runtime_state import get_runtime_state as get_state
    return get_state().get_task_queue()


def pre_llm_call(conversation_history=None, user_message=None, **kwargs) -> dict:
    """If a pending A2A task exists and the agent is idle, inject task context.

    Returns ``{"context": "[A2A trigger]<task_id>|<sender_name>|<task_text>"}``
    which the agent receives as an extra user message. The task is marked
    as processing so subsequent calls don't double-inject.
    """
    logger.debug("[A2A hooks] pre_llm_call FIRED — messages=%d",
                 len(kwargs.get("conversation_history", [])))
    queue = _get_task_queue()
    if queue is None:
        logger.warning("[A2A hooks] pre_llm_call: queue is None")
        return {}

    pending = queue.drain_pending()
    logger.info("[A2A hooks] pre_llm_call: queue=%s, pending_count=%d, drained=%d",
                id(queue), queue.pending_count(), len(pending))
    if not pending:
        return {}

    task = pending[0]
    task_id = task.task_id
    sender = task.metadata.get("sender_name", "unknown")
    task_text = task.text

    # Transition: submitted → working
    queue.transition(task_id, "working")
    queue.mark_processing(task_id)

    # Fire state change hook (SSE + push)
    _trigger_state_change_hook(task_id, "submitted", "working")

    # Re-queue any additional tasks that weren't processed
    if len(pending) > 1:
        queue.requeue_tasks(pending[1:])

    context = (
        f"[A2A trigger]<{task_id}>|<{sender}>|{task_text}\n"
        "Use the tools above to respond to this inbound A2A task."
    )

    logger.info("[A2A hooks] Injecting pending task %s from %s", task_id, sender)
    return {"context": context}


def post_llm_call(conversation_history=None, assistant_response=None, session_id=None, model=None, platform=None, **kwargs) -> None:
    """After the LLM generates a response, write it to the task queue and persist.

    If no ``task_id`` is provided, completes the oldest processing task.
    Triggers TaskStateChangeHook (SSE + push) on completion.
    """
    task_id = kwargs.get("task_id")
    logger.info("[A2A hooks] post_llm_call called: task_id=%s response_len=%d",
                task_id, len(assistant_response) if assistant_response else 0)
    if not assistant_response:
        return

    queue = _get_task_queue()
    if queue is None:
        return

    # Find the task to complete — use explicit task_id or the oldest processing task
    target_id = task_id
    if not target_id:
        for tid in queue.get_processing_tasks():
            target_id = tid
            break

    if target_id:
        # Capture old state before completing
        old_status = queue.get_status(target_id)
        old_state = old_status.get("state", "working")

        queue.complete(target_id, assistant_response)
        logger.info("[A2A hooks] Completed task %s with response length %d", target_id, len(assistant_response))

        # Fire state change hook: working → completed
        _trigger_state_change_hook(target_id, old_state, "completed")

        # Persist the exchange
        meta = queue.get_task_metadata(target_id)
        agent_label = meta.get("sender_name", "a2a_peer")
        try:
            save_exchange(
                agent_name=agent_label,
                task_id=target_id,
                inbound_text=meta.get("original_text", ""),
                outbound_text=assistant_response,
                metadata=meta,
                direction="inbound",
            )
        except Exception as exc:
            logger.debug("Failed to persist A2A exchange: %s", exc)
    else:
        processing = queue.get_processing_tasks()
        if processing:
            tid = processing[0]
            old_status = queue.get_status(tid)
            old_state = old_status.get("state", "working")
            queue.complete(tid, assistant_response)
            logger.info("[A2A hooks] Completed processing task %s", tid)
            _trigger_state_change_hook(tid, old_state, "completed")


# ---------------------------------------------------------------------------
# TaskStateChangeHook — SSE + Push wiring
# ---------------------------------------------------------------------------

# Lazy singletons — imported here to avoid circular imports
_hook_sse_streamer: "SSEStreamer | None" = None
_hook_subscription_store: "SubscriptionStore | None" = None
_hook_push_delivery: "PushDelivery | None" = None


def _get_hook_sse_streamer() -> "SSEStreamer":
    global _hook_sse_streamer
    if _hook_sse_streamer is None:
        from .sse_handler import get_sse_streamer
        _hook_sse_streamer = get_sse_streamer()
    return _hook_sse_streamer


def _get_hook_subscription_store() -> "SubscriptionStore | None":
    global _hook_subscription_store
    if _hook_subscription_store is None:
        try:
            from .subscription_store import get_subscription_store
            _hook_subscription_store = get_subscription_store()
        except Exception:
            logger.debug("[A2A hooks] SubscriptionStore unavailable")
            return None
    return _hook_subscription_store


def _get_hook_push_delivery() -> "PushDelivery":
    global _hook_push_delivery
    if _hook_push_delivery is None:
        from .push_delivery import get_push_delivery
        _hook_push_delivery = get_push_delivery()
    return _hook_push_delivery


def _trigger_state_change_hook(task_id: str, old_state: str, new_state: str) -> None:
    """Fire the TaskStateChangeHook: push SSE events + deliver webhooks."""
    try:
        hook = TaskStateChangeHook()
        hook.on_state_change(task_id, old_state, new_state)
    except Exception as exc:
        logger.debug("[A2A hooks] TaskStateChangeHook error: %s", exc)


def _event_name_for_state(state: str) -> str:
    """Map a task state to its SSE event name."""
    mapping = {
        "working": "TaskWorking",
        "completed": "TaskCompleted",
        "failed": "TaskFailed",
        "canceled": "TaskCanceled",
        "rejected": "TaskRejected",
        "auth_required": "TaskAuthRequired",
        "authenticated": "TaskAuthenticated",
        "submitted": "TaskSubmitted",
    }
    return mapping.get(state, "TaskUpdated")


class TaskStateChangeHook:
    """Fires SSE events and push notifications when a task changes state.

    Contract:
        on_state_change(task_id: str, old_state: str, new_state: str) → None

    This hook is called by pre_llm_call (on transition to working) and
    post_llm_call (on transition to completed/failed/canceled).
    """

    # Injectable singletons for testing
    _sse_streamer: "SSEStreamer | None" = None
    _subscription_store: "SubscriptionStore | None" = None
    _push_delivery: "PushDelivery | None" = None

    def on_state_change(self, task_id: str, old_state: str, new_state: str) -> None:
        """Handle a task state transition.

        1. Push an SSE event to all open streams for this task.
        2. Look up push subscriptions and deliver signed webhooks.
        """
        from .sse_handler import SSEEvent

        event = SSEEvent(
            task_id=task_id,
            state=new_state,
            event=_event_name_for_state(new_state),
        )

        # 1. SSE push — broadcast to all open streams for this task
        self._push_sse(task_id, event)

        # 2. Push notifications — deliver to all webhook subscribers
        self._push_notifications(task_id, old_state, new_state)

    def _push_sse(self, task_id: str, event: "SSEEvent") -> None:
        """Push an SSE event to all open streams subscribed to task_id."""
        try:
            streamer = self._sse_streamer or _get_hook_sse_streamer()
            stream_ids = streamer.get_stream_ids_for_task(task_id)
            for sid in stream_ids:
                streamer.push_event(sid, event)
        except Exception as exc:
            logger.debug("[A2A hooks] SSE push error for task %s: %s", task_id, exc)

    def _push_notifications(self, task_id: str, old_state: str, new_state: str) -> None:
        """Deliver push notifications to all subscribers of task_id."""
        try:
            store = self._subscription_store or _get_hook_subscription_store()
            if store is None:
                return
            subs = store.get(task_id)
            if not subs:
                return
        except Exception as exc:
            logger.debug("[A2A hooks] SubscriptionStore unavailable for %s: %s", task_id, exc)
            return

        try:
            pusher = self._push_delivery or _get_hook_push_delivery()
        except Exception as exc:
            logger.debug("[A2A hooks] PushDelivery unavailable: %s", exc)
            return

        payload = {
            "taskId": task_id,
            "state": new_state,
            "event": _event_name_for_state(new_state),
        }

        for sub in subs:
            ok = pusher.deliver_with_retry(sub.url, payload, sub.hmac_key)
            if not ok:
                logger.debug(
                    "[A2A hooks] Push delivery failed for subscription %s → %s",
                    sub.subscription_id, sub.url,
                )


# Matches "[A2A trigger]<task_id>|<sender>|<text>" and extracts task_id
_A2A_TRIGGER_RE = re.compile(r"^\[A2A trigger\]<([^|]+)\|")

# Legacy comma-style trigger: "[A2A trigger]<task_id>,<sender>,<text>"
_A2A_TRIGGER_LEGACY_RE = re.compile(r"^\[A2A trigger\]<([^>,]+),([^,]+),(.+)$")


def pre_gateway_dispatch(event: str, **kwargs) -> str:
    """Replace synthetic ``[A2A trigger]`` text with the real task content.

    When the gateway receives an event whose text begins with ``[A2A trigger]``,
    this hook looks up the task in the queue and substitutes the actual message.
    If the trigger pattern doesn't match or the task isn't found, the original
    text is returned unchanged.
    """
    # event may be a MessageEvent object or a string depending on call context
    event_text = getattr(event, "text", event) if not isinstance(event, str) else event
    if not event_text.startswith("[A2A trigger]"):
        return event_text

    queue = _get_task_queue()
    if queue is None:
        return event_text

    # Extract task_id from pipe-delimited trigger: [A2A trigger]<task_id>|<sender>|<text>
    m = _A2A_TRIGGER_RE.match(event_text)
    if m:
        task_id = m.group(1)
        t = queue.find_task_by_id(task_id)
        if t:
            return f"[A2A trigger]<{task_id}>|<{t.metadata.get('sender_name','?')}>|{t.text}"
        return event_text

    # Fallback: legacy comma format
    m2 = _A2A_TRIGGER_LEGACY_RE.match(event_text)
    if m2:
        task_id = m2.group(1)
        t = queue.find_task_by_id(task_id)
        if t:
            return f"[A2A trigger]<{task_id}>|<{t.metadata.get('sender_name','?')}>|{t.text}"
        return event_text

    return event_text
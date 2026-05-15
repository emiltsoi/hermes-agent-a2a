"""A2A LLM hooks — pre/post call interception and gateway dispatch.

These hooks bridge the A2A HTTP server (which queues inbound tasks) with
the Hermes LLM loop. They are registered in plugin.py on_boot().
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

from . import server as _server_module
from .persistence import save_exchange

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

    queue.mark_processing(task_id)

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
        # Walk processing tasks to find one without an explicit id
        for tid in queue.get_processing_tasks():
            target_id = tid
            break

    if target_id:
        queue.complete(target_id, assistant_response)
        logger.info("[A2A hooks] Completed task %s with response length %d", target_id, len(assistant_response))

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
        # No specific task — complete the oldest processing task
        processing = queue.get_processing_tasks()
        if processing:
            tid = processing[0]
            queue.complete(tid, assistant_response)
            logger.info("[A2A hooks] Completed processing task %s", tid)


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
    event_text = event  # event is a string in this hook context
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

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
    """Access the process-wide task queue from the server module."""
    state = _server_module._runtime_state()
    return state.get("task_queue")


def pre_llm_call(agent, messages: list) -> dict:
    """If a pending A2A task exists and the agent is idle, inject task context.

    Returns ``{"context": "[A2A trigger]<task_id>|<sender_name>|<task_text>"}``
    which the agent receives as an extra user message. The task is marked
    as processing so subsequent calls don't double-inject.
    """
    queue = _get_task_queue()
    if queue is None:
        return {}

    pending = queue.drain_pending()
    if not pending:
        return {}

    task = pending[0]
    task_id = task.task_id
    sender = task.metadata.get("sender_name", "unknown")
    task_text = task.text

    queue.mark_processing(task_id)

    context = (
        f"[A2A trigger]<{task_id}>|<{sender}>|{task_text}\n"
        "Use the tools above to respond to this inbound A2A task."
    )

    logger.info("[A2A hooks] Injecting pending task %s from %s", task_id, sender)
    return {"context": context}


def post_llm_call(agent, assistant_response: str, task_id: str | None = None) -> None:
    """After the LLM generates a response, write it to the task queue and persist.

    If no ``task_id`` is provided, completes the oldest processing task.
    """
    if not assistant_response:
        return

    queue = _get_task_queue()
    if queue is None:
        return

    # Find the task to complete — use explicit task_id or the oldest processing task
    target_id = task_id
    if not target_id:
        state = _server_module._runtime_state()
        # Walk processing tasks to find one without an explicit id
        for tid in list(getattr(queue, "_processing", [])):
            target_id = tid
            break

    if target_id:
        queue.complete(target_id, assistant_response)
        logger.info("[A2A hooks] Completed task %s with response length %d", target_id, len(assistant_response))

        # Persist the exchange
        state = _server_module._runtime_state()
        pending = {t.task_id: t for t in list(state.get("task_queue")._pending.values()) +
                                        list(state.get("task_queue")._completed.values())}
        meta = {}
        if target_id in pending:
            meta = getattr(pending[target_id], "metadata", {})
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
        state = _server_module._runtime_state()
        processing = list(state.get("task_queue")._processing)
        if processing:
            tid = processing[0]
            queue.complete(tid, assistant_response)
            logger.info("[A2A hooks] Completed processing task %s", tid)


# Matches "[A2A trigger]<task_id>|<sender>|<text>"
_A2A_TRIGGER_RE = re.compile(r"^\[A2A trigger\]<([^|]+)\|")

# Legacy comma-style trigger: "[A2A trigger]<task_id>,<sender>,<text>"
_A2A_TRIGGER_LEGACY_RE = re.compile(r"^\[A2A trigger\]<([^>,]+),([^,]+),(.+)$")


def pre_gateway_dispatch(event_text: str) -> str:
    """Replace synthetic ``[A2A trigger]`` text with the real task content.

    When the gateway receives an event whose text begins with ``[A2A trigger]``,
    this hook looks up the task in the queue and substitutes the actual message.
    If the trigger pattern doesn't match or the task isn't found, the original
    text is returned unchanged.
    """
    if not event_text.startswith("[A2A trigger]"):
        return event_text

    queue = _get_task_queue()
    if queue is None:
        return event_text

    # Extract task_id from pipe-delimited trigger: [A2A trigger]<task_id>|<sender>|<text>
    m = _A2A_TRIGGER_RE.match(event_text)
    if m:
        task_id = m.group(1)
        with queue._lock:
            for t in list(queue._pending.values()) + list(queue._completed.values()):
                if t.task_id == task_id:
                    return f"[A2A trigger]<{task_id}>|<{t.metadata.get('sender_name','?')}>|{t.text}"
        return event_text

    # Fallback: legacy comma format
    m2 = _A2A_TRIGGER_LEGACY_RE.match(event_text)
    if m2:
        task_id = m2.group(1)
        with queue._lock:
            for t in list(queue._pending.values()) + list(queue._completed.values()):
                if t.task_id == task_id:
                    return f"[A2A trigger]<{task_id}>|<{t.metadata.get('sender_name','?')}>|{t.text}"
        return event_text

    return event_text

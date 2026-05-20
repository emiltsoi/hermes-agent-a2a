#!/usr/bin/env python3
"""Minimal A2A test agent for field-testing hermes-agent-a2a plugin Modes 1-3.

Starts a real A2A agent on port 41920 using a2a-sdk 1.0.3.
Run from the hermes-agent venv:
    /home/emil/.hermes/hermes-agent/venv/bin/python tests/a2a_test_agent.py
"""
import asyncio
from a2a.server.agent_execution import AgentExecutor
from a2a.server.tasks import InMemoryTaskStore
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_jsonrpc_routes
from a2a.types import (
    AgentCard,
    Part,
    Message,
    Role,
    TaskState,
    TaskStatus,
    TaskStatusUpdateEvent,
    Task,
)
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse
import uvicorn


class EchoAgent(AgentExecutor):
    """Minimal echo agent — responds with uppercase of the input text."""

    async def execute(self, context, event_queue) -> None:
        # First: enqueue a Task in SUBMITTED state
        task_id = context.task_id or "unknown"
        context_id = context.context_id or ""

        await event_queue.enqueue_event(
            Task(
                id=task_id,
                context_id=context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
            )
        )

        # Extract text from incoming message
        input_text = ""
        if context.message and context.message.parts:
            for part in context.message.parts:
                text = getattr(part, "text", None)
                if text:
                    input_text += text + " "
        input_text = input_text.strip()

        if not input_text:
            input_text = "(empty)"

        # Echo back uppercase
        result = f"[ECHO] {input_text.upper()}"

        # Update: mark COMPLETED with the response message
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=task_id,
                context_id=context_id,
                status=TaskStatus(
                    state=TaskState.TASK_STATE_COMPLETED,
                    message=Message(role=Role.ROLE_AGENT, parts=[Part(text=result)]),
                ),
            )
        )

    async def cancel(self, context, event_queue) -> None:
        await event_queue.enqueue_event(
            TaskStatusUpdateEvent(
                task_id=context.task_id,
                context_id=context.context_id,
                status=TaskStatus(state=TaskState.TASK_STATE_CANCELED),
            )
        )


def create_agent_card() -> AgentCard:
    return AgentCard(
        name="A2A Test Echo Agent",
        description="Minimal echo agent for field-testing hermes-agent-a2a plugin Modes 1-3",
        version="1.0.0",
        supported_interfaces=[
            {
                "url": "http://localhost:41920",
                "protocol_binding": "https://a2a.ai/spec",
                "protocol_version": "1.0.0",
            }
        ],
        provider={"url": "http://localhost:41920"},
        capabilities={"streaming": False, "push_notifications": False},
        skills=[],
    )


def get_agent_card_json():
    """Return Agent Card as JSON dict for the route handler."""
    ac = create_agent_card()
    return {
        "name": ac.name,
        "description": ac.description,
        "version": ac.version,
        "supportedUrl": "http://localhost:41920",
        "capabilities": {
            "streaming": ac.capabilities.streaming if ac.capabilities else False,
            "pushNotifications": ac.capabilities.push_notifications if ac.capabilities else False,
        },
        "skills": [],
    }


def main():
    agent_executor = EchoAgent()
    task_store = InMemoryTaskStore()
    agent_card = create_agent_card()
    request_handler = DefaultRequestHandler(
        agent_executor=agent_executor,
        task_store=task_store,
        agent_card=agent_card,
    )

    jsonrpc_routes = create_jsonrpc_routes(
        request_handler=request_handler,
        rpc_url="/",
        enable_v0_3_compat=True,
    )

    async def agent_card_route(request):
        return JSONResponse(get_agent_card_json())

    agent_card_routes = [
        Route("/.well-known/agent.json", agent_card_route, methods=["GET"]),
    ]

    app = Starlette(routes=jsonrpc_routes + agent_card_routes)

    print("=== A2A Test Agent ===")
    print("Agent Card: http://localhost:41920/.well-known/agent.json")
    print("JSON-RPC:   http://localhost:41920/")
    print("Echoes input in uppercase. Ctrl+C to stop.")
    uvicorn.run(app, host="0.0.0.0", port=41920, log_level="info")


if __name__ == "__main__":
    main()

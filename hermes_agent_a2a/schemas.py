"""A2A tool schemas — what the LLM sees."""

A2A_DISCOVER = {
    "name": "a2a_discover",
    "description": (
        "Discover a remote A2A agent by fetching its Agent Card. "
        "Returns the agent's name, description, capabilities, and supported skills. "
        "Use this before calling an agent to understand what it can do. "
        "Provide either 'url' or 'name' (at least one is required)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Base URL of the remote agent (e.g. http://agent:8081)",
            },
            "name": {
                "type": "string",
                "description": "Name of an agent from the A2A fleet identity registry",
            },
        },
    },
}

A2A_CALL = {
    "name": "a2a_send_task",
    "description": (
        "Send a protocol-level task/message to an A2A-compatible agent over the A2A RPC transport and get its response. "
        "Use a2a_discover first to learn what the agent can do. "
        "Modes: worker_at=caller (local ephemeral), worker_at=target (remote ephemeral), "
        "default (queued webhook delivery)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Base URL of the remote agent",
            },
            "name": {
                "type": "string",
                "description": "Name of an agent from the A2A fleet identity registry (alternative to url)",
            },
            "message": {
                "type": "string",
                "description": "The message or task to send to the remote agent",
            },
            "task_id": {
                "type": "string",
                "description": "Optional task ID for continuing an existing conversation",
            },
            "reply_to_task_id": {
                "type": "string",
                "description": "Task ID this message is replying to (for multi-turn threading)",
            },
            "intent": {
                "type": "string",
                "enum": ["action_request", "review", "consultation", "notification", "instruction"],
                "description": "What kind of message this is",
            },
            "expected_action": {
                "type": "string",
                "enum": ["reply", "forward", "acknowledge"],
                "description": "What you expect the remote agent to do",
            },
            "worker_at": {
                "type": "string",
                "enum": ["caller", "target"],
                "description": (
                    "Where to run the ephemeral worker. "
                    "'caller' (Mode 2): spawn worker on LOCAL machine, bypass HTTP server. "
                    "'target' (Mode 3): HTTP POST to remote agent's A2A server, worker runs on TARGET. "
                    "Omit for default queued delivery (Mode 1 via webhook)."
                ),
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds for ephemeral worker modes (default: 300)",
            },
        },
        "required": ["message"],
    },
}

A2A_LIST = {
    "name": "a2a_list",
    "description": (
        "List all configured remote A2A agents from the fleet identity registry. "
        "Shows agent names, URLs, and descriptions."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

A2A_TELEGRAM = {
    "name": "a2a_send_session_message",
    "description": (
        "Send a message through a target Hermes gateway into its configured platform session context. "
        "The target gateway owns session routing via config.yaml. "
        "Also echoes the same message to the sender's own Telegram DM for visibility when configured. "
        "Auto-pads [a2a][from:<self>][to:<agent>][id:<uuid>][cta:<cta>] header. "
        "Caller passes raw message; tool handles mesh metadata."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The message body to send into the target agent's configured session (header is auto-padded)",
            },
            "agent": {
                "type": "string",
                "description": "Name of the target Hermes mesh peer (e.g. daji, yoyo, jessie, agent0)",
            },
            "cta": {
                "type": "string",
                "description": "Call-to-action: reply | ack | nop (default: reply)",
                "default": "reply",
            },
            "ref": {
                "type": "string",
                "description": "Optional message ID being replied to (for threading)",
            },
        },
        "required": ["message", "agent"],
    },
}

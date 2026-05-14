"""A2A tool schemas — what the LLM sees."""

A2A_HELP = {
    "name": "a2a_help",
    "description": (
        "Explain the hermes-agent-a2a toolset, when to use each A2A tool, "
        "and how Hermes fleet calls differ from external A2A protocol calls."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "topic": {
                "type": "string",
                "enum": ["overview", "protocol", "workers", "sessions", "external_agents", "examples"],
                "description": "Optional help topic to focus on",
                "default": "overview",
            },
        },
    },
}

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
    "name": "a2a_send_protocol_task",
    "description": (
        "Send a protocol-level task/message to an A2A-compatible agent over the A2A RPC transport and get its response. "
        "Use a2a_discover first to learn what the agent can do. "
        "This is the actual A2A protocol task path. It does not spawn ephemeral Hermes workers; use a2a_run_local_agent_task or a2a_run_remote_agent_task for those modes."
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
        },
        "required": ["message"],
    },
}

A2A_RUN_LOCAL_AGENT_TASK = {
    "name": "a2a_run_local_agent_task",
    "description": (
        "Run a target Hermes agent profile as an ephemeral local worker on the caller machine. "
        "This bypasses the target A2A HTTP server and requires the target profile to exist on the caller filesystem."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the local target agent profile to run",
            },
            "message": {
                "type": "string",
                "description": "The task/message to give to the local ephemeral worker",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds for the local worker subprocess (default: 300)",
            },
        },
        "required": ["name", "message"],
    },
}

A2A_RUN_REMOTE_AGENT_TASK = {
    "name": "a2a_run_remote_agent_task",
    "description": (
        "Ask a target Hermes agent to run its own ephemeral worker on the remote/target machine over A2A RPC. "
        "Requires the target to run hermes-agent-a2a and support target-side worker execution."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Name of the target agent from the A2A fleet identity registry",
            },
            "message": {
                "type": "string",
                "description": "The task/message to give to the remote ephemeral worker",
            },
            "task_id": {
                "type": "string",
                "description": "Optional task ID for the remote worker request",
            },
            "timeout": {
                "type": "integer",
                "description": "Timeout in seconds for the remote A2A RPC call and target worker (default: 300)",
            },
        },
        "required": ["name", "message"],
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

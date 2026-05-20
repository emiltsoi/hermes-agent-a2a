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
                "enum": ["overview", "protocol", "workers", "sessions", "external_agents", "external_requirements", "register_external", "security", "troubleshooting", "examples", "architecture"],
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
            "auth_token": {
                "type": "string",
                "description": "Optional bearer token for direct external A2A URL discovery",
            },
            "auth_type": {
                "type": "string",
                "enum": ["none", "bearer", "api_key", "custom_header"],
                "description": "Optional auth mode for direct external A2A URL discovery",
            },
            "auth_header": {
                "type": "string",
                "description": "Header name for api_key/custom_header auth",
            },
            "auth_value": {
                "type": "string",
                "description": "Secret value for api_key/custom_header auth",
            },
            "agent_card_path": {
                "type": "string",
                "description": "Optional Agent Card path for external agents (default: /.well-known/agent.json)",
                "default": "/.well-known/agent.json",
            },
            "register": {
                "type": "boolean",
                "description": "Persist a discovered external agent into the local A2A fleet registry",
                "default": False,
            },
            "register_as": {
                "type": "string",
                "description": "Registry name to use when register=true",
            },
            "rpc_url": {
                "type": "string",
                "description": "JSON-RPC task endpoint URL to save when register=true; defaults to url",
            },
            "auth_token_env": {
                "type": "string",
                "description": "Environment variable name containing bearer token to save in identity.yaml instead of a raw token",
            },
            "auth_value_env": {
                "type": "string",
                "description": "Environment variable name containing api_key/custom_header value to save in identity.yaml instead of a raw secret",
            },
            "register_overwrite": {
                "type": "boolean",
                "description": "Overwrite an existing registered identity with the same name",
                "default": False,
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
                "description": "Base URL or JSON-RPC endpoint of the remote A2A agent",
            },
            "auth_token": {
                "type": "string",
                "description": "Optional bearer token for direct external A2A URL calls",
            },
            "auth_type": {
                "type": "string",
                "enum": ["none", "bearer", "api_key", "custom_header"],
                "description": "Optional auth mode for direct external A2A URL calls",
            },
            "auth_header": {
                "type": "string",
                "description": "Header name for api_key/custom_header auth",
            },
            "auth_value": {
                "type": "string",
                "description": "Secret value for api_key/custom_header auth",
            },
            "name": {
                "type": "string",
                "description": "Name of an agent from the A2A fleet identity registry (alternative to url)",
            },
            "message": {
                "type": "string",
                "description": "The message or task to send to the remote agent",
            },
            "skill": {
                "type": "string",
                "description": "Optional Agent Card skill name/id to target for external A2A agents",
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
            "timeout": {
                "type": "integer",
                "description": "HTTP timeout in seconds for send/get requests (default: 120)",
            },
            "poll_interval": {
                "type": "integer",
                "description": "Seconds between tasks/get polling attempts when the remote task is working (default: 5)",
            },
            "poll_attempts": {
                "type": "integer",
                "description": "Maximum tasks/get polling attempts when the remote task is working (default: 60)",
            },
        },
        "required": ["message"],
    },
}

A2A_CANCEL_PROTOCOL_TASK = {
    "name": "a2a_cancel_protocol_task",
    "description": (
        "Cancel an A2A task. With name/url it sends JSON-RPC tasks/cancel to a remote agent; "
        "without name/url it attempts local Hermes worker cancellation by task_id."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Optional name of an agent from the A2A fleet identity registry for remote cancellation",
            },
            "url": {
                "type": "string",
                "description": "Optional base URL or JSON-RPC endpoint of the remote A2A agent for remote cancellation",
            },
            "task_id": {
                "type": "string",
                "description": "Task ID to cancel locally and/or remotely",
            },
            "auth_token": {
                "type": "string",
                "description": "Optional bearer token for direct external A2A URL calls",
            },
            "auth_type": {
                "type": "string",
                "enum": ["none", "bearer", "api_key", "custom_header"],
                "description": "Optional auth mode for direct external A2A URL calls",
            },
            "auth_header": {
                "type": "string",
                "description": "Header name for api_key/custom_header auth",
            },
            "auth_value": {
                "type": "string",
                "description": "Secret value for api_key/custom_header auth",
            },
            "timeout": {
                "type": "integer",
                "description": "HTTP timeout in seconds for the cancel request (default: 120)",
            },
        },
        "required": ["task_id"],
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
            "task_id": {
                "type": "string",
                "description": "Optional task ID for the local worker request",
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

A2A_ANNOUNCE = {
    "name": "a2a_announce",
    "description": (
        "Announce this agent to a shared A2A registry so other agents can discover it. "
        "The registry URL is read from A2A_REGISTRY_URL (env var) by default; "
        "pass 'url' to override per-call. "
        "Announces the agent's full Agent Card including name, URL, capabilities, and skills. "
        "Use a2a_list to verify the announcement succeeded."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "Registry URL override (defaults to A2A_REGISTRY_URL env var)",
            },
            "auth_token": {
                "type": "string",
                "description": "Bearer token for the registry (defaults to A2A_REGISTRY_AUTH_TOKEN env var)",
            },
            "auth_type": {
                "type": "string",
                "enum": ["none", "bearer", "api_key", "custom_header"],
                "description": "Auth type for registry (defaults to bearer)",
            },
            "auth_header": {
                "type": "string",
                "description": "Header name for api_key/custom_header auth",
            },
            "auth_value": {
                "type": "string",
                "description": "Secret value for api_key/custom_header auth",
            },
        },
    },
}

A2A_TELEGRAM = {
    "name": "a2a_send_session_message",
    "description": (
        "Send a one-way message through a target Hermes gateway into its configured platform session context. "
        "The target gateway owns session routing via config.yaml. "
        "Returns delivery/relay status only; it does not wait for or guarantee the recipient's semantic reply. "
        "Also echoes the same message to the sender's own Telegram DM for visibility when configured. "
        "Auto-pads [a2a][from:<self>][to:<agent>][id:<uuid>][action:<action>][reply:<reply>] header. "
        "Caller passes raw message; tool handles mesh metadata."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "message": {
                "type": "string",
                "description": "The one-way message body to send into the target agent's configured session (header is auto-padded)",
            },
            "agent": {
                "type": "string",
                "description": "Name of the target Hermes mesh peer (e.g. daji, yoyo, jessie, agent0)",
            },
            "action": {
                "type": "string",
                "enum": ["do", "info"],
                "description": "Action type: do (recipient should take action) | info (information only, log or acknowledge)",
                "default": "do",
            },
            "reply": {
                "type": "string",
                "enum": ["yes", "no"],
                "description": "Reply expectation: yes (sender expects reply) | no (fire-and-forget)",
                "default": "yes",
            },
            "ref": {
                "type": "string",
                "description": "Optional message ID being replied to (for threading)",
            },
        },
        "required": ["message", "agent"],
    },
}

A2A_GET_METRICS = {
    "name": "a2a_get_metrics",
    "description": (
        "Get current A2A plugin metrics including uptime, webhook delivery statistics, "
        "task counts, and queue depth. Useful for monitoring plugin health and performance."
    ),
    "parameters": {
        "type": "object",
        "properties": {},
    },
}

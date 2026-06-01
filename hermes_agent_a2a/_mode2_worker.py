#!/usr/bin/env python3
"""
Mode 2 ephemeral A2A worker — runs as a subprocess with the hermes-agent venv.
Reads params from stdin (JSON), writes result to stdout (JSON), errors to stderr.

Usage: python _mode2_worker.py < stdin > stdout
"""
import sys
import os
import json
import uuid

MAX_STDIN_BYTES = 1 * 1024 * 1024  # 1 MB hard limit

def main():
    raw = sys.stdin.buffer.read(MAX_STDIN_BYTES + 1)
    if len(raw) > MAX_STDIN_BYTES:
        raise RuntimeError(
            f"stdin input exceeds {MAX_STDIN_BYTES} byte limit. "
            "If this is a large prompt, consider splitting the work."
        )
    params = json.loads(raw)

    agent_home = params["agent_home"]
    message = params["message"]
    timeout = params.get("timeout", 300)

    # Resolve HERMES_HOME from params or environment — must be explicit for Mode 2.
    # Inherit HERMES_HOME from parent environment if not passed in params.
    _hermes_home = params.get("hermes_home") or os.environ.get("HERMES_HOME", "")
    if not _hermes_home:
        print("ERROR: hermes_home not set in params or HERMES_HOME env var", file=sys.stderr)
        sys.exit(1)
    _hermes_agent = os.path.join(_hermes_home, "hermes-agent")
    _plugin_dir = os.path.dirname(os.path.abspath(__file__))
    # Move hermes-agent to front, keep plugin dir at front if present
    new_path = [_hermes_agent]
    for p in sys.path:
        if p == _hermes_agent or p == _plugin_dir or p.startswith(_plugin_dir + os.sep):
            continue
        new_path.append(p)
    sys.path[:] = new_path

    # HERMES_HOME controls profile resolution, SOUL loading, etc.
    os.environ["HERMES_HOME"] = agent_home

    # Re-load hermes_constants so get_hermes_home() picks up the new HERMES_HOME
    import importlib
    import hermes_constants
    importlib.reload(hermes_constants)

    # Load target profile's .env for API keys
    from hermes_cli.env_loader import load_hermes_dotenv
    load_hermes_dotenv(hermes_home=agent_home)

    # Read provider and model from target profile config
    import yaml
    profile_config_path = os.path.join(agent_home, "config.yaml")
    cfg = {}
    if os.path.isfile(profile_config_path):
        with open(profile_config_path) as f:
            cfg = yaml.safe_load(f) or {}
    model_cfg = cfg.get("model", {}) or {}
    target_provider = model_cfg.get("provider", "minimax-cn") if isinstance(model_cfg, dict) else "minimax-cn"
    target_model = model_cfg.get("default", "MiniMax-M2.7") if isinstance(model_cfg, dict) else "MiniMax-M2.7"

    if target_provider == "minimax-cn":
        target_api_mode = "anthropic_messages"
    elif target_provider == "anthropic":
        target_api_mode = "anthropic_messages"
    else:
        target_api_mode = "chat_completions"

    # Resolve API credentials directly from provider registry
    from hermes_cli.auth import resolve_api_key_provider_credentials
    creds = resolve_api_key_provider_credentials(target_provider)

    # Suppress AIAgent's startup banner — it prints emoji to stdout which
    # would corrupt the JSON response. Capture and discard; stderr is unaffected.
    import io
    _orig_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        from run_agent import AIAgent
        agent = AIAgent(
            max_iterations=90,
            skip_context_files=False,
            skip_memory=True,
            load_soul_identity=True,
            session_id=f"a2a-m2-{uuid.uuid4().hex[:8]}",
            model=target_model,
            provider=target_provider,
            api_mode=target_api_mode,
            api_key=creds.get("api_key"),
            base_url=creds.get("base_url"),
        )
        conv_result = agent.run_conversation(message)
    finally:
        sys.stdout = _orig_stdout

    final = ""
    if isinstance(conv_result, dict):
        final = conv_result.get("final_response", "") or str(conv_result)
    else:
        final = str(conv_result)

    result = {
        "task_id": f"a2a-m2-{uuid.uuid4().hex[:8]}",
        "state": "completed",
        "response": final.strip(),
        "source": f"ephemeral:{os.path.basename(agent_home)}",
    }

    print(json.dumps(result), flush=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)

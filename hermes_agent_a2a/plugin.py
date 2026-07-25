"""HermesA2A plugin entry point."""
import logging
import os
import threading


from importlib.metadata import version as _get_metadata_version

logger = logging.getLogger(__name__)


def _get_version() -> str:
    """Read version from installed package metadata."""
    try:
        return _get_metadata_version("hermes-agent-a2a")
    except Exception:
        return "3.0.0"


__version__ = _get_version()

_server_process = None


def _load_rate_limit_config() -> "RateLimitConfig":
    """Load rate_limit section from config.yaml and build RateLimitConfig dataclass.

    Falls back to defaults (disabled) when config.yaml is unavailable
    or the section is missing.
    """
    from .rate_limiter import RateLimitConfig

    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        rl_section = cfg.get("rate_limit", {}) if isinstance(cfg, dict) else {}
    except Exception:
        rl_section = {}

    if not rl_section:
        return RateLimitConfig()  # all defaults, disabled

    return RateLimitConfig(
        enabled=bool(rl_section.get("enabled", False)),
        requests_per_window=int(rl_section.get("requests_per_window", 100)),
        window_seconds=int(rl_section.get("window_seconds", 60)),
        burst_multiplier=float(rl_section.get("burst_multiplier", 2.0)),
        header_name=str(rl_section.get("header_name", "X-Forwarded-For")),
        cleanup_interval_seconds=int(rl_section.get("cleanup_interval_seconds", 300)),
        max_entries=int(rl_section.get("max_entries", 10000)),
    )


def _start_a2a_server() -> None:
    """Start A2A HTTP server as a daemon thread inside the gateway process.

    Both the server thread and the gateway hooks share builtins._hermes_a2a_runtime_state,
    so they access the same task queue without any IPC.
    """
    global _server_process
    if _server_process is not None and _server_process.is_alive():
        return  # already running

    # Verify worker script exists before starting server
    from pathlib import Path
    worker_script = Path(__file__).parent / "_mode2_worker.py"
    if not worker_script.exists():
        raise RuntimeError(
            f"Required worker script not found: {worker_script}. "
            "Please ensure the plugin is installed correctly."
        )

    from .server import A2AServer, set_runtime_server

    rate_limit_config = _load_rate_limit_config()

    port = int(os.getenv("A2A_PORT", "8081"))
    host = os.getenv("A2A_HOST", "127.0.0.1")
    max_port_retries = int(os.getenv("A2A_PORT_RETRY_MAX", "10"))

    server = None
    for attempt in range(max_port_retries):
        try:
            server = A2AServer(host, port, rate_limit_config=rate_limit_config)
            break
        except OSError as e:
            if "Address already in use" in str(e) and attempt < max_port_retries - 1:
                port += 1
                logger.warning("[HermesA2A] Port %d in use, retrying with %d", port - 1, port)
            else:
                raise

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _server_process = thread

    set_runtime_server(server, thread)

    logger.info("[HermesA2A] A2A HTTP server started as thread on %s:%s", host, port)


def _get_vault_resolver():
    """Lazily create VaultResolver (requires config dict, not PluginContext)."""
    from . import identity as identity_module
    from hermes_cli.config import load_config
    cfg = load_config()
    return identity_module.VaultResolver(cfg)


class HermesAgentA2APlugin:
    name = "hermes-agent-a2a"
    version = __version__

    def register(self, registry) -> None:
        """Register tools and hooks. Server starts lazily on first tool call."""
        from . import hooks as hooks_module
        from . import tools as tools_module
        from .runtime_state import _start_metrics_logger

        registry.register_hook("pre_llm_call", hooks_module.pre_llm_call)
        registry.register_hook("post_llm_call", hooks_module.post_llm_call)
        registry.register_hook("pre_gateway_dispatch", hooks_module.pre_gateway_dispatch)
        logger.info("[HermesA2A] Phase 2 hooks registered")

        tools_module.register(registry, _start_a2a_server, _get_vault_resolver)
        logger.info("[HermesA2A] Phase 3 tools registered")

        # Boot-strap identity validation — fail fast if A2A transport identity is missing or unresolved
        vault_resolver = _get_vault_resolver()
        identity = vault_resolver.resolve()
        from .validators import BootValidator
        BootValidator(vault_resolver).validate(identity)
        logger.info("[HermesA2A] BootValidator passed")

        # Start A2A server eagerly on plugin load — no need to wait for first tool call
        _start_a2a_server()

        # Start metrics logger if enabled
        _start_metrics_logger()

    def on_shutdown(self) -> None:
        """Stop the A2A server thread, rate limiter cleanup, and SSE streamer."""
        logger.info("[HermesA2A] shutdown — stopping A2A server thread and SSE streamer")
        from .runtime_state import get_runtime_state
        from .server import clear_runtime_server
        from .sse_handler import get_sse_streamer
        try:
            state = get_runtime_state()
            server = state.get_server()
            if server:
                clear_runtime_server(server)
                server.limiter.stop_cleanup()
                server.shutdown()
        except Exception as exc:
            logger.debug("Error shutting down A2A server: %s", exc)

        try:
            streamer = get_sse_streamer()
            streamer.shutdown()
        except Exception as exc:
            logger.debug("Error shutting down SSE streamer: %s", exc)

def register(registry) -> None:
    """Entry point for hermes plugin system."""
    plugin = HermesAgentA2APlugin()
    plugin.register(registry)


__all__ = ["HermesAgentA2APlugin", "register", "__version__"]

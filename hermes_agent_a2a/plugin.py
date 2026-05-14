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


def _ensure_a2a_server() -> None:
    """Start A2A HTTP server as a daemon thread inside the gateway process.

    Both the server thread and the gateway hooks share builtins._hermes_a2a_runtime_state,
    so they access the same task queue without any IPC.
    """
    global _server_process
    if _server_process is not None and _server_process.is_alive():
        return  # already running

    from .server import A2AServer, set_runtime_server

    port = int(os.getenv("A2A_PORT", "8081"))
    host = os.getenv("A2A_HOST", "127.0.0.1")

    server = A2AServer(host, port)
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

        registry.register_hook("pre_llm_call", hooks_module.pre_llm_call)
        registry.register_hook("post_llm_call", hooks_module.post_llm_call)
        registry.register_hook("pre_gateway_dispatch", hooks_module.pre_gateway_dispatch)
        logger.info("[HermesA2A] Phase 2 hooks registered")

        tools_module.register(registry, _ensure_a2a_server, _get_vault_resolver)
        logger.info("[HermesA2A] Phase 3 tools registered")

        # Start A2A server eagerly on plugin load — no need to wait for first tool call
        _ensure_a2a_server()

    def on_shutdown(self) -> None:
        """Stop the A2A server thread."""
        logger.info("[HermesA2A] shutdown — stopping A2A server thread")
        from .server import clear_runtime_server, get_runtime_state
        try:
            state = get_runtime_state()
            server = state.get("server")
            if server:
                clear_runtime_server(server)
                server.shutdown()
        except Exception as exc:
            logger.debug("Error shutting down A2A server: %s", exc)


class HermesA2AV2Plugin(HermesA2AV3Plugin):
    """Alias for backward compatibility with existing integrations."""
    pass


def register(registry) -> None:
    """Entry point for hermes plugin system."""
    plugin = HermesAgentA2APlugin()
    plugin.register(registry)


__all__ = ["HermesAgentA2APlugin", "register", "__version__"]

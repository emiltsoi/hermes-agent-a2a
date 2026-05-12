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

# Module-level server state (survives plugin reloads)
_server_started = False
_server_instance = None
_server_thread = None
_init_lock = threading.Lock()


def _ensure_a2a_server() -> None:
    """Lazily start the A2A HTTP server. Thread-safe, idempotent."""
    global _server_started, _server_instance, _server_thread
    if _server_started:
        return
    with _init_lock:
        if _server_started:
            return
        port = int(os.getenv("A2A_PORT", "8081"))
        host = os.getenv("A2A_HOST", "127.0.0.1")
        try:
            import hermes_agent_a2a.server as a2a_server_module
            _server_instance = a2a_server_module.A2AServer(host, port)
            _server_thread = threading.Thread(
                target=_server_instance.serve_forever,
                name="a2a-server",
                daemon=True,
            )
            _server_thread.start()
            a2a_server_module.set_runtime_server(_server_instance, _server_thread)
            _server_started = True
            logger.info("[HermesA2A] A2A HTTP server started on %s:%s", host, port)
        except Exception:
            logger.exception("[HermesA2A] Failed to start A2A server")


def _get_vault_resolver():
    """Lazily create VaultResolver (requires config dict, not PluginContext)."""
    import hermes_agent_a2a.identity as identity_module
    from hermes_cli.config import load_config
    cfg = load_config()
    return identity_module.VaultResolver(cfg)


class HermesA2AV3Plugin:
    name = "hermes-agent-a2a"
    version = __version__

    def register(self, registry) -> None:
        """Register tools and hooks. Server starts lazily on first tool call."""
        import hermes_agent_a2a.hooks as hooks_module
        import hermes_agent_a2a.tools as tools_module

        registry.register_hook("pre_llm_call", hooks_module.pre_llm_call)
        registry.register_hook("post_llm_call", hooks_module.post_llm_call)
        registry.register_hook("pre_gateway_dispatch", hooks_module.pre_gateway_dispatch)
        logger.info("[HermesA2A] Phase 2 hooks registered")

        tools_module.register(registry, _ensure_a2a_server, _get_vault_resolver)
        logger.info("[HermesA2A] Phase 3 tools registered")

        # Start A2A server eagerly on plugin load — no need to wait for first tool call
        _ensure_a2a_server()

    def on_shutdown(self) -> None:
        """Stop the A2A server gracefully."""
        global _server_instance, _server_started
        logger.info("[HermesA2A] shutdown — stopping A2A server")
        try:
            if _server_instance is not None:
                import hermes_agent_a2a.server as a2a_server_module
                a2a_server_module.clear_runtime_server(_server_instance)
                _server_instance.shutdown()
                _server_started = False
        except Exception as exc:
            logger.debug("Error shutting down A2A server: %s", exc)


class HermesA2AV2Plugin(HermesA2AV3Plugin):
    """Alias for backward compatibility with existing integrations."""
    pass


def register(registry) -> None:
    """Entry point for hermes plugin system."""
    plugin = HermesA2AV3Plugin()
    plugin.register(registry)


__all__ = ["HermesA2AV2Plugin", "HermesA2AV3Plugin", "register", "__version__"]

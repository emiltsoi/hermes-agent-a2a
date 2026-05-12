"""HermesA2A plugin entry point."""
import logging
import os
import threading
from pathlib import Path
from functools import lru_cache

from .identity import VaultResolver
from .bootstrap import AutoSourceBootstrap
from .validators import BootValidator
from . import server as a2a_server_module
from . import hooks

logger = logging.getLogger(__name__)


@lru_cache(maxsize=1)
def _get_version() -> str:
    """Read version from pyproject.toml, cached after first parse."""
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    try:
        import tomllib
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        return data["project"]["version"]
    except Exception:
        return "0.0.0"


__version__ = _get_version()


class HermesA2AV3Plugin:
    name = "hermes-agent-a2a"
    version = "3.0.0"

    def __init__(self, config: dict):
        self.config = config
        self.vault_resolver = VaultResolver(config)
        self.bootstrap = AutoSourceBootstrap(config, self.vault_resolver)
        self.validator = BootValidator(self.vault_resolver)
        self._server = None
        self._server_thread = None

    def register(self, registry) -> None:
        """Register tools and hooks with the Hermes gateway registry."""
        # Register A2A hooks so the gateway calls them at the right points
        registry.hooks.register("pre_llm_call", hooks.pre_llm_call)
        registry.hooks.register("post_llm_call", hooks.post_llm_call)
        registry.hooks.register("pre_gateway_dispatch", hooks.pre_gateway_dispatch)
        logger.info("[HermesA2A] Phase 2 hooks registered")

    def on_boot(self) -> None:
        """Start the A2A HTTP server before the gateway accepts messages."""
        port = int(os.getenv("A2A_PORT", "8081"))
        host = os.getenv("A2A_HOST", "127.0.0.1")

        try:
            self._server = a2a_server_module.A2AServer(host, port)
            self._server_thread = threading.Thread(
                target=self._server.serve_forever,
                name="a2a-server",
                daemon=True,
            )
            self._server_thread.start()
            a2a_server_module.set_runtime_server(self._server, self._server_thread)
            logger.info(
                "[HermesA2A] A2A HTTP server started on %s:%s",
                host,
                port,
            )
        except Exception as exc:
            logger.error("[HermesA2A] Failed to start A2A server: %s", exc)

    def on_shutdown(self) -> None:
        """Stop the A2A server gracefully."""
        logger.info("[HermesA2A] shutdown — stopping A2A server")
        try:
            a2a_server_module.clear_runtime_server(self._server)
            if self._server is not None:
                self._server.shutdown()
        except Exception as exc:
            logger.debug("Error shutting down A2A server: %s", exc)

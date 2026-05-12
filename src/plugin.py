"""HermesA2A plugin entry point."""
import logging
from pathlib import Path
from functools import lru_cache

from .identity import VaultResolver
from .bootstrap import AutoSourceBootstrap
from .validators import BootValidator

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

    def register(self, registry) -> None:
        """Phase 1 — no tools registered yet."""
        pass

    def on_boot(self) -> None:
        """Run before the gateway starts accepting messages."""
        logger.info("Phase 1 — identity only, no tools registered.")

    def on_shutdown(self) -> None:
        """Run on gateway shutdown."""
        logger.info("[HermesA2A] shutdown")

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


class HermesA2APlugin:
    name = "hermes-agent-a2a"
    version = __version__

    def __init__(self, config: dict):
        self.config = config
        self.vault_resolver = VaultResolver(config)
        self.bootstrap = AutoSourceBootstrap(config, self.vault_resolver)
        self.validator = BootValidator(self.vault_resolver)

    def on_boot(self) -> None:
        """Run before the gateway starts accepting messages."""
        resolved_identity = self.vault_resolver.resolve()
        self.validator.validate(resolved_identity)
        token = (
            resolved_identity.get("platforms", {})
            .get("telegram", {})
            .get("bot_token", "")
        )
        self.validator.validate_token_with_telegram(token)
        self.bootstrap.bootstrap_routes(self.config)
        logger.info("[HermesA2A] boot complete -- identity resolved, routes bootstrapped, token verified")

    def on_shutdown(self) -> None:
        """Run on gateway shutdown."""
        logger.info("[HermesA2A] shutdown")

"""HermesA2A plugin entry point."""
import logging
from .identity import VaultResolver
from .bootstrap import AutoSourceBootstrap
from .validators import BootValidator

logger = logging.getLogger(__name__)


class HermesA2AV2Plugin:
    name = "hermes-a2a-v2"
    version = "0.1.0"

    def __init__(self, config: dict):
        self.config = config
        self.vault_resolver = VaultResolver(config)
        self.bootstrap = AutoSourceBootstrap(config, self.vault_resolver)
        self.validator = BootValidator(self.vault_resolver)

    def on_boot(self) -> None:
        """Run before the gateway starts accepting messages."""
        resolved_identity = self.vault_resolver.resolve()
        self.validator.validate(resolved_identity)
        self.bootstrap.bootstrap_routes(self.config)
        logger.info("[HermesA2A] boot complete -- identity resolved, routes bootstrapped")

    def on_shutdown(self) -> None:
        """Run on gateway shutdown."""
        logger.info("[HermesA2A] shutdown")

"""Auto-source bootstrap for A2A routes.

If source: block is absent from a route config, bootstrap from resolved vault defaults.
Explicit source blocks always win over auto.

Ehrlich & Lindstrom — HermesA2A 2026.
"""
import logging
from typing import Optional

logger = logging.getLogger(__name__)


class AutoSourceBootstrap:
    """Auto-resolves source metadata from vault if not explicitly configured."""

    def __init__(self, config: dict, vault_resolver):
        self.config = config
        self.vault = vault_resolver

    def bootstrap_route(
        self,
        route_name: str,
        route_cfg: dict,
        inbound_context: Optional[dict] = None,
    ) -> dict:
        """Return a source dict for a route. Priority: inbound > explicit > vault > defaults."""
        if route_cfg.get("source"):
            logger.debug(f"[AutoSourceBootstrap] route {route_name}: using explicit source")
            return route_cfg["source"]

        if inbound_context:
            logger.debug(f"[AutoSourceBootstrap] route {route_name}: bootstrapping from inbound context")
            return {
                "platform": inbound_context.get("platform", "telegram"),
                "chat_type": inbound_context.get("chat_type", "dm"),
                "chat_id": inbound_context.get("chat_id"),
                "user_id": inbound_context.get("user_id"),
                "user_name": inbound_context.get("user_name"),
            }

        resolved = self.vault.resolve()
        telegram_cfg = resolved.get("platforms", {}).get("telegram", {})
        defaults = resolved.get("defaults", {})

        logger.debug(f"[AutoSourceBootstrap] route {route_name}: bootstrapping from vault defaults")
        return {
            "platform": defaults.get("platform", "telegram"),
            "chat_type": defaults.get("chat_type", "dm"),
            "chat_id": telegram_cfg.get("default_chat_id"),
            "user_id": telegram_cfg.get("default_chat_id"),
        }

    def bootstrap_routes(self, config: dict, inbound_context: Optional[dict] = None) -> None:
        """Walk all webhook routes in config and fill in missing source blocks."""
        routes = (
            config.get("webhook", {})
            .get("extra", {})
            .get("routes", {})
        )
        for route_name, route_cfg in routes.items():
            if not route_cfg.get("source"):
                bootstrapped = self.bootstrap_route(route_name, route_cfg, inbound_context=inbound_context)
                route_cfg["source"] = bootstrapped
                logger.info(f"[AutoSourceBootstrap] bootstrapped route {route_name}: {bootstrapped}")

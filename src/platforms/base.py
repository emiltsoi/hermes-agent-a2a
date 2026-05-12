"""Base platform handler."""


class PlatformHandler:
    """Abstract base for platform-specific message sending."""

    name = "base"

    def send_message(self, token: str, chat_id: str, text: str, **kwargs) -> dict:
        raise NotImplementedError

    def get_me(self, token: str) -> dict:
        raise NotImplementedError

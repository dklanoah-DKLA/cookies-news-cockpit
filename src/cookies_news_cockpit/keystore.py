from __future__ import annotations

import os


class KeyStoreError(RuntimeError):
    pass


class DeepSeekKeyStore:
    """Store the API key in the OS credential vault, never in app files."""

    service = "cookies-news-cockpit"
    username = "deepseek-api-key"

    def get(self) -> str | None:
        # An environment override is useful for development and headless CI; it
        # is not written to an export or app data.
        value = os.getenv("DEEPSEEK_API_KEY")
        if value:
            return value.strip() or None
        try:
            import keyring

            return keyring.get_password(self.service, self.username)
        except Exception:
            return None

    def set(self, api_key: str) -> None:
        try:
            import keyring

            keyring.set_password(self.service, self.username, api_key)
        except Exception as exc:
            raise KeyStoreError("系统钥匙串不可用，未保存 API Key") from exc

    def delete(self) -> None:
        try:
            import keyring

            existing = keyring.get_password(self.service, self.username)
            if existing:
                keyring.delete_password(self.service, self.username)
        except Exception as exc:
            raise KeyStoreError("系统钥匙串不可用，未删除 API Key") from exc

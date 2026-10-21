"""Image provider registry."""

from __future__ import annotations

from weaver.config import Settings
from weaver.providers.base import AuthError, ImageProvider
from weaver.secrets import ACCOUNT_ID, API_TOKEN, get_credential


def get_provider(settings: Settings, model: str | None = None) -> ImageProvider:
    """``model`` overrides weaver.yaml for one run (it must have a known price)."""
    if settings.provider_name == "cloudflare":
        from weaver.providers.cloudflare import CloudflareWorkersAI

        account = get_credential(ACCOUNT_ID).value
        token = get_credential(API_TOKEN).value
        if not account or not token:
            raise AuthError(
                "Cloudflare credentials not found. Set CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_API_TOKEN "
                "as environment variables or in the git-ignored .env file (see .env.example)."
            )
        return CloudflareWorkersAI(
            account, token, model or settings.model,
            timeout_s=settings.request_timeout_s, max_retries=settings.max_retries,
        )
    raise ValueError(f"Unknown provider '{settings.provider_name}'")

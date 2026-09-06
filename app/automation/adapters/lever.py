from __future__ import annotations

from app.automation.adapters.greenhouse import _EmbeddedProviderAdapter
from app.automation.targets import trusted_provider_for_url


class LeverAdapter(_EmbeddedProviderAdapter):
    name = "lever"

    @classmethod
    def matches(cls, url: str, html: str = "") -> bool:
        del html  # DOM labels are mutable and cannot establish provider trust.
        return trusted_provider_for_url(url) == "lever"

from __future__ import annotations

from app.automation.adapters.greenhouse import _EmbeddedProviderAdapter
from app.automation.targets import trusted_provider_for_url


class SmartRecruitersAdapter(_EmbeddedProviderAdapter):
    name = "smartrecruiters"

    @classmethod
    def matches(cls, url: str, html: str = "") -> bool:
        del html
        return trusted_provider_for_url(url) == cls.name

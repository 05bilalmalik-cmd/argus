from __future__ import annotations

from typing import Protocol

from playwright.sync_api import Page

from app.automation.types import InspectedField


class AtsAdapter(Protocol):
    name: str

    @classmethod
    def matches(cls, url: str, html: str = "") -> bool: ...

    def inspect(self, page: Page) -> list[InspectedField]: ...

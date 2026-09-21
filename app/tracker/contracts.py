"""Data-only boundary between public discovery and the local tracker."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class Listing:
    employer: str
    title: str
    url: str
    location: str = ""
    programme: str = ""
    source_id: str = ""
    posted_at: str | None = None
    deadline: str | None = None
    description: str = ""
    deadline_text: str = ""
    posted_text: str = ""


@dataclass(frozen=True, slots=True)
class SourceResult:
    name: str
    url: str
    status: str
    listings: list[Listing] = field(default_factory=list)
    error: str = ""
    checked_at: str = field(default_factory=utc_now)
    elapsed_seconds: float = 0.0

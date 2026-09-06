"""Truthful, fail-closed application-window state for scouting inventory."""

from __future__ import annotations

import re
from datetime import date
from enum import StrEnum
from urllib.parse import urlsplit


class ApplicationWindowStatus(StrEnum):
    """Whether an opportunity can be worked at the present time."""

    OPEN = "OPEN"
    NOT_YET_OPEN = "NOT_YET_OPEN"
    CLOSED = "CLOSED"
    UNKNOWN = "UNKNOWN"

    @property
    def work_eligible(self) -> bool:
        return self is ApplicationWindowStatus.OPEN


_EXPLICIT_OPEN = frozenset(
    {
        "active",
        "application_open",
        "applications_open",
        "live",
        "open",
        "opened",
    }
)
_EXPLICIT_WAITING = frozenset(
    {
        "coming_soon",
        "not_open",
        "not_yet_open",
        "opens_soon",
        "pending",
        "tbc",
        "to_be_confirmed",
        "upcoming",
    }
)
_EXPLICIT_CLOSED = frozenset(
    {
        "application_closed",
        "applications_closed",
        "closed",
        "expired",
        "filled",
        "inactive",
    }
)


def _normalise_explicit_status(value: object) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().casefold()).strip("_")


def tracker_owned_host(raw_url: str | None) -> bool:
    """Return true only for Trackr-controlled hosts, not attribution queries."""

    if not raw_url:
        return False
    try:
        host = (urlsplit(raw_url).hostname or "").casefold().rstrip(".")
    except ValueError:
        return False
    return host == "the-trackr.com" or host.endswith(".the-trackr.com")


def derive_application_window(
    *,
    explicit_status: object = None,
    opening_date: date | None = None,
    closing_date: date | None = None,
    application_url: str | None = None,
    link_signal_known: bool = False,
    today: date | None = None,
) -> ApplicationWindowStatus:
    """Derive window state using the brief's strict evidence priority.

    A non-empty but unrecognised explicit status is authoritative uncertainty;
    it does not fall through to weaker date or URL inference.
    """

    if explicit_status is not None and str(explicit_status).strip():
        if isinstance(explicit_status, bool):
            return (
                ApplicationWindowStatus.OPEN
                if explicit_status
                else ApplicationWindowStatus.CLOSED
            )
        normalised = _normalise_explicit_status(explicit_status)
        if normalised in _EXPLICIT_OPEN:
            return ApplicationWindowStatus.OPEN
        if normalised in _EXPLICIT_WAITING:
            return ApplicationWindowStatus.NOT_YET_OPEN
        if normalised in _EXPLICIT_CLOSED:
            return ApplicationWindowStatus.CLOSED
        return ApplicationWindowStatus.UNKNOWN

    if opening_date is not None and not isinstance(opening_date, date):
        return ApplicationWindowStatus.UNKNOWN
    if closing_date is not None and not isinstance(closing_date, date):
        return ApplicationWindowStatus.UNKNOWN

    current = today or date.today()
    if opening_date is not None and closing_date is not None:
        if opening_date > closing_date:
            return ApplicationWindowStatus.UNKNOWN
    if closing_date is not None and closing_date < current:
        return ApplicationWindowStatus.CLOSED
    if opening_date is not None and opening_date > current:
        return ApplicationWindowStatus.NOT_YET_OPEN
    if opening_date is not None and opening_date <= current:
        return ApplicationWindowStatus.OPEN
    if closing_date is not None and closing_date >= current:
        return ApplicationWindowStatus.OPEN

    if application_url and not tracker_owned_host(application_url):
        return ApplicationWindowStatus.OPEN
    if link_signal_known:
        return ApplicationWindowStatus.NOT_YET_OPEN
    return ApplicationWindowStatus.UNKNOWN


def coerce_application_window(value: object) -> ApplicationWindowStatus:
    """Coerce persisted/external input to fail-closed window state."""

    try:
        return ApplicationWindowStatus(str(value or ""))
    except ValueError:
        return ApplicationWindowStatus.UNKNOWN

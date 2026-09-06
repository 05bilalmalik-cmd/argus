"""Greenhouse job-board API source.

Endpoint pattern (public, unauthenticated) — the boards-api JSON host is the
canonical feed (the /embed/job_board HTML endpoint 404s for scripted clients):

    https://boards-api.greenhouse.io/v1/boards/<org>/jobs

returns JSON ``{"jobs": [{"title", "absolute_url", "location": {"name"},
"updated_at", ...}]}``.
"""
from __future__ import annotations

import json
from typing import Any

from app.scouting.programmes import classify_programme
from app.scouting.sources.base import (
    _ats_from_url,
    _clean,
    http_get_json,
    is_early_careers,
)
from app.scouting.trackr import ScrapedOpportunity

ENDPOINT = "https://boards-api.greenhouse.io/v1/boards/{org}/jobs"

# Curated org slugs — LIVE-VERIFIED 2026-08-23 against boards-api.greenhouse.io
# (each slug returned HTTP 200; dead slugs removed after probe). Extend freely;
# unknown slugs simply 404 and the per-source try/except in collect_all()
# skips them.
ORGS: tuple[str, ...] = (
    # citadel 404s on boards-api (own ATS) — not listed
    "point72",
    "jumptrading",
    "akunacapital",
    "optiver",              # board exists (0 jobs at probe time)
    "imc",
    "flowtraders",
    "janestreet",
    "liontree",
    "williamblair",
    "mavensecuritiesholdingltd",
    "exoduspoint",
    "schonfeld",
    "marshallwace",         # board exists (0 jobs at probe time)
)


def parse_payload(raw: Any, org: str) -> list[ScrapedOpportunity]:
    """Parse a Greenhouse embed payload (JSON string or decoded object).

    Tolerant of malformed input: returns [] on undecodable strings, wrong
    shapes, or individual jobs with missing fields.
    """
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []
    else:
        payload = raw
    if isinstance(payload, dict):
        jobs = payload.get("jobs")
    elif isinstance(payload, list):  # some embed variants return a bare list
        jobs = payload
    else:
        return []
    if not isinstance(jobs, list):
        return []

    employer = org.replace("-", " ").strip()
    out: list[ScrapedOpportunity] = []
    for job in jobs:
        if not isinstance(job, dict):
            continue
        title = _clean(job.get("title"))
        url = _clean(job.get("absolute_url") or job.get("url"))
        if not title or not url.startswith("http"):
            continue
        location = job.get("location")
        loc_name = location.get("name") if isinstance(location, dict) else location
        updated_at = _clean(job.get("updated_at"))
        out.append(
            ScrapedOpportunity(
                employer=employer,
                role_title=title,
                source_url=url,
                # ``absolute_url`` is supplied by the official Greenhouse
                # board feed for this exact structured job record.
                application_url=url,
                location=_clean(loc_name),
                ats_type="greenhouse",
                programme_type=classify_programme(title),
                source=f"greenhouse:{org}",
            )
        )
    return out


def fetch_org(org: str) -> list[ScrapedOpportunity]:
    """Fetch one org's board and keep only early-careers roles."""
    payload = http_get_json(ENDPOINT.format(org=org))
    rows = [row for row in parse_payload(payload, org) if is_early_careers(row.role_title)]
    for row in rows:
        row.ats_type = _ats_from_url(row.url) or "greenhouse"
        row.source = f"greenhouse:{org}"
    return rows

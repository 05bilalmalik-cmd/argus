"""Lever postings API source.

Endpoint pattern (public, unauthenticated):

    https://api.lever.co/v0/postings/<company>?mode=json

returns a JSON list of postings shaped like::

    {"text": "<title>", "hostedUrl": "https://jobs.lever.co/<co>/<id>",
     "categories": {"location": "...", "commitment": "Internship"},
     "createdAt": 1700000000000, ...}

Postings whose ``commitment`` is present and clearly not an internship are
dropped; when ``commitment`` is absent the title filter decides.
"""
from __future__ import annotations

import json
from typing import Any

from app.scouting.programmes import classify_programme
from app.scouting.sources.base import (
    _clean,
    http_get_json,
    is_early_careers,
)
from app.scouting.trackr import ScrapedOpportunity

ENDPOINT = "https://api.lever.co/v0/postings/{company}?mode=json"

# Curated company slugs for public Lever boards. Lever is less common among
# UK finance targets than Greenhouse, so this list is short — extend only with
# slugs you have verified (a wrong slug just 404s and gets skipped).
COMPANIES: tuple[str, ...] = (
    # UNCERTAIN: squarepoint runs a careers site; lever slug unverified.
    "squarepoint",
)


def _commitment_is_internship(commitment: str) -> bool:
    if not commitment:
        return True  # absent commitment: let the title filter decide
    folded = commitment.casefold()
    return "intern" in folded or "placement" in folded


def parse_postings(raw: Any, company: str) -> list[ScrapedOpportunity]:
    """Parse a Lever postings payload (JSON string or decoded list)."""
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return []
    else:
        payload = raw
    if not isinstance(payload, list):
        return []

    employer = company.replace("-", " ").strip()
    out: list[ScrapedOpportunity] = []
    for posting in payload:
        if not isinstance(posting, dict):
            continue
        title = _clean(posting.get("text"))
        source_url = _clean(posting.get("hostedUrl") or posting.get("applyUrl"))
        application_url = _clean(posting.get("applyUrl")) or None
        if not title or not source_url.startswith("http"):
            continue
        categories = posting.get("categories")
        categories = categories if isinstance(categories, dict) else {}
        commitment = _clean(categories.get("commitment"))
        if not _commitment_is_internship(commitment):
            continue
        out.append(
            ScrapedOpportunity(
                employer=employer,
                role_title=title,
                source_url=source_url,
                application_url=application_url,
                location=_clean(categories.get("location")),
                ats_type="lever",
                programme_type=classify_programme(title),
                source=f"lever:{company}",
            )
        )
    return out


def fetch_company(company: str) -> list[ScrapedOpportunity]:
    """Fetch one company's Lever board and keep only early-careers roles."""
    payload = http_get_json(ENDPOINT.format(company=company))
    rows = [row for row in parse_postings(payload, company) if is_early_careers(row.role_title)]
    for row in rows:
        row.source = f"lever:{company}"
    return rows

"""Headless multi-source opportunity collectors for ARGUS.

Pure HTTP (httpx) + BeautifulSoup — no browser required. Sources:

- ``greenhouse``  Greenhouse job-board embed API
- ``lever``       Lever postings API
- ``aggregators`` Bright Network / RateMyPlacement / WikiJob listing pages
- ``watchlist``   curated firm -> ATS-slug mapping (env-overridable)

Entry point: :func:`collect_all`.
"""
from __future__ import annotations

from app.scouting.sources.base import collect_all, collect_all_report, dedupe, is_early_careers, normalize_url

__all__ = ["collect_all", "collect_all_report", "dedupe", "is_early_careers", "normalize_url"]

"""Expansion guard for the no-login source supply (Greenhouse + Lever).

Covers the 2026-09-18 expansion of app/scouting/sources/:

- build_sources() returns one source object per configured slug + aggregators.
- every configured slug is well-formed (lowercase, no spaces, no URL).
- no duplicate slugs anywhere (ORGS / COMPANIES / watchlist merge).
- the source list is strictly larger than the pre-expansion baseline while
  retaining every previously-enabled source (regression guard).
- UNVERIFIED candidate lists are NOT enabled by default.
- a dead/unknown slug fails harmlessly (per-source isolation in collect_all).

Offline only: no network calls.
"""
from __future__ import annotations

import re

from app.scouting.sources import greenhouse, lever, watchlist
from app.scouting.sources.base import build_sources, collect_all

# --- Pre-expansion baseline (hard regression guard) ---------------------------
# greenhouse.ORGS @ 2026-08-23 probe + lever.COMPANIES + 3 aggregators.
GH_BEFORE: tuple[str, ...] = (
    "point72",
    "jumptrading",
    "akunacapital",
    "optiver",
    "imc",
    "flowtraders",
    "janestreet",
    "liontree",
    "williamblair",
    "mavensecuritiesholdingltd",
    "exoduspoint",
    "schonfeld",
    "marshallwace",
)
LEVER_BEFORE: tuple[str, ...] = ("squarepoint",)
AGGREGATOR_NAMES: tuple[str, ...] = ("brightnetwork", "ratemyplacement", "wikijob")
BEFORE_SOURCE_COUNT = len(GH_BEFORE) + len(LEVER_BEFORE) + len(AGGREGATOR_NAMES)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


def _all_configured_slugs() -> list[str]:
    slugs: list[str] = list(greenhouse.ORGS) + list(lever.COMPANIES)
    for spec in watchlist.load_watchlist({}).values():
        slugs += list(spec.get("greenhouse") or [])
        slugs += list(spec.get("lever") or [])
    return slugs


class TestBuildSourcesShape:
    def test_returns_expected_source_objects(self):
        sources = build_sources({})
        names = [name for name, _ in sources]
        # one entry per Greenhouse org
        for org in greenhouse.ORGS:
            assert f"greenhouse:{org}" in names, org
        # one entry per Lever company
        for company in lever.COMPANIES:
            assert f"lever:{company}" in names, company
        # aggregators present, in declared order at the tail
        assert names[-3:] == list(AGGREGATOR_NAMES)
        # every fetch callable is callable
        for name, fetch in sources:
            assert callable(fetch), name

    def test_greenhouse_and_lever_sources_sorted_and_prefixed(self):
        sources = build_sources({})
        names = [name for name, _ in sources]
        gh_names = [n for n in names if n.startswith("greenhouse:")]
        lever_names = [n for n in names if n.startswith("lever:")]
        assert gh_names == sorted(gh_names)
        assert lever_names == sorted(lever_names)


class TestSlugHygiene:
    def test_slugs_well_formed(self):
        for slug in _all_configured_slugs():
            assert slug, "empty slug configured"
            assert " " not in slug, slug
            assert "\t" not in slug and "\n" not in slug, slug
            assert "http" not in slug and "/" not in slug, slug
            assert slug == slug.lower(), slug
            assert SLUG_RE.match(slug), slug

    def test_no_duplicate_slugs(self):
        assert len(greenhouse.ORGS) == len(set(greenhouse.ORGS))
        assert len(lever.COMPANIES) == len(set(lever.COMPANIES))
        configured = _all_configured_slugs()
        # watchlist intentionally mirrors ORGS/COMPANIES; the *merged* source
        # set (what build_sources emits) must still be duplicate-free.
        names = [name for name, _ in build_sources({})]
        assert len(names) == len(set(names))
        # within each board family, the merged slug set has no dupes
        gh_merged = [n for n in names if n.startswith("greenhouse:")]
        lever_merged = [n for n in names if n.startswith("lever:")]
        assert len(gh_merged) == len(set(gh_merged))
        assert len(lever_merged) == len(set(lever_merged))

    def test_unverified_lists_not_enabled_by_default(self):
        names = {name for name, _ in build_sources({})}
        for slug in getattr(greenhouse, "UNVERIFIED_ORGS", ()):
            assert f"greenhouse:{slug}" not in names, slug
        for slug in getattr(lever, "UNVERIFIED_COMPANIES", ()):
            assert f"lever:{slug}" not in names, slug
        # unverified slugs are still well-formed (so promotion is trivial)
        for slug in list(getattr(greenhouse, "UNVERIFIED_ORGS", ())):
            assert SLUG_RE.match(slug), slug
        for slug in list(getattr(lever, "UNVERIFIED_COMPANIES", ())):
            assert SLUG_RE.match(slug), slug
        # unverified must not overlap the enabled lists
        assert not (set(getattr(greenhouse, "UNVERIFIED_ORGS", ())) & set(greenhouse.ORGS))
        assert not (set(getattr(lever, "UNVERIFIED_COMPANIES", ())) & set(lever.COMPANIES))


class TestExpansionRegression:
    def test_retains_every_previously_enabled_source(self):
        for org in GH_BEFORE:
            assert org in greenhouse.ORGS, f"dropped Greenhouse org: {org}"
        for company in LEVER_BEFORE:
            assert company in lever.COMPANIES, f"dropped Lever company: {company}"
        names = {name for name, _ in build_sources({})}
        for org in GH_BEFORE:
            assert f"greenhouse:{org}" in names, org
        for company in LEVER_BEFORE:
            assert f"lever:{company}" in names, company
        for agg in AGGREGATOR_NAMES:
            assert agg in names, agg

    def test_source_list_strictly_larger_than_before(self):
        sources = build_sources({})
        assert len(sources) > BEFORE_SOURCE_COUNT, (
            f"expected > {BEFORE_SOURCE_COUNT} sources, got {len(sources)}"
        )
        assert len(greenhouse.ORGS) > len(GH_BEFORE)
        assert len(lever.COMPANIES) > len(LEVER_BEFORE)


class TestDeadSlugFailsHarmlessly:
    def test_collect_all_skips_failing_source(self):
        def dead():
            raise RuntimeError("404 Client Error: Not Found")

        merged = collect_all(sources=[("greenhouse:nosuchorg", dead)])
        assert merged == []

    def test_fetch_org_propagates_so_collect_all_can_isolate(self, monkeypatch):
        def boom(_url):
            raise RuntimeError("404")

        monkeypatch.setattr(greenhouse, "http_get_json", boom)
        try:
            greenhouse.fetch_org("nosuchorg")
        except RuntimeError:
            pass
        else:  # pragma: no cover - fetch must raise so collect_all can skip
            raise AssertionError("fetch_org should raise on transport error")
        merged = collect_all(
            sources=[("greenhouse:nosuchorg", lambda: greenhouse.fetch_org("nosuchorg"))]
        )
        assert merged == []

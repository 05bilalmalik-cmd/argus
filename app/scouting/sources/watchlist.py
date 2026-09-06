"""Watchlist: firm -> ATS slug mapping for direct board queries.

LIVE-VERIFIED 2026-08-23 against boards-api.greenhouse.io and
api.lever.co. Slugs marked DEAD returned 404 at probe time and were removed;
firms with no public board (own ATS portals) are listed with empty slugs so
the aggregator sources still cover them via job-search pages.
Extendable at runtime: set ARGUS_WATCHLIST_JSON to a file path whose JSON has
the same shape; entries merge over these defaults.
"""
from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

WATCHLIST: dict[str, dict[str, list[str]]] = {
    # --- Prop trading / market making (live Greenhouse boards) ---
    "Point72": {"greenhouse": ["point72"], "lever": []},
    "Jump Trading": {"greenhouse": ["jumptrading"], "lever": []},
    "Akuna Capital": {"greenhouse": ["akunacapital"], "lever": []},
    "Optiver": {"greenhouse": ["optiver"], "lever": []},          # board live, 0 jobs at probe
    "IMC Trading": {"greenhouse": ["imc"], "lever": []},
    "Flow Traders": {"greenhouse": ["flowtraders"], "lever": []},
    "Jane Street": {"greenhouse": ["janestreet"], "lever": []},
    "Marshall Wace": {"greenhouse": ["marshallwace"], "lever": []},  # board live, 0 jobs at probe
    # --- Hedge funds / multi-strategy (live boards) ---
    "ExodusPoint Capital": {"greenhouse": ["exoduspoint"], "lever": []},
    "Schonfeld Strategic Advisors": {"greenhouse": ["schonfeld"], "lever": []},
    "Maven Securities": {"greenhouse": ["mavensecuritiesholdingltd"], "lever": []},
    # --- Advisory / banking (live boards) ---
    "LionTree": {"greenhouse": ["liontree"], "lever": []},
    "William Blair": {"greenhouse": ["williamblair"], "lever": []},
    # --- Firms with NO public Greenhouse/Lever board (verified 404 / own ATS) ---
    # Citadel(+Securities): own Workday portal. HRT: own portal. DRW: own portal.
    # Two Sigma, SIG, Five Rings, Belvedere, Millennium, Jefferies, Houlihan
    # Lokey, Blackstone, Carlyle, KKR, Apollo, Ares, Balyasny, Squarepoint,
    # Rothschild: all 404 on both APIs 2026-08-23 — covered via Trackr +
    # aggregators instead of guessed slugs.
    "G-Research": {"greenhouse": [], "lever": []},   # own careers portal
}


def load_watchlist(settings: object | None = None) -> dict[str, dict[str, list[str]]]:
    """Merge the default watchlist with an optional JSON override file.

    The override path is read from ``ARGUS_WATCHLIST_JSON`` in the settings
    mapping (or os.environ as fallback). File entries are merged per firm:
    its greenhouse/lever lists extend the defaults.
    """
    merged: dict[str, dict[str, list[str]]] = {
        firm: {"greenhouse": list(spec.get("greenhouse") or []),
               "lever": list(spec.get("lever") or [])}
        for firm, spec in WATCHLIST.items()
    }
    path = ""
    if isinstance(settings, dict):
        path = str(settings.get("ARGUS_WATCHLIST_JSON", "") or "")
    if not path:
        path = os.environ.get("ARGUS_WATCHLIST_JSON", "")
    if not path:
        return merged
    try:
        with open(path, encoding="utf-8") as fh:
            extra = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("watchlist override %s unreadable: %s", path, exc)
        return merged
    if not isinstance(extra, dict):
        return merged
    for firm, spec in extra.items():
        if not isinstance(spec, dict):
            continue
        entry = merged.setdefault(firm, {"greenhouse": [], "lever": []})
        entry["greenhouse"] += [str(s) for s in spec.get("greenhouse") or []]
        entry["lever"] += [str(s) for s in spec.get("lever") or []]
    return merged

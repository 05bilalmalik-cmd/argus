"""Tests for division inference and its wiring into ScoutService.ingest."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.db import Database
from app.models import Opportunity
from app.scouting.divisions import DEFAULT_DIVISION, DIVISION_PATTERNS, infer_division
from app.scouting.service import ScoutService
from app.scouting.trackr import ScrapedOpportunity


def setup(tmp_path: Path):
    from app.security.crypto import CryptoBox

    settings = Settings.load({"ARGUS_DATA_DIR": str(tmp_path)})
    settings.ensure_directories()
    db = Database(settings)
    db.create_schema()
    return settings, db, CryptoBox.from_path(settings.secret_key_path)


# ------------------------------------------------------------- infer_division


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        # every canonical slug gets at least one positive
        ("Summer Analyst - Investment Banking Division", "investment-banking"),
        ("IBD Off-cycle Internship", "investment-banking"),
        ("M&A Summer Analyst", "investment-banking"),
        ("Capital Markets Analyst Programme", "investment-banking"),
        ("Global Markets Summer Internship", "global-markets"),
        ("Sales and Trading Year in Industry", "sales-and-trading"),
        ("S&T Spring Week", "sales-and-trading"),
        ("Asset Management Placement", "asset-management"),
        ("Private Equity Internship 2027", "private-equity"),
        ("Venture Capital Analyst", "venture-capital"),
        ("Hedge Fund Summer Internship", "hedge-fund"),
        ("Quantitative Research Spring Insight", "quant-research"),
        ("Quant Research Internship", "quant-research"),
        ("Quantitative Trading Summer Analyst", "quant-trading"),
        ("Algorithmic Trading Placement", "quant-trading"),
        ("Risk Management Year in Industry", "risk-management"),
        ("Audit & Advisory Summer Programme", "audit-and-advisory"),
        ("Assurance Placement", "audit-and-advisory"),
        ("Consulting Spring Week", "consulting"),
        ("Technology Year in Industry - Software Engineering", "technology"),
        ("Cyber Security Placement", "technology"),
        ("Operations Summer Internship", "operations"),
        ("Actuarial Placement Year", "actuarial"),
        ("Corporate Finance Internship", "corporate-finance"),
        ("Private Wealth Management Spring Week", "private-wealth-management"),
        ("Equity Research Summer Analyst", "research"),
        ("Fintech Placement", "fintech"),
        ("Financial Services Spring Insight", "financial-services"),
    ],
)
def test_infer_division_positive(title: str, expected: str) -> None:
    assert infer_division(title) == expected


def test_financial_services_slug_is_reachable() -> None:
    assert any(slug == "financial-services" for slug, _ in DIVISION_PATTERNS)


def test_specificity_ordering() -> None:
    # "quantitative research" must beat the bare "research" pattern...
    assert infer_division("Quantitative Research Internship") == "quant-research"
    # ...and quantitative trading must beat generic trading/markets wording.
    assert (
        infer_division("Quantitative Trading - Global Markets Desk")
        == "quant-trading"
    )
    # investment banking beats a bare "markets"/"advisory" mention.
    assert (
        infer_division("Investment Banking Advisory Summer Analyst")
        == "investment-banking"
    )


def test_unknown_falls_back_to_general_finance() -> None:
    assert infer_division("Spring Insight Programme") == DEFAULT_DIVISION
    assert infer_division("Spring Insight Programme") == "general-finance"
    assert infer_division("", "") == "general-finance"


def test_employer_hint_only_used_when_title_silent() -> None:
    assert infer_division("Summer Internship", "Citadel") == "hedge-fund"
    assert infer_division("Summer Internship", "Blackstone") == "private-equity"
    # an explicit title keyword always wins over the employer hint
    assert (
        infer_division("Technology Summer Internship", "Citadel") == "technology"
    )


# ------------------------------------------------------- ingest wiring


def test_ingest_populates_division(tmp_path: Path) -> None:
    """ScoutService.ingest must store an inferred division, not ''."""
    settings, db, crypto = setup(tmp_path)
    scraped = [
        ScrapedOpportunity(
            employer="Barclays",
            role_title="Investment Banking Summer Analyst 2027",
            url="https://x.example.com/ib",
            source="test",
        ),
        ScrapedOpportunity(
            employer="Goldman Sachs",
            role_title="Year in Industry Placement - Global Markets",
            url="https://x.example.com/gm",
            source="test",
        ),
        ScrapedOpportunity(
            employer="Unknown Co",
            role_title="Spring Insight Programme",
            url="https://x.example.com/spring",
            source="test",
        ),
    ]
    with db.session_scope() as session:
        scout = ScoutService(session, settings, crypto)
        stats = scout.ingest(scraped)
        assert stats["imported"] == 3
        divisions = {
            opp.role_title: opp.division
            for opp in session.query(Opportunity).all()
        }
        assert divisions["Investment Banking Summer Analyst 2027"] == (
            "investment-banking"
        )
        assert (
            divisions["Year in Industry Placement - Global Markets"]
            == "global-markets"
        )
        # unknown title falls back to general-finance, never ''
        assert divisions["Spring Insight Programme"] == "general-finance"


def test_ingest_respects_explicit_scraped_division(tmp_path: Path) -> None:
    settings, db, crypto = setup(tmp_path)
    scraped = [
        ScrapedOpportunity(
            employer="HSBC",
            role_title="Summer Internship",
            url="https://x.example.com/hsbc",
            source="test",
            division="global-banking-and-markets",
        ),
    ]
    with db.session_scope() as session:
        ScoutService(session, settings, crypto).ingest(scraped)
        opp = session.query(Opportunity).one()
        assert opp.division == "global-banking-and-markets"


def test_every_pattern_slug_is_canonical() -> None:
    canonical = {
        "investment-banking", "global-markets", "sales-and-trading",
        "asset-management", "private-equity", "venture-capital", "hedge-fund",
        "quant-research", "quant-trading", "risk-management",
        "audit-and-advisory", "consulting", "technology", "operations",
        "actuarial", "corporate-finance", "financial-services",
        "private-wealth-management", "research", "fintech", "general-finance",
    }
    slugs = {slug for slug, _ in DIVISION_PATTERNS} | {"general-finance"}
    assert slugs <= canonical
    # every canonical slug must be reachable from DIVISION_PATTERNS or default
    assert canonical - slugs == set()

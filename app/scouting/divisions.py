"""Division inference for scraped opportunities.

Trackr listings rarely carry an explicit division field, yet CV tag matching
(DocumentService.select_approved via ApplicationService.prepare) builds desired
tags from ``employer_slug + division + variant_tag``.  Without a division slug,
firm-division CV tags such as ``["barclays", "investment-banking", "yii-cv"]``
can never win the overlap score and every application falls back to the
variant-only CV.

``infer_division`` maps a raw role title (plus optional employer hint) to one of
the canonical division slugs used by the CV library.  Patterns are evaluated in
order, most specific first: "quantitative research" is tested before the bare
"research" pattern, "quantitative trading" before generic "trading", and the
investment-banking cluster ("investment banking division", "IBD", "M&A",
"capital markets") before anything that merely mentions banking.  Anything that
matches nothing lands on ``general-finance`` - deliberately conservative, since
a wrong-but-specific slug would silently pick the wrong firm-division CV.
"""
from __future__ import annotations

import re

# Ordered, most specific first.  First match wins.
DIVISION_PATTERNS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "quant-trading",
        (
            re.compile(r"quant(?:itative|um)?\s+(?:\w+\s+)??trad(?:ing|er)", re.I),
            re.compile(r"algorithmic\s+trad(?:ing|er)", re.I),
            re.compile(r"systematic\s+trad(?:ing|er)", re.I),
        ),
    ),
    (
        "quant-research",
        (
            re.compile(r"quant(?:itative)?\s+(?:\w+\s+)?research", re.I),
            re.compile(r"quant(?:itative)?\s+analy(?:st|tics)", re.I),
            re.compile(r"quantitative\s+developer", re.I),
            re.compile(r"\bquant\s+(?:dev|strat)\b", re.I),
        ),
    ),
    ("hedge-fund", (re.compile(r"hedge\s+funds?", re.I),)),
    ("private-equity", (re.compile(r"private\s+equity", re.I),)),
    ("venture-capital", (re.compile(r"venture\s+cap(?:ital|itals?)", re.I),)),
    (
        "sales-and-trading",
        (
            re.compile(r"sales\s*(?:&|and)\s*trading", re.I),
            re.compile(r"\bs\s*/?\s*&\s*/?\s*t\b", re.I),
            re.compile(r"\btrading\s+(?:desk|division|floor)\b", re.I),
            re.compile(r"\bflow\s+trad(?:ing|er)\b", re.I),
        ),
    ),
    (
        "global-markets",
        (
            re.compile(r"global\s+(?:banking\s+&?\s*)?markets", re.I),
            re.compile(r"markets\s+division", re.I),
            re.compile(r"\bFICC\b", re.I),
            re.compile(r"equities?\s+division", re.I),
        ),
    ),
    (
        "investment-banking",
        (
            re.compile(r"investment\s+banks?\s+divisions?", re.I),
            re.compile(r"investment\s+banking", re.I),
            re.compile(r"\bIBD\b"),
            re.compile(r"\bM\s*&\s*A\b"),
            re.compile(r"mergers?(?:\s*&|\s+and)\s*acquisitions?", re.I),
            # bare "Mergers"/"Acquisitions" - tolerates employer typos
            re.compile(r"\bmergers?\b|\bacquisitions?\b", re.I),
            re.compile(r"capital\s+markets", re.I),
            re.compile(r"leveraged\s+finance", re.I),
            re.compile(r"debt\s+advisory", re.I),
        ),
    ),
    ("asset-management", (re.compile(r"asset\s+managements?", re.I),)),
    (
        "private-wealth-management",
        (
            re.compile(r"private\s+wealths?\s+managements?", re.I),
            re.compile(r"private\s+banking", re.I),
            re.compile(r"wealth\s+managements?", re.I),
        ),
    ),
    ("actuarial", (re.compile(r"actuar(?:ial|y)", re.I),)),
    ("risk-management", (re.compile(r"\brisk\b", re.I),)),
    (
        "audit-and-advisory",
        (
            re.compile(r"audit\s*(?:&|and)\s*advisory", re.I),
            re.compile(r"\baudit(?:ing)?\b", re.I),
            re.compile(r"\bassurance\b", re.I),
        ),
    ),
    ("corporate-finance", (re.compile(r"corporates?\s+finances?", re.I),)),
    (
        "financial-services",
        (re.compile(r"financials?\s+services?", re.I),),
    ),
    (
        "consulting",
        (
            re.compile(r"consultanc(?:y|ies)", re.I),
            re.compile(r"consultings?", re.I),
            re.compile(r"strategy\s+and\s+consulting", re.I),
        ),
    ),
    (
        "fintech",
        (
            re.compile(r"fintechs?", re.I),
            re.compile(r"financials?\s+technolog(?:y|ies)", re.I),
        ),
    ),
    (
        "technology",
        (
            re.compile(r"technolog(?:y|ies)", re.I),
            re.compile(r"\btech\s+(?:division|department|team)\b", re.I),
            re.compile(r"software\s+(?:engineer|development|developer)", re.I),
            re.compile(r"data\s+(?:engineer|science|analytics)", re.I),
            re.compile(r"cyber\s*-?\s*security", re.I),
            re.compile(r"machine\s+learning\s+engineer", re.I),
        ),
    ),
    (
        "operations",
        (
            re.compile(r"\boperations?\b", re.I),
            re.compile(r"\bops\s+(?:division|team|intern)\b", re.I),
        ),
    ),
    # Bare "research" LAST so "quantitative research" (and any other more
    # specific compound) always wins.
    ("research", (re.compile(r"\bresearch\b", re.I),)),
)

DEFAULT_DIVISION = "general-finance"

# Employer-only hints: consulted ONLY when the title matches nothing, because
# brand names imply the firm's core business, not necessarily this role's desk.
_EMPLOYER_HINTS: tuple[tuple[str, tuple[re.Pattern[str], ...]], ...] = (
    (
        "hedge-fund",
        (
            re.compile(r"citadel|millennium|two sigma|brevan howard|marshall wace", re.I),
            re.compile(r"bluecrest|winton|man group|marshall\s+wace|capula", re.I),
        ),
    ),
    (
        "private-equity",
        (
            re.compile(r"blackstone|\bkkr\b|apollo|carlyle|\bcvc\b|permira", re.I),
            re.compile(r"bridgepoint|cinven|\bcinven\b|eqt\b|tpg\b|advent", re.I),
        ),
    ),
    (
        "venture-capital",
        (re.compile(r"balderton|index ventures|accel|sequoia|localglobe", re.I),),
    ),
)


def infer_division(role_title: str, employer: str = "") -> str:
    """Map a role title (with optional employer hint) to a canonical slug."""
    blob = f"{role_title or ''} {employer or ''}"
    for _slug, patterns in DIVISION_PATTERNS:
        if any(p.search(blob) for p in patterns):
            return _slug
    # Title gave nothing: fall back to what the *firm itself* is known for.
    for slug, patterns in _EMPLOYER_HINTS:
        if any(p.search(employer or "") for p in patterns):
            return slug
    return DEFAULT_DIVISION

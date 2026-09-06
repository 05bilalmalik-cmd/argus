"""Provider-independent, fail-closed browser read-back comparisons.

The browser can represent one approved answer in more than one truthful way:
a native select has a machine value and a rendered option label, while a
provider may reformat a date or phone number on blur.  These helpers compare
those representations without accepting an unrelated value.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Iterable


_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y-%m",
    "%Y/%m",
    "%m/%Y",
    "%B %Y",
    "%b %Y",
)


def _text(value: object) -> str:
    return " ".join(str(value or "").split()).casefold()


def _date_precision(value: object) -> tuple[str, int, int | None, int | None] | None:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{4}", text):
        year = int(text)
        return ("year", year, None, None) if 1900 <= year <= 2100 else None
    for date_format in _DATE_FORMATS:
        try:
            parsed = datetime.strptime(text, date_format)
        except ValueError:
            continue
        if not 1900 <= parsed.year <= 2100:
            return None
        precision = "day" if "%d" in date_format else "month"
        return (precision, parsed.year, parsed.month, parsed.day if precision == "day" else None)
    return None


def _date_values_match(expected: object, actual: object) -> bool:
    expected_date = _date_precision(expected)
    actual_date = _date_precision(actual)
    if expected_date is None or actual_date is None:
        return False
    expected_precision, expected_year, expected_month, expected_day = expected_date
    actual_precision, actual_year, actual_month, actual_day = actual_date
    if expected_year != actual_year:
        return False
    if expected_precision == "year":
        return True
    if expected_month != actual_month:
        return False
    if expected_precision == "month":
        return True
    return actual_precision == "day" and expected_day == actual_day


def _is_date_field(label: str, control_type: str) -> bool:
    text = _text(label)
    if "year of study" in text or "study year" in text or "academic year" in text:
        return False
    return control_type.casefold() in {"date", "datetime", "datetime-local", "month"} or bool(
        re.search(r"\b(?:date|month|graduat\w*|complet\w*|finish\w*)\b", text)
    )


def _is_phone_field(label: str, control_type: str) -> bool:
    return control_type.casefold() == "tel" or bool(
        re.search(r"\b(?:phone|telephone|mobile)\b", _text(label))
    )


def _phone_digits(value: object) -> str:
    return "".join(character for character in str(value or "") if character.isdigit())


def semantic_value_matches(
    expected: object,
    actual: object,
    *,
    label: str = "",
    control_type: str = "text",
    alternatives: Iterable[object] = (),
) -> bool:
    """Return whether a browser read-back is the same approved value.

    ``alternatives`` is used for native controls whose submitted value and
    displayed label are both valid representations of the selected option.
    The comparison remains exact after normalization: date/phone handling is
    limited to the field's independently observed semantic kind.
    """

    candidates = (actual, *tuple(alternatives))
    expected_text = _text(expected)
    for candidate in candidates:
        if _text(candidate) == expected_text:
            return True

    if _is_date_field(label, control_type):
        return any(_date_values_match(expected, candidate) for candidate in candidates)

    if _is_phone_field(label, control_type):
        expected_digits = _phone_digits(expected)
        if expected_digits:
            return any(_phone_digits(candidate) == expected_digits for candidate in candidates)

    return False


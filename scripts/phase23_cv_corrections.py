from __future__ import annotations

import argparse
import io
import hashlib
import json
import os
import re
import sqlite3
import struct
import time
import unicodedata
import zipfile
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
W = f"{{{W_NS}}}"
XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"

QUANT_TAGS = frozenset(
    {
        "quant",
        "quant-research",
        "quant-trading",
        "quantitative-research",
        "strats",
        "trading-intern",
        "global-trading",
        "hedge-fund",
        "financial-data-analytics",
    }
)

NON_QUANT_MONOLITH_LINES = (
    "Founded Monolith Quant Research, designing and testing systematic trading "
    "strategies across equities, rates, FX, commodities and crypto in Python, using "
    "multi-decade market data.",
    "Built the firm's validation layer — walk-forward testing, sealed out-of-sample "
    "holdouts and pre-registered hypotheses — holding every strategy to an "
    "institutional evidence standard before approval.",
)

QUANT_MONOLITH_LINES = (
    "Founded Monolith Quant Research: end-to-end research infrastructure for "
    "systematic strategies across equities, rates, FX, commodities and crypto, built "
    "in Python on 17 years of tick-level FX data with explicit execution-cost and "
    "slippage modelling.",
    "Built the validation layer — deflated and probabilistic Sharpe ratios, probability "
    "of backtest overfitting (PBO), walk-forward analysis and pre-registered sealed "
    "holdouts — so a strategy is approved only on out-of-sample evidence, never "
    "in-sample performance.",
    "Maintains a documented kill record: every candidate strategy is pre-registered "
    "before testing and retired on its sealed holdout result, the discipline that "
    "separates genuine edge from overfitting.",
)

OLD_MONOLITH_PREFIX = (
    "Founded Monolith Quant Research: backtesting and risk-managing systematic "
    "strategies across equities, rates, FX, commodities and crypto — "
)

_PWC_OUTPUT_PREFIX = (
    "Shadowed analysts in PwC’s Risk Analytics division, observing "
)
_PWC_SUPPORTED_PREFIXES = tuple(
    prefix
    for apostrophe in ("’", "'")
    for prefix in (
        f"Embedded within PwC{apostrophe}s Risk Analytics division, gaining hands-on exposure to ",
        f"Embedded within PwC{apostrophe}s Risk Analytics division, gaining exposure to ",
        f"Embedded within PwC{apostrophe}s Risk Analytics division, developing fluency in ",
        f"Completed PwC{apostrophe}s Risk Analytics division work-experience placement, gaining exposure to ",
    )
)
_PWC_CANDIDATE_PREFIXES = (
    "Embedded within PwC",
    "Completed PwC",
)
_FAILURE_LANGUAGE = ("kill record", "retired", "overfitting")
_DEMONSTRATING_SUFFIX = re.compile(
    r",\s*demonstrating the initiative and analytical precision valued in "
    r"fast-paced financial environments\.?$",
    re.IGNORECASE,
)
_SELF_JUSTIFYING_MARKERS = (
    "aligned with",
    "applied across",
    "applied in",
    "building the",
    "combining ",
    "core competencies",
    "core to",
    "demonstrating ",
    "developing ",
    "directly aligned",
    "directly analogous",
    "directly applicable",
    "directly mirroring",
    "directly relevant",
    "directly transferable",
    "evidence of",
    "exactly the",
    "honing",
    "mirroring",
    "one of the strongest",
    "precisely the",
    "reinforcing",
    "sharpening ",
    "skills directly",
    "skills that directly",
    "skills that translate directly",
    "strong grounding",
    "the core of",
    "the same mindset",
)
_NAMED_FACT_MARKERS = (
    "arbitrage",
    "bloomberg terminal",
    "deflated sharpe",
    "market-making",
    "monte carlo",
    "options trading",
    "probabilistic sharpe",
    "probability of backtest overfitting",
    "psr",
    "dsr",
    "pbo",
    "python",
    "sealed holdout",
    "walk-forward",
)
_NON_DISTINCTIVE_EMPLOYER_FIRST_WORDS = frozenset(
    {
        "asset",
        "bank",
        "capital",
        "financial",
        "global",
        "group",
        "management",
        "royal",
        "standard",
        "the",
    }
)


class UnsupportedSource(ValueError):
    """A source cannot be transformed without exceeding the approved copy."""


@dataclass(frozen=True, slots=True)
class ExtractedDocx:
    paragraphs: tuple[str, ...]
    paragraph_count: int
    document_xml: bytes

    @property
    def text(self) -> str:
        return "\n".join(self.paragraphs)


@dataclass(frozen=True, slots=True)
class TransformResult:
    content: bytes
    text: str
    pwc_paragraph: str
    monolith_lines: tuple[str, ...]
    source_paragraph_count: int
    output_paragraph_count: int
    fat_clauses_removed: int
    employer_flattery_clauses: int
    graduation_tag: str


@dataclass(frozen=True, slots=True)
class PlannedDocument:
    source_id: str
    source_name: str
    source_kind: str
    source_path: str
    source_sha256: str
    source_approved: bool
    source_tags_json: str
    source_created_at: str
    is_quant: bool
    output_name: str
    output_sha256: str
    derived_content: bytes
    source_paragraph_count: int
    output_paragraph_count: int
    fat_clauses_removed: int
    employer_flattery_clauses: int
    staged_path: str | None = None


@dataclass(frozen=True, slots=True)
class SkippedSource:
    source_id: str
    source_name: str
    reason: str


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    db_path: Path
    documents_dir: Path
    employer_names: tuple[str, ...]
    source_document_count: int
    eligible: tuple[PlannedDocument, ...]
    skipped: tuple[SkippedSource, ...]


@dataclass(frozen=True, slots=True)
class ApplyReport:
    applied: int
    already_applied: int


@dataclass(slots=True)
class _Package:
    content: bytes
    root: ElementTree.Element
    document_xml: bytes
    namespaces: tuple[tuple[str, str], ...]


def _local_name(element: ElementTree.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _paragraph_text(paragraph: ElementTree.Element) -> str:
    parts: list[str] = []
    for node in paragraph.iter():
        local = _local_name(node)
        if local == "t":
            parts.append(node.text or "")
        elif local == "tab":
            parts.append("\t")
        elif local in {"br", "cr"}:
            parts.append("\n")
    return "".join(parts)


def _read_package(content: bytes) -> _Package:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            if archive.testzip() is not None:
                raise UnsupportedSource("invalid_docx")
            document_xml = archive.read("word/document.xml")
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise UnsupportedSource("invalid_docx") from exc
    upper = document_xml.upper()
    if b"<!DOCTYPE" in upper or b"<!ENTITY" in upper:
        raise UnsupportedSource("invalid_docx")
    try:
        namespaces = tuple(
            dict(
                ElementTree.iterparse(
                    io.BytesIO(document_xml),
                    events=("start-ns",),
                )
            ).items()
        )
        for prefix, uri in namespaces:
            if prefix != "xml" and not re.fullmatch(r"ns\d+", prefix or ""):
                ElementTree.register_namespace(prefix, uri)
        root = ElementTree.fromstring(document_xml)
    except (ElementTree.ParseError, ValueError) as exc:
        raise UnsupportedSource("invalid_docx") from exc
    return _Package(content, root, document_xml, namespaces)


def extract_docx(content: bytes) -> ExtractedDocx:
    package = _read_package(content)
    paragraphs = tuple(
        _paragraph_text(paragraph) for paragraph in package.root.iter(W + "p")
    )
    return ExtractedDocx(paragraphs, len(paragraphs), package.document_xml)


def _set_space_preserve(node: ElementTree.Element) -> None:
    value = node.text or ""
    if value[:1].isspace() or value[-1:].isspace():
        node.set(XML_SPACE, "preserve")
    else:
        node.attrib.pop(XML_SPACE, None)


def _replace_span(
    paragraph: ElementTree.Element,
    start: int,
    end: int,
    replacement: str,
) -> None:
    nodes = [node for node in paragraph.iter(W + "t")]
    visible = "".join(node.text or "" for node in nodes)
    if start < 0 or end < start or end > len(visible):
        raise UnsupportedSource("run_span_out_of_bounds")
    offset = 0
    replaced = False
    for node in nodes:
        original = node.text or ""
        node_start = offset
        node_end = offset + len(original)
        offset = node_end
        overlaps = node_start < end and node_end > start
        insertion_at_empty_boundary = start == end == node_start and not replaced
        if not overlaps and not insertion_at_empty_boundary:
            continue
        local_start = max(start, node_start) - node_start
        local_end = min(end, node_end) - node_start
        if not replaced:
            node.text = original[:local_start] + replacement + original[local_end:]
            replaced = True
        else:
            node.text = original[:local_start] + original[local_end:]
        _set_space_preserve(node)
    if not replaced:
        raise UnsupportedSource("run_span_not_replaced")


def _replace_pwc(root: ElementTree.Element) -> str:
    candidates = [
        paragraph
        for paragraph in root.iter(W + "p")
        if _paragraph_text(paragraph).startswith(_PWC_CANDIDATE_PREFIXES)
    ]
    if len(candidates) != 1:
        raise UnsupportedSource("unsupported_pwc_opening")
    paragraph = candidates[0]
    visible = _paragraph_text(paragraph)
    old_prefix = next(
        (prefix for prefix in _PWC_SUPPORTED_PREFIXES if visible.startswith(prefix)),
        None,
    )
    if old_prefix is None:
        raise UnsupportedSource("unsupported_pwc_opening")
    _replace_span(paragraph, 0, len(old_prefix), _PWC_OUTPUT_PREFIX)
    result = _paragraph_text(paragraph)
    if "hands-on" in result.casefold():
        raise UnsupportedSource("unsupported_pwc_hands_on")
    return result


def _replace_paragraph_with_lines(
    paragraph: ElementTree.Element,
    lines: tuple[str, ...],
) -> None:
    direct_runs = [child for child in paragraph if child.tag == W + "r"]
    if not direct_runs:
        raise UnsupportedSource("monolith_run_structure")
    first_run = direct_runs[0]
    run_properties = first_run.find(W + "rPr")
    for child in list(paragraph):
        if child.tag != W + "pPr":
            paragraph.remove(child)
    run = ElementTree.SubElement(paragraph, W + "r")
    if run_properties is not None:
        run.append(deepcopy(run_properties))
    for index, line in enumerate(lines):
        if index:
            ElementTree.SubElement(run, W + "br")
        text_node = ElementTree.SubElement(run, W + "t")
        text_node.text = line if index == 0 else f"• {line}"
        _set_space_preserve(text_node)


def _replace_monolith(
    root: ElementTree.Element,
    *,
    is_quant: bool,
) -> tuple[str, ...]:
    paragraphs = [
        paragraph
        for paragraph in root.iter(W + "p")
        if _paragraph_text(paragraph).startswith("Founded Monolith Quant Research")
    ]
    if len(paragraphs) != 1:
        raise UnsupportedSource("unsupported_monolith_line")
    paragraph = paragraphs[0]
    visible = _paragraph_text(paragraph)
    if not visible.startswith(OLD_MONOLITH_PREFIX):
        raise UnsupportedSource("unsupported_monolith_line")
    lines = QUANT_MONOLITH_LINES if is_quant else NON_QUANT_MONOLITH_LINES
    _replace_paragraph_with_lines(paragraph, lines)
    return lines


def _normalised_words(value: str) -> tuple[str, ...]:
    apostrophe_normalised = value.translate(
        str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'"})
    )
    ascii_value = unicodedata.normalize("NFKD", apostrophe_normalised).encode(
        "ascii", "ignore"
    ).decode("ascii")
    return tuple(re.findall(r"[a-z0-9]+", ascii_value.casefold().replace("&", " and ")))


@dataclass(frozen=True, slots=True)
class _EmployerMatcher:
    entries: tuple[tuple[str, tuple[str, ...]], ...]

    @classmethod
    def compile(cls, employer_names: tuple[str, ...]) -> _EmployerMatcher:
        return cls(
            tuple(
                (employer, candidate)
                for employer in employer_names
                if (candidate := _normalised_words(employer))
            )
        )

    @staticmethod
    def _matches(
        words: tuple[str, ...],
        apostrophe_normalised: str,
        candidate: tuple[str, ...],
    ) -> bool:
        size = len(candidate)
        if any(words[index : index + size] == candidate for index in range(len(words) - size + 1)):
            return True
        first = candidate[0]
        if (
            len(candidate) > 1
            and len(first) >= 4
            and first not in _NON_DISTINCTIVE_EMPLOYER_FIRST_WORDS
        ):
            possessive = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(first)}(?:'s|s')(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            if any(match.group(0)[0].isupper() for match in possessive.finditer(apostrophe_normalised)):
                return True
        return False

    @staticmethod
    def _text_evidence(text: str) -> tuple[tuple[str, ...], str]:
        return (
            _normalised_words(text),
            text.translate(
                str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'"})
            ),
        )

    def contains(self, text: str) -> bool:
        words, apostrophe_normalised = self._text_evidence(text)
        if re.search(
            r"(?<![A-Za-z0-9])[A-Z][A-Za-z0-9.&-]{2,}(?:'s|s')"
            r"(?![A-Za-z0-9])",
            apostrophe_normalised,
        ):
            return True
        for match in re.finditer(
            r"\b(?:at|for|with)\s+([A-Z][A-Za-z0-9.&-]{1,})",
            apostrophe_normalised,
        ):
            name = match.group(1)
            if name.casefold() not in {"big", "leading", "the"} and (
                name.isupper() or len(name) >= 4
            ):
                return True
        return any(
            self._matches(words, apostrophe_normalised, candidate)
            for _, candidate in self.entries
        )

    def named(self, text: str) -> frozenset[str]:
        words, apostrophe_normalised = self._text_evidence(text)
        return frozenset(
            employer
            for employer, candidate in self.entries
            if self._matches(words, apostrophe_normalised, candidate)
        )


def _contains_employer_name(text: str, employer_names: tuple[str, ...]) -> bool:
    return _EmployerMatcher.compile(employer_names).contains(text)


def _named_employers(text: str, employer_names: tuple[str, ...]) -> frozenset[str]:
    return _EmployerMatcher.compile(employer_names).named(text)


def _has_named_fact(tail: str) -> bool:
    folded = tail.casefold()
    return bool(re.search(r"\d", tail)) or any(
        marker in folded for marker in _NAMED_FACT_MARKERS
    )


def _is_self_justifying(tail: str) -> bool:
    folded = tail.casefold().strip()
    return any(marker in folded for marker in _SELF_JUSTIFYING_MARKERS)


def _sentence(text: str) -> str:
    stripped = text.rstrip()
    return stripped if stripped.endswith((".", "?", "!")) else stripped + "."


def _trim_fat(
    root: ElementTree.Element,
    employer_matcher: _EmployerMatcher,
) -> tuple[int, int]:
    removed = 0
    employer_flattery = 0
    for paragraph in root.iter(W + "p"):
        visible = _paragraph_text(paragraph)
        if visible.startswith(NON_QUANT_MONOLITH_LINES[0]) or visible.startswith(
            QUANT_MONOLITH_LINES[0]
        ):
            continue
        demonstrating = _DEMONSTRATING_SUFFIX.search(visible)
        if demonstrating is not None:
            replacement = _sentence(visible[: demonstrating.start()])
            _replace_span(paragraph, 0, len(visible), replacement)
            removed += 1
            continue
        delimiter = " — "
        if delimiter not in visible:
            continue
        prefix, tail = visible.rsplit(delimiter, 1)
        if not _is_self_justifying(tail):
            continue
        if employer_matcher.contains(tail):
            employer_flattery += 1
            continue
        if _has_named_fact(tail):
            continue
        _replace_span(paragraph, 0, len(visible), _sentence(prefix))
        removed += 1
    return removed, employer_flattery


def validate_graduation(text: str, tags: frozenset[str]) -> str:
    years = re.findall(r"\b(?:2028|2029)\b", text)
    grad_tags = tags.intersection({"grad-2028", "grad-2029"})
    if len(years) != 1 or len(grad_tags) != 1:
        raise UnsupportedSource("graduation_coupling")
    year = years[0]
    expected = f"grad-{year}"
    if grad_tags != {expected}:
        raise UnsupportedSource("graduation_coupling")
    has_yii = "Year in Industry" in text
    if (year == "2028" and has_yii) or (year == "2029" and not has_yii):
        raise UnsupportedSource("graduation_coupling")
    return expected


def _serialize_package(package: _Package) -> bytes:
    replacement_xml = ElementTree.tostring(
        package.root,
        encoding="utf-8",
        xml_declaration=True,
    )
    output = io.BytesIO()
    try:
        with zipfile.ZipFile(io.BytesIO(package.content)) as source:
            with zipfile.ZipFile(output, "w") as target:
                target.comment = source.comment
                for info in source.infolist():
                    payload = source.read(info.filename)
                    if info.filename == "word/document.xml":
                        payload = replacement_xml
                    target.writestr(info, payload)
    except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
        raise UnsupportedSource("invalid_docx") from exc
    return output.getvalue()


def transform_docx(
    content: bytes,
    *,
    is_quant: bool,
    tags: frozenset[str],
    employer_names: tuple[str, ...],
    _employer_matcher: _EmployerMatcher | None = None,
) -> TransformResult:
    source = extract_docx(content)
    graduation_tag = validate_graduation(source.text, tags)
    employer_matcher = _employer_matcher or _EmployerMatcher.compile(employer_names)
    source_employers = employer_matcher.named(source.text)
    package = _read_package(content)
    pwc_paragraph = _replace_pwc(package.root)
    monolith_lines = _replace_monolith(package.root, is_quant=is_quant)
    fat_removed, employer_flattery = _trim_fat(package.root, employer_matcher)
    derived_content = _serialize_package(package)
    derived = extract_docx(derived_content)

    if derived.paragraph_count != source.paragraph_count:
        raise UnsupportedSource("paragraph_count_changed")
    if "Embedded within" in derived.text:
        raise UnsupportedSource("embedded_within_remaining")
    if "Shadowed analysts in PwC" not in derived.text:
        raise UnsupportedSource("pwc_shadowing_missing")
    if "hands-on" in pwc_paragraph.casefold():
        raise UnsupportedSource("pwc_hands_on_remaining")
    for line in monolith_lines:
        if line not in derived.text:
            raise UnsupportedSource("monolith_copy_missing")
    if not is_quant and any(
        phrase in derived.text.casefold() for phrase in _FAILURE_LANGUAGE
    ):
        raise UnsupportedSource("non_quant_failure_language")
    if validate_graduation(derived.text, tags) != graduation_tag:
        raise UnsupportedSource("graduation_changed")
    if employer_matcher.named(derived.text) != source_employers:
        raise UnsupportedSource("employer_names_changed")
    return TransformResult(
        content=derived_content,
        text=derived.text,
        pwc_paragraph=pwc_paragraph,
        monolith_lines=monolith_lines,
        source_paragraph_count=source.paragraph_count,
        output_paragraph_count=derived.paragraph_count,
        fat_clauses_removed=fat_removed,
        employer_flattery_clauses=employer_flattery,
        graduation_tag=graduation_tag,
    )


def _safe_output_name(source_name: str, digest: str) -> str:
    basename = Path(source_name.replace("\\", "/")).name
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(basename).stem).strip("._")
    return f"{stem or 'document'}_P23_{digest[:8]}.docx"


def _is_relative_to(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _database_employer_names(connection: sqlite3.Connection) -> tuple[str, ...]:
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='opportunities'"
    ).fetchone()
    names = {
        "PwC",
        "IMC Trading",
        "Bloomberg",
        "Goldman Sachs",
        "J.P. Morgan",
        "Lloyds",
        "PIMCO",
    }
    if table is not None:
        names.update(
            str(row[0]).strip()
            for row in connection.execute(
                "SELECT DISTINCT employer FROM opportunities WHERE employer != ''"
            )
            if isinstance(row[0], str) and row[0].strip()
        )
    return tuple(sorted(names, key=str.casefold))


def _decode_tags(tags_json: str) -> tuple[frozenset[str], tuple[str, ...]]:
    try:
        payload = json.loads(tags_json)
    except (TypeError, ValueError) as exc:
        raise UnsupportedSource("invalid_tags") from exc
    if not isinstance(payload, list) or any(
        not isinstance(value, str) or not value.strip() for value in payload
    ):
        raise UnsupportedSource("invalid_tags")
    original = tuple(payload)
    normalised = frozenset(value.strip().casefold() for value in payload)
    if len(normalised) != len(original):
        raise UnsupportedSource("invalid_tags")
    return normalised, original


def build_plan(db_path: Path, documents_dir: Path) -> MigrationPlan:
    resolved_db = Path(db_path).resolve()
    resolved_documents = Path(documents_dir).resolve()
    connection = sqlite3.connect(
        f"file:{resolved_db.as_posix()}?mode=ro",
        uri=True,
        timeout=30,
    )
    connection.row_factory = sqlite3.Row
    try:
        rows = list(
            connection.execute(
                "SELECT id,name,kind,path,sha256,approved,tags_json,created_at "
                "FROM documents WHERE approved=1 ORDER BY created_at,id"
            )
        )
        employer_names = _database_employer_names(connection)
    finally:
        connection.close()

    eligible: list[PlannedDocument] = []
    skipped: list[SkippedSource] = []
    seen_output_hashes: set[str] = set()
    employer_matcher = _EmployerMatcher.compile(employer_names)
    for row in rows:
        source_id = str(row["id"])
        source_name = str(row["name"])
        try:
            if str(row["kind"]).casefold() != "cv":
                raise UnsupportedSource("not_cv")
            source_path = Path(str(row["path"])).resolve()
            if not _is_relative_to(source_path, resolved_documents):
                raise UnsupportedSource("source_path_outside_documents")
            try:
                source_content = source_path.read_bytes()
            except OSError as exc:
                raise UnsupportedSource("source_file_missing") from exc
            source_sha = hashlib.sha256(source_content).hexdigest()
            if source_sha != str(row["sha256"]):
                raise UnsupportedSource("source_file_drift")
            tags, _ = _decode_tags(str(row["tags_json"]))
            is_quant = bool(tags.intersection(QUANT_TAGS))
            transformed = transform_docx(
                source_content,
                is_quant=is_quant,
                tags=tags,
                employer_names=employer_names,
                _employer_matcher=employer_matcher,
            )
            output_sha = hashlib.sha256(transformed.content).hexdigest()
            if output_sha in seen_output_hashes:
                raise UnsupportedSource("duplicate_derived_sha")
            seen_output_hashes.add(output_sha)
            eligible.append(
                PlannedDocument(
                    source_id=source_id,
                    source_name=source_name,
                    source_kind=str(row["kind"]),
                    source_path=str(source_path),
                    source_sha256=source_sha,
                    source_approved=bool(row["approved"]),
                    source_tags_json=str(row["tags_json"]),
                    source_created_at=str(row["created_at"]),
                    is_quant=is_quant,
                    output_name=_safe_output_name(source_name, output_sha),
                    output_sha256=output_sha,
                    derived_content=transformed.content,
                    source_paragraph_count=transformed.source_paragraph_count,
                    output_paragraph_count=transformed.output_paragraph_count,
                    fat_clauses_removed=transformed.fat_clauses_removed,
                    employer_flattery_clauses=transformed.employer_flattery_clauses,
                )
            )
        except UnsupportedSource as exc:
            skipped.append(SkippedSource(source_id, source_name, str(exc)))
    return MigrationPlan(
        db_path=resolved_db,
        documents_dir=resolved_documents,
        employer_names=employer_names,
        source_document_count=len(rows),
        eligible=tuple(eligible),
        skipped=tuple(skipped),
    )


def stage_plan(plan: MigrationPlan, stage_dir: Path) -> MigrationPlan:
    resolved_stage = Path(stage_dir).resolve()
    resolved_stage.mkdir(parents=True, exist_ok=True)
    staged: list[PlannedDocument] = []
    for item in plan.eligible:
        target = resolved_stage / item.output_name
        if target.exists():
            try:
                current = target.read_bytes()
            except OSError as exc:
                raise UnsupportedSource("staged_file_unreadable") from exc
            if hashlib.sha256(current).hexdigest() != item.output_sha256:
                raise UnsupportedSource("staged_target_exists_mismatch")
        else:
            with target.open("xb") as handle:
                handle.write(item.derived_content)
        staged_content = target.read_bytes()
        if hashlib.sha256(staged_content).hexdigest() != item.output_sha256:
            raise UnsupportedSource("staged_file_hash_mismatch")
        extracted = extract_docx(staged_content)
        if extracted.paragraph_count != item.source_paragraph_count:
            raise UnsupportedSource("staged_paragraph_count_changed")
        staged.append(replace(item, staged_path=str(target)))
    return replace(plan, eligible=tuple(staged))


def _canonical_json(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _plan_payload(plan: MigrationPlan) -> dict[str, object]:
    eligible: list[dict[str, object]] = []
    for item in plan.eligible:
        if item.staged_path is None:
            raise UnsupportedSource("item_not_staged")
        eligible.append(
            {
                "source_id": item.source_id,
                "source_name": item.source_name,
                "source_kind": item.source_kind,
                "source_path": item.source_path,
                "source_sha256": item.source_sha256,
                "source_approved": item.source_approved,
                "source_tags_json": item.source_tags_json,
                "source_created_at": item.source_created_at,
                "is_quant": item.is_quant,
                "output_name": item.output_name,
                "output_sha256": item.output_sha256,
                "source_paragraph_count": item.source_paragraph_count,
                "output_paragraph_count": item.output_paragraph_count,
                "fat_clauses_removed": item.fat_clauses_removed,
                "employer_flattery_clauses": item.employer_flattery_clauses,
                "staged_path": item.staged_path,
            }
        )
    return {
        "schema_version": 1,
        "db_path": str(plan.db_path),
        "documents_dir": str(plan.documents_dir),
        "employer_names": list(plan.employer_names),
        "source_document_count": plan.source_document_count,
        "eligible": eligible,
        "skipped": [
            {
                "source_id": item.source_id,
                "source_name": item.source_name,
                "reason": item.reason,
            }
            for item in plan.skipped
        ],
    }


def save_preflight_report(plan: MigrationPlan, report_path: Path) -> None:
    payload = _plan_payload(plan)
    payload_json = _canonical_json(payload)
    envelope = {
        "payload": payload,
        "payload_sha256": hashlib.sha256(payload_json.encode("utf-8")).hexdigest(),
    }
    target = Path(report_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True))
        handle.write("\n")


def _required_mapping(value: object, reason: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise UnsupportedSource(reason)
    return value


def load_preflight_report(report_path: Path) -> MigrationPlan:
    try:
        envelope = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UnsupportedSource("invalid_preflight_report") from exc
    envelope = _required_mapping(envelope, "invalid_preflight_report")
    payload = _required_mapping(envelope.get("payload"), "invalid_preflight_report")
    expected_hash = envelope.get("payload_sha256")
    actual_hash = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    if not isinstance(expected_hash, str) or expected_hash != actual_hash:
        raise UnsupportedSource("preflight_report_hash_mismatch")
    if payload.get("schema_version") != 1:
        raise UnsupportedSource("unsupported_preflight_schema")
    employer_names_raw = payload.get("employer_names")
    eligible_raw = payload.get("eligible")
    skipped_raw = payload.get("skipped")
    if (
        not isinstance(employer_names_raw, list)
        or any(not isinstance(value, str) for value in employer_names_raw)
        or not isinstance(eligible_raw, list)
        or not isinstance(skipped_raw, list)
    ):
        raise UnsupportedSource("invalid_preflight_report")
    eligible: list[PlannedDocument] = []
    for raw in eligible_raw:
        item = _required_mapping(raw, "invalid_preflight_report")
        try:
            staged_path = Path(str(item["staged_path"])).resolve()
            content = staged_path.read_bytes()
            output_sha = str(item["output_sha256"])
            if hashlib.sha256(content).hexdigest() != output_sha:
                raise UnsupportedSource("staged_file_hash_mismatch")
            extracted = extract_docx(content)
            source_paragraph_count = int(item["source_paragraph_count"])
            output_paragraph_count = int(item["output_paragraph_count"])
            if (
                extracted.paragraph_count != output_paragraph_count
                or output_paragraph_count != source_paragraph_count
            ):
                raise UnsupportedSource("staged_paragraph_count_changed")
            eligible.append(
                PlannedDocument(
                    source_id=str(item["source_id"]),
                    source_name=str(item["source_name"]),
                    source_kind=str(item["source_kind"]),
                    source_path=str(item["source_path"]),
                    source_sha256=str(item["source_sha256"]),
                    source_approved=bool(item["source_approved"]),
                    source_tags_json=str(item["source_tags_json"]),
                    source_created_at=str(item["source_created_at"]),
                    is_quant=bool(item["is_quant"]),
                    output_name=str(item["output_name"]),
                    output_sha256=output_sha,
                    derived_content=content,
                    source_paragraph_count=source_paragraph_count,
                    output_paragraph_count=output_paragraph_count,
                    fat_clauses_removed=int(item["fat_clauses_removed"]),
                    employer_flattery_clauses=int(item["employer_flattery_clauses"]),
                    staged_path=str(staged_path),
                )
            )
        except KeyError as exc:
            raise UnsupportedSource("invalid_preflight_report") from exc
        except OSError as exc:
            raise UnsupportedSource("staged_file_unreadable") from exc
    skipped: list[SkippedSource] = []
    for raw in skipped_raw:
        item = _required_mapping(raw, "invalid_preflight_report")
        try:
            skipped.append(
                SkippedSource(
                    source_id=str(item["source_id"]),
                    source_name=str(item["source_name"]),
                    reason=str(item["reason"]),
                )
            )
        except KeyError as exc:
            raise UnsupportedSource("invalid_preflight_report") from exc
    try:
        source_document_count = int(payload["source_document_count"])
        db_path = Path(str(payload["db_path"])).resolve()
        documents_dir = Path(str(payload["documents_dir"])).resolve()
    except KeyError as exc:
        raise UnsupportedSource("invalid_preflight_report") from exc
    if source_document_count != len(eligible) + len(skipped):
        raise UnsupportedSource("preflight_source_count_mismatch")
    return MigrationPlan(
        db_path=db_path,
        documents_dir=documents_dir,
        employer_names=tuple(employer_names_raw),
        source_document_count=source_document_count,
        eligible=tuple(eligible),
        skipped=tuple(skipped),
    )


def _plan_sha256(plan: MigrationPlan) -> str:
    return hashlib.sha256(_canonical_json(_plan_payload(plan)).encode("utf-8")).hexdigest()


def _png_dimensions(path: Path) -> tuple[int, int]:
    try:
        header = path.read_bytes()[:24]
    except OSError as exc:
        raise UnsupportedSource("render_png_unreadable") from exc
    if (
        len(header) != 24
        or header[:8] != b"\x89PNG\r\n\x1a\n"
        or header[12:16] != b"IHDR"
    ):
        raise UnsupportedSource("render_png_invalid")
    width, height = struct.unpack(">II", header[16:24])
    if width <= 0 or height <= 0:
        raise UnsupportedSource("render_png_invalid")
    return width, height


def save_render_qa(
    plan: MigrationPlan,
    render_root: Path,
    qa_report_path: Path,
    *,
    visual_inspected: bool,
) -> None:
    if visual_inspected is not True:
        raise UnsupportedSource("render_not_visually_inspected")
    resolved_root = Path(render_root).resolve()
    records: list[dict[str, object]] = []
    for item in plan.eligible:
        output_dir = resolved_root / item.output_sha256
        pages = sorted(output_dir.glob("page-*.png"))
        if len(pages) != 1 or pages[0].name != "page-1.png":
            raise UnsupportedSource("render_page_count")
        width, height = _png_dimensions(pages[0])
        png_content = pages[0].read_bytes()
        records.append(
            {
                "output_sha256": item.output_sha256,
                "page_count": 1,
                "page_path": str(pages[0].resolve()),
                "page_sha256": hashlib.sha256(png_content).hexdigest(),
                "width": width,
                "height": height,
            }
        )
    payload = {
        "schema_version": 1,
        "plan_sha256": _plan_sha256(plan),
        "visual_inspected": True,
        "records": records,
    }
    envelope = {
        "payload": payload,
        "payload_sha256": hashlib.sha256(
            _canonical_json(payload).encode("utf-8")
        ).hexdigest(),
    }
    target = Path(qa_report_path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("x", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(envelope, ensure_ascii=False, indent=2, sort_keys=True))
        handle.write("\n")


def load_render_qa(qa_report_path: Path, plan: MigrationPlan) -> None:
    try:
        envelope = json.loads(Path(qa_report_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise UnsupportedSource("invalid_render_qa_report") from exc
    envelope = _required_mapping(envelope, "invalid_render_qa_report")
    payload = _required_mapping(envelope.get("payload"), "invalid_render_qa_report")
    expected_hash = envelope.get("payload_sha256")
    actual_hash = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
    if not isinstance(expected_hash, str) or expected_hash != actual_hash:
        raise UnsupportedSource("render_qa_report_hash_mismatch")
    if payload.get("schema_version") != 1 or payload.get("visual_inspected") is not True:
        raise UnsupportedSource("invalid_render_qa_report")
    if payload.get("plan_sha256") != _plan_sha256(plan):
        raise UnsupportedSource("render_qa_plan_mismatch")
    records = payload.get("records")
    if not isinstance(records, list) or len(records) != len(plan.eligible):
        raise UnsupportedSource("render_qa_record_count")
    by_hash: dict[str, dict[str, object]] = {}
    for raw in records:
        record = _required_mapping(raw, "invalid_render_qa_report")
        output_sha = record.get("output_sha256")
        if not isinstance(output_sha, str) or output_sha in by_hash:
            raise UnsupportedSource("invalid_render_qa_report")
        by_hash[output_sha] = record
    if set(by_hash) != {item.output_sha256 for item in plan.eligible}:
        raise UnsupportedSource("render_qa_output_mismatch")
    for item in plan.eligible:
        record = by_hash[item.output_sha256]
        if record.get("page_count") != 1:
            raise UnsupportedSource("render_page_count")
        page_path = Path(str(record.get("page_path", ""))).resolve()
        output_dir = page_path.parent
        pages = sorted(output_dir.glob("page-*.png"))
        if len(pages) != 1 or pages[0] != page_path or page_path.name != "page-1.png":
            raise UnsupportedSource("render_page_count")
        width, height = _png_dimensions(page_path)
        if width != record.get("width") or height != record.get("height"):
            raise UnsupportedSource("render_png_dimensions_changed")
        if hashlib.sha256(page_path.read_bytes()).hexdigest() != record.get("page_sha256"):
            raise UnsupportedSource("render_png_hash_mismatch")


def _source_file_matches(item: PlannedDocument) -> bool:
    try:
        content = Path(item.source_path).read_bytes()
    except OSError:
        return False
    return hashlib.sha256(content).hexdigest() == item.source_sha256


def _write_live_output(item: PlannedDocument, documents_dir: Path) -> Path:
    if item.staged_path is None:
        raise UnsupportedSource("item_not_staged")
    try:
        content = Path(item.staged_path).read_bytes()
    except OSError as exc:
        raise UnsupportedSource("staged_file_unreadable") from exc
    if hashlib.sha256(content).hexdigest() != item.output_sha256:
        raise UnsupportedSource("staged_file_hash_mismatch")
    extract_docx(content)
    target = documents_dir / item.output_name
    if target.exists():
        if hashlib.sha256(target.read_bytes()).hexdigest() != item.output_sha256:
            raise UnsupportedSource("live_target_exists_mismatch")
        return target
    try:
        with target.open("xb") as handle:
            handle.write(content)
    except FileExistsError:
        if hashlib.sha256(target.read_bytes()).hexdigest() != item.output_sha256:
            raise UnsupportedSource("live_target_exists_mismatch")
    if hashlib.sha256(target.read_bytes()).hexdigest() != item.output_sha256:
        raise UnsupportedSource("live_target_hash_mismatch")
    return target


def _apply_item_once(
    plan: MigrationPlan,
    item: PlannedDocument,
    target: Path,
) -> str:
    connection = sqlite3.connect(plan.db_path, timeout=0, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("BEGIN IMMEDIATE")
        existing = connection.execute(
            "SELECT id,name,kind,path,sha256,approved,tags_json,created_at "
            "FROM documents WHERE sha256=?",
            (item.output_sha256,),
        ).fetchone()
        source = connection.execute(
            "SELECT id,name,kind,path,sha256,approved,tags_json,created_at "
            "FROM documents WHERE id=?",
            (item.source_id,),
        ).fetchone()
        if existing is not None:
            if (
                bool(existing["approved"])
                and str(existing["path"]) == str(target.resolve())
                and str(existing["tags_json"]) == item.source_tags_json
                and source is not None
                and not bool(source["approved"])
            ):
                connection.commit()
                return "already_applied"
            raise UnsupportedSource("derived_row_conflict")
        expected = (
            item.source_id,
            item.source_name,
            item.source_kind,
            item.source_path,
            item.source_sha256,
            item.source_approved,
            item.source_tags_json,
            item.source_created_at,
        )
        actual = None if source is None else (
            str(source["id"]),
            str(source["name"]),
            str(source["kind"]),
            str(Path(str(source["path"])).resolve()),
            str(source["sha256"]),
            bool(source["approved"]),
            str(source["tags_json"]),
            str(source["created_at"]),
        )
        if actual != expected:
            raise UnsupportedSource("source_row_drift")
        created_at = datetime.now(timezone.utc).replace(tzinfo=None).isoformat(" ")
        connection.execute(
            "INSERT INTO documents "
            "(id,name,kind,path,sha256,approved,tags_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (
                str(uuid4()),
                item.output_name,
                item.source_kind,
                str(target.resolve()),
                item.output_sha256,
                1,
                item.source_tags_json,
                created_at,
            ),
        )
        changed = connection.execute(
            "UPDATE documents SET approved=0 "
            "WHERE id=? AND approved=1 AND path=? AND sha256=? AND tags_json=?",
            (
                item.source_id,
                item.source_path,
                item.source_sha256,
                item.source_tags_json,
            ),
        ).rowcount
        if changed != 1:
            raise UnsupportedSource("source_row_drift")
        connection.commit()
        return "applied"
    except BaseException:
        try:
            connection.rollback()
        except sqlite3.Error:
            pass
        raise
    finally:
        connection.close()


def apply_plan(
    plan: MigrationPlan,
    *,
    lock_retries: int = 8,
    retry_delay: float = 0.05,
) -> ApplyReport:
    if lock_retries < 1:
        raise ValueError("lock_retries must be positive")
    applied = 0
    already_applied = 0
    for item in plan.eligible:
        if not _source_file_matches(item):
            raise UnsupportedSource("source_file_drift")
        target = _write_live_output(item, plan.documents_dir)
        for attempt in range(lock_retries):
            try:
                outcome = _apply_item_once(plan, item, target)
                break
            except sqlite3.OperationalError as exc:
                if "database is locked" not in str(exc).casefold() or attempt + 1 >= lock_retries:
                    raise
                time.sleep(retry_delay * (attempt + 1))
        else:  # pragma: no cover - loop always breaks or raises
            raise RuntimeError("database lock retry loop exhausted")
        if outcome == "applied":
            applied += 1
        else:
            already_applied += 1
    return ApplyReport(applied=applied, already_applied=already_applied)


def _summary(plan: MigrationPlan) -> dict[str, object]:
    reasons: dict[str, int] = {}
    for item in plan.skipped:
        reasons[item.reason] = reasons.get(item.reason, 0) + 1
    return {
        "source_documents": plan.source_document_count,
        "eligible": len(plan.eligible),
        "skipped": len(plan.skipped),
        "quant": sum(item.is_quant for item in plan.eligible),
        "non_quant": sum(not item.is_quant for item in plan.eligible),
        "fat_clauses_removed": sum(
            item.fat_clauses_removed for item in plan.eligible
        ),
        "employer_flattery_clauses": sum(
            item.employer_flattery_clauses for item in plan.eligible
        ),
        "skip_reasons": dict(sorted(reasons.items())),
    }


def _required_path(value: str | None, option: str) -> Path:
    if not value:
        raise SystemExit(f"{option} is required for this operation")
    return Path(value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ARGUS Phase 23 CV corrections")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--preflight", action="store_true")
    mode.add_argument("--record-render-qa", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--db")
    parser.add_argument("--documents-dir")
    parser.add_argument("--stage-dir")
    parser.add_argument("--report-json")
    parser.add_argument("--preflight-report")
    parser.add_argument("--render-dir")
    parser.add_argument("--render-qa-report")
    parser.add_argument("--visual-inspected", action="store_true")
    args = parser.parse_args(argv)

    if args.preflight:
        plan = build_plan(
            _required_path(args.db, "--db"),
            _required_path(args.documents_dir, "--documents-dir"),
        )
        staged = stage_plan(plan, _required_path(args.stage_dir, "--stage-dir"))
        save_preflight_report(
            staged,
            _required_path(args.report_json, "--report-json"),
        )
        print(json.dumps(_summary(staged), ensure_ascii=False, sort_keys=True))
        return 0

    plan = load_preflight_report(
        _required_path(args.preflight_report, "--preflight-report")
    )
    if args.record_render_qa:
        save_render_qa(
            plan,
            _required_path(args.render_dir, "--render-dir"),
            _required_path(args.render_qa_report, "--render-qa-report"),
            visual_inspected=args.visual_inspected,
        )
        print(json.dumps({"rendered": len(plan.eligible), "page_spills": 0}, sort_keys=True))
        return 0

    load_render_qa(
        _required_path(args.render_qa_report, "--render-qa-report"),
        plan,
    )
    report = apply_plan(plan)
    print(
        json.dumps(
            {
                "applied": report.applied,
                "already_applied": report.already_applied,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main
    raise SystemExit(main())

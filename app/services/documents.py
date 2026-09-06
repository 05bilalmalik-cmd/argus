from __future__ import annotations

import hashlib
import io
import json
import os
import re
import unicodedata
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
from xml.etree import ElementTree

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Document, Opportunity
from app.repositories import DocumentRepository
from app.security.audit import AuditInput, append_audit


@dataclass(frozen=True, slots=True)
class EmployerExclusivity:
    """Explicit employer identities carried by one document's tags."""

    marker_tags: frozenset[str]
    employer_keys: frozenset[str]

    @property
    def is_exclusive(self) -> bool:
        return bool(self.marker_tags)


class DocumentService:
    GRADUATION_TAGS = frozenset({"grad-2028", "grad-2029"})
    VARIANT_GRADUATION_TAG = {
        "summer-cv": "grad-2028",
        "yii-cv": "grad-2029",
    }
    _MAX_DOCUMENT_XML_BYTES = 2 * 1024 * 1024
    _YEAR_RANGE = re.compile(
        r"\b(?:19|20)\d{2}\s*[-\u2010-\u2015]\s*(2028|2029)\b"
    )
    _DATE_2029_RANGE = re.compile(
        r"\b2025\s*[-\u2010-\u2015]\s*2029\b"
    )
    _YII_DEGREE = "Bachelor of Science: Finance with Year in Industry"
    _SUMMER_DEGREE = "Bachelor of Science: Finance"
    _LEGAL_SUFFIXES = frozenset(
        {
            "co",
            "company",
            "corp",
            "corporation",
            "inc",
            "incorporated",
            "limited",
            "llc",
            "llp",
            "ltd",
            "plc",
        }
    )
    # These aliases are identity normalization, not a candidate-maintained
    # document tag scheme.  They cover punctuation/legal-name variants plus
    # the employer-tailored summer CVs explicitly present in the library.
    _EMPLOYER_KEY_ALIASES = {
        "apolloglobalmanagement": "apolloglobalmanagement",
        "apollo": "apolloglobalmanagement",
        "avivainvestors": "avivainvestors",
        "baincapital": "baincapital",
        "barclays": "barclays",
        "blackrock": "blackrock",
        "carlyle": "carlyle",
        "carlylegroup": "carlyle",
        "evelynpartners": "evelynpartners",
        "goldmansachs": "goldmansachs",
        "imc": "imc",
        "imctrading": "imc",
        "ing": "ing",
        "jpmorgan": "jpmorgan",
        "jpmorganchase": "jpmorgan",
        "loomissayles": "loomissayles",
        "mha": "mha",
        "rsm": "rsm",
        "rsmuk": "rsm",
        "silverlake": "silverlake",
        "tpg": "tpg",
        "tpgcapital": "tpg",
        "troweprice": "troweprice",
        "ubs": "ubs",
        "warburgpincus": "warburgpincus",
        "wellington": "wellingtonmanagement",
        "wellingtonmanagement": "wellingtonmanagement",
    }
    _STATIC_EMPLOYER_KEYS = frozenset(_EMPLOYER_KEY_ALIASES.values())
    _BODY_ALIASES = {
        "apolloglobalmanagement": ("Apollo", "Apollo Global Management"),
        "carlyle": ("Carlyle", "The Carlyle Group"),
        "jpmorgan": (
            "JPMorgan",
            "J.P. Morgan",
            "JP Morgan",
            "JPMorgan Chase",
            "JP Morgan Chase",
        ),
        "tpg": ("TPG", "TPG Capital"),
        "wellingtonmanagement": ("Wellington", "Wellington Management"),
    }
    _NON_EMPLOYER_TAGS = frozenset(
        {"apply-cv", "blanket", "grad-2028", "grad-2029", "summer-cv", "yii-cv"}
    )

    def __init__(self, session: Session, documents_dir: Path):
        self.session = session
        self.documents_dir = documents_dir.resolve()
        self.documents_dir.mkdir(parents=True, exist_ok=True)
        self.repository = DocumentRepository(session)
        self._known_employer_names_cache: tuple[str, ...] | None = None
        self._known_employer_keys_cache: frozenset[str] | None = None
        self._verified_content_cache: dict[
            tuple[str, str, int, int, int], bytes | None
        ] = {}
        self._body_text_cache: dict[
            tuple[str, str, int, int, int], str | None
        ] = {}
        self._body_employer_cache: dict[
            tuple[str, str, int, int, int], frozenset[str] | None
        ] = {}
        self._candidate_background_employer_keys_cache: frozenset[str] | None = None
        self._variant_cache: dict[
            tuple[tuple[str, str, int, int, int], str], bool
        ] = {}

    @staticmethod
    def _safe_filename(filename: str) -> str:
        basename = Path(filename.replace("\\", "/")).name
        stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(basename).stem).strip("._")
        suffix = re.sub(r"[^A-Za-z0-9.]", "", Path(basename).suffix.lower())
        return f"{stem or 'document'}{suffix}"

    @classmethod
    def _filename_employer_keys(
        cls,
        filename: str,
        known_keys: frozenset[str],
    ) -> frozenset[str]:
        """Return employer keys found in a CV filename using the same vocabulary.

        ``JPMorgan``, ``Goldman Sachs``, ``Bank_of_America`` and similar
        employer-bearing name components are detected the same way tags and
        body text are — through ``normalise_employer_name`` and the alias
        table.  Only the first multi-word match starting at each position is
        used to avoid hallucinating sub-word aliases.
        """
        stem = Path(filename.replace("\\", "/")).stem
        words = cls._normalised_words(stem)
        found: set[str] = set()
        for i in range(len(words)):
            for j in range(i + 1, min(i + 4, len(words) + 1)):
                compact = "".join(words[i:j])
                resolved = cls._EMPLOYER_KEY_ALIASES.get(compact, compact)
                if resolved in known_keys:
                    found.add(resolved)
                    break
        return frozenset(found)

    @classmethod
    def _document_xml_bytes(cls, content: bytes) -> bytes | None:
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                info = archive.getinfo("word/document.xml")
                if (
                    info.is_dir()
                    or info.flag_bits & 0x1
                    or info.file_size > cls._MAX_DOCUMENT_XML_BYTES
                ):
                    return None
                with archive.open(info) as source:
                    document_xml = source.read(cls._MAX_DOCUMENT_XML_BYTES + 1)
        except (KeyError, OSError, RuntimeError, zipfile.BadZipFile):
            return None
        if len(document_xml) > cls._MAX_DOCUMENT_XML_BYTES:
            return None
        upper_xml = document_xml.upper()
        if b"<!DOCTYPE" in upper_xml or b"<!ENTITY" in upper_xml:
            return None
        return document_xml

    @classmethod
    def extract_docx_body_text(cls, content: bytes) -> str | None:
        """Return bounded DOCX body text, or ``None`` on any unsafe input.

        The same fail-closed parser backs graduation derivation and employer
        body checks: encrypted or oversized XML, entities/DOCTYPEs, malformed
        ZIPs, and malformed XML are never treated as readable evidence.
        """

        document_xml = cls._document_xml_bytes(content)
        if document_xml is None:
            return None
        try:
            root = ElementTree.fromstring(document_xml)
        except ElementTree.ParseError:
            return None
        paragraphs: list[str] = []
        for paragraph in root.iter():
            if paragraph.tag.rsplit("}", 1)[-1] != "p":
                continue
            visible = "".join(
                node.text or ""
                for node in paragraph.iter()
                if node.tag.rsplit("}", 1)[-1] == "t"
            )
            if visible:
                paragraphs.append(visible)
        return "\n".join(paragraphs)

    @classmethod
    def derive_graduation_tag(cls, content: bytes) -> str | None:
        """Derive exactly one supported graduation year from DOCX body text."""

        text = cls.extract_docx_body_text(content)
        if text is None:
            return None
        years = set(cls._YEAR_RANGE.findall(text))
        if len(years) != 1:
            return None
        return f"grad-{years.pop()}"

    @classmethod
    def cv_degree_year_is_consistent(cls, content: bytes) -> bool:
        """Enforce the existing programme-framing degree/year coupling.

        A summer/2028 CV must use the three-year degree, while a 2029 CV must
        carry the Year in Industry wording.  Unreadable or ambiguous content
        is never accepted as evidence of consistency.
        """

        text = cls.extract_docx_body_text(content)
        graduation_tag = cls.derive_graduation_tag(content)
        if text is None or graduation_tag not in cls.GRADUATION_TAGS:
            return False
        has_year_in_industry = "Year in Industry" in text
        return has_year_in_industry == (graduation_tag == "grad-2029")

    @classmethod
    def _normalised_words(
        cls,
        value: str,
        *,
        strip_legal_suffix: bool = True,
    ) -> tuple[str, ...]:
        ascii_value = unicodedata.normalize("NFKD", str(value or "")).encode(
            "ascii", "ignore"
        ).decode("ascii")
        words = re.findall(r"[a-z0-9]+", ascii_value.casefold().replace("&", " and "))
        if words and words[0] == "the":
            words.pop(0)
        if strip_legal_suffix:
            while words and words[-1] in cls._LEGAL_SUFFIXES:
                words.pop()
            if words and words[-1] == "and":
                words.pop()
        return tuple(words)

    @classmethod
    def normalise_employer_name(cls, value: str) -> str:
        """Return a conservative employer identity key.

        Case, punctuation, ``&``/``and``, whitespace, and trailing legal
        suffixes are presentation differences.  No fuzzy edit-distance or
        substring match is used: an unrecognized tag never becomes evidence
        that two employers are the same.
        """

        compact = "".join(cls._normalised_words(value))
        return cls._EMPLOYER_KEY_ALIASES.get(compact, compact)

    def known_employer_names(self) -> tuple[str, ...]:
        cached = getattr(self, "_known_employer_names_cache", None)
        if cached is not None:
            return cached
        if not hasattr(self, "session"):
            return ()
        names = tuple(
            value
            for value in self.session.scalars(
                select(Opportunity.employer)
                .where(Opportunity.employer != "")
                .distinct()
                .order_by(Opportunity.employer)
            )
            if isinstance(value, str) and value.strip()
        )
        self._known_employer_names_cache = names
        return names

    def known_employer_keys(self) -> frozenset[str]:
        cached = getattr(self, "_known_employer_keys_cache", None)
        if cached is not None:
            return cached
        keys = frozenset(
            {
                *self._STATIC_EMPLOYER_KEYS,
                *(
                    self.normalise_employer_name(name)
                    for name in self.known_employer_names()
                ),
            }
        )
        self._known_employer_keys_cache = keys
        return keys

    def employer_exclusivity(self, document: Document) -> EmployerExclusivity:
        """Classify tags that explicitly resolve to known employer identities."""

        known_keys = self.known_employer_keys()
        markers: dict[str, str] = {}
        for tag in self.tags(document):
            if tag in self._NON_EMPLOYER_TAGS:
                continue
            key = self.normalise_employer_name(tag)
            if key and key in known_keys:
                markers[tag] = key
        return EmployerExclusivity(
            marker_tags=frozenset(markers),
            employer_keys=frozenset(markers.values()),
        )

    @classmethod
    def _normalised_body_text(cls, value: str) -> str:
        return " ".join(cls._normalised_words(value, strip_legal_suffix=False))

    @classmethod
    def _body_aliases_for_employer(cls, employer: str) -> frozenset[str]:
        key = cls.normalise_employer_name(employer)
        aliases = {employer, *cls._BODY_ALIASES.get(key, ())}
        return frozenset(alias.strip() for alias in aliases if alias.strip())

    @classmethod
    def _single_word_alias_in_text(cls, text: str, alias: str) -> bool:
        ascii_alias = unicodedata.normalize("NFKD", alias).encode(
            "ascii", "ignore"
        ).decode("ascii")
        display_words = re.findall(r"[A-Za-z0-9]+", ascii_alias)
        if len(display_words) != 1:
            return False
        display = display_words[0]
        ascii_text = unicodedata.normalize("NFKD", text).encode(
            "ascii", "ignore"
        ).decode("ascii")
        pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(display)}(?![A-Za-z0-9])")
        if pattern.search(ascii_text):
            return True
        if display.islower() and len(display) <= 4:
            insensitive = re.compile(
                rf"(?<![A-Za-z0-9]){re.escape(display)}(?![A-Za-z0-9])",
                re.IGNORECASE,
            )
            return any(match.group(0).isupper() for match in insensitive.finditer(ascii_text))
        if any(character.isdigit() for character in display):
            return re.search(
                rf"(?<![A-Za-z0-9]){re.escape(display)}(?![A-Za-z0-9])",
                ascii_text,
                re.IGNORECASE,
            ) is not None
        return False

    def named_employer_keys_in_text(self, text: str) -> frozenset[str]:
        """Find opportunity-table employer names in text without model calls."""

        normalised = f" {self._normalised_body_text(text)} "
        hits: set[str] = set()
        for employer in self.known_employer_names():
            key = self.normalise_employer_name(employer)
            for alias in self._body_aliases_for_employer(employer):
                alias_words = self._normalised_words(
                    alias,
                    strip_legal_suffix=False,
                )
                if len(alias_words) == 1:
                    found = self._single_word_alias_in_text(text, alias)
                else:
                    found = f" {' '.join(alias_words)} " in normalised
                if found:
                    hits.add(key)
                    break
        return frozenset(hits)

    @staticmethod
    def _document_cache_key(
        document: Document,
    ) -> tuple[str, str, int, int, int] | None:
        try:
            stat = Path(document.path).stat()
        except OSError:
            return None
        if not Path(document.path).is_file():
            return None
        return (
            str(document.id),
            str(document.sha256),
            stat.st_mtime_ns,
            stat.st_size,
            stat.st_ino,
        )

    def _verified_document_content(self, document: Document) -> bytes | None:
        cache_key = self._document_cache_key(document)
        if cache_key is None:
            return None
        cache = getattr(self, "_verified_content_cache", None)
        if cache is None:
            cache = self._verified_content_cache = {}
        if cache_key in cache:
            return cache[cache_key]
        try:
            content = Path(document.path).read_bytes()
        except OSError:
            content = None
        if content is not None and hashlib.sha256(content).hexdigest() != document.sha256:
            content = None
        cache[cache_key] = content
        return content

    def _verified_document_body_text(self, document: Document) -> str | None:
        cache_key = self._document_cache_key(document)
        if cache_key is None:
            return None
        if cache_key in self._body_text_cache:
            return self._body_text_cache[cache_key]
        content = self._verified_document_content(document)
        text = None if content is None else self.extract_docx_body_text(content)
        self._body_text_cache[cache_key] = text
        return text

    def document_body_employer_keys(
        self,
        document: Document,
    ) -> frozenset[str] | None:
        cache_key = self._document_cache_key(document)
        if cache_key is None:
            return None
        if cache_key in self._body_employer_cache:
            return self._body_employer_cache[cache_key]
        text = self._verified_document_body_text(document)
        result = None if text is None else self.named_employer_keys_in_text(text)
        self._body_employer_cache[cache_key] = result
        return result

    def candidate_background_employer_keys(self) -> frozenset[str]:
        """Derive shared candidate-history employers from generic yii CVs.

        An identity is background only when it appears in at least two
        employer-nonexclusive yii CVs and in every such CV considered.  This
        captures shared work/competition history without exempting a
        document-specific employer-tailoring mention.
        """

        cached = getattr(self, "_candidate_background_employer_keys_cache", None)
        if cached is not None:
            return cached
        mention_sets: list[set[str]] = []
        for document in self.repository.approved_by_kind("cv"):
            if not isinstance(document, Document):
                continue
            tags = self.tags(document)
            if "yii-cv" not in tags or self.employer_exclusivity(document).is_exclusive:
                continue
            mentions = self.document_body_employer_keys(document)
            if mentions is not None:
                mention_sets.append(set(mentions))
        background = (
            frozenset(set.intersection(*mention_sets))
            if len(mention_sets) >= 2
            else frozenset()
        )
        self._candidate_background_employer_keys_cache = background
        return background

    def actionable_body_employer_keys(
        self,
        document: Document,
    ) -> frozenset[str] | None:
        mentions = self.document_body_employer_keys(document)
        if mentions is None:
            return None
        return mentions.difference(self.candidate_background_employer_keys())

    def _cv_matches_variant_cached(
        self,
        document: Document,
        required_tag: str,
    ) -> bool:
        required = required_tag.strip().casefold()
        cache_key = self._document_cache_key(document)
        if cache_key is None:
            return False
        key = (cache_key, required)
        if key in self._variant_cache:
            return self._variant_cache[key]
        tags = self.tags(document)
        expected = self.VARIANT_GRADUATION_TAG.get(required)
        if required not in tags:
            result = False
        elif expected is None:
            result = True
        else:
            stored_derived = tags.intersection(self.GRADUATION_TAGS)
            content = self._verified_document_content(document)
            result = (
                stored_derived == {expected}
                and content is not None
                and self.derive_graduation_tag(content) == expected
                and self.cv_degree_year_is_consistent(content)
            )
        self._variant_cache[key] = result
        return result

    @classmethod
    def _replace_visible_text_once(
        cls,
        root: ElementTree.Element,
        old: str,
        new: str,
    ) -> None:
        matches: list[tuple[list[ElementTree.Element], int]] = []
        for paragraph in root.iter():
            if paragraph.tag.rsplit("}", 1)[-1] != "p":
                continue
            nodes = [
                node
                for node in paragraph.iter()
                if node.tag.rsplit("}", 1)[-1] == "t"
            ]
            visible = "".join(node.text or "" for node in nodes)
            start = visible.find(old)
            if start >= 0:
                if visible.find(old, start + 1) >= 0:
                    raise ValueError(f"Expected exactly one occurrence of {old!r}")
                matches.append((nodes, start))
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one occurrence of {old!r}")

        nodes, start = matches[0]
        end = start + len(old)
        replacement = new
        offset = 0
        for node in nodes:
            original = node.text or ""
            node_start, node_end = offset, offset + len(original)
            offset = node_end
            overlap_start = max(start, node_start)
            overlap_end = min(end, node_end)
            if overlap_start >= overlap_end:
                continue
            local_start = overlap_start - node_start
            local_end = overlap_end - node_start
            old_segment_length = local_end - local_start
            if overlap_end == end:
                chunk, replacement = replacement, ""
            else:
                chunk, replacement = (
                    replacement[:old_segment_length],
                    replacement[old_segment_length:],
                )
            node.text = original[:local_start] + chunk + original[local_end:]
        if replacement:
            raise ValueError(f"Replacement for {old!r} did not fit its Word runs")

    @classmethod
    def derive_grad_2028_cv_bytes(cls, content: bytes) -> bytes:
        """Mechanically derive the approved plain-Finance/2028 DOCX variant."""

        source_text = cls.extract_docx_body_text(content)
        document_xml = cls._document_xml_bytes(content)
        if source_text is None or document_xml is None:
            raise ValueError("Source CV body is unreadable or unsafe")
        date_matches = list(cls._DATE_2029_RANGE.finditer(source_text))
        if source_text.count(cls._YII_DEGREE) != 1 or len(date_matches) != 1:
            raise ValueError("Source CV does not contain the exact approved yii degree pair")
        try:
            root = ElementTree.fromstring(document_xml)
        except ElementTree.ParseError as exc:  # pragma: no cover - prechecked
            raise ValueError("Source CV body XML is malformed") from exc

        cls._replace_visible_text_once(root, cls._YII_DEGREE, cls._SUMMER_DEGREE)
        old_range = date_matches[0].group(0)
        cls._replace_visible_text_once(root, old_range, f"{old_range[:-1]}8")
        replacement_xml = ElementTree.tostring(
            root,
            encoding="utf-8",
            xml_declaration=True,
        )

        output = io.BytesIO()
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as source_archive:
                with zipfile.ZipFile(output, "w") as target_archive:
                    target_archive.comment = source_archive.comment
                    for info in source_archive.infolist():
                        payload = source_archive.read(info.filename)
                        if info.filename == "word/document.xml":
                            payload = replacement_xml
                        target_archive.writestr(info, payload)
        except (OSError, RuntimeError, zipfile.BadZipFile) as exc:
            raise ValueError("Source CV archive could not be transformed") from exc

        derived = output.getvalue()
        text = cls.extract_docx_body_text(derived)
        years = cls._YEAR_RANGE.findall(text or "")
        if (
            text is None
            or "Year in Industry" in text
            or "2029" in text
            or text.count(cls._SUMMER_DEGREE) != 1
            or years != ["2028"]
            or cls.derive_graduation_tag(derived) != "grad-2028"
        ):
            raise ValueError("Derived CV failed the plain-Finance/2028 integrity checks")
        return derived

    @staticmethod
    def tags(document: Document) -> set[str]:
        try:
            payload = json.loads(document.tags_json or "[]")
        except (TypeError, ValueError):
            return set()
        if not isinstance(payload, list):
            return set()
        return {
            value.strip().casefold()
            for value in payload
            if isinstance(value, str) and value.strip()
        }

    @classmethod
    def derived_tag_for_document(cls, document: Document) -> str | None:
        path = Path(document.path)
        try:
            content = path.read_bytes()
        except OSError:
            return None
        if hashlib.sha256(content).hexdigest() != document.sha256:
            return None
        return cls.derive_graduation_tag(content)

    @classmethod
    def cv_matches_variant(cls, document: Document, required_tag: str) -> bool:
        required = required_tag.strip().casefold()
        tags = cls.tags(document)
        if required not in tags:
            return False
        expected = cls.VARIANT_GRADUATION_TAG.get(required)
        if expected is None:
            return True
        stored_derived = tags.intersection(cls.GRADUATION_TAGS)
        try:
            content = Path(document.path).read_bytes()
        except OSError:
            return False
        if hashlib.sha256(content).hexdigest() != document.sha256:
            return False
        return (
            stored_derived == {expected}
            and cls.derive_graduation_tag(content) == expected
            and cls.cv_degree_year_is_consistent(content)
        )

    def store_bytes(
        self,
        *,
        filename: str,
        content: bytes,
        kind: str,
        tags: tuple[str, ...] = (),
        approved: bool = False,
        actor: str = "user",
    ) -> Document:
        digest = hashlib.sha256(content).hexdigest()
        existing = self.session.scalar(select(Document).where(Document.sha256 == digest))
        if existing:
            return existing
        normalised_tags = {
            value.strip().casefold()
            for value in tags
            if isinstance(value, str) and value.strip()
        }
        normalised_tags.difference_update(self.GRADUATION_TAGS)
        if kind.strip().casefold() == "cv":
            derived_tag = self.derive_graduation_tag(content)
            if derived_tag is not None:
                normalised_tags.add(derived_tag)
        safe = self._safe_filename(filename)
        target = self.documents_dir / safe
        if target.exists():
            target = target.with_name(f"{target.stem}_{digest[:8]}{target.suffix}")
        temporary = self.documents_dir / f".{uuid4().hex}.upload"
        temporary.write_bytes(content)
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        temporary.replace(target)
        if os.name != "nt":
            os.chmod(target, 0o600)
        document = self.repository.add(
            Document(
                name=safe,
                kind=kind,
                path=str(target.resolve()),
                sha256=digest,
                approved=approved,
                tags_json=json.dumps(sorted(normalised_tags)),
            )
        )
        append_audit(
            self.session,
            AuditInput(
                actor,
                "document.stored",
                "document",
                document.id,
                {"kind": kind, "approved": approved, "sha256": digest},
            ),
        )
        return document

    @staticmethod
    def verify(document: Document) -> bool:
        path = Path(document.path)
        if not path.is_file():
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == document.sha256

    def supersede(
        self,
        source: Document,
        derivative: Document,
        *,
        actor: str = "system",
        reason: str,
    ) -> bool:
        """Reversibly retire ``source`` in favour of a verified derivative."""

        if source.id == derivative.id:
            raise ValueError("A document cannot supersede itself")
        if source.kind.casefold() != derivative.kind.casefold():
            raise ValueError("A supersession must preserve the document kind")
        if not derivative.approved:
            raise ValueError("The superseding derivative must be approved")
        if not self.verify(source) or not self.verify(derivative):
            raise ValueError("Both supersession documents must pass hash verification")
        if not source.approved:
            return False
        source.approved = False
        append_audit(
            self.session,
            AuditInput(
                actor,
                "document.superseded",
                "document",
                source.id,
                {
                    "derivative_document_id": derivative.id,
                    "derivative_sha256": derivative.sha256,
                    "reason": reason,
                    "source_sha256": source.sha256,
                },
            ),
        )
        self.session.flush()
        return True

    def create_grad_2028_blanket_variant(
        self,
        source: Document,
        *,
        filename: str,
        actor: str = "system",
    ) -> Document:
        """Create one verified summer variant without changing its source."""

        if source.kind.casefold() != "cv" or not source.approved:
            raise ValueError("Blanket source must be an approved CV")
        if not self.verify(source) or not self.cv_matches_variant(source, "yii-cv"):
            raise ValueError("Blanket source does not verify as a yii/grad-2029 CV")
        source_path = Path(source.path)
        source_content = source_path.read_bytes()
        derived_content = self.derive_grad_2028_cv_bytes(source_content)
        derived_text = self.extract_docx_body_text(derived_content)
        if derived_text is None:
            raise ValueError("Derived blanket CV body is unreadable")
        named_employers = self.named_employer_keys_in_text(derived_text).difference(
            self.candidate_background_employer_keys()
        )
        if named_employers:
            raise ValueError(
                "Blanket source names an employer: "
                + ", ".join(sorted(named_employers))
            )

        source_tags = self.tags(source)
        target_tags = source_tags.difference(
            self.GRADUATION_TAGS,
            self.VARIANT_GRADUATION_TAG,
        )
        target_tags.add("summer-cv")
        existing = self.session.scalar(
            select(Document).where(
                Document.sha256 == hashlib.sha256(derived_content).hexdigest()
            )
        )
        derived = self.store_bytes(
            filename=filename,
            content=derived_content,
            kind="cv",
            tags=tuple(sorted(target_tags)),
            approved=True,
            actor=actor,
        )
        expected_tags = target_tags | {"grad-2028"}
        if (
            not derived.approved
            or self.tags(derived) != expected_tags
            or not self.cv_matches_variant(derived, "summer-cv")
        ):
            raise RuntimeError("Existing or created summer CV does not match its contract")
        if existing is None:
            append_audit(
                self.session,
                AuditInput(
                    actor,
                    "document.grad_2028_blanket_derived",
                    "document",
                    derived.id,
                    {
                        "source_document_id": source.id,
                        "source_sha256": source.sha256,
                        "derived_sha256": derived.sha256,
                        "degree_transform": f"{self._YII_DEGREE} -> {self._SUMMER_DEGREE}",
                        "date_transform": "2025-2029 -> 2025-2028",
                    },
                ),
            )
        return derived

    def _cv_permitted_for_employer(
        self,
        document: Document,
        employer_key: str,
    ) -> bool:
        exclusivity = self.employer_exclusivity(document)
        if exclusivity.is_exclusive and exclusivity.employer_keys != {employer_key}:
            return False
        # Unit-test stand-ins exercise tag scoring only.  Every production row
        # is a real Document and therefore must pass verified body extraction.
        if not isinstance(document, Document):
            return True
        body_keys = self.actionable_body_employer_keys(document)
        if body_keys is None:
            return False
        if body_keys.difference({employer_key}):
            return False
        # Filename must not name a known employer other than the target.
        # A CV named Demo_Candidate_JPMorgan_CV.docx must never be sent to
        # Bloomberg, even if the tag and body-text checks don't catch it.
        filename_keys = self._filename_employer_keys(
            getattr(document, "name", "") or "",
            self.known_employer_keys(),
        )
        if filename_keys and employer_key not in filename_keys:
            return False
        return True

    def document_body_only_employer_keys(self, document: Document) -> frozenset[str]:
        """Return body employer identities not represented by employer tags."""

        body_keys = self.actionable_body_employer_keys(document)
        if body_keys is None:
            return frozenset()
        return body_keys.difference(self.employer_exclusivity(document).employer_keys)

    BLANKET_TAG = "blanket"  # marks generic-role copies; never passed in desired_tags

    @staticmethod
    def _created_at_sort_value(document: Document) -> float:
        created_at = getattr(document, "created_at", None)
        if not isinstance(created_at, datetime):
            return float("inf")
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at.astimezone(timezone.utc).timestamp()

    def select_approved(
        self,
        kind: str,
        desired_tags: tuple[str, ...] = (),
        required_tag: str | None = None,
        *,
        employer: str | None = None,
        forbidden_body_phrases: tuple[str, ...] = (),
    ) -> Document | None:
        """Pick the approved document with the most POSITIVE tag evidence.

        A document with zero overlap with ``desired_tags`` is never selected:
        silently uploading an unrelated CV variant would misrepresent the
        candidate.  When no approved document shares at least one desired
        tag, return None so the caller fails closed (required_cv_missing).
        """

        documents = self.repository.approved_by_kind(kind)
        if not documents:
            return None
        forbidden_phrases = tuple(
            phrase
            for phrase in forbidden_body_phrases
            if isinstance(phrase, str) and phrase
        )
        if kind.strip().casefold() == "cv" and forbidden_phrases:
            documents = [
                document
                for document in documents
                if (
                    (body_text := self._verified_document_body_text(document))
                    is not None
                    and not any(phrase in body_text for phrase in forbidden_phrases)
                )
            ]
            if not documents:
                return None
        required = str(required_tag or "").strip().casefold()
        if required:
            documents = [
                document
                for document in documents
                if required in self.tags(document)
                and (
                    required not in self.VARIANT_GRADUATION_TAG
                    or self._cv_matches_variant_cached(document, required)
                )
            ]
            if not documents:
                return None
        desired = {
            value.strip().casefold()
            for value in desired_tags
            if isinstance(value, str) and value.strip()
        }
        if kind.strip().casefold() == "cv":
            employer_key = self.normalise_employer_name(employer or "")
            if not employer_key:
                known_keys = self.known_employer_keys()
                desired_employer_keys = {
                    key
                    for value in desired
                    if (key := self.normalise_employer_name(value)) in known_keys
                }
                if len(desired_employer_keys) == 1:
                    employer_key = desired_employer_keys.pop()
            if employer_key or required:
                documents = [
                    document
                    for document in documents
                    if self._cv_permitted_for_employer(document, employer_key)
                ]
                if not documents:
                    return None
        if not desired:
            # No tags requested: only a document with no tag constraints
            # (empty tag set) may be considered an exact match.
            untagged = [d for d in documents if not self.tags(d)]
            return untagged[0] if len(untagged) == 1 else None
        scored = [
            (len(desired.intersection(self.tags(document))), document)
            for document in documents
        ]
        scored.sort(
            key=lambda pair: (
                -pair[0],
                self._created_at_sort_value(pair[1]),
                getattr(pair[1], "id", ""),
            )
        )
        best_score, best = scored[0]
        if best_score <= 0:
            return None  # zero overlap is NOT evidence of suitability
        # Deterministic tie rule: most overlap, then oldest created_at, then id.
        return best

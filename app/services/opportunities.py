from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import asc, desc, func, or_, select
from sqlalchemy.orm import Session

from app.models import Opportunity
from app.repositories import OpportunityRepository
from app.security.audit import AuditInput, append_audit
from app.domain.targets import (
    canonical_url_key,
    source_fingerprint,
    source_identity_key,
    validate_navigation_url,
)


@dataclass(frozen=True, slots=True)
class ImportErrorRecord:
    row_number: int
    message: str


@dataclass(frozen=True, slots=True)
class ImportReport:
    imported: int
    skipped_duplicates: int
    errors: tuple[ImportErrorRecord, ...]


@dataclass(frozen=True, slots=True)
class OpportunityPage:
    """A bounded, deterministic slice for the opportunities inbox."""

    records: tuple[Opportunity, ...]
    total_count: int
    page: int
    per_page: int
    sort: str
    direction: str
    query: str
    source: str
    cycle: str
    programme_group: str

    @property
    def total_pages(self) -> int:
        return max(1, (self.total_count + self.per_page - 1) // self.per_page)


_HEADER_ALIASES = {
    "employer": {"employer", "company", "firm", "organisation", "organization"},
    "role_title": {"role_title", "role", "title", "programme", "program", "job"},
    "division": {"division", "category", "business_area", "team"},
    "programme_group": {"programme_group", "program_group", "application_group"},
    "location": {"location", "city", "office"},
    "cycle": {"cycle", "year", "internship_year", "recruitment_cycle"},
    "url": {"url", "link", "application_url", "application_link", "job_url"},
    "source": {"source"},
    "ats_type": {"ats", "ats_type", "platform"},
    "deadline": {"deadline", "closing_date", "close_date", "application_deadline"},
    "rolling": {"rolling", "rolling_basis"},
    "min_graduation_year": {"min_graduation_year", "min_grad_year"},
    "max_graduation_year": {"max_graduation_year", "max_grad_year"},
    "sponsorship_supported": {"sponsorship", "sponsorship_supported"},
    "cv_required": {"cv", "cv_required", "resume", "resume_required"},
    "cover_letter_required": {"cover_letter", "cover_letter_required", "cover_letter_"},
    "written_answers_required": {"written_answers", "written_answers_required", "questions"},
    "notes": {"notes", "comments"},
}


def _normalise_header(value: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    for canonical, aliases in _HEADER_ALIASES.items():
        if value in aliases:
            return canonical
    return value


def normalise_url(value: str) -> str:
    return canonical_url_key(value)


def normalise_application_url(value: str) -> str:
    try:
        # Despite the legacy function name this value is persisted as the
        # source navigation URL.  A lossy canonical key is computed separately
        # by ``source_fingerprint`` and must never replace the URL we navigate.
        return validate_navigation_url(value)
    except ValueError as exc:
        raise ValueError("Application URL must use http or https") from exc


def _parse_bool(value: str, default: bool | None = False) -> bool | None:
    normalised = value.strip().lower()
    if not normalised:
        return default
    if normalised in {"yes", "y", "true", "1", "required", "supported"}:
        return True
    if normalised in {"no", "n", "false", "0", "not required", "unsupported"}:
        return False
    if normalised in {"unknown", "tbc", "n/a"}:
        return None
    raise ValueError(f"Invalid boolean value: {value}")


def _parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    for pattern in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y"):
        try:
            return datetime.strptime(value, pattern).date()
        except ValueError:
            pass
    raise ValueError(f"Invalid deadline: {value}")


def _parse_int(value: str) -> int | None:
    value = value.strip()
    if not value:
        return None
    return int(value)


class OpportunityService:
    def __init__(self, session: Session):
        self.session = session
        self.repository = OpportunityRepository(session)

    def list(self) -> list[Opportunity]:
        return list(
            self.session.scalars(
                select(Opportunity).order_by(Opportunity.deadline, Opportunity.created_at.desc())
            ).all()
        )

    def list_page(
        self,
        *,
        query: str = "",
        source: str = "",
        cycle: str = "",
        programme_group: str = "",
        sort: str = "deadline",
        direction: str = "asc",
        page: int = 1,
        per_page: int = 20,
    ) -> OpportunityPage:
        """Return one server-filtered page without loading the entire inbox."""

        page = max(1, int(page))
        per_page = min(100, max(1, int(per_page)))
        sort = (
            sort
            if sort in {"deadline", "created_at", "employer", "role_title", "cycle"}
            else "deadline"
        )
        direction = "desc" if direction.casefold() == "desc" else "asc"
        query = query.strip()
        source = source.strip()
        cycle = cycle.strip()
        programme_group = programme_group.strip()

        filters = []
        if query:
            pattern = f"%{query}%"
            filters.append(
                or_(
                    Opportunity.employer.ilike(pattern),
                    Opportunity.role_title.ilike(pattern),
                    Opportunity.division.ilike(pattern),
                    Opportunity.location.ilike(pattern),
                )
            )
        if source:
            filters.append(Opportunity.source == source)
        if cycle:
            filters.append(Opportunity.cycle == cycle)
        if programme_group:
            filters.append(Opportunity.programme_group == programme_group)

        count = self.session.scalar(
            select(func.count(Opportunity.id)).where(*filters)
        ) or 0
        total_pages = max(1, (int(count) + per_page - 1) // per_page)
        page = min(page, total_pages)
        sort_column = {
            "deadline": Opportunity.deadline,
            "created_at": Opportunity.created_at,
            "employer": Opportunity.employer,
            "role_title": Opportunity.role_title,
            "cycle": Opportunity.cycle,
        }[sort]
        ordering = desc(sort_column) if direction == "desc" else asc(sort_column)
        statement = (
            select(Opportunity)
            .where(*filters)
            .order_by(ordering.nulls_last(), asc(Opportunity.id))
            .offset((page - 1) * per_page)
            .limit(per_page)
        )
        records = tuple(self.session.scalars(statement).all())
        return OpportunityPage(
            records=records,
            total_count=int(count),
            page=page,
            per_page=per_page,
            sort=sort,
            direction=direction,
            query=query,
            source=source,
            cycle=cycle,
            programme_group=programme_group,
        )

    def find_exact(self, opportunity: Opportunity) -> Opportunity | None:
        opportunity.url = normalise_application_url(opportunity.url)
        fingerprint = source_fingerprint(
            employer=opportunity.employer,
            role_title=opportunity.role_title,
            cycle=opportunity.cycle,
            source_url=opportunity.url,
            location=opportunity.location,
            division=opportunity.division,
        )
        wanted = source_identity_key(
            employer=opportunity.employer,
            role_title=opportunity.role_title,
            cycle=opportunity.cycle,
            source_url=opportunity.url,
            location=opportunity.location,
            division=opportunity.division,
        )
        candidates = self.repository.source_identity_candidates(
            fingerprint=fingerprint,
            employer=opportunity.employer,
            role_title=opportunity.role_title,
            cycle=opportunity.cycle,
        )
        for candidate in candidates:
            candidate_key = source_identity_key(
                employer=candidate.employer,
                role_title=candidate.role_title,
                cycle=candidate.cycle,
                source_url=candidate.url,
                location=candidate.location,
                division=candidate.division,
            )
            if candidate_key == wanted:
                return candidate
        return None

    def add(self, opportunity: Opportunity, *, actor: str = "user") -> Opportunity:
        opportunity.url = normalise_application_url(opportunity.url)
        opportunity.source_fingerprint = source_fingerprint(
            employer=opportunity.employer,
            role_title=opportunity.role_title,
            cycle=opportunity.cycle,
            source_url=opportunity.url,
            location=opportunity.location,
            division=opportunity.division,
        )
        existing = self.find_exact(opportunity)
        if existing:
            return existing
        saved = self.repository.add(opportunity)
        append_audit(
            self.session,
            AuditInput(
                actor,
                "opportunity.created",
                "opportunity",
                saved.id,
                {"employer": saved.employer, "role": saved.role_title, "source": saved.source},
            ),
        )
        if not saved.is_archived and saved.is_open_for_applications:
            from app.services.notifications import (
                OpportunityDiscoveredEvent,
                queue_discovery_notification,
            )

            queue_discovery_notification(
                self.session,
                OpportunityDiscoveredEvent(
                    employer=saved.employer,
                    role_title=saved.role_title,
                    deadline=str(saved.deadline) if saved.deadline else None,
                    application_url=saved.application_url,
                ),
            )
        return saved

    def import_csv(self, content: bytes, source: str) -> ImportReport:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            return ImportReport(0, 0, (ImportErrorRecord(1, "CSV must use UTF-8 encoding"),))
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return ImportReport(0, 0, (ImportErrorRecord(1, "CSV has no header row"),))
        canonical_headers = [_normalise_header(header or "") for header in reader.fieldnames]
        for required in ("employer", "role_title", "url", "cycle"):
            if required not in canonical_headers:
                return ImportReport(
                    0, 0, (ImportErrorRecord(1, f"Missing required column: {required}"),)
                )

        imported = 0
        duplicates = 0
        errors: list[ImportErrorRecord] = []
        seen: set[tuple[str, str, str, str, str, str]] = set()
        for row_number, raw in enumerate(reader, start=2):
            row = {
                _normalise_header(key or ""): (value or "").strip()
                for key, value in raw.items()
            }
            try:
                url = normalise_application_url(row["url"])
                employer = row["employer"].strip()
                role = row["role_title"].strip()
                cycle = row["cycle"].strip()
                if not employer or not role or not cycle:
                    raise ValueError("Employer, role, cycle, and URL are required")
                identity = source_identity_key(
                    employer=employer,
                    role_title=role,
                    cycle=cycle,
                    source_url=url,
                    location=row.get("location", ""),
                    division=row.get("division", ""),
                )
                if identity in seen:
                    duplicates += 1
                    continue
                opportunity = Opportunity(
                    employer=employer,
                    role_title=role,
                    division=row.get("division", ""),
                    programme_group=row.get("programme_group", ""),
                    location=row.get("location", ""),
                    cycle=cycle,
                    url=url,
                    source=source,
                    ats_type=row.get("ats_type", "unknown") or "unknown",
                    deadline=_parse_date(row.get("deadline", "")),
                    rolling=bool(_parse_bool(row.get("rolling", ""), False)),
                    min_graduation_year=_parse_int(row.get("min_graduation_year", "")),
                    max_graduation_year=_parse_int(row.get("max_graduation_year", "")),
                    sponsorship_supported=_parse_bool(
                        row.get("sponsorship_supported", ""), None
                    ),
                    cv_required=bool(_parse_bool(row.get("cv_required", ""), True)),
                    cover_letter_required=bool(
                        _parse_bool(row.get("cover_letter_required", ""), False)
                    ),
                    written_answers_required=bool(
                        _parse_bool(row.get("written_answers_required", ""), False)
                    ),
                    notes=row.get("notes", ""),
                )
                if self.find_exact(opportunity) is not None:
                    duplicates += 1
                    continue
                self.add(opportunity, actor="csv_import")
                seen.add(identity)
                imported += 1
            except (ValueError, KeyError) as exc:
                errors.append(ImportErrorRecord(row_number, str(exc)))
        return ImportReport(imported, duplicates, tuple(errors))

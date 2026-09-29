from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr, parsedate_to_datetime
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.domain.states import ApplicationState, InvalidTransition, validate_transition
from app.models import Application, EmailMessage, Opportunity
from app.security.audit import AuditInput, append_audit

_LONDON = ZoneInfo("Europe/London")


@dataclass(frozen=True, slots=True)
class ParsedEmail:
    message_id: str | None
    sender: str
    subject: str
    received_at: datetime
    body_text: str


@dataclass(frozen=True, slots=True)
class EmailClassification:
    kind: str
    confidence: float
    reason: str


def _plain_body(message) -> str:
    if message.is_multipart():
        plain: list[str] = []
        html: list[str] = []
        for part in message.walk():
            if part.get_content_disposition() == "attachment":
                continue
            content_type = part.get_content_type()
            if content_type not in {"text/plain", "text/html"}:
                continue
            try:
                content = part.get_content()
            except Exception:
                payload = part.get_payload(decode=True) or b""
                content = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
            if content_type == "text/plain":
                plain.append(str(content))
            else:
                html.append(BeautifulSoup(str(content), "html.parser").get_text(" ", strip=True))
        return "\n".join(plain or html)
    try:
        content = message.get_content()
    except Exception:
        payload = message.get_payload(decode=True) or b""
        content = payload.decode(message.get_content_charset() or "utf-8", errors="replace")
    if message.get_content_type() == "text/html":
        return BeautifulSoup(str(content), "html.parser").get_text(" ", strip=True)
    return str(content)


def parse_eml(content: bytes) -> ParsedEmail:
    if not content:
        raise ValueError("Email file is empty")
    message = BytesParser(policy=policy.default).parsebytes(content)
    raw_date = message.get("Date")
    try:
        received = parsedate_to_datetime(raw_date) if raw_date else datetime.now(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        received = datetime.now(timezone.utc)
    if received.tzinfo is None:
        received = received.replace(tzinfo=timezone.utc)
    sender = parseaddr(str(message.get("From", "")))[1] or str(message.get("From", ""))
    return ParsedEmail(
        message_id=str(message.get("Message-ID")) if message.get("Message-ID") else None,
        sender=sender.strip(),
        subject=str(message.get("Subject", "")).strip(),
        received_at=received,
        body_text=_plain_body(message).strip(),
    )


def classify_email(email: ParsedEmail) -> EmailClassification:
    text = f"{email.subject}\n{email.body_text}".casefold()
    rules: tuple[tuple[str, tuple[str, ...], float, str], ...] = (
        (
            "rejection",
            (
                "regret to inform",
                "will not proceed",
                "not be progressing",
                "unsuccessful",
                "unfortunately your application",
            ),
            0.99,
            "Rejection language detected",
        ),
        (
            "hirevue",
            ("hirevue", "record your video", "video interview", "recorded interview"),
            0.99,
            "Recorded-video invitation detected",
        ),
        (
            "assessment",
            (
                "online assessment",
                "numerical reasoning",
                "psychometric",
                "coding assessment",
                "complete the test",
            ),
            0.98,
            "Online assessment invitation detected",
        ),
        (
            "interview",
            (
                "interview invitation",
                "invite you to an interview",
                "first-round interview",
                "schedule your interview",
                "interview availability",
            ),
            0.98,
            "Interview invitation detected",
        ),
        (
            "confirmation",
            (
                "thank you for applying",
                "application received",
                "we received your application",
                "application has been submitted",
                "application submission confirmation",
            ),
            0.96,
            "Application confirmation detected",
        ),
    )
    for kind, phrases, confidence, reason in rules:
        if any(phrase in text for phrase in phrases):
            return EmailClassification(kind, confidence, reason)
    return EmailClassification("other", 0.2, "No recruitment event matched")


def _local_datetime(day: date, hour: int = 23, minute: int = 59) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=_LONDON)


def extract_deadline(text: str, reference_date: date) -> datetime | None:
    compact = " ".join(text.split())
    month_pattern = (
        r"(?P<month>January|February|March|April|May|June|July|August|September|October|"
        r"November|December|Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)"
    )
    pattern = re.compile(
        rf"(?:by|before|deadline(?: is)?|complete(?: this)? by)\s*"
        rf"(?:"
        rf"(?P<hour12>\d{{1,2}})(?::(?P<minute12>\d{{2}}))?\s*(?P<meridiem>am|pm)\s*(?:on\s+)?"
        rf"|(?P<hour24>\d{{1,2}}):(?P<minute24>\d{{2}})\s*(?:on\s+)?"
        rf")?"
        rf"(?P<day>\d{{1,2}})\s+{month_pattern}\s+(?P<year>\d{{4}})",
        re.IGNORECASE,
    )
    match = pattern.search(compact)
    if match:
        month_names = {
            "jan": 1,
            "january": 1,
            "feb": 2,
            "february": 2,
            "mar": 3,
            "march": 3,
            "apr": 4,
            "april": 4,
            "may": 5,
            "jun": 6,
            "june": 6,
            "jul": 7,
            "july": 7,
            "aug": 8,
            "august": 8,
            "sep": 9,
            "sept": 9,
            "september": 9,
            "oct": 10,
            "october": 10,
            "nov": 11,
            "november": 11,
            "dec": 12,
            "december": 12,
        }
        try:
            parsed_day = date(
                int(match.group("year")),
                month_names[match.group("month").casefold()],
                int(match.group("day")),
            )
            hour = int(match.group("hour24") or match.group("hour12") or 23)
            minute = int(match.group("minute24") or match.group("minute12") or 59)
            meridiem = match.group("meridiem")
            if meridiem:
                if not 1 <= hour <= 12:
                    return None
                if meridiem.casefold() == "pm" and hour < 12:
                    hour += 12
                elif meridiem.casefold() == "am" and hour == 12:
                    hour = 0
            return _local_datetime(parsed_day, hour, minute)
        except (KeyError, TypeError, ValueError):
            return None

    numeric = re.search(
        r"(?:by|before|deadline(?: is)?|complete(?: this)? by)\s*"
        r"(?:(\d{1,2}):([0-5]\d)\s*(?:on\s+)?)?"
        r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})",
        compact,
        re.IGNORECASE,
    )
    if numeric:
        hour, minute, day_raw, month_raw, year_raw = numeric.groups()
        try:
            return _local_datetime(
                date(int(year_raw), int(month_raw), int(day_raw)),
                int(hour) if hour else 23,
                int(minute) if minute else 59,
            )
        except ValueError:
            return None

    relative = re.search(r"\bwithin\s+(\d{1,2})\s+(business\s+)?days?\b", compact, re.IGNORECASE)
    if relative:
        count = int(relative.group(1))
        target = reference_date
        if relative.group(2):
            remaining = count
            while remaining:
                target += timedelta(days=1)
                if target.weekday() < 5:
                    remaining -= 1
        else:
            target += timedelta(days=count)
        return _local_datetime(target)
    if re.search(r"\bby tomorrow\b", compact, re.IGNORECASE):
        return _local_datetime(reference_date + timedelta(days=1))
    return None


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.casefold())


class MailService:
    def __init__(self, session: Session):
        self.session = session

    def score_candidates(
        self,
        *,
        sender: str,
        subject: str,
        body_text: str,
        limit: int = 5,
        rows=None,  # noqa: ANN001 - preloaded (Application, Opportunity) pairs
    ) -> list[tuple[Application, int]]:
        """Score every application against mail text, best first, bounded."""

        try:
            keep = max(1, min(int(limit), 25))
        except (TypeError, ValueError):
            keep = 5
        text = f"{sender} {subject} {body_text}"
        normalised = _normalise(text)
        if rows is None:
            rows = self.session.execute(
                select(Application, Opportunity).join(Opportunity)
            ).all()
        scored: list[tuple[int, Application]] = []
        for application, opportunity in rows:
            score = 0
            if application.submission_reference and application.submission_reference.casefold() in text.casefold():
                score += 100
            employer = _normalise(opportunity.employer)
            role = _normalise(opportunity.role_title)
            if employer and employer in normalised:
                score += 20
            if role and role in normalised:
                score += 8
            if score:
                scored.append((score, application))
        scored.sort(key=lambda item: item[0], reverse=True)
        return [(application, score) for score, application in scored[:keep]]

    def _match_application(self, parsed: ParsedEmail) -> Application | None:
        top = self.score_candidates(
            sender=parsed.sender,
            subject=parsed.subject,
            body_text=parsed.body_text,
            limit=1,
        )
        return top[0][0] if top else None

    def match_candidates(
        self, record_id: str, *, limit: int = 5, rows=None  # noqa: ANN001 - preloaded pairs
    ) -> list[dict[str, object]]:
        """Return bounded, scored binding candidates for a stored message."""

        record = self.session.get(EmailMessage, record_id)
        if record is None:
            raise KeyError(f"Email not found: {record_id}")
        candidates = []
        for application, score in self.score_candidates(
            sender=record.sender or "",
            subject=record.subject or "",
            body_text=record.body_text or "",
            limit=limit,
            rows=rows,
        ):
            opportunity = application.opportunity
            candidates.append(
                {
                    "application_id": application.id,
                    "employer": str(getattr(opportunity, "employer", "")),
                    "role": str(getattr(opportunity, "role_title", "")),
                    "state": str(application.state),
                    "score": int(score),
                }
            )
        return candidates

    def override_match(
        self, record_id: str, application_id: str | None
    ) -> EmailMessage:
        """Rebind a stored message; never rewrites past application states.

        Binding (or clearing) the record is exact and audited. Applying the
        message to the newly bound application reuses ingest semantics, which
        refuse invalid transitions instead of rewriting history. A transition
        the message previously caused on another application is left standing.
        """

        record = self.session.get(EmailMessage, record_id)
        if record is None:
            raise KeyError(f"Email not found: {record_id}")
        previous = record.application_id
        target = (application_id or "").strip() or None
        if target is not None and self.session.get(Application, target) is None:
            raise KeyError(f"Application not found: {target}")
        record.application_id = target
        self.session.flush()
        if target is not None:
            application = self.session.get(Application, target)
            if application is None:  # re-checked after flush; never assume
                raise KeyError(f"Application not found: {target}")
            pseudo = EmailClassification(record.classification or "other", 0.0, "operator override")
            self._apply(application, pseudo, record.action_deadline)
        append_audit(
            self.session,
            AuditInput(
                "mail",
                "email.match_overridden",
                "email",
                record.id,
                {"previous_application_id": previous, "application_id": target},
            ),
        )
        return record

    def _transition(self, application: Application, target: ApplicationState) -> None:
        current = ApplicationState(application.state)
        if current == target:
            return
        validate_transition(current, target)
        application.state = target.value
        self.session.flush()
        append_audit(
            self.session,
            AuditInput(
                "mail",
                "application.state_changed",
                "application",
                application.id,
                {"from": current.value, "to": target.value},
            ),
        )

    def _apply(self, application: Application, classification: EmailClassification, deadline: datetime | None) -> None:
        kind = classification.kind
        current = ApplicationState(application.state)
        try:
            if kind == "confirmation" and current == ApplicationState.SUBMITTED:
                self._transition(application, ApplicationState.CONFIRMATION_VERIFIED)
            elif kind in {"assessment", "hirevue"} and current in {
                ApplicationState.CONFIRMATION_VERIFIED,
                ApplicationState.NEEDS_OA,
            }:
                self._transition(application, ApplicationState.OA_PENDING)
                application.next_action = (
                    "Complete HireVue video interview" if kind == "hirevue" else "Complete online assessment"
                )
            elif kind == "interview" and current in {
                ApplicationState.CONFIRMATION_VERIFIED,
                ApplicationState.OA_PENDING,
            }:
                self._transition(application, ApplicationState.INTERVIEW)
                application.next_action = "Prepare for interview"
            elif kind == "rejection" and current in {
                ApplicationState.CONFIRMATION_VERIFIED,
                ApplicationState.OA_PENDING,
                ApplicationState.INTERVIEW,
            }:
                self._transition(application, ApplicationState.REJECTED)
                application.next_action = ""
        except InvalidTransition:
            return
        if deadline and kind in {"assessment", "hirevue", "interview"}:
            application.next_action_deadline = deadline
        self.session.flush()

    def ingest(self, content: bytes) -> EmailMessage:
        parsed = parse_eml(content)
        if parsed.message_id:
            existing = self.session.scalar(
                select(EmailMessage).where(EmailMessage.message_id == parsed.message_id)
            )
            if existing:
                return existing
        classification = classify_email(parsed)
        deadline = extract_deadline(parsed.body_text, parsed.received_at.astimezone(_LONDON).date())
        application = self._match_application(parsed)
        record = EmailMessage(
            message_id=parsed.message_id,
            sender=parsed.sender,
            subject=parsed.subject,
            received_at=parsed.received_at,
            body_text=parsed.body_text,
            classification=classification.kind,
            application_id=application.id if application else None,
            action_deadline=deadline,
        )
        self.session.add(record)
        self.session.flush()
        if application:
            self._apply(application, classification, deadline)
        append_audit(
            self.session,
            AuditInput(
                "mail",
                "email.ingested",
                "email",
                record.id,
                {
                    "classification": classification.kind,
                    "application_id": record.application_id,
                    "deadline": deadline.isoformat() if deadline else None,
                    "message_id": parsed.message_id or "",
                },
            ),
        )
        return record

"""Evidence-backed enrichment for the standalone, tracker-first ARGUS app.

This module owns only the separate enrichment cache.  It never touches the
legacy application database and never submits an employer application.  The
public API is intentionally small so the parent pipeline can collect first,
then call ``run`` and ``decorate``.
"""
from __future__ import annotations

import copy
import hashlib
import inspect
import ipaddress
import json
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

from .profile import load_profile, save_profile, validate_profile
from .public_web import PublicWebClient, PublicWebError, WebPageResult, parse_job_page


HEALTHY_TTL_SECONDS = 12 * 60 * 60
ERROR_TTL_SECONDS = 60 * 60
DEFAULT_MAX_BATCH = 60
_ALLOWED_AVAILABILITY = frozenset({"open", "closed", "unknown"})
_ALLOWED_MATCH_STATUS = frozenset({"potential", "review", "excluded"})
_PROGRAMMES = ("summer", "year_in_industry", "spring_week")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS enrichments (
    job_key TEXT PRIMARY KEY,
    job_url TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    checked_epoch REAL NOT NULL,
    cache_class TEXT NOT NULL,
    availability TEXT NOT NULL,
    verification_error TEXT NOT NULL DEFAULT '',
    response_hash TEXT,
    payload_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_queue (
    job_key TEXT PRIMARY KEY,
    job_url TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    queued_epoch REAL NOT NULL,
    next_due_epoch REAL NOT NULL,
    last_checked_epoch REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    last_status TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS enrichment_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS refresh_queue_due_idx
    ON refresh_queue(next_due_epoch, ordinal);
"""


class EnrichmentError(ValueError):
    """A job cannot be safely represented at the enrichment boundary."""


def _now() -> tuple[str, float]:
    current = datetime.now(timezone.utc)
    return current.isoformat(), current.timestamp()


def _text(value: object, limit: int = 6000) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "")[:limit].strip()


def _safe_url(url: object) -> str:
    if not isinstance(url, str) or not url or any(ord(char) < 32 for char in url):
        raise EnrichmentError("job URL is not printable text")
    try:
        parsed = urlsplit(url)
        hostname = (parsed.hostname or "").rstrip(".").casefold()
        _ = parsed.port
    except ValueError as exc:
        raise EnrichmentError("job URL is malformed") from exc
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc or not hostname:
        raise EnrichmentError("job URL must be absolute http(s)")
    if parsed.username is not None or parsed.password is not None:
        raise EnrichmentError("job URL must not contain credentials")
    if hostname in {"localhost", "local", "localdomain"} or hostname.endswith(
        (".localhost", ".internal", ".lan", ".home")
    ):
        raise EnrichmentError("job URL targets a special-use host")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise EnrichmentError("job URL targets a private or special-use address")
    return url


def _job_key(job: Mapping[str, object]) -> str:
    identifier = _text(job.get("id"), 240)
    url = _text(job.get("url"), 2000)
    if identifier:
        return "id:" + identifier
    if url:
        return "url:" + hashlib.sha256(url.encode("utf-8")).hexdigest()
    raise EnrichmentError("job needs an id or URL")


def _programme(job: Mapping[str, object]) -> str:
    direct = _text(job.get("programme"), 100).casefold().replace("-", "_").replace(" ", "_")
    aliases = {
        "summer_internship": "summer",
        "summer_placement": "summer",
        "off_cycle": "summer",
        "off_cycle_internship": "summer",
        "spring_insight": "spring_week",
        "spring_week": "spring_week",
        "insight_week": "spring_week",
        "industrial_placement": "year_in_industry",
        "placement_year": "year_in_industry",
        "placement": "year_in_industry",
        "year_in_industry": "year_in_industry",
    }
    if direct in _PROGRAMMES:
        return direct
    if direct in aliases:
        return aliases[direct]
    title = _text(job.get("title"), 300).casefold()
    if re.search(r"spring\s+(?:week|insight|programme|program)", title):
        return "spring_week"
    if re.search(r"industrial\s+placement|year\s+in\s+industry|placement\s+(?:year|student)", title):
        return "year_in_industry"
    if re.search(r"summer|internship|intern|off[-\s]?cycle", title):
        return "summer"
    return ""


def _location_text(job: Mapping[str, object], facts: Mapping[str, object]) -> str:
    return " ".join(
        part
        for part in (
            _text(job.get("location"), 300),
            _text(facts.get("location"), 300),
        )
        if part
    )


def _criteria_text(job: Mapping[str, object], facts: Mapping[str, object]) -> str:
    return " ".join(
        part
        for part in (
            _text(job.get("title"), 500),
            _text(job.get("description"), 6000),
            _text(facts.get("title"), 500),
            _text(facts.get("role_text"), 6000),
        )
        if part
    )


def _append_unique(items: list[str], value: str) -> None:
    if value not in items:
        items.append(value)


def _match(job: Mapping[str, object], facts: Mapping[str, object], profile: Mapping[str, object]) -> dict[str, object]:
    """Only explicit mandatory contradictions exclude; unknowns remain review."""
    from bs4 import BeautifulSoup
    reasons: list[str] = []
    unknowns: list[str] = []
    exclusion: list[str] = []
    programme = _programme(job)
    desired_roles = set(profile["desired_roles"])
    if programme not in desired_roles:
        (exclusion if programme else unknowns).append("programme_not_selected" if programme else "programme_unknown")
    location = _text(job.get("location"), 300) or _text(facts.get("location"), 300)
    location_lower = location.casefold()
    if any(marker in location_lower for marker in ('uk', 'united kingdom', 'london', 'england', 'scotland', 'wales', 'manchester', 'bristol', 'edinburgh', 'birmingham', 'leeds')):
        reasons.append("UK_location")
    elif any(marker in location_lower for marker in ('united states', 'usa', 'new york', 'singapore', 'hong kong')):
        exclusion.append("non_UK_location")
    else:
        unknowns.append("location")
    if facts.get("availability") == "closed":
        exclusion.append("role_closed")
    elif facts.get("availability") != "open":
        unknowns.append("availability_unverified")
    if _text(facts.get("verification_error"), 300):
        unknowns.append("verification_incomplete")

    # Programme titles name the recruiting cycle, not a graduation cutoff.
    raw = _text(facts.get("role_text"), 12000) or _text(job.get("description"), 12000)
    text = BeautifulSoup(raw, "html.parser").get_text(" ", strip=True) if "<" in raw else raw
    clauses = re.split(r"(?<=[.;!?])\s+|\n+", text)
    degree = _text(profile.get("degree")).casefold()
    acceptable_degree = re.compile(r"\b(?:finance|economics|accounting|business|any\s+(?:degree|discipline|subject))\b", re.I)
    optional = re.compile(r"\b(?:preferred|desirable|advantage|not required|optional)\b", re.I)
    masters_pattern = re.compile(r"\b(?:master(?:'s|s)?|msc|phd|postgraduate|mba)\b", re.I)
    stem_pattern = re.compile(r"\b(?:stem|computer science|engineering|mathematics|physics)\b", re.I)
    for clause in clauses:
        if masters_pattern.search(clause) and re.search(r"\b(?:required|must|only|essential)\b", clause, re.I):
            alternative = re.search(r"\bbachelor", clause, re.I) and re.search(r"\b(?:or|accepted|also)\b", clause, re.I)
            if not optional.search(clause) and not alternative:
                exclusion.append("requires_masters")
        if re.search(r"\b(?:degree|background|qualification)\b", clause, re.I) and not optional.search(clause):
            mandatory_stem = bool(stem_pattern.search(clause)) and bool(re.search(r"\b(?:required|must|only|essential)\b", clause, re.I))
            if mandatory_stem and not acceptable_degree.search(clause) and not re.search(r"\bany\b", clause, re.I):
                if not stem_pattern.search(degree):
                    exclusion.append("requires_stem_degree")
    if "finance" in degree and acceptable_degree.search(text):
        reasons.append("finance_degree_relevant")
    elif not re.search(r"\bany\s+(?:degree|discipline|subject)\b", text, re.I):
        unknowns.append("degree_requirements_not_confirmed")

    allowed_years: set[int] = set()
    for clause in clauses:
        marker = re.search(r"\b(?:graduat(?:e|es|ing|ion)|class of)\b", clause, re.I)
        if not marker:
            continue
        eligibility = clause[marker.end():]
        eligibility = re.split(r"\b(?:applications?|deadline|starts?|programme|program)\b", eligibility, maxsplit=1, flags=re.I)[0]
        years = {int(value) for value in re.findall(r"\b20\d{2}\b", eligibility)}
        for first, last in re.findall(r"\b(20\d{2})\s*[-–—]\s*(20\d{2})\b", eligibility):
            low, high = int(first), int(last)
            if 0 <= high - low <= 6:
                years.update(range(low, high + 1))
        allowed_years.update(years)
    target_year = profile["graduation_years"].get(programme)
    if target_year and allowed_years:
        if target_year not in allowed_years:
            exclusion.append("graduation_year_mismatch")
        else:
            reasons.append("graduation_year_matches_profile")
    else:
        unknowns.append("graduation_year_not_confirmed")

    # The presence of an employer requirement does not prove the applicant meets it.
    # This profile deliberately contains no asserted grades or immigration status.
    unknowns.extend(["sponsorship", "grades"])
    if not text:
        unknowns.append("requirements_not_captured")
    reasons.extend(exclusion)
    return {"match_status": "excluded" if exclusion else "review" if unknowns else "potential",
            "match_reasons": list(dict.fromkeys(reasons)),
            "match_unknowns": list(dict.fromkeys(unknowns))}


def _evidence(value: object, source_url: str, observed_at: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    if not isinstance(value, list):
        return result
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, Mapping):
            continue
        quote = _text(item.get("quote"), 500)
        if not quote:
            continue
        url = _text(item.get("source_url"), 2000) or source_url
        observed = _text(item.get("observed_at"), 80) or observed_at
        key = (quote, url)
        if key in seen:
            continue
        seen.add(key)
        result.append({"quote": quote, "source_url": url, "observed_at": observed})
    return result


class EnrichmentService:
    """Bounded, durable verification and profile matching service."""

    def __init__(
        self,
        data_dir: Path,
        profile: dict | None = None,
        fetcher: Callable[..., object] | object | None = None,
        max_workers: int = 4,
        *,
        max_batch: int | None = None,
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "enrichment.sqlite3"
        self.max_workers = max_workers
        self.max_batch = self._max_batch(max_batch)
        self._default_client = PublicWebClient()
        self._fetcher = fetcher if fetcher is not None else self._default_client
        self._using_default_fetcher = fetcher is None or fetcher is self._default_client
        self._profile = validate_profile(profile) if profile is not None else load_profile(self.data_dir)
        self._initialise_db()

    @staticmethod
    def _max_batch(value: int | None) -> int:
        raw: object = value
        if raw is None:
            raw = os.environ.get("ARGUS_TRACKER_MAX_BATCH", str(DEFAULT_MAX_BATCH))
        try:
            parsed = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_batch must be a positive integer") from exc
        if parsed < 1 or parsed > 10_000:
            raise ValueError("max_batch must be between 1 and 10000")
        return parsed

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _initialise_db(self) -> None:
        with self._connect() as connection:
            connection.executescript(_SCHEMA)

    def get_profile(self) -> dict[str, object]:
        return copy.deepcopy(self._profile)

    def set_profile(self, profile: dict) -> dict[str, object]:
        validated = validate_profile(profile)
        save_profile(self.data_dir, validated)
        self._profile = validated
        return self.get_profile()

    @staticmethod
    def _base_facts(job: Mapping[str, object]) -> dict[str, object]:
        return {
            "availability": "unknown",
            "verification_error": "not yet verified",
            "deadline": job.get("deadline"),
            "deadline_text": _text(job.get("deadline_text"), 300),
            "deadline_basis": "source_listing" if job.get("deadline") else "unknown",
            "posted_at": job.get("posted_at"),
            "posted_text": _text(job.get("posted_text"), 300),
            "evidence": [],
            "source_url": _text(job.get("url"), 2000),
            "final_url": _text(job.get("url"), 2000),
            "observed_at": None,
            "source_response_hash": None,
            "status_code": None,
            "title": _text(job.get("title"), 500),
            "employer": _text(job.get("employer"), 300),
            "location": _text(job.get("location"), 300),
            "role_text": "",
        }

    def _queue_jobs(self, jobs: list[Mapping[str, object]], now_epoch: float) -> list[tuple[str, Mapping[str, object]]]:
        queued: list[tuple[str, Mapping[str, object]]] = []
        with self._connect() as connection:
            for job in jobs:
                key = _job_key(job)
                raw_url = _text(job.get("url"), 2000)
                try:
                    url = _safe_url(raw_url)
                except EnrichmentError:
                    # Keep a malformed/private URL in the durable queue so the
                    # run records an unknown verification rather than ever
                    # handing it to a network client.
                    url = raw_url
                row = connection.execute(
                    "SELECT job_url, ordinal FROM refresh_queue WHERE job_key = ?", (key,)
                ).fetchone()
                if row is None:
                    meta = connection.execute(
                        "SELECT value FROM enrichment_meta WHERE key = 'next_ordinal'"
                    ).fetchone()
                    ordinal = int(meta["value"]) if meta else 0
                    connection.execute(
                        """INSERT INTO refresh_queue
                           (job_key, job_url, ordinal, queued_epoch, next_due_epoch)
                           VALUES (?, ?, ?, ?, 0)""",
                        (key, url, ordinal, now_epoch),
                    )
                    connection.execute(
                        """INSERT INTO enrichment_meta(key, value) VALUES('next_ordinal', ?)
                           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
                        (str(ordinal + 1),),
                    )
                else:
                    if row["job_url"] != url:
                        connection.execute(
                            """UPDATE refresh_queue SET job_url = ?, next_due_epoch = 0,
                               last_status = '' WHERE job_key = ?""",
                            (url, key),
                        )
                    else:
                        connection.execute(
                            "UPDATE refresh_queue SET job_url = ? WHERE job_key = ?", (url, key)
                        )
                queued.append((key, job))
        return queued

    def _due_jobs(
        self,
        queued: list[tuple[str, Mapping[str, object]]],
        now_epoch: float,
    ) -> list[tuple[str, Mapping[str, object]]]:
        if not queued:
            return []
        keys = {key for key, _ in queued}
        placeholders = ",".join("?" for _ in keys)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT job_key FROM refresh_queue WHERE next_due_epoch <= ? AND job_key IN ({placeholders})"
                " ORDER BY ordinal",
                (now_epoch, *keys),
            ).fetchall()
        by_key = {key: job for key, job in queued}
        return [(row["job_key"], by_key[row["job_key"]]) for row in rows[: self.max_batch]]

    def _call_fetcher(self, job: Mapping[str, object], observed_at: str) -> object:
        url = _safe_url(job.get("url"))
        title = _text(job.get("title"), 500)
        employer = _text(job.get("employer"), 300)
        target = getattr(self._fetcher, "fetch", self._fetcher)
        if not callable(target):
            raise EnrichmentError("fetcher is not callable")
        if self._using_default_fetcher:
            return target(url, expected_title=title, expected_employer=employer, observed_at=observed_at)
        try:
            signature = inspect.signature(target)
        except (TypeError, ValueError):
            signature = None
        kwargs: dict[str, object] = {}
        if signature is None or any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in signature.parameters.values()
        ):
            kwargs = {"expected_title": title, "expected_employer": employer, "observed_at": observed_at}
        elif "expected_title" in signature.parameters:
            kwargs["expected_title"] = title
            if "expected_employer" in signature.parameters:
                kwargs["expected_employer"] = employer
            if "observed_at" in signature.parameters:
                kwargs["observed_at"] = observed_at
        return target(url, **kwargs)

    def _parse_fetch_result(
        self,
        raw: object,
        job: Mapping[str, object],
        observed_at: str,
    ) -> dict[str, object]:
        source_url = _text(job.get("url"), 2000)
        if isinstance(raw, WebPageResult):
            result = raw
            facts = result.as_dict()
            if result.body is not None and not facts.get("source_response_hash"):
                facts["source_response_hash"] = hashlib.sha256(result.body).hexdigest()
        elif isinstance(raw, (str, bytes)):
            result = parse_job_page(
                raw,
                source_url,
                expected_title=_text(job.get("title"), 500),
                expected_employer=_text(job.get("employer"), 300),
                observed_at=observed_at,
            )
            facts = result.as_dict()
        elif isinstance(raw, Mapping):
            value = dict(raw)
            body = value.get("body", value.get("html"))
            if body is not None:
                result = parse_job_page(
                    body,
                    source_url,
                    expected_title=_text(job.get("title"), 500),
                    expected_employer=_text(job.get("employer"), 300),
                    observed_at=observed_at,
                    final_url=_text(value.get("final_url"), 2000) or source_url,
                    status_code=int(value.get("status_code", 200)),
                    response_hash=_text(value.get("source_response_hash"), 100) or None,
                )
                facts = result.as_dict()
            else:
                facts = value
        else:
            raise EnrichmentError(f"fetcher returned unsupported {type(raw).__name__}")

        availability = _text(facts.get("availability"), 20).casefold()
        status_code = facts.get("status_code")
        try:
            status_number = int(status_code) if status_code is not None else None
        except (TypeError, ValueError):
            status_number = None
        if status_number in {404, 410}:
            availability = "closed"
        if availability not in _ALLOWED_AVAILABILITY:
            availability = "unknown"
        error = _text(facts.get("verification_error"), 500)
        if not error and availability == "unknown":
            error = "role evidence was insufficient"
        response_hash = _text(facts.get("source_response_hash"), 128) or None
        body = facts.get("body")
        if body is not None:
            raw_body = body if isinstance(body, bytes) else str(body).encode("utf-8", errors="replace")
            response_hash = hashlib.sha256(raw_body).hexdigest()
        source = _text(facts.get("source_url"), 2000) or source_url
        observed = _text(facts.get("observed_at"), 80) or observed_at
        evidence = _evidence(facts.get("evidence"), source, observed)
        if availability == "open" and not evidence:
            availability = "unknown"
            error = error or "open result lacked role evidence"
        deadline = facts.get("deadline")
        if deadline is not None:
            deadline = _text(deadline, 40) or None
        posted_at = facts.get("posted_at")
        if posted_at is not None:
            posted_at = _text(posted_at, 80) or None
        return {
            "availability": availability,
            "verification_error": error,
            "deadline": deadline,
            "deadline_text": _text(facts.get("deadline_text"), 300),
            "deadline_basis": _text(facts.get("deadline_basis"), 80) or "unknown",
            "posted_at": posted_at,
            "posted_text": _text(facts.get("posted_text"), 300),
            "evidence": evidence,
            "source_url": source,
            "final_url": _text(facts.get("final_url"), 2000) or source,
            "observed_at": observed,
            "source_response_hash": response_hash,
            "status_code": status_number,
            "title": _text(facts.get("title"), 500),
            "employer": _text(facts.get("employer"), 300),
            "location": _text(facts.get("location"), 300),
            "role_text": _text(facts.get("role_text"), 6000),
        }

    @staticmethod
    def _failed_facts(job: Mapping[str, object], observed_at: str, error: str) -> dict[str, object]:
        facts = EnrichmentService._base_facts(job)
        facts.update(
            {
                "verification_error": _text(error, 500) or "verification failed",
                "observed_at": observed_at,
                "source_url": _text(job.get("url"), 2000),
                "final_url": _text(job.get("url"), 2000),
            }
        )
        return facts

    def _fetch_one(self, key: str, job: Mapping[str, object]) -> tuple[str, dict[str, object], str]:
        observed_at, _ = _now()
        try:
            raw = self._call_fetcher(job, observed_at)
            facts = self._parse_fetch_result(raw, job, observed_at)
            healthy = facts["availability"] == "closed" or (
                facts["availability"] == "open" and not facts["verification_error"]
            )
            return key, facts, "healthy" if healthy else "error"
        except (PublicWebError, EnrichmentError, OSError, TimeoutError) as exc:
            return key, self._failed_facts(job, observed_at, f"{type(exc).__name__}: {exc}"), "error"
        except Exception as exc:  # one broken public page must not sink the batch
            return key, self._failed_facts(job, observed_at, f"{type(exc).__name__}: {exc}"), "error"

    def _save_facts(self, key: str, job: Mapping[str, object], facts: dict[str, object], cache_class: str) -> None:
        checked_at = _text(facts.get("observed_at"), 80) or _now()[0]
        try:
            checked_epoch = datetime.fromisoformat(checked_at.replace("Z", "+00:00")).timestamp()
        except ValueError:
            checked_at, checked_epoch = _now()
        payload = {field: facts.get(field) for field in (
            "availability",
            "verification_error",
            "deadline",
            "deadline_text",
            "deadline_basis",
            "posted_at",
            "posted_text",
            "evidence",
            "source_url",
            "final_url",
            "observed_at",
            "status_code",
            "title",
            "employer",
            "location",
            "role_text",
        )}
        payload["source_response_hash"] = facts.get("source_response_hash")
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO enrichments
                   (job_key, job_url, checked_at, checked_epoch, cache_class,
                    availability, verification_error, response_hash, payload_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(job_key) DO UPDATE SET
                    job_url=excluded.job_url, checked_at=excluded.checked_at,
                    checked_epoch=excluded.checked_epoch, cache_class=excluded.cache_class,
                    availability=excluded.availability, verification_error=excluded.verification_error,
                    response_hash=excluded.response_hash, payload_json=excluded.payload_json""",
                (
                    key,
                    _text(job.get("url"), 2000),
                    checked_at,
                    checked_epoch,
                    cache_class,
                    _text(facts.get("availability"), 20),
                    _text(facts.get("verification_error"), 500),
                    facts.get("source_response_hash"),
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                ),
            )
            ttl = HEALTHY_TTL_SECONDS if cache_class == "healthy" else ERROR_TTL_SECONDS
            connection.execute(
                """UPDATE refresh_queue SET next_due_epoch = ?, last_checked_epoch = ?,
                   attempts = attempts + 1, last_status = ? WHERE job_key = ?""",
                (
                    checked_epoch + ttl,
                    checked_epoch,
                    _text(facts.get("availability"), 20),
                    key,
                ),
            )

    def run(self, jobs: list[dict]) -> dict:
        """Verify a fair, bounded due batch and persist only safe facts/hashes."""

        if not isinstance(jobs, list):
            raise ValueError("jobs must be a list")
        now_text, now_epoch = _now()
        normalised_jobs: list[Mapping[str, object]] = []
        for job in jobs:
            if not isinstance(job, Mapping):
                raise ValueError("each job must be an object")
            normalised_jobs.append(job)
        queued = self._queue_jobs(normalised_jobs, now_epoch)
        selected = self._due_jobs(queued, now_epoch)
        results: list[tuple[str, dict[str, object], str]] = []
        if selected:
            workers = min(self.max_workers, len(selected))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="argus-enrichment") as pool:
                futures = [pool.submit(self._fetch_one, key, job) for key, job in selected]
                results = [future.result() for future in futures]
            job_by_key = {key: job for key, job in selected}
            for key, facts, cache_class in results:
                self._save_facts(key, job_by_key[key], facts, cache_class)
        remaining_count = self.summary()["queued"]
        errors = sum(1 for _, _, cache_class in results if cache_class == "error")
        availability = {"open": 0, "closed": 0, "unknown": 0}
        for _, facts, _ in results:
            state = facts.get("availability")
            if state in availability:
                availability[state] += 1
        return {
            "checked": len(results),
            "queued": remaining_count,
            "errors": errors,
            "open": availability["open"],
            "closed": availability["closed"],
            "unknown": availability["unknown"],
            "started_at": now_text,
        }

    def _load_facts(self, key: str) -> dict[str, object] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM enrichments WHERE job_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row["payload_json"])
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def decorate(self, jobs: list[dict]) -> list[dict]:
        """Attach the fixed enrichment contract without exposing response bodies."""

        if not isinstance(jobs, list):
            raise ValueError("jobs must be a list")
        decorated: list[dict] = []
        for raw_job in jobs:
            if not isinstance(raw_job, Mapping):
                raise ValueError("each job must be an object")
            job = dict(raw_job)
            try:
                key = _job_key(job)
            except EnrichmentError:
                key = ""
            facts = self._load_facts(key) if key else None
            base = self._base_facts(job)
            if facts and facts.get("source_url") == _text(job.get("url"), 2000):
                base.update(facts)
            availability = _text(base.get("availability"), 20).casefold()
            if availability not in _ALLOWED_AVAILABILITY:
                availability = "unknown"
                base["verification_error"] = "cached availability was invalid"
            base["availability"] = availability
            match = _match(job, base, self._profile)
            job.update(
                {
                    "availability": availability,
                    "verified_at": base.get("observed_at"),
                    "verification_error": base.get("verification_error", ""),
                    "deadline": base.get("deadline") if base.get("deadline") is not None else job.get("deadline"),
                    "deadline_text": _text(base.get("deadline_text"), 300) or _text(job.get("deadline_text"), 300),
                    "deadline_basis": ("source_listing" if base.get("deadline") is None and job.get("deadline")
                                       else base.get("deadline_basis", "unknown")),
                    "posted_at": base.get("posted_at") if base.get("posted_at") is not None else job.get("posted_at"),
                    "posted_text": _text(job.get("posted_text"), 300) or _text(base.get("posted_text"), 300),
                    "match_status": match["match_status"],
                    "match_reasons": list(match["match_reasons"]),
                    "match_unknowns": list(match["match_unknowns"]),
                    "evidence": list(base.get("evidence") or []),
                    "source_response_hash": base.get("source_response_hash"),
                }
            )
            # The role text is useful during matching but is deliberately not a
            # decorated/API field; evidence quotes and the response hash suffice.
            job.pop("html", None)
            job.pop("body", None)
            decorated.append(job)
        return decorated

    def summary(self) -> dict:
        now_epoch = time.time()
        with self._connect() as connection:
            counts = connection.execute(
                "SELECT availability, COUNT(*) AS count FROM enrichments GROUP BY availability"
            ).fetchall()
            queued = connection.execute(
                "SELECT COUNT(*) AS count FROM refresh_queue WHERE next_due_epoch <= ?", (now_epoch,)
            ).fetchone()["count"]
            total = connection.execute("SELECT COUNT(*) AS count FROM enrichments").fetchone()["count"]
            last = connection.execute("SELECT MAX(checked_epoch) AS checked FROM enrichments").fetchone()["checked"]
        result = {"total": total, "open": 0, "closed": 0, "unknown": 0, "queued": queued, "last_checked_epoch": last}
        for row in counts:
            if row["availability"] in _ALLOWED_AVAILABILITY:
                result[row["availability"]] = row["count"]
        return result


__all__ = [
    "DEFAULT_MAX_BATCH",
    "EnrichmentError",
    "EnrichmentService",
    "ERROR_TTL_SECONDS",
    "HEALTHY_TTL_SECONDS",
]

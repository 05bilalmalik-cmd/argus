"""Persistent, source-only storage for the standalone ARGUS tracker."""
from __future__ import annotations

import ipaddress
import json
import math
import re
import sqlite3
import unicodedata
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .contracts import Listing, SourceResult

_ALLOWED_STAGES = {
    "not_applied",
    "applied",
    "assessment",
    "interview",
    "offer",
    "rejected",
    "withdrawn",
}
_ALLOWED_SOURCE_STATUSES = {"ok", "empty", "partial", "error"}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    employer TEXT NOT NULL,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    location TEXT NOT NULL DEFAULT '',
    programme TEXT NOT NULL DEFAULT '',
    posted_at TEXT,
    deadline TEXT,
    first_seen TEXT NOT NULL,
    first_seen_epoch REAL NOT NULL,
    last_seen TEXT NOT NULL,
    last_seen_epoch REAL NOT NULL,
    metadata_seen_at_epoch REAL NOT NULL,
    saved INTEGER NOT NULL DEFAULT 0,
    stage TEXT NOT NULL DEFAULT 'not_applied',
    notes TEXT NOT NULL DEFAULT '',
    due_date TEXT
);

CREATE TABLE IF NOT EXISTS job_identities (
    identity_key TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id)
);

CREATE TABLE IF NOT EXISTS source_aliases (
    source_name TEXT NOT NULL,
    source_id TEXT NOT NULL,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    PRIMARY KEY (source_name, source_id)
);

CREATE TABLE IF NOT EXISTS job_sources (
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    source_name TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    first_seen_epoch REAL NOT NULL,
    last_seen TEXT NOT NULL,
    last_seen_epoch REAL NOT NULL,
    PRIMARY KEY (job_id, source_name)
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_id TEXT NOT NULL,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    observed_at TEXT NOT NULL,
    observed_epoch REAL NOT NULL,
    employer TEXT NOT NULL,
    title TEXT NOT NULL,
    listing_url TEXT NOT NULL,
    location TEXT NOT NULL,
    programme TEXT NOT NULL,
    posted_at TEXT,
    deadline TEXT,
    identity_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL,
    checked_at_epoch REAL NOT NULL,
    row_count INTEGER NOT NULL,
    elapsed_seconds REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS sources (
    name TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    checked_at TEXT NOT NULL,
    checked_at_epoch REAL NOT NULL,
    last_success_at TEXT,
    last_success_epoch REAL,
    row_count INTEGER NOT NULL,
    elapsed_seconds REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS listing_metadata (
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    source_name TEXT NOT NULL,
    source_url TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    observed_epoch REAL NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    deadline_text TEXT NOT NULL DEFAULT '',
    posted_text TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (job_id, source_name)
);

CREATE INDEX IF NOT EXISTS observations_job_idx ON observations(job_id);
CREATE INDEX IF NOT EXISTS observations_source_idx ON observations(source_name, observed_epoch);
CREATE INDEX IF NOT EXISTS source_runs_name_idx ON source_runs(source_name, checked_at_epoch);
"""


class _InvalidListing(ValueError):
    """A source result contains a malformed listing and must fail as a unit."""


def _text(value: object, field: str, *, required: bool = False) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ValueError(f"{field} must be text")
    if required and not value.strip():
        raise ValueError(f"{field} is required")
    return value


def _utc_datetime(value: object, field: str) -> datetime:
    value = _text(value, field, required=True)
    candidate = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc_iso(parsed: datetime) -> str:
    return parsed.astimezone(timezone.utc).isoformat()


def _date_text(value: object, field: str) -> str:
    value = _text(value, field, required=True)
    if not _DATE_RE.fullmatch(value):
        raise ValueError(f"{field} must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{field} must be YYYY-MM-DD") from exc
    return value


def _validated_url(value: object, field: str) -> str:
    value = _text(value, field, required=True)
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"{field} contains control characters")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{field} is not a valid URL") from exc
    if scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise ValueError(f"{field} must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{field} must not contain credentials")
    host = hostname.rstrip(".").casefold()
    if host == "localhost" or host.endswith(".localhost"):
        raise ValueError(f"{field} must not target localhost")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_unspecified
        or address.is_multicast
    ):
        raise ValueError(f"{field} must not target a local or private address")
    if re.fullmatch(r"[0-9.]+", host) or re.fullmatch(r"[0-9a-f:]+", host):
        raise ValueError(f"{field} must not target a numeric address")
    if port is not None and not 0 < port < 65536:
        raise ValueError(f"{field} has an invalid port")
    return value


def _canonical_url(value: str) -> str:
    parsed = urlsplit(value)
    host = (parsed.hostname or "").casefold().rstrip(".")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError:
        pass
    port = parsed.port
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    if port is not None and not ((parsed.scheme.lower() == "http" and port == 80) or (parsed.scheme.lower() == "https" and port == 443)):
        host = f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    # Keep the query verbatim: job identity parameters must not disappear.
    return urlunsplit((parsed.scheme.lower(), host, path, parsed.query, ""))


def _normalised(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _ats_identity(url: str) -> str | None:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").casefold().rstrip(".")
    segments = [part for part in parsed.path.split("/") if part]
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io"}:
        lowered = [part.casefold() for part in segments]
        if "jobs" in lowered:
            index = lowered.index("jobs")
            if index and index + 1 < len(segments) and segments[index + 1]:
                tenant = segments[index - 1].casefold()
                requisition = segments[index + 1].casefold()
                return f"ats:greenhouse:{tenant}:{requisition}"
        query = parse_qs(parsed.query, keep_blank_values=False)
        tenant = query.get("for", [""])[0].strip().casefold()
        requisition = (query.get("gh_jid") or query.get("token") or [""])[0].strip().casefold()
        if tenant and requisition:
            return f"ats:greenhouse:{tenant}:{requisition}"
    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and len(segments) >= 2:
        return f"ats:lever:{segments[0].casefold()}:{segments[1].casefold()}"
    if host == "jobs.ashbyhq.com" and len(segments) >= 2:
        return f"ats:ashby:{segments[0].casefold()}:{segments[1].casefold()}"
    if host in {"apply.workable.com", "jobs.workable.com"}:
        lowered = [part.casefold() for part in segments]
        if "j" in lowered:
            index = lowered.index("j")
            if index and index + 1 < len(segments):
                return f"ats:workable:{segments[index - 1].casefold()}:{segments[index + 1].casefold()}"
    if host == "jobs.smartrecruiters.com" and len(segments) >= 2:
        return f"ats:smartrecruiters:{segments[0].casefold()}:{segments[1].casefold()}"
    return None


def _generic_url(url: str) -> bool:
    parsed = urlsplit(url)
    segments = [part.casefold() for part in parsed.path.split("/") if part]
    query_keys = {key.casefold() for key in parse_qs(parsed.query, keep_blank_values=True)}
    if query_keys & {"q", "query", "search", "keyword", "keywords", "page", "offset", "sort", "location"}:
        return True
    if not segments:
        return True
    generic = {
        "careers",
        "jobs",
        "job-listings",
        "listing",
        "listings",
        "opportunities",
        "positions",
        "results",
        "roles",
        "search",
        "search-results",
        "vacancies",
    }
    if segments[-1] in generic or "search" in segments:
        return True
    # Absence from a finite list does not establish a direct role URL. Require
    # a recognisable detail route before allowing URL-only cross-source merges.
    # Unknown landing pages retain title/location identity instead.
    detail_routes = {"job", "jobs", "role", "roles", "posting", "postings", "o", "j"}
    return not any(part in detail_routes for part in segments[:-1])


def _identity_keys(listing: Listing) -> list[str]:
    canonical = _canonical_url(listing.url)
    keys: list[str] = []
    ats = _ats_identity(canonical)
    if ats:
        keys.append(ats)
    fallback = [canonical, _normalised(listing.title), _normalised(listing.location)]
    role_key = "url-role:" + json.dumps(fallback, ensure_ascii=False, separators=(",", ":"))
    keys.append(role_key)
    if not _generic_url(canonical):
        # A direct job URL is a safe cross-source fallback after the
        # role-aware key; generic listing/search URLs deliberately have no
        # URL-only key so different roles cannot collapse.
        keys.append("url:" + canonical)
    return list(dict.fromkeys(keys))


def _validate_listing(listing: object, index: int) -> Listing:
    if not isinstance(listing, Listing):
        raise _InvalidListing(f"listing {index} is not a Listing")
    _text(listing.employer, f"listing {index} employer", required=True)
    _text(listing.title, f"listing {index} title", required=True)
    _validated_url(listing.url, f"listing {index} url")
    for field in ("location", "programme", "source_id"):
        _text(getattr(listing, field), f"listing {index} {field}")
    for field in ("description", "deadline_text", "posted_text"):
        value = getattr(listing, field, "")
        _text(value, f"listing {index} {field}")
        if len(value) > 100000:
            raise ValueError(f"listing {index} {field} exceeds 100000 characters")
    if listing.posted_at is not None:
        _utc_datetime(listing.posted_at, f"listing {index} posted_at")
    if listing.deadline is not None:
        _date_text(listing.deadline, f"listing {index} deadline")
    return listing


class TrackerStore:
    """A small SQLite store with no dependency on the legacy application DB."""

    def __init__(self, path: str | Path):
        if str(path) == ":memory:":
            raise ValueError("TrackerStore needs a file path for per-operation connections")
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.path, timeout=30) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _prepare_result(self, result: object, index: int) -> dict:
        if not isinstance(result, SourceResult):
            raise ValueError(f"result {index} is not a SourceResult")
        name = _text(result.name, f"result {index} name", required=True).strip()
        url = _validated_url(result.url, f"result {index} url")
        if not isinstance(result.status, str) or result.status not in _ALLOWED_SOURCE_STATUSES:
            raise ValueError(f"invalid source status: {result.status!r}")
        checked = _utc_datetime(result.checked_at, f"result {index} checked_at")
        if isinstance(result.elapsed_seconds, bool):
            raise ValueError("elapsed_seconds must be a finite non-negative number")
        try:
            elapsed = float(result.elapsed_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError("elapsed_seconds must be a finite non-negative number") from exc
        if not math.isfinite(elapsed) or elapsed < 0:
            raise ValueError("elapsed_seconds must be a finite non-negative number")
        error = _text(result.error, f"result {index} error")
        try:
            listings = list(result.listings)
        except TypeError as exc:
            raise ValueError(f"result {index} listings must be iterable") from exc
        malformed: str | None = None
        prepared: list[Listing] = []
        if result.status in {"ok", "partial"}:
            for listing_index, listing in enumerate(listings):
                try:
                    prepared.append(_validate_listing(listing, listing_index))
                except ValueError as exc:
                    malformed = str(exc)
                    break
        elif result.status == "empty" and listings:
            malformed = "empty source result contained listings"
        return {
            "name": name,
            "url": url,
            "status": result.status,
            "error": error,
            "checked_at": _utc_iso(checked),
            "checked_epoch": checked.timestamp(),
            "elapsed": elapsed,
            "listings": prepared,
            "malformed": malformed,
        }

    def ingest(self, results: list[SourceResult]) -> dict:
        try:
            raw_results = list(results)
        except TypeError as exc:
            raise ValueError("results must be iterable") from exc
        prepared = [self._prepare_result(result, index) for index, result in enumerate(raw_results)]
        totals = {"new": 0, "updated": 0, "observed": 0, "sources": len(prepared), "errors": 0}
        connection = self._connect()
        try:
            with connection:
                for index, result in enumerate(prepared):
                    savepoint = f"source_{index}"
                    connection.execute(f"SAVEPOINT {savepoint}")
                    try:
                        new, updated, observed, errors = self._ingest_one(connection, result)
                        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                    except Exception:
                        connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                        connection.execute(f"RELEASE SAVEPOINT {savepoint}")
                        raise
                    totals["new"] += new
                    totals["updated"] += updated
                    totals["observed"] += observed
                    totals["errors"] += errors
        finally:
            connection.close()
        return totals

    def _ingest_one(self, connection: sqlite3.Connection, result: dict) -> tuple[int, int, int, int]:
        status = result["status"]
        error = result["error"]
        actual_status = status
        if result["malformed"]:
            actual_status = "error"
            error = f"invalid source result: {result['malformed']}"
        elif status == "error" and not error:
            error = "source reported an error"
        new = updated = observed = 0
        if actual_status in {"ok", "partial"}:
            for listing in result["listings"]:
                was_new = self._ingest_listing(connection, result, listing)
                observed += 1
                if was_new:
                    new += 1
                else:
                    updated += 1
        row_count = observed if actual_status in {"ok", "partial"} else 0
        connection.execute(
            """INSERT INTO source_runs
               (source_name, source_url, status, error, checked_at, checked_at_epoch,
                row_count, elapsed_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                result["name"],
                result["url"],
                actual_status,
                error,
                result["checked_at"],
                result["checked_epoch"],
                row_count,
                result["elapsed"],
            ),
        )
        self._update_source_health(connection, result, actual_status, error, row_count)
        return new, updated, observed, int(actual_status == "error")

    @staticmethod
    def _update_source_health(
        connection: sqlite3.Connection,
        result: dict,
        status: str,
        error: str,
        row_count: int,
    ) -> None:
        current = connection.execute(
            "SELECT * FROM sources WHERE name = ?", (result["name"],)
        ).fetchone()
        if current is not None and result["checked_epoch"] < current["checked_at_epoch"]:
            return
        last_success_at = current["last_success_at"] if current else None
        last_success_epoch = current["last_success_epoch"] if current else None
        if status in {"ok", "empty"}:
            last_success_at = result["checked_at"]
            last_success_epoch = result["checked_epoch"]
        connection.execute(
            """INSERT INTO sources
               (name, url, status, error, checked_at, checked_at_epoch,
                last_success_at, last_success_epoch, row_count, elapsed_seconds)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(name) DO UPDATE SET
                 url = excluded.url,
                 status = excluded.status,
                 error = excluded.error,
                 checked_at = excluded.checked_at,
                 checked_at_epoch = excluded.checked_at_epoch,
                 last_success_at = excluded.last_success_at,
                 last_success_epoch = excluded.last_success_epoch,
                 row_count = excluded.row_count,
                 elapsed_seconds = excluded.elapsed_seconds""",
            (
                result["name"],
                result["url"],
                status,
                error,
                result["checked_at"],
                result["checked_epoch"],
                last_success_at,
                last_success_epoch,
                row_count,
                result["elapsed"],
            ),
        )

    @staticmethod
    def _ingest_listing(connection: sqlite3.Connection, result: dict, listing: Listing) -> bool:
        source_name = result["name"]
        observed_at = result["checked_at"]
        observed_epoch = result["checked_epoch"]
        source_id = listing.source_id.strip()
        job_id = None
        if source_id:
            alias = connection.execute(
                "SELECT job_id FROM source_aliases WHERE source_name = ? AND source_id = ?",
                (source_name, source_id),
            ).fetchone()
            if alias is not None:
                job_id = alias["job_id"]
        keys = _identity_keys(listing)
        if job_id is None:
            for key in keys:
                identity = connection.execute(
                    "SELECT job_id FROM job_identities WHERE identity_key = ?", (key,)
                ).fetchone()
                if identity is not None:
                    job_id = identity["job_id"]
                    break
        is_new = job_id is None
        if is_new:
            cursor = connection.execute(
                """INSERT INTO jobs
                   (employer, title, url, location, programme, posted_at, deadline,
                    first_seen, first_seen_epoch, last_seen, last_seen_epoch,
                    metadata_seen_at_epoch)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    listing.employer,
                    listing.title,
                    listing.url,
                    listing.location,
                    listing.programme,
                    listing.posted_at,
                    listing.deadline,
                    observed_at,
                    observed_epoch,
                    observed_at,
                    observed_epoch,
                    observed_epoch,
                ),
            )
            job_id = cursor.lastrowid
        else:
            current = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if current is None:
                raise sqlite3.IntegrityError("identity points to a missing job")
            first_seen = current["first_seen"]
            first_epoch = current["first_seen_epoch"]
            last_seen = current["last_seen"]
            last_epoch = current["last_seen_epoch"]
            if observed_epoch < first_epoch:
                first_seen, first_epoch = observed_at, observed_epoch
            if observed_epoch > last_epoch:
                last_seen, last_epoch = observed_at, observed_epoch
            values = {
                "employer": current["employer"],
                "title": current["title"],
                "url": current["url"],
                "location": current["location"],
                "programme": current["programme"],
                "posted_at": current["posted_at"],
                "deadline": current["deadline"],
                "metadata_seen_at_epoch": current["metadata_seen_at_epoch"],
            }
            if observed_epoch >= current["metadata_seen_at_epoch"]:
                values.update(
                    employer=listing.employer,
                    title=listing.title,
                    url=listing.url,
                    metadata_seen_at_epoch=observed_epoch,
                )
                if listing.location:
                    values["location"] = listing.location
                if listing.programme:
                    values["programme"] = listing.programme
                if listing.posted_at is not None:
                    values["posted_at"] = listing.posted_at
                if listing.deadline is not None:
                    values["deadline"] = listing.deadline
            connection.execute(
                """UPDATE jobs SET employer = ?, title = ?, url = ?, location = ?,
                   programme = ?, posted_at = ?, deadline = ?, first_seen = ?,
                   first_seen_epoch = ?, last_seen = ?, last_seen_epoch = ?,
                   metadata_seen_at_epoch = ? WHERE id = ?""",
                (
                    values["employer"],
                    values["title"],
                    values["url"],
                    values["location"],
                    values["programme"],
                    values["posted_at"],
                    values["deadline"],
                    first_seen,
                    first_epoch,
                    last_seen,
                    last_epoch,
                    values["metadata_seen_at_epoch"],
                    job_id,
                ),
            )
        metadata = [getattr(listing, field, "") for field in ("description", "deadline_text", "posted_text")]
        if any(metadata):
            connection.execute(
                """INSERT INTO listing_metadata
                (job_id, source_name, source_url, observed_at, observed_epoch,
                 description, deadline_text, posted_text) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(job_id, source_name) DO UPDATE SET
                  source_url=excluded.source_url, observed_at=excluded.observed_at,
                  observed_epoch=excluded.observed_epoch,
                  description=excluded.description, deadline_text=excluded.deadline_text,
                  posted_text=excluded.posted_text
                WHERE excluded.observed_epoch >= listing_metadata.observed_epoch""",
                (job_id, source_name, result["url"], observed_at, observed_epoch, *metadata),
            )
        for key in keys:
            connection.execute(
                "INSERT OR IGNORE INTO job_identities(identity_key, job_id) VALUES (?, ?)",
                (key, job_id),
            )
        if source_id:
            connection.execute(
                "INSERT OR IGNORE INTO source_aliases(source_name, source_id, job_id) VALUES (?, ?, ?)",
                (source_name, source_id, job_id),
            )
        source_row = connection.execute(
            "SELECT * FROM job_sources WHERE job_id = ? AND source_name = ?",
            (job_id, source_name),
        ).fetchone()
        if source_row is None:
            connection.execute(
                """INSERT INTO job_sources
                   (job_id, source_name, first_seen, first_seen_epoch, last_seen, last_seen_epoch)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (job_id, source_name, observed_at, observed_epoch, observed_at, observed_epoch),
            )
        else:
            first_seen = source_row["first_seen"]
            first_epoch = source_row["first_seen_epoch"]
            last_seen = source_row["last_seen"]
            last_epoch = source_row["last_seen_epoch"]
            if observed_epoch < first_epoch:
                first_seen, first_epoch = observed_at, observed_epoch
            if observed_epoch > last_epoch:
                last_seen, last_epoch = observed_at, observed_epoch
            connection.execute(
                """UPDATE job_sources SET first_seen = ?, first_seen_epoch = ?,
                   last_seen = ?, last_seen_epoch = ?
                   WHERE job_id = ? AND source_name = ?""",
                (first_seen, first_epoch, last_seen, last_epoch, job_id, source_name),
            )
        connection.execute(
            """INSERT INTO observations
               (source_name, source_url, source_id, job_id, observed_at, observed_epoch,
                employer, title, listing_url, location, programme, posted_at, deadline,
                identity_key)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                source_name,
                result["url"],
                listing.source_id,
                job_id,
                observed_at,
                observed_epoch,
                listing.employer,
                listing.title,
                listing.url,
                listing.location,
                listing.programme,
                listing.posted_at,
                listing.deadline,
                keys[0],
            ),
        )
        return is_new

    @staticmethod
    def _job_dict(connection: sqlite3.Connection, row: sqlite3.Row) -> dict:
        sources = connection.execute(
            "SELECT source_name FROM job_sources WHERE job_id = ? ORDER BY source_name",
            (row["id"],),
        ).fetchall()
        metadata = connection.execute(
            """SELECT * FROM listing_metadata WHERE job_id = ?
            ORDER BY (source_name IN ('simplytk', 'trackr')) ASC, observed_epoch DESC""",
            (row["id"],),
        ).fetchall()
        evidence = []
        extra = {"description": "", "deadline_text": "", "posted_text": "", "metadata_source_url": ""}
        for field in ("description", "deadline_text", "posted_text"):
            item = next((item for item in metadata if item[field]), None)
            if item is not None:
                extra[field] = item[field]
                if not extra["metadata_source_url"]:
                    extra["metadata_source_url"] = item["source_url"]
                evidence.append({"field": field, "source_name": item["source_name"],
                                 "source_url": item["source_url"], "observed_at": item["observed_at"]})
        return {
            **extra,
            "metadata_evidence": evidence,
            "id": row["id"],
            "employer": row["employer"],
            "title": row["title"],
            "url": row["url"],
            "location": row["location"],
            "programme": row["programme"],
            "posted_at": row["posted_at"],
            "deadline": row["deadline"],
            "first_seen": row["first_seen"],
            "last_seen": row["last_seen"],
            "saved": bool(row["saved"]),
            "stage": row["stage"],
            "notes": row["notes"],
            "due_date": row["due_date"],
            "sources": [source["source_name"] for source in sources],
        }

    def list_jobs(self) -> list[dict]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT * FROM jobs ORDER BY id").fetchall()
            return [self._job_dict(connection, row) for row in rows]
        finally:
            connection.close()

    def update_job(
        self,
        id: int,
        *,
        saved: bool | None = None,
        stage: str | None = None,
        notes: str | None = None,
        due_date: str | None = None,
    ) -> dict:
        if saved is not None and not isinstance(saved, bool):
            raise ValueError("saved must be a boolean")
        if stage is not None and stage not in _ALLOWED_STAGES:
            raise ValueError(f"invalid stage: {stage!r}")
        if notes is not None:
            _text(notes, "notes")
        if due_date is not None and due_date != "":
            _date_text(due_date, "due_date")
        connection = self._connect()
        try:
            with connection:
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (id,)).fetchone()
                if row is None:
                    raise KeyError(id)
                updates: list[str] = []
                values: list[object] = []
                if saved is not None:
                    updates.append("saved = ?")
                    values.append(int(saved))
                if stage is not None:
                    updates.append("stage = ?")
                    values.append(stage)
                if notes is not None:
                    updates.append("notes = ?")
                    values.append(notes)
                if due_date is not None:
                    updates.append("due_date = ?")
                    values.append(due_date or None)
                if updates:
                    values.append(id)
                    connection.execute(
                        f"UPDATE jobs SET {', '.join(updates)} WHERE id = ?", values
                    )
                row = connection.execute("SELECT * FROM jobs WHERE id = ?", (id,)).fetchone()
                return self._job_dict(connection, row)
        finally:
            connection.close()

    def list_sources(self) -> list[dict]:
        connection = self._connect()
        try:
            rows = connection.execute("SELECT * FROM sources ORDER BY name").fetchall()
            return [
                {
                    "name": row["name"],
                    "url": row["url"],
                    "status": row["status"],
                    "error": row["error"],
                    "checked_at": row["checked_at"],
                    "last_success_at": row["last_success_at"],
                    "row_count": row["row_count"],
                    "elapsed_seconds": row["elapsed_seconds"],
                }
                for row in rows
            ]
        finally:
            connection.close()

    def summary(self) -> dict:
        now = datetime.now(timezone.utc).timestamp()
        connection = self._connect()
        try:
            row = connection.execute(
                """SELECT COUNT(*) AS total,
                          COALESCE(SUM(saved), 0) AS saved,
                          COALESCE(SUM(first_seen_epoch BETWEEN ? AND ?), 0) AS new_24h
                   FROM jobs""",
                (now - 86400, now),
            ).fetchone()
            errors = connection.execute(
                "SELECT COUNT(*) AS count FROM sources WHERE status = 'error'"
            ).fetchone()["count"]
            return {
                "total": row["total"],
                "saved": row["saved"],
                "new_24h": row["new_24h"],
                "source_errors": errors,
            }
        finally:
            connection.close()

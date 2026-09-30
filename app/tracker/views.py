"""Presentation queries without eligibility or application side effects."""
from __future__ import annotations

import csv
import io
import re
from datetime import date, datetime, timedelta, timezone

from app.scouting.divisions import infer_division

PROGRAMMES = {'year_in_industry', 'spring_week', 'summer'}
_UK = re.compile(
    r'\b(?:united kingdom|uk|england|scotland|wales|northern ireland|london|belfast|'
    r'edinburgh|glasgow|manchester|birmingham|bristol|leeds|cardiff|reading|oxford|'
    r'cambridge|nottingham|sheffield|liverpool|newcastle|milton keynes|southampton|'
    r'aberdeen|bournemouth|basingstoke|guildford|watford|leicester|york)\b', re.I)
_FOREIGN_QUALIFIER = re.compile(r'\b(?:ontario|canada|kentucky|tennessee|massachusetts)\b', re.I)


def timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        raise ValueError('Timestamp has no timezone')
    return parsed.astimezone(timezone.utc)


def decorate_job(row: dict, *, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    first, last = timestamp(row['first_seen']), timestamp(row['last_seen'])
    deadline = date.fromisoformat(row['deadline']) if row.get('deadline') else None
    loc = row.get('location', '')
    # Explicit foreign qualifiers disambiguate shared city names; broad EMEA or
    # remote records remain unknown rather than being advertised as UK roles.
    uk = bool(_UK.search(loc)) and not bool(_FOREIGN_QUALIFIER.search(loc))
    return {**row, 'uk_match': uk,
            'new': timedelta(0) <= now - first < timedelta(hours=24),
            'stale': now - last > timedelta(hours=24),
            'expired': bool(deadline and deadline < now.date()),
            'deadline_soon': bool(deadline and now.date() <= deadline <= now.date() + timedelta(days=14)),
            'division': infer_division(row['title'], row['employer'])}


def filter_jobs(rows: list[dict], *, q: str = '', programmes: str = '', uk_only: bool = True,
                saved_only: bool = False, new_only: bool = False, deadline_soon: bool = False,
                include_expired: bool = False, stage: str = '', source: str = '',
                sort: str = 'newest', now: datetime | None = None,
                match_status: str = '', availability: str = '') -> list[dict]:
    wanted = {item for item in programmes.split(',') if item} or PROGRAMMES
    if not wanted <= PROGRAMMES:
        raise ValueError('Unknown programme filter')
    matches = {part for part in match_status.split(',') if part}
    if not matches <= {'potential', 'review', 'excluded'}:
        raise ValueError('Unknown profile match filter')
    if availability not in {'', 'open', 'closed', 'unknown'}:
        raise ValueError('Unknown availability filter')
    result = []
    for row in rows:
        item = decorate_job(row, now=now)
        if matches and item.get('match_status', 'review') not in matches:
            continue
        if availability and item.get('availability', 'unknown') != availability:
            continue
        if item['programme'] not in wanted or (uk_only and not item['uk_match']):
            continue
        if (saved_only and not item['saved']) or (new_only and not item['new']):
            continue
        if (deadline_soon and not item['deadline_soon']) or (not include_expired and item['expired']):
            continue
        if stage and item['stage'] != stage:
            continue
        if source and source not in item.get('sources', []):
            continue
        searchable = ' '.join(str(item.get(k, '')) for k in ('employer', 'title', 'location', 'division'))
        if q.casefold().strip() not in searchable.casefold():
            continue
        result.append(item)
    result.sort(key=lambda row: (row['first_seen'], row['id']), reverse=True)
    if sort == 'deadline':
        result.sort(key=lambda row: row['deadline'] or '9999-12-31')
    elif sort == 'priority':
        result.sort(key=lambda row: (
            {'potential': 0, 'review': 1, 'excluded': 2}.get(row.get('match_status'), 1),
            {'year_in_industry': 0, 'spring_week': 1, 'summer': 2}.get(row['programme'], 3),
            row.get('availability') != 'open', row['deadline'] or '9999-12-31',
        ))
    elif sort != 'newest':
        raise ValueError('Unknown sort order')
    return result


def _csv_safe(value) -> str:
    text = str(value) if value is not None else ''
    if text and (text[0] in '=+-@\t\r\n' or text.lstrip().startswith(('=', '+', '-', '@'))):
        return "'" + text
    return text


def export_csv(rows: list[dict]) -> str:
    stream = io.StringIO(newline='')
    columns = ['employer', 'role_title', 'cycle', 'url', 'programme_group', 'location',
               'deadline', 'source', 'first_seen', 'last_seen', 'stage', 'notes', 'due_date']
    writer = csv.DictWriter(stream, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        year = re.search(r'\b20\d{2}\b', row['title'])
        values = {**row, 'role_title': row['title'], 'programme_group': row['programme'],
                  'cycle': year.group() if year else 'UNSPECIFIED',
                  'source': '; '.join(row.get('sources', []))}
        writer.writerow({key: _csv_safe(values.get(key)) for key in columns})
    return '\ufeff' + stream.getvalue()

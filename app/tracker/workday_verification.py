"""Verify a concrete Workday vacancy using its public CXS job-detail response."""
from __future__ import annotations
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit
from bs4 import BeautifulSoup
from app.tracker.public_web import WebPageResult, _parse_date, _title_score, validate_public_url


def workday_api_url(url: str) -> str | None:
    parsed = urlsplit(validate_public_url(url))
    host = parsed.hostname or ''
    if not re.fullmatch(r'[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com', host):
        return None
    parts = parsed.path.strip('/').split('/')
    if parts and re.fullmatch(r'[a-z]{2}-[A-Z]{2}', parts[0]):
        parts = parts[1:]
    if len(parts) < 3 or parts[1] != 'job' or any(unquote(p) in {'.', '..'} for p in parts):
        return None
    return f'https://{host}/wday/cxs/{host.split(".")[0]}/' + '/'.join(parts)


def parse_workday_payload(body: bytes, source_url: str, api_url: str, *, expected_title='',
                          expected_employer='', observed_at=None, status_code=200) -> WebPageResult:
    observed = observed_at or datetime.now(timezone.utc).isoformat()
    result = WebPageResult(source_url=source_url, final_url=api_url, observed_at=observed,
                           status_code=status_code, source_response_hash=hashlib.sha256(body).hexdigest())
    if status_code in {404, 410}:
        return result.__class__(**{**result.as_dict(), 'availability': 'closed',
            'verification_error': 'Public Workday vacancy no longer exists'})
    if status_code != 200:
        return result.__class__(**{**result.as_dict(), 'verification_error': f'Workday HTTP {status_code}'})
    try:
        raw = json.loads(body)
        job = raw['jobPostingInfo']
        if not isinstance(job, dict):
            raise ValueError('Missing job object')
    except (ValueError, KeyError, TypeError):
        return result.__class__(**{**result.as_dict(), 'verification_error': 'Workday job detail was not valid JSON'})
    title = str(job.get('title') or '')
    requisition = str(job.get('jobReqId') or '')
    source_id = unquote(urlsplit(source_url).path.rstrip('/').rsplit('_', 1)[-1])
    if not title or not requisition or source_id != requisition:
        return result.__class__(**{**result.as_dict(), 'verification_error': 'Workday requisition identity mismatch'})
    deadline = _parse_date(str(job.get('endDate') or ''))
    if job.get('posted') is False or (deadline and deadline < observed[:10]):
        availability, error = 'closed', 'Workday vacancy is no longer posted or its deadline passed'
    elif job.get('canApply') is True and job.get('posted') is True:
        availability, error = 'open', ''
    else:
        availability, error = 'unknown', 'Public Workday apply state was not confirmed'
    description = BeautifulSoup(str(job.get('jobDescription') or ''), 'html.parser').get_text(' ', strip=True)
    quotes = [title, description[:500]]
    if deadline:
        quotes.append(str(job.get('endDate')))
    raw_text = body.decode('utf-8', 'replace')
    for key in ('canApply', 'posted'):
        marker = re.search(r'"' + key + r'"\s*:\s*(?:true|false)', raw_text)
        if marker:
            quotes.append(marker.group(0))
    return WebPageResult(availability=availability, verification_error=error,
        deadline=deadline, deadline_text=str(job.get('jobPostingEndDateAsText') or job.get('endDate') or ''),
        deadline_basis='workday_end_date' if deadline else 'unknown',
        posted_text=str(job.get('postedOn') or ''), posted_at=None,
        title=title, employer=expected_employer, location=str(job.get('location') or ''),
        role_text=description, source_url=source_url, final_url=api_url, observed_at=observed,
        source_response_hash=result.source_response_hash, status_code=status_code,
        evidence=[{'quote': q, 'source_url': api_url, 'observed_at': observed} for q in quotes if q])

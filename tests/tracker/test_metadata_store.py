from dataclasses import dataclass
import sqlite3

from app.tracker.contracts import Listing, SourceResult
from app.tracker.store import TrackerStore


@dataclass(frozen=True, slots=True)
class RichListing(Listing):
    description: str = ''
    deadline_text: str = ''
    posted_text: str = ''


def result(listing, *, name='direct', checked='2026-09-11T12:00:00+00:00'):
    return SourceResult(name, 'https://careers.example.com/jobs', 'ok', [listing], checked_at=checked)


def test_source_evidence_survives_store_and_retains_user_notes(tmp_path):
    path = tmp_path / 'tracker.sqlite3'
    store = TrackerStore(path)
    item = RichListing('Firm', 'Summer Internship 2027', 'https://careers.example.com/jobs/123',
                       location='London', programme='summer', description='Finance degree. Graduate 2028.',
                       deadline_text='30 September 2026', posted_text='2 days ago')
    store.ingest([result(item)])
    row = store.list_jobs()[0]
    assert row['description'] == item.description
    assert row['deadline_text'] == item.deadline_text
    assert row['posted_text'] == item.posted_text
    assert row['metadata_source_url'] == 'https://careers.example.com/jobs'
    store.update_job(row['id'], notes='My note', saved=True, stage='assessment')
    reopened = TrackerStore(path)
    reopened.ingest([result(Listing('Firm', item.title, item.url), name='simplytk')])
    updated = reopened.list_jobs()[0]
    assert updated['description'] == item.description
    assert updated['deadline_text'] == item.deadline_text
    assert updated['notes'] == 'My note' and updated['saved'] and updated['stage'] == 'assessment'


def test_replayed_metadata_does_not_replace_newer_source_evidence(tmp_path):
    store = TrackerStore(tmp_path / 'tracker.sqlite3')
    item = RichListing('Firm', 'Summer Intern', 'https://careers.example.com/jobs/123', description='New requirements')
    store.ingest([result(item)])
    old = RichListing('Firm', item.title, item.url, description='Old requirements')
    store.ingest([result(old, checked='2026-09-10T12:00:00+00:00')])
    assert store.list_jobs()[0]['description'] == 'New requirements'

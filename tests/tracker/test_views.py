from datetime import datetime, timezone

from app.tracker.views import filter_jobs, export_csv, decorate_job

NOW = datetime(2026, 9, 11, 12, tzinfo=timezone.utc)


def job(**kwargs):
    return dict(id='id', employer='Firm', title='Summer Internship 2027',
                url='https://jobs.example.com/jobs/7', location='London', programme='summer',
                first_seen='2026-09-11T10:00:00+00:00', last_seen='2026-09-11T10:00:00+00:00',
                saved=False, stage='not_applied', deadline=None, notes='', due_date=None,
                **kwargs)


def test_uk_filters_are_not_substring_guesses():
    rows = [job(), {**job(), 'id': 'ca', 'location': 'London, Ontario, Canada'},
            {**job(), 'id': 'unknown', 'location': 'EMEA'},
            {**job(), 'id': 'multi', 'location': 'New York; London'}]
    assert {r['id'] for r in filter_jobs(rows, now=NOW)} == {'id', 'multi'}
    assert len(filter_jobs(rows, uk_only=False, now=NOW)) == 4


def test_expired_deadlines_and_stale_observations_are_distinct():
    row = {**job(), 'deadline': '2026-09-10', 'last_seen': '2026-09-08T12:00:00Z'}
    assert decorate_job(row, now=NOW)['expired'] is True
    assert decorate_job(row, now=NOW)['stale'] is True
    assert filter_jobs([row], now=NOW) == []
    assert len(filter_jobs([row], include_expired=True, now=NOW)) == 1
    assert len(filter_jobs([job()], new_only=True, now=NOW)) == 1


def test_export_prevents_formula_execution_and_is_argus_importable():
    text = export_csv([{**job(), 'employer': '=HYPERLINK("bad")', 'notes': '\t=1+1'}])
    assert "'=HYPERLINK" in text
    assert "'\t=1+1" in text
    assert 'cycle' in text.splitlines()[0]
    assert '2027' in text

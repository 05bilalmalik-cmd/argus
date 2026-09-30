from app.tracker import sources, additional_sources
from app.tracker.contracts import SourceResult


def test_composed_public_collector_registers_primary_finance_boards(monkeypatch):
    regular = SourceResult('fixture', 'https://example.test/jobs', 'empty')
    direct = SourceResult('workday:fixture', 'https://example.test/direct', 'empty')
    monkeypatch.setattr(sources, 'collect_sources', lambda: [regular])
    monkeypatch.setattr(additional_sources, 'collect_additional_sources', lambda: [direct])
    assert sources.collect_all_sources() == [regular, direct]

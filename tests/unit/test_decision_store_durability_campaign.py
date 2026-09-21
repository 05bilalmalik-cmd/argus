"""Root-scoped readback and durability failures must not create human answers."""
from types import SimpleNamespace
import json
import pytest
from app.services import decision_store as store


def record(identifier='answer', choice='first'):
    return dict(decision_id=identifier, chosen_option=choice, decided_by='synthetic',
                decided_at='2026-09-20T12:00:00+00:00', application_id='synthetic-app',
                canonical_key='synthetic.key', sensitivity='standard', context_revision='synthetic-revision')


def test_legacy_records_belong_only_to_the_legacy_data_root(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'ambient'))
    legacy = store.legacy_jsonl_path()
    legacy.parent.mkdir(parents=True)
    legacy.write_text(json.dumps(record())+'\n', encoding='utf-8')
    for name in ('root-a', 'root-b'):
        assert store.read_records(SimpleNamespace(data_dir=tmp_path/name)) == {}
    assert store.read_records(SimpleNamespace(data_dir=legacy.parent))['answer']['chosen_option'] == 'first'


def test_failed_fsync_mirror_record_is_not_an_accepted_answer(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'ambient'))
    settings = SimpleNamespace(data_dir=tmp_path/'data')
    def fail(_):
        raise OSError('synthetic fsync failure')
    with monkeypatch.context() as fault:
        fault.setattr(store.os, 'fsync', fail)
        with pytest.raises(store.DecisionStoreCorruptError, match='mirror append failed'):
            store.try_record(settings, record())
    assert store.read_records(settings) == {}
    assert store.try_record(settings, record(choice='second')) is True
    assert store.read_records(settings)['answer']['chosen_option'] == 'second'
    assert store.try_record(settings, record(choice='third')) is False
    assert store.read_records(settings)['answer']['chosen_option'] == 'second'


def test_malformed_mirror_remains_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path/'ambient'))
    settings = SimpleNamespace(data_dir=tmp_path/'data')
    mirror, _ = store.init_store(settings)
    mirror.write_text('{broken', encoding='utf-8')
    with pytest.raises(store.DecisionStoreCorruptError):
        store.read_records(settings)

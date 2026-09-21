from app.tracker.__main__ import main


def test_cli_enables_composed_automation_by_default(tmp_path, monkeypatch):
    from app.tracker import server
    seen = []
    class Store:
        def summary(self): return {}
        def list_sources(self): return [{'status': 'ok'}]
    class Refresh:
        def refresh_sync(self): return True
        def snapshot(self): return {}
    class Fake:
        class state:
            store = Store()
            refresh = Refresh()
    def factory(root, **kwargs):
        seen.append(kwargs)
        return Fake()
    monkeypatch.setattr(server, 'create_tracker_app', factory)
    assert main(['--data-dir', str(tmp_path), '--once']) == 0
    assert seen[0]['automated'] is True

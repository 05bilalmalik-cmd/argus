"""Durable stage reporting for the public collection/verification/email pipeline."""
from __future__ import annotations

import copy
import json
import os
import threading
from pathlib import Path

from app.tracker.contracts import utc_now
from app.tracker.views import decorate_job


class AutomationPipeline:
    def __init__(self, data_dir: Path, store, collector, intelligence, alerts):
        self.path = Path(data_dir) / 'pipeline.json'
        self.store, self.collector = store, collector
        self.intelligence, self.alerts = intelligence, alerts
        self._guard = threading.RLock()
        if self.path.exists():
            self._state = json.loads(self.path.read_text(encoding='utf-8'))
            if self._state.get('version') != 1 or not isinstance(self._state.get('stages'), dict):
                raise ValueError('Invalid pipeline state; preserve and investigate the state file')
        else:
            self._state = {'version': 1, 'started_at': None, 'finished_at': None,
                           'stages': {name: {'status': 'never_checked', 'last_error': '',
                                             'last_success_at': None, 'last_finished_at': None,
                                             'result': None}
                                      for name in ('discovery', 'verification', 'delivery')}}

    def snapshot(self) -> dict:
        with self._guard:
            return copy.deepcopy(self._state)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(f'.{os.getpid()}.tmp')
        with temporary.open('w', encoding='utf-8') as stream:
            json.dump(self._state, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def _stage(self, name: str, action):
        with self._guard:
            stage = self._state['stages'][name]
            # A retry does not erase the previous failure while it runs.
            stage.update(status='running', last_started_at=utc_now())
            self._save()
        try:
            result, status = action()
            if status not in {'ok', 'partial', 'blocked'}:
                raise ValueError('Unknown pipeline stage result')
            with self._guard:
                stage.update(status=status, result=result, last_finished_at=utc_now())
                if status == 'ok':
                    stage.update(last_success_at=utc_now(), last_error='')
                else:
                    stage['last_error'] = f'{name} {status}; inspect its health details'
                self._save()
            return result
        except Exception as exc:
            with self._guard:
                # Provider exception strings can contain transport credentials.
                stage.update(status='error', last_error=type(exc).__name__, last_finished_at=utc_now())
                self._save()
            raise

    def run(self) -> dict:
        with self._guard:
            self._state.update(started_at=utc_now(), finished_at=None)
            self._save()
        try:
            def discover():
                results = self.collector()
                if not results:
                    raise ValueError('Collector returned no source results')
                totals = self.store.ingest(results)
                complete = all(item['status'] in {'ok', 'empty'} for item in self.store.list_sources())
                return totals, 'ok' if complete else 'partial'
            totals = self._stage('discovery', discover)

            def verify():
                result = self.intelligence.run(self.store.list_jobs())
                return result, 'partial' if result.get('errors') or result.get('queued') else 'ok'
            self._stage('verification', verify)

            def deliver():
                # Re-read tracking decisions after network work, not before it.
                result = self.alerts.run([decorate_job(job) for job in
                                         self.intelligence.decorate(self.store.list_jobs())])
                health = self.alerts.summary()
                state = str(result.get('status', health.get('status', 'unknown')))
                if state in {'disabled', 'unconfigured', 'blocked', 'unknown'}:
                    status = 'blocked'
                elif (any(result.get(key) or health.get(key) for key in ('failed', 'uncertain', 'pending', 'queued'))
                      or health.get('success') is False or state in {'error', 'failed', 'degraded'}):
                    status = 'partial'
                else:
                    status = 'ok'
                return result, status
            self._stage('delivery', deliver)
            return totals
        finally:
            with self._guard:
                self._state['finished_at'] = utc_now()
                self._save()

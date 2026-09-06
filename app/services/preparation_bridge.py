"""Controlled local preparation authority; no browser or HTTP ownership.

The injected executor must be PREFILL-only and call mutation_guard() immediately
before *every* browser mutation. True authorizes only that next mutation, never
submission. Machine dispatch is not an authentication boundary: routes must call
authenticate() separately before dispatch and must not expose operator methods.
"""
from __future__ import annotations

import time
import hashlib
import json
import re
import os
import secrets
import hmac
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import select

from app.bridge.storage import BridgeStorage
from app.models import Application, AutomationRun, CandidateProfile, AnswerEntry, Document, ConflictRule
from app.services.applications import ApplicationService


def canonical_uuid(value):
    try:
        return isinstance(value, str) and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


class PreparationRefused(ValueError):
    """Contains only a closed protocol reason; never a raw underlying error."""


def _columns(record):
    return {column.name: getattr(record, column.name) for column in record.__table__.columns}


def wire(status, reason='NONE', run_id=None, *, pending=0, handoffs=None, next_cursor=None):
    return {'protocol': 'argus.preparation.v1', 'status': status, 'reason': reason, 'run_id': run_id, 'pending': min(100000, max(0, pending)), 'handoffs': handoffs or [], 'next_cursor': next_cursor}


class PreparationBridge:
    def __init__(self, database, settings, crypto, navigator, execute_prefill, *, enabled=False, clock=time.time):
        self.database = database
        self.settings = settings
        self.crypto = crypto
        self.navigator = navigator
        self.execute_prefill = execute_prefill
        self.enabled = enabled
        self.clock = clock
        self._storage = None
        self._runtime_lock = None

    def _store(self):
        if not self.enabled:
            raise ValueError('DISABLED')
        if self._storage is None:
            store = BridgeStorage(self.database, self.settings)
            store.initialize()
            self._storage = store
        return self._storage

    def start(self):
        if self.enabled and self._runtime_lock is None:
            self._runtime_lock = self._store().acquire_runtime()
            with self._store().transaction() as connection:
                orphaned = connection.execute("UPDATE bridge_runs SET status='INTERRUPTED',reason='INTERRUPTED' WHERE status='RUNNING'").rowcount
                if orphaned:
                    connection.execute("UPDATE bridge_state SET paused='INTERRUPTED' WHERE id=1")
                    connection.execute('UPDATE bridge_grants SET revoked=1')

    def authenticate(self, token):
        if not self.enabled or not isinstance(token, str) or re.fullmatch(r'[0-9a-f]{64}', token) is None:
            return False
        try:
            with self._store().transaction() as connection:
                row = connection.execute('SELECT * FROM bridge_credential WHERE id=1').fetchone()
            return bool(row and row['expires_at'] > self.clock() and hmac.compare_digest(row['token_hash'], hashlib.sha256(token.encode('ascii')).hexdigest()))
        except Exception:
            return False

    def provision(self, credential_file, endpoint, ttl_seconds):
        match = re.fullmatch(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})', endpoint) if isinstance(endpoint, str) else None
        if not match or not 1 <= int(match[1]) <= 65535 or type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 86400:
            raise ValueError('INVALID_REQUEST')
        store = self._store()
        token = secrets.token_hex(32)
        expires_at = self.clock() + ttl_seconds
        path = Path(credential_file)
        created = False
        try:
            # Exclusive create: never overwrite an unrelated operator file.
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            created = True
            with os.fdopen(fd, 'w', encoding='utf-8') as stream:
                if os.name == 'nt':
                    import subprocess
                    # Restrict inherited ACL before writing any credential bytes.
                    identity = subprocess.run(['whoami'], check=True, capture_output=True, text=True).stdout.strip()
                    subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r', identity + ':F'], check=True, capture_output=True)
                json.dump({'endpoint': endpoint, 'token': token, 'expires_at': expires_at}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            with store.transaction() as connection:
                connection.execute('INSERT OR REPLACE INTO bridge_credential VALUES (1,?,?)', (hashlib.sha256(token.encode('ascii')).hexdigest(), expires_at))
        except Exception:
            if created:
                path.unlink(missing_ok=True)
            raise ValueError('NOT_READY') from None

    def approve(self, application_id, confirm_application_id, ttl_seconds):
        if not canonical_uuid(application_id) or confirm_application_id != application_id:
            raise ValueError('INVALID_REQUEST')
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 900:
            raise ValueError('INVALID_REQUEST')
        store = self._store()
        try:
            # Evaluation writes assessment/audit rows. Roll it back explicitly;
            # approval never commits applicant mutations or calls prepare/queue.
            with self.database.SessionLocal() as session:
                application = session.get(Application, application_id)
                if application is None or application.state != 'PACKAGE_PREPARED':
                    raise ValueError('NOT_READY')
                opportunity = application.opportunity
                from app.domain.states import user_status_exclusion_reason
                from app.domain.opportunity_scope import out_of_scope_programme_reason
                from app.scouting.programmes import resolve_programme_framing
                from app.services.applications import select_cv_for_opportunity, cv_preference_tags
                from app.services.documents import DocumentService
                framing = resolve_programme_framing(opportunity.programme_group)
                if (not opportunity.automation_url or not opportunity.resolved_at or
                        opportunity.is_archived or not opportunity.is_open_for_applications or
                        user_status_exclusion_reason(opportunity.user_status) or not framing or
                        out_of_scope_programme_reason(opportunity.role_title, opportunity.programme_group)):
                    raise ValueError('NOT_READY')
                documents = DocumentService(session, self.settings.documents_dir)
                cv = select_cv_for_opportunity(documents, opportunity)
                cover = documents.select_approved('cover_letter', (*cv_preference_tags(opportunity), framing.cv_variant_tag)) if opportunity.cover_letter_required else None
                if ((opportunity.cv_required and cv is None) or
                        (opportunity.cover_letter_required and cover is None) or
                        application.selected_cv_id != (cv.id if cv else None) or
                        application.selected_cover_letter_id != (cover.id if cover else None)):
                    raise ValueError('NOT_READY')
                decision = ApplicationService(session, self.settings, self.crypto).evaluate(application.opportunity_id)
                if not decision.eligible or decision.requires_review or decision.conflict_blocked:
                    raise ValueError('NOT_READY')
                session.rollback()
                fingerprint = self._fingerprint(session, application_id)
        except Exception:
            raise ValueError('NOT_READY') from None
        grant_id = str(uuid4())
        with store.transaction() as connection:
            connection.execute('INSERT INTO bridge_grants (id,application_id,fingerprint,expires_at) VALUES (?,?,?,?)', (grant_id, application_id, fingerprint, self.clock()+ttl_seconds))
        return grant_id

    @staticmethod
    def _run_wire(row):
        return wire(row['status'], row['reason'], row['id'])

    def _fingerprint(self, session, application_id):
        application = session.get(Application, application_id)
        if application is None or application.state not in {'PACKAGE_PREPARED', 'FILLING', 'NEEDS_USER', 'READY_TO_SUBMIT'}:
            raise PreparationRefused('DRIFT')
        opportunity = application.opportunity
        if not opportunity.automation_url or not opportunity.resolved_at or opportunity.is_archived:
            raise PreparationRefused('DRIFT')
        profile = session.get(CandidateProfile, 1)
        if profile is None:
            raise PreparationRefused('DRIFT')
        documents = []
        for document_id in (application.selected_cv_id, application.selected_cover_letter_id):
            if document_id is None:
                continue
            document = session.get(Document, document_id)
            if document is None or not document.approved:
                raise PreparationRefused('DRIFT')
            path = Path(document.path).resolve(strict=True)
            if not path.is_relative_to(self.settings.documents_dir.resolve()) or not path.is_file():
                raise PreparationRefused('DRIFT')
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != document.sha256:
                raise PreparationRefused('DRIFT')
            documents.append({'metadata': _columns(document), 'actual_sha256': digest})
        payload = {'application_id': application_id, 'opportunity': _columns(opportunity), 'profile': _columns(profile), 'documents': documents,
                   'selected': [application.selected_cv_id, application.selected_cover_letter_id],
                   'answers': [_columns(row) for row in session.scalars(select(AnswerEntry).order_by(AnswerEntry.id))],
                   'conflict_rules': [_columns(row) for row in session.scalars(select(ConflictRule).order_by(ConflictRule.id))],
                   'other_applications': [(row.id, row.opportunity_id, row.state) for row in session.scalars(select(Application).where(Application.id != application_id).order_by(Application.id))]}
        return hashlib.sha256(json.dumps(payload, default=str, sort_keys=True, separators=(',', ':')).encode()).hexdigest()

    def _busy_sessions(self, application_id=None):
        for snapshot in self.navigator.all_sessions():
            if snapshot.worker_alive or not snapshot.cleanup_complete:
                if application_id == snapshot.application_id and str(snapshot.state) in {'OPENING', 'ACTIVE'} and snapshot.mode == 'prefill':
                    continue
                return True
        return False

    def revoke(self, grant_id):
        if not canonical_uuid(grant_id):
            raise ValueError('INVALID_REQUEST')
        with self._store().transaction() as connection:
            connection.execute('UPDATE bridge_grants SET revoked=1 WHERE id=?', (grant_id,))

    def clear_pause(self):
        with self._store().transaction() as connection:
            connection.execute("UPDATE bridge_state SET paused='' WHERE id=1")

    def _pause(self, reason):
        with self._store().transaction() as connection:
            connection.execute('UPDATE bridge_state SET paused=? WHERE id=1', (reason,))
            connection.execute('UPDATE bridge_grants SET revoked=1')

    def _guard(self, grant_id, *, check_sessions=True):
        with self._store().transaction() as connection:
            grant = connection.execute('SELECT * FROM bridge_grants WHERE id=?', (grant_id,)).fetchone()
            paused = connection.execute('SELECT paused FROM bridge_state WHERE id=1').fetchone()[0]
            run = connection.execute('SELECT status FROM bridge_runs WHERE grant_id=?', (grant_id,)).fetchone()
        if run is not None and run['status'] != 'RUNNING':
            raise PreparationRefused('INTERRUPTED')
        if paused:
            raise PreparationRefused('PAUSED')
        if grant is None or grant['revoked']:
            raise PreparationRefused('REVOKED')
        if grant['expires_at'] <= self.clock():
            raise PreparationRefused('EXPIRED')
        try:
            with self.database.SessionLocal() as session:
                digest = self._fingerprint(session, grant['application_id'])
            if digest != grant['fingerprint']:
                raise PreparationRefused('DRIFT')
        except Exception:
            raise PreparationRefused('DRIFT') from None
        if check_sessions and self._busy_sessions(grant['application_id']):
            raise PreparationRefused('BUSY')
        return True

    def _request(self, body):
        store = self._store()
        with store.transaction() as connection:
            existing = connection.execute('SELECT * FROM bridge_runs WHERE idempotency_key=?', (body['idempotency_key'],)).fetchone()
            if existing:
                return self._run_wire(existing)
            if connection.execute('SELECT paused FROM bridge_state WHERE id=1').fetchone()[0]:
                return wire('PAUSED', 'PAUSED')
            if connection.execute("SELECT 1 FROM bridge_runs WHERE status='RUNNING' LIMIT 1").fetchone():
                return wire('BUSY', 'BUSY')
            if self._busy_sessions():
                return wire('BUSY', 'BUSY')
            grant = connection.execute('SELECT * FROM bridge_grants WHERE consumed=0 AND revoked=0 AND expires_at>? ORDER BY rowid LIMIT 1', (self.clock(),)).fetchone()
            if grant is None:
                last = connection.execute('SELECT * FROM bridge_grants WHERE consumed=0 ORDER BY rowid DESC LIMIT 1').fetchone()
                if last:
                    return wire('REFUSED', 'REVOKED' if last['revoked'] else 'EXPIRED')
                return wire('REFUSED', 'NO_APPROVAL')
            run_id = str(uuid4())
            connection.execute('UPDATE bridge_grants SET consumed=1 WHERE id=?', (grant['id'],))
            connection.execute('INSERT INTO bridge_runs (id,idempotency_key,grant_id,application_id,status,reason) VALUES (?,?,?,?,?,?)', (run_id, body['idempotency_key'], grant['id'], grant['application_id'], 'RUNNING', 'NONE'))
        status, reason = 'FAILED', 'EXECUTION_FAILED'
        underlying_id = session_id = None
        refusal = []
        def mutation_guard():
            if refusal:
                raise PreparationRefused(refusal[0])
            try:
                return self._guard(grant['id'])
            except PreparationRefused as exc:
                refusal.append(str(exc))
                raise
        try:
            mutation_guard()
            result = self.execute_prefill(grant['application_id'], mutation_guard)
            if refusal:
                raise PreparationRefused(refusal[0])
            from starlette.responses import JSONResponse
            if isinstance(result, JSONResponse):
                if len(result.body) <= 16384 and self._submission_evidence(json.loads(result.body)):
                    self._pause('INTEGRITY')
                    status, reason = 'BLOCKED', 'INTEGRITY'
                elif 400 <= result.status_code < 500:
                    status, reason = 'BLOCKED', 'NOT_READY'
                raise ValueError('EXECUTION_FAILED')
            if not isinstance(result, dict):
                raise ValueError('EXECUTION_FAILED')
            underlying_id = result.get('run_id')
            session_id = result.get('handoff_session_id')
            with self.database.SessionLocal() as session:
                underlying = session.get(AutomationRun, underlying_id) if canonical_uuid(underlying_id) else None
                integrity = self._submission_evidence(result) or (underlying is not None and (underlying.state in {'SUBMITTED', 'CONFIRMATION_VERIFIED'} or underlying.receipt_json not in (None, '', '{}', 'null')))
                correlated = underlying is not None and underlying.application_id == grant['application_id'] and underlying.mode == 'prefill' and underlying.state in {'NEEDS_USER', 'READY_TO_SUBMIT'}
            if integrity:
                self._pause('INTEGRITY')
                status, reason = 'BLOCKED', 'INTEGRITY'
            else:
                self._guard(grant['id'], check_sessions=False)
            live = next((item for item in self.navigator.all_sessions() if item.session_id == session_id and item.application_id == grant['application_id'] and item.worker_alive and item.headed and not item.cleanup_complete and item.mode == 'prefill' and item.expires_at.timestamp() > self.clock()), None)
            if not integrity and correlated and live and str(live.state) in {'FINAL_REVIEW', 'HUMAN_REQUIRED'}:
                status = 'PREFILLED_HANDOFF' if str(live.state) == 'FINAL_REVIEW' else 'HUMAN_REQUIRED'
                reason = 'HANDOFF'
        except PreparationRefused as exc:
            status, reason = 'REFUSED', str(exc)
        except Exception:
            pass
        with store.transaction() as connection:
            connection.execute("UPDATE bridge_runs SET status=?,reason=?,underlying_run_id=?,session_id=? WHERE id=? AND status='RUNNING'", (status, reason, underlying_id if canonical_uuid(underlying_id) else None, session_id if canonical_uuid(session_id) else None, run_id))
            return self._run_wire(connection.execute('SELECT * FROM bridge_runs WHERE id=?', (run_id,)).fetchone())

    @classmethod
    def _submission_evidence(cls, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {'receipt', 'receipt_json', 'submitted', 'click_boundary_crossed', 'submit_clicked', 'final_click_attempted', 'submission_attempted', 'next_clicked', 'apply_clicked'} and item:
                    return True
                if key in {'state', 'status'} and item in ('SUBMITTED', 'CONFIRMATION_VERIFIED'):
                    return True
                if cls._submission_evidence(item):
                    return True
        elif isinstance(value, (list, tuple)):
            return any(cls._submission_evidence(item) for item in value)
        return False

    def dispatch(self, operation, body):
        if not self.enabled:
            return wire('DISABLED', 'DISABLED')
        if not self._valid_request(operation, body):
            return wire('REFUSED', 'INVALID_REQUEST')
        try:
            return self._dispatch(operation, body)
        except Exception:
            return wire('REFUSED', 'NOT_READY')

    @staticmethod
    def _valid_request(operation, body):
        fields = {'get_preparation_readiness': set(), 'request_approved_preparation': {'idempotency_key'}, 'get_preparation_run': {'run_id'}, 'list_preparation_handoffs': {'after_cursor', 'limit'}, 'pause_preparation': {'reason_code'}}
        if not isinstance(operation, str) or operation not in fields or not isinstance(body, dict) or set(body) != fields[operation]:
            return False
        if operation == 'request_approved_preparation':
            return isinstance(body['idempotency_key'], str) and re.fullmatch(r'[A-Za-z0-9_-]{8,64}', body['idempotency_key']) is not None
        if operation == 'get_preparation_run':
            return canonical_uuid(body['run_id'])
        if operation == 'list_preparation_handoffs':
            return (body['after_cursor'] is None or canonical_uuid(body['after_cursor'])) and type(body['limit']) is int and 1 <= body['limit'] <= 100
        if operation == 'pause_preparation':
            return body['reason_code'] in ('OPERATOR_REQUESTED', 'INTEGRITY', 'TRANSPORT_ERROR')
        return True

    def _dispatch(self, operation, body):
        if operation == 'list_preparation_handoffs':
            with self._store().transaction() as connection:
                after = 0
                if body['after_cursor'] is not None:
                    cursor = connection.execute('SELECT seq FROM bridge_runs WHERE id=?', (body['after_cursor'],)).fetchone()
                    if cursor is None:
                        return wire('REFUSED', 'NOT_FOUND')
                    after = cursor['seq']
                rows = connection.execute("SELECT * FROM bridge_runs WHERE seq>? AND status IN ('PREFILLED_HANDOFF','HUMAN_REQUIRED') ORDER BY seq LIMIT ?", (after, body['limit'] + 1)).fetchall()
                page = rows[:body['limit']]
                return wire('READY', handoffs=[{'run_id': row['id'], 'status': row['status'], 'reason': row['reason']} for row in page], next_cursor=page[-1]['id'] if len(rows) > body['limit'] else None)
        if operation == 'get_preparation_readiness':
            with self._store().transaction() as connection:
                if connection.execute('SELECT paused FROM bridge_state WHERE id=1').fetchone()[0]:
                    return wire('PAUSED', 'PAUSED')
                if connection.execute("SELECT 1 FROM bridge_runs WHERE status='RUNNING' LIMIT 1").fetchone() or self._busy_sessions():
                    return wire('BUSY', 'BUSY')
                pending = connection.execute('SELECT COUNT(*) FROM bridge_grants WHERE consumed=0 AND revoked=0 AND expires_at>?', (self.clock(),)).fetchone()[0]
            return wire('READY', pending=pending)
        if operation == 'request_approved_preparation':
            return self._request(body)
        if operation == 'pause_preparation':
            self._pause(body['reason_code'])
            return wire('PAUSED', 'PAUSED')
        if operation == 'get_preparation_run':
            with self._store().transaction() as connection:
                row = connection.execute('SELECT * FROM bridge_runs WHERE id=?', (body['run_id'],)).fetchone()
                return self._run_wire(row) if row else wire('REFUSED', 'NOT_FOUND')
        return wire('REFUSED', 'NO_APPROVAL')

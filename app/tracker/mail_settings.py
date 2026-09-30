"""Operator-controlled email setup. Never expose credentials in tracker APIs.

The user enters an app-specific password through the loopback-only setup page.
It is stored only under the private service data directory, not in the tracker
profile or event database. Ordinary agent inspection must not read that file.
"""
from __future__ import annotations

import json
import os
import re
import smtplib
import ssl
import tempfile
import threading
from pathlib import Path

from app.tracker.alerts import AlertConfig, AlertService

# Explicit transports for interactive setup; other operator-managed relays can
# still use the documented environment configuration without this web endpoint.
_MAIL_HOSTS = {'smtp.gmail.com', 'smtp.office365.com', 'smtp.mail.yahoo.com', 'smtp.zoho.eu'}
_FIELDS = {'smtp_host', 'smtp_port', 'smtp_user', 'smtp_password', 'from_address', 'to_address'}
_ADDRESS = re.compile(r'^[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+$')


def validate_settings(payload: dict) -> dict:
    if not isinstance(payload, dict) or set(payload) != _FIELDS:
        raise ValueError('Supply the six email setup fields; unsupported fields are refused')
    values = dict(payload)
    if values['smtp_host'] not in _MAIL_HOSTS:
        raise ValueError('Choose a supported SMTP host')
    if type(values['smtp_port']) is not int or values['smtp_port'] != 587:
        raise ValueError('Interactive setup requires STARTTLS port 587')
    for field in ('smtp_user', 'from_address', 'to_address'):
        value = values[field]
        if not isinstance(value, str) or len(value) > 254 or not _ADDRESS.fullmatch(value):
            raise ValueError('Sender, recipient and login must be single email addresses')
    secret = values['smtp_password']
    if not isinstance(secret, str) or not 1 <= len(secret) <= 512 or any(c in secret for c in '\r\n\x00'):
        raise ValueError('An app-specific mail password is required')
    return {'enabled': True, 'transport': 'smtp', 'smtp_tls': True, **values}


def check_credentials(config: AlertConfig) -> None:
    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=20) as client:
        client.ehlo()
        client.starttls(context=ssl.create_default_context())
        client.ehlo()
        client.login(config.smtp_user, config.smtp_password)


class ConfiguredAlerts:
    """Serialize setup with delivery; never reload configuration mid-send."""
    def __init__(self, data_dir: Path, *, credential_check=check_credentials):
        self.root = Path(data_dir)
        self.path = self.root / 'mail-settings.json'
        self._guard = threading.RLock()
        self._check = credential_check
        config = None
        if self.path.exists():
            # Runtime-only credential access, never returned or logged.
            config = validate_settings(json.loads(self.path.read_text(encoding='utf-8')))
        self._service = AlertService(self.root, config=config)

    def configuration_status(self) -> dict:
        with self._guard:
            config = self._service.config
            return {'configured': bool(config.enabled and config.smtp_host),
                    'smtp_host': config.smtp_host, 'smtp_user': config.smtp_user,
                    'from_address': config.from_address, 'to_address': config.to_address,
                    'storage': 'private service data directory; never returned by API'}

    def configure(self, payload: dict) -> dict:
        values = validate_settings(payload)
        with self._guard:
            try:
                self._check(AlertConfig.from_dict(values))
            except Exception:
                raise ValueError('SMTP authentication or verified TLS connection failed; settings not saved') from None
            self.root.mkdir(parents=True, exist_ok=True)
            handle, temporary = tempfile.mkstemp(prefix='.mail-', dir=self.root)
            try:
                with os.fdopen(handle, 'w', encoding='utf-8') as stream:
                    # Persist only the allowed input fields, never environment state.
                    json.dump({key: values[key] for key in _FIELDS}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.chmod(temporary, 0o600)
                os.replace(temporary, self.path)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)
            self._service = AlertService(self.root, config=values)
            return self.configuration_status()

    def send_test(self, *, sender=None) -> dict:
        from uuid import uuid4
        from app.tracker.alerts import (
            AlertMessage, DeliveryReceipt, DeliveryUncertainError, ProviderRefusedError, SmtpAlertSender,
        )
        from app.tracker.contracts import utc_now
        with self._guard:
            if not self.configuration_status()['configured']:
                raise ValueError('Configure email before sending a connection test')
            path = self.root / 'mail-test-receipt.json'
            revision = self.path.stat().st_mtime_ns if self.path.exists() else 0
            if path.exists():
                previous = json.loads(path.read_text(encoding='utf-8'))
                if previous.get('config_revision') == revision:
                    if previous.get('status') == 'accepted':
                        return previous
                    if previous.get('status') in {'sending', 'uncertain'}:
                        raise ValueError('Previous test outcome is uncertain; automatic resend refused')
            token = uuid4().hex
            config = self._service.config
            receipt = {'status': 'sending', 'message_id': f'<argus-test-{token}@argus.invalid>',
                       'delivery_id': token, 'created_at': utc_now(), 'config_revision': revision,
                       'inbox_arrival_proven': False,
                       'note': 'Provider acceptance is not proof of inbox arrival.'}

            def persist():
                temporary = path.with_suffix('.tmp')
                with temporary.open('w', encoding='utf-8') as stream:
                    json.dump(receipt, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, path)
            persist()
            message = AlertMessage(event_keys=('connection-test',), roles=(),
                subject='ARGUS tracker — email connection test',
                body='Your ARGUS tracker email transport is connected. This is a connection test, not a job opening or application submission.\n\nMessage-ID: ' + receipt['message_id'],
                message_id=receipt['message_id'], delivery_id=token,
                from_address=config.from_address, to_address=config.to_address)
            try:
                result = (sender or SmtpAlertSender(config)).send(message)
                if not isinstance(result, DeliveryReceipt) or not result.accepted or result.uncertain:
                    raise DeliveryUncertainError('No positive transport receipt')
                receipt['status'] = 'accepted'
            except ProviderRefusedError:
                receipt['status'] = 'failed'
                receipt['note'] = 'Provider refused delivery; check the mail settings.'
            except Exception:
                receipt['status'] = 'uncertain'
                receipt['note'] = 'Delivery could not be confirmed; automatic resend is disabled.'
            persist()
            return receipt

    def run(self, jobs):
        with self._guard:
            return self._service.run(jobs)

    def summary(self):
        with self._guard:
            value = self._service.summary()
            state = value.get('config_status', 'unknown')
            value = {**value, 'status': 'disabled' if state == 'blocked_disabled' else state}
            return value

    def list_events(self, limit=50):
        with self._guard:
            return self._service.list_events(limit=limit)

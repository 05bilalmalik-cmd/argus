"""Adversarial parent-owned regressions. All SMTP objects are fixtures."""
import smtplib
import ssl
import pytest
from app.tracker.alerts import AlertConfig, AlertMessage, AlertService, SmtpAlertSender, DeliveryReceipt


def message():
    return AlertMessage(('fixture',), (), 'Fixture', 'No real message', '<fixture@example.test>',
                        'fixture', 'from@example.test', 'to@example.test')


class SMTPFixture:
    def __init__(self, *args, **kwargs):
        self.context = None
    def __enter__(self): return self
    def __exit__(self, *args): self.quit()
    def ehlo(self): return 250, b'fixture'
    def starttls(self, *, context=None): self.context = context
    def login(self, *args): pass
    def send_message(self, value): return {}
    def quit(self): pass
    def close(self): pass


def test_smtp_uses_certificate_verifying_context(monkeypatch):
    client = SMTPFixture()
    monkeypatch.setattr(smtplib, 'SMTP', lambda *a, **kw: client)
    receipt = SmtpAlertSender(AlertConfig(enabled=True, smtp_host='smtp.example.test')).send(message())
    assert receipt.accepted
    assert client.context is not None and client.context.verify_mode == ssl.CERT_REQUIRED
    assert client.context.check_hostname


def test_quit_failure_does_not_retry_an_already_accepted_message(monkeypatch):
    class QuitFailure(SMTPFixture):
        def quit(self): raise smtplib.SMTPResponseException(500, b'fixture QUIT failure')
    monkeypatch.setattr(smtplib, 'SMTP', QuitFailure)
    assert SmtpAlertSender(AlertConfig(enabled=True, smtp_host='smtp.example.test')).send(message()).accepted


@pytest.mark.parametrize('response', [None, {}, 'looks-like-an-id', {'accepted': 'false'},
    {'accepted': True, 'uncertain': True}, DeliveryReceipt(accepted=True, uncertain=True)])
def test_delivery_requires_an_explicit_unambiguous_receipt(response):
    _, _, uncertain, _ = AlertService._receipt(response)
    assert uncertain, 'Missing or ambiguous provider acknowledgement must not become sent'

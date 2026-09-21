"""Secure mail configuration is tested with fake credentials and no network."""
from pathlib import Path
import json
import pytest


def test_mail_settings_never_publish_password_and_persist_separately(tmp_path):
    from app.tracker.mail_settings import ConfiguredAlerts
    verified = []
    settings = ConfiguredAlerts(tmp_path, credential_check=lambda config: verified.append(config.smtp_host))
    assert settings.summary()['status'] == 'disabled'
    settings.configure({'smtp_host': 'smtp.gmail.com', 'smtp_port': 587,
                        'smtp_user': 'fixture@example.test', 'smtp_password': 'test-password-not-a-secret',
                        'from_address': 'fixture@example.test', 'to_address': 'fixture@example.test'})
    assert verified == ['smtp.gmail.com']
    public = json.dumps(settings.configuration_status())
    assert 'test-password-not-a-secret' not in public
    assert 'smtp_password' not in public
    assert settings.configuration_status()['configured']
    assert ConfiguredAlerts(tmp_path).configuration_status()['configured']
    assert not (tmp_path / 'profile.json').exists()


def test_unverified_credentials_are_not_saved(tmp_path):
    from app.tracker.mail_settings import ConfiguredAlerts
    def reject(config):
        raise RuntimeError('fixture-password-should-never-be-returned')
    settings = ConfiguredAlerts(tmp_path, credential_check=reject)
    with pytest.raises(ValueError, match='not saved') as error:
        settings.configure({'smtp_host': 'smtp.gmail.com', 'smtp_port': 587,
                            'smtp_user': 'fixture@example.test', 'smtp_password': 'fixture-password',
                            'from_address': 'fixture@example.test', 'to_address': 'fixture@example.test'})
    assert 'fixture-password' not in str(error.value)
    assert not settings.configuration_status()['configured']


def test_connection_test_is_idempotent_and_never_invents_inbox_receipt(tmp_path):
    from app.tracker.mail_settings import ConfiguredAlerts
    from app.tracker.alerts import DeliveryReceipt
    class Sender:
        def __init__(self):
            self.calls = 0
        def send(self, message):
            self.calls += 1
            return DeliveryReceipt(accepted=True, delivery_id=message.delivery_id)
    sender = Sender()
    settings = ConfiguredAlerts(tmp_path, credential_check=lambda config: None)
    settings.configure({'smtp_host': 'smtp.gmail.com', 'smtp_port': 587,
                        'smtp_user': 'fixture@example.test', 'smtp_password': 'test-placeholder',
                        'from_address': 'fixture@example.test', 'to_address': 'fixture@example.test'})
    receipt = settings.send_test(sender=sender)
    assert receipt['status'] == 'accepted'
    assert receipt['inbox_arrival_proven'] is False
    assert settings.send_test(sender=sender)['message_id'] == receipt['message_id']
    assert sender.calls == 1

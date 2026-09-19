from types import SimpleNamespace
from unittest.mock import Mock

from kerui_recruit.api import settings as module
from kerui_recruit.core.settings import Settings
from kerui_recruit.core.settings_service import SettingsService
from kerui_recruit.core.settings_store import SettingsStore
from kerui_recruit.encryption.service import EncryptionService


def test_mail_probe_uses_new_saved_settings_and_environment_fallback(tmp_path, monkeypatch):
    service = SettingsService(SettingsStore(tmp_path / 'config/settings.json'), EncryptionService(key_path=str(tmp_path / 'config/encryption.key')))
    service.update({'imap_host': 'new.imap', 'imap_account': 'new-user', 'imap_auth_code': 'new-secret', 'smtp_host': 'new.smtp', 'smtp_ssl': False, 'smtp_port': 587})
    monkeypatch.setenv('SMTP_ACCOUNT', 'env-user')
    monkeypatch.setenv('SMTP_AUTH_CODE', 'env-secret')
    settings = Settings(data_root=tmp_path, session_token='fake', smtp_host='stale.smtp', smtp_account='stale-user', smtp_auth_code='stale-secret')
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(settings=settings, settings_service=service))))
    imap = Mock()
    smtp = Mock()
    import imaplib
    monkeypatch.setattr(imaplib, 'IMAP4_SSL', imap)
    monkeypatch.setattr(module.smtplib, 'SMTP', smtp)
    monkeypatch.setattr(module.smtplib, 'SMTP_SSL', Mock())
    result = module.test_mail(request)
    assert result['imap']['ok'] and result['smtp']['ok']
    imap.assert_called_once_with('new.imap', 993, timeout=15)
    imap.return_value.login.assert_called_once_with('new-user', 'new-secret')
    imap.return_value.logout.assert_called_once()
    smtp.assert_called_once_with('new.smtp', 587, timeout=15)
    smtp.return_value.login.assert_called_once_with('env-user', 'env-secret')
    smtp.return_value.quit.assert_called_once()


def test_mail_probe_closes_clients_after_login_failure(tmp_path, monkeypatch):
    for key, value in {'IMAP_HOST': 'fake.imap', 'IMAP_ACCOUNT': 'fake', 'IMAP_AUTH_CODE': 'fake', 'SMTP_HOST': 'fake.smtp', 'SMTP_ACCOUNT': 'fake', 'SMTP_AUTH_CODE': 'fake'}.items():
        monkeypatch.setenv(key, value)
    settings = Settings(data_root=tmp_path, session_token='fake')
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(services=SimpleNamespace(settings=settings))))
    imap, smtp = Mock(), Mock()
    imap.return_value.login.side_effect = RuntimeError('fake imap failure')
    smtp.return_value.login.side_effect = RuntimeError('fake smtp failure')
    monkeypatch.setattr(module.imaplib, 'IMAP4_SSL', imap)
    monkeypatch.setattr(module.smtplib, 'SMTP_SSL', smtp)
    monkeypatch.setattr(module.smtplib, 'SMTP', smtp)
    result = module.test_mail(request)
    assert not result['imap']['ok'] and not result['smtp']['ok']
    imap.return_value.logout.assert_called_once()
    smtp.return_value.quit.assert_called_once()

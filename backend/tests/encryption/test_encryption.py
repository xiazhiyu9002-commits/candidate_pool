from pathlib import Path

from kerui_recruit.encryption.service import EncryptionService


def test_encrypt_and_decrypt_round_trip(tmp_path: Path) -> None:
    key_path = str(tmp_path / "encryption.key")
    service = EncryptionService(key_path=key_path)

    original = "张三 zhang@test.com 13800138000"
    encrypted = service.encrypt(original)
    assert encrypted != original
    assert isinstance(encrypted, str)

    decrypted = service.decrypt(encrypted)
    assert decrypted == original


def test_key_is_persisted_and_reused(tmp_path: Path) -> None:
    key_path = str(tmp_path / "encryption.key")
    svc1 = EncryptionService(key_path=key_path)
    encrypted = svc1.encrypt("secret data")

    # Create a new service that reuses the same key file
    svc2 = EncryptionService(key_path=key_path)
    assert svc2.decrypt(encrypted) == "secret data"
from cryptography.fernet import Fernet

from app import crypto


def test_encrypt_decrypt_roundtrip(monkeypatch):
    monkeypatch.setattr("app.config.FERNET_KEY", Fernet.generate_key().decode())
    crypto.reset_cache()
    token = crypto.encrypt("super-secret-refresh-token")
    assert token != "super-secret-refresh-token"
    assert crypto.decrypt(token) == "super-secret-refresh-token"
    crypto.reset_cache()


def test_missing_key_raises(monkeypatch):
    monkeypatch.setattr("app.config.FERNET_KEY", "")
    crypto.reset_cache()
    try:
        crypto.encrypt("x")
        assert False, "expected CryptoError"
    except crypto.CryptoError:
        pass
    crypto.reset_cache()


def test_none_passthrough(monkeypatch):
    monkeypatch.setattr("app.config.FERNET_KEY", Fernet.generate_key().decode())
    crypto.reset_cache()
    assert crypto.encrypt(None) is None
    assert crypto.decrypt(None) is None
    crypto.reset_cache()

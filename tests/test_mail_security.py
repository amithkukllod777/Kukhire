from cryptography.fernet import Fernet

from app.mail_security import TokenCipher, WebhookVerifier


def test_token_cipher_round_trip() -> None:
    cipher = TokenCipher(Fernet.generate_key().decode("utf-8"))
    encrypted = cipher.encrypt("refresh-token")

    assert encrypted != "refresh-token"
    assert cipher.decrypt(encrypted) == "refresh-token"


def test_webhook_signature_verification() -> None:
    verifier = WebhookVerifier.from_string("x" * 32)
    body = b'{"message_id":"m1"}'
    signature = verifier.sign(body)

    assert verifier.verify(body, signature) is True
    assert verifier.verify(b"tampered", signature) is False

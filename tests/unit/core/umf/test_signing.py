"""Tests for UMF HMAC-SHA256 message signing and verification.

Covers signing, verification, tamper detection, unsigned message rejection,
and multi-key rotation support.
"""
from __future__ import annotations

from backend.core.umf.signing import (
    sign_message,
    verify_message,
    verify_message_multi_key,
)
from backend.core.umf.types import (
    Kind,
    MessageSource,
    MessageTarget,
    Stream,
    UmfMessage,
)


def _make_source(**overrides) -> MessageSource:
    defaults = dict(
        repo="jarvis-ai-agent",
        component="supervisor",
        instance_id="inst-001",
        session_id="sess-abc",
    )
    defaults.update(overrides)
    return MessageSource(**defaults)


def _make_target(**overrides) -> MessageTarget:
    defaults = dict(repo="reactor-core", component="event_bus")
    defaults.update(overrides)
    return MessageTarget(**defaults)


def _make_msg(**overrides) -> UmfMessage:
    defaults = dict(
        stream=Stream.command,
        kind=Kind.command,
        source=_make_source(),
        target=_make_target(),
        payload={"action": "test"},
    )
    defaults.update(overrides)
    return UmfMessage(**defaults)


class TestUmfSigning:
    """Six tests covering HMAC-SHA256 signing, verification, and key rotation."""

    def test_sign_adds_signature_fields(self) -> None:
        msg = _make_msg()
        signed = sign_message(msg, secret="my-secret", key_id="k1")

        assert signed.signature_alg == "HMAC-SHA256"
        assert signed.signature_key_id == "k1"
        assert signed.signature_value != ""

    def test_verify_valid_signature(self) -> None:
        msg = _make_msg()
        signed = sign_message(msg, secret="my-secret", key_id="k1")

        assert verify_message(signed, secret="my-secret") is True

    def test_verify_invalid_signature(self) -> None:
        msg = _make_msg()
        signed = sign_message(msg, secret="my-secret", key_id="k1")

        assert verify_message(signed, secret="wrong-secret") is False

    def test_verify_tampered_payload_fails(self) -> None:
        msg = _make_msg()
        signed = sign_message(msg, secret="my-secret", key_id="k1")
        signed.payload = {"action": "tampered"}

        assert verify_message(signed, secret="my-secret") is False

    def test_unsigned_message_verify_returns_false(self) -> None:
        msg = _make_msg()

        assert verify_message(msg, secret="my-secret") is False

    def test_key_rotation_accepts_both_keys(self) -> None:
        msg = _make_msg()
        signed = sign_message(msg, secret="old-key", key_id="k1")

        keys = {"k1": "old-key", "k2": "new-key"}
        assert verify_message_multi_key(signed, keys=keys) is True

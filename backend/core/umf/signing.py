"""UMF message signing and verification -- HMAC-SHA256 with key rotation.

Provides deterministic signing of ``UmfMessage`` envelopes.  Signature
fields (``signature_alg``, ``signature_key_id``, ``signature_value``)
are excluded from the signable content so that a signed message can be
verified without stripping those fields first.

Key rotation is supported via ``verify_message_multi_key`` which looks
up the signing secret by the ``signature_key_id`` embedded in the message.

Design rules
------------
* **No** third-party or JARVIS imports -- stdlib only (plus ``UmfMessage``).
* Constant-time comparison via ``hmac.compare_digest``.
* ``sign_message`` returns a *copy* of the original message (no mutation).
"""
from __future__ import annotations

import copy
import hmac
import hashlib
import json
from typing import Dict

from backend.core.umf.types import UmfMessage

# Signature fields excluded from the signable content.
_SIG_FIELDS = frozenset({"signature_alg", "signature_key_id", "signature_value"})


def _signable_content(msg: UmfMessage) -> str:
    """Return deterministic JSON of the message without signature fields."""
    d = msg.to_dict()
    for key in _SIG_FIELDS:
        d.pop(key, None)
    return json.dumps(d, sort_keys=True, separators=(",", ":"))


def sign_message(msg: UmfMessage, secret: str, key_id: str) -> UmfMessage:
    """Return a signed copy of *msg* using HMAC-SHA256.

    The HMAC is computed over the signable content of the **original**
    (unsigned) message.  The returned copy has ``signature_alg``,
    ``signature_key_id``, and ``signature_value`` populated.
    """
    content = _signable_content(msg)
    digest = hmac.new(
        secret.encode("utf-8"),
        content.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    signed = copy.copy(msg)
    signed.signature_alg = "HMAC-SHA256"
    signed.signature_key_id = key_id
    signed.signature_value = digest
    return signed


def verify_message(msg: UmfMessage, secret: str) -> bool:
    """Verify the HMAC-SHA256 signature on *msg*.

    Returns ``False`` if the message has no signature fields set.
    Uses ``hmac.compare_digest`` for constant-time comparison.
    """
    if not msg.signature_alg or not msg.signature_value:
        return False

    content = _signable_content(msg)
    expected = hmac.new(
        secret.encode("utf-8"),
        content.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    return hmac.compare_digest(expected, msg.signature_value)


def verify_message_multi_key(msg: UmfMessage, keys: Dict[str, str]) -> bool:
    """Verify *msg* against a dict of ``{key_id: secret}`` pairs.

    Looks up the secret by ``msg.signature_key_id``.  Returns ``False``
    if the key ID is not found in *keys*.
    """
    secret = keys.get(msg.signature_key_id)
    if secret is None:
        return False
    return verify_message(msg, secret)

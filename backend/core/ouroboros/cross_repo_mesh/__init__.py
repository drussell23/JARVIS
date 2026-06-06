"""Cross-repo distributed event mesh (Slice 97).

PREDICTIONS, NOT REQUESTS: JARVIS asynchronously EMITS a cryptographically
signed NOTIFICATION ("ripple") when its state changes. A consumer repo
INDEPENDENTLY VERIFIES the signature + replay + freshness, then DECIDES
what to do — it NEVER executes JARVIS-dictated remote code.

Two layers:

  * ``ripple_contract`` — PORTABLE, stdlib-only verification contract.
    Vendored verbatim into jarvis-prime + reactor-core (which cannot
    import ``backend.core.ouroboros.*``).  Implements the identical
    HMAC-SHA256 wire format used by ``aegis.lease``.

  * ``ripple_emitter`` — JARVIS-side signer that composes the existing
    crypto substrate and publishes durable, signed receipts.
"""
from __future__ import annotations

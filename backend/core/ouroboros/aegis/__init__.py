"""Aegis — Arc #1 out-of-process egress + budget chokepoint.

§43.7 spine made physical: a separate OS process, minted before any
autonomous code runs, that owns the upstream API credentials and the
authoritative budget ledger.

JARVIS can rewrite anything inside its own process. It cannot mint a
credential it does not have, and it cannot debit a ledger it cannot
reach.

Slice 1 — dark substrate. Master flag ``JARVIS_AEGIS_ENABLED`` default
**false**. Endpoints in Slice 1:

  * ``GET  /health``              — liveness + version + port
  * ``POST /session/establish``   — bootstrap PSK -> scoped session token
  * ``POST /lease/acquire``       — session token -> lease (cap-checked)
  * ``POST /lease/redeem``        — lease + actual cost -> reconciled verdict

Slice 1 contains **no** ``/v1/*`` upstream proxy routes. AST-pinned.
Provider forwarding lands in Slice 2.

This package is the ONLY surface that may hold raw upstream credentials
or the HMAC signing key K. The JARVIS process is unprivileged.
"""
from __future__ import annotations

AEGIS_SCHEMA_VERSION: str = "aegis.1"

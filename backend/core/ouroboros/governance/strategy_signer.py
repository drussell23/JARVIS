"""Slice 202 — Strategy Signer (OPERATOR utility, never autonomous).

Elevates an advisory ``.jarvis/roadmap.yaml`` to SIGNED authenticity by
computing the HMAC the RoadmapReader verifies. This exists so the OPERATOR can
deliberately attest authorship of a set of goals — it is NOT, and must never
be, wired into any boot path. A roadmap the organism signs over its own goals
would be a false authenticity claim and the self-authorization anti-pattern
the cage forbids (operator = zero-order doll, §41.2).

Honest reuse: the signature primitive lives in ``roadmap_reader``
(``compute_signature`` / ``_build_signing_payload``) — this module is a thin
operator-facing wrapper + secret generator + CLI. The operator runs:

    python3 -m backend.core.ouroboros.governance.strategy_signer .jarvis/roadmap.yaml

which prints a freshly generated secret (to place in
``JARVIS_ROADMAP_READER_HMAC_SECRET``) and writes the matching signature into
the file. The secret is shown to the OPERATOR, never persisted autonomously.
"""
from __future__ import annotations

import secrets
from typing import Any, Dict, Mapping, Optional


def generate_secret(nbytes: int = 32) -> str:
    """A strong random HMAC secret for the OPERATOR to install in
    ``JARVIS_ROADMAP_READER_HMAC_SECRET``. NEVER raises."""
    try:
        return secrets.token_hex(max(16, int(nbytes)))
    except Exception:  # noqa: BLE001
        return secrets.token_hex(32)


def sign_roadmap_doc(doc: Mapping[str, Any], secret: str) -> Dict[str, Any]:
    """Return a copy of ``doc`` with a ``signature`` field computed over the
    reader's canonical signing payload. Marks ``signed: true``. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.roadmap_reader import (
            _build_signing_payload, compute_signature,
        )
        out = dict(doc)
        out.pop("signature", None)
        payload = _build_signing_payload(out)
        sig = compute_signature(payload, secret)
        out["signature"] = sig
        out["signed"] = bool(sig)
        return out
    except Exception:  # noqa: BLE001
        return dict(doc)


def _main(argv: Optional[list] = None) -> int:
    import sys
    import json
    from pathlib import Path
    args = list(argv if argv is not None else sys.argv[1:])
    if not args:
        print("usage: python3 -m ...strategy_signer <roadmap.yaml> [secret]")
        return 2
    path = Path(args[0])
    secret = args[1] if len(args) > 1 else generate_secret()
    try:
        import yaml  # type: ignore
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        doc = json.loads(path.read_text(encoding="utf-8"))
    signed = sign_roadmap_doc(doc, secret)
    try:
        import yaml  # type: ignore
        path.write_text(
            yaml.safe_dump(signed, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
    except Exception:  # noqa: BLE001
        path.write_text(json.dumps(signed, indent=2), encoding="utf-8")
    print(f"signed {path}")
    print("OPERATOR ACTION — set this in your environment / .env:")
    print(f"  export JARVIS_ROADMAP_READER_HMAC_SECRET={secret}")
    print("  export JARVIS_ROADMAP_READER_REQUIRE_SIGNATURE=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

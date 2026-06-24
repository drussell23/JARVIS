"""Immutable, hash-chained audit ledger for the Biometric Edge-Gate.

Every authorization attempt -- AUTHORIZED **and** REJECTED -- appends an
immutable record binding *which voice-print authorized which AST
mutation*. The records are cryptographically chained:

    record_hash = sha256(prev_record_hash + canonical(payload))

Tampering with any past record (or deleting / reordering a record)
breaks the chain, which :meth:`verify_chain` detects.

Reuses the durable-JSONL append discipline of
``adaptation/graduation_ledger.py`` (append-only, separators=(",", ":"),
fail-soft writes that log loudly) -- but kept self-contained so this
security-critical module imports cleanly in a bare test env (no heavy
governance imports at module load).

The record schema NEVER contains the raw audio -- only ``audio_sha256``.

Append-only at ``.jarvis/command_node_audit.jsonl``.

M2 (audit-then-approve ordering)
=================================
``append`` now accepts ``raise_on_write_failure=True`` (default False).
The AUTHORIZED path in BiometricAuthMiddleware calls append with
raise_on_write_failure=True BEFORE calling approve_fn -- so no merge can
outrun its immutable record. If the durable write (fsync confirmed) fails,
``AuditWriteError`` is raised, the middleware catches it, rejects the
authorization, and NEVER calls approve_fn. REJECTED outcomes always use
the default fail-soft mode (never raise).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("CommandNode.AuditLedger")

# Genesis: the prev_hash of the very first record. A fixed sentinel so
# an empty ledger has a well-defined chain root.
GENESIS_HASH = "0" * 64

# The ordered, canonical payload fields hashed into the chain. Order is
# load-bearing -- it is part of the canonicalization. ``prev_hash`` and
# ``record_hash`` are chain metadata, NOT part of the hashed payload.
_PAYLOAD_FIELDS = (
    "ts",
    "pr_id",
    "target_repo",
    "ast_mutation_id",
    "blast_radius_hash",
    "challenge_nonce",
    "voiceprint_id",
    "ecapa_score",
    "antispoof_verdict",
    "freshness_ok",
    "decision",
    "audio_sha256",
    # Phase 3 -- Biometric-Semantic Binding evidence. The transcript is
    # NEVER persisted -- only its sha256 (``transcript_hash``). Added to the
    # hashed payload so the WER + phrase verdict are tamper-evident too.
    "wer",
    "transcript_hash",
    "phrase_match_ok",
)


class AuditWriteError(Exception):
    """Raised by :meth:`BiometricAuditLedger.append` when
    ``raise_on_write_failure=True`` and the durable write (fsync
    confirmed) fails. Used by the middleware to enforce the
    audit-then-approve ordering invariant: no merge can outrun its
    immutable record."""


def _default_audit_path() -> Path:
    """Resolve the ledger path. Env-overridable; default
    ``.jarvis/command_node_audit.jsonl`` under the cwd (matches the
    graduation-ledger / posture artifact convention)."""
    raw = os.environ.get("JARVIS_COMMAND_NODE_AUDIT_PATH", "").strip()
    if raw:
        return Path(raw)
    return Path(".jarvis") / "command_node_audit.jsonl"


def _canonical_payload(record: Dict[str, Any]) -> str:
    """Deterministic canonical serialization of the hashed payload.

    Only the ``_PAYLOAD_FIELDS`` participate -- in fixed order -- so the
    hash is stable and independent of dict insertion order. Booleans /
    numbers / strings serialize via ``json.dumps`` with compact
    separators + ``sort_keys`` for total determinism.
    """
    ordered = {k: record.get(k) for k in _PAYLOAD_FIELDS}
    return json.dumps(ordered, separators=(",", ":"), sort_keys=True)


def compute_record_hash(prev_hash: str, record: Dict[str, Any]) -> str:
    """``sha256(prev_hash + canonical(payload))`` -- the chain link."""
    h = hashlib.sha256()
    h.update((prev_hash or GENESIS_HASH).encode("utf-8"))
    h.update(_canonical_payload(record).encode("utf-8"))
    return h.hexdigest()


class BiometricAuditLedger:
    """Append-only, hash-chained JSONL audit ledger.

    Construction reads the existing tail (if any) to recover the
    last ``record_hash`` so a new process continues the chain. Bounded:
    only the last record's hash is needed to append; ``verify_chain``
    streams the whole file but caps the records loaded.
    """

    # Defensive cap on records replayed during verify (a forged file
    # can't force unbounded work).
    MAX_RECORDS = 1_000_000

    def __init__(self, *, path: Optional[Path] = None) -> None:
        self.path: Path = Path(path) if path is not None else _default_audit_path()

    # --- internal ---------------------------------------------------------

    def _read_last_hash(self) -> str:
        """Return the ``record_hash`` of the last record on disk, or
        ``GENESIS_HASH`` for an empty / absent / unreadable ledger."""
        try:
            if not self.path.exists():
                return GENESIS_HASH
            last = None
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        last = line
            if last is None:
                return GENESIS_HASH
            rec = json.loads(last)
            rh = rec.get("record_hash")
            return rh if isinstance(rh, str) and rh else GENESIS_HASH
        except Exception:  # noqa: BLE001 -- fail-soft, never raise
            logger.warning(
                "[CommandNodeAudit] could not read last hash from %s -- "
                "starting from genesis (chain continuity may break)",
                self.path,
                exc_info=True,
            )
            return GENESIS_HASH

    # --- public API -------------------------------------------------------

    def append(
        self,
        payload: Dict[str, Any],
        *,
        raise_on_write_failure: bool = False,
    ) -> Dict[str, Any]:
        """Append a hash-chained record. Returns the full record dict
        (including ``ts`` / ``prev_hash`` / ``record_hash``).

        Default (``raise_on_write_failure=False``): fail-soft -- a write
        failure is logged LOUDLY but never raises. The record (with its
        hash) is still returned so the caller can mirror it elsewhere.

        When ``raise_on_write_failure=True`` (used by the AUTHORIZED path
        in BiometricAuthMiddleware): raises :exc:`AuditWriteError` if the
        record cannot be durably written (fsync confirmed). This enforces
        the audit-then-approve ordering invariant: the middleware gates
        ``approve_fn`` behind a confirmed audit write so no merge can
        outrun its immutable record.

        REJECTED outcomes ALWAYS call ``append`` with the default fail-soft
        mode (``raise_on_write_failure=False``) so they never raise.
        """
        record: Dict[str, Any] = {}
        # Stamp ts first so it participates in the canonical payload.
        record["ts"] = payload.get("ts") or _now_iso()
        for k in _PAYLOAD_FIELDS:
            if k == "ts":
                continue
            record[k] = payload.get(k)

        prev_hash = self._read_last_hash()
        record["prev_hash"] = prev_hash
        record["record_hash"] = compute_record_hash(prev_hash, record)

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record, separators=(",", ":"), sort_keys=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[CommandNodeAudit] FAILED to persist audit record for "
                "pr_id=%r decision=%r -- record computed but NOT durably "
                "written (path=%s)",
                record.get("pr_id"),
                record.get("decision"),
                self.path,
                exc_info=True,
            )
            if raise_on_write_failure:
                raise AuditWriteError(
                    "audit durable-write failed for pr_id=" + repr(record.get("pr_id")) + ": " + str(exc)
                ) from exc
        return record

    def verify_chain(self) -> bool:
        """Recompute the whole chain from genesis; return ``True`` iff
        every link is intact (tamper-evident). Empty / absent ledger ->
        ``True`` (a vacuously valid chain). Any parse error / hash
        mismatch / broken link -> ``False``."""
        try:
            if not self.path.exists():
                return True
            prev = GENESIS_HASH
            count = 0
            with self.path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    count += 1
                    if count > self.MAX_RECORDS:
                        logger.error(
                            "[CommandNodeAudit] ledger exceeds MAX_RECORDS "
                            "-- refusing to verify (treat as broken)",
                        )
                        return False
                    rec = json.loads(line)
                    if rec.get("prev_hash") != prev:
                        return False
                    expected = compute_record_hash(prev, rec)
                    if rec.get("record_hash") != expected:
                        return False
                    prev = rec["record_hash"]
            return True
        except Exception:  # noqa: BLE001 -- any error -> NOT verified
            logger.warning(
                "[CommandNodeAudit] verify_chain failed on %s",
                self.path,
                exc_info=True,
            )
            return False

    def records(self, *, limit: int = 200) -> List[Dict[str, Any]]:
        """Read-only tail projection for the AuditLedgerView. Returns
        the last ``limit`` records (oldest-first). Fail-soft -> []."""
        try:
            if not self.path.exists():
                return []
            limit = max(1, min(10_000, int(limit)))
            with self.path.open("r", encoding="utf-8") as fh:
                lines = [ln.strip() for ln in fh if ln.strip()]
            out: List[Dict[str, Any]] = []
            for ln in lines[-limit:]:
                try:
                    out.append(json.loads(ln))
                except (TypeError, ValueError):
                    continue
            return out
        except Exception:  # noqa: BLE001 -- fail-soft
            logger.warning(
                "[CommandNodeAudit] records() read failed", exc_info=True,
            )
            return []


def _now_iso() -> str:
    """UTC ISO-8601 timestamp (stamped from now -- no Date.now break)."""
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# Process-default singleton (lazy).
_DEFAULT_LEDGER: Optional[BiometricAuditLedger] = None


def get_default_ledger() -> BiometricAuditLedger:
    global _DEFAULT_LEDGER
    if _DEFAULT_LEDGER is None:
        _DEFAULT_LEDGER = BiometricAuditLedger()
    return _DEFAULT_LEDGER


__all__ = [
    "AuditWriteError",
    "GENESIS_HASH",
    "BiometricAuditLedger",
    "compute_record_hash",
    "get_default_ledger",
]

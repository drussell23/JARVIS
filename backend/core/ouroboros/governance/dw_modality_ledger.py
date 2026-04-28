"""Phase 12 Slice G — Dynamic Capability Verdict Ledger.

Persistent per-model_id modality state. Three verdicts:

  * ``CHAT_CAPABLE`` — model verified (via metadata or micro-probe) to
    accept ``/chat/completions``. Eligible for generative routes.
  * ``NON_CHAT`` — observed 4xx modality response from /chat/completions.
    PERMANENTLY excluded from generative routes until next full catalog
    refresh resets the ledger.
  * ``UNKNOWN`` — newly discovered, metadata ambiguous, no probe yet.
    SPECULATIVE quarantine only (Zero-Trust §3.6 pattern reused).

Operator-mandated 2026-04-27: NON_CHAT verdicts MUST come from
ground-truth signals only:
  1. Explicit metadata flag from DW's /models response (capabilities /
     architecture / task fields)
  2. Server-observed 4xx modality response with body marker

Regex pattern-matching on model_id is strictly forbidden. The ledger
neither stores nor reads model name strings for inference — verdicts
come from one of the two ground-truth sources above.

Persistence: ``.jarvis/dw_modality_ledger.json``, atomic temp+rename
mirrored from ``posture_store.py``. Survives restart. Ledger version
field allows full reset on schema or catalog snapshot change.

NEVER raises out of any public method. Defensive try/except guards
all input paths so a malformed verdict can't take down the dispatcher.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag + tunables
# ---------------------------------------------------------------------------


def modality_verification_enabled() -> bool:
    """``JARVIS_DW_MODALITY_VERIFICATION_ENABLED`` (default ``false``).

    Re-read at call time so monkeypatch works in tests + operators
    can flip live. Default flips to ``true`` at Slice G graduation
    after the once-proof confirms the probe + ledger flow."""
    raw = os.environ.get(
        "JARVIS_DW_MODALITY_VERIFICATION_ENABLED", "",
    ).strip().lower()
    return raw in ("1", "true", "yes", "on")


def _ledger_path() -> Path:
    """``JARVIS_DW_MODALITY_LEDGER_PATH`` (default
    ``.jarvis/dw_modality_ledger.json``). Override for tests."""
    raw = os.environ.get(
        "JARVIS_DW_MODALITY_LEDGER_PATH",
        ".jarvis/dw_modality_ledger.json",
    ).strip()
    return Path(raw)


# ---------------------------------------------------------------------------
# Verdict enum (string constants, not Enum, for trivial JSON round-trip)
# ---------------------------------------------------------------------------


VERDICT_CHAT_CAPABLE = "CHAT_CAPABLE"
VERDICT_NON_CHAT = "NON_CHAT"
VERDICT_UNKNOWN = "UNKNOWN"
_VALID_VERDICTS = frozenset({
    VERDICT_CHAT_CAPABLE,
    VERDICT_NON_CHAT,
    VERDICT_UNKNOWN,
})


# How a verdict was reached (provenance — operator-readable audit trail).
SOURCE_METADATA = "metadata"          # capability flag in /models response
SOURCE_PROBE_4XX = "probe_4xx"        # observed 4xx modality response
SOURCE_PROBE_2XX = "probe_2xx"        # micro-probe returned 200 OK
SOURCE_DISPATCH_4XX = "dispatch_4xx"  # observed during real dispatch
SOURCE_OPERATOR = "operator"          # explicit operator override
_VALID_SOURCES = frozenset({
    SOURCE_METADATA, SOURCE_PROBE_4XX, SOURCE_PROBE_2XX,
    SOURCE_DISPATCH_4XX, SOURCE_OPERATOR,
})


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


LEDGER_SCHEMA_VERSION = "dw_modality.1"


@dataclass
class ModalityRecord:
    """Per-model verdict + provenance + first-observed timestamp.

    Mutable — the ledger owns lifecycle. Snapshot copies returned to
    consumers via ``snapshot()`` are frozen views."""
    model_id: str
    verdict: str
    source: str                              # SOURCE_* constant
    response_body_excerpt: str = ""          # ground-truth marker text
    catalog_snapshot_id: str = ""            # invalidates on catalog refresh
    first_seen_unix: float = field(default_factory=time.time)
    last_event_unix: float = field(default_factory=time.time)

    def snapshot(self) -> "ModalityRecordSnapshot":
        return ModalityRecordSnapshot(
            model_id=self.model_id,
            verdict=self.verdict,
            source=self.source,
            response_body_excerpt=self.response_body_excerpt,
            catalog_snapshot_id=self.catalog_snapshot_id,
            first_seen_unix=self.first_seen_unix,
            last_event_unix=self.last_event_unix,
        )

    def to_json_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "verdict": self.verdict,
            "source": self.source,
            "response_body_excerpt": self.response_body_excerpt,
            "catalog_snapshot_id": self.catalog_snapshot_id,
            "first_seen_unix": self.first_seen_unix,
            "last_event_unix": self.last_event_unix,
        }

    @classmethod
    def from_json_dict(cls, raw: Mapping[str, Any]) -> Optional["ModalityRecord"]:
        try:
            mid = str(raw.get("model_id", "")).strip()
            if not mid:
                return None
            verdict = str(raw.get("verdict", VERDICT_UNKNOWN))
            if verdict not in _VALID_VERDICTS:
                verdict = VERDICT_UNKNOWN
            source = str(raw.get("source", SOURCE_METADATA))
            if source not in _VALID_SOURCES:
                source = SOURCE_METADATA
            return cls(
                model_id=mid,
                verdict=verdict,
                source=source,
                response_body_excerpt=str(
                    raw.get("response_body_excerpt", ""),
                )[:512],
                catalog_snapshot_id=str(raw.get("catalog_snapshot_id", "")),
                first_seen_unix=float(
                    raw.get("first_seen_unix", time.time()) or time.time(),
                ),
                last_event_unix=float(
                    raw.get("last_event_unix", time.time()) or time.time(),
                ),
            )
        except Exception:  # noqa: BLE001 — defensive
            return None


@dataclass(frozen=True)
class ModalityRecordSnapshot:
    """Frozen, hashable view of a ModalityRecord."""
    model_id: str
    verdict: str
    source: str
    response_body_excerpt: str
    catalog_snapshot_id: str
    first_seen_unix: float
    last_event_unix: float


# ---------------------------------------------------------------------------
# Atomic disk I/O (mirrored from posture_store.py)
# ---------------------------------------------------------------------------


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# Ledger
# ---------------------------------------------------------------------------


class ModalityLedger:
    """Persistent per-model_id modality verdict tracker.

    Thread-safe via ``RLock``. Mutating methods write through to disk
    so verdicts survive process restart. Read methods return immutable
    snapshots.

    Lifecycle of a model:

       newly discovered with metadata.capabilities.chat=true
           ↓ (caller invokes record_metadata_verdict)
       CHAT_CAPABLE (source=metadata)

       OR newly discovered with no/ambiguous metadata
           ↓ (caller invokes register_unknown)
       UNKNOWN
           ↓ (caller invokes micro-probe → record_probe_result)
       CHAT_CAPABLE | NON_CHAT (source=probe_*)

       OR observed 4xx modality during real dispatch
           ↓ (caller invokes record_dispatch_modality_failure)
       NON_CHAT (source=dispatch_4xx)

    Reset paths:
      * full catalog refresh detected → clear stale verdicts whose
        catalog_snapshot_id doesn't match current
      * operator override → ``override_verdict`` (manual demote/promote)
    """

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        autosave: bool = True,
    ) -> None:
        self._path = path
        self._autosave = autosave
        self._records: Dict[str, ModalityRecord] = {}
        self._lock = threading.RLock()
        self._loaded = False

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _resolved_path(self) -> Path:
        return self._path if self._path is not None else _ledger_path()

    def load(self) -> None:
        """Load from disk. Missing file = empty ledger; corrupt =
        log warn + start empty. NEVER raises."""
        with self._lock:
            self._loaded = True
            p = self._resolved_path()
            if not p.exists():
                return
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "[ModalityLedger] corrupt or unreadable ledger at %s — "
                    "starting empty (%s)", p, exc,
                )
                return
            if not isinstance(payload, Mapping):
                return
            if payload.get("schema_version") != LEDGER_SCHEMA_VERSION:
                logger.warning(
                    "[ModalityLedger] schema mismatch at %s "
                    "(found=%r expected=%r) — starting empty",
                    p, payload.get("schema_version"), LEDGER_SCHEMA_VERSION,
                )
                return
            records_raw = payload.get("records", [])
            if not isinstance(records_raw, list):
                return
            loaded = 0
            for r in records_raw:
                if not isinstance(r, Mapping):
                    continue
                rec = ModalityRecord.from_json_dict(r)
                if rec is not None:
                    self._records[rec.model_id] = rec
                    loaded += 1
            logger.info(
                "[ModalityLedger] loaded %d record(s) from %s "
                "(chat=%d non_chat=%d unknown=%d)",
                loaded, p,
                sum(1 for r in self._records.values()
                    if r.verdict == VERDICT_CHAT_CAPABLE),
                sum(1 for r in self._records.values()
                    if r.verdict == VERDICT_NON_CHAT),
                sum(1 for r in self._records.values()
                    if r.verdict == VERDICT_UNKNOWN),
            )

    def save(self) -> None:
        """Write current state to disk atomically. NEVER raises."""
        with self._lock:
            payload = {
                "schema_version": LEDGER_SCHEMA_VERSION,
                "records": [
                    rec.to_json_dict() for rec in self._records.values()
                ],
            }
            try:
                _atomic_write(
                    self._resolved_path(),
                    json.dumps(payload, sort_keys=True, indent=2),
                )
            except OSError as exc:
                logger.warning(
                    "[ModalityLedger] save failed: %s — "
                    "ledger remains in memory", exc,
                )

    def _maybe_autosave(self) -> None:
        if self._autosave:
            self.save()

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    # ------------------------------------------------------------------
    # Verdict recording (5 sources)
    # ------------------------------------------------------------------

    def record_metadata_verdict(
        self,
        model_id: str,
        *,
        is_chat_capable: bool,
        catalog_snapshot_id: str = "",
    ) -> None:
        """Record a verdict from explicit /models metadata. Idempotent —
        re-recording the same verdict updates last_event_unix. NEVER
        raises on bad input."""
        if not model_id or not model_id.strip():
            return
        verdict = (
            VERDICT_CHAT_CAPABLE if is_chat_capable else VERDICT_NON_CHAT
        )
        self._upsert(
            model_id=model_id,
            verdict=verdict,
            source=SOURCE_METADATA,
            response_body_excerpt="",
            catalog_snapshot_id=catalog_snapshot_id,
        )

    def register_unknown(
        self,
        model_id: str,
        *,
        catalog_snapshot_id: str = "",
    ) -> None:
        """Mark a newly-discovered model as UNKNOWN until a probe
        resolves it. Idempotent — does NOT overwrite an existing
        CHAT_CAPABLE / NON_CHAT verdict (those are sticky)."""
        if not model_id or not model_id.strip():
            return
        self._ensure_loaded()
        with self._lock:
            existing = self._records.get(model_id)
            if existing is not None and existing.verdict in (
                VERDICT_CHAT_CAPABLE, VERDICT_NON_CHAT,
            ):
                # Don't downgrade existing verdict
                return
            self._upsert(
                model_id=model_id,
                verdict=VERDICT_UNKNOWN,
                source=SOURCE_METADATA,  # placeholder; probe will overwrite
                response_body_excerpt="",
                catalog_snapshot_id=catalog_snapshot_id,
            )

    def record_probe_result(
        self,
        model_id: str,
        *,
        is_chat_capable: bool,
        response_body_excerpt: str = "",
        catalog_snapshot_id: str = "",
    ) -> None:
        """Record verdict from the modality micro-probe. NON_CHAT
        outcomes carry the response body excerpt for operator audit."""
        if not model_id or not model_id.strip():
            return
        verdict = (
            VERDICT_CHAT_CAPABLE if is_chat_capable else VERDICT_NON_CHAT
        )
        source = SOURCE_PROBE_2XX if is_chat_capable else SOURCE_PROBE_4XX
        self._upsert(
            model_id=model_id,
            verdict=verdict,
            source=source,
            response_body_excerpt=response_body_excerpt,
            catalog_snapshot_id=catalog_snapshot_id,
        )

    def record_dispatch_modality_failure(
        self,
        model_id: str,
        *,
        response_body_excerpt: str = "",
        catalog_snapshot_id: str = "",
    ) -> None:
        """A real dispatch returned a 4xx modality error. Demote to
        NON_CHAT immediately. This is the strongest signal — the model
        was actually invoked and the server itself rejected the
        chat-completions payload."""
        if not model_id or not model_id.strip():
            return
        self._upsert(
            model_id=model_id,
            verdict=VERDICT_NON_CHAT,
            source=SOURCE_DISPATCH_4XX,
            response_body_excerpt=response_body_excerpt,
            catalog_snapshot_id=catalog_snapshot_id,
        )

    def override_verdict(
        self,
        model_id: str,
        *,
        verdict: str,
        catalog_snapshot_id: str = "",
    ) -> bool:
        """Operator-mandated override. Returns True if state changed.
        NEVER raises."""
        if not model_id or not model_id.strip():
            return False
        if verdict not in _VALID_VERDICTS:
            return False
        self._ensure_loaded()
        with self._lock:
            existing = self._records.get(model_id)
            if existing is not None and existing.verdict == verdict:
                return False
            self._upsert(
                model_id=model_id,
                verdict=verdict,
                source=SOURCE_OPERATOR,
                response_body_excerpt="",
                catalog_snapshot_id=catalog_snapshot_id,
            )
            return True

    def _upsert(
        self,
        *,
        model_id: str,
        verdict: str,
        source: str,
        response_body_excerpt: str,
        catalog_snapshot_id: str,
    ) -> None:
        """Internal — common write path. NEVER raises."""
        self._ensure_loaded()
        with self._lock:
            existing = self._records.get(model_id)
            now = time.time()
            if existing is None:
                self._records[model_id] = ModalityRecord(
                    model_id=model_id,
                    verdict=verdict,
                    source=source,
                    response_body_excerpt=response_body_excerpt[:512],
                    catalog_snapshot_id=catalog_snapshot_id,
                    first_seen_unix=now,
                    last_event_unix=now,
                )
            else:
                existing.verdict = verdict
                existing.source = source
                if response_body_excerpt:
                    existing.response_body_excerpt = (
                        response_body_excerpt[:512]
                    )
                if catalog_snapshot_id:
                    existing.catalog_snapshot_id = catalog_snapshot_id
                existing.last_event_unix = now
            self._maybe_autosave()

    # ------------------------------------------------------------------
    # Catalog refresh — invalidate stale verdicts
    # ------------------------------------------------------------------

    def reset_for_catalog_refresh(
        self,
        new_snapshot_id: str,
    ) -> int:
        """Drop verdicts whose catalog_snapshot_id doesn't match the
        current snapshot. Models that have re-appeared under the new
        snapshot will be re-evaluated by the next discovery cycle.

        Models with verdicts pinned to an empty catalog_snapshot_id
        (legacy or operator overrides) are PRESERVED — those are
        intentionally cross-snapshot.

        Returns the number of records dropped. NEVER raises."""
        if not new_snapshot_id:
            return 0
        self._ensure_loaded()
        with self._lock:
            to_drop = [
                mid for mid, rec in self._records.items()
                if rec.catalog_snapshot_id
                and rec.catalog_snapshot_id != new_snapshot_id
                and rec.source != SOURCE_OPERATOR
            ]
            for mid in to_drop:
                del self._records[mid]
            if to_drop:
                logger.info(
                    "[ModalityLedger] catalog refresh dropped %d stale "
                    "verdict(s); new snapshot_id=%s",
                    len(to_drop), new_snapshot_id[:16],
                )
                self._maybe_autosave()
            return len(to_drop)

    # ------------------------------------------------------------------
    # Read API
    # ------------------------------------------------------------------

    def verdict_for(self, model_id: str) -> str:
        """Return the current verdict for ``model_id``, or
        ``VERDICT_UNKNOWN`` for never-seen models. NEVER raises."""
        if not model_id or not model_id.strip():
            return VERDICT_UNKNOWN
        self._ensure_loaded()
        with self._lock:
            rec = self._records.get(model_id)
            return rec.verdict if rec is not None else VERDICT_UNKNOWN

    def is_chat_capable(self, model_id: str) -> bool:
        return self.verdict_for(model_id) == VERDICT_CHAT_CAPABLE

    def is_non_chat(self, model_id: str) -> bool:
        return self.verdict_for(model_id) == VERDICT_NON_CHAT

    def is_unknown(self, model_id: str) -> bool:
        return self.verdict_for(model_id) == VERDICT_UNKNOWN

    def chat_capable_models(self) -> Tuple[str, ...]:
        self._ensure_loaded()
        with self._lock:
            return tuple(sorted(
                mid for mid, rec in self._records.items()
                if rec.verdict == VERDICT_CHAT_CAPABLE
            ))

    def non_chat_models(self) -> Tuple[str, ...]:
        self._ensure_loaded()
        with self._lock:
            return tuple(sorted(
                mid for mid, rec in self._records.items()
                if rec.verdict == VERDICT_NON_CHAT
            ))

    def unknown_models(self) -> Tuple[str, ...]:
        self._ensure_loaded()
        with self._lock:
            return tuple(sorted(
                mid for mid, rec in self._records.items()
                if rec.verdict == VERDICT_UNKNOWN
            ))

    def snapshot(self, model_id: str) -> Optional[ModalityRecordSnapshot]:
        if not model_id or not model_id.strip():
            return None
        self._ensure_loaded()
        with self._lock:
            rec = self._records.get(model_id)
            return rec.snapshot() if rec is not None else None

    def all_snapshots(self) -> Tuple[ModalityRecordSnapshot, ...]:
        self._ensure_loaded()
        with self._lock:
            return tuple(rec.snapshot() for rec in self._records.values())


__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "ModalityLedger",
    "ModalityRecord",
    "ModalityRecordSnapshot",
    "SOURCE_DISPATCH_4XX",
    "SOURCE_METADATA",
    "SOURCE_OPERATOR",
    "SOURCE_PROBE_2XX",
    "SOURCE_PROBE_4XX",
    "VERDICT_CHAT_CAPABLE",
    "VERDICT_NON_CHAT",
    "VERDICT_UNKNOWN",
    "modality_verification_enabled",
]

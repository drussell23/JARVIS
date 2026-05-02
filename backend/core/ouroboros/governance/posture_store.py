"""PostureStore — durable current-reading + history ring buffer + override audit.

Four on-disk artifacts under ``.jarvis/``:

  * ``posture_current.json``  — latest PostureReading, atomically written
    via temp+rename so readers never see a torn write.
  * ``posture_history.jsonl`` — ring buffer of the last N readings, one
    JSON object per line. Trimmed in-place on write.
  * ``posture_audit.jsonl``   — append-only log of ``/posture override``
    operations (set / clear / expired). Dedicated file per §8 so the
    agentic side of the system can never alter its own posture logs by
    touching the current-state file.
  * ``posture_change_marker.json`` — companion to ``current``: timestamp
    at which the *posture* (not just the reading) became authoritative.
    Refreshed atomically with ``write_current`` only on real transitions
    so same-posture refreshes don't reset the hysteresis window.
    Survives process restarts so the PostureObserver doesn't lose its
    hysteresis state and falsely lock into a 15-minute blackout after
    every reboot. Posture-mismatch invariant: if the marker's recorded
    posture doesn't match ``current``'s posture, the marker is rejected
    on read and the observer falls back to the legacy ``inferred_at``
    proxy (cold-start safety net).

Schema discipline:
  Every written payload carries ``schema_version="1.0"``. Readers reject
  mismatched versions with a warning and treat the state as cold-start
  rather than coerce — same pattern as SemanticIndex cache.

Authority invariant (grep-pinned in Slice 4):
  This module imports nothing from ``orchestrator``, ``policy``,
  ``iron_gate``, ``risk_tier``, ``change_engine``, ``candidate_generator``,
  or ``gate``. Pure disk I/O + dataclass round-trip.

Concurrency:
  A ``threading.Lock`` guards the three-file triplet. Rotation is not
  coordinated with external readers (e.g. the IDE observability GET) —
  readers take a snapshot of the current file and tolerate tail writes.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from backend.core.ouroboros.governance.posture import (
    Posture,
    PostureReading,
    SCHEMA_VERSION,
    SignalContribution,
)

logger = logging.getLogger(__name__)


POSTURE_STORE_SCHEMA = SCHEMA_VERSION


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def default_history_size() -> int:
    return max(16, _env_int("JARVIS_POSTURE_HISTORY_SIZE", 256, minimum=16))


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


def reading_to_json(reading: PostureReading) -> Dict[str, Any]:
    """Structured JSON round-trip form of a PostureReading."""
    return reading.to_dict()


def reading_from_json(payload: Dict[str, Any]) -> Optional[PostureReading]:
    """Inverse of ``reading_to_json``. Returns ``None`` on schema mismatch
    or malformed shape (caller treats as cold-start)."""
    try:
        if payload.get("schema_version") != POSTURE_STORE_SCHEMA:
            logger.warning(
                "[PostureStore] schema mismatch: got %r, want %r; treating as cold-start",
                payload.get("schema_version"), POSTURE_STORE_SCHEMA,
            )
            return None
        posture = Posture.from_str(payload["posture"])
        evidence: List[SignalContribution] = []
        for raw in payload.get("evidence", []):
            evidence.append(
                SignalContribution(
                    signal_name=str(raw["signal_name"]),
                    raw_value=float(raw["raw_value"]),
                    normalized=float(raw["normalized"]),
                    weight=float(raw["weight"]),
                    contributed_to=Posture.from_str(raw["contributed_to"]),
                    contribution_score=float(raw["contribution_score"]),
                )
            )
        all_scores: List[Tuple[Posture, float]] = []
        for p_name, score in payload.get("all_scores", []):
            all_scores.append((Posture.from_str(p_name), float(score)))
        return PostureReading(
            posture=posture,
            confidence=float(payload["confidence"]),
            evidence=tuple(evidence),
            inferred_at=float(payload["inferred_at"]),
            signal_bundle_hash=str(payload["signal_bundle_hash"]),
            all_scores=tuple(all_scores),
        )
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("[PostureStore] malformed reading payload: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Override record (audit)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OverrideRecord:
    """One entry in posture_audit.jsonl."""

    event: str  # "set" | "clear" | "expired"
    posture: Optional[Posture]
    who: str
    at: float
    until: Optional[float]
    reason: str
    schema_version: str = POSTURE_STORE_SCHEMA

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event": self.event,
            "posture": self.posture.value if self.posture is not None else None,
            "who": self.who,
            "at": self.at,
            "until": self.until,
            "reason": self.reason,
            "schema_version": self.schema_version,
        }


# ---------------------------------------------------------------------------
# PostureStore
# ---------------------------------------------------------------------------


class PostureStore:
    """Durable posture state triplet: current + history + override audit."""

    CURRENT_FILENAME = "posture_current.json"
    HISTORY_FILENAME = "posture_history.jsonl"
    AUDIT_FILENAME = "posture_audit.jsonl"
    CHANGE_MARKER_FILENAME = "posture_change_marker.json"

    def __init__(
        self,
        base_dir: Path,
        *,
        history_size: Optional[int] = None,
    ) -> None:
        self._base = Path(base_dir).resolve()
        self._history_size = history_size if history_size is not None else default_history_size()
        self._lock = threading.Lock()

    @property
    def base_dir(self) -> Path:
        return self._base

    @property
    def current_path(self) -> Path:
        return self._base / self.CURRENT_FILENAME

    @property
    def history_path(self) -> Path:
        return self._base / self.HISTORY_FILENAME

    @property
    def audit_path(self) -> Path:
        return self._base / self.AUDIT_FILENAME

    @property
    def change_marker_path(self) -> Path:
        return self._base / self.CHANGE_MARKER_FILENAME

    # ---- current ----------------------------------------------------------

    def _atomic_write(self, path: Path, text: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        # tempfile in same dir → rename is atomic on POSIX
        fd, tmp_name = tempfile.mkstemp(
            prefix=path.name + ".", suffix=".tmp", dir=str(path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(text)
            os.replace(tmp_name, path)
        except Exception:
            # Clean up partial temp on failure
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    def write_current(
        self,
        reading: PostureReading,
        *,
        change_marker_at: Optional[float] = None,
    ) -> None:
        """Atomically persist the latest reading.

        ``change_marker_at`` (optional): when supplied, ALSO refreshes the
        change-marker side-car with this timestamp + the reading's posture.
        Pass it ONLY on actual posture transitions (or cold-start) — passing
        ``None`` on same-posture refreshes preserves the existing marker so
        the hysteresis window isn't reset every cycle. Both files are
        written atomically (temp+rename) under the same lock acquisition,
        so a reader never sees a marker pointing at the wrong posture so
        long as the writer uses this API."""
        payload = reading_to_json(reading)
        text = json.dumps(payload, indent=2, sort_keys=True)
        with self._lock:
            self._atomic_write(self.current_path, text)
            if change_marker_at is not None:
                marker_payload = {
                    "schema_version": POSTURE_STORE_SCHEMA,
                    "posture": reading.posture.value,
                    "change_marker_at": float(change_marker_at),
                }
                self._atomic_write(
                    self.change_marker_path,
                    json.dumps(marker_payload, indent=2, sort_keys=True),
                )

    def load_current(self) -> Optional[PostureReading]:
        """Return the latest reading, or ``None`` if absent / malformed /
        schema-mismatched."""
        path = self.current_path
        if not path.exists():
            return None
        with self._lock:
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("[PostureStore] current file is not valid JSON")
            return None
        return reading_from_json(payload)

    def load_change_marker_at(
        self,
        expected_posture: Optional[Posture] = None,
    ) -> Optional[float]:
        """Return the change-marker timestamp paired with ``current``, or
        ``None`` when the marker is absent / malformed / schema-mismatched
        / posture-mismatched against ``expected_posture``.

        The posture-mismatch check is the safety net for the case where a
        legacy observer (no marker support) wrote ``current`` without the
        side-car, OR a partial write left the marker pointing at a stale
        posture. In both cases the caller should fall back to the legacy
        ``inferred_at`` proxy rather than honor a contaminated marker."""
        path = self.change_marker_path
        if not path.exists():
            return None
        with self._lock:
            try:
                raw = path.read_text(encoding="utf-8")
            except OSError:
                return None
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning(
                "[PostureStore] change_marker file is not valid JSON",
            )
            return None
        if payload.get("schema_version") != POSTURE_STORE_SCHEMA:
            logger.warning(
                "[PostureStore] change_marker schema mismatch: %r != %r",
                payload.get("schema_version"), POSTURE_STORE_SCHEMA,
            )
            return None
        try:
            marker_posture = Posture.from_str(str(payload.get("posture")))
            marker_at = float(payload["change_marker_at"])
        except (KeyError, ValueError, TypeError):
            logger.warning(
                "[PostureStore] change_marker malformed payload",
            )
            return None
        if (
            expected_posture is not None
            and marker_posture is not expected_posture
        ):
            logger.debug(
                "[PostureStore] change_marker posture %s != expected "
                "%s — discarding (legacy or torn write)",
                marker_posture, expected_posture,
            )
            return None
        return marker_at

    # ---- history ----------------------------------------------------------

    def append_history(self, reading: PostureReading) -> None:
        """Append to the ring buffer, trim to history_size from the front."""
        line = json.dumps(reading_to_json(reading), separators=(",", ":"))
        with self._lock:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            # Read existing, append new line, trim, rewrite atomically
            lines: List[str] = []
            if self.history_path.exists():
                try:
                    lines = [
                        ln for ln in self.history_path.read_text(
                            encoding="utf-8"
                        ).splitlines() if ln.strip()
                    ]
                except OSError:
                    lines = []
            lines.append(line)
            if len(lines) > self._history_size:
                lines = lines[-self._history_size:]
            self._atomic_write(self.history_path, "\n".join(lines) + "\n")

    def load_history(self, limit: Optional[int] = None) -> List[PostureReading]:
        """Return readings from history, newest last. ``limit`` slices the
        tail (most recent)."""
        if not self.history_path.exists():
            return []
        with self._lock:
            try:
                raw_lines = [
                    ln for ln in self.history_path.read_text(
                        encoding="utf-8"
                    ).splitlines() if ln.strip()
                ]
            except OSError:
                return []
        if limit is not None and limit > 0:
            raw_lines = raw_lines[-int(limit):]
        out: List[PostureReading] = []
        for ln in raw_lines:
            try:
                payload = json.loads(ln)
            except json.JSONDecodeError:
                continue
            reading = reading_from_json(payload)
            if reading is not None:
                out.append(reading)
        return out

    # ---- override audit ---------------------------------------------------

    def append_audit(self, record: OverrideRecord) -> None:
        """Append-only audit log. Never truncated — §8 immutable audit."""
        line = json.dumps(record.to_dict(), separators=(",", ":"))
        with self._lock:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")

    def load_audit(self, limit: Optional[int] = None) -> List[OverrideRecord]:
        """Read the audit log. Newest last."""
        if not self.audit_path.exists():
            return []
        with self._lock:
            try:
                raw_lines = [
                    ln for ln in self.audit_path.read_text(
                        encoding="utf-8"
                    ).splitlines() if ln.strip()
                ]
            except OSError:
                return []
        if limit is not None and limit > 0:
            raw_lines = raw_lines[-int(limit):]
        out: List[OverrideRecord] = []
        for ln in raw_lines:
            try:
                payload = json.loads(ln)
            except json.JSONDecodeError:
                continue
            try:
                posture_raw = payload.get("posture")
                posture = (
                    Posture.from_str(posture_raw) if posture_raw else None
                )
                out.append(
                    OverrideRecord(
                        event=str(payload["event"]),
                        posture=posture,
                        who=str(payload.get("who", "unknown")),
                        at=float(payload.get("at", 0.0)),
                        until=(
                            float(payload["until"])
                            if payload.get("until") is not None else None
                        ),
                        reason=str(payload.get("reason", "")),
                    )
                )
            except (KeyError, ValueError):
                continue
        return out

    # ---- diagnostics ------------------------------------------------------

    def stats(self) -> Dict[str, Any]:
        history_count = 0
        if self.history_path.exists():
            try:
                history_count = sum(
                    1 for ln in self.history_path.read_text(
                        encoding="utf-8"
                    ).splitlines() if ln.strip()
                )
            except OSError:
                pass
        audit_count = 0
        if self.audit_path.exists():
            try:
                audit_count = sum(
                    1 for ln in self.audit_path.read_text(
                        encoding="utf-8"
                    ).splitlines() if ln.strip()
                )
            except OSError:
                pass
        return {
            "schema_version": POSTURE_STORE_SCHEMA,
            "history_count": history_count,
            "audit_count": audit_count,
            "capacity": self._history_size,
            "has_current": self.current_path.exists(),
            "base_dir": str(self._base),
        }

    def clear_all(self) -> None:
        """Test helper — remove all four files."""
        with self._lock:
            for p in (
                self.current_path,
                self.history_path,
                self.audit_path,
                self.change_marker_path,
            ):
                if p.exists():
                    try:
                        p.unlink()
                    except OSError:
                        pass


__all__ = [
    "POSTURE_STORE_SCHEMA",
    "OverrideRecord",
    "PostureStore",
    "default_history_size",
    "reading_from_json",
    "reading_to_json",
]

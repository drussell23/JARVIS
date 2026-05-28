"""DW Capacity Ledger — Slice 34 substrate (Phase 0).

Closes the v25→v29 diagnostic gap: 5 capability soaks produced
zero successful DW 397B candidates with no per-(model, shape)
observation data to attribute the failure. This ledger records
EVERY outbound DW call's full envelope as one JSONL row, building
the empirical dataset that §48.7's 4 hypotheses (a-d) need to
resolve which is true.

# Design discipline (per operator binding)

  * **No shortcuts:** every DW call records — passive observation,
    no sampling. False negatives (missing records) would invalidate
    the diagnostic.
  * **No hardcoding:** every threshold env-knobbed.
  * **Compose existing:** writes go through
    :func:`cross_process_jsonl.flock_append_line` (Slice 33 Arc 2
    Phase 2 instrumented) — no parallel write path.
  * **Async-native:** :meth:`record_call` dispatches the file write
    via ``asyncio.to_thread`` so the asyncio loop keeps ticking
    during file I/O.
  * **Fail-closed:** any internal error logs at WARN and swallows.
    Recording failure MUST NEVER affect the provider call's outcome.
  * **Default ON (passive):** ``JARVIS_DW_CAPACITY_LEDGER_ENABLED``
    default TRUE — observation has no behavior side-effects.

# Schema (v1)

Each row is one JSON object. Fields:

  schema_version       : "dw_capacity.1"
  timestamp_unix       : float (record time)
  model_id             : str (e.g. "Qwen/Qwen3.5-397B-A17B-FP8")
  route                : str (immediate/standard/complex/background/speculative)
  prompt_chars         : int (input prompt size)
  outcome              : str (ok/timeout/infra_error/syntax_error/...)
  ttft_ms              : float | null (time to first token; streaming only)
  total_elapsed_ms     : float (wall-clock total)
  response_tokens      : int | null (output token count if available)
  response_chars       : int | null (output char count if available)
  cost_usd             : float (per-call cost)
  error_class          : str (exception type if outcome != ok; e.g. TimeoutError)
  error_detail         : str (truncated to 256 chars)
  caller               : str (op_id or "probe" / "health_probe" / etc.)

# What this module does NOT do

  * Does NOT decide routing (that's :mod:`dw_adaptive_timeout`
    + :mod:`dw_per_shape_stats`).
  * Does NOT bound the ledger file size (operators rotate via
    standard log-rotation tools; future arc may add internal
    rotation).
  * Does NOT replace ``PromotionLedger`` (which holds *small
    persistent state*; this holds *append-only event stream*).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any, Dict, List, Optional


logger = logging.getLogger("Ouroboros.DWCapacityLedger")


# ============================================================================
# Env knobs (re-read at call time so tests + operators can flip)
# ============================================================================


_LEDGER_ENABLED_ENV: str = "JARVIS_DW_CAPACITY_LEDGER_ENABLED"
_LEDGER_PATH_ENV: str = "JARVIS_DW_CAPACITY_LEDGER_PATH"
_DEFAULT_LEDGER_PATH: str = ".jarvis/dw_capacity_ledger.jsonl"
_ERROR_DETAIL_MAX_CHARS: int = 256


def is_enabled() -> bool:
    """Default TRUE — passive observation has no behaviour effect."""
    raw = os.environ.get(_LEDGER_ENABLED_ENV, "").strip().lower()
    if not raw:
        return True
    return raw not in ("0", "false", "no", "off")


def ledger_path() -> Path:
    raw = os.environ.get(_LEDGER_PATH_ENV, "").strip()
    if not raw:
        raw = _DEFAULT_LEDGER_PATH
    return Path(raw)


# ============================================================================
# Schema
# ============================================================================


LEDGER_SCHEMA_VERSION = "dw_capacity.1"


@dataclass(frozen=True)
class DWCallRecord:
    """Frozen envelope of one DW call. All fields IPC-safe primitives."""

    schema_version: str = LEDGER_SCHEMA_VERSION
    timestamp_unix: float = 0.0
    model_id: str = ""
    route: str = ""
    prompt_chars: int = 0
    outcome: str = ""
    ttft_ms: Optional[float] = None
    total_elapsed_ms: float = 0.0
    response_tokens: Optional[int] = None
    response_chars: Optional[int] = None
    cost_usd: float = 0.0
    error_class: str = ""
    error_detail: str = ""
    caller: str = ""

    def to_jsonl_line(self) -> str:
        """Render as single JSONL line (trailing newline added by writer)."""
        d = asdict(self)
        return json.dumps(d, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "DWCallRecord":
        """Parse from dict. Defensive — unknown fields ignored,
        missing fields take defaults. NEVER raises on malformed input
        (returns a record with default fields if parse fails)."""
        try:
            return cls(
                schema_version=str(d.get("schema_version", LEDGER_SCHEMA_VERSION)),
                timestamp_unix=float(d.get("timestamp_unix", 0.0)),
                model_id=str(d.get("model_id", "")),
                route=str(d.get("route", "")),
                prompt_chars=int(d.get("prompt_chars", 0)),
                outcome=str(d.get("outcome", "")),
                ttft_ms=(
                    float(d["ttft_ms"]) if d.get("ttft_ms") is not None
                    else None
                ),
                total_elapsed_ms=float(d.get("total_elapsed_ms", 0.0)),
                response_tokens=(
                    int(d["response_tokens"]) if d.get("response_tokens") is not None
                    else None
                ),
                response_chars=(
                    int(d["response_chars"]) if d.get("response_chars") is not None
                    else None
                ),
                cost_usd=float(d.get("cost_usd", 0.0)),
                error_class=str(d.get("error_class", "")),
                error_detail=str(d.get("error_detail", "")),
                caller=str(d.get("caller", "")),
            )
        except Exception:  # noqa: BLE001 — defensive
            return cls()


# ============================================================================
# Ledger writer
# ============================================================================


class DWCapacityLedger:
    """Append-only per-call record sink. Composes ``flock_append_line``
    (Slice 33 Arc 2 Phase 2) for cross-process-safe writes.

    Public API:
      * :meth:`record_call` — async write of one record
      * :meth:`read_recent` — sync read of last N records (for stats
        + REPL inspection)
      * :meth:`aggregate_by_model_shape` — group + summarize
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or ledger_path()
        # Lazy-create parent dir
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

    @property
    def path(self) -> Path:
        return self._path

    async def record_call(
        self,
        record: DWCallRecord,
        *,
        sanitize_error_detail: bool = True,
    ) -> bool:
        """Append one record to the ledger.

        Returns True on success, False on any failure. NEVER raises.

        Truncates ``error_detail`` to ``_ERROR_DETAIL_MAX_CHARS`` if
        ``sanitize_error_detail=True`` (default — prevents accidentally
        logging a 1 MB stack trace per failed call).
        """
        if not is_enabled():
            return False
        try:
            # Sanitize error_detail length to avoid pathological writes
            rec = record
            if sanitize_error_detail and len(record.error_detail) > _ERROR_DETAIL_MAX_CHARS:
                truncated = record.error_detail[:_ERROR_DETAIL_MAX_CHARS] + "...[truncated]"
                rec = DWCallRecord(
                    **{**asdict(record), "error_detail": truncated},
                )
            line = rec.to_jsonl_line()
            # Lazy import — avoid module-init cycle.
            from backend.core.ouroboros.governance.cross_process_jsonl import (
                flock_append_line,
            )
            # Off-loop write — keeps asyncio main thread responsive
            # even when ledger growth triggers FS sync.
            ok = await asyncio.to_thread(
                flock_append_line, self._path, line,
            )
            return bool(ok)
        except Exception as exc:  # noqa: BLE001 — fail-closed
            logger.warning(
                "[DWCapacityLedger] record_call failed: %s "
                "(model=%s outcome=%s)", exc, record.model_id, record.outcome,
            )
            return False

    def read_recent(self, limit: int = 1000) -> List[DWCallRecord]:
        """Read the last N records from the ledger. NEVER raises.

        Returns the records in file order (oldest first within the
        window). Malformed lines are silently skipped.

        For large ledgers (>10K rows) this reads the entire file —
        future arc could add tail-only fast path.
        """
        if not self._path.exists():
            return []
        try:
            with self._path.open("r", encoding="utf-8") as f:
                all_lines = f.readlines()
        except OSError:
            return []
        # Take last `limit` lines + parse
        recent_lines = all_lines[-int(max(1, limit)):]
        records: List[DWCallRecord] = []
        for line in recent_lines:
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
                if not isinstance(d, dict):
                    continue
                records.append(DWCallRecord.from_dict(d))
            except (json.JSONDecodeError, ValueError):
                continue
        return records

    def aggregate_by_model_shape(
        self,
        *,
        window: int = 1000,
        prompt_chars_bucket: int = 5000,
    ) -> Dict[str, Dict[str, Any]]:
        """Group recent records by ``(model_id, route,
        prompt_chars // prompt_chars_bucket)`` and produce per-shape
        summary stats.

        Returns ``{shape_key: {"count": N, "success_rate": R,
        "p50_ms": ..., "p95_ms": ..., "p99_ms": ...}, ...}`` where
        shape_key is ``"<model>::<route>::<bucket>"``.

        Pure-function on the file snapshot — no DB, no in-memory state.
        Suitable for REPL inspection + Phase 2 hypothesis-resolution
        report generation. NEVER raises.
        """
        recs = self.read_recent(limit=window)
        if not recs:
            return {}
        buckets: Dict[str, List[DWCallRecord]] = {}
        bucket_size = max(1, int(prompt_chars_bucket))
        for r in recs:
            key = f"{r.model_id}::{r.route}::{(r.prompt_chars // bucket_size) * bucket_size}"
            buckets.setdefault(key, []).append(r)
        out: Dict[str, Dict[str, Any]] = {}
        for key, group in buckets.items():
            n = len(group)
            successes = sum(1 for g in group if g.outcome == "ok")
            latencies = sorted(g.total_elapsed_ms for g in group)
            out[key] = {
                "count": n,
                "success_rate": (successes / n) if n else 0.0,
                "p50_ms": _percentile(latencies, 0.50),
                "p95_ms": _percentile(latencies, 0.95),
                "p99_ms": _percentile(latencies, 0.99),
                "ttft_p50_ms": _percentile(
                    sorted(
                        g.ttft_ms for g in group if g.ttft_ms is not None
                    ),
                    0.50,
                ),
                "outcomes": _outcome_histogram(group),
            }
        return out


def _percentile(sorted_samples: List[float], q: float) -> float:
    """Nearest-rank percentile. Returns 0.0 on empty input."""
    n = len(sorted_samples)
    if n == 0:
        return 0.0
    idx = max(0, min(n - 1, int(round(q * n)) - 1))
    return float(sorted_samples[idx])


def _outcome_histogram(records: List[DWCallRecord]) -> Dict[str, int]:
    """Per-outcome count for one bucket."""
    hist: Dict[str, int] = {}
    for r in records:
        key = r.outcome or "unknown"
        hist[key] = hist.get(key, 0) + 1
    return hist


# ============================================================================
# Module-level singleton accessor
# ============================================================================


_default_ledger: Optional[DWCapacityLedger] = None


def get_default_ledger() -> DWCapacityLedger:
    """Lazy module-level singleton. Per-process; tests can replace
    via ``reset_for_tests``."""
    global _default_ledger
    if _default_ledger is None:
        _default_ledger = DWCapacityLedger()
    return _default_ledger


def reset_for_tests() -> None:
    """Test isolation — clears singleton so next call gets a fresh
    ledger pointing at the (possibly-overridden) env path."""
    global _default_ledger
    _default_ledger = None


# ============================================================================
# Public surface
# ============================================================================


__all__ = [
    "DWCallRecord",
    "DWCapacityLedger",
    "LEDGER_SCHEMA_VERSION",
    "get_default_ledger",
    "is_enabled",
    "ledger_path",
    "reset_for_tests",
]

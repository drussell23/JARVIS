"""Slicing-metrics ledger — Phase 11 P11.2.

Append-only JSONL at ``.jarvis/slicing_metrics.jsonl`` capturing one
row per ``read_file(target_symbol=...)`` invocation so the operator
can quantify token savings: actual chunk size vs the would-be full-
file size.

Three reasons this lives in its own tiny module rather than inside
``tool_executor``:

  1. **Authority isolation** — the metrics layer touches disk; the
     tool handler is sync + boundary-aware. Keeping the writer in a
     separate module makes the failure surface a single import +
     one ``logger.debug`` away from no-op safety.
  2. **NEVER raises** — every caller of :func:`record_slice` swallows
     internal errors, returning ``False``. A disk-full / permission
     failure can NEVER take down a Venom tool call.
  3. **Future feed** for Phase 11 P11.4 (Telemetry for Local RL) —
     the schema already maps ``{request_context, action_taken,
     outcome_reward}`` shape so a future fine-tuning consumer can
     read this ledger natively.

## Schema (jsonl)

```
{
  "schema_version": "slicing.1",
  "ts_iso": "2026-04-27T...Z",
  "ts_epoch": 1234567890.5,
  "op_id": "op-019dd0...",
  "file_path": "backend/foo.py",
  "target_symbol": "ClassName.method_name",
  "full_chars": 12345,
  "sliced_chars": 678,
  "savings_ratio": 0.945,
  "include_callers": false,
  "include_imports": true,
  "fallback_reason": null,
  "outcome": "ok"
}
```

When slicing wasn't applied (e.g. master flag off, non-Python file,
target symbol not found), the row carries ``outcome="fallback"`` +
``fallback_reason`` describing why. This is the data Phase 11 P11.7
graduation criterion will read to verify the ≥10× input-token
reduction target.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional


logger = logging.getLogger(__name__)


SCHEMA_VERSION = "slicing.1"


_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Env helpers (mirror the same idiom as posture_observer / topology_sentinel)
# ---------------------------------------------------------------------------


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def _env_int(name: str, default: int, minimum: int = 0) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# Master flag + tunables
# ---------------------------------------------------------------------------


def is_metrics_enabled() -> bool:
    """``JARVIS_TOOL_AST_SLICE_METRICS_ENABLED`` (default true).

    When off, :func:`record_slice` is a no-op. Off is useful for
    test environments that don't want to pollute the project's
    ``.jarvis/`` directory."""
    return _env_bool(
        "JARVIS_TOOL_AST_SLICE_METRICS_ENABLED", default=True,
    )


def metrics_path() -> Path:
    """Path to ``slicing_metrics.jsonl``. Env-overridable for tests
    via ``JARVIS_SLICING_METRICS_PATH``."""
    raw = os.environ.get("JARVIS_SLICING_METRICS_PATH")
    if raw:
        return Path(raw)
    return Path(".jarvis").resolve() / "slicing_metrics.jsonl"


def history_capacity() -> int:
    """Ring-buffer capacity. Older rows are trimmed in-place on
    write. Default 4096 — about 2-3 hours of intense sentinel-soak
    activity at sub-second op cadence."""
    return _env_int(
        "JARVIS_SLICING_METRICS_HISTORY_SIZE", default=4096, minimum=64,
    )


# ---------------------------------------------------------------------------
# Record dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SliceMetric:
    """One row of the ledger. Frozen so the caller can construct +
    discard without coordination."""

    file_path: str
    target_symbol: str
    full_chars: int
    sliced_chars: int
    op_id: str = ""
    include_callers: bool = False
    include_imports: bool = True
    fallback_reason: Optional[str] = None
    outcome: str = "ok"          # "ok" | "fallback" | "error"
    schema_version: str = SCHEMA_VERSION
    ts_epoch: float = field(default_factory=time.time)

    @property
    def savings_ratio(self) -> float:
        """Fraction of source-text NOT injected. 0.95 means 95%
        of the file was clipped. Returns 0 when full_chars is 0
        (defensive — prevents div-by-zero)."""
        if self.full_chars <= 0:
            return 0.0
        saved = max(0, self.full_chars - self.sliced_chars)
        return saved / self.full_chars

    def to_json(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "ts_iso": datetime.fromtimestamp(
                self.ts_epoch, timezone.utc,
            ).isoformat(),
            "ts_epoch": self.ts_epoch,
            "op_id": self.op_id,
            "file_path": self.file_path,
            "target_symbol": self.target_symbol,
            "full_chars": self.full_chars,
            "sliced_chars": self.sliced_chars,
            "savings_ratio": round(self.savings_ratio, 4),
            "include_callers": self.include_callers,
            "include_imports": self.include_imports,
            "fallback_reason": self.fallback_reason,
            "outcome": self.outcome,
        }


# ---------------------------------------------------------------------------
# Writer (NEVER raises)
# ---------------------------------------------------------------------------


_WRITE_LOCK = threading.Lock()


def record_slice(metric: SliceMetric) -> bool:
    """Append one row. Returns True on success, False on disabled or
    failure. NEVER raises — disk-full / permission errors degrade
    silently to a debug log line."""
    if not is_metrics_enabled():
        return False
    path = metrics_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.debug(
            "[SlicingMetrics] cannot create dir %s: %s",
            path.parent, exc,
        )
        return False
    line = json.dumps(metric.to_json(), sort_keys=True) + "\n"
    with _WRITE_LOCK:
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(line)
        except OSError as exc:
            logger.debug(
                "[SlicingMetrics] append failed: %s", exc,
            )
            return False
        _maybe_trim(path)
    return True


def _maybe_trim(path: Path) -> None:
    """Keep the last ``history_capacity()`` lines. Atomic temp+rename
    so a concurrent reader never sees a torn write. NEVER raises."""
    cap = history_capacity()
    try:
        with open(path, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return
    if len(lines) <= cap:
        return
    kept = lines[-cap:]
    try:
        fd, tmp = tempfile.mkstemp(
            prefix="slicing_metrics_", suffix=".jsonl.tmp",
            dir=str(path.parent),
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.writelines(kept)
        os.replace(tmp, path)
    except OSError as exc:
        logger.debug(
            "[SlicingMetrics] trim failed: %s", exc,
        )


__all__ = [
    "SCHEMA_VERSION",
    "SliceMetric",
    "history_capacity",
    "is_metrics_enabled",
    "metrics_path",
    "record_slice",
]

"""Slice 39 Task 1 — Per-surface transport health taxonomy + record.

Defines the *orthogonal* per-SURFACE health substrate that sits beside
the existing per-MODEL ``dw_modality_ledger.py``.  Three surfaces are
tracked:

  * ``batch_storage``    — ``/v1/files`` batch-file upload / retrieval
  * ``direct_streaming`` — ``/v1/chat/completions`` SSE stream
  * ``auth_sync``        — Aegis authentication handshake

Five verdicts capture the failure taxonomy:

  * ``healthy``            — last probe completed without error
  * ``transport_degraded`` — TCP/TLS/timeout before the server spoke
  * ``upstream_degraded``  — server replied 5xx / stream ended early
  * ``auth_failed``        — 401 / 403 / token-refresh failure
  * ``error_other``        — anything else (unexpected exception, etc.)

Only the closed-taxonomy enums, the frozen ``SurfaceHealthRecord``
dataclass, and the ``LEDGER_SCHEMA_VERSION`` constant live here.
Task 2 will add the mutable ledger class and atomic-write path.

NEVER raises out of any public method.  ``from_json_dict`` returns
``None`` on any structural problem.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, Mapping, Optional

logger = logging.getLogger("Ouroboros.SurfaceHealth")

# ---------------------------------------------------------------------------
# Schema version — Task 2 will embed this in the on-disk envelope.
# ---------------------------------------------------------------------------

LEDGER_SCHEMA_VERSION = 1


# ---------------------------------------------------------------------------
# Closed taxonomy enums
# ---------------------------------------------------------------------------


class SurfaceKind(str, Enum):
    """The three DW transport surfaces JARVIS uses."""

    BATCH_STORAGE = "batch_storage"
    DIRECT_STREAMING = "direct_streaming"
    AUTH_SYNC = "auth_sync"


class SurfaceVerdict(str, Enum):
    """Health verdict for a single surface probe outcome."""

    HEALTHY = "healthy"
    TRANSPORT_DEGRADED = "transport_degraded"
    UPSTREAM_DEGRADED = "upstream_degraded"
    AUTH_FAILED = "auth_failed"
    ERROR_OTHER = "error_other"


# ---------------------------------------------------------------------------
# Frozen record dataclass with JSON round-trip
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurfaceHealthRecord:
    """Immutable snapshot of one surface's health state.

    Scalar fields use defensive coercion in ``from_json_dict`` so
    minor type drift (e.g. JSON int vs float) never causes a crash.
    """

    surface: SurfaceKind
    verdict: SurfaceVerdict
    last_probe_unix: float = 0.0
    latency_ms: int = 0
    diagnostic: str = ""
    consecutive_failures: int = 0

    def to_json_dict(self) -> Dict[str, Any]:
        """Serialise to a plain dict suitable for ``json.dumps``."""
        return {
            "surface": self.surface.value,
            "verdict": self.verdict.value,
            "last_probe_unix": self.last_probe_unix,
            "latency_ms": self.latency_ms,
            "diagnostic": self.diagnostic,
            "consecutive_failures": self.consecutive_failures,
        }

    @classmethod
    def from_json_dict(
        cls,
        raw: Mapping[str, Any],
    ) -> Optional["SurfaceHealthRecord"]:
        """Deserialise from a plain dict.  Returns ``None`` on any
        structural problem (unknown/missing ``surface`` or ``verdict``,
        unexpected exception)."""
        try:
            surface = SurfaceKind(raw["surface"])
            verdict = SurfaceVerdict(raw["verdict"])
            return cls(
                surface=surface,
                verdict=verdict,
                last_probe_unix=float(raw.get("last_probe_unix", 0.0) or 0.0),
                latency_ms=int(raw.get("latency_ms", 0) or 0),
                diagnostic=str(raw.get("diagnostic", "")),
                consecutive_failures=int(
                    raw.get("consecutive_failures", 0) or 0
                ),
            )
        except (KeyError, ValueError):
            return None
        except Exception:  # noqa: BLE001 — defensive; never propagate
            logger.warning(
                "[SurfaceHealth] unexpected error in from_json_dict; raw=%r",
                raw,
            )
            return None


__all__ = [
    "LEDGER_SCHEMA_VERSION",
    "SurfaceHealthRecord",
    "SurfaceKind",
    "SurfaceVerdict",
]

"""Slice 39 Task 7 — Per-surface transport-health probes + concurrent sweep.

Three probes mirror the three ``SurfaceKind`` values:

  probe_batch_storage     — exercises ``/v1/files`` via ``_upload_file``
  probe_direct_streaming  — exercises SSE streaming via ``build_heavyprobe_adapter``
  probe_auth_sync         — validates the Aegis/legacy bearer header

All three probes NEVER raise.  Unexpected exceptions are caught and
translated into the appropriate degraded verdict.

``run_surface_sweep`` fires all three concurrently via ``asyncio.gather``
with per-probe timeout isolation (env ``JARVIS_DW_SURFACE_PROBE_TIMEOUT_S``,
default 10 s), records results into a ``SurfaceHealthLedger``, and returns
the post-record snapshot.  NEVER raises.

Design note — monkeypatch compatibility
----------------------------------------
``run_surface_sweep`` calls ``probe_batch_storage``, ``probe_direct_streaming``,
and ``probe_auth_sync`` by resolving the names through the *module global
namespace* at call time (bare ``probe_*(...)`` references in the function
body).  Because Python resolves unqualified function calls through the
enclosing module's ``__dict__``, ``monkeypatch.setattr(mod, "probe_*", fake)``
replaces the binding seen by ``run_surface_sweep`` — no local alias is
captured.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, Dict, Optional, Tuple, cast

from backend.core.ouroboros.governance.aegis_provider_bridge import (
    dw_session_auth_header,
)
from backend.core.ouroboros.governance.dw_surface_health import (
    SurfaceHealthLedger,
    SurfaceHealthRecord,
    SurfaceKind,
    SurfaceVerdict,
)

logger = logging.getLogger("Ouroboros.SurfaceProbes")


# ---------------------------------------------------------------------------
# Env helper
# ---------------------------------------------------------------------------


def _envf(name: str, default: float) -> float:
    """Read a float env var; return *default* on missing / bad value."""
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# ---------------------------------------------------------------------------
# Probe A — Batch Storage (/v1/files)
# ---------------------------------------------------------------------------


async def probe_batch_storage(
    provider: object,
    model_id: str,
) -> Tuple[SurfaceVerdict, str, int]:
    """Probe the ``BATCH_STORAGE`` surface by uploading a minimal JSONL entry.

    Returns ``(verdict, diagnostic, latency_ms)``.  NEVER raises.
    """
    entry = {
        "custom_id": f"slice39-surface-a-{int(time.time())}",
        "method": "POST",
        "url": "/v1/chat/completions",
        "body": {
            "model": model_id,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        },
    }
    t0 = time.monotonic()
    try:
        jsonl = provider._compose_jsonl_batch_entry(entry)  # type: ignore[union-attr]
        file_id = await provider._upload_file(jsonl, op_id=entry["custom_id"])  # type: ignore[union-attr]
        latency = int((time.monotonic() - t0) * 1000)
        if file_id:
            return SurfaceVerdict.HEALTHY, f"file_id={file_id}", latency
        return SurfaceVerdict.UPSTREAM_DEGRADED, "upload_returned_none", latency
    except Exception as exc:  # noqa: BLE001 — never raise
        latency = int((time.monotonic() - t0) * 1000)
        diag = f"{type(exc).__name__}:{str(exc)[:120]}"
        logger.debug("probe_batch_storage raised (caught): %s", diag)
        return SurfaceVerdict.UPSTREAM_DEGRADED, diag, latency


# ---------------------------------------------------------------------------
# Probe B — Direct Streaming (/v1/chat/completions SSE)
# ---------------------------------------------------------------------------


async def probe_direct_streaming(
    provider: object,
    model_id: str,
) -> Tuple[SurfaceVerdict, str, int]:
    """Probe the ``DIRECT_STREAMING`` surface via the heavy-probe adapter.

    Returns ``(verdict, diagnostic, latency_ms)``.  NEVER raises.
    """
    try:
        from backend.core.ouroboros.governance.dw_transport_disambiguator import (
            FailureClass,
            classify_surface_failure,
        )
        from backend.core.ouroboros.governance.preflight_probe import (
            build_heavyprobe_adapter,
        )

        probe_fn = build_heavyprobe_adapter(provider)
        outcome = await probe_fn(model_id)

        if getattr(outcome, "success", False):
            latency = int(getattr(outcome, "latency_ms", 0) or 0)
            return SurfaceVerdict.HEALTHY, "", latency

        cls = classify_surface_failure(outcome)
        diag: str = getattr(outcome, "error_message", None) or "stream_failed"
        latency = int(getattr(outcome, "latency_ms", 0) or 0)

        if cls is FailureClass.TRANSPORT:
            return SurfaceVerdict.TRANSPORT_DEGRADED, diag, latency
        return SurfaceVerdict.UPSTREAM_DEGRADED, diag, latency

    except Exception as exc:  # noqa: BLE001 — never raise
        diag = f"{type(exc).__name__}:{str(exc)[:120]}"
        logger.debug("probe_direct_streaming raised (caught): %s", diag)
        return SurfaceVerdict.ERROR_OTHER, diag, 0


# ---------------------------------------------------------------------------
# Probe C — Auth Sync (Aegis / legacy bearer)
# ---------------------------------------------------------------------------


async def probe_auth_sync() -> Tuple[SurfaceVerdict, str]:
    """Probe the ``AUTH_SYNC`` surface by fetching the session auth header.

    Returns ``(verdict, diagnostic)``.  NEVER raises.
    """
    try:
        header = await dw_session_auth_header()
        if header.get("Authorization", "").startswith("Bearer "):
            return SurfaceVerdict.HEALTHY, ""
        return SurfaceVerdict.AUTH_FAILED, "no_bearer_in_header"
    except Exception as exc:  # noqa: BLE001 — never raise
        diag = f"{type(exc).__name__}:{str(exc)[:120]}"
        logger.debug("probe_auth_sync raised (caught): %s", diag)
        return SurfaceVerdict.AUTH_FAILED, diag


# ---------------------------------------------------------------------------
# Concurrent sweep
# ---------------------------------------------------------------------------


async def run_surface_sweep(
    *,
    provider: object,
    model_id: str,
    ledger: SurfaceHealthLedger,
    timeout_s: Optional[float] = None,
) -> Dict[SurfaceKind, SurfaceHealthRecord]:
    """Run all three surface probes concurrently and record into *ledger*.

    Each probe is individually guarded by *timeout_s* (env
    ``JARVIS_DW_SURFACE_PROBE_TIMEOUT_S``, default 10 s) so a single
    slow probe cannot starve the others.

    Returns the post-record ``ledger.snapshot()``.  NEVER raises.
    """
    eff_timeout: float = (
        timeout_s if timeout_s is not None
        else _envf("JARVIS_DW_SURFACE_PROBE_TIMEOUT_S", 10.0)
    )

    async def _guarded(coro) -> Any:
        try:
            return await asyncio.wait_for(coro, eff_timeout)
        except asyncio.TimeoutError:
            return ("__timeout__",)
        except Exception as exc:  # noqa: BLE001
            return ("__error__", f"{type(exc).__name__}:{str(exc)[:120]}")

    def _unpack3(res: Any) -> Tuple[SurfaceVerdict, str, int]:
        # 3-tuple probe result, or a sentinel from _guarded.
        if isinstance(res, tuple) and len(res) == 1 and res[0] == "__timeout__":
            return SurfaceVerdict.TRANSPORT_DEGRADED, "probe_timeout", 0
        if isinstance(res, tuple) and len(res) == 2 and res[0] == "__error__":
            return SurfaceVerdict.ERROR_OTHER, str(res[1]), 0
        return cast(Tuple[SurfaceVerdict, str, int], res)  # probe 3-tuple

    def _unpack2(res: Any) -> Tuple[SurfaceVerdict, str]:
        # 2-tuple auth result, or a sentinel from _guarded.
        if isinstance(res, tuple) and len(res) == 1 and res[0] == "__timeout__":
            return SurfaceVerdict.AUTH_FAILED, "probe_timeout"
        if isinstance(res, tuple) and len(res) == 2 and res[0] == "__error__":
            return SurfaceVerdict.ERROR_OTHER, str(res[1])
        return cast(Tuple[SurfaceVerdict, str], res)  # probe 2-tuple

    # Fire all three concurrently.
    # NOTE: probe_batch_storage / probe_direct_streaming / probe_auth_sync are
    # resolved via the module's global namespace at call time — monkeypatch
    # replacements on the module object are visible here.
    a, b, c = await asyncio.gather(
        _guarded(probe_batch_storage(provider, model_id)),
        _guarded(probe_direct_streaming(provider, model_id)),
        _guarded(probe_auth_sync()),
    )

    va, da, la = _unpack3(a)   # Probe A (batch storage)
    vb, db, lb = _unpack3(b)   # Probe B (direct streaming)
    vc, dc = _unpack2(c)       # Probe C (auth sync)

    # --- Record into ledger ---
    ledger.record(SurfaceKind.BATCH_STORAGE, va, latency_ms=la, diagnostic=da)
    ledger.record(SurfaceKind.DIRECT_STREAMING, vb, latency_ms=lb, diagnostic=db)
    ledger.record(SurfaceKind.AUTH_SYNC, vc, diagnostic=dc)

    logger.info(
        "[SurfaceProbes] sweep complete: batch=%s stream=%s auth=%s",
        va.value, vb.value, vc.value,
    )
    return ledger.snapshot()


__all__ = [
    "probe_auth_sync",
    "probe_batch_storage",
    "probe_direct_streaming",
    "run_surface_sweep",
]

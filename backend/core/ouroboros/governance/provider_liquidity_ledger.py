"""Provider Liquidity Ledger — telemetry-driven quota shaping substrate.

Circadian Resilience (2026-07-18): waiting for a 429 is a reactive
anti-pattern. Providers DECLARE their remaining liquidity on every 200 OK
(``anthropic-ratelimit-tokens-remaining`` / ``...-reset``, generic
``x-ratelimit-*``, and ``retry-after`` on 429s) — this ledger captures that
telemetry once, at the ONE point that sees every upstream response (the Aegis
forwarding proxy), and serves it to every consumer:

  * the **SensorGovernor** sheds BACKGROUND/SPECULATIVE load preemptively when
    forecast burn exceeds remaining runway (BEFORE any 429);
  * the **HibernationProber** seeds its first probe delay from the declared
    reset horizon instead of blind exponential backoff.

File-backed at ``.jarvis/provider_liquidity.json`` (the same cross-process
ledger discipline as ``dw_surface_health.json``) because Aegis runs as a child
process — an in-memory ledger there would be invisible to the governor here.
Atomic temp+rename writes; mtime-cached reads; thread-safe; NEVER raises.

CLOCK-SKEW SAFETY (mandate 2): hibernation/shaping durations are computed from
RELATIVE deltas only —
  * ``retry-after`` (seconds) is used verbatim;
  * an absolute reset timestamp is converted to a delta against the SERVER's
    own ``Date`` header when present (server-vs-server, skew-free), falling
    back to local time only when the server clock is unavailable;
  * readers get ``seconds_to_reset`` = recorded delta minus local elapsed-
    since-record (a pure local-duration measurement, skew-free).
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

PROVIDER_LIQUIDITY_SCHEMA_VERSION = "provider_liquidity.v1"

_FALSY = ("0", "false", "no", "off")
_LOCK = threading.RLock()
# (path, mtime) -> parsed payload
_read_cache: Dict[Tuple[str, float], Dict[str, Any]] = {}


def liquidity_ledger_enabled() -> bool:
    """``JARVIS_PROVIDER_LIQUIDITY_ENABLED`` (default ON — pure telemetry;
    the governor's use of it has its own flag). NEVER raises."""
    return os.environ.get(
        "JARVIS_PROVIDER_LIQUIDITY_ENABLED", "true",
    ).strip().lower() not in _FALSY


def _ledger_path() -> Path:
    return Path(os.environ.get(
        "JARVIS_PROVIDER_LIQUIDITY_PATH", ".jarvis/provider_liquidity.json",
    ))


def min_tokens_floor() -> int:
    """``JARVIS_LIQUIDITY_MIN_TOKENS_FLOOR`` (default 2000) — below this many
    remaining tokens a provider's runway counts as exhausted for shaping
    purposes (absent a larger explicit forecast). NEVER raises."""
    try:
        return max(0, int(os.environ.get("JARVIS_LIQUIDITY_MIN_TOKENS_FLOOR", "2000")))
    except (TypeError, ValueError):
        return 2000


# ---------------------------------------------------------------------------
# Header parsing — skew-safe by construction.
# ---------------------------------------------------------------------------


def _lower(headers: Mapping[str, Any]) -> Dict[str, str]:
    try:
        return {str(k).lower(): str(v) for k, v in dict(headers).items()}
    except Exception:  # noqa: BLE001
        return {}


def _parse_reset_delta_s(h: Dict[str, str]) -> Optional[float]:
    """The seconds-until-reset as a RELATIVE delta. Preference order:
      1. ``retry-after`` (already relative — skew-free by definition);
      2. an absolute reset stamp (``anthropic-ratelimit-tokens-reset`` RFC3339,
         or epoch-seconds ``x-ratelimit-reset``) minus the SERVER ``date``
         header (server-vs-server → skew-free);
      3. the absolute stamp minus LOCAL time (last resort — logged).
    NEVER raises."""
    try:
        ra = h.get("retry-after", "").strip()
        if ra:
            try:
                return max(0.0, float(ra))
            except ValueError:
                pass  # HTTP-date form falls through to absolute handling
        raw = (
            h.get("anthropic-ratelimit-tokens-reset", "")
            or h.get("x-ratelimit-reset", "")
            or (ra if ra else "")
        ).strip()
        if not raw:
            return None
        # Absolute → datetime.
        reset_ts: Optional[float] = None
        try:
            reset_ts = float(raw)                      # epoch seconds
            if reset_ts < 10_000_000:                  # small = already a delta
                return max(0.0, reset_ts)
        except ValueError:
            try:
                from datetime import datetime  # noqa: PLC0415
                reset_ts = datetime.fromisoformat(
                    raw.replace("Z", "+00:00"),
                ).timestamp()
            except ValueError:
                try:
                    reset_ts = parsedate_to_datetime(raw).timestamp()
                except Exception:  # noqa: BLE001
                    return None
        if reset_ts is None:
            return None
        # Server-vs-server delta when the server told us ITS clock.
        server_date = h.get("date", "").strip()
        if server_date:
            try:
                server_now = parsedate_to_datetime(server_date).timestamp()
                return max(0.0, reset_ts - server_now)
            except Exception:  # noqa: BLE001
                pass
        logger.debug(
            "[LiquidityLedger] no server Date header — falling back to local "
            "clock for reset delta (skew possible)",
        )
        return max(0.0, reset_ts - time.time())
    except Exception:  # noqa: BLE001
        return None


def _parse_tokens_remaining(h: Dict[str, str]) -> Optional[int]:
    for key in ("anthropic-ratelimit-tokens-remaining",
                "x-ratelimit-remaining-tokens", "x-ratelimit-remaining"):
        raw = h.get(key, "").strip()
        if raw:
            try:
                return max(0, int(float(raw)))
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Write side (called by Aegis forwarding — the one point seeing every response).
# ---------------------------------------------------------------------------


def record_headers(
    provider: str,
    headers: Mapping[str, Any],
    *,
    status: int = 200,
    now: "Optional[float]" = None,
) -> bool:
    """Parse rate-limit telemetry from a response's *headers* and persist it.
    Returns True when something was recorded. NEVER raises."""
    try:
        if not liquidity_ledger_enabled():
            return False
        h = _lower(headers)
        tokens = _parse_tokens_remaining(h)
        reset_delta = _parse_reset_delta_s(h)
        if tokens is None and reset_delta is None:
            return False                              # nothing declared
        t = float(now if now is not None else time.time())
        entry = {
            "tokens_remaining": tokens,
            "reset_delta_s": reset_delta,
            "recorded_unix": t,
            "last_status": int(status),
        }
        path = _ledger_path()
        with _LOCK:
            payload: Dict[str, Any] = {}
            try:
                payload = json.loads(path.read_text())
            except (OSError, ValueError):
                payload = {}
            providers = payload.get("providers") or {}
            providers[str(provider or "unknown").lower()] = entry
            payload = {
                "schema_version": PROVIDER_LIQUIDITY_SCHEMA_VERSION,
                "providers": providers,
            }
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, sort_keys=True))
            os.replace(tmp, path)
        return True
    except Exception:  # noqa: BLE001 — telemetry must never break forwarding
        logger.debug("[LiquidityLedger] record degraded", exc_info=True)
        return False


def provider_for_upstream(path_or_host: str) -> str:
    """Classify the upstream target into a ledger provider key. Pure. NEVER
    raises."""
    try:
        s = str(path_or_host or "").lower()
        if "anthropic" in s or "/v1/messages" in s:
            return "anthropic"
        if "doubleword" in s or "/chat/completions" in s:
            return "doubleword"
        return "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


# ---------------------------------------------------------------------------
# Read side (governor / prober / observability).
# ---------------------------------------------------------------------------


def _load() -> Dict[str, Any]:
    try:
        path = _ledger_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        key = (str(path), mtime)
        cached = _read_cache.get(key)
        if cached is not None:
            return cached
        payload = json.loads(path.read_text())
        _read_cache.clear()
        _read_cache[key] = payload
        return payload
    except Exception:  # noqa: BLE001
        return {}


def liquidity(provider: str) -> "Tuple[Optional[int], Optional[float]]":
    """``(tokens_remaining, seconds_to_reset)`` for *provider* — seconds
    computed as recorded delta minus LOCAL elapsed-since-record (pure duration,
    skew-free). ``(None, None)`` when unknown. NEVER raises."""
    try:
        entry = (_load().get("providers") or {}).get(str(provider).lower())
        if not entry:
            return None, None
        tokens = entry.get("tokens_remaining")
        delta = entry.get("reset_delta_s")
        recorded = float(entry.get("recorded_unix") or 0.0)
        remaining = None
        if delta is not None and recorded > 0:
            remaining = max(0.0, float(delta) - (time.time() - recorded))
        return (int(tokens) if tokens is not None else None), remaining
    except Exception:  # noqa: BLE001
        return None, None


def runway_exhausted(provider: str, forecast_tokens: "Optional[int]" = None) -> bool:
    """True iff *provider* declared fewer remaining tokens than the forecast
    (default: the min-tokens floor) AND the reset horizon has not yet passed.
    Unknown/expired telemetry → False (fail-open: shaping never blocks on
    missing data). NEVER raises."""
    try:
        tokens, secs = liquidity(provider)
        if tokens is None:
            return False
        need = max(min_tokens_floor(), int(forecast_tokens or 0))
        if tokens >= need:
            return False
        # Telemetry stales out once the declared reset has passed.
        if secs is not None and secs <= 0.0:
            return False
        return True
    except Exception:  # noqa: BLE001
        return False


def any_runway_exhausted(forecast_tokens: "Optional[int]" = None) -> bool:
    """True iff ANY recorded provider's runway is exhausted. NEVER raises."""
    try:
        for name in (_load().get("providers") or {}):
            if runway_exhausted(name, forecast_tokens):
                return True
        return False
    except Exception:  # noqa: BLE001
        return False


def max_seconds_to_reset() -> "Optional[float]":
    """The longest declared reset horizon across exhausted providers — the
    hibernation prober's first-probe hint. None when nothing is exhausted.
    NEVER raises."""
    try:
        out: "Optional[float]" = None
        for name in (_load().get("providers") or {}):
            if runway_exhausted(name):
                _t, secs = liquidity(name)
                if secs is not None and (out is None or secs > out):
                    out = secs
        return out
    except Exception:  # noqa: BLE001
        return None


def _reset_for_tests() -> None:
    """Test helper — drop the read cache (the file itself is test-scoped via
    JARVIS_PROVIDER_LIQUIDITY_PATH). NEVER raises."""
    try:
        with _LOCK:
            _read_cache.clear()
    except Exception:  # noqa: BLE001
        pass

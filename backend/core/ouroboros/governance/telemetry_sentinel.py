"""Slice 141 — The Sovereign Telemetry Failsafe (anomaly radar).

A detached 12-month soak must not be a black box. This sentinel watches three
CATASTROPHIC signals and fires a webhook only on those — never on routine events:

  1. **COST_CAP_90** — cumulative spend crosses 90% of the cost cap.
  2. **CONSECUTIVE_5XX** — provider 5xx streak beyond the backoff retry budget.
  3. **REFUSED_SAFETY** — a safety refusal / rogue-FSM block in the live loop.

Design (mirrors the episodic synapses): SYNC detector methods that update state +
return a ``SentinelAlert`` (or None), an ASYNC fail-soft webhook ``dispatch``, and
fire-and-forget ``note_*_nowait`` hooks for the hot path. Per-kind cooldown stops
alert spam. Gated ``JARVIS_TELEMETRY_SENTINEL_ENABLED`` default-FALSE. The webhook
poster is injectable; the default uses stdlib ``urllib`` (no new dependency).
"""
from __future__ import annotations

import dataclasses
import enum
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional

logger = logging.getLogger(__name__)

_ENV_MASTER = "JARVIS_TELEMETRY_SENTINEL_ENABLED"
_ENV_WEBHOOK = "JARVIS_SENTINEL_WEBHOOK_URL"
_ENV_COST_FRAC = "JARVIS_SENTINEL_COST_FRAC"
_ENV_5XX = "JARVIS_SENTINEL_5XX_THRESHOLD"
_ENV_COOLDOWN = "JARVIS_SENTINEL_COOLDOWN_S"

_DEFAULT_COST_FRAC = 0.9
_DEFAULT_5XX = 5
_DEFAULT_COOLDOWN = 3600.0


def telemetry_sentinel_enabled() -> bool:
    """Master gate, default-FALSE per §33.1. NEVER raises."""
    return os.getenv(_ENV_MASTER, "false").strip().lower() in ("1", "true", "yes", "on")


class AnomalyKind(str, enum.Enum):
    COST_CAP_90 = "cost_cap_90"
    CONSECUTIVE_5XX = "consecutive_5xx"
    REFUSED_SAFETY = "refused_safety"


@dataclasses.dataclass(frozen=True)
class SentinelAlert:
    kind: AnomalyKind
    detail: str
    ts: float


# poster(url, payload) -> status_code
_Poster = Callable[[str, Dict[str, Any]], Awaitable[int]]


class TelemetrySentinel:
    """Async, fail-soft anomaly detector + webhook dispatcher."""

    def __init__(
        self,
        *,
        cost_frac: Optional[float] = None,
        max_consec_5xx: Optional[int] = None,
        cooldown_s: Optional[float] = None,
        webhook_url: Optional[str] = None,
    ) -> None:
        self._cost_frac = cost_frac if cost_frac is not None else _env_float(_ENV_COST_FRAC, _DEFAULT_COST_FRAC)
        self._max_5xx = max_consec_5xx if max_consec_5xx is not None else _env_int(_ENV_5XX, _DEFAULT_5XX)
        self._cooldown = cooldown_s if cooldown_s is not None else _env_float(_ENV_COOLDOWN, _DEFAULT_COOLDOWN)
        self._webhook = webhook_url if webhook_url is not None else (os.getenv(_ENV_WEBHOOK, "") or "")
        self._consec_5xx = 0
        self._last_alert: Dict[AnomalyKind, float] = {}

    # ── cooldown ─────────────────────────────────────────────────────────────
    def _cooldown_ok(self, kind: AnomalyKind, now: float) -> bool:
        last = self._last_alert.get(kind)
        if last is not None and (now - last) < self._cooldown:
            return False
        self._last_alert[kind] = now
        return True

    def _alert(self, kind: AnomalyKind, detail: str) -> Optional[SentinelAlert]:
        now = time.monotonic()
        if not self._cooldown_ok(kind, now):
            return None
        return SentinelAlert(kind=kind, detail=detail, ts=time.time())

    # ── detectors (sync, return an alert or None) ────────────────────────────
    def note_cost(self, *, spent: float, cap: float) -> Optional[SentinelAlert]:
        try:
            if cap <= 0:
                return None
            if float(spent) >= self._cost_frac * float(cap):
                pct = 100.0 * float(spent) / float(cap)
                return self._alert(
                    AnomalyKind.COST_CAP_90,
                    f"spend ${float(spent):.2f} / ${float(cap):.2f} ({pct:.0f}% of cap)",
                )
        except Exception:  # noqa: BLE001
            return None
        return None

    def note_provider_result(self, status_code: int) -> Optional[SentinelAlert]:
        try:
            code = int(status_code)
            if 500 <= code <= 599:
                self._consec_5xx += 1
                if self._consec_5xx >= self._max_5xx:
                    return self._alert(
                        AnomalyKind.CONSECUTIVE_5XX,
                        f"{self._consec_5xx} consecutive provider 5xx (last={code}) "
                        f"beyond backoff budget",
                    )
            else:
                self._consec_5xx = 0  # any non-5xx resets the streak
        except Exception:  # noqa: BLE001
            return None
        return None

    def note_safety_refusal(self, detail: str) -> Optional[SentinelAlert]:
        return self._alert(AnomalyKind.REFUSED_SAFETY, str(detail or "safety refusal"))

    # ── dispatch (async, fail-soft) ──────────────────────────────────────────
    async def dispatch(self, alert: Optional[SentinelAlert], *, poster: Optional[_Poster] = None) -> bool:
        if alert is None:
            return False
        url = self._webhook
        if not url:
            logger.debug("[Sentinel] no webhook url — alert %s not dispatched", alert.kind.value)
            return False
        msg = f"[JARVIS SENTINEL] {alert.kind.value.upper()}: {alert.detail}"
        payload: Dict[str, Any] = {
            "text": msg, "content": msg,         # Slack + Discord compatible
            "kind": alert.kind.value, "ts": alert.ts,
        }
        try:
            send = poster or _default_poster
            rc = await send(url, payload)
            ok = 200 <= int(rc) < 300
            (logger.warning if not ok else logger.info)(
                "[Sentinel] %s dispatched rc=%s", alert.kind.value, rc)
            return ok
        except Exception as exc:  # noqa: BLE001 — a dead webhook never crashes the soak
            logger.warning("[Sentinel] dispatch swallowed (%s): %s", alert.kind.value, exc)
            return False


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


async def _default_poster(url: str, payload: Dict[str, Any]) -> int:
    """Stdlib urllib POST off the event loop, hard-timeout. Returns the HTTP
    status (0 on transport error). NEVER raises."""
    import asyncio

    def _post() -> int:
        import urllib.request
        try:
            req = urllib.request.Request(
                url, data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST",
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return int(getattr(resp, "status", 200) or 200)
        except Exception:  # noqa: BLE001
            return 0
    return await asyncio.to_thread(_post)


# ── singleton + fire-and-forget hot-path hooks ──────────────────────────────
_singleton: Optional[TelemetrySentinel] = None


def get_sentinel() -> TelemetrySentinel:
    global _singleton
    if _singleton is None:
        _singleton = TelemetrySentinel()
    return _singleton


def reset_sentinel() -> None:
    global _singleton
    _singleton = None


_pending: set = set()


def _fire(coro) -> None:
    import asyncio
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None:
            t = loop.create_task(coro)
            _pending.add(t)
            t.add_done_callback(_pending.discard)
        else:
            asyncio.run(coro)
    except Exception:  # noqa: BLE001
        try:
            coro.close()
        except Exception:  # noqa: BLE001
            pass


def note_cost_nowait(*, spent: float, cap: float, poster: Optional[_Poster] = None) -> None:
    """Fire-and-forget cost check — alerts at ≥90% of cap. Gated + non-blocking."""
    if not telemetry_sentinel_enabled():
        return
    s = get_sentinel()
    alert = s.note_cost(spent=spent, cap=cap)
    if alert is not None:
        _fire(s.dispatch(alert, poster=poster))


def note_provider_result_nowait(status_code: int, *, poster: Optional[_Poster] = None) -> None:
    """Fire-and-forget provider-status check — alerts on a 5xx streak. Gated."""
    if not telemetry_sentinel_enabled():
        return
    s = get_sentinel()
    alert = s.note_provider_result(status_code)
    if alert is not None:
        _fire(s.dispatch(alert, poster=poster))


def note_safety_refusal_nowait(detail: str, *, poster: Optional[_Poster] = None) -> None:
    """Fire-and-forget safety-refusal alert (rogue FSM state). Gated."""
    if not telemetry_sentinel_enabled():
        return
    s = get_sentinel()
    alert = s.note_safety_refusal(detail)
    if alert is not None:
        _fire(s.dispatch(alert, poster=poster))


__all__ = [
    "telemetry_sentinel_enabled",
    "AnomalyKind",
    "SentinelAlert",
    "TelemetrySentinel",
    "get_sentinel",
    "reset_sentinel",
    "note_cost_nowait",
    "note_provider_result_nowait",
    "note_safety_refusal_nowait",
]

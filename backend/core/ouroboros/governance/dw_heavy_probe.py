"""Phase 12.2 Slice D — Heavy Probe (VRAM allocation verification).

Closes the chicken-and-egg gap between Slice G (modality micro-probe,
1 token, proves schema acceptance only) and Slice B/C (TtftObserver,
records TTFT from real ops — empty until production use).

A model that passes the 1-token Slice G probe AND has zero observer
samples can still be in cold storage when a real op hits it. The
heavy probe issues a deliberate sized completion (50-200 tokens by
default) under cost+rate guards and feeds TTFT into the same observer
Slice C consumes for cold-storage detection + promotion gating.

Design invariants:

  * **Cost-bounded** — daily USD budget atomic on disk; probes refuse
    when exhausted. NOT a tuning parameter; a hard fence.
  * **Rate-bounded** — per-model probe interval (default 10 min) so a
    single model can't dominate the budget. Globally rate-bounded by
    scheduler cadence + per-cycle limits.
  * **Read-only authority** — heavy prober NEVER mutates
    PromotionLedger directly. It only feeds TtftObserver. Promotion
    decisions remain Slice C's surface, classifier's authority.
  * **Skip-when-redundant** — already-promoted models skipped (signal
    proven); cold-storage flagged models skipped (signal already
    present); TERMINAL_OPEN breakered models skipped (probe won't
    help and may waste budget).
  * **NEVER raises out of public methods.** Defensive try/except at
    every external surface; observer / sentinel / breaker faults are
    swallowed at the seam.

Authority surface:
  * ``heavy_probe_enabled()`` — master flag, re-read at call time.
  * ``HeavyProbeResult`` — frozen dataclass.
  * ``HeavyProbeBudget`` — daily-rollover USD ledger with atomic disk.
  * ``HeavyProber`` — fires one probe, no scheduling.
  * ``HeavyProbeScheduler`` — async loop calling HeavyProber per model.

Default flips to ``true`` at Phase 12.2 Slice E graduation. Hot-revert:
``export JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED=false`` immediately
disables the scheduler + new probes (in-flight probe completes).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Master flag + tunables
# ---------------------------------------------------------------------------


def heavy_probe_enabled() -> bool:
    """``JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED`` (default ``true`` —
    graduated in Phase 12.2 Slice E).

    Master kill switch. Hot-revert path: ``export
    JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED=false`` → scheduler refuses
    to spawn, ad-hoc ``HeavyProber.probe()`` calls return a no-op
    result with ``error=master_flag_off``, budget ledger untouched."""
    raw = os.environ.get(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_ENABLED", "",
    ).strip().lower()
    if raw == "":
        return True  # graduated default
    return raw in ("1", "true", "yes", "on")


def _probe_tokens() -> int:
    """``JARVIS_TOPOLOGY_HEAVY_PROBE_TOKENS`` (default 50).

    Completion size of the heavy probe. Larger = more confident
    VRAM-warm signal but more cost. 50 tokens at $0.40/M output is
    $0.00002 per probe — negligible per-call but bounds the daily
    sweep cost predictably."""
    try:
        return max(1, int(
            os.environ.get(
                "JARVIS_TOPOLOGY_HEAVY_PROBE_TOKENS", "50",
            ).strip()
        ))
    except (ValueError, TypeError):
        return 50


def _probe_interval_s() -> int:
    """``JARVIS_TOPOLOGY_HEAVY_PROBE_INTERVAL_S`` (default 600).

    Per-model minimum interval between heavy probes. A model probed
    in the last ``interval_s`` seconds is skipped. 600s = 10 minutes
    matches the typical VRAM eviction window of self-hosted endpoints."""
    try:
        return max(60, int(
            os.environ.get(
                "JARVIS_TOPOLOGY_HEAVY_PROBE_INTERVAL_S", "600",
            ).strip()
        ))
    except (ValueError, TypeError):
        return 600


def _probe_timeout_s() -> float:
    """``JARVIS_TOPOLOGY_HEAVY_PROBE_TIMEOUT_S`` (default 30).

    Single-probe wall-clock timeout. A probe that doesn't return
    first content within this window is treated as cold-storage
    grade slow (records the timeout as a ``ttft_ms`` ceiling so the
    observer's cold-storage gate fires)."""
    try:
        return max(1.0, float(
            os.environ.get(
                "JARVIS_TOPOLOGY_HEAVY_PROBE_TIMEOUT_S", "30",
            ).strip()
        ))
    except (ValueError, TypeError):
        return 30.0


def _budget_daily_usd() -> float:
    """``JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_USD_DAILY`` (default 0.05).

    Daily USD ceiling. Auto-resets at UTC midnight (rolling
    calendar-day, not 24h sliding window — operators reason about
    day-boundary spend, not floating intervals)."""
    try:
        return max(0.0, float(
            os.environ.get(
                "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_USD_DAILY", "0.05",
            ).strip()
        ))
    except (ValueError, TypeError):
        return 0.05


def _scheduler_cycle_s() -> int:
    """``JARVIS_TOPOLOGY_HEAVY_PROBE_CYCLE_S`` (default 120).

    How often the scheduler wakes to look for probe candidates.
    Smaller = more reactive, larger = less overhead. The probe
    interval governs which models are eligible; cycle just sets
    the polling cadence."""
    try:
        return max(10, int(
            os.environ.get(
                "JARVIS_TOPOLOGY_HEAVY_PROBE_CYCLE_S", "120",
            ).strip()
        ))
    except (ValueError, TypeError):
        return 120


def _budget_state_path() -> Path:
    """``JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_PATH`` (default
    ``.jarvis/dw_heavy_probe_budget.json``)."""
    raw = os.environ.get(
        "JARVIS_TOPOLOGY_HEAVY_PROBE_BUDGET_PATH",
        ".jarvis/dw_heavy_probe_budget.json",
    ).strip()
    return Path(raw)


# DW pricing (read at call time so test fixtures can pin)
def _dw_input_per_m() -> float:
    try:
        return float(os.environ.get(
            "JARVIS_DW_INPUT_COST_PER_M", "0.10",
        ).strip())
    except (ValueError, TypeError):
        return 0.10


def _dw_output_per_m() -> float:
    try:
        return float(os.environ.get(
            "JARVIS_DW_OUTPUT_COST_PER_M", "0.40",
        ).strip())
    except (ValueError, TypeError):
        return 0.40


# Probe prompt is fixed + minimal so input cost stays ~constant. The
# completion size dominates output cost. Prompt is deliberately
# generic — we want VRAM-warm observation, not capability discovery.
_PROBE_PROMPT = "Reply with one short sentence about clouds."
_PROBE_PROMPT_INPUT_TOKENS = 10  # generous estimate for prompt+system


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------


BUDGET_SCHEMA_VERSION = "heavy_probe_budget.1"


@dataclass(frozen=True)
class HeavyProbeResult:
    """One heavy-probe outcome. Frozen + hashable.

    ``success`` is True iff a content chunk was received within the
    probe timeout. ``ttft_ms`` is set on success; on timeout/failure
    it's the timeout ceiling (signals cold-storage to the observer
    asymmetrically — slow OR failed both register as "not warm")."""
    model_id: str
    success: bool
    ttft_ms: int
    total_latency_ms: int
    cost_usd: float
    error: str = ""
    sample_unix: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Atomic disk I/O (mirrored from posture_store.py / dw_promotion_ledger.py)
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
# Budget — daily-rollover USD ledger
# ---------------------------------------------------------------------------


def _utc_today() -> str:
    """Calendar day in UTC as ``YYYY-MM-DD``. Used as the rollover
    key — at UTC midnight the per-day spend resets."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class HeavyProbeBudget:
    """Daily-USD heavy-probe spend ledger.

    Auto-rollover at UTC midnight (compares ``current_day`` against
    today; mismatch → reset). Atomic temp+rename persistence so a
    crashed process doesn't lose accumulation.

    NEVER raises out of public methods. ``check_and_charge`` returns
    True iff the charge fits within remaining daily budget; on True
    the spend is committed + persisted, on False budget remains
    untouched."""

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
    ) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._current_day: str = _utc_today()
        self._spent_today_usd: float = 0.0
        self._loaded = False

    def _resolved_path(self) -> Path:
        return self._path if self._path is not None else _budget_state_path()

    def load(self) -> None:
        """Hydrate from disk. Missing/corrupt → start fresh.
        NEVER raises."""
        with self._lock:
            self._loaded = True
            p = self._resolved_path()
            if not p.exists():
                return
            try:
                payload = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "[HeavyProbeBudget] corrupt ledger at %s — starting "
                    "fresh (%s)", p, exc,
                )
                return
            if not isinstance(payload, Mapping):
                return
            if payload.get("schema_version") != BUDGET_SCHEMA_VERSION:
                return
            day = str(payload.get("current_day", "")).strip()
            try:
                spent = float(payload.get("spent_today_usd", 0.0) or 0.0)
            except (ValueError, TypeError):
                spent = 0.0
            today = _utc_today()
            if day != today:
                # Stale day — discard accumulated spend
                self._current_day = today
                self._spent_today_usd = 0.0
            else:
                self._current_day = day
                self._spent_today_usd = max(0.0, spent)

    def _save(self) -> None:
        try:
            _atomic_write(
                self._resolved_path(),
                json.dumps({
                    "schema_version": BUDGET_SCHEMA_VERSION,
                    "current_day": self._current_day,
                    "spent_today_usd": self._spent_today_usd,
                }, sort_keys=True, indent=2),
            )
        except OSError as exc:
            logger.warning(
                "[HeavyProbeBudget] save failed: %s — accumulated "
                "spend remains in memory only", exc,
            )

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load()

    def _maybe_rollover(self) -> None:
        """Reset accumulator if calendar day flipped since last touch.
        Called inside the lock by all mutators."""
        today = _utc_today()
        if today != self._current_day:
            self._current_day = today
            self._spent_today_usd = 0.0

    def remaining_usd(self) -> float:
        """Daily budget minus today's spend (post-rollover). NEVER
        raises."""
        self._ensure_loaded()
        with self._lock:
            self._maybe_rollover()
            cap = _budget_daily_usd()
            return max(0.0, cap - self._spent_today_usd)

    def spent_today_usd(self) -> float:
        self._ensure_loaded()
        with self._lock:
            self._maybe_rollover()
            return self._spent_today_usd

    def check_and_charge(self, cost_usd: float) -> bool:
        """Atomically check whether ``cost_usd`` fits + charge.
        Returns True iff committed. NEVER raises."""
        try:
            cost = float(cost_usd)
        except (ValueError, TypeError):
            return False
        if cost < 0:
            return False
        self._ensure_loaded()
        with self._lock:
            self._maybe_rollover()
            cap = _budget_daily_usd()
            if self._spent_today_usd + cost > cap:
                return False
            self._spent_today_usd += cost
            self._save()
            return True


# ---------------------------------------------------------------------------
# HeavyProber — fires one probe
# ---------------------------------------------------------------------------


class HeavyProber:
    """Single-shot heavy probe.

    Uses the supplied aiohttp session + DW base_url + api_key to issue
    a chat-completions request with ``max_tokens=_probe_tokens()``.
    Records first-chunk TTFT into the supplied observer (or the
    discovery-runner singleton when ``observer=None``).

    NEVER raises out of public methods. Returns a HeavyProbeResult
    with ``success=False`` + populated ``error`` on any failure path."""

    def __init__(
        self,
        *,
        budget: Optional[HeavyProbeBudget] = None,
    ) -> None:
        self._budget = budget

    async def probe(
        self,
        *,
        session: Any,
        model_id: str,
        base_url: str,
        api_key: str,
        observer: Optional[Any] = None,
    ) -> HeavyProbeResult:
        """Fire one heavy probe. Returns a result regardless of
        success — caller can branch on ``result.success``."""
        if not heavy_probe_enabled():
            return HeavyProbeResult(
                model_id=model_id,
                success=False,
                ttft_ms=0,
                total_latency_ms=0,
                cost_usd=0.0,
                error="master_flag_off",
            )
        if not model_id or not model_id.strip():
            return HeavyProbeResult(
                model_id="",
                success=False,
                ttft_ms=0,
                total_latency_ms=0,
                cost_usd=0.0,
                error="empty_model_id",
            )

        # Pre-flight cost estimate. Real cost may be slightly different
        # if the model emits fewer tokens than max_tokens, but the
        # ceiling check guards against a runaway probe.
        max_tokens = _probe_tokens()
        est_cost = self._estimate_cost(max_tokens)
        budget = self._budget
        if budget is not None and not budget.check_and_charge(est_cost):
            return HeavyProbeResult(
                model_id=model_id,
                success=False,
                ttft_ms=0,
                total_latency_ms=0,
                cost_usd=0.0,
                error="budget_exhausted",
            )

        # Issue probe. Defensive try/except — every failure path
        # yields a well-formed result, never a raised exception.
        t_start = time.monotonic()
        try:
            ttft_ms, total_ms, ok, err = await self._do_probe(
                session=session, model_id=model_id,
                base_url=base_url, api_key=api_key,
                max_tokens=max_tokens,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — defense-in-depth
            return HeavyProbeResult(
                model_id=model_id,
                success=False,
                ttft_ms=int(_probe_timeout_s() * 1000),
                total_latency_ms=int((time.monotonic() - t_start) * 1000),
                cost_usd=est_cost,
                error=f"unhandled:{type(exc).__name__}:{str(exc)[:80]}",
            )

        # Slice G — Failure Ignorance (operator directive 2026-04-28).
        # ConnectionTimeoutError + transport failures + empty-stream
        # cases are NETWORK FAILURES, not TTFT measurements. Feeding
        # them to the observer would poison the rolling stats — a
        # uniform-30s-timeout sample stream looks "consistent" by CV
        # math but is functionally meaningless. Only record genuine
        # first-chunk arrival measurements.
        #
        # The asymmetric ceiling-on-failure pattern from initial Slice D
        # was rejected: cold-storage detection should fall to the
        # modality ledger / terminal breaker for "endpoint dead" cases,
        # not to the TTFT observer.
        if ok:
            try:
                self._feed_observer(
                    model_id=model_id, ttft_ms=ttft_ms,
                    explicit_observer=observer,
                )
            except Exception:  # noqa: BLE001 — defensive
                pass
        # On failure: the observer is NOT updated. The HeavyProbeResult
        # still reports the timeout-ceiling ttft_ms for caller
        # introspection (logs, telemetry, debugging), but no sample
        # enters the warmth dataset.
        recorded_ttft = ttft_ms if ok else int(_probe_timeout_s() * 1000)
        return HeavyProbeResult(
            model_id=model_id,
            success=ok,
            ttft_ms=recorded_ttft,
            total_latency_ms=total_ms,
            cost_usd=est_cost,
            error=err,
        )

    @staticmethod
    def _estimate_cost(max_tokens: int) -> float:
        """Conservative upper-bound cost estimate: full max_tokens
        emitted + fixed prompt input. Real cost tracked by provider's
        own ledger; this is for budget pre-check only."""
        in_cost = (_PROBE_PROMPT_INPUT_TOKENS / 1_000_000.0) * _dw_input_per_m()
        out_cost = (max_tokens / 1_000_000.0) * _dw_output_per_m()
        return in_cost + out_cost

    @staticmethod
    def _feed_observer(
        *,
        model_id: str,
        ttft_ms: int,
        explicit_observer: Optional[Any],
    ) -> None:
        """Feed TTFT into the supplied observer or, when None, the
        discovery-runner singleton. Both paths defensive — observer
        faults swallowed."""
        if explicit_observer is not None:
            try:
                explicit_observer.record_ttft(model_id, ttft_ms)
            except Exception:  # noqa: BLE001 — defensive
                pass
            return
        try:
            from backend.core.ouroboros.governance.dw_discovery_runner import (
                get_ttft_observer,
            )
            obs = get_ttft_observer()
            if obs is not None:
                obs.record_ttft(model_id, ttft_ms)
        except Exception:  # noqa: BLE001 — defensive
            pass

    @staticmethod
    async def _do_probe(
        *,
        session: Any,
        model_id: str,
        base_url: str,
        api_key: str,
        max_tokens: int,
    ) -> Tuple[int, int, bool, str]:
        """Issue the SSE request + read until first content chunk OR
        timeout OR done. Returns (ttft_ms, total_ms, ok, error_str)."""
        body = {
            "model": model_id,
            "messages": [
                {"role": "user", "content": _PROBE_PROMPT},
            ],
            "max_tokens": max_tokens,
            "stream": True,
            "temperature": 0.0,
        }
        url = f"{base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        timeout_s = _probe_timeout_s()
        t_start = time.monotonic()
        ttft_ms = 0
        first_chunk_seen = False
        try:
            async with session.post(
                url, json=body, headers=headers,
            ) as resp:
                if resp.status >= 300:
                    body_text = ""
                    try:
                        body_text = (await resp.text())[:200]
                    except Exception:  # noqa: BLE001 — defensive
                        pass
                    return (
                        int(timeout_s * 1000),
                        int((time.monotonic() - t_start) * 1000),
                        False,
                        f"status_{resp.status}:{body_text}",
                    )
                # Read SSE until first non-empty content chunk
                while True:
                    try:
                        line = await asyncio.wait_for(
                            resp.content.readline(), timeout=timeout_s,
                        )
                    except asyncio.TimeoutError:
                        return (
                            int(timeout_s * 1000),
                            int((time.monotonic() - t_start) * 1000),
                            False,
                            "ttft_timeout",
                        )
                    if not line:
                        # Stream closed before first chunk → cold/dead
                        return (
                            int(timeout_s * 1000),
                            int((time.monotonic() - t_start) * 1000),
                            False,
                            "stream_closed_early",
                        )
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text or not text.startswith("data: "):
                        continue
                    data_str = text[6:]
                    if data_str == "[DONE]":
                        if not first_chunk_seen:
                            return (
                                int(timeout_s * 1000),
                                int((time.monotonic() - t_start) * 1000),
                                False,
                                "done_before_content",
                            )
                        break
                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue
                    delta = (chunk.get("choices") or [{}])[0].get(
                        "delta", {},
                    )
                    token = delta.get("content", "")
                    if token:
                        if not first_chunk_seen:
                            first_chunk_seen = True
                            ttft_ms = int(
                                (time.monotonic() - t_start) * 1000
                            )
                            # First chunk is the only one we need —
                            # close stream early to bound cost.
                            break
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — defense-in-depth
            return (
                int(timeout_s * 1000),
                int((time.monotonic() - t_start) * 1000),
                False,
                f"transport:{type(exc).__name__}:{str(exc)[:80]}",
            )
        if not first_chunk_seen:
            return (
                int(timeout_s * 1000),
                int((time.monotonic() - t_start) * 1000),
                False,
                "no_content",
            )
        return (
            ttft_ms,
            int((time.monotonic() - t_start) * 1000),
            True,
            "",
        )


# ---------------------------------------------------------------------------
# Scheduler — picks candidates, respects intervals + budget
# ---------------------------------------------------------------------------


class HeavyProbeScheduler:
    """Async loop that picks probe candidates from the catalog +
    fires probes via ``HeavyProber``.

    Skip rules (composable):
      * model already promoted (signal proven, no need)
      * model in cold-storage state (signal already present)
      * model in TERMINAL_OPEN breaker (probe would waste budget)
      * model probed within ``_probe_interval_s()`` (cooldown)
      * daily budget exhausted

    Cycle cadence: ``_scheduler_cycle_s()`` (default 120s). Each cycle
    walks the catalog, picks at most one model per cycle (cap on
    aggressive probing), fires the probe, sleeps until next cycle.

    NEVER raises. Cancellation honored at every await boundary."""

    def __init__(
        self,
        *,
        prober: HeavyProber,
        budget: HeavyProbeBudget,
    ) -> None:
        self._prober = prober
        self._budget = budget
        self._last_probed_at_unix: Dict[str, float] = {}
        self._lock = threading.RLock()

    async def run_cycle(
        self,
        *,
        session: Any,
        base_url: str,
        api_key: str,
        candidate_ids: Tuple[str, ...],
    ) -> Optional[HeavyProbeResult]:
        """One scheduler tick. Picks the first eligible candidate
        from ``candidate_ids`` (preserving caller's ranking) + fires
        a probe. Returns the probe result or None if nothing eligible.
        NEVER raises."""
        if not heavy_probe_enabled():
            return None
        for mid in candidate_ids:
            if not self._is_eligible(mid):
                continue
            try:
                with self._lock:
                    self._last_probed_at_unix[mid] = time.time()
                result = await self._prober.probe(
                    session=session,
                    model_id=mid,
                    base_url=base_url,
                    api_key=api_key,
                )
                logger.info(
                    "[HeavyProbe] model=%s success=%s ttft_ms=%d "
                    "total_ms=%d cost_usd=%.6f error=%s",
                    mid, result.success, result.ttft_ms,
                    result.total_latency_ms, result.cost_usd,
                    result.error or "-",
                )
                return result
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — defensive
                logger.debug(
                    "[HeavyProbe] cycle failed for model=%s: %s",
                    mid, exc, exc_info=True,
                )
                continue
        return None

    def _is_eligible(self, model_id: str) -> bool:
        """Apply skip rules. NEVER raises."""
        if not model_id or not model_id.strip():
            return False
        # Cooldown
        with self._lock:
            last = self._last_probed_at_unix.get(model_id)
        if last is not None and (time.time() - last) < _probe_interval_s():
            return False
        # Budget
        if self._budget.remaining_usd() <= 0:
            return False
        # Already-promoted / TERMINAL_OPEN / cold-storage skip rules.
        # Lazy lookups so test harnesses that don't wire these still work.
        try:
            from backend.core.ouroboros.governance.dw_discovery_runner import (
                _get_or_create_ledger, get_ttft_observer,
            )
            led = _get_or_create_ledger()
            if led is not None and led.is_promoted(model_id):
                return False
            obs = get_ttft_observer()
            if obs is not None:
                try:
                    if obs.is_cold_storage(model_id):
                        return False
                except Exception:  # noqa: BLE001 — defensive
                    pass
        except Exception:  # noqa: BLE001 — defensive
            pass
        # TERMINAL_OPEN breaker check
        try:
            from backend.core.ouroboros.governance.topology_sentinel import (
                get_default_sentinel,
            )
            sent = get_default_sentinel()
            if sent is not None and hasattr(sent, "is_terminal_open"):
                try:
                    if sent.is_terminal_open(model_id):
                        return False
                except Exception:  # noqa: BLE001 — defensive
                    pass
        except Exception:  # noqa: BLE001 — defensive
            pass
        return True


__all__ = [
    "BUDGET_SCHEMA_VERSION",
    "HeavyProbeBudget",
    "HeavyProbeResult",
    "HeavyProber",
    "HeavyProbeScheduler",
    "heavy_probe_enabled",
]

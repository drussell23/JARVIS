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


def _ttft_ref_params_b() -> float:
    """``JARVIS_HEAVY_PROBE_TTFT_REF_PARAMS_B`` (default 14.0).

    Reference parameter count (billions) that maps to the **base**
    probe timeout. Deliberately defaults to the ``standard``-route
    ``min_params_b`` (dw_catalog_classifier) so a model exactly at
    the eligibility floor gets the unscaled base, and larger models
    scale up from there — leveraging the EXISTING min_params_b datum,
    not a new magic number. Read at call time."""
    try:
        return max(0.1, float(os.environ.get(
            "JARVIS_HEAVY_PROBE_TTFT_REF_PARAMS_B", "14.0",
        ).strip()))
    except (ValueError, TypeError):
        return 14.0


def _ttft_max_s() -> float:
    """``JARVIS_HEAVY_PROBE_TTFT_MAX_S`` (default 300.0).

    Absolute ceiling on the adaptive probe timeout — a probe can be
    generous for a 397B cold-start but must never be unbounded.
    Read at call time."""
    try:
        return max(1.0, float(os.environ.get(
            "JARVIS_HEAVY_PROBE_TTFT_MAX_S", "300.0",
        ).strip()))
    except (ValueError, TypeError):
        return 300.0


def _adaptive_probe_timeout_s(
    parameter_count_b: "Optional[float]" = None,
) -> float:
    """Probe TTFT allowance scaled by model geometry.

    A massive local model's first token is gated by cold-start VRAM
    weight-load, which scales with parameter count — the static 30s
    ceiling false-negative-excluded the 35B–397B general models in
    v18 ``bt-2026-05-16-175621`` (`done_before_content @ ttft_ms=
    30000`), collapsing the ``standard``-route DW catalog and making
    Claude a single point of failure.

    Scaling (all env-tunable — no hardcoded seconds):

      base       = :func:`_probe_timeout_s`  (small-model floor)
      multiplier = max(1.0, params_b / ref_params_b)
      adaptive   = min(:func:`_ttft_max_s`, base * multiplier)

    Invariants (spine-pinned):
      * **Floor** — never below ``base`` (a 4B model → multiplier
        1.0 → unchanged strict timeout; unknown/None/≤0 params →
        ``base``, byte-identical to legacy behavior).
      * **Ceiling** — never above :func:`_ttft_max_s`.
      * **Monotonic** — non-decreasing in ``parameter_count_b``.

    NEVER raises."""
    base = _probe_timeout_s()
    try:
        if parameter_count_b is None:
            return base
        p = float(parameter_count_b)
        if p <= 0.0:
            return base
        ref = _ttft_ref_params_b()
        multiplier = p / ref
        if multiplier < 1.0:
            multiplier = 1.0
        scaled = base * multiplier
        ceiling = _ttft_max_s()
        if scaled > ceiling:
            scaled = ceiling
        return scaled if scaled >= base else base
    except (ValueError, TypeError):
        return base


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
        parameter_count_b: Optional[float] = None,
    ) -> HeavyProbeResult:
        """Fire one heavy probe. Returns a result regardless of
        success — caller can branch on ``result.success``.

        ``parameter_count_b`` (from the model's catalog card) scales
        the probe TTFT allowance via :func:`_adaptive_probe_timeout_s`
        so massive cold-starting models aren't false-negative-graded.
        ``None`` → legacy static base (byte-identical)."""
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
                parameter_count_b=parameter_count_b,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — defense-in-depth
            return HeavyProbeResult(
                model_id=model_id,
                success=False,
                ttft_ms=int(
                    _adaptive_probe_timeout_s(parameter_count_b) * 1000
                ),
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
        recorded_ttft = ttft_ms if ok else int(
            _adaptive_probe_timeout_s(parameter_count_b) * 1000
        )
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
        parameter_count_b: Optional[float] = None,
    ) -> Tuple[int, int, bool, str]:
        """Issue the SSE request + read until first content chunk OR
        timeout OR done. Returns (ttft_ms, total_ms, ok, error_str).

        The first-content wait is bounded by
        :func:`_adaptive_probe_timeout_s` (scaled by
        ``parameter_count_b`` — a 397B cold-start gets a far larger
        allowance than a 4B model; ``None`` → static base)."""
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
        # Slice 2B-ii.2 — Aegis Provider Bridge wire. When
        # JARVIS_AEGIS_ENABLED is true: dw_authorization_header()
        # returns {} (real DOUBLEWORD_API_KEY confiscated to daemon;
        # ``api_key`` arg is ignored) and a per-call X-JARVIS-Lease
        # is acquired. When disabled: legacy ``Bearer {api_key}``
        # restored byte-identically. Closes the missing_lease_header
        # 401 surfaced by re-detonation soak bt-2026-05-24-225714 —
        # this is the 11th DW upstream call site that Slice 2B-ii
        # missed (lives outside doubleword_provider.py).
        from backend.core.ouroboros.governance.aegis_provider_bridge import (
            acquire_call_lease as _aegis_acquire_call_lease,
            compose_dw_bearer_header as _aegis_compose_bearer,
            dw_authorization_header as _aegis_dw_auth_header,
            merge_lease_into_session_headers as _aegis_merge_lease_headers,
        )
        from backend.core.ouroboros.aegis.client import is_enabled as _aegis_is_enabled
        if _aegis_is_enabled():
            # Aegis-on: real key already confiscated; ignore caller's
            # api_key; auth header from bridge (empty dict).
            _call_headers: Dict[str, str] = dict(_aegis_dw_auth_header())
        else:
            # Aegis-off: legacy direct-to-DW path; caller-supplied
            # bearer composed via the bridge (single seam — the
            # literal ``"Bearer "`` string lives only inside
            # aegis_provider_bridge._compose_bearer).
            _call_headers = dict(_aegis_compose_bearer(api_key))
        _call_headers["Content-Type"] = "application/json"
        _aegis_lease = await _aegis_acquire_call_lease(
            op_id=f"dw-heavy-probe:{model_id}",
            route="background",
            estimated_cost_usd=0.001,
        )
        headers = _aegis_merge_lease_headers(_call_headers, _aegis_lease)
        timeout_s = _adaptive_probe_timeout_s(parameter_count_b)
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
                    # Task #86 — entitlement-block disambiguation.
                    # The legacy ``status_{N}:body`` error string was
                    # opaque to downstream observability: an account-
                    # entitlement rejection (per-model permanent) and a
                    # bad-auth failure (global transient) produced the
                    # same payload, so neither could route the model to
                    # TERMINAL_OPEN.  Now the classifier discriminates
                    # via DW's own response-body marker — on
                    # ENTITLEMENT_BLOCKED, the structured error string
                    # carries the marker for log forensics and lets a
                    # downstream consumer (caller of ``probe()``) flip
                    # the breaker to TERMINAL_OPEN without re-parsing
                    # the body.  See dw_entitlement_classifier.py.
                    from backend.core.ouroboros.governance.dw_entitlement_classifier import (  # noqa: E501
                        classify_4xx,
                        KIND_ENTITLEMENT_BLOCKED,
                    )
                    _ent = classify_4xx(resp.status, body_text)
                    if _ent.kind == KIND_ENTITLEMENT_BLOCKED:
                        _err = (
                            f"entitlement_blocked:{_ent.matched_marker}:"
                            f"status_{resp.status}"
                        )
                    else:
                        _err = f"status_{resp.status}:{body_text}"
                    return (
                        int(timeout_s * 1000),
                        int((time.monotonic() - t_start) * 1000),
                        False,
                        _err,
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
        param_by_id: "Optional[Mapping[str, float]]" = None,
    ) -> Optional[HeavyProbeResult]:
        """One scheduler tick. Picks the first eligible candidate
        from ``candidate_ids`` (preserving caller's ranking) + fires
        a probe. Returns the probe result or None if nothing eligible.
        NEVER raises.

        ``param_by_id`` maps model_id → parameter_count_b (built by
        the caller from the catalog it already holds). The matched
        value scales the probe TTFT (adaptive cold-start allowance).
        ``None`` / missing key → static base (byte-identical)."""
        if not heavy_probe_enabled():
            return None
        _pmap = param_by_id or {}
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
                    parameter_count_b=_pmap.get(mid),
                )
                logger.info(
                    "[HeavyProbe] model=%s success=%s ttft_ms=%d "
                    "total_ms=%d cost_usd=%.6f error=%s",
                    mid, result.success, result.ttft_ms,
                    result.total_latency_ms, result.cost_usd,
                    result.error or "-",
                )
                # Task #86b — autonomous entitlement adaptation.
                # When the structured error string from _do_probe starts
                # with "entitlement_blocked:" (set by Task #86's
                # classifier dispatch), route the detection to
                # topology_sentinel.report_failure(is_terminal=True).
                # This flips the model's breaker to TERMINAL_OPEN, which
                # _is_eligible() already honors (line 791-795), and
                # which dw_catalog_classifier excludes from future route
                # assignments.  No hardcoded model lookup, no operator
                # config — the system DISCOVERS entitlement boundaries
                # from DW's own response bodies and prunes them at the
                # next discovery cycle.  Best-effort: sentinel unwired
                # in unit tests is fine, the probe still returns
                # normally so the result is observable.
                if (
                    not result.success
                    and result.error
                    and result.error.startswith("entitlement_blocked:")
                ):
                    try:
                        from backend.core.ouroboros.governance.topology_sentinel import (  # noqa: E501
                            get_default_sentinel,
                            FailureSource,
                        )
                        sent = get_default_sentinel()
                        if sent is not None and hasattr(sent, "report_failure"):
                            sent.report_failure(
                                model_id=mid,
                                source=FailureSource.HEAVY_PROBE_FAIL,
                                detail=result.error,
                                status_code=403,
                                response_body=result.error,
                                is_terminal=True,
                            )
                            logger.info(
                                "[HeavyProbe] entitlement_blocked routed "
                                "to TERMINAL_OPEN: model=%s detail=%r",
                                mid, result.error[:120],
                            )
                    except Exception:  # noqa: BLE001 — defensive
                        logger.debug(
                            "[HeavyProbe] entitlement→sentinel route "
                            "failed for model=%s", mid, exc_info=True,
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

"""DW Capacity Probe — Slice 34 substrate (Phase 0).

Out-of-band diagnostic probe. Runs N=10 trials at each of 4 prompt
sizes against a target DW model, records every call into
:class:`DWCapacityLedger`, and produces a per-size summary.

# Why "out-of-band"

The probe MUST be invokable independently of the full O+V harness +
sensor + intake + Aegis daemon — operator binding §48.7.2: *"Isolate
the variable — is it the harness or the endpoint?"* This module
takes only a provider instance + ledger; zero coupling to
orchestrator, sensors, or governance.

# Composition

  * **Provider:** uses ``DoublewordProvider.prompt_only()`` —
    the lowest-level surface that exercises the same HTTP transport
    + Aegis bearer + per-call lease + Slice 28 timeout math that
    production dispatch uses.
  * **Ledger:** records every trial into the same
    :class:`DWCapacityLedger` production reads. Probe data + soak
    data accumulate in the same place so :class:`DWPerShapeStats`
    sees both.
  * **No retry loop, no Venom, no orchestrator:** isolation is the
    point. If the probe succeeds where the harness fails, the
    delta is the harness — not the endpoint.

# Hypotheses the probe disambiguates (§48.7.3)

  (a) **Account capacity:** probe times out across all prompt sizes
      with relatively uniform latency → endpoint serving capacity
      hit — operator-side fix needed.
  (b) **Slice 28 budget math:** probe succeeds at high latency (e.g.
      30-60 s) within manually-elevated timeout → static formula was
      under-budgeting — tune ``JARVIS_ADAPTIVE_TIER0_HEAVY_SCALAR``.
  (c) **Prompt complexity:** probe succeeds at small sizes but
      timeouts at large → DW's effective input-size ceiling; refactor
      prompts or compose §48.9.5 prefix trie.
  (d) **Network/regional latency:** baseline RTT > ½ of typical
      response time → physical-path issue; operator switches VPN /
      region.

# Public surface

  * :class:`ProbeResult` — frozen per-size summary
  * :class:`DWCapacityProbe.probe` — async entry point
  * :func:`build_capacity_probe_from_default_provider` — convenience
    factory used by the operator-runnable script
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from backend.core.ouroboros.governance.dw_capacity_ledger import (
    DWCallRecord,
    DWCapacityLedger,
    get_default_ledger,
)


logger = logging.getLogger("Ouroboros.DWCapacityProbe")


# ============================================================================
# Probe configuration
# ============================================================================


_DEFAULT_PROMPT_SIZES: List[int] = [1024, 5120, 20480, 51200]
_DEFAULT_TRIALS_PER_SIZE: int = 10
_DEFAULT_TIMEOUT_PER_CALL_S: float = 60.0
_DEFAULT_PROBE_CALLER: str = "dw_capacity_probe"

# Templated probe prompt — deterministic, repeatable, exercises code-gen
# instinct without depending on any external data.
_PROBE_TEMPLATE = (
    "You are a helpful assistant. Produce a single Python function "
    "called `compute_sum` that takes a list of integers and returns "
    "the sum. Include a one-sentence docstring. Output only the "
    "function definition. "
    "Padding for size testing follows; please ignore it: "
)


def _build_probe_prompt(target_chars: int) -> str:
    """Construct a prompt of approximately ``target_chars`` size.

    Uses ``_PROBE_TEMPLATE`` as the load-bearing instruction + ASCII
    padding ('x') to hit the target size. Deterministic — same input
    size produces same prompt across runs."""
    if target_chars <= len(_PROBE_TEMPLATE):
        return _PROBE_TEMPLATE
    padding_chars = target_chars - len(_PROBE_TEMPLATE)
    return _PROBE_TEMPLATE + ("x" * padding_chars)


# ============================================================================
# Result types
# ============================================================================


@dataclass(frozen=True)
class ProbeTrial:
    """One probe trial's empirical outcome."""

    target_size: int
    outcome: str               # ok / timeout / error
    total_elapsed_ms: float
    response_chars: int = 0
    error_class: str = ""
    error_detail: str = ""


@dataclass(frozen=True)
class ProbeResult:
    """Per-size aggregate. Captures enough data to disambiguate the
    4 hypotheses from §48.7.1."""

    model_id: str
    target_size: int
    trials_run: int
    successes: int
    timeouts: int
    other_failures: int
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float
    min_ms: float
    avg_response_chars: float
    trials: List[ProbeTrial] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        return (self.successes / self.trials_run) if self.trials_run else 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "model_id": self.model_id,
            "target_size": self.target_size,
            "trials_run": self.trials_run,
            "successes": self.successes,
            "timeouts": self.timeouts,
            "other_failures": self.other_failures,
            "success_rate": self.success_rate,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
            "max_ms": self.max_ms,
            "min_ms": self.min_ms,
            "avg_response_chars": self.avg_response_chars,
        }


# ============================================================================
# Probe
# ============================================================================


class DWCapacityProbe:
    """Out-of-band capacity diagnostic.

    Composes any object that exposes ``async prompt_only(prompt: str,
    *, model_id: str = ..., timeout_s: float = ...) -> str`` — the
    canonical DW provider surface. Tests inject fakes; production
    code uses ``build_capacity_probe_from_default_provider()``.

    NEVER raises into the caller. Per-trial errors are captured as
    ``ProbeTrial(outcome=...)`` entries; aggregate ``ProbeResult``
    is always populated.
    """

    def __init__(
        self,
        *,
        provider: Any,
        ledger: Optional[DWCapacityLedger] = None,
        prompt_builder: Optional[Callable[[int], str]] = None,
    ) -> None:
        self._provider = provider
        self._ledger = ledger or get_default_ledger()
        self._prompt_builder = prompt_builder or _build_probe_prompt

    async def probe(
        self,
        *,
        model_id: str,
        prompt_sizes: Optional[List[int]] = None,
        trials_per_size: int = _DEFAULT_TRIALS_PER_SIZE,
        timeout_per_call_s: float = _DEFAULT_TIMEOUT_PER_CALL_S,
        caller: str = _DEFAULT_PROBE_CALLER,
    ) -> List[ProbeResult]:
        """Run the full probe matrix. Returns one
        :class:`ProbeResult` per ``prompt_sizes`` entry.

        Args:
          model_id: DW model to target.
          prompt_sizes: input sizes to test. Defaults to
            ``[1024, 5120, 20480, 51200]`` (1KB, 5KB, 20KB, 50KB).
          trials_per_size: N trials per size (default 10).
          timeout_per_call_s: per-call timeout. Set generously
            (default 60s) — probe wants to MEASURE actual response
            time, not exit at the production Slice 28 budget.
          caller: ledger ``caller`` field for filtering probe data
            from production data in later analysis.

        Returns:
          List of ProbeResult, one per prompt size, in input order.
        """
        sizes = list(prompt_sizes or _DEFAULT_PROMPT_SIZES)
        results: List[ProbeResult] = []
        for size in sizes:
            trials: List[ProbeTrial] = []
            for trial_idx in range(int(max(1, trials_per_size))):
                trial = await self._run_one_trial(
                    model_id=model_id,
                    size=size,
                    timeout_s=timeout_per_call_s,
                    caller=caller,
                )
                trials.append(trial)
                logger.info(
                    "[DWCapacityProbe] model=%s size=%d trial=%d/%d "
                    "outcome=%s elapsed_ms=%.1f response_chars=%d",
                    model_id, size, trial_idx + 1,
                    max(1, trials_per_size),
                    trial.outcome, trial.total_elapsed_ms,
                    trial.response_chars,
                )
            results.append(_aggregate_trials(model_id, size, trials))
        return results

    async def _run_one_trial(
        self,
        *,
        model_id: str,
        size: int,
        timeout_s: float,
        caller: str,
    ) -> ProbeTrial:
        """Single probe trial. Records into ledger; never raises."""
        prompt = self._prompt_builder(size)
        actual_prompt_chars = len(prompt)
        t0 = time.monotonic()
        outcome = "error"
        response_text = ""
        error_class = ""
        error_detail = ""
        try:
            response_text = await asyncio.wait_for(
                self._call_provider(prompt, model_id=model_id),
                timeout=timeout_s,
            )
            outcome = "ok"
        except asyncio.TimeoutError:
            outcome = "timeout"
            error_class = "TimeoutError"
            error_detail = f"probe timeout after {timeout_s:.1f}s"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            outcome = "error"
            error_class = type(exc).__name__
            error_detail = str(exc)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        response_chars = len(response_text or "")
        # Record into ledger (await — but if it fails, trial still
        # captured)
        try:
            await self._ledger.record_call(DWCallRecord(
                timestamp_unix=time.time(),
                model_id=model_id,
                route="probe",
                prompt_chars=actual_prompt_chars,
                outcome=outcome,
                ttft_ms=None,  # prompt_only doesn't expose TTFT
                total_elapsed_ms=elapsed_ms,
                response_tokens=None,
                response_chars=response_chars,
                cost_usd=0.0,  # probe doesn't track cost — diagnostic only
                error_class=error_class,
                error_detail=error_detail,
                caller=caller,
            ))
        except Exception:  # noqa: BLE001 — never raise from probe
            pass
        return ProbeTrial(
            target_size=size,
            outcome=outcome,
            total_elapsed_ms=elapsed_ms,
            response_chars=response_chars,
            error_class=error_class,
            error_detail=error_detail,
        )

    async def _call_provider(
        self,
        prompt: str,
        *,
        model_id: str,
    ) -> str:
        """Wrapped provider call. Accepts any provider-shaped object
        with ``async prompt_only(prompt, model_id=...) -> str``.

        Falls back to plain ``prompt_only(prompt)`` for older providers
        that don't accept ``model_id`` kwarg — minimal surface, max
        compatibility."""
        # Try the modern signature first
        try:
            return await self._provider.prompt_only(
                prompt, model_id=model_id,
            )
        except TypeError:
            # Legacy provider — drop the kwarg
            return await self._provider.prompt_only(prompt)


def _aggregate_trials(
    model_id: str,
    size: int,
    trials: List[ProbeTrial],
) -> ProbeResult:
    """Pure-function aggregation. Never raises."""
    n = len(trials)
    if n == 0:
        return ProbeResult(
            model_id=model_id,
            target_size=size,
            trials_run=0,
            successes=0,
            timeouts=0,
            other_failures=0,
            p50_ms=0.0, p95_ms=0.0, p99_ms=0.0,
            max_ms=0.0, min_ms=0.0,
            avg_response_chars=0.0,
            trials=[],
        )
    successes = sum(1 for t in trials if t.outcome == "ok")
    timeouts = sum(1 for t in trials if t.outcome == "timeout")
    other = n - successes - timeouts
    latencies = sorted(t.total_elapsed_ms for t in trials)
    response_chars = [t.response_chars for t in trials if t.outcome == "ok"]
    avg_response = (
        sum(response_chars) / len(response_chars) if response_chars else 0.0
    )
    return ProbeResult(
        model_id=model_id,
        target_size=size,
        trials_run=n,
        successes=successes,
        timeouts=timeouts,
        other_failures=other,
        p50_ms=_percentile(latencies, 0.50),
        p95_ms=_percentile(latencies, 0.95),
        p99_ms=_percentile(latencies, 0.99),
        max_ms=max(latencies),
        min_ms=min(latencies),
        avg_response_chars=avg_response,
        trials=list(trials),
    )


def _percentile(sorted_samples: List[float], q: float) -> float:
    n = len(sorted_samples)
    if n == 0:
        return 0.0
    idx = max(0, min(n - 1, int(round(q * n)) - 1))
    return float(sorted_samples[idx])


# ============================================================================
# Convenience factory for the operator-runnable script
# ============================================================================


def build_capacity_probe_from_default_provider() -> DWCapacityProbe:
    """Build a probe pointing at the default DW provider + default
    ledger. Used by ``scripts/dw_capacity_probe.py``.

    Lazy import — avoids coupling this module's import path to the
    full provider stack when only the substrate is needed (tests
    import this module without instantiating a real provider).
    """
    from backend.core.ouroboros.governance.doubleword_provider import (
        DoublewordProvider,
    )
    provider = DoublewordProvider()
    return DWCapacityProbe(provider=provider)


# ============================================================================
# Hypothesis classifier (Phase 0 → Phase 1 bridge)
# ============================================================================


def classify_probe_results(
    results: List[ProbeResult],
) -> Dict[str, Any]:
    """Apply §48.7.1's 4-hypothesis decision matrix to the probe
    output. Returns a structured verdict for operators + downstream
    Phase 1 tooling.

    Verdict keys:
      hypothesis            : str — "a"|"b"|"c"|"d"|"mixed"|"undetermined"
      confidence            : float (0-1)
      reasoning             : str (human-readable)
      recommended_action    : str (next operator step)
      per_size_summary      : list of dicts

    This is heuristic — operator binding "intelligent" not "ML-trained."
    Single-pass deterministic over the probe data. Pure function.
    """
    if not results:
        return {
            "hypothesis": "undetermined",
            "confidence": 0.0,
            "reasoning": "no probe results provided",
            "recommended_action": "re-run probe",
            "per_size_summary": [],
        }

    summaries = [r.to_dict() for r in results]
    # Sort by target_size ascending
    by_size = sorted(results, key=lambda r: r.target_size)

    # Heuristic decisions
    all_failed = all(r.success_rate == 0.0 for r in by_size)
    all_succeeded = all(r.success_rate >= 0.8 for r in by_size)
    size_correlated_failure = (
        by_size[0].success_rate >= 0.5
        and by_size[-1].success_rate <= 0.2
    )
    high_latency_succeeded = any(
        r.success_rate >= 0.5 and r.p95_ms >= 30_000.0 for r in by_size
    )

    if all_failed:
        # Hypothesis (a) or (d) — endpoint unreachable or fundamentally over budget
        smallest = by_size[0]
        if smallest.p95_ms < 5_000.0:
            # Failed FAST → network/auth, not capacity
            return {
                "hypothesis": "d",
                "confidence": 0.7,
                "reasoning": (
                    f"All sizes failed with p95<5s on smallest "
                    f"({smallest.p95_ms:.0f}ms). Suggests network/auth "
                    f"path issue, not capacity exhaustion."
                ),
                "recommended_action": (
                    "Measure raw TCP RTT to DW endpoint; verify Aegis "
                    "bearer; check for HTTP 4xx in ledger error_class "
                    "field."
                ),
                "per_size_summary": summaries,
            }
        return {
            "hypothesis": "a",
            "confidence": 0.65,
            "reasoning": (
                f"All sizes failed with elevated latency (smallest "
                f"p95={smallest.p95_ms:.0f}ms). Suggests DW endpoint "
                f"capacity exhaustion on this account."
            ),
            "recommended_action": (
                "Contact DW for capacity diagnosis; consider Slice 22 "
                "tier-decay with raised JARVIS_CLAUDE_SESSION_CAP_USD "
                "(§48.11) as temporary fallback."
            ),
            "per_size_summary": summaries,
        }

    if size_correlated_failure:
        # Hypothesis (c) — prompt complexity is the variable
        return {
            "hypothesis": "c",
            "confidence": 0.85,
            "reasoning": (
                f"Smallest size ({by_size[0].target_size}B) succeeds "
                f"({by_size[0].success_rate:.0%}); largest "
                f"({by_size[-1].target_size}B) fails "
                f"({by_size[-1].success_rate:.0%}). Endpoint capacity "
                f"is sufficient for small inputs but breaks down at "
                f"scale."
            ),
            "recommended_action": (
                "Refactor O+V prompts to stay below the empirical "
                "ceiling (likely between "
                f"{by_size[0].target_size}B and "
                f"{by_size[-1].target_size}B); §48.9.5 prefix-trie "
                "candidate."
            ),
            "per_size_summary": summaries,
        }

    if high_latency_succeeded:
        # Hypothesis (b) — DW works but slowly; static budget under-sized
        max_p95 = max(r.p95_ms for r in by_size if r.success_rate >= 0.5)
        return {
            "hypothesis": "b",
            "confidence": 0.8,
            "reasoning": (
                f"DW succeeds with elevated latency (max successful "
                f"p95={max_p95:.0f}ms). Slice 28 static budget "
                f"(default 75s heavy-model) may under-budget actual "
                f"response times."
            ),
            "recommended_action": (
                f"Raise JARVIS_ADAPTIVE_TIER0_CAP_S above "
                f"{(max_p95/1000)*1.5:.0f}s OR enable "
                f"JARVIS_DW_ADAPTIVE_TIMEOUT_ENABLED=1 (Slice 34's "
                f"adaptive-timeout substrate)."
            ),
            "per_size_summary": summaries,
        }

    if all_succeeded:
        # No defect detected in probe — issue is harness-side
        return {
            "hypothesis": "harness_variable",
            "confidence": 0.9,
            "reasoning": (
                "All probe sizes succeeded with healthy success rates. "
                "DW endpoint + auth + network are functional. The "
                "v25→v29 100% TIMEOUT rate is a harness-side variable "
                "(orchestrator, sensor load, intake pressure, or "
                "GIL contention raising effective per-call latency)."
            ),
            "recommended_action": (
                "Re-run v30 soak with LoopSink enabled; compare per-call "
                "latency in production ledger vs probe ledger; identify "
                "harness-side dispatch overhead."
            ),
            "per_size_summary": summaries,
        }

    return {
        "hypothesis": "mixed",
        "confidence": 0.4,
        "reasoning": (
            "Probe results don't cleanly match any single hypothesis. "
            "Suggests multi-factor interaction — re-run with more "
            "trials per size for statistical power."
        ),
        "recommended_action": (
            "Increase trials_per_size to 30; inspect per_size_summary "
            "manually."
        ),
        "per_size_summary": summaries,
    }


__all__ = [
    "DWCapacityProbe",
    "ProbeResult",
    "ProbeTrial",
    "build_capacity_probe_from_default_provider",
    "classify_probe_results",
]

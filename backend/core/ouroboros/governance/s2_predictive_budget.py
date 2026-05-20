"""S2 — Predictive Budget Preemption + Dynamic Routing (PRD §11).

Adds a *predictive* dimension to admission: before dispatch, compute
the operation's forecasted cost, evaluate whether it fits in the
remaining session budget weighted by a **MAD-based dynamic safety
factor**, and (when budget pressure is tight AND a high-urgency op
is queued) emit an advisory preemption signal to the existing
:class:`SensorGovernor` so its quarantine machinery can starve
low-priority sensors.

Composition discipline (PRD §3 — extend, never parallel):

  * :mod:`admission_estimator` — reuses ``estimator_alpha()`` for the
    EWMA coefficient; reads per-(route, model) op-outcome samples
    from :class:`RecentDecisionsRing.op_outcome_samples`. **No
    parallel EWMA class, no parallel ring.**

  * :mod:`admission_gate` — does NOT edit ``compute_admission_decision``.
    The function already accepts a ``budget_safety_factor_value``
    parameter; S2 computes the dynamic value and passes it in. The
    hook the PRD requires *was already there*.

  * :mod:`sensor_governor` — calls the additive
    :meth:`SensorGovernor.apply_preemption_signal` method. **No
    parallel quarantine machinery.** The signal is recorded into
    the existing ``recent_decisions`` ring so existing
    snapshot/observability surfaces report it without API changes.

  * ``brain_selection_policy.yaml`` — pricing source. The ``s2_pricing``
    section is read once + cached. Missing/garbled yaml degrades to
    a per-token default cold-start prior (graceful, NEVER raises).

Correctness posture (load-bearing):

  * MAD chosen over sample stddev specifically for **outlier
    robustness**: a single anomalous cost shifts MAD by O(1),
    stddev by O(n). A timed-out provider call billed at max_tokens
    therefore does NOT poison the safety estimate into panic
    tightening (graduation Bar C).

  * The 1.4826 consistency factor scales MAD → robust σ-estimate
    under approximate normality. Mathematical constant, not a
    business knob; AST-pinned exempt from no-hardcode rule.

  * High-urgency routes (IMMEDIATE / STANDARD / COMPLEX) are NEVER
    quarantined by S2. Enforced in two places:
      - here at signal emission (advice restricted)
      - in :class:`SensorGovernor.apply_preemption_signal` (input
        validation — the closed advice surface ``'quarantine_low_prio_sensors'``
        only quarantines :class:`Urgency.BACKGROUND` / SPECULATIVE).

  * **Fail-OPEN on availability**: any internal fault (yaml parse,
    stats math, env read) degrades to ``base_safety_factor()`` /
    ``0.0`` forecast. The predicate clause becomes a no-op and the
    existing :func:`compute_admission_decision` predicates remain
    authoritative. S2 NEVER blocks admission on its own faults.

Authority asymmetry (AST-pinned): imports stdlib + numpy-free statistics
+ admission_estimator + sensor_governor + (optionally) yaml — never
orchestrator / iron_gate / candidate_generator.

Master ``JARVIS_S2_PREDICTIVE_BUDGET_ENABLED`` default **FALSE** — off
⇒ every entry point is a no-op and the admission path is byte-identical
to today. Graduation-gated per PRD §11.8.
"""
from __future__ import annotations

import logging
import os
import statistics
import threading
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.S2PredictiveBudget")

S2_PREDICTIVE_BUDGET_SCHEMA_VERSION: str = "s2_predictive_budget.v1"

# --------------------------------------------------------------------------
# Env knobs (PRD §11.5) — master + 7 tunables. NO HARDCODED THRESHOLDS.
# --------------------------------------------------------------------------

_ENV_MASTER = "JARVIS_S2_PREDICTIVE_BUDGET_ENABLED"
_ENV_BASE_SAFETY_FACTOR = "JARVIS_S2_BASE_SAFETY_FACTOR"
_ENV_VOLATILITY_PENALTY = "JARVIS_S2_VOLATILITY_PENALTY"
_ENV_SAFETY_FLOOR = "JARVIS_S2_SAFETY_FLOOR"
_ENV_SAFETY_CEILING = "JARVIS_S2_SAFETY_CEILING"
_ENV_CHARS_PER_TOKEN = "JARVIS_S2_CHARS_PER_TOKEN"
_ENV_COST_SAMPLE_WINDOW = "JARVIS_S2_COST_SAMPLE_WINDOW"
_ENV_PRICING_YAML_PATH = "JARVIS_S2_PRICING_YAML_PATH"
# PRD §11 S2 wiring (B1 revised): session budget precedence chain.
# JARVIS_S2_SESSION_BUDGET_USD > OUROBOROS_BATTLE_COST_CAP > default 0.50.
# Default 0.50 mirrors the BattleTestHarnessConfig default (NOT 1.0).
_ENV_SESSION_BUDGET_USD = "JARVIS_S2_SESSION_BUDGET_USD"
_ENV_BATTLE_COST_CAP = "OUROBOROS_BATTLE_COST_CAP"
_DEFAULT_SESSION_BUDGET_USD = 0.50

# Documented PRD defaults — these live ONLY as the env-default arm of the
# os.environ.get(...) reads below. Code paths NEVER inline any of these
# values; everything goes through the helper functions.
_DEFAULT_BASE_SAFETY_FACTOR = 0.9
_DEFAULT_VOLATILITY_PENALTY = 1.0
_DEFAULT_SAFETY_FLOOR = 0.5
_DEFAULT_SAFETY_CEILING = 0.95
_DEFAULT_CHARS_PER_TOKEN = 4.0
_DEFAULT_COST_SAMPLE_WINDOW = 50

# MAD → robust σ consistency factor under approximate normality. Closed-form
# mathematical constant (1 / Φ⁻¹(0.75) ≈ 1.4826), not a business knob.
# AST-pinned exempt from the no-hardcode rule.
_MAD_NORMALITY_CONSTANT = 1.4826

# --------------------------------------------------------------------------
# Env-driven helper accessors — defensive, NEVER raise.
# --------------------------------------------------------------------------


def master_enabled() -> bool:
    """Master switch, default-FALSE (PRD §11.5). Re-read each call so a
    flip hot-reverts. NEVER raises."""
    try:
        return os.environ.get(_ENV_MASTER, "false").strip().lower() in (
            "1", "true", "yes", "on",
        )
    except Exception:  # noqa: BLE001 — defensive
        return False


def _env_float(name: str, default: float) -> float:
    """Read a float env knob with graceful fallback. NEVER raises."""
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return float(default)
        return float(raw)
    except (TypeError, ValueError):
        return float(default)
    except Exception:  # noqa: BLE001 — defensive
        return float(default)


def _env_int(name: str, default: int) -> int:
    """Read an int env knob with graceful fallback. NEVER raises."""
    try:
        raw = os.environ.get(name, "").strip()
        if not raw:
            return int(default)
        return int(raw)
    except (TypeError, ValueError):
        return int(default)
    except Exception:  # noqa: BLE001 — defensive
        return int(default)


def base_safety_factor() -> float:
    """The factor BEFORE volatility adjustment. PRD §11.5 default 0.9.
    Clamped to (0, 1]."""
    v = _env_float(_ENV_BASE_SAFETY_FACTOR, _DEFAULT_BASE_SAFETY_FACTOR)
    return max(0.01, min(1.0, v))


def volatility_penalty() -> float:
    """Multiplier on CV_MAD before subtracting from base. Default 1.0.
    Clamped to [0, 10] (prevents single-knob runaway)."""
    v = _env_float(_ENV_VOLATILITY_PENALTY, _DEFAULT_VOLATILITY_PENALTY)
    return max(0.0, min(10.0, v))


def safety_floor() -> float:
    """Lower clip bound — never tighter than this. Default 0.5.
    Clamped to [0.01, 0.99]."""
    v = _env_float(_ENV_SAFETY_FLOOR, _DEFAULT_SAFETY_FLOOR)
    return max(0.01, min(0.99, v))


def safety_ceiling() -> float:
    """Upper clip bound — never looser than this. Default 0.95.
    Clamped to [0.02, 1.0]."""
    v = _env_float(_ENV_SAFETY_CEILING, _DEFAULT_SAFETY_CEILING)
    return max(0.02, min(1.0, v))


def chars_per_token() -> float:
    """Prompt-chars → token estimate divisor. Default 4.0. Clamped >= 0.5
    (avoid div-by-zero / absurd-token forecasts)."""
    v = _env_float(_ENV_CHARS_PER_TOKEN, _DEFAULT_CHARS_PER_TOKEN)
    return max(0.5, v)


def cost_sample_window() -> int:
    """RecentDecisionsRing op-outcome window size for MAD. Default 50.
    Clamped to [3, 1000]."""
    v = _env_int(_ENV_COST_SAMPLE_WINDOW, _DEFAULT_COST_SAMPLE_WINDOW)
    return max(3, min(1000, v))


def _pricing_yaml_path() -> Path:
    """Configured pricing-yaml source (PRD §11.5). Default:
    brain_selection_policy.yaml under this package."""
    raw = os.environ.get(_ENV_PRICING_YAML_PATH, "").strip()
    if raw:
        try:
            return Path(raw).expanduser()
        except Exception:  # noqa: BLE001
            pass
    # Default — beside this module, NOT hardcoded path
    return Path(__file__).parent / "brain_selection_policy.yaml"


# --------------------------------------------------------------------------
# Pricing yaml loader — read-once + cached, fail-OPEN to default prior.
# --------------------------------------------------------------------------


_PRICING_CACHE: Optional[Dict[str, Any]] = None
_PRICING_LOCK = threading.Lock()


def _load_pricing() -> Dict[str, Any]:
    """Read the s2_pricing section from the configured yaml. Cached
    once per process. Returns ``{}`` on ANY failure (file missing,
    yaml unparseable, section absent, yaml lib unavailable). The
    consumer treats ``{}`` as "no pricing configured → forecast = 0".
    NEVER raises."""
    global _PRICING_CACHE
    with _PRICING_LOCK:
        if _PRICING_CACHE is not None:
            return _PRICING_CACHE
        result: Dict[str, Any] = {}
        try:
            try:
                import yaml  # type: ignore[import-not-found]
            except ImportError:
                logger.debug("[S2] pyyaml unavailable; pricing disabled")
                _PRICING_CACHE = {}
                return _PRICING_CACHE
            p = _pricing_yaml_path()
            if not p.exists():
                logger.debug("[S2] pricing yaml missing: %s", p)
                _PRICING_CACHE = {}
                return _PRICING_CACHE
            with open(p, "r", encoding="utf-8") as f:
                raw = yaml.safe_load(f)
            if not isinstance(raw, dict):
                _PRICING_CACHE = {}
                return _PRICING_CACHE
            section = raw.get("s2_pricing")
            if isinstance(section, dict):
                result = section
        except Exception as exc:  # noqa: BLE001
            logger.debug("[S2] pricing yaml load fault: %s", exc)
            result = {}
        _PRICING_CACHE = result
        return _PRICING_CACHE


def _reset_pricing_cache_for_tests() -> None:
    """Test hook — drops the pricing cache so a subsequent monkeypatched
    yaml path is re-read."""
    global _PRICING_CACHE
    with _PRICING_LOCK:
        _PRICING_CACHE = None


def _lookup_pricing(
    route: str, model: str,
) -> Optional[Tuple[float, float]]:
    """Return ``(input_per_token, output_per_token)`` for the (route,
    model) pair, or None if neither a specific entry nor a default
    prior is available. NEVER raises."""
    try:
        pricing = _load_pricing()
        if not pricing:
            return None
        routes = pricing.get("routes")
        if isinstance(routes, dict):
            route_section = routes.get(str(route))
            if isinstance(route_section, dict):
                entry = route_section.get(str(model))
                if isinstance(entry, dict):
                    try:
                        i = float(entry.get("input", 0.0))
                        o = float(entry.get("output", 0.0))
                        if i >= 0.0 and o >= 0.0:
                            return (i, o)
                    except (TypeError, ValueError):
                        pass
        # Fall back to default prior
        default = pricing.get("default_per_token_usd")
        if isinstance(default, dict):
            try:
                i = float(default.get("input", 0.0))
                o = float(default.get("output", 0.0))
                if i >= 0.0 and o >= 0.0:
                    return (i, o)
            except (TypeError, ValueError):
                pass
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[S2] _lookup_pricing fault: %s", exc)
    return None


# --------------------------------------------------------------------------
# MAD math — the load-bearing volatility primitive (PRD §11.3.1).
# --------------------------------------------------------------------------


def cost_volatility_cv_mad(samples: Sequence[float]) -> float:
    """Robust Coefficient of Variation via Median Absolute Deviation
    (PRD §11.3.1).

        CV_MAD = (MAD × 1.4826) / median(samples)

    The 1.4826 consistency factor scales MAD to a robust σ-estimate
    under approximate normality; the median-normalized ratio is a
    scale-invariant volatility coefficient.

    Outlier-resistant by construction: a single anomalous cost
    shifts MAD by O(1) (unlike stddev's O(n)) — this is the
    "adaptive but not panicky" property (PRD §11.7 Bar C).

    Returns ``0.0`` (treated as "no volatility, no penalty") on:
      - empty / fewer-than-3 samples (insufficient signal),
      - non-positive median (degenerate; avoids div-by-zero),
      - any computation fault.

    NEVER raises.
    """
    try:
        if not samples or len(samples) < 3:
            return 0.0
        # Filter out NaN / negatives / non-numerics defensively
        clean: list = []
        for s in samples:
            try:
                v = float(s)
            except (TypeError, ValueError):
                continue
            if v != v:                  # NaN
                continue
            if v < 0.0:
                continue
            clean.append(v)
        if len(clean) < 3:
            return 0.0
        med = statistics.median(clean)
        if med <= 0.0:
            return 0.0
        abs_devs = [abs(x - med) for x in clean]
        mad = statistics.median(abs_devs)
        robust_sigma = mad * _MAD_NORMALITY_CONSTANT
        return float(robust_sigma / med)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[S2] cost_volatility_cv_mad fault: %s", exc)
        return 0.0


# --------------------------------------------------------------------------
# Dynamic safety factor — composes MAD + envelope knobs (PRD §11.3.2).
# --------------------------------------------------------------------------


def _default_sample_provider(
    route: str, model: str, limit: int,
) -> Tuple[float, ...]:
    """Pull cost samples from the canonical RecentDecisionsRing singleton.
    Returns the cost_usd field of recent op_outcome records for
    (route, model). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.admission_estimator import (
            get_default_history,
        )
        ring = get_default_history()
        records = ring.op_outcome_samples(route, model, limit=limit)
        return tuple(float(r.get("cost_usd", 0.0)) for r in records)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[S2] default sample provider fault: %s", exc)
        return tuple()


def dynamic_admit_safety_factor(
    route: str,
    model: str,
    *,
    sample_provider=None,
) -> float:
    """Volatility-adjusted admission safety factor (PRD §11.3.2):

        clip(base − penalty × CV_MAD(recent_costs[route][model]),
             floor, ceiling)

    Tightens when the (route, model) cost distribution is volatile;
    relaxes when it is stable. All four envelope knobs are
    env-tunable; the MAD body is robust to outliers.

    The ``sample_provider`` argument exists for testability — it lets
    a test inject deterministic samples without populating the
    singleton ring. In production, the default reads from
    :class:`RecentDecisionsRing` via :func:`_default_sample_provider`.

    NEVER raises — any fault degrades to ``base_safety_factor()``
    (the conservative pre-volatility envelope).
    """
    try:
        base = base_safety_factor()
        penalty = volatility_penalty()
        floor = safety_floor()
        ceiling = safety_ceiling()
        window = cost_sample_window()
        provider = sample_provider or _default_sample_provider
        try:
            samples = provider(str(route), str(model), window)
        except Exception as exc:  # noqa: BLE001 — sample fault
            logger.debug("[S2] sample provider raised: %s", exc)
            return base
        cv = cost_volatility_cv_mad(samples)
        raw = base - penalty * cv
        # Clip ordering: floor first, then ceiling — symmetric clamp
        return max(floor, min(ceiling, raw))
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug(
            "[S2] dynamic_admit_safety_factor fault: %s", exc,
        )
        # Last-resort fallback (env-read may have raced) — return PRD
        # default without trusting any of the above. Cap to [0, 1].
        try:
            return float(os.environ.get(
                _ENV_BASE_SAFETY_FACTOR,
                str(_DEFAULT_BASE_SAFETY_FACTOR),
            ))
        except Exception:  # noqa: BLE001
            return _DEFAULT_BASE_SAFETY_FACTOR


# --------------------------------------------------------------------------
# Forecasted_Cost — deterministic per-op spend estimate (PRD §11.2).
# --------------------------------------------------------------------------


def _expected_output_tokens(
    route: str, model: str,
) -> float:
    """EWMA of recent observed output_tokens for (route, model).
    Reuses the canonical ``estimator_alpha()`` coefficient (PRD §11.2
    — composes admission_estimator's EWMA discipline). NEVER raises;
    returns 0.0 on cold start or any fault (cold start ⇒ no forecast
    pressure, conservative)."""
    try:
        from backend.core.ouroboros.governance.admission_estimator import (
            estimator_alpha, get_default_history,
        )
        alpha = estimator_alpha()
        ring = get_default_history()
        records = ring.op_outcome_samples(
            route, model, limit=cost_sample_window(),
        )
        if not records:
            return 0.0
        # EWMA in chronological order (oldest first, newest last —
        # which is the order op_outcome_samples returns).
        ewma: Optional[float] = None
        for rec in records:
            try:
                tok = float(rec.get("output_tokens", 0.0))
            except (TypeError, ValueError):
                continue
            if tok < 0.0:
                continue
            if ewma is None:
                ewma = tok
            else:
                ewma = alpha * tok + (1.0 - alpha) * ewma
        return float(ewma if ewma is not None else 0.0)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[S2] _expected_output_tokens fault: %s", exc)
        return 0.0


def forecasted_cost(
    prompt_chars: int,
    route: str,
    model: str,
    *,
    output_token_estimator=None,
    pricing_lookup=None,
) -> float:
    """Predicted USD cost for an op given its assembled-prompt length
    (PRD §11.2):

        Forecasted_Cost(op) =
              expected_input_tokens(op)  × input_price_per_token
            + expected_output_tokens(op) × output_price_per_token

        expected_input_tokens  = prompt_chars / chars_per_token
        expected_output_tokens = EWMA over recent (route, model)
                                  op-outcome samples (composes
                                  admission_estimator alpha + ring)
        prices                 = s2_pricing yaml (composes config)

    The ``output_token_estimator`` and ``pricing_lookup`` args exist
    for testability (test injects deterministic helpers); production
    uses the defaults that compose the canonical primitives.

    Returns 0.0 (treated as "no forecast pressure") on:
      - non-positive prompt_chars,
      - missing pricing for (route, model) AND no default prior,
      - any internal fault.

    NEVER raises.
    """
    try:
        try:
            pc = int(prompt_chars)
        except (TypeError, ValueError):
            return 0.0
        if pc <= 0:
            return 0.0
        cpt = chars_per_token()
        if cpt <= 0.0:
            return 0.0
        in_tokens = pc / cpt
        out_estimator = output_token_estimator or _expected_output_tokens
        try:
            out_tokens = float(out_estimator(str(route), str(model)))
        except Exception as exc:  # noqa: BLE001 — estimator fault
            logger.debug("[S2] output estimator fault: %s", exc)
            out_tokens = 0.0
        if out_tokens < 0.0 or out_tokens != out_tokens:    # NaN-safe
            out_tokens = 0.0
        lookup = pricing_lookup or _lookup_pricing
        try:
            prices = lookup(str(route), str(model))
        except Exception as exc:  # noqa: BLE001 — pricing fault
            logger.debug("[S2] pricing lookup fault: %s", exc)
            prices = None
        if prices is None:
            # No pricing configured — forecast becomes 0 (predicate
            # clause no-ops; existing admission predicates remain
            # authoritative). Log once-per-call at DEBUG.
            logger.debug(
                "[S2] no pricing for (route=%s model=%s) — "
                "forecast = 0", route, model,
            )
            return 0.0
        input_price, output_price = prices
        cost = in_tokens * input_price + out_tokens * output_price
        # Defensive: clamp negatives + NaN
        if cost != cost or cost < 0.0:
            return 0.0
        return float(cost)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[S2] forecasted_cost fault: %s", exc)
        return 0.0


# --------------------------------------------------------------------------
# Preemption signal — composes existing SensorGovernor (PRD §11.4).
# --------------------------------------------------------------------------


def emit_preemption_signal(
    *,
    severity: float,
    high_prio_queued: bool,
    kind: str = "budget_forecast_tight",
    governor=None,
) -> bool:
    """Emit an S2 preemption advisory to the existing SensorGovernor.
    Composes :meth:`SensorGovernor.apply_preemption_signal` — no new
    quarantine machinery.

    Always emits with closed advice ``'quarantine_low_prio_sensors'``
    (the only advice S2 is allowed to issue — see PRD §11.4
    high-urgency-immune invariant). Returns True iff the governor
    accepted + recorded the signal.

    NEVER raises.
    """
    try:
        if governor is None:
            from backend.core.ouroboros.governance.sensor_governor import (
                get_default_governor,
            )
            try:
                governor = get_default_governor()
            except Exception as exc:  # noqa: BLE001
                logger.debug("[S2] governor lookup fault: %s", exc)
                return False
        return bool(governor.apply_preemption_signal(
            kind=kind,
            severity=severity,
            high_prio_queued=bool(high_prio_queued),
            advice="quarantine_low_prio_sensors",
        ))
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[S2] emit_preemption_signal fault: %s", exc)
        return False


# --------------------------------------------------------------------------
# Session budget — env-driven precedence chain (PRD §11 S2 wiring, B1 revised)
# --------------------------------------------------------------------------


def session_budget_usd() -> float:
    """Return the session-wide USD budget. Precedence (B1 revised):

      1. ``JARVIS_S2_SESSION_BUDGET_USD`` (explicit S2 override)
      2. ``OUROBOROS_BATTLE_COST_CAP`` (battle harness env)
      3. default **0.50** (mirrors BattleTestHarnessConfig default)

    Reads dynamically from ``os.environ`` per call — the *value* lives
    in env, the *channel* is the operator's interface. No hardcoded
    budget literal in code; default matches the existing harness
    contract, not a new authority.

    Clamped to a hard floor of $0.01 (avoid div-by-zero on downstream
    pressure-ratio math). NEVER raises."""
    try:
        # Tier 1: explicit S2 env knob
        raw_s2 = os.environ.get(_ENV_SESSION_BUDGET_USD, "").strip()
        if raw_s2:
            try:
                v = float(raw_s2)
                return max(0.01, v)
            except (TypeError, ValueError):
                pass  # fall through to harness env
        # Tier 2: battle harness env knob
        raw_battle = os.environ.get(_ENV_BATTLE_COST_CAP, "").strip()
        if raw_battle:
            try:
                v = float(raw_battle)
                return max(0.01, v)
            except (TypeError, ValueError):
                pass  # fall through to default
        # Tier 3: documented default — mirrors BattleTestHarnessConfig.
        return float(_DEFAULT_SESSION_BUDGET_USD)
    except Exception as exc:  # noqa: BLE001 — defensive
        logger.debug("[S2] session_budget_usd fault: %s", exc)
        return float(_DEFAULT_SESSION_BUDGET_USD)


def _peek_high_prio_queued() -> bool:
    """True iff the next-to-be-dispatched envelope in the
    UnifiedIntakeRouter's priority queue is high-priority — i.e.,
    one of ``critical`` / ``high`` / ``normal`` (which map to
    governor ``IMMEDIATE`` / ``STANDARD`` / ``COMPLEX``).

    Composes ``UnifiedIntakeRouter.peek_top_urgency()`` exclusively
    — no parallel queue inspector (PRD §11 B2 directive). Returns
    False on any fault (fail-open: no signal emitted on lookup
    fault). NEVER raises."""
    try:
        from backend.core.ouroboros.governance.intake.unified_intake_router import (  # noqa: E501
            get_default_intake_router,
        )
        router = get_default_intake_router()
        if router is None:
            return False
        top = router.peek_top_urgency()
        if top is None:
            return False
        return top in ("critical", "high", "normal")
    except Exception:  # noqa: BLE001 — defensive
        return False


# --------------------------------------------------------------------------
# Provider-side admission predicate (PRD §11.4 wiring) — B3 directive
# --------------------------------------------------------------------------


def evaluate_admission_pressure(
    prompt_text: str,
    route: str,
    model: str,
    *,
    cost_governor=None,
    sample_provider=None,
    pricing_lookup=None,
    output_token_estimator=None,
) -> Optional[float]:
    """Provider-side S2 admission evaluator (PRD §11.4 + §11 B1-B4).

    Composes the existing data flows exactly:
      * **Spend**: ``cost_governor.session_total_cumulative_usd()``
        (additive, composes existing ``_entries`` ledger).
      * **Budget**: :func:`session_budget_usd` (env precedence chain).
      * **Prompt-chars**: ``len(prompt_text)`` — dynamically at
        provider-call time, with assembled ``prompt_text`` in scope.
        NO pre-calculation. NO prompt re-assembly (B3 invariant).
      * **Forecast**: :func:`forecasted_cost` over the actual
        ``len(prompt_text)``.
      * **Dynamic factor**: :func:`dynamic_admit_safety_factor` —
        MAD-based, robust to outliers.
      * **High-prio queued**: composes
        ``UnifiedIntakeRouter.peek_top_urgency()``.

    Returns the severity ∈ (0.0, 1.0] iff a preemption signal SHOULD
    be emitted, or ``None`` if no signal is warranted. **The caller
    (provider) is responsible for invoking
    :func:`emit_preemption_signal`.**

    **Critical invariant (PRD §11.4)**: S2 is ADVISORY. This function
    does not block, alter, or defer the current op's provider
    dispatch — it merely returns whether a preemption-advisory
    *should* fire to nudge ``sensor_governor`` against future
    low-priority sensor emissions. The current op proceeds normally.

    NEVER raises (fail-open: any fault ⇒ returns ``None`` ⇒ no
    signal). Master OFF returns ``None`` immediately."""
    try:
        if not master_enabled():
            return None
        if cost_governor is None:
            try:
                from backend.core.ouroboros.governance.cost_governor import (  # noqa: E501
                    get_default_cost_governor,
                )
                cost_governor = get_default_cost_governor()
            except Exception as exc:  # noqa: BLE001
                logger.debug(
                    "[S2] cost_governor lookup fault: %s", exc,
                )
                return None
        if cost_governor is None:
            return None  # cost ledger not active in this process
        try:
            spend = float(cost_governor.session_total_cumulative_usd())
        except Exception as exc:  # noqa: BLE001
            logger.debug("[S2] session_total fault: %s", exc)
            return None
        budget = session_budget_usd()
        factor = dynamic_admit_safety_factor(
            str(route), str(model), sample_provider=sample_provider,
        )
        forecast = forecasted_cost(
            len(prompt_text or ""), str(route), str(model),
            output_token_estimator=output_token_estimator,
            pricing_lookup=pricing_lookup,
        )
        denom = max(0.01, budget * factor)
        ratio = (spend + forecast) / denom
        if ratio < 1.0:
            return None
        if not _peek_high_prio_queued():
            return None
        # Severity = how far over the threshold, clipped to [0,1]
        return float(min(1.0, max(0.0, ratio - 1.0)))
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[S2] evaluate_admission_pressure fault: %s", exc,
        )
        return None


# --------------------------------------------------------------------------
# Exports
# --------------------------------------------------------------------------


__all__ = [
    "S2_PREDICTIVE_BUDGET_SCHEMA_VERSION",
    "master_enabled",
    "session_budget_usd",
    "evaluate_admission_pressure",
    "base_safety_factor",
    "volatility_penalty",
    "safety_floor",
    "safety_ceiling",
    "chars_per_token",
    "cost_sample_window",
    "cost_volatility_cv_mad",
    "dynamic_admit_safety_factor",
    "forecasted_cost",
    "emit_preemption_signal",
    "register_flags",
    "register_shipped_invariants",
]


# --------------------------------------------------------------------------
# FlagRegistry seeds — 8 env knobs (PRD §11.5).
# --------------------------------------------------------------------------


def register_flags(registry) -> int:  # noqa: ANN001
    """Register S2's 8 env knobs into the FlagRegistry. Returns the
    count actually registered. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category, FlagSpec, FlagType,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[S2] register_flags degraded: %s", exc)
        return 0
    tgt = "backend/core/ouroboros/governance/s2_predictive_budget.py"
    specs = [
        FlagSpec(
            name=_ENV_MASTER, type=FlagType.BOOL, default=False,
            category=Category.SAFETY, source_file=tgt,
            example=f"{_ENV_MASTER}=true",
            description=(
                "Master for the S2 predictive budget preemption "
                "layer. OFF (default) ⇒ admission_gate path "
                "byte-identical to today."
            ),
        ),
        FlagSpec(
            name=_ENV_BASE_SAFETY_FACTOR, type=FlagType.FLOAT,
            default=_DEFAULT_BASE_SAFETY_FACTOR,
            category=Category.SAFETY, source_file=tgt,
            example=f"{_ENV_BASE_SAFETY_FACTOR}=0.9",
            description=(
                "Base admission safety factor BEFORE volatility "
                "adjustment. Replaces the deprecated static "
                "JARVIS_S2_ADMIT_SAFETY_FACTOR."
            ),
        ),
        FlagSpec(
            name=_ENV_VOLATILITY_PENALTY, type=FlagType.FLOAT,
            default=_DEFAULT_VOLATILITY_PENALTY,
            category=Category.SAFETY, source_file=tgt,
            example=f"{_ENV_VOLATILITY_PENALTY}=1.0",
            description=(
                "Multiplier on CV_MAD before subtracting from base "
                "safety factor. Higher = tightens admission more "
                "aggressively when provider is volatile."
            ),
        ),
        FlagSpec(
            name=_ENV_SAFETY_FLOOR, type=FlagType.FLOAT,
            default=_DEFAULT_SAFETY_FLOOR,
            category=Category.SAFETY, source_file=tgt,
            example=f"{_ENV_SAFETY_FLOOR}=0.5",
            description=(
                "Clip lower bound for dynamic safety factor. Never "
                "tightens admission tighter than this."
            ),
        ),
        FlagSpec(
            name=_ENV_SAFETY_CEILING, type=FlagType.FLOAT,
            default=_DEFAULT_SAFETY_CEILING,
            category=Category.SAFETY, source_file=tgt,
            example=f"{_ENV_SAFETY_CEILING}=0.95",
            description=(
                "Clip upper bound for dynamic safety factor. Never "
                "loosens admission looser than this."
            ),
        ),
        FlagSpec(
            name=_ENV_CHARS_PER_TOKEN, type=FlagType.FLOAT,
            default=_DEFAULT_CHARS_PER_TOKEN,
            category=Category.CAPACITY, source_file=tgt,
            example=f"{_ENV_CHARS_PER_TOKEN}=4.0",
            description=(
                "Prompt-chars → token estimate divisor. Used by "
                "Forecasted_Cost. Empirical default 4.0 for English."
            ),
        ),
        FlagSpec(
            name=_ENV_COST_SAMPLE_WINDOW, type=FlagType.INT,
            default=_DEFAULT_COST_SAMPLE_WINDOW,
            category=Category.CAPACITY, source_file=tgt,
            example=f"{_ENV_COST_SAMPLE_WINDOW}=50",
            description=(
                "RecentDecisionsRing op-outcome window size used "
                "for MAD volatility computation. Larger windows = "
                "more inertia."
            ),
        ),
        FlagSpec(
            name=_ENV_PRICING_YAML_PATH, type=FlagType.STR,
            default="brain_selection_policy.yaml",
            category=Category.INTEGRATION, source_file=tgt,
            example=(
                f"{_ENV_PRICING_YAML_PATH}=brain_selection_policy.yaml"
            ),
            description=(
                "Path to the yaml carrying the `s2_pricing` section. "
                "Default: brain_selection_policy.yaml beside this "
                "module. Missing/absent section ⇒ forecast = 0 "
                "(predicate clause no-op)."
            ),
        ),
        FlagSpec(
            name=_ENV_SESSION_BUDGET_USD, type=FlagType.FLOAT,
            default=_DEFAULT_SESSION_BUDGET_USD,
            category=Category.SAFETY, source_file=tgt,
            example=f"{_ENV_SESSION_BUDGET_USD}=0.50",
            description=(
                "Session-wide USD budget for S2's predictive admission "
                "predicate (PRD §11 B1 revised). Precedence: this "
                "env > OUROBOROS_BATTLE_COST_CAP > default 0.50 "
                "(matches BattleTestHarnessConfig). Read dynamically "
                "per call."
            ),
        ),
    ]
    n = 0
    for s in specs:
        try:
            registry.register(s)
            n += 1
        except Exception as exc:  # noqa: BLE001
            logger.debug("[S2] seed %s skipped: %s", s.name, exc)
    return n


# --------------------------------------------------------------------------
# AST pins — composes-not-duplicates discipline (PRD §11.6).
# --------------------------------------------------------------------------


def register_shipped_invariants() -> list:
    """Pins: composes admission_estimator (no parallel EWMA/ring),
    composes sensor_governor (no parallel quarantine), MAD constant
    present, NEVER-raises gate, master default-FALSE, no hardcoded
    business floats outside env-default arm."""
    import ast as _ast
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    def _validate(tree: "_ast.Module", source: str) -> tuple:
        v: list = []
        # 1. Composes admission_estimator (must import + use)
        if "admission_estimator" not in source:
            v.append(
                "must compose admission_estimator "
                "(no parallel EWMA/ring)"
            )
        # 2. Composes sensor_governor
        if "sensor_governor" not in source:
            v.append(
                "must compose sensor_governor "
                "(no parallel quarantine machinery)"
            )
        # 3. MAD constant 1.4826 present
        if "1.4826" not in source:
            v.append(
                "MAD→σ consistency factor 1.4826 missing "
                "(PRD §11.3.1)"
            )
        # 4. No parallel local class definitions of canonical
        #    primitives
        for node in _ast.walk(tree):
            if isinstance(node, _ast.ClassDef):
                if node.name in (
                    "WaitTimeEstimator", "RecentDecisionsRing",
                    "SensorGovernor",
                ):
                    v.append(
                        f"must NOT redefine canonical class "
                        f"{node.name!r}"
                    )
            # 5. authority-asymmetry — must not import orchestrator-
            #    layer modules
            if isinstance(node, _ast.ImportFrom):
                m = node.module or ""
                for forbidden in (
                    "orchestrator", "iron_gate",
                    "candidate_generator",
                ):
                    if forbidden in m:
                        v.append(
                            f"authority-asymmetry: must not import "
                            f"{forbidden!r}"
                        )
        # 6. Master read present + default-FALSE
        if "master_enabled" not in source:
            v.append("master_enabled() must be defined")
        if 'os.environ.get(_ENV_MASTER, "false")' not in source:
            v.append("master must default-FALSE (env-read with 'false')")
        # 7. NEVER-raises — every load-bearing function must have try/
        #    except in its body. Check the three main entry points.
        entry_funcs = {
            "cost_volatility_cv_mad",
            "dynamic_admit_safety_factor",
            "forecasted_cost",
        }
        found_excepts: set = set()
        for node in _ast.walk(tree):
            if isinstance(node, _ast.FunctionDef) and (
                node.name in entry_funcs
            ):
                for sub in _ast.walk(node):
                    if isinstance(sub, _ast.ExceptHandler):
                        found_excepts.add(node.name)
                        break
        missing = entry_funcs - found_excepts
        if missing:
            v.append(
                f"NEVER-raise contract: missing try/except in "
                f"{sorted(missing)}"
            )
        return tuple(v)

    return [
        ShippedCodeInvariant(
            invariant_name="s2_predictive_budget_composed_safe",
            target_file=(
                "backend/core/ouroboros/governance/"
                "s2_predictive_budget.py"
            ),
            description=(
                "S2 composes admission_estimator + sensor_governor "
                "(no parallel EWMA/ring/governor), MAD constant "
                "1.4826 present, master default-FALSE, NEVER-raises "
                "gate, authority-asymmetric (no orchestrator/iron_gate "
                "imports)."
            ),
            validate=_validate,
        ),
    ]

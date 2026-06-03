"""Payload-adaptive GENERATE budget — Stage 2 Slice 2 (PRD §40.7.10).

Root cause closed: the route-derived ``_gen_timeout`` (orchestrator
~3975-4026) was a *rigid* allocation calibrated against a trivial
1-file fixture. A real ScaleAI benchmark problem (e.g. the 2028-line
multi-file ``ansible`` known-hard) needs many Venom tool rounds over
a large repo; GENERATE ran 356s against a 213s-derived budget and
was killed ``C_external_cancel`` by the outer Iron-Gate ``wait_for``
before it could reach a terminal — so it could never be scored
(a false rubric pass risk).

This module computes a deterministic :class:`PayloadWeight` from
geometry **already on the op context** (zero new I/O, no re-fetch)
and scales the route-base ``_gen_timeout`` by a bounded multiplier.
It is injected at the single highest-enforcement seam (where the
binding deadline is *born*) so the deadline, the outer Iron-Gate
``wait_for``, and the downstream tool-loop ``BudgetPlan`` all inherit
the scaled value coherently — no per-layer workaround.

Invariants (spine-pinned)
-------------------------

1. **Floor = route base.** The scaled budget is NEVER below the
   original ``base_s``. Worst case (weight 0 / flag off / any error)
   is byte-identical to today → zero regression on the Bar-A
   baseline.
2. **Ceiling = thermodynamic wall cap.** The scaled budget never
   exceeds the session ``--max-wall-seconds`` cap
   (``OUROBOROS_BATTLE_MAX_WALL_SECONDS``); D2 containment is
   preserved — a single op can request headroom but never escape
   the global envelope.
3. **Monotonic.** Heavier payload ⇒ a multiplier that is
   non-decreasing in weight (more geometry never *shrinks* budget).
4. **No hardcoded seconds.** Every coefficient + the ceiling are
   env-tunable + FlagRegistry-seeded; defaults are documented
   calibration, the same pattern as the route ``_route_timeouts``
   themselves — not magic numbers.
5. **Master flag default-FALSE (§33.1).** Byte-identical until
   graduated.

§7 fail-closed: every surface NEVER raises into the orchestrator
(``asyncio.CancelledError`` is not caught here — there is no await).
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

ADAPTIVE_GEN_BUDGET_ENABLED_ENV_VAR: str = (
    "JARVIS_ADAPTIVE_GEN_BUDGET_ENABLED"
)
# Tunable coefficients (documented calibration, NOT magic — same
# discipline as JARVIS_GEN_TIMEOUT_*_S route windows).
_CHARS_PER_TOKEN_ENV_VAR: str = "JARVIS_ADAPTIVE_GEN_CHARS_PER_TOKEN"
_TOKEN_REF_ENV_VAR: str = "JARVIS_ADAPTIVE_GEN_TOKEN_REF"
_FILE_REF_ENV_VAR: str = "JARVIS_ADAPTIVE_GEN_FILE_REF"
_MAX_MULTIPLIER_ENV_VAR: str = "JARVIS_ADAPTIVE_GEN_MAX_MULTIPLIER"
_WALL_CAP_ENV_VAR: str = "OUROBOROS_BATTLE_MAX_WALL_SECONDS"

# Defaults — calibration, env-overridable.
_DEFAULT_CHARS_PER_TOKEN: float = 4.0   # standard ~4 chars/token heuristic
_DEFAULT_TOKEN_REF: float = 4000.0      # ~a heavy problem-statement payload
_DEFAULT_FILE_REF: float = 12.0         # multi-file change reference
_DEFAULT_MAX_MULTIPLIER: float = 6.0    # weight saturates here
# Fraction of the session wall cap a single op may claim (so two
# discriminator ops can't both demand the entire wall). Env-tunable.
_FRACTION_OF_WALL_ENV_VAR: str = "JARVIS_ADAPTIVE_GEN_WALL_FRACTION"
_DEFAULT_FRACTION_OF_WALL: float = 0.5


# ===========================================================================
# Env helpers (NEVER raise — fail-soft to documented default)
# ===========================================================================


def _env_float(name: str, default: float, *, minimum: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return v if v >= minimum else minimum


def adaptive_gen_budget_enabled() -> bool:
    """Master switch — GRADUATED to default-TRUE (Slice 79). The Floor
    invariant guarantees zero regression (trivial payload → multiplier 1.0 →
    byte-identical), so only heavy multi-file payloads (e.g. the ansible /
    NodeBB SWE-bench instances) gain runway. Set the env to a falsey value to
    restore the pre-graduation byte-identical path. NEVER raises."""
    raw = os.environ.get(
        ADAPTIVE_GEN_BUDGET_ENABLED_ENV_VAR, "true",
    ).strip().lower()
    return raw not in ("0", "false", "no", "off")


# ===========================================================================
# Payload weight
# ===========================================================================


@dataclass(frozen=True)
class PayloadWeight:
    """Deterministic geometry of one op's GENERATE payload, derived
    purely from signals already on the op context (no I/O)."""

    text_tokens_est: int
    file_count: int
    projected_rounds: int
    score: float  # normalized blend, >= 0.0 (0.0 == trivial payload)

    def to_dict(self) -> dict:
        return {
            "text_tokens_est": self.text_tokens_est,
            "file_count": self.file_count,
            "projected_rounds": self.projected_rounds,
            "score": round(self.score, 4),
        }


def _ctx_text_len(ctx: Any) -> int:
    """Total payload text length from ctx — defensive/duck-typed.
    ``description`` (the human-readable op text incl. the problem
    statement for swe_bench_pro) + ``intake_evidence_json`` (the
    envelope evidence carried verbatim). NEVER raises."""
    total = 0
    for attr in ("description", "intake_evidence_json"):
        try:
            val = getattr(ctx, attr, "") or ""
            if isinstance(val, str):
                total += len(val)
        except Exception:  # noqa: BLE001 — defensive
            continue
    return total


def _ctx_file_count(ctx: Any) -> int:
    try:
        tf = getattr(ctx, "target_files", ()) or ()
        return len(tuple(tf))
    except Exception:  # noqa: BLE001 — defensive
        return 0


def compute_payload_weight(ctx: Any) -> PayloadWeight:
    """Derive the deterministic payload weight from ctx geometry.

    score = (tokens / TOKEN_REF) + (files / FILE_REF), clamped >= 0.
    A trivial 1-file fixture → score ≈ 0 → multiplier ≈ 1.0 (no
    change even when the flag is ON). A heavy multi-file real repo →
    score >> 0 → larger multiplier (bounded). NEVER raises."""
    chars_per_token = _env_float(
        _CHARS_PER_TOKEN_ENV_VAR, _DEFAULT_CHARS_PER_TOKEN,
        minimum=1.0,
    )
    token_ref = _env_float(
        _TOKEN_REF_ENV_VAR, _DEFAULT_TOKEN_REF, minimum=1.0,
    )
    file_ref = _env_float(
        _FILE_REF_ENV_VAR, _DEFAULT_FILE_REF, minimum=1.0,
    )

    text_len = _ctx_text_len(ctx)
    file_count = _ctx_file_count(ctx)
    tokens_est = int(text_len / chars_per_token)

    # Projected Venom rounds: monotone in file_count (more files →
    # more read/edit rounds). Derived, not a separate input.
    projected_rounds = max(1, file_count)

    score = (tokens_est / token_ref) + (file_count / file_ref)
    if score < 0.0:
        score = 0.0
    return PayloadWeight(
        text_tokens_est=tokens_est,
        file_count=file_count,
        projected_rounds=projected_rounds,
        score=score,
    )


# ===========================================================================
# The scaling function — the only public budget seam
# ===========================================================================


def _wall_ceiling_s(base_s: float) -> float:
    """Resolve the thermodynamic ceiling: a fraction of the session
    ``--max-wall-seconds`` cap. When the cap is unset/0 (legacy
    3-way-race soaks) there is no enforced wall — fall back to a
    generous multiple of base_s so the function still clamps (never
    literally unbounded) without inventing a fixed second value."""
    wall_cap = _env_float(_WALL_CAP_ENV_VAR, 0.0, minimum=0.0)
    fraction = _env_float(
        _FRACTION_OF_WALL_ENV_VAR, _DEFAULT_FRACTION_OF_WALL,
        minimum=0.01,
    )
    if wall_cap > 0.0:
        return max(base_s, wall_cap * fraction)
    # No session wall cap configured → ceiling is a bounded multiple
    # of the route base (max_multiplier), NOT a hardcoded second
    # value — keeps the clamp well-defined under legacy soaks.
    max_mult = _env_float(
        _MAX_MULTIPLIER_ENV_VAR, _DEFAULT_MAX_MULTIPLIER, minimum=1.0,
    )
    return base_s * max_mult


def scale_gen_timeout(base_s: float, ctx: Any) -> float:
    """Scale the route-base generation timeout by payload weight.

    Returns ``base_s`` unchanged when the master flag is OFF, on any
    error, or when the payload is trivial — guaranteeing the Floor
    invariant (zero regression). The result is clamped to
    ``[base_s, wall_ceiling]`` (Floor + Ceiling invariants) and is a
    non-decreasing function of payload weight (Monotonic invariant).

    NEVER raises (the orchestrator seam wraps this in try/except too;
    this is the inner belt to its suspenders)."""
    try:
        b = float(base_s)
        if b <= 0.0:
            return base_s
        if not adaptive_gen_budget_enabled():
            return b  # Floor — byte-identical when flag OFF

        weight = compute_payload_weight(ctx)
        max_mult = _env_float(
            _MAX_MULTIPLIER_ENV_VAR, _DEFAULT_MAX_MULTIPLIER,
            minimum=1.0,
        )
        # multiplier = 1 + score, saturated at max_mult. Monotone
        # non-decreasing in score; score 0 → multiplier 1.0 → b.
        multiplier = 1.0 + max(0.0, weight.score)
        if multiplier > max_mult:
            multiplier = max_mult

        scaled = b * multiplier
        ceiling = _wall_ceiling_s(b)

        # Floor then Ceiling — order matters: never below base,
        # never above the thermodynamic cap.
        result = max(b, scaled)
        if result > ceiling:
            result = ceiling
        if result < b:  # ceiling can't drop us below the floor
            result = b
        return result
    except Exception:  # noqa: BLE001 — fail-soft to the floor
        logger.debug(
            "[AdaptiveGenBudget] scale_gen_timeout fell back to "
            "base_s (%.1fs) on error", base_s, exc_info=True,
        )
        try:
            return float(base_s)
        except Exception:  # noqa: BLE001
            return base_s


# ===========================================================================
# FlagRegistry self-registration (§33.3 walker)
# ===========================================================================


def register_flags(registry: Any) -> int:
    """Module-owned FlagRegistry registration.  NEVER raises."""
    try:
        from backend.core.ouroboros.governance.flag_registry import (
            Category,
            FlagSpec,
            FlagType,
        )
    except ImportError:
        return 0

    src = "backend/core/ouroboros/governance/adaptive_gen_budget.py"
    specs = [
        FlagSpec(
            name=ADAPTIVE_GEN_BUDGET_ENABLED_ENV_VAR,
            type=FlagType.BOOL,
            default=True,
            description=(
                "Payload-adaptive GENERATE budget master switch "
                "(GRADUATED default-TRUE, Slice 79). The route-base "
                "_gen_timeout is scaled by deterministic payload "
                "geometry (text tokens + file count) at the "
                "generate_runner deadline seam — floor=route base "
                "(zero regression on trivial ops), ceiling=session "
                "wall cap. Set falsey to restore the pre-graduation "
                "byte-identical path."
            ),
            category=Category.SAFETY,
            source_file=src,
            example="true",
            since="v3.7 Stage 2 Slice 2 (2026-05-16); graduated Slice 79 (2026-06-03)",
        ),
        FlagSpec(
            name=_MAX_MULTIPLIER_ENV_VAR,
            type=FlagType.FLOAT,
            default=_DEFAULT_MAX_MULTIPLIER,
            description=(
                "Saturation ceiling for the payload-weight "
                "multiplier (and the no-wall-cap fallback ceiling "
                "as a multiple of route base). Bounds how much "
                "headroom the heaviest payload can claim."
            ),
            category=Category.TUNING,
            source_file=src,
            example=str(_DEFAULT_MAX_MULTIPLIER),
            since="v3.7 Stage 2 Slice 2 adaptive gen budget (2026-05-16)",
        ),
        FlagSpec(
            name=_TOKEN_REF_ENV_VAR,
            type=FlagType.FLOAT,
            default=_DEFAULT_TOKEN_REF,
            description=(
                "Reference token volume that contributes 1.0 to the "
                "payload weight score (calibration, not magic — "
                "env-tunable like the route _gen_timeout windows)."
            ),
            category=Category.TUNING,
            source_file=src,
            example=str(_DEFAULT_TOKEN_REF),
            since="v3.7 Stage 2 Slice 2 adaptive gen budget (2026-05-16)",
        ),
        FlagSpec(
            name=_FILE_REF_ENV_VAR,
            type=FlagType.FLOAT,
            default=_DEFAULT_FILE_REF,
            description=(
                "Reference file count that contributes 1.0 to the "
                "payload weight score (calibration, env-tunable)."
            ),
            category=Category.TUNING,
            source_file=src,
            example=str(_DEFAULT_FILE_REF),
            since="v3.7 Stage 2 Slice 2 adaptive gen budget (2026-05-16)",
        ),
        FlagSpec(
            name=_CHARS_PER_TOKEN_ENV_VAR,
            type=FlagType.FLOAT,
            default=_DEFAULT_CHARS_PER_TOKEN,
            description=(
                "Chars-per-token heuristic divisor for the text "
                "token estimate (standard ~4; env-tunable)."
            ),
            category=Category.TUNING,
            source_file=src,
            example=str(_DEFAULT_CHARS_PER_TOKEN),
            since="v3.7 Stage 2 Slice 2 adaptive gen budget (2026-05-16)",
        ),
        FlagSpec(
            name=_FRACTION_OF_WALL_ENV_VAR,
            type=FlagType.FLOAT,
            default=_DEFAULT_FRACTION_OF_WALL,
            description=(
                "Fraction of the session --max-wall-seconds cap a "
                "single op's adaptive budget may claim (so parallel "
                "discriminator ops cannot each demand the whole "
                "wall). Thermodynamic ceiling input."
            ),
            category=Category.TUNING,
            source_file=src,
            example=str(_DEFAULT_FRACTION_OF_WALL),
            since="v3.7 Stage 2 Slice 2 adaptive gen budget (2026-05-16)",
        ),
    ]
    n = 0
    for spec in specs:
        try:
            registry.register(spec)
            n += 1
        except Exception:  # noqa: BLE001 — best-effort
            logger.debug(
                "[AdaptiveGenBudget] flag register failed for %s",
                spec.name, exc_info=True,
            )
    return n


__all__ = [
    "ADAPTIVE_GEN_BUDGET_ENABLED_ENV_VAR",
    "PayloadWeight",
    "compute_payload_weight",
    "scale_gen_timeout",
    "adaptive_gen_budget_enabled",
    "register_flags",
]

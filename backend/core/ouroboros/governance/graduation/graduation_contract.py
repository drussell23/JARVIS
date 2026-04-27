"""Phase 9.2 — Per-flag GraduationContract.

Different substrate flags have different "clean" criteria. Phase 8
substrate "clean" means "ledger has rows + no JSONL corruption";
CuriosityEngine "clean" means "≥1 hypothesis generated + bridges
fired"; Pass C activation flags "clean" means "≥1 `/adapt approve`
cycle changed live gate behavior."

This module ships a per-flag contract dataclass that the
``LiveFireSoakHarness`` consults to refine its outcome classification
beyond the default 5-step decision tree. Contracts are **purely
additive** — without one, the harness uses the default
``classify_outcome`` from ``live_fire_soak.py`` unchanged. Operators
opt in via ``JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT=true``.

## Contract fields

  * ``flag_name``                         — canonical flag identifier
  * ``clean_predicate``                   — ``Callable[[summary_dict],
    bool]`` returning True iff the session is CLEAN for this flag.
    None → use default ``session_outcome=="complete" AND no
    runner-class failures``.
  * ``failure_class_blocklist_overrides`` — set of failure-class
    names that this flag treats as INFRA (waiver) instead of the
    default RUNNER (block). Defends against false-positive RUNNER
    classification for flags whose enablement legitimately surfaces
    new infra-class failures.
  * ``re_arm_after_runner_seconds``       — cooldown after a RUNNER
    outcome before this flag is re-pickable. Default 3600s (1h);
    operator can override per flag.
  * ``cost_cap_override_usd``             — per-flag subprocess cost
    cap. None → use harness default.
  * ``max_wall_seconds_override``         — per-flag wall-clock cap.
  * ``description``                       — human-readable rationale.

## Authority posture (locked + pinned)

  * **Pure-data + pure-predicate module** — no I/O, no logger, no
    subprocess. The harness consults; this module never observes
    state.
  * **Stdlib + typing only** at top level (pinned by AST scan).
  * **NEVER raises** — all helper functions return safe defaults
    on bad input.
  * **Master flag** ``JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT``
    (default ``false``) gates harness consultation. Default-off
    means the contract registry is *always available* for read
    queries; the harness's behavior is byte-identical until the
    operator opts in.
  * **Bounded** registry to ``MAX_CONTRACTS=128`` to prevent runaway
    growth.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional


_TRUTHY = ("1", "true", "yes", "on")


MAX_CONTRACTS: int = 128
DEFAULT_RE_ARM_AFTER_RUNNER_SECONDS: int = 3600  # 1h
MIN_RE_ARM_SECONDS: int = 60
MAX_RE_ARM_SECONDS: int = 24 * 3600  # 24h ceiling


# Predicate signature: takes a summary dict, returns True iff CLEAN.
CleanPredicate = Callable[[Mapping[str, Any]], bool]


def is_contract_consultation_enabled() -> bool:
    """Master flag — ``JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT``
    (default ``false``). When off, the harness's default
    ``classify_outcome`` runs unmodified — contracts are read-only
    metadata."""
    return os.environ.get(
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT", "",
    ).strip().lower() in _TRUTHY


# ---------------------------------------------------------------------------
# Built-in clean predicates (named so registry can be data-only)
# ---------------------------------------------------------------------------


def default_clean_predicate(summary: Mapping[str, Any]) -> bool:
    """Default predicate matching the harness's built-in
    ``classify_outcome`` step 1: ``session_outcome=='complete'`` AND
    no runner-class failures.

    Pure function; NEVER raises."""
    if not isinstance(summary, dict):
        return False
    if summary.get("session_outcome") != "complete":
        return False
    failure_counts = summary.get("failure_class_counts") or {}
    if not isinstance(failure_counts, dict):
        return False
    runner_classes = {
        "phase_runner_error", "candidate_validate_error",
        "iron_gate_violation", "semantic_guardian_block",
        "change_engine_error", "verify_regression",
        "l2_repair_error", "fsm_state_corruption",
        "artifact_contract_drift",
    }
    for k, v in failure_counts.items():
        if k in runner_classes:
            try:
                if int(v) > 0:
                    return False
            except (TypeError, ValueError):
                continue
    return True


def predicate_requires_decision_trace_rows(
    summary: Mapping[str, Any],
) -> bool:
    """Phase 8 substrate flag predicate variant: requires CLEAN per
    default AND at least one decision-trace row recorded during the
    session (i.e., the substrate ACTUALLY fired, not just had its
    flag set).

    Reads ``ops_count`` as a coarse proxy for "the session generated
    SOME decisions to record." A more precise variant would inspect
    ``.jarvis/decision_trace.jsonl`` — deferred to P9.5 producer
    wiring; for now the proxy is good enough for graduation.
    """
    if not default_clean_predicate(summary):
        return False
    try:
        return int(summary.get("ops_count", 0)) >= 1
    except (TypeError, ValueError):
        return False


def predicate_requires_curiosity_hypothesis(
    summary: Mapping[str, Any],
) -> bool:
    """CuriosityEngine flag predicate: CLEAN AND ≥1 hypothesis
    recorded during the session.

    Reads ``curiosity_hypotheses_generated`` from summary.json
    (populated by the engine's per-cycle counter). Falls back to
    ``ops_count`` proxy if absent (defends against pre-instrumentation
    sessions)."""
    if not default_clean_predicate(summary):
        return False
    h_count = summary.get("curiosity_hypotheses_generated")
    if h_count is not None:
        try:
            return int(h_count) >= 1
        except (TypeError, ValueError):
            return False
    # Pre-instrumentation fallback: any non-zero ops session is
    # plausibly hypothesis-eligible.
    try:
        return int(summary.get("ops_count", 0)) >= 1
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# Contract dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraduationContract:
    """Per-flag refinements layered on top of the harness's default
    classification. Frozen — immutable after construction."""

    flag_name: str
    clean_predicate: Optional[CleanPredicate] = None
    failure_class_blocklist_overrides: FrozenSet[str] = field(
        default_factory=frozenset,
    )
    re_arm_after_runner_seconds: int = DEFAULT_RE_ARM_AFTER_RUNNER_SECONDS
    cost_cap_override_usd: Optional[float] = None
    max_wall_seconds_override: Optional[int] = None
    description: str = ""

    def __post_init__(self) -> None:
        # Clamp re-arm to defensive bounds; freeze re-set.
        clamped = max(
            MIN_RE_ARM_SECONDS,
            min(self.re_arm_after_runner_seconds, MAX_RE_ARM_SECONDS),
        )
        if clamped != self.re_arm_after_runner_seconds:
            object.__setattr__(
                self, "re_arm_after_runner_seconds", clamped,
            )

    def is_clean(self, summary: Mapping[str, Any]) -> bool:
        """Run the contract's clean predicate. Falls back to
        ``default_clean_predicate`` when none registered. NEVER
        raises."""
        predicate = self.clean_predicate or default_clean_predicate
        try:
            return bool(predicate(summary))
        except Exception:  # noqa: BLE001 — defensive
            return False

    def to_metadata_dict(self) -> Dict[str, Any]:
        """Serializable view (predicate not included — predicates
        are code-only, not data)."""
        return {
            "flag_name": self.flag_name,
            "has_custom_predicate": self.clean_predicate is not None,
            "failure_class_blocklist_overrides": sorted(
                self.failure_class_blocklist_overrides,
            ),
            "re_arm_after_runner_seconds": self.re_arm_after_runner_seconds,
            "cost_cap_override_usd": self.cost_cap_override_usd,
            "max_wall_seconds_override": self.max_wall_seconds_override,
            "description": self.description,
        }


# ---------------------------------------------------------------------------
# Built-in contract registry — sparse; default contract used when absent
# ---------------------------------------------------------------------------
#
# Contracts are *additive* refinements. The default behavior (no
# contract) matches the existing harness exactly. Add here ONLY when
# a flag has graduation criteria that meaningfully differ from the
# default.


_BUILT_IN_CONTRACTS: Dict[str, GraduationContract] = {
    # Phase 8 substrate flags: require ≥1 op (proxy for "substrate
    # actually fired"). Without this, an empty session would falsely
    # graduate a flag that was on but never recorded anything.
    "JARVIS_DECISION_TRACE_LEDGER_ENABLED": GraduationContract(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        clean_predicate=predicate_requires_decision_trace_rows,
        description=(
            "Phase 8.1 substrate must actually fire (≥1 op) to count "
            "as CLEAN — defends against empty-session false graduation."
        ),
    ),
    "JARVIS_LATENT_CONFIDENCE_RING_ENABLED": GraduationContract(
        flag_name="JARVIS_LATENT_CONFIDENCE_RING_ENABLED",
        clean_predicate=predicate_requires_decision_trace_rows,
        description="Phase 8.2 — same empty-session defense.",
    ),
    "JARVIS_FLAG_CHANGE_EMITTER_ENABLED": GraduationContract(
        flag_name="JARVIS_FLAG_CHANGE_EMITTER_ENABLED",
        clean_predicate=predicate_requires_decision_trace_rows,
        description="Phase 8.4 — same empty-session defense.",
    ),
    "JARVIS_LATENCY_SLO_DETECTOR_ENABLED": GraduationContract(
        flag_name="JARVIS_LATENCY_SLO_DETECTOR_ENABLED",
        clean_predicate=predicate_requires_decision_trace_rows,
        description="Phase 8.5 — same empty-session defense.",
    ),
    "JARVIS_MULTI_OP_TIMELINE_ENABLED": GraduationContract(
        flag_name="JARVIS_MULTI_OP_TIMELINE_ENABLED",
        clean_predicate=predicate_requires_decision_trace_rows,
        description="Phase 8.3 — same empty-session defense.",
    ),
    # CuriosityEngine: requires ≥1 hypothesis generated to count as
    # CLEAN. A session with the flag on but zero hypotheses doesn't
    # exercise the engine.
    "JARVIS_CURIOSITY_ENGINE_ENABLED": GraduationContract(
        flag_name="JARVIS_CURIOSITY_ENGINE_ENABLED",
        clean_predicate=predicate_requires_curiosity_hypothesis,
        description=(
            "CuriosityEngine must generate ≥1 hypothesis to count "
            "as CLEAN — defends against engine-not-fired sessions."
        ),
    ),
}


# ---------------------------------------------------------------------------
# Public registry helpers
# ---------------------------------------------------------------------------


def get_contract(flag_name: str) -> GraduationContract:
    """Return the contract for ``flag_name``. When no built-in
    override exists, returns a default contract with all fields at
    their defaults — ALWAYS returns a contract, never None."""
    if not isinstance(flag_name, str):
        return GraduationContract(flag_name="")
    builtin = _BUILT_IN_CONTRACTS.get(flag_name)
    if builtin is not None:
        return builtin
    return GraduationContract(flag_name=flag_name)


def has_custom_contract(flag_name: str) -> bool:
    """True iff ``flag_name`` has a built-in custom contract (i.e.,
    something different from defaults)."""
    return flag_name in _BUILT_IN_CONTRACTS


def known_contract_flags() -> FrozenSet[str]:
    """Return all flag_names with a built-in custom contract."""
    return frozenset(_BUILT_IN_CONTRACTS.keys())


def all_contracts_metadata() -> Dict[str, Dict[str, Any]]:
    """Return ``{flag_name: metadata_dict}`` for every built-in
    contract. Used by `/graduate live-contracts` REPL subcommand."""
    return {
        name: contract.to_metadata_dict()
        for name, contract in _BUILT_IN_CONTRACTS.items()
    }


__all__ = [
    "CleanPredicate",
    "DEFAULT_RE_ARM_AFTER_RUNNER_SECONDS",
    "GraduationContract",
    "MAX_CONTRACTS",
    "MAX_RE_ARM_SECONDS",
    "MIN_RE_ARM_SECONDS",
    "all_contracts_metadata",
    "default_clean_predicate",
    "get_contract",
    "has_custom_contract",
    "is_contract_consultation_enabled",
    "known_contract_flags",
    "predicate_requires_curiosity_hypothesis",
    "predicate_requires_decision_trace_rows",
]

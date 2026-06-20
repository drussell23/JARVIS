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

import inspect
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, FrozenSet, Mapping, Optional, Tuple


_TRUTHY = ("1", "true", "yes", "on")


MAX_CONTRACTS: int = 128
DEFAULT_RE_ARM_AFTER_RUNNER_SECONDS: int = 3600  # 1h
MIN_RE_ARM_SECONDS: int = 60
MAX_RE_ARM_SECONDS: int = 24 * 3600  # 24h ceiling


# Predicate signature: ``(summary) -> bool`` OR ``(summary, metrics) -> bool``.
# Both arities are supported; :meth:`GraduationContract.is_clean` dispatches by
# inspecting the callable's positional arity. ``metrics`` is duck-typed (the
# telemetry_parse.Metrics object) so this module stays import-light + stdlib-
# only (no telemetry_parse import, no cycle).
CleanPredicate = Callable[..., bool]


def is_contract_consultation_enabled() -> bool:
    """Master flag — ``JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT``
    (default ``false``). When off, the harness's default
    ``classify_outcome`` runs unmodified — contracts are read-only
    metadata."""
    return os.environ.get(
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT", "",
    ).strip().lower() in _TRUTHY


def is_telemetry_arbiter_enabled() -> bool:
    """Master flag — ``JARVIS_LIVE_FIRE_TELEMETRY_ARBITER_ENABLED``
    (default ``false``). Gates the Sovereign Arbiter Protocol: when on
    (AND contract consultation is on AND harvester Metrics are available),
    the substrate routes outcome classification through
    :meth:`GraduationContract.arbitrate` instead of the summary-only
    contract refinement. Off ⇒ byte-identical legacy behavior."""
    return os.environ.get(
        "JARVIS_LIVE_FIRE_TELEMETRY_ARBITER_ENABLED", "",
    ).strip().lower() in _TRUTHY


def _predicate_is_metrics_aware(predicate: CleanPredicate) -> bool:
    """True iff ``predicate`` can accept a second positional ``metrics``
    argument. Defensive: callables without an introspectable signature
    (builtins, some C-level callables) fall back to legacy 1-arg.
    NEVER raises."""
    try:
        sig = inspect.signature(predicate)
    except (TypeError, ValueError):
        return False
    positional = 0
    for p in sig.parameters.values():
        if p.kind in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        ):
            positional += 1
        elif p.kind is inspect.Parameter.VAR_POSITIONAL:
            return True  # *args swallows metrics
    return positional >= 2


def _metrics_shows_full_recovery(metrics: Any) -> bool:
    """Deterministic 'the system autonomously caught + healed' signal.

    Requires the COMPLETE self-heal trajectory: a live-fire failure
    fired, was routed back as a build fault, the op retried, and a
    subsequent recovery state was observed — AND no OOM anomaly. Pure;
    duck-typed; NEVER raises."""
    if metrics is None:
        return False
    try:
        fired = bool(getattr(metrics, "livefire_fired", None))
        return (
            fired
            and bool(getattr(metrics, "routed_build", False))
            and bool(getattr(metrics, "retried", False))
            and bool(getattr(metrics, "recovered", False))
            and not bool(getattr(metrics, "oom", False))
        )
    except Exception:  # noqa: BLE001 — defensive
        return False


def _is_positive_int_value(v: Any) -> bool:
    """True iff ``v`` coerces to an int > 0. Pure; NEVER raises."""
    try:
        return int(v) > 0
    except (TypeError, ValueError):
        return False


def _metrics_anomaly(metrics: Any) -> Optional[str]:
    """Return a structured anomaly tag iff a hard hardware/wiring
    invariant was violated (``oom`` / ``gate_inert``), else None. These
    DISQUALIFY a CLEAN classification (P1, highest priority). Pure;
    NEVER raises."""
    if metrics is None:
        return None
    try:
        if bool(getattr(metrics, "oom", False)):
            return "oom"
        if bool(getattr(metrics, "gate_inert", False)):
            return "gate_inert"
    except Exception:  # noqa: BLE001
        return None
    return None


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


def _session_ops_count(summary: Mapping[str, Any]) -> int:
    """Canonical reader for "how many ops did this session run."

    Slice 4 latent-bug fix (2026-05-05): the battle-test
    ``summary.json`` schema does NOT emit a top-level
    ``ops_count`` field. The canonical session-level op count
    lives at ``summary.strategic_drift.total_ops`` (computed by
    ``GoalActivityLedger.compute_drift`` in ``harness.py``). Both
    pre-existing predicates were reading the non-existent
    top-level ``ops_count`` key, silently zeroing every Phase 9
    soak's evidence and masking real op activity (verified via
    debug.log on bt-2026-05-05-224545: 16 ops fired including 3
    cadence_synthetic; ``strategic_drift.total_ops=16`` but the
    legacy reader returned 0 → contract downgraded CLEAN→RUNNER
    forever).

    Reuse-before-inventing: ``strategic_drift.total_ops`` is the
    canonical authoritative count; this helper composes it with
    a top-level ``ops_count`` read for forward-compat (when the
    harness's ``save_summary`` path eventually emits ops_count
    explicitly, the helper will pick it up first; until then the
    fallback closes the gap).

    Pure function. NEVER raises. Returns 0 on any malformed
    input.
    """
    if not isinstance(summary, dict):
        return 0
    # Forward-compat: top-level ``ops_count`` if/when the harness
    # emits it. Today (2026-05-05) this returns 0 for every
    # session; the fallback below carries the load.
    try:
        top = int(summary.get("ops_count", 0))
        if top > 0:
            return top
    except (TypeError, ValueError):
        pass
    # Canonical fallback: strategic_drift.total_ops. This is the
    # field the harness ACTUALLY emits for the "how many ops did
    # this session run" question.
    drift = summary.get("strategic_drift")
    if isinstance(drift, dict):
        try:
            return max(0, int(drift.get("total_ops", 0)))
        except (TypeError, ValueError):
            return 0
    return 0


def predicate_requires_decision_trace_rows(
    summary: Mapping[str, Any],
) -> bool:
    """Phase 8 substrate flag predicate variant: requires CLEAN per
    default AND at least one decision-trace row recorded during the
    session (i.e., the substrate ACTUALLY fired, not just had its
    flag set).

    Reads the canonical session ops count via
    :func:`_session_ops_count` (which composes
    ``strategic_drift.total_ops`` per the Slice 4 latent-bug fix).
    A more precise variant would inspect
    ``.jarvis/decision_trace.jsonl`` — deferred to P9.5 producer
    wiring; for now the proxy is good enough for graduation.
    """
    if not default_clean_predicate(summary):
        return False
    return _session_ops_count(summary) >= 1


def predicate_requires_curiosity_hypothesis(
    summary: Mapping[str, Any],
) -> bool:
    """CuriosityEngine flag predicate: CLEAN AND ≥1 hypothesis
    recorded during the session.

    Reads ``curiosity_hypotheses_generated`` from summary.json
    (populated by the engine's per-cycle counter). Falls back to
    the canonical ops-count proxy via :func:`_session_ops_count`
    if absent (defends against pre-instrumentation sessions)."""
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
    return _session_ops_count(summary) >= 1


def predicate_requires_live_kernel_validation(
    summary: Mapping[str, Any],
    metrics: Any = None,
) -> bool:
    """Capstone Dogfood predicate for ``JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED``.

    The LiveKernelValidator's OWN graduation demands the highest evidence
    bar — proof from the harvester telemetry that the validator actually
    exercised its self-heal path on this session, not merely that the
    session completed. Metrics-aware (2-arg) signature.

    CLEAN iff ALL hold:
      * default clean (``session_outcome=='complete'``, no runner faults)
      * ``metrics.livefire_fired``  — a live-fire test executed (≥1 catch)
      * NOT ``metrics.oom``         — zero OOM anomalies (16GB invariant)
      * ``metrics.recovered``       — the candidate was successfully
        processed (state=applied/complete observed)

    When metrics are unavailable (arbiter off / no telemetry), falls back
    to the default predicate so the contract degrades gracefully rather
    than blocking graduation on missing instrumentation. Pure; NEVER
    raises."""
    if not default_clean_predicate(summary):
        return False
    if metrics is None:
        # No telemetry stream — cannot prove self-heal; defer to default
        # (deployment proven). The arbiter only routes here when Metrics
        # exist, so production graduation still demands the full bar.
        return True
    try:
        return (
            bool(getattr(metrics, "livefire_fired", None))
            and not bool(getattr(metrics, "oom", False))
            and bool(getattr(metrics, "recovered", False))
        )
    except Exception:  # noqa: BLE001 — defensive
        return False


def predicate_cognitive_graduation(
    summary: Mapping[str, Any],
    metrics: Any = None,
) -> bool:
    """Sovereign Cognitive Crucible universal veto (2026-06-20).

    The default clean-predicate for any flag WITHOUT a more-specific built-in
    contract. Composes the canonical :func:`default_clean_predicate` (which
    already buckets FSM-exhaustion-class faults — ``phase_runner_error`` /
    ``candidate_validate_error`` / ``fsm_state_corruption`` — as runner faults)
    with the two latency/structural vetoes the autonomic crucible demands:

      * TTFT degradation (``crucible_verdict.ttft_degraded``)
      * AST corruption    (``crucible_verdict.ast_corrupted``)

    Metrics-aware (2-arg), **VETO-ONLY** by construction: it returns ``False``
    ONLY on positive evidence of TTFT/AST harm, and ``True`` otherwise. This is
    deliberate — ``default_clean`` is already enforced upstream (the legacy
    ``classify_outcome`` the arbiter composes), and the arbiter's metrics-
    predicate step only ever *downgrades* a CLEAN outcome. A veto-only predicate
    therefore adds the latency/structural gate WITHOUT undoing the arbiter's
    recovery-override (P2): a session a legacy error + full self-heal promoted to
    CLEAN stays CLEAN unless it ALSO degraded TTFT or corrupted AST.

    **Fail-OPEN on absent telemetry** — ``metrics`` None (arbiter off / no
    parse) → no objection (cannot prove harm). Pure; NEVER raises.

    The ``crucible_verdict`` import is lazy to preserve this module's AST-pinned
    stdlib-only top-level import posture (``crucible_verdict`` is itself pure +
    stdlib-only, so there is no real coupling — only the pin to respect)."""
    if metrics is None:
        return True
    try:
        from backend.core.ouroboros.governance.graduation import (
            crucible_verdict as _cv,
        )
        ttft_bad, _ = _cv.ttft_degraded(metrics)
        ast_bad, _ = _cv.ast_corrupted(metrics)
        return (not ttft_bad) and (not ast_bad)
    except Exception:  # noqa: BLE001 — defensive; no objection on error
        return True


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

    def is_clean(
        self,
        summary: Mapping[str, Any],
        metrics: Any = None,
    ) -> bool:
        """Run the contract's clean predicate. Falls back to
        ``default_clean_predicate`` when none registered.

        Dual-signature dispatch: a ``(summary, metrics)`` predicate
        receives the harvester Metrics; a legacy ``(summary)`` predicate
        is called with summary only (metrics ignored safely). NEVER
        raises."""
        predicate = self.clean_predicate or default_clean_predicate
        try:
            if _predicate_is_metrics_aware(predicate):
                return bool(predicate(summary, metrics))
            return bool(predicate(summary))
        except Exception:  # noqa: BLE001 — defensive
            return False

    def arbitrate(
        self,
        *,
        legacy_outcome: str,
        runner_attributed: bool,
        class_notes: str,
        summary: Mapping[str, Any],
        metrics: Any,
    ) -> Tuple[str, bool, str]:
        """Sovereign Arbiter — resolve the legacy classify_outcome stream
        against the harvester Metrics stream via a deterministic priority
        matrix. Returns the synthesized ``(outcome, runner_attributed,
        notes)``. Pure; NEVER raises into the caller.

        Priority (a later rule can only TIGHTEN, never resurrect CLEAN
        once an anomaly pulled it down):

          P2 recovery override : legacy error + full self-heal -> CLEAN
          P1 anomaly guard     : oom / gate_inert  -> CLEAN forbidden (INFRA)
          P3 metrics predicate : CLEAN but contract predicate False -> RUNNER
          P4 blocklist override: RUNNER -> INFRA waiver

        ``metrics is None`` degrades gracefully to the legacy summary-only
        contract refinement (recovery/anomaly steps skip)."""
        outcome = str(legacy_outcome)
        ra = bool(runner_attributed)
        extra: list[str] = []
        try:
            # --- P2: autonomous-recovery override --------------------
            # A legacy infra/runner verdict that the system PROVABLY
            # caught + healed is actually a success. Recovery > static
            # error.
            if (
                outcome in ("infra", "runner")
                and _metrics_shows_full_recovery(metrics)
            ):
                outcome = "clean"
                ra = False
                extra.append("arbiter_recovery_override")

            # --- P1: anomaly guard (dominates CLEAN) -----------------
            # A hardware/wiring invariant violation can NEVER be CLEAN,
            # even post-recovery-override. Downgrades to INFRA waiver
            # (environmental fault, not feature fault).
            if outcome == "clean":
                anomaly = _metrics_anomaly(metrics)
                if anomaly is not None:
                    outcome = "infra"
                    ra = False
                    extra.append(f"arbiter_anomaly_{anomaly}")

            # --- P3: metrics-aware predicate downgrade ---------------
            # The contract gets the LAST word on whether a CLEAN session
            # actually exercised the feature (now Metrics-aware).
            if outcome == "clean" and self.clean_predicate is not None:
                if not self.is_clean(summary, metrics):
                    outcome = "runner"
                    ra = True
                    extra.append("contract_metrics_predicate_downgraded")

            # --- P4: legacy blocklist override (RUNNER -> INFRA) ------
            if outcome == "runner" and self.failure_class_blocklist_overrides:
                failure_counts = {}
                if isinstance(summary, Mapping):
                    failure_counts = summary.get("failure_class_counts") or {}
                if isinstance(failure_counts, dict):
                    positive = {
                        k for k, v in failure_counts.items()
                        if _is_positive_int_value(v)
                    }
                    if positive and positive.issubset(
                        self.failure_class_blocklist_overrides,
                    ):
                        outcome = "infra"
                        ra = False
                        extra.append(
                            "contract_blocklist_upgraded_runner_to_infra",
                        )
        except Exception:  # noqa: BLE001 — arbiter NEVER raises
            return (str(legacy_outcome), bool(runner_attributed), class_notes)

        notes = class_notes
        if extra:
            notes = (notes + "|" + ",".join(extra)) if notes else "|".join(
                extra,
            )
        return (outcome, ra, notes)

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
    # Capstone Dogfood Contract (Sovereign Telemetry Unification,
    # 2026-06-15). The LiveKernelValidator is the live-fire boot check
    # whose self-heal trajectory the telemetry harvester measures — so
    # its graduation is gated on harvester-PROVEN evidence that it fired,
    # did not OOM, and recovered. Metrics-aware predicate (2-arg). This
    # closes the loop: the validator graduates only when the telemetry
    # that watches it proves it works.
    "JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED": GraduationContract(
        flag_name="JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED",
        clean_predicate=predicate_requires_live_kernel_validation,
        description=(
            "LiveKernelValidator may only graduate when harvester "
            "telemetry proves a live-fire test executed, ZERO OOM "
            "anomalies occurred, and the candidate was successfully "
            "processed (self-heal recovered)."
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
    # Sovereign Cognitive Crucible: flags without a specific built-in contract
    # graduate under the universal TTFT/AST math-veto (composed on default-clean,
    # fail-open on absent telemetry). No hardcoded per-flag list — every
    # crucible-discovered candidate is auto-vetoed.
    return GraduationContract(
        flag_name=flag_name,
        clean_predicate=predicate_cognitive_graduation,
    )


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
    "is_telemetry_arbiter_enabled",
    "known_contract_flags",
    "predicate_requires_curiosity_hypothesis",
    "predicate_requires_decision_trace_rows",
    "predicate_requires_live_kernel_validation",
]

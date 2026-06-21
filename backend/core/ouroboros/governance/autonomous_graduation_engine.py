"""Slice 96 — Autonomous Telemetry-Driven Graduation Engine (TIERED).

Autonomously evaluates whether built-but-dormant §33.1 subsystems have
EARNED graduation (master flag FALSE→TRUE) and, for NON-safety
features, flips them — without ever bypassing the security cage.

## TIERED autonomy (the §1 zero-order-doll invariant)

Auto-activating a SAFETY / governance capability (a kill-switch or a
gate) is itself a governance self-modification. So the engine routes
SAFETY-tier graduations to an OPERATOR ADVISORY, not an auto-flip.
NON-safety (STANDARD-tier) features auto-flip. The TIER is DATA-DRIVEN
off the FlagRegistry category:

    FlagSpec.category is Category.SAFETY  → SAFETY tier (advisory)
    else                                  → STANDARD tier (auto-flip)

with the governance_boundary_gate composed as a belt-and-suspenders
corroboration signal (a source_file that boundary-crosses into the
cage is also treated as SAFETY).

## No source mutation — the delivery channel

The engine NEVER edits ``flag_registry`` / ``*_seed.py`` source
defaults — that would land inside ``governance/`` and trip the
boundary gate + hash-cap. Instead, an AUTO_FLIP is delivered as an
immutable receipt appended to the durable env-override ledger
(:mod:`graduation_override_ledger`); a boot-time applier injects the
authorized flags into ``os.environ`` (OS-level, external to the cage).

## The mathematically-absolute decision matrix

For each candidate (= a flag in the GraduationLedger's
``eligible_flags()``), ALL gates must pass for READY; ANY failure
holds with a precise ``delta`` naming the failing gate:

  * Gate A (regression/telemetry): flag in ``eligible_flags()`` — the
    entry condition (zero runner/regression failures, clean-session
    cadence met). Composed, not reimplemented.
  * Gate B (AST stability): ``shipped_code_invariants.validate_all()``
    has ZERO ``InvariantViolation`` whose ``target_file`` matches the
    flag's ``FlagSpec.source_file``. Any AST drift → HOLD AST_DRIFT.
  * Gate C (contract, if any): if a graduation contract in the
    unified dashboard maps to this flag, its verdict must be
    READY-equivalent; if no contract maps, Gate C is vacuously
    satisfied.

If all gates pass → READY. Tier from category; disposition is
STANDARD→AUTO_FLIP, SAFETY→APPROVAL_ADVISORY. Master OFF → DISABLED.

## Authority posture (§33.1)

* Master ``JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED`` default-FALSE.
* Pure / never-raises everywhere.
* Authority-asymmetry: stdlib-only at module load; lazy-imports the
  composed substrates; FORBIDS orchestrator / iron_gate / policy /
  providers / candidate_generator / urgency_router / change_engine /
  semantic_guardian / auto_committer / risk_tier_floor.
"""
from __future__ import annotations

import asyncio
import enum
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


AUTONOMOUS_GRADUATION_ENGINE_SCHEMA_VERSION: str = "graduation_engine.1"

_TRUTHY = ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


def autonomous_graduation_engine_enabled() -> bool:
    """``JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED`` (default
    FALSE)."""
    raw = os.environ.get(
        "JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED", "",
    ).strip().lower()
    return raw in _TRUTHY


def advisory_ledger_path() -> str:
    raw = os.environ.get("JARVIS_GRADUATION_ADVISORY_LEDGER_PATH")
    if raw:
        return raw
    return ".jarvis/graduation_advisories.jsonl"


# ---------------------------------------------------------------------------
# Closed enums
# ---------------------------------------------------------------------------


class GraduationTier(str, enum.Enum):
    """Closed 2-value taxonomy. The TIER decides autonomy:
    STANDARD auto-flips; SAFETY routes to operator approval."""

    STANDARD = "standard"
    SAFETY = "safety"


class GraduationDisposition(str, enum.Enum):
    """Closed 4-value taxonomy — the engine's verdict per candidate."""

    AUTO_FLIP = "auto_flip"
    APPROVAL_ADVISORY = "approval_advisory"
    HOLD = "hold"
    DISABLED = "disabled"


class HoldReason(str, enum.Enum):
    """Closed taxonomy of why a candidate was held."""

    NOT_ELIGIBLE = "not_eligible"
    AST_DRIFT = "ast_drift"
    CONTRACT_NOT_READY = "contract_not_ready"
    MASTER_OFF = "master_off"
    UNKNOWN_SPEC = "unknown_spec"
    EVALUATION_ERROR = "evaluation_error"


# ---------------------------------------------------------------------------
# Frozen artifacts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GraduationDecision:
    """One candidate's decision. Frozen — §33.5 versioned artifact."""

    flag_name: str
    tier: GraduationTier
    disposition: GraduationDisposition
    evidence: Dict[str, Any]
    delta: str
    evidence_sha256: str
    schema_version: str = AUTONOMOUS_GRADUATION_ENGINE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "flag_name": self.flag_name,
            "tier": self.tier.value,
            "disposition": self.disposition.value,
            "evidence": self.evidence,
            "delta": self.delta,
            "evidence_sha256": self.evidence_sha256,
        }


@dataclass(frozen=True)
class GraduationEngineReport:
    """Aggregate report. Frozen — §33.5 versioned artifact."""

    schema_version: str
    evaluated_at_unix: float
    decisions: Tuple[GraduationDecision, ...]
    auto_flipped: Tuple[str, ...]
    advisories: Tuple[str, ...]
    held: Tuple[str, ...]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "evaluated_at_unix": float(self.evaluated_at_unix),
            "decisions": [d.to_dict() for d in self.decisions],
            "auto_flipped": list(self.auto_flipped),
            "advisories": list(self.advisories),
            "held": list(self.held),
        }


@dataclass(frozen=True)
class ExecutionResult:
    """Outcome of executing a report — receipts + advisories emitted."""

    recorded_overrides: Tuple[str, ...] = field(default_factory=tuple)
    advisories_emitted: Tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "recorded_overrides": list(self.recorded_overrides),
            "advisories_emitted": list(self.advisories_emitted),
        }


# ---------------------------------------------------------------------------
# Evidence hashing
# ---------------------------------------------------------------------------


def _evidence_sha256(evidence: Dict[str, Any]) -> str:
    try:
        blob = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError):
        blob = repr(evidence)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Substrate composition (lazy — keeps module-load stdlib-only)
# ---------------------------------------------------------------------------


def _default_ledger() -> Any:
    from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
        get_default_ledger,
    )
    return get_default_ledger()


def _default_registry() -> Any:
    from backend.core.ouroboros.governance.flag_registry import (
        ensure_seeded,
    )
    return ensure_seeded()


def _default_validate_all() -> Tuple[Any, ...]:
    """Lazy bridge to shipped_code_invariants.validate_all().

    Slice 96 review fix — does NOT swallow failures to ``()``. An empty
    tuple means "validator ran, found no violations" (Gate B clean); a
    FAILURE must be distinguishable so Gate B can fail CLOSED. The
    exception propagates to ``_drifted_source_files``, which converts it
    to the ``None`` (unavailable) sentinel that HOLDs every candidate.
    """
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        validate_all,
    )
    return tuple(validate_all())


def _safety_category_names() -> Tuple[str, ...]:
    """The category values that signal a SAFETY tier. Lazy-resolves the
    canonical ``flag_registry.Category.SAFETY`` enum so the tier signal
    is DATA-DRIVEN off the registry, not a hardcoded string."""
    try:
        from backend.core.ouroboros.governance.flag_registry import Category
        return (Category.SAFETY.value,)
    except Exception:  # noqa: BLE001
        return ("safety",)


def _known_category_names() -> Tuple[str, ...]:
    """Every recognized ``flag_registry.Category`` value. STANDARD (the
    auto-flippable tier) requires POSITIVE recognition: a category that is
    NOT in this set (None / "" / a forged/garbage value) is treated as
    SAFETY (fail-CLOSED) so a malformed or partially-registered spec can
    never silently auto-activate. Slice 96 review fix."""
    try:
        from backend.core.ouroboros.governance.flag_registry import Category
        return tuple(c.value for c in Category)
    except Exception:  # noqa: BLE001
        # Lazy-import failure → empty set → every flag reads as "unknown
        # category" → SAFETY tier. Fail-closed by construction.
        return ()


def _boundary_crosses(source_file: str) -> bool:
    """Belt-and-suspenders SAFETY corroboration: a source_file that
    boundary-crosses into the governance cage is treated as SAFETY-tier
    even if its category somehow isn't SAFETY. Composes the canonical
    governance_boundary_gate predicate. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            evaluate_target_files,
        )
        report = evaluate_target_files([source_file])
        return getattr(report.verdict, "value", "") == "boundary_crossed"
    except Exception:  # noqa: BLE001 — defensive
        return False


def _contract_verdict_for_flag(flag_name: str) -> Optional[bool]:
    """Gate C: if a graduation contract in the unified dashboard maps to
    this flag, return whether its verdict is READY-equivalent. Returns
    None when NO contract maps (Gate C vacuously satisfied). Composes
    ``unified_graduation_dashboard.aggregate_dashboard``. NEVER
    raises."""
    try:
        from backend.core.ouroboros.governance.unified_graduation_dashboard import (  # noqa: E501
            aggregate_dashboard,
        )
        snapshot = aggregate_dashboard()
        for row in snapshot.rows:
            # Contract rows are keyed by contract name, not flag name.
            # A contract maps to a flag only if its name matches the
            # flag (today none do — Gate C is vacuous). Conservative:
            # exact-match only; no fuzzy mapping.
            if getattr(row, "source", "") != "contract":
                continue
            if getattr(row, "name", "") != flag_name:
                continue
            return getattr(row.verdict, "value", "") == "ready"
    except Exception:  # noqa: BLE001 — defensive
        return None
    return None


# ---------------------------------------------------------------------------
# evaluate_graduations — the decision matrix
# ---------------------------------------------------------------------------


def evaluate_graduations(
    *,
    now_unix: Optional[float] = None,
    ledger: Optional[Any] = None,
    registry: Optional[Any] = None,
    validate_all_fn: Optional[Callable[[], Tuple[Any, ...]]] = None,
) -> GraduationEngineReport:
    """Evaluate every eligible candidate against the absolute decision
    matrix. Pure read; NEVER raises.

    ``ledger`` / ``registry`` / ``validate_all_fn`` are injectable for
    hermetic testing; production callers omit them to compose the
    canonical singletons."""
    ts = float(now_unix) if now_unix is not None else time.time()

    if not autonomous_graduation_engine_enabled():
        # Master off — emit a DISABLED report. Still enumerate
        # candidates so the report is shaped, but disposition=DISABLED.
        return _disabled_report(ts, ledger, registry)

    try:
        live_ledger = ledger if ledger is not None else _default_ledger()
    except Exception as exc:  # noqa: BLE001
        logger.debug("[GraduationEngine] ledger init failed: %s", exc)
        return GraduationEngineReport(
            schema_version=AUTONOMOUS_GRADUATION_ENGINE_SCHEMA_VERSION,
            evaluated_at_unix=ts,
            decisions=(), auto_flipped=(), advisories=(), held=(),
        )
    try:
        live_registry = (
            registry if registry is not None else _default_registry()
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[GraduationEngine] registry init failed: %s", exc)
        live_registry = None

    validate_fn = validate_all_fn or _default_validate_all

    # Gate A entry condition — the eligible candidate set.
    try:
        candidates = list(live_ledger.eligible_flags())
    except Exception as exc:  # noqa: BLE001
        logger.debug("[GraduationEngine] eligible_flags failed: %s", exc)
        candidates = []

    # Gate B input — AST violations grouped by target_file (one call).
    drifted_files = _drifted_source_files(validate_fn)

    safety_names = _safety_category_names()

    decisions: List[GraduationDecision] = []
    for flag in candidates:
        decisions.append(
            _evaluate_one(
                flag,
                registry=live_registry,
                ledger=live_ledger,
                drifted_files=drifted_files,
                safety_names=safety_names,
            )
        )

    auto = tuple(
        d.flag_name for d in decisions
        if d.disposition is GraduationDisposition.AUTO_FLIP
    )
    adv = tuple(
        d.flag_name for d in decisions
        if d.disposition is GraduationDisposition.APPROVAL_ADVISORY
    )
    held = tuple(
        d.flag_name for d in decisions
        if d.disposition is GraduationDisposition.HOLD
    )
    return GraduationEngineReport(
        schema_version=AUTONOMOUS_GRADUATION_ENGINE_SCHEMA_VERSION,
        evaluated_at_unix=ts,
        decisions=tuple(decisions),
        auto_flipped=auto,
        advisories=adv,
        held=held,
    )


def _drifted_source_files(
    validate_fn: Callable[[], Tuple[Any, ...]],
) -> Optional[frozenset]:
    """Run the AST validator and project to the set of target_files
    carrying at least one violation. NEVER raises.

    Slice 96 review fix — returns ``None`` (the UNAVAILABLE sentinel) if
    the validator itself raises/errors, distinct from an empty frozenset
    ("ran clean, no drift"). Gate B treats ``None`` as fail-CLOSED: it
    cannot PROVE AST stability, so every candidate HOLDs. Swallowing the
    failure to an empty set (the prior behavior) would let a broken
    validator wave every flag through — a fail-OPEN safety hole."""
    try:
        violations = validate_fn()
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "[GraduationEngine] validate_fn raised — Gate B fail-CLOSED "
            "(holding all candidates): %s", exc,
        )
        return None
    files: set = set()
    for v in violations or ():
        tf = getattr(v, "target_file", None)
        if isinstance(tf, str) and tf:
            files.add(_normalize(tf))
    return frozenset(files)


def _normalize(path: str) -> str:
    return path.replace("\\", "/").lstrip("./").strip()


def _evaluate_one(
    flag: str,
    *,
    registry: Optional[Any],
    ledger: Any,
    drifted_files: Optional[frozenset],
    safety_names: Tuple[str, ...],
) -> GraduationDecision:
    """Compute the absolute decision for a single eligible flag.
    NEVER raises."""
    try:
        spec = registry.get_spec(flag) if registry is not None else None
    except Exception as exc:  # noqa: BLE001
        logger.debug("[GraduationEngine] get_spec(%s) failed: %s", flag, exc)
        spec = None

    # progress snapshot (composed telemetry evidence).
    try:
        progress = ledger.progress(flag)
    except Exception:  # noqa: BLE001
        progress = {}

    if spec is None:
        # Unknown to the registry — cannot determine tier/source_file.
        return _hold(
            flag, GraduationTier.STANDARD, HoldReason.UNKNOWN_SPEC,
            delta=(
                f"Gate-registry: flag {flag} not in FlagRegistry — "
                f"cannot resolve source_file / category"
            ),
            evidence={"progress": progress, "registry": "missing"},
        )

    source_file = _normalize(getattr(spec, "source_file", "") or "")
    category_val = getattr(getattr(spec, "category", None), "value", "")

    # ---- Tier determination (DATA-DRIVEN, category-primary) ----
    # The PRIMARY signal is the FlagRegistry category: SAFETY ("kill
    # switches, gates") → SAFETY tier. The governance_boundary_gate is
    # composed as a corroborating SECOND signal — but a boundary-cross
    # alone does NOT flip a non-SAFETY-category flag, because virtually
    # every §33.1 flag's source_file lives inside governance/ (so an
    # unconditional OR would neuter the STANDARD tier entirely). The
    # boundary verdict is recorded as evidence; it only *escalates*
    # (never de-escalates) and is honored as a tier flip only when the
    # category is ALREADY ambiguous (EXPERIMENTAL — the "not yet
    # categorized" slot). This keeps the boundary gate a true
    # belt-and-suspenders without making every cage-resident flag SAFETY.
    category_is_safety = category_val in safety_names
    # Slice 96 review fix — FAIL-CLOSED tier default: STANDARD (the
    # auto-flippable tier) requires POSITIVE recognition as a known,
    # non-SAFETY category. None / "" / a forged or unregistered category
    # value is NOT in the known set → treated as SAFETY → routes to
    # APPROVAL_ADVISORY, never auto-flip. A malformed/partial spec can no
    # longer silently auto-activate a (possibly safety-relevant) capability.
    category_known = category_val in _known_category_names()
    boundary_crossed = _boundary_crosses(source_file)
    is_safety = (
        category_is_safety
        or (not category_known)
        or (boundary_crossed and category_val == "experimental")
    )
    tier = GraduationTier.SAFETY if is_safety else GraduationTier.STANDARD

    evidence: Dict[str, Any] = {
        "flag_name": flag,
        "source_file": source_file,
        "category": category_val,
        "category_known": category_known,
        "tier": tier.value,
        "tier_signal": (
            "category_safety" if category_is_safety
            else (
                "unknown_category_failclosed" if not category_known
                else (
                    "boundary_experimental" if is_safety
                    else "category_standard"
                )
            )
        ),
        "boundary_crossed": boundary_crossed,
        "progress": progress,
        "gate_a_eligible": True,
    }

    # ---- Gate B (AST stability) ----
    # Slice 96 review fix — fail-CLOSED on validator UNAVAILABLE.
    # drifted_files is None when shipped_code_invariants.validate_all()
    # itself raised: we cannot PROVE the subsystem's AST is stable, so we
    # refuse graduation rather than wave it through (the prior empty-set
    # swallow was a fail-OPEN hole).
    if drifted_files is None:
        evidence["hold_reason"] = HoldReason.AST_DRIFT.value
        evidence["gate_b_ast_clean"] = False
        evidence["gate_b_validator_unavailable"] = True
        return _decision(
            flag, tier, GraduationDisposition.HOLD,
            delta=(
                "Gate-B AST_DRIFT (fail-closed): "
                "shipped_code_invariants.validate_all() was UNAVAILABLE "
                "(raised) — cannot prove AST stability; refusing graduation"
            ),
            evidence=evidence,
        )
    if source_file and source_file in drifted_files:
        evidence["hold_reason"] = HoldReason.AST_DRIFT.value
        evidence["gate_b_ast_clean"] = False
        return _decision(
            flag, tier, GraduationDisposition.HOLD,
            delta=(
                f"Gate-B AST_DRIFT: shipped_code_invariants.validate_all() "
                f"reports a violation on {source_file} — subsystem AST "
                f"unstable; refusing graduation"
            ),
            evidence=evidence,
        )
    evidence["gate_b_ast_clean"] = True

    # ---- Gate C (contract, if any) ----
    contract = _contract_verdict_for_flag(flag)
    if contract is None:
        evidence["gate_c_contract"] = "vacuous"
    elif contract is False:
        evidence["gate_c_contract"] = "not_ready"
        evidence["hold_reason"] = HoldReason.CONTRACT_NOT_READY.value
        return _decision(
            flag, tier, GraduationDisposition.HOLD,
            delta=(
                f"Gate-C CONTRACT_NOT_READY: a graduation contract maps "
                f"to {flag} and its verdict is not READY — refusing "
                f"graduation"
            ),
            evidence=evidence,
        )
    else:
        evidence["gate_c_contract"] = "ready"

    # ---- All gates passed → READY ----
    if tier is GraduationTier.SAFETY:
        evidence["disposition_reason"] = (
            "SAFETY-tier (§1 zero-order-doll): auto-activating a "
            "governance/kill-switch capability is a governance self-"
            "modification — routes to operator approval, not auto-flip"
        )
        return _decision(
            flag, tier, GraduationDisposition.APPROVAL_ADVISORY,
            delta=(
                f"READY → APPROVAL_ADVISORY: {flag} met all gates but is "
                f"SAFETY-tier ({category_val}); operator must approve the "
                f"flip"
            ),
            evidence=evidence,
        )
    return _decision(
        flag, tier, GraduationDisposition.AUTO_FLIP,
        delta=(
            f"READY → AUTO_FLIP: {flag} met Gate-A (telemetry) + Gate-B "
            f"(AST) + Gate-C (contract) and is STANDARD-tier — autonomous "
            f"env-override authorized"
        ),
        evidence=evidence,
    )


def _decision(
    flag: str,
    tier: GraduationTier,
    disposition: GraduationDisposition,
    *,
    delta: str,
    evidence: Dict[str, Any],
) -> GraduationDecision:
    return GraduationDecision(
        flag_name=flag,
        tier=tier,
        disposition=disposition,
        evidence=evidence,
        delta=delta,
        evidence_sha256=_evidence_sha256(evidence),
    )


def _hold(
    flag: str,
    tier: GraduationTier,
    reason: HoldReason,
    *,
    delta: str,
    evidence: Dict[str, Any],
) -> GraduationDecision:
    ev = dict(evidence)
    ev["hold_reason"] = reason.value
    return _decision(flag, tier, GraduationDisposition.HOLD, delta=delta,
                     evidence=ev)


def _disabled_report(
    ts: float,
    ledger: Optional[Any],
    registry: Optional[Any],
) -> GraduationEngineReport:
    """Master-off shape: enumerate candidates but mark them DISABLED."""
    decisions: List[GraduationDecision] = []
    try:
        live = ledger if ledger is not None else _default_ledger()
        for flag in live.eligible_flags():
            ev = {"hold_reason": HoldReason.MASTER_OFF.value}
            decisions.append(GraduationDecision(
                flag_name=flag,
                tier=GraduationTier.STANDARD,
                disposition=GraduationDisposition.DISABLED,
                evidence=ev,
                delta="master_off: JARVIS_AUTONOMOUS_GRADUATION_ENGINE_"
                      "ENABLED is FALSE",
                evidence_sha256=_evidence_sha256(ev),
            ))
    except Exception:  # noqa: BLE001 — defensive
        pass
    return GraduationEngineReport(
        schema_version=AUTONOMOUS_GRADUATION_ENGINE_SCHEMA_VERSION,
        evaluated_at_unix=ts,
        decisions=tuple(decisions),
        auto_flipped=(), advisories=(), held=(),
    )


# ---------------------------------------------------------------------------
# execute_graduations — deliver receipts + advisories
# ---------------------------------------------------------------------------


def _maybe_propose_source_pr(decision: GraduationDecision) -> bool:
    """Sovereign Cognitive Crucible source-of-truth graduation (2026-06-20).

    When ``JARVIS_CRUCIBLE_GRADUATION_PR_ENABLED`` is on, an AUTO_FLIP decision
    ALSO fires the autonomous [SOVEREIGN GRADUATION] PR: it reads the flag's last
    3 CLEAN soaks' crucible evidence and, IF they clear the TTFT/AST veto, opens
    a cage-respecting PR that rewrites the source default literal (never merges).
    The shadow-gated override-ledger receipt (the .env audit path) is unaffected.

    Best-effort, fail-soft, gate-default-off. Returns True iff a PR proposal was
    SCHEDULED. NEVER raises."""
    try:
        from backend.core.ouroboros.governance.graduation.graduation_pr_proposer import (  # noqa: E501
            graduation_pr_enabled,
            propose_graduation_pr,
        )
        if not graduation_pr_enabled():
            return False
        from backend.core.ouroboros.governance.graduation.live_fire_soak import (
            recent_clean_crucible_evidence,
        )
        from backend.core.ouroboros.governance.graduation.telemetry_manifest import (  # noqa: E501
            manifest_recommends_merge,
        )
        from backend.core.ouroboros.governance.graduation import (
            crucible_verdict as _cv,
        )
        flag = decision.flag_name
        evidence = recent_clean_crucible_evidence(flag, limit=3)
        required = 3
        try:
            from backend.core.ouroboros.governance.adaptation.graduation_ledger import (  # noqa: E501
                CADENCE_POLICY,
            )
            for entry in CADENCE_POLICY:
                if getattr(entry, "flag_name", None) == flag:
                    required = int(getattr(entry, "required_clean_sessions", 3))
                    break
        except Exception:  # noqa: BLE001
            pass
        if not manifest_recommends_merge(evidence, required_clean=required):
            return False
        ceiling = _cv._env_float(
            _cv._TTFT_CEILING_ENV, _cv._DEFAULT_TTFT_CEILING_MS,
        )
        session_ids = [str(e.get("session_id", "")) for e in evidence]
        repo_root = os.environ.get("JARVIS_AUTO_COMMIT_WORKSPACE") or os.getcwd()

        async def _go() -> None:
            try:
                _res = await propose_graduation_pr(
                    flag,
                    soak_evidence=evidence,
                    session_ids=session_ids,
                    required_clean=required,
                    ttft_ceiling_ms=ceiling,
                    repo_root=repo_root,
                )
                # Sovereign Ephemeral Self-Termination Matrix (2026-06-21):
                # the instant a graduation PR opens, the ephemeral crucible
                # node flushes state to GCS and severs its own compute. Gated
                # default-OFF (only the ephemeral overlay opts in); fires ONLY
                # on a genuine pr_url; fail-soft — NEVER undoes the graduation.
                if (
                    _res is not None
                    and getattr(_res, "proposed", False)
                    and getattr(_res, "pr_url", None)
                ):
                    try:
                        from backend.core.ouroboros.governance.sovereign_self_termination import (  # noqa: E501
                            trigger_self_termination,
                        )
                        await asyncio.to_thread(
                            trigger_self_termination, _res.pr_url,
                        )
                    except Exception:  # noqa: BLE001
                        pass
            except Exception as exc:  # noqa: BLE001
                logger.debug("[GraduationEngine] PR proposal failed: %s", exc)

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(_go())
        except RuntimeError:
            asyncio.run(_go())
        return True
    except Exception as exc:  # noqa: BLE001 — fail-soft; never block graduation
        logger.debug("[GraduationEngine] _maybe_propose_source_pr: %s", exc)
        return False


def execute_graduations(
    report: GraduationEngineReport,
    *,
    now_unix: Optional[float] = None,
) -> ExecutionResult:
    """Deliver the report. AUTO_FLIP → immutable override receipt
    (the override ledger structurally refuses non-STANDARD tiers).
    APPROVAL_ADVISORY → a receipt-backed advisory JSONL. NEVER writes
    source; NEVER auto-flips a SAFETY flag. NEVER raises."""
    recorded: List[str] = []
    advised: List[str] = []
    try:
        from backend.core.ouroboros.governance import (
            graduation_override_ledger as override_ledger,
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("[GraduationEngine] override ledger unavailable: %s", exc)
        override_ledger = None  # type: ignore

    for decision in report.decisions:
        try:
            if decision.disposition is GraduationDisposition.AUTO_FLIP:
                if override_ledger is None:
                    continue
                # The override ledger STRUCTURALLY refuses any non-
                # STANDARD tier — a SAFETY flag can never be recorded
                # even if a bug mislabeled its disposition.
                if override_ledger.record_graduation(
                    decision, now_unix=now_unix,
                ):
                    recorded.append(decision.flag_name)
                # Sovereign Cognitive Crucible: also fire the source-of-truth
                # [SOVEREIGN GRADUATION] PR (gated, fail-soft, cage-respecting —
                # proposes, never merges). Independent of the shadow-gated
                # override receipt above.
                _maybe_propose_source_pr(decision)
            elif (
                decision.disposition
                is GraduationDisposition.APPROVAL_ADVISORY
            ):
                if _emit_advisory(decision, now_unix=now_unix):
                    advised.append(decision.flag_name)
        except Exception as exc:  # noqa: BLE001 — best-effort per decision
            logger.debug(
                "[GraduationEngine] execute %s failed: %s",
                decision.flag_name, exc,
            )
            continue
    return ExecutionResult(
        recorded_overrides=tuple(recorded),
        advisories_emitted=tuple(advised),
    )


def _emit_advisory(
    decision: GraduationDecision,
    *,
    now_unix: Optional[float] = None,
) -> bool:
    """Append a receipt-backed advisory for a SAFETY-tier graduation.
    Dedicated advisory JSONL (lowest coupling). NEVER raises."""
    try:
        from pathlib import Path

        ts = float(now_unix) if now_unix is not None else time.time()
        record = {
            "schema_version": AUTONOMOUS_GRADUATION_ENGINE_SCHEMA_VERSION,
            "flag_name": decision.flag_name,
            "tier": decision.tier.value,
            "disposition": decision.disposition.value,
            "requires_operator_approval": True,
            "evidence": decision.evidence,
            "evidence_sha256": decision.evidence_sha256,
            "delta": decision.delta,
            "advised_at_unix": ts,
            "authorized_by": "autonomous_graduation_engine",
        }
        try:
            line = json.dumps(record, separators=(",", ":"))
        except (TypeError, ValueError):
            return False
        path = Path(advisory_ledger_path())
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            return False
        try:
            from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
                flock_append_line,
            )
            return bool(flock_append_line(path, line))
        except ImportError:
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
                f.write("\n")
            return True
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.debug("[GraduationEngine] advisory emit failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Auto-discovered §33.1 registrars
# ---------------------------------------------------------------------------


def register_flags(registry: Any) -> int:  # noqa: ANN001
    """Register this engine's env knobs with the FlagRegistry.
    Auto-discovered via ``flag_registry_seed`` module-discovery."""
    if registry is None:
        return 0
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/"
        "autonomous_graduation_engine.py"
    )
    seeds = [
        FlagSpec(
            name="JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED",
            type=FlagType.BOOL,
            default=False,
            description=(
                "Master switch for the Autonomous Telemetry-Driven "
                "Graduation Engine (§40 / Slice 96). Default-FALSE per "
                "§33.1. When on, the engine evaluates eligible §33.1 "
                "flags and auto-flips STANDARD-tier ones via the durable "
                "env-override ledger; SAFETY-tier flags route to operator "
                "advisory only."
            ),
            category=Category.SAFETY,
            source_file=src,
            example="true",
        ),
        FlagSpec(
            name="JARVIS_GRADUATION_OVERRIDE_APPLY_ENABLED",
            type=FlagType.BOOL,
            default=False,
            description=(
                "Boot-time applier sub-gate. When on (or the engine "
                "master is on), apply_overrides_to_environ injects "
                "previously-authorized STANDARD-tier graduations into "
                "os.environ — honoring operator env-precedence. "
                "Default-FALSE: dormant-by-default."
            ),
            category=Category.SAFETY,
            source_file=(
                "backend/core/ouroboros/governance/"
                "graduation_override_ledger.py"
            ),
            example="true",
        ),
    ]
    n = 0
    for spec in seeds:
        try:
            registry.register(spec)
            n += 1
        except Exception:  # noqa: BLE001 — fail-open per §33.1
            continue
    return n


def register_shipped_invariants() -> list:
    """Auto-discovered AST pins for this module. NEVER raises.

    Pins:
      1. tier + disposition enums closed (no smuggled members).
      2. authority-asymmetry — no orchestrator/iron_gate/policy/
         providers/candidate_generator/urgency_router/change_engine/
         semantic_guardian/auto_committer/risk_tier imports.
      3. master default-FALSE (no unconditional `return True`).
      4. composes-canonical — references graduation_ledger +
         flag_registry + shipped_code_invariants +
         unified_graduation_dashboard.
    """
    import ast

    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/"
        "autonomous_graduation_engine.py"
    )

    def _validate_enums_closed(tree, source):  # noqa: ANN001
        violations: list = []
        expected = {
            "GraduationTier": {"STANDARD", "SAFETY"},
            "GraduationDisposition": {
                "AUTO_FLIP", "APPROVAL_ADVISORY", "HOLD", "DISABLED",
            },
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name not in expected:
                continue
            seen: set = set()
            for stmt in node.body:
                if isinstance(stmt, ast.Assign):
                    for tgt in stmt.targets:
                        if isinstance(tgt, ast.Name):
                            seen.add(tgt.id)
            req = expected[node.name]
            missing = req - seen
            extras = seen - req
            if missing:
                violations.append(
                    f"{node.name} missing members: {sorted(missing)}"
                )
            if extras:
                violations.append(
                    f"{node.name} has extra members (closed-taxonomy "
                    f"violation): {sorted(extras)}"
                )
        return tuple(violations)

    def _validate_authority_asymmetry(tree, source):  # noqa: ANN001
        forbidden = (
            "orchestrator", "iron_gate", "policy", "providers",
            "candidate_generator", "urgency_router", "change_engine",
            "semantic_guardian", "auto_committer", "risk_tier_floor",
        )
        violations: list = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in forbidden:
                    if f in module:
                        violations.append(
                            f"forbidden authority import: {module}"
                        )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    for f in forbidden:
                        if f in alias.name:
                            violations.append(
                                f"forbidden authority import: {alias.name}"
                            )
        return tuple(violations)

    def _validate_master_default_false(tree, source):  # noqa: ANN001
        violations: list = []
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "autonomous_graduation_engine_enabled"
            ):
                func_src = ast.unparse(node)
                if "return True" in func_src:
                    violations.append(
                        "autonomous_graduation_engine_enabled MUST NOT "
                        "unconditionally return True (master default-FALSE "
                        "per §33.1)"
                    )
                key = "JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED"
                if key not in func_src:
                    violations.append(
                        f"master gate MUST read {key!r}"
                    )
        return tuple(violations)

    def _validate_composes_canonical(tree, source):  # noqa: ANN001
        required = (
            "graduation_ledger",
            "flag_registry",
            "shipped_code_invariants",
            "unified_graduation_dashboard",
        )
        violations: list = []
        for token in required:
            if token not in source:
                violations.append(
                    f"engine MUST compose canonical substrate {token!r} "
                    f"(no reimplemented verdict/eligibility/AST logic)"
                )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name="autonomous_graduation_engine_enums_closed",
            target_file=target,
            description=(
                "GraduationTier (STANDARD/SAFETY) + GraduationDisposition "
                "(AUTO_FLIP/APPROVAL_ADVISORY/HOLD/DISABLED) are closed "
                "taxonomies — no smuggled member can widen the tiered "
                "autonomy boundary."
            ),
            validate=_validate_enums_closed,
        ),
        ShippedCodeInvariant(
            invariant_name="autonomous_graduation_engine_authority_asymmetry",
            target_file=target,
            description=(
                "Engine MUST stay pure substrate composing graduation "
                "ledger + registry + shipped invariants + dashboard. NEVER "
                "imports orchestrator / iron_gate / policy / providers / "
                "candidate_generator / urgency_router / change_engine / "
                "semantic_guardian / auto_committer / risk_tier_floor."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name="autonomous_graduation_engine_master_default_false",
            target_file=target,
            description=(
                "Master flag JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED "
                "stays default-FALSE per §33.1."
            ),
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name="autonomous_graduation_engine_composes_canonical",
            target_file=target,
            description=(
                "Engine composes the canonical graduation_ledger + "
                "flag_registry + shipped_code_invariants + "
                "unified_graduation_dashboard substrates — no parallel "
                "eligibility / AST / verdict reasoning."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


__all__ = [
    "AUTONOMOUS_GRADUATION_ENGINE_SCHEMA_VERSION",
    "ExecutionResult",
    "GraduationDecision",
    "GraduationDisposition",
    "GraduationEngineReport",
    "GraduationTier",
    "HoldReason",
    "autonomous_graduation_engine_enabled",
    "advisory_ledger_path",
    "evaluate_graduations",
    "execute_graduations",
    "register_flags",
    "register_shipped_invariants",
]

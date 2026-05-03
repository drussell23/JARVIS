#!/usr/bin/env python3
"""Empirical-closure verdict for the Production Oracle → auto_action_router
VERIFY-consumer wiring (Tier 2 #6 follow-up Arc 1).

Five primary contracts (in-process; no soak required):

  C1 — AutoActionContext accepts the new recent_oracle_observation
       field and the existing fields stay backwards-compatible.
  C2 — Oracle veto rule fires correctly for FAILED + DEGRADED + HEALTHY:
        * FAILED + SAFE_AUTO  -> DEMOTE_RISK_TIER (proposed=NOTIFY_APPLY)
        * FAILED + NOTIFY_APPLY -> ROUTE_TO_NOTIFY_APPLY
        * DEGRADED + has op_family -> RAISE_EXPLORATION_FLOOR
        * HEALTHY -> falls through to existing rules (NO_ACTION)
  C3 — Master env knob JARVIS_AUTO_ACTION_ORACLE_VETO_ENABLED defaults
       True post-graduation; explicit "false" silences the rule.
  C4 — gather_context(include_oracle=True) reads the production oracle
       observer's most-recent observation and projects into the context;
       include_oracle=False leaves recent_oracle_observation as None.
  C5 — Orchestrator VERIFY hook is structurally present + the SSE
       publisher publish_auto_action_proposal is importable.

Exit codes:
    0 = all five primary contracts PASSED
    1 = at least one primary contract FAILED
"""
from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@dataclass(frozen=True)
class ContractVerdict:
    name: str
    passed: bool
    evidence: str
    details: Dict[str, object] = field(default_factory=dict)


def _eval_context_shape() -> ContractVerdict:
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionContext, RecentOracleObservation,
    )
    obs = RecentOracleObservation(
        aggregate_verdict="failed", observed_at_ts=1.0,
        signal_count=3, adapters_queried=2, adapters_failed=0,
        posture="HARDEN",
    )
    ctx_with = AutoActionContext(
        recent_oracle_observation=obs,
        current_op_family="test",
    )
    ctx_without = AutoActionContext(current_op_family="test")
    backwards_ok = (
        ctx_without.recent_oracle_observation is None
        and ctx_with.recent_oracle_observation is obs
    )
    return ContractVerdict(
        name="C1 AutoActionContext accepts oracle slot (backwards-compat)",
        passed=backwards_ok,
        evidence=(
            f"with_obs.observation={ctx_with.recent_oracle_observation is obs} "
            f"without_obs.observation_is_None={ctx_without.recent_oracle_observation is None}"
        ),
    )


def _eval_decision_precedence() -> ContractVerdict:
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionContext, RecentOracleObservation,
        propose_advisory_action, AdvisoryActionType,
    )
    cases: List[str] = []
    failures: List[str] = []

    def _check(label, ctx, expected):
        action = propose_advisory_action(ctx)
        if action.action_type is expected:
            cases.append(f"{label}->{action.action_type.value}")
        else:
            failures.append(
                f"{label}: expected {expected.value} got "
                f"{action.action_type.value}"
            )

    failed_obs = RecentOracleObservation(
        aggregate_verdict="failed", observed_at_ts=1.0,
        signal_count=3, adapters_queried=2, adapters_failed=0,
    )
    degraded_obs = RecentOracleObservation(
        aggregate_verdict="degraded", observed_at_ts=1.0,
        signal_count=3, adapters_queried=2, adapters_failed=0,
    )
    healthy_obs = RecentOracleObservation(
        aggregate_verdict="healthy", observed_at_ts=1.0,
        signal_count=3, adapters_queried=2, adapters_failed=0,
    )
    _check(
        "FAILED+safe_auto",
        AutoActionContext(
            current_op_family="t", current_risk_tier="safe_auto",
            current_route="standard", recent_oracle_observation=failed_obs,
        ),
        AdvisoryActionType.DEMOTE_RISK_TIER,
    )
    _check(
        "FAILED+notify_apply",
        AutoActionContext(
            current_op_family="t", current_risk_tier="notify_apply",
            current_route="standard", recent_oracle_observation=failed_obs,
        ),
        AdvisoryActionType.ROUTE_TO_NOTIFY_APPLY,
    )
    _check(
        "DEGRADED+family",
        AutoActionContext(
            current_op_family="t", current_risk_tier="safe_auto",
            current_route="standard",
            recent_oracle_observation=degraded_obs,
        ),
        AdvisoryActionType.RAISE_EXPLORATION_FLOOR,
    )
    _check(
        "HEALTHY",
        AutoActionContext(
            current_op_family="t", current_risk_tier="safe_auto",
            current_route="standard",
            recent_oracle_observation=healthy_obs,
        ),
        AdvisoryActionType.NO_ACTION,
    )
    return ContractVerdict(
        name="C2 Oracle veto rule produces correct AdvisoryActionType",
        passed=not failures,
        evidence=(
            f"cases=[{', '.join(cases)}]"
            + (f" failures={failures}" if failures else "")
        ),
    )


def _eval_master_default() -> ContractVerdict:
    from backend.core.ouroboros.governance.auto_action_router import (
        auto_action_oracle_veto_enabled,
        AutoActionContext, RecentOracleObservation,
        propose_advisory_action, AdvisoryActionType,
    )
    os.environ.pop("JARVIS_AUTO_ACTION_ORACLE_VETO_ENABLED", None)
    default_on = auto_action_oracle_veto_enabled()
    os.environ["JARVIS_AUTO_ACTION_ORACLE_VETO_ENABLED"] = "false"
    explicit_off = auto_action_oracle_veto_enabled()
    # When off, the rule is skipped even if the obs is FAILED.
    failed_obs = RecentOracleObservation(
        aggregate_verdict="failed", observed_at_ts=1.0,
        signal_count=3, adapters_queried=2, adapters_failed=0,
    )
    ctx = AutoActionContext(
        current_op_family="t", current_risk_tier="safe_auto",
        current_route="standard", recent_oracle_observation=failed_obs,
    )
    action_when_off = propose_advisory_action(ctx)
    os.environ.pop("JARVIS_AUTO_ACTION_ORACLE_VETO_ENABLED", None)
    return ContractVerdict(
        name="C3 Master env knob default-true; explicit false silences rule",
        passed=(
            default_on is True
            and explicit_off is False
            and action_when_off.action_type is AdvisoryActionType.NO_ACTION
        ),
        evidence=(
            f"default_on={default_on} explicit_off={explicit_off} "
            f"action_when_off={action_when_off.action_type.value}"
        ),
    )


def _eval_gather_context() -> ContractVerdict:
    from backend.core.ouroboros.governance.auto_action_router import (
        gather_context,
    )
    from backend.core.ouroboros.governance.production_oracle_observer import (  # noqa: E501
        get_default_observer, reset_default_observer,
    )
    # Force a tick so the observer has an observation to project.
    reset_default_observer()
    obs = get_default_observer(project_root=REPO_ROOT)
    asyncio.run(obs.tick_once(posture="HARDEN"))
    ctx_with = gather_context(
        current_op_family="test", current_risk_tier="safe_auto",
        current_route="standard", include_oracle=True,
    )
    ctx_without = gather_context(
        current_op_family="test", current_risk_tier="safe_auto",
        current_route="standard", include_oracle=False,
    )
    return ContractVerdict(
        name="C4 gather_context populates oracle when include_oracle=True",
        passed=(
            ctx_with.recent_oracle_observation is not None
            and ctx_without.recent_oracle_observation is None
        ),
        evidence=(
            f"with_oracle.verdict="
            f"{getattr(ctx_with.recent_oracle_observation, 'aggregate_verdict', None)} "
            f"without_oracle={ctx_without.recent_oracle_observation}"
        ),
    )


def _eval_orchestrator_hook_present() -> ContractVerdict:
    """Static check: verify the VERIFY hook block exists in
    orchestrator.py source AND the SSE publisher is importable."""
    failures: List[str] = []
    try:
        orch_src = (
            REPO_ROOT
            / "backend/core/ouroboros/governance/orchestrator.py"
        ).read_text(encoding="utf-8")
    except Exception as exc:
        return ContractVerdict(
            name="C5 Orchestrator VERIFY hook + SSE publisher present",
            passed=False,
            evidence=f"orchestrator.py read failed: {exc!r}",
        )
    hook_marker = "JARVIS_AUTO_ACTION_VERIFY_HOOK_ENABLED"
    propose_call = "_aa_propose(_aa_ctx)"
    publish_call = "publish_auto_action_proposal"
    if hook_marker not in orch_src:
        failures.append("orchestrator.py missing VERIFY hook env knob")
    if propose_call not in orch_src:
        failures.append(
            "orchestrator.py missing propose_advisory_action call"
        )
    if publish_call not in orch_src:
        failures.append(
            "orchestrator.py missing publish_auto_action_proposal call"
        )
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_AUTO_ACTION_PROPOSAL,
            publish_auto_action_proposal,
        )
        sse_event_ok = (
            EVENT_TYPE_AUTO_ACTION_PROPOSAL == "auto_action_proposal"
        )
    except Exception as exc:
        failures.append(f"SSE import failed: {exc!r}")
        sse_event_ok = False
    return ContractVerdict(
        name="C5 Orchestrator VERIFY hook + SSE publisher present",
        passed=not failures,
        evidence=(
            f"hook_marker={hook_marker in orch_src} "
            f"propose_call={propose_call in orch_src} "
            f"publish_call={publish_call in orch_src} "
            f"sse_event_ok={sse_event_ok}"
            + (f" failures={failures}" if failures else "")
        ),
    )


def main() -> int:
    print("Empirical-closure verdict for Production Oracle → "
          "auto_action_router VERIFY wiring (Arc 1)")
    print(f"  repo_root: {REPO_ROOT}")
    print()
    primary = [
        _eval_context_shape(),
        _eval_decision_precedence(),
        _eval_master_default(),
        _eval_gather_context(),
        _eval_orchestrator_hook_present(),
    ]
    for v in primary:
        mark = "PASS" if v.passed else "FAIL"
        print(f"  [{mark}] {v.name}")
        print(f"         {v.evidence}")
    print()
    if all(v.passed for v in primary):
        print("VERDICT: Arc 1 EMPIRICALLY CLOSED -- all five primary "
              "contracts PASSED. Production Oracle now drives "
              "auto_action_router proposals at VERIFY phase.")
        return 0
    print("VERDICT: at least one primary contract FAILED -- "
          "Arc 1 not yet empirically closed.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

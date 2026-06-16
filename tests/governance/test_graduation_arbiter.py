"""Unit B — Sovereign Arbiter Protocol + dual-signature predicates.

The GraduationContract becomes the definitive Arbiter over two independent
assessment streams (legacy classify_outcome + harvester Metrics), resolving
conflicts with a deterministic priority matrix:

  P1 (highest) anomaly guard      : oom / gate_inert  -> never CLEAN
  P2           recovery override  : legacy error + full self-heal -> CLEAN
  P3           metrics predicate  : CLEAN but contract says not really -> RUNNER
  P4 (lowest)  blocklist override : RUNNER -> INFRA waiver

Plus dual-signature clean_predicate dispatch: (summary) OR (summary, metrics).

TDD red: these are written before arbitrate()/Metrics-aware is_clean() exist.
"""
from __future__ import annotations

from backend.core.ouroboros.governance.graduation.graduation_contract import (
    GraduationContract,
)
from backend.core.ouroboros.governance.graduation.telemetry_parse import (
    Metrics,
)

_CLEAN_SUMMARY = {
    "session_outcome": "complete",
    "stop_reason": "wall_clock_cap",
    "failure_class_counts": {},
}


def _full_recovery_metrics() -> Metrics:
    return Metrics(
        booted=True,
        livefire_fired=["ImportError"],
        routed_build=True,
        retried=True,
        recovered=True,
        oom=False,
        session_outcome="complete",
    )


# --- P2: autonomous recovery override --------------------------------------
def test_recovery_override_promotes_legacy_infra_to_clean():
    contract = GraduationContract(flag_name="X")
    outcome, ra, notes = contract.arbitrate(
        legacy_outcome="infra",
        runner_attributed=False,
        class_notes="api_timeout",
        summary=_CLEAN_SUMMARY,
        metrics=_full_recovery_metrics(),
    )
    assert outcome == "clean"
    assert ra is False
    assert "arbiter_recovery_override" in notes


def test_recovery_override_requires_full_trajectory():
    # Missing routed_build -> NOT a proven recovery -> legacy stands.
    m = _full_recovery_metrics()
    m.routed_build = False
    contract = GraduationContract(flag_name="X")
    outcome, ra, notes = contract.arbitrate(
        legacy_outcome="runner",
        runner_attributed=True,
        class_notes="",
        summary=_CLEAN_SUMMARY,
        metrics=m,
    )
    assert outcome == "runner"
    assert "arbiter_recovery_override" not in notes


# --- P1: anomaly guard (dominates) -----------------------------------------
def test_anomaly_guard_oom_blocks_clean():
    m = Metrics(recovered=True, oom=True, session_outcome="complete")
    contract = GraduationContract(flag_name="X")
    outcome, ra, notes = contract.arbitrate(
        legacy_outcome="clean",
        runner_attributed=False,
        class_notes="",
        summary=_CLEAN_SUMMARY,
        metrics=m,
    )
    assert outcome == "infra"  # waiver — hardware fault, not feature fault
    assert ra is False
    assert "arbiter_anomaly_oom" in notes


def test_anomaly_guard_dominates_recovery_override():
    # Full recovery trajectory BUT gate_inert (stale wiring) -> anomaly wins.
    m = _full_recovery_metrics()
    m.gate_inert = True
    contract = GraduationContract(flag_name="X")
    outcome, ra, notes = contract.arbitrate(
        legacy_outcome="infra",
        runner_attributed=False,
        class_notes="",
        summary=_CLEAN_SUMMARY,
        metrics=m,
    )
    assert outcome == "infra"
    assert "arbiter_anomaly_gate_inert" in notes


# --- P3: metrics-aware predicate downgrade ---------------------------------
def test_metrics_aware_predicate_downgrades_clean():
    def needs_recovery(summary, metrics) -> bool:  # 2-arg predicate
        return bool(getattr(metrics, "recovered", False))

    contract = GraduationContract(
        flag_name="X", clean_predicate=needs_recovery,
    )
    m = Metrics(recovered=False, session_outcome="complete")
    outcome, ra, notes = contract.arbitrate(
        legacy_outcome="clean",
        runner_attributed=False,
        class_notes="",
        summary=_CLEAN_SUMMARY,
        metrics=m,
    )
    assert outcome == "runner"
    assert ra is True
    assert "contract_metrics_predicate_downgraded" in notes


# --- P4: legacy blocklist override preserved -------------------------------
def test_legacy_blocklist_override_preserved():
    contract = GraduationContract(
        flag_name="X",
        failure_class_blocklist_overrides=frozenset({"dw_provider_timeout"}),
    )
    summary = {
        "session_outcome": "incomplete_kill",
        "failure_class_counts": {"dw_provider_timeout": 2},
    }
    m = Metrics(session_outcome="incomplete_kill")
    outcome, ra, notes = contract.arbitrate(
        legacy_outcome="runner",
        runner_attributed=True,
        class_notes="",
        summary=summary,
        metrics=m,
    )
    assert outcome == "infra"
    assert ra is False
    assert "contract_blocklist_upgraded_runner_to_infra" in notes


# --- dual-signature predicate dispatch -------------------------------------
def test_is_clean_dispatches_one_arg_predicate():
    seen = {}

    def legacy_pred(summary) -> bool:  # 1-arg
        seen["n"] = 1
        return summary.get("session_outcome") == "complete"

    contract = GraduationContract(flag_name="X", clean_predicate=legacy_pred)
    assert contract.is_clean(_CLEAN_SUMMARY, _full_recovery_metrics()) is True
    assert seen["n"] == 1  # called with one arg, metrics ignored safely


def test_is_clean_dispatches_two_arg_predicate_with_metrics():
    captured = {}

    def metrics_pred(summary, metrics) -> bool:  # 2-arg
        captured["metrics"] = metrics
        return bool(getattr(metrics, "oom", False)) is False

    contract = GraduationContract(flag_name="X", clean_predicate=metrics_pred)
    m = _full_recovery_metrics()
    assert contract.is_clean(_CLEAN_SUMMARY, m) is True
    assert captured["metrics"] is m


def test_is_clean_two_arg_predicate_with_no_metrics_passes_none():
    def metrics_pred(summary, metrics) -> bool:
        return metrics is None  # called without metrics -> None

    contract = GraduationContract(flag_name="X", clean_predicate=metrics_pred)
    assert contract.is_clean(_CLEAN_SUMMARY) is True


# --- robustness ------------------------------------------------------------
def test_arbitrate_never_raises_on_none_metrics():
    contract = GraduationContract(flag_name="X")
    outcome, ra, notes = contract.arbitrate(
        legacy_outcome="clean",
        runner_attributed=False,
        class_notes="",
        summary=_CLEAN_SUMMARY,
        metrics=None,
    )
    assert outcome == "clean"  # no metrics -> legacy stands


def test_arbitrate_clean_stays_clean_when_all_good():
    contract = GraduationContract(flag_name="X")
    outcome, ra, notes = contract.arbitrate(
        legacy_outcome="clean",
        runner_attributed=False,
        class_notes="ok",
        summary=_CLEAN_SUMMARY,
        metrics=_full_recovery_metrics(),
    )
    assert outcome == "clean"
    assert ra is False


# --- Capstone Dogfood Contract: JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED --------
def test_capstone_contract_registered_for_live_kernel_validator():
    from backend.core.ouroboros.governance.graduation.graduation_contract import (  # noqa: E501
        get_contract,
        has_custom_contract,
    )
    flag = "JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED"
    assert has_custom_contract(flag)
    contract = get_contract(flag)
    assert contract.flag_name == flag
    assert contract.clean_predicate is not None


def test_capstone_clean_requires_livefire_fired_no_oom_and_recovered():
    from backend.core.ouroboros.governance.graduation.graduation_contract import (  # noqa: E501
        get_contract,
    )
    contract = get_contract("JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED")

    # Full evidence: validator fired, no OOM, candidate recovered -> CLEAN.
    good = _full_recovery_metrics()
    assert contract.is_clean(_CLEAN_SUMMARY, good) is True

    # Validator never fired -> NOT clean (deployment proven, not self-heal).
    never_fired = Metrics(recovered=True, session_outcome="complete")
    assert contract.is_clean(_CLEAN_SUMMARY, never_fired) is False

    # OOM anomaly -> NOT clean.
    oomed = _full_recovery_metrics()
    oomed.oom = True
    assert contract.is_clean(_CLEAN_SUMMARY, oomed) is False

    # Fired but never recovered -> NOT clean.
    no_recover = _full_recovery_metrics()
    no_recover.recovered = False
    assert contract.is_clean(_CLEAN_SUMMARY, no_recover) is False


def test_capstone_arbitrate_downgrades_clean_without_evidence():
    from backend.core.ouroboros.governance.graduation.graduation_contract import (  # noqa: E501
        get_contract,
    )
    contract = get_contract("JARVIS_LIVE_KERNEL_VALIDATOR_ENABLED")
    # Legacy said clean, but the validator never fired -> arbiter downgrades.
    m = Metrics(recovered=True, session_outcome="complete")  # no livefire
    outcome, ra, notes = contract.arbitrate(
        legacy_outcome="clean",
        runner_attributed=False,
        class_notes="",
        summary=_CLEAN_SUMMARY,
        metrics=m,
    )
    assert outcome == "runner"
    assert "contract_metrics_predicate_downgraded" in notes

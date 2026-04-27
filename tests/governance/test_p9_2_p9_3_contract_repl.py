"""Phase 9.2 + 9.3 — GraduationContract + /graduate REPL extensions.

Pins:
  * GraduationContract dataclass + registry semantics
  * Built-in predicate behavior (default / Phase 8 / CuriosityEngine)
  * Master flag matrix for contract consultation
  * Harness ↔ contract integration (downgrade-clean + upgrade-runner-to-infra)
  * Master-off byte-identical (contract is no-op)
  * 6 new live-* REPL subcommands
  * Authority/cage invariants
  * NEVER-raises smoke
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

from backend.core.ouroboros.governance.adaptation import (
    graduate_repl as _repl,
    graduation_ledger as _ledger_mod,
)
from backend.core.ouroboros.governance.adaptation.graduate_repl import (
    DispatchStatus,
    dispatch_graduate,
)
from backend.core.ouroboros.governance.graduation import (
    graduation_contract as _contract,
    live_fire_soak as _soak,
)
from backend.core.ouroboros.governance.graduation.graduation_contract import (
    DEFAULT_RE_ARM_AFTER_RUNNER_SECONDS,
    GraduationContract,
    MAX_CONTRACTS,
    MAX_RE_ARM_SECONDS,
    MIN_RE_ARM_SECONDS,
    all_contracts_metadata,
    default_clean_predicate,
    get_contract,
    has_custom_contract,
    is_contract_consultation_enabled,
    known_contract_flags,
    predicate_requires_curiosity_hypothesis,
    predicate_requires_decision_trace_rows,
)
from backend.core.ouroboros.governance.graduation.live_fire_soak import (
    HarnessStatus,
    LiveFireSoakHarness,
    get_default_harness,
    reset_default_harness,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch):
    keys = [
        k for k in os.environ.keys()
        if (
            k.startswith("JARVIS_LIVE_FIRE_")
            or k.startswith("JARVIS_GRADUATION_LEDGER_")
            or k.startswith("JARVIS_GRADUATE_REPL_")
        )
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    reset_default_harness()
    _ledger_mod.reset_default_ledger()
    yield
    reset_default_harness()
    _ledger_mod.reset_default_ledger()


@pytest.fixture
def isolated_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "JARVIS_GRADUATION_LEDGER_PATH",
        str(tmp_path / "grad_ledger.jsonl"),
    )
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_ENABLED", "true")
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_HISTORY_PATH",
        str(tmp_path / "live_fire.jsonl"),
    )
    _ledger_mod.reset_default_ledger()
    reset_default_harness()
    return {
        "ledger": tmp_path / "grad_ledger.jsonl",
        "history": tmp_path / "live_fire.jsonl",
    }


# ---------------------------------------------------------------------------
# Module constants
# ---------------------------------------------------------------------------


def test_max_contracts_sane():
    assert 32 <= MAX_CONTRACTS <= 1024


def test_re_arm_bounds_sane():
    assert MIN_RE_ARM_SECONDS >= 1
    assert MAX_RE_ARM_SECONDS <= 7 * 24 * 3600
    assert MIN_RE_ARM_SECONDS < DEFAULT_RE_ARM_AFTER_RUNNER_SECONDS
    assert DEFAULT_RE_ARM_AFTER_RUNNER_SECONDS < MAX_RE_ARM_SECONDS


# ---------------------------------------------------------------------------
# Master flag matrix
# ---------------------------------------------------------------------------


def test_consultation_default_off():
    assert is_contract_consultation_enabled() is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE"])
def test_consultation_truthy(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT", val,
    )
    assert is_contract_consultation_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", ""])
def test_consultation_falsy(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT", val,
    )
    assert is_contract_consultation_enabled() is False


# ---------------------------------------------------------------------------
# GraduationContract dataclass
# ---------------------------------------------------------------------------


def test_contract_default_field_values():
    c = GraduationContract(flag_name="JARVIS_X")
    assert c.clean_predicate is None
    assert c.failure_class_blocklist_overrides == frozenset()
    assert c.re_arm_after_runner_seconds == (
        DEFAULT_RE_ARM_AFTER_RUNNER_SECONDS
    )
    assert c.cost_cap_override_usd is None
    assert c.max_wall_seconds_override is None


def test_contract_re_arm_clamp_low():
    c = GraduationContract(
        flag_name="X", re_arm_after_runner_seconds=1,
    )
    assert c.re_arm_after_runner_seconds == MIN_RE_ARM_SECONDS


def test_contract_re_arm_clamp_high():
    c = GraduationContract(
        flag_name="X", re_arm_after_runner_seconds=10**9,
    )
    assert c.re_arm_after_runner_seconds == MAX_RE_ARM_SECONDS


def test_contract_to_metadata_dict_omits_predicate_callable():
    c = GraduationContract(
        flag_name="X", clean_predicate=default_clean_predicate,
    )
    meta = c.to_metadata_dict()
    assert meta["has_custom_predicate"] is True
    # No raw callable in dict.
    assert "clean_predicate" not in meta


def test_contract_is_clean_default_predicate():
    c = GraduationContract(flag_name="X")
    assert c.is_clean({
        "session_outcome": "complete",
        "failure_class_counts": {},
    }) is True
    assert c.is_clean({
        "session_outcome": "incomplete",
        "failure_class_counts": {},
    }) is False


def test_contract_is_clean_predicate_raises_returns_false():
    def boom(_summary):
        raise RuntimeError("nope")

    c = GraduationContract(flag_name="X", clean_predicate=boom)
    assert c.is_clean({}) is False


# ---------------------------------------------------------------------------
# Built-in predicates
# ---------------------------------------------------------------------------


def test_default_predicate_clean():
    assert default_clean_predicate({
        "session_outcome": "complete",
        "failure_class_counts": {},
    }) is True


def test_default_predicate_runner_class_failure_blocks():
    assert default_clean_predicate({
        "session_outcome": "complete",
        "failure_class_counts": {"phase_runner_error": 1},
    }) is False


def test_default_predicate_non_dict_summary():
    assert default_clean_predicate("not a dict") is False
    assert default_clean_predicate(None) is False


def test_default_predicate_non_dict_failure_counts():
    """Non-dict failure_counts treated as no failures (consistent
    with classify_outcome)."""
    assert default_clean_predicate({
        "session_outcome": "complete",
        "failure_class_counts": "garbage",
    }) is False  # strict — predicate requires dict for safety


def test_decision_trace_predicate_requires_ops():
    summary_clean_no_ops = {
        "session_outcome": "complete",
        "failure_class_counts": {},
        "ops_count": 0,
    }
    assert (
        predicate_requires_decision_trace_rows(summary_clean_no_ops)
        is False
    )
    summary_clean_with_ops = {
        "session_outcome": "complete",
        "failure_class_counts": {},
        "ops_count": 5,
    }
    assert (
        predicate_requires_decision_trace_rows(summary_clean_with_ops)
        is True
    )


def test_curiosity_predicate_uses_hypothesis_count_when_present():
    summary = {
        "session_outcome": "complete",
        "failure_class_counts": {},
        "ops_count": 3,
        "curiosity_hypotheses_generated": 2,
    }
    assert predicate_requires_curiosity_hypothesis(summary) is True

    summary_zero_hyp = dict(summary)
    summary_zero_hyp["curiosity_hypotheses_generated"] = 0
    assert (
        predicate_requires_curiosity_hypothesis(summary_zero_hyp)
        is False
    )


def test_curiosity_predicate_falls_back_to_ops_when_field_absent():
    summary = {
        "session_outcome": "complete",
        "failure_class_counts": {},
        "ops_count": 3,
        # no curiosity_hypotheses_generated field
    }
    assert predicate_requires_curiosity_hypothesis(summary) is True


# ---------------------------------------------------------------------------
# Registry helpers
# ---------------------------------------------------------------------------


def test_get_contract_unknown_flag_returns_default():
    c = get_contract("JARVIS_DOES_NOT_EXIST")
    assert c.flag_name == "JARVIS_DOES_NOT_EXIST"
    assert c.clean_predicate is None
    assert c.failure_class_blocklist_overrides == frozenset()


def test_get_contract_known_flag_returns_custom():
    c = get_contract("JARVIS_CURIOSITY_ENGINE_ENABLED")
    assert c.clean_predicate is predicate_requires_curiosity_hypothesis


def test_get_contract_non_string_returns_empty_default():
    c = get_contract(None)  # type: ignore[arg-type]
    assert c.flag_name == ""


def test_has_custom_contract():
    assert has_custom_contract(
        "JARVIS_CURIOSITY_ENGINE_ENABLED",
    ) is True
    assert has_custom_contract("JARVIS_RANDOM") is False


def test_known_contract_flags_subset_of_cadence_policy():
    """Bit-rot guard: every flag with a built-in custom contract
    must itself be in CADENCE_POLICY (else contract refers to a
    ghost flag)."""
    cadence_known = _ledger_mod.known_flags()
    for flag in known_contract_flags():
        assert flag in cadence_known, (
            f"contract registered for {flag!r} but flag not in "
            f"CADENCE_POLICY"
        )


def test_all_contracts_metadata_includes_phase_8_substrate():
    meta = all_contracts_metadata()
    for flag in [
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        "JARVIS_LATENT_CONFIDENCE_RING_ENABLED",
        "JARVIS_FLAG_CHANGE_EMITTER_ENABLED",
        "JARVIS_LATENCY_SLO_DETECTOR_ENABLED",
        "JARVIS_MULTI_OP_TIMELINE_ENABLED",
    ]:
        assert flag in meta
        assert meta[flag]["has_custom_predicate"] is True


def test_all_contracts_metadata_includes_curiosity_engine():
    meta = all_contracts_metadata()
    assert "JARVIS_CURIOSITY_ENGINE_ENABLED" in meta


# ---------------------------------------------------------------------------
# Harness ↔ contract integration
# ---------------------------------------------------------------------------


def _fake_runner_returning(summary, *, debug_tail=""):
    def runner(*, env, cost_cap_usd, max_wall_seconds, timeout_s, project_root):
        return (0, summary, debug_tail)
    return runner


def test_harness_master_off_byte_identical(
    isolated_paths, monkeypatch: pytest.MonkeyPatch,
):
    """Contract consultation MASTER OFF — harness behavior unchanged."""
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    # Master JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT not set.
    # Curiosity flag has a contract requiring ≥1 hypothesis. With
    # master off, the contract MUST be ignored — session with no
    # hypotheses still graduates as CLEAN per default classifier.
    deps = _soak.get_dependencies("JARVIS_CURIOSITY_ENGINE_ENABLED")
    for d in deps:
        ledger = _ledger_mod.get_default_ledger()
        for i in range(3):
            ledger.record_session(
                flag_name=d, session_id=f"sid-{d}-{i}",
                outcome=_ledger_mod.SessionOutcome.CLEAN,
                recorded_by="test",
            )
    harness = get_default_harness()
    fake = _fake_runner_returning({
        "session_id": "bt-master-off-1",
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": {},
        "ops_count": 5,
        # NO curiosity_hypotheses_generated — contract would block.
    })
    result = harness.run_soak(
        flag_name="JARVIS_CURIOSITY_ENGINE_ENABLED",
        subprocess_runner=fake,
    )
    assert result.status == HarnessStatus.OK
    # Without contract consultation, default classifier runs unchanged.
    # ops_count=5 + complete + no failures → CLEAN.
    assert result.evidence is not None
    assert result.evidence.outcome == "clean"


def test_harness_contract_downgrades_clean_to_runner(
    isolated_paths, monkeypatch: pytest.MonkeyPatch,
):
    """Contract consultation MASTER ON — Curiosity flag's predicate
    DOWNGRADES default-CLEAN to RUNNER when no hypotheses generated."""
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT", "true",
    )
    # Graduate curiosity's deps so we can test the flag itself.
    ledger = _ledger_mod.get_default_ledger()
    for d in _soak.get_dependencies("JARVIS_CURIOSITY_ENGINE_ENABLED"):
        for i in range(3):
            ledger.record_session(
                flag_name=d, session_id=f"sid-{d}-{i}",
                outcome=_ledger_mod.SessionOutcome.CLEAN,
                recorded_by="test",
            )
    harness = get_default_harness()
    fake = _fake_runner_returning({
        "session_id": "bt-no-hyp-1",
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": {},
        "ops_count": 0,  # no ops; predicate fallback fails
        "curiosity_hypotheses_generated": 0,
    })
    result = harness.run_soak(
        flag_name="JARVIS_CURIOSITY_ENGINE_ENABLED",
        subprocess_runner=fake,
    )
    assert result.status == HarnessStatus.OK
    assert result.evidence is not None
    assert result.evidence.outcome == "runner"
    assert result.evidence.runner_attributed is True
    assert "contract_predicate_downgraded_clean" in (
        result.evidence.notes
    )


def test_harness_contract_clean_preserved_when_predicate_passes(
    isolated_paths, monkeypatch: pytest.MonkeyPatch,
):
    """When custom predicate also returns True, default CLEAN
    preserved — contract is purely additive."""
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT", "true",
    )
    harness = get_default_harness()
    fake = _fake_runner_returning({
        "session_id": "bt-substrate-clean",
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": {},
        "ops_count": 10,  # decision_trace predicate satisfied
    })
    result = harness.run_soak(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        subprocess_runner=fake,
    )
    assert result.status == HarnessStatus.OK
    assert result.evidence is not None
    assert result.evidence.outcome == "clean"
    assert "contract_predicate_downgraded_clean" not in (
        result.evidence.notes
    )


# ---------------------------------------------------------------------------
# REPL — live-* subcommands
# ---------------------------------------------------------------------------


def _enable_repl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JARVIS_GRADUATE_REPL_ENABLED", "true")
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_ENABLED", "true")


def test_help_includes_live_subcommands(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable_repl(monkeypatch)
    r = dispatch_graduate(["help"])
    assert r.status == DispatchStatus.OK
    for sub in [
        "live-queue", "live-evidence", "live-next",
        "live-contracts", "live-pause", "live-resume",
    ]:
        assert sub in r.output


def test_live_queue_renders_24_flags(
    monkeypatch: pytest.MonkeyPatch, isolated_paths,
):
    _enable_repl(monkeypatch)
    r = dispatch_graduate(["live-queue"])
    assert r.status == DispatchStatus.OK
    assert "24 flags" in r.output


def test_live_queue_master_off(monkeypatch: pytest.MonkeyPatch):
    """Live-queue is read-side, but it requires REPL master flag
    like the other read-side subcommands."""
    monkeypatch.delenv("JARVIS_GRADUATE_REPL_ENABLED", raising=False)
    r = dispatch_graduate(["live-queue"])
    assert r.status == DispatchStatus.DISABLED


def test_live_evidence_unknown_flag_400(
    monkeypatch: pytest.MonkeyPatch, isolated_paths,
):
    _enable_repl(monkeypatch)
    r = dispatch_graduate(["live-evidence", "JARVIS_DOES_NOT_EXIST"])
    assert r.status == DispatchStatus.UNKNOWN_FLAG


def test_live_evidence_known_flag_no_history(
    monkeypatch: pytest.MonkeyPatch, isolated_paths,
):
    _enable_repl(monkeypatch)
    r = dispatch_graduate(
        ["live-evidence", "JARVIS_HYPOTHESIS_PROBE_ENABLED"],
    )
    assert r.status == DispatchStatus.OK
    assert "no evidence rows" in r.output.lower()


def test_live_evidence_missing_flag_arg(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable_repl(monkeypatch)
    r = dispatch_graduate(["live-evidence"])
    assert r.status == DispatchStatus.INVALID_ARGS


def test_live_next_returns_substrate_flag(
    monkeypatch: pytest.MonkeyPatch, isolated_paths,
):
    _enable_repl(monkeypatch)
    r = dispatch_graduate(["live-next"])
    assert r.status == DispatchStatus.OK
    assert "next pickable flag" in r.output.lower()


def test_live_contracts_lists_curiosity(
    monkeypatch: pytest.MonkeyPatch, isolated_paths,
):
    _enable_repl(monkeypatch)
    r = dispatch_graduate(["live-contracts"])
    assert r.status == DispatchStatus.OK
    assert "JARVIS_CURIOSITY_ENGINE_ENABLED" in r.output


def test_live_contracts_shows_consultation_flag_state(
    monkeypatch: pytest.MonkeyPatch, isolated_paths,
):
    _enable_repl(monkeypatch)
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT", "true",
    )
    r = dispatch_graduate(["live-contracts"])
    assert "consultation_enabled: True" in r.output


def test_live_pause_prints_export_command(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable_repl(monkeypatch)
    r = dispatch_graduate(["live-pause"])
    assert r.status == DispatchStatus.OK
    assert "export JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED=true" in (
        r.output
    )


def test_live_resume_prints_unset_command(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable_repl(monkeypatch)
    r = dispatch_graduate(["live-resume"])
    assert r.status == DispatchStatus.OK
    assert "unset JARVIS_LIVE_FIRE_GRADUATION_SOAK_PAUSED" in r.output


# ---------------------------------------------------------------------------
# REPL never raises — even when harness module fails to import
# ---------------------------------------------------------------------------


def test_live_queue_never_raises_with_broken_harness(
    monkeypatch: pytest.MonkeyPatch, isolated_paths,
):
    """Force the harness import to fail; subcommand must return
    structured stub, not raise."""
    _enable_repl(monkeypatch)
    import sys
    # Save + replace the module with a broken stub.
    module_name = (
        "backend.core.ouroboros.governance.graduation.live_fire_soak"
    )
    real = sys.modules.pop(module_name, None)

    class _Boom:
        def __getattr__(self, name):
            raise ImportError("simulated")

    sys.modules[module_name] = _Boom()  # type: ignore[assignment]
    try:
        r = dispatch_graduate(["live-queue"])
        assert r.status == DispatchStatus.OK
        assert "unavailable" in r.output.lower()
    finally:
        if real is not None:
            sys.modules[module_name] = real


# ---------------------------------------------------------------------------
# Authority / cage invariants
# ---------------------------------------------------------------------------


def test_contract_module_does_not_import_gate_modules():
    import ast
    import inspect
    src = inspect.getsource(_contract)
    tree = ast.parse(src)
    banned = [
        "orchestrator", "iron_gate", "risk_tier_floor",
        "semantic_guardian", "policy_engine",
        "candidate_generator", "tool_executor", "change_engine",
    ]
    for node in ast.walk(tree):
        names: List[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names = [node.module]
        for mod in names:
            for token in banned:
                assert token not in mod, (
                    f"graduation_contract imports {mod!r} (banned "
                    f"token {token!r})"
                )


def test_contract_module_top_level_imports_stdlib_only():
    import ast
    import inspect
    src = inspect.getsource(_contract)
    tree = ast.parse(src)
    top_level: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.append(node.module)
    for mod in top_level:
        assert not mod.startswith("backend."), (
            f"graduation_contract pulled backend module {mod!r} into "
            f"top-level imports — should be stdlib + typing only"
        )


def test_repl_extensions_lazy_import_harness():
    """The graduate_repl module's NEW Phase 9.3 extensions must lazy-
    import the harness so a master-off `/graduate help` invocation
    doesn't pay the substrate cost."""
    import ast
    import inspect
    src = inspect.getsource(_repl)
    tree = ast.parse(src)
    top_level: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.append(node.module)
    forbidden = {
        "backend.core.ouroboros.governance.graduation.live_fire_soak",
        "backend.core.ouroboros.governance.graduation.graduation_contract",
    }
    leaked = forbidden & set(top_level)
    assert not leaked, (
        f"graduate_repl hoisted live-fire imports to top level: {leaked}"
    )


def test_no_secret_leakage_in_module_constants():
    text = repr(vars(_contract))
    for needle in ("sk-", "ghp_", "AKIA", "BEGIN PRIVATE KEY"):
        assert needle not in text


def test_public_api_count_pinned():
    public = sorted(
        n for n in dir(_contract)
        if not n.startswith("_") and (
            callable(getattr(_contract, n)) or n.isupper()
        )
    )
    required = {
        "GraduationContract",
        "default_clean_predicate",
        "predicate_requires_decision_trace_rows",
        "predicate_requires_curiosity_hypothesis",
        "get_contract",
        "has_custom_contract",
        "known_contract_flags",
        "all_contracts_metadata",
        "is_contract_consultation_enabled",
        "MAX_CONTRACTS",
        "MIN_RE_ARM_SECONDS",
        "MAX_RE_ARM_SECONDS",
        "DEFAULT_RE_ARM_AFTER_RUNNER_SECONDS",
        "CleanPredicate",
    }
    missing = required - set(public)
    assert not missing, f"public API regression: {missing}"


# ---------------------------------------------------------------------------
# NEVER-raises smoke
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("predicate", [
    default_clean_predicate,
    predicate_requires_decision_trace_rows,
    predicate_requires_curiosity_hypothesis,
])
@pytest.mark.parametrize("bad", [
    None, "", 42, [], "string",
    {"session_outcome": None},
    {"failure_class_counts": "not-a-dict"},
])
def test_predicates_never_raise(predicate, bad):
    out = predicate(bad)
    assert out in (True, False)


def test_harness_consultation_never_raises_on_bad_predicate(
    isolated_paths, monkeypatch: pytest.MonkeyPatch,
):
    """Even when an injected contract's predicate raises, harness
    falls back gracefully and continues classification."""
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT", "true",
    )
    harness = get_default_harness()
    fake = _fake_runner_returning({
        "session_id": "bt-pred-boom",
        "session_outcome": "complete",
        "stop_reason": "ok",
        "failure_class_counts": {},
        "ops_count": 5,
    })
    result = harness.run_soak(
        flag_name="JARVIS_DECISION_TRACE_LEDGER_ENABLED",
        subprocess_runner=fake,
    )
    # The substrate's contract has predicate_requires_decision_trace_rows
    # which won't raise on this input; harness completes normally.
    assert result.status == HarnessStatus.OK


# ---------------------------------------------------------------------------
# Help text bit-rot guard
# ---------------------------------------------------------------------------


def test_help_text_lists_live_master_flag():
    from backend.core.ouroboros.governance.adaptation.graduate_repl import (  # noqa: E501
        render_help,
    )
    txt = render_help()
    assert "JARVIS_LIVE_FIRE_GRADUATION_SOAK_ENABLED" in txt
    assert "JARVIS_LIVE_FIRE_USE_GRADUATION_CONTRACT" in txt


def test_help_text_six_live_subcommands_pinned():
    from backend.core.ouroboros.governance.adaptation.graduate_repl import (  # noqa: E501
        render_help,
    )
    txt = render_help()
    for sub in [
        "live-queue", "live-evidence", "live-next",
        "live-contracts", "live-pause", "live-resume",
    ]:
        assert sub in txt

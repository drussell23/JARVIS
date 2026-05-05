"""Phase 10 Slice 5 — graduation contract harness regression
spine (PRD §9 / §32.8.1 / §1610).

Verifies:

  * 5-value `ContractVerdict` closed taxonomy
  * Master flag asymmetric env semantics
  * Per-session evidence extraction (queue tokens + recovery
    transition chain detection)
  * 3-session rolling window + verdict ladder
  * Frozen `SessionEvidence` + `ContractReport` schema +
    `to_dict()` projection
  * 2 AST pins auto-registered + auto-discovered + green
  * Authority asymmetry — pure substrate
  * `is_clean` semantics — both criteria must appear in same
    session window
  * Public API surface
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# ContractVerdict closed taxonomy
# ---------------------------------------------------------------------------


def test_verdict_taxonomy_is_closed_5_values():
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        ContractVerdict,
    )
    expected = {
        "ready_for_purge",
        "insufficient_sessions",
        "missing_queue_evidence",
        "missing_recovery_evidence",
        "disabled",
    }
    assert {v.value for v in ContractVerdict} == expected
    assert len(ContractVerdict) == 5


# ---------------------------------------------------------------------------
# Master flag
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("v", ["true", "1", "yes", "on", "TRUE"])
def test_master_flag_truthy(monkeypatch, v):
    monkeypatch.setenv(
        "JARVIS_PHASE10_GRADUATION_CONTRACT_ENABLED", v,
    )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        graduation_contract_enabled,
    )
    assert graduation_contract_enabled() is True


@pytest.mark.parametrize("v", ["false", "0", "no", "off"])
def test_master_flag_falsy(monkeypatch, v):
    monkeypatch.setenv(
        "JARVIS_PHASE10_GRADUATION_CONTRACT_ENABLED", v,
    )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        graduation_contract_enabled,
    )
    assert graduation_contract_enabled() is False


def test_master_flag_default_true(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_PHASE10_GRADUATION_CONTRACT_ENABLED",
        raising=False,
    )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        graduation_contract_enabled,
    )
    assert graduation_contract_enabled() is True


def test_master_off_yields_disabled_verdict(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_PHASE10_GRADUATION_CONTRACT_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        ContractVerdict, is_ready_for_purge,
    )
    report = is_ready_for_purge(root=tmp_path)
    assert report.verdict == ContractVerdict.DISABLED


# ---------------------------------------------------------------------------
# required_clean_sessions env knob
# ---------------------------------------------------------------------------


def test_required_clean_sessions_default_3(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_PHASE10_REQUIRED_CLEAN_SESSIONS", raising=False,
    )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        required_clean_sessions,
    )
    assert required_clean_sessions() == 3


def test_required_clean_sessions_clamped(monkeypatch):
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        required_clean_sessions,
    )
    monkeypatch.setenv(
        "JARVIS_PHASE10_REQUIRED_CLEAN_SESSIONS", "0",
    )
    assert required_clean_sessions() == 1
    monkeypatch.setenv(
        "JARVIS_PHASE10_REQUIRED_CLEAN_SESSIONS", "999",
    )
    assert required_clean_sessions() == 10
    monkeypatch.setenv(
        "JARVIS_PHASE10_REQUIRED_CLEAN_SESSIONS", "garbage",
    )
    assert required_clean_sessions() == 3


# ---------------------------------------------------------------------------
# Session evidence extraction — synthetic sessions
# ---------------------------------------------------------------------------


def _make_session(
    root: Path,
    *,
    sid: str,
    queue_lines: int = 0,
    recovery_chains: int = 0,
) -> Path:
    """Build a synthetic session dir with controlled queue +
    recovery evidence."""
    sd = root / sid
    sd.mkdir(parents=True, exist_ok=True)
    # debug.log carries queue evidence tokens
    debug_lines = []
    for i in range(queue_lines):
        debug_lines.append(
            f"2026-05-05 10:00:{i:02d} [worker] "
            f"dw_severed_queued:standard:kimi-k2.6:probe_stall (op-{i})"
        )
    (sd / "debug.log").write_text(
        "\n".join(debug_lines), encoding="utf-8",
    )
    # topology_sentinel_history.jsonl carries the OPEN→HALF_OPEN→CLOSED chain
    history_rows = []
    for c in range(recovery_chains):
        mid = f"model-{c}"
        # Each chain: 3 state_change rows in order.
        for idx, (frm, to) in enumerate([
            ("CLOSED", "OPEN"),
            ("OPEN", "HALF_OPEN"),
            ("HALF_OPEN", "CLOSED"),
        ]):
            history_rows.append({
                "ts_epoch": time.time() + c * 100 + idx,
                "model_id": mid,
                "transition_kind": "state_change",
                "from_state": frm,
                "to_state": to,
                "weighted_failure_streak": 0.0,
                "failure_source": None,
                "failure_detail": "",
                "schema_version": "topology_sentinel.1",
            })
    (sd / "topology_sentinel_history.jsonl").write_text(
        "\n".join(json.dumps(r) for r in history_rows),
        encoding="utf-8",
    )
    # summary.json present so the dir looks like a real session
    (sd / "summary.json").write_text(
        json.dumps({"session_outcome": "complete"}),
        encoding="utf-8",
    )
    return sd


def test_extract_session_evidence_clean(tmp_path):
    sd = _make_session(
        tmp_path, sid="bt-test-clean",
        queue_lines=2, recovery_chains=1,
    )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        extract_session_evidence,
    )
    ev = extract_session_evidence(sd)
    assert ev.session_id == "bt-test-clean"
    assert ev.has_queue_evidence is True
    assert ev.has_recovery_transition is True
    assert ev.queue_event_count == 2
    assert ev.recovery_transition_count == 1
    assert ev.is_clean is True


def test_extract_session_evidence_no_queue(tmp_path):
    sd = _make_session(
        tmp_path, sid="bt-no-queue",
        queue_lines=0, recovery_chains=1,
    )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        extract_session_evidence,
    )
    ev = extract_session_evidence(sd)
    assert ev.has_queue_evidence is False
    assert ev.has_recovery_transition is True
    assert ev.is_clean is False


def test_extract_session_evidence_no_recovery(tmp_path):
    sd = _make_session(
        tmp_path, sid="bt-no-recovery",
        queue_lines=2, recovery_chains=0,
    )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        extract_session_evidence,
    )
    ev = extract_session_evidence(sd)
    assert ev.has_queue_evidence is True
    assert ev.has_recovery_transition is False
    assert ev.is_clean is False


def test_extract_session_missing_dir(tmp_path):
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        extract_session_evidence,
    )
    ev = extract_session_evidence(tmp_path / "nope")
    assert ev.is_clean is False
    assert "session_dir_missing" in ev.diagnostics


def test_recovery_chain_detection_partial_no_match(tmp_path):
    """Partial chain (e.g. CLOSED→OPEN→HALF_OPEN without
    HALF_OPEN→CLOSED) MUST NOT count."""
    sd = tmp_path / "bt-partial-recovery"
    sd.mkdir()
    (sd / "debug.log").write_text(
        "dw_severed_queued:standard:m:probe", encoding="utf-8",
    )
    (sd / "summary.json").write_text("{}", encoding="utf-8")
    rows = [
        {
            "ts_epoch": 1.0,
            "model_id": "m",
            "transition_kind": "state_change",
            "from_state": "CLOSED",
            "to_state": "OPEN",
        },
        {
            "ts_epoch": 2.0,
            "model_id": "m",
            "transition_kind": "state_change",
            "from_state": "OPEN",
            "to_state": "HALF_OPEN",
        },
        # No HALF_OPEN→CLOSED — chain incomplete.
    ]
    (sd / "topology_sentinel_history.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows),
        encoding="utf-8",
    )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        extract_session_evidence,
    )
    ev = extract_session_evidence(sd)
    assert ev.has_recovery_transition is False
    assert ev.recovery_transition_count == 0


# ---------------------------------------------------------------------------
# is_ready_for_purge verdict ladder
# ---------------------------------------------------------------------------


def test_verdict_insufficient_sessions(tmp_path):
    """Fewer than required (3) sessions → INSUFFICIENT_SESSIONS."""
    _make_session(
        tmp_path, sid="bt-only-1",
        queue_lines=2, recovery_chains=1,
    )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        ContractVerdict, is_ready_for_purge,
    )
    report = is_ready_for_purge(root=tmp_path)
    assert report.verdict == ContractVerdict.INSUFFICIENT_SESSIONS
    assert report.sessions_inspected == 1
    assert report.required_clean_sessions == 3


def test_verdict_missing_queue_evidence(tmp_path):
    """3 sessions, but at least one lacks queue evidence."""
    _make_session(
        tmp_path, sid="bt-clean-1",
        queue_lines=2, recovery_chains=1,
    )
    _make_session(
        tmp_path, sid="bt-clean-2",
        queue_lines=2, recovery_chains=1,
    )
    _make_session(
        tmp_path, sid="bt-no-q-3",
        queue_lines=0, recovery_chains=1,
    )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        ContractVerdict, is_ready_for_purge,
    )
    report = is_ready_for_purge(root=tmp_path)
    assert report.verdict == ContractVerdict.MISSING_QUEUE_EVIDENCE


def test_verdict_missing_recovery_evidence(tmp_path):
    _make_session(
        tmp_path, sid="bt-clean-1",
        queue_lines=2, recovery_chains=1,
    )
    _make_session(
        tmp_path, sid="bt-no-r-2",
        queue_lines=2, recovery_chains=0,
    )
    _make_session(
        tmp_path, sid="bt-clean-3",
        queue_lines=2, recovery_chains=1,
    )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        ContractVerdict, is_ready_for_purge,
    )
    report = is_ready_for_purge(root=tmp_path)
    assert report.verdict == ContractVerdict.MISSING_RECOVERY_EVIDENCE


def test_verdict_ready_for_purge(tmp_path):
    """Three clean sessions in window → READY_FOR_PURGE."""
    for sid in ("bt-clean-1", "bt-clean-2", "bt-clean-3"):
        _make_session(
            tmp_path, sid=sid,
            queue_lines=2, recovery_chains=1,
        )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        ContractVerdict, is_ready_for_purge,
    )
    report = is_ready_for_purge(root=tmp_path)
    assert report.verdict == ContractVerdict.READY_FOR_PURGE
    assert report.clean_sessions == 3


def test_rolling_window_uses_most_recent_three(tmp_path):
    """4 sessions: 3 most-recent clean → READY_FOR_PURGE."""
    # Build with explicit mtime ordering — older session NOT clean
    older = _make_session(
        tmp_path, sid="bt-old-not-clean",
        queue_lines=0, recovery_chains=0,
    )
    # Force old mtime
    import os
    os.utime(older, (1.0, 1.0))
    for sid in ("bt-recent-1", "bt-recent-2", "bt-recent-3"):
        _make_session(
            tmp_path, sid=sid,
            queue_lines=2, recovery_chains=1,
        )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        ContractVerdict, is_ready_for_purge,
    )
    report = is_ready_for_purge(root=tmp_path)
    assert report.verdict == ContractVerdict.READY_FOR_PURGE


# ---------------------------------------------------------------------------
# Frozen result containers + projection
# ---------------------------------------------------------------------------


def test_session_evidence_is_frozen():
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        SessionEvidence,
    )
    ev = SessionEvidence(
        session_id="x",
        has_queue_evidence=True,
        has_recovery_transition=True,
    )
    with pytest.raises(Exception):
        ev.session_id = "y"  # type: ignore


def test_contract_report_to_dict(tmp_path):
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        is_ready_for_purge,
        PHASE10_GRADUATION_CONTRACT_SCHEMA_VERSION,
    )
    report = is_ready_for_purge(root=tmp_path)
    d = report.to_dict()
    assert (
        d["schema_version"]
        == PHASE10_GRADUATION_CONTRACT_SCHEMA_VERSION
    )
    assert d["verdict"] == "insufficient_sessions"
    assert "session_evidence" in d
    assert "elapsed_s" in d


def test_session_evidence_to_dict(tmp_path):
    sd = _make_session(
        tmp_path, sid="bt-projection",
        queue_lines=1, recovery_chains=1,
    )
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        extract_session_evidence,
    )
    ev = extract_session_evidence(sd)
    d = ev.to_dict()
    assert d["session_id"] == "bt-projection"
    assert d["is_clean"] is True
    assert d["has_queue_evidence"] is True
    assert d["queue_event_count"] == 1


# ---------------------------------------------------------------------------
# AST pins — auto-registered + green
# ---------------------------------------------------------------------------


_EXPECTED_PIN_NAMES = {
    "topology_sentinel_master_flag_stays_default_false",
    "phase10_graduation_contract_authority_asymmetry",
}


def test_pins_auto_registered():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        list_shipped_code_invariants,
    )
    registered = {
        inv.invariant_name
        for inv in list_shipped_code_invariants()
    }
    missing = _EXPECTED_PIN_NAMES - registered
    assert not missing, (
        f"missing Phase 10 graduation pins: {missing}"
    )


def test_pins_pass_validation():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        validate_all,
    )
    violations = validate_all()
    relevant = [
        v for v in violations
        if v.invariant_name in _EXPECTED_PIN_NAMES
    ]
    assert not relevant, (
        "Phase 10 graduation pin violations: "
        + "; ".join(
            f"{v.invariant_name}: {v.violation}"
            for v in relevant
        )
    )


def test_master_flag_pin_blocks_premature_flip():
    """If a future PR flips the master flag default to True
    BEFORE running 3 forced-clean sessions, the AST pin should
    fire. Synthetic check via ad-hoc validate call."""
    from backend.core.ouroboros.governance.phase10_graduation_contract import (  # noqa: E501
        register_shipped_invariants,
    )
    pins = register_shipped_invariants()
    flag_pin = next(
        p for p in pins
        if p.invariant_name
        == "topology_sentinel_master_flag_stays_default_false"
    )
    import ast
    synthetic_source = (
        'def topology_sentinel_enabled():\n'
        '    return _env_bool('
        '"JARVIS_TOPOLOGY_SENTINEL_ENABLED", default=True)\n'
    )
    tree = ast.parse(synthetic_source)
    violations = flag_pin.validate(tree, synthetic_source)
    assert violations  # premature flip detected


# ---------------------------------------------------------------------------
# Authority asymmetry
# ---------------------------------------------------------------------------


def test_authority_asymmetry():
    import ast as _ast
    target = (
        Path(__file__).resolve().parents[2]
        / "backend" / "core" / "ouroboros" / "governance"
        / "phase10_graduation_contract.py"
    )
    tree = _ast.parse(target.read_text(encoding="utf-8"))
    forbidden = (
        "orchestrator",
        "iron_gate",
        "policy",
        "providers",
        "candidate_generator",
        "urgency_router",
        "change_engine",
        "semantic_guardian",
    )
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            module = node.module or ""
            for f in forbidden:
                if f in module:
                    pytest.fail(
                        f"phase10_graduation_contract MUST NOT "
                        f"import {module!r}"
                    )


# ---------------------------------------------------------------------------
# Public API stability
# ---------------------------------------------------------------------------


def test_public_api_exports():
    from backend.core.ouroboros.governance import (
        phase10_graduation_contract as h,
    )
    expected = (
        "ContractVerdict",
        "ContractReport",
        "SessionEvidence",
        "is_ready_for_purge",
        "extract_session_evidence",
        "graduation_contract_enabled",
        "required_clean_sessions",
        "session_root",
        "register_shipped_invariants",
        "PHASE10_GRADUATION_CONTRACT_SCHEMA_VERSION",
    )
    for name in expected:
        assert hasattr(h, name), f"missing public symbol: {name}"

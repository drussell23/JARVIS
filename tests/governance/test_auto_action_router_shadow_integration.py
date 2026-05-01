"""Move 3 Slice 3 — shadow-mode integration regression suite.

Pins the contract for the four Slice 3 surfaces:

  1. ``_VerdictRingBuffer`` + ``record_confidence_verdict`` —
     bounded process-local ring buffer for confidence verdicts.
     Closes Slice 2's empty-tuple stub for
     ``recent_confidence_verdicts``.

  2. ``AutoActionProposalLedger`` — append-only JSONL ledger for
     advisory action proposals. NO_ACTION proposals are skipped
     to keep the ledger focused on operator-relevant signal.

  3. ``PostPostmortemObserver`` protocol +
     ``register_post_postmortem_observer`` /
     ``get_post_postmortem_observer`` — mirror of the
     ``OpsDigestObserver`` pattern from
     ``backend.core.ouroboros.governance.ops_digest_observer``.

  4. ``AutoActionShadowObserver`` — concrete observer that runs
     ``gather_context`` → ``propose_advisory_action`` → ledger
     append on each terminal-postmortem-persisted event. Master
     flag default still false; observer first-line-checks
     ``auto_action_router_enabled`` and short-circuits.

  5. Bridge wirings: ``confidence_observability.publish_*_event``
     → ``record_confidence_verdict``;
     ``postmortem_observability.publish_terminal_postmortem_persisted``
     → ``observer.on_terminal_postmortem_persisted``.

Authority Invariant
-------------------
Tests import only the modules under test + stdlib. No
orchestrator / phase_runners / iron_gate / change_engine.
"""
from __future__ import annotations

import json
import os
import pathlib
import threading
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True)
def _isolate_router_state():
    """Clear all Slice 3 process-local state (ring buffer, ledger
    singleton, ctx registry, observer registration) before every
    test so prior-test pollution doesn't bleed into assertions."""
    from backend.core.ouroboros.governance.auto_action_router import (
        _verdict_buffer, reset_default_ledger_for_tests,
        clear_op_context_registry, reset_post_postmortem_observer,
    )
    _verdict_buffer.clear()
    reset_default_ledger_for_tests()
    clear_op_context_registry()
    reset_post_postmortem_observer()
    yield
    _verdict_buffer.clear()
    reset_default_ledger_for_tests()
    clear_op_context_registry()
    reset_post_postmortem_observer()


# -----------------------------------------------------------------------
# § A — Verdict ring buffer
# -----------------------------------------------------------------------


def _reset_verdict_buffer():
    """Helper: clear the singleton buffer between tests."""
    from backend.core.ouroboros.governance.auto_action_router import (
        _verdict_buffer,
    )
    _verdict_buffer.clear()


def test_record_confidence_verdict_appends_to_buffer():
    _reset_verdict_buffer()
    from backend.core.ouroboros.governance.auto_action_router import (
        record_confidence_verdict, recent_confidence_verdicts,
    )
    record_confidence_verdict(
        op_id="op-1", verdict="BELOW_FLOOR", rolling_margin=0.05,
    )
    record_confidence_verdict(
        op_id="op-2", verdict="OK", rolling_margin=0.7,
    )
    out = recent_confidence_verdicts(limit=10)
    assert len(out) == 2
    # Verdict mapping: BELOW_FLOOR -> ESCALATE; OK -> RETRY
    assert out[0].verdict == "ESCALATE"
    assert out[1].verdict == "RETRY"
    assert out[0].rolling_margin == 0.05


def test_record_confidence_verdict_normalizes_dispatcher_strings():
    _reset_verdict_buffer()
    from backend.core.ouroboros.governance.auto_action_router import (
        record_confidence_verdict, recent_confidence_verdicts,
    )
    # Dispatcher form passed directly (case-insensitive)
    record_confidence_verdict(op_id="op-1", verdict="escalate")
    record_confidence_verdict(op_id="op-2", verdict="retry")
    record_confidence_verdict(op_id="op-3", verdict="INCONCLUSIVE")
    out = recent_confidence_verdicts(limit=10)
    assert out[0].verdict == "ESCALATE"
    assert out[1].verdict == "RETRY"
    assert out[2].verdict == "INCONCLUSIVE"


def test_record_confidence_verdict_empty_op_id_skipped():
    _reset_verdict_buffer()
    from backend.core.ouroboros.governance.auto_action_router import (
        record_confidence_verdict, recent_confidence_verdicts,
    )
    record_confidence_verdict(op_id="", verdict="BELOW_FLOOR")
    record_confidence_verdict(op_id="op-1", verdict="")
    assert recent_confidence_verdicts() == ()


def test_verdict_buffer_drops_oldest_when_full(monkeypatch):
    monkeypatch.setenv("JARVIS_AUTO_ACTION_VERDICT_BUFFER_MAXLEN", "8")
    from backend.core.ouroboros.governance.auto_action_router import (
        _verdict_buffer, record_confidence_verdict,
        recent_confidence_verdicts,
    )
    _verdict_buffer.reset_maxlen()
    for i in range(20):
        record_confidence_verdict(
            op_id=f"op-{i}", verdict="BELOW_FLOOR",
        )
    out = recent_confidence_verdicts(limit=100)
    # Maxlen 8 — oldest 12 dropped; only newest 8 remain
    assert len(out) == 8
    op_ids = [v.op_id for v in out]
    assert op_ids[-1] == "op-19"
    assert "op-0" not in op_ids


def test_verdict_buffer_thread_safe(monkeypatch):
    """Concurrent producers must not lose appends or corrupt
    state. We don't assert exact ordering, just count integrity
    + no exceptions."""
    monkeypatch.delenv(
        "JARVIS_AUTO_ACTION_VERDICT_BUFFER_MAXLEN", raising=False,
    )
    from backend.core.ouroboros.governance.auto_action_router import (
        _verdict_buffer, record_confidence_verdict,
        recent_confidence_verdicts,
    )
    _verdict_buffer.reset_maxlen()  # rebuilds with default maxlen=32
    _verdict_buffer.clear()
    n_threads = 4
    per_thread = 25

    def producer(tid: int) -> None:
        for i in range(per_thread):
            record_confidence_verdict(
                op_id=f"t{tid}-op-{i}", verdict="OK",
            )

    threads = [
        threading.Thread(target=producer, args=(i,))
        for i in range(n_threads)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    out = recent_confidence_verdicts(limit=200)
    # Total should be n_threads * per_thread = 100 (under maxlen 32 = 32)
    # Default maxlen is 32, so we get 32 newest.
    assert len(out) == 32
    # All entries are well-formed
    for v in out:
        assert v.op_id.startswith("t")
        assert v.verdict == "RETRY"  # OK → RETRY


def test_verdict_buffer_default_maxlen():
    monkeypatch_env = os.environ.copy()
    os.environ.pop("JARVIS_AUTO_ACTION_VERDICT_BUFFER_MAXLEN", None)
    try:
        from backend.core.ouroboros.governance.auto_action_router import (
            _verdict_buffer_maxlen,
        )
        assert _verdict_buffer_maxlen() == 32
    finally:
        os.environ.clear()
        os.environ.update(monkeypatch_env)


def test_verdict_buffer_maxlen_floor():
    os.environ["JARVIS_AUTO_ACTION_VERDICT_BUFFER_MAXLEN"] = "2"
    try:
        from backend.core.ouroboros.governance.auto_action_router import (
            _verdict_buffer_maxlen,
        )
        assert _verdict_buffer_maxlen() == 8  # floor
    finally:
        os.environ.pop(
            "JARVIS_AUTO_ACTION_VERDICT_BUFFER_MAXLEN", None,
        )


# -----------------------------------------------------------------------
# § B — Advisory proposal ledger
# -----------------------------------------------------------------------


def test_ledger_skips_no_action(tmp_path):
    """NO_ACTION proposals are not written — keeps the ledger
    focused on operator-actionable signal."""
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionProposalLedger, AdvisoryAction, AdvisoryActionType,
    )
    ledger_path = tmp_path / "test_ledger.jsonl"
    ledger = AutoActionProposalLedger(path=ledger_path)
    no_action = AdvisoryAction(
        action_type=AdvisoryActionType.NO_ACTION,
        reason_code="no_signal",
        evidence="nothing",
    )
    assert ledger.append(no_action) is False
    assert not ledger_path.exists()


def test_ledger_appends_actionable_proposals(tmp_path):
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionProposalLedger, AdvisoryAction, AdvisoryActionType,
    )
    ledger_path = tmp_path / "test_ledger.jsonl"
    ledger = AutoActionProposalLedger(path=ledger_path)
    a1 = AdvisoryAction(
        action_type=AdvisoryActionType.DEMOTE_RISK_TIER,
        reason_code="op_family_failure_rate_safe_auto",
        evidence="3/3 failed",
        target_op_family="doc_staleness",
        proposed_risk_tier="notify_apply",
        rolling_failure_rate=1.0,
        history_size=3,
        op_id="op-test-1",
    )
    a2 = AdvisoryAction(
        action_type=AdvisoryActionType.ROUTE_TO_NOTIFY_APPLY,
        reason_code="recurring_confidence_escalation",
        evidence="2/2 ESCALATE",
        op_id="op-test-2",
    )
    assert ledger.append(a1) is True
    assert ledger.append(a2) is True
    rows = ledger.read_recent(limit=10)
    assert len(rows) == 2
    assert rows[0]["action_type"] == "demote_risk_tier"
    assert rows[0]["target_op_family"] == "doc_staleness"
    assert rows[0]["op_id"] == "op-test-1"
    assert rows[1]["action_type"] == "route_to_notify_apply"


def test_ledger_serializes_to_valid_jsonl(tmp_path):
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionProposalLedger, AdvisoryAction, AdvisoryActionType,
    )
    ledger_path = tmp_path / "test_ledger.jsonl"
    ledger = AutoActionProposalLedger(path=ledger_path)
    ledger.append(AdvisoryAction(
        action_type=AdvisoryActionType.DEFER_OP_FAMILY,
        reason_code="op_family_failure_rate",
        evidence="5/8 failed",
        target_op_family="github_issue",
    ))
    raw = ledger_path.read_text(encoding="utf-8").splitlines()
    assert len(raw) == 1
    record = json.loads(raw[0])
    assert record["schema_version"] == "auto_action_router.1"
    assert record["action_type"] == "defer_op_family"
    assert "recorded_at_unix" in record


def test_ledger_read_recent_handles_missing_file(tmp_path):
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionProposalLedger,
    )
    ledger = AutoActionProposalLedger(path=tmp_path / "nonexistent.jsonl")
    assert ledger.read_recent(limit=10) == ()


def test_ledger_read_recent_skips_malformed_rows(tmp_path):
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionProposalLedger,
    )
    ledger_path = tmp_path / "test_ledger.jsonl"
    ledger_path.write_text(
        '{"action_type": "defer_op_family"}\n'
        'not valid json\n'
        '{"action_type": "demote_risk_tier"}\n',
    )
    ledger = AutoActionProposalLedger(path=ledger_path)
    rows = ledger.read_recent(limit=10)
    assert len(rows) == 2  # malformed row skipped
    assert rows[0]["action_type"] == "defer_op_family"
    assert rows[1]["action_type"] == "demote_risk_tier"


# -----------------------------------------------------------------------
# § C — PostPostmortemObserver protocol
# -----------------------------------------------------------------------


def test_default_observer_is_noop():
    from backend.core.ouroboros.governance.auto_action_router import (
        get_post_postmortem_observer, reset_post_postmortem_observer,
        _NoopPostPostmortemObserver,
    )
    reset_post_postmortem_observer()
    obs = get_post_postmortem_observer()
    assert isinstance(obs, _NoopPostPostmortemObserver)
    # Calling it does not raise
    obs.on_terminal_postmortem_persisted(
        op_id="op-1",
        terminal_phase="VERIFY",
        has_blocking_failures=False,
    )


def test_register_observer_replaces_default():
    from backend.core.ouroboros.governance.auto_action_router import (
        register_post_postmortem_observer, get_post_postmortem_observer,
        reset_post_postmortem_observer,
    )
    reset_post_postmortem_observer()
    calls = []

    class _RecordingObserver:
        def on_terminal_postmortem_persisted(
            self, *, op_id, terminal_phase, has_blocking_failures,
        ):
            calls.append((op_id, terminal_phase, has_blocking_failures))

    register_post_postmortem_observer(_RecordingObserver())
    obs = get_post_postmortem_observer()
    obs.on_terminal_postmortem_persisted(
        op_id="op-x", terminal_phase="GENERATE",
        has_blocking_failures=True,
    )
    assert calls == [("op-x", "GENERATE", True)]
    reset_post_postmortem_observer()


def test_register_none_restores_noop():
    from backend.core.ouroboros.governance.auto_action_router import (
        register_post_postmortem_observer, get_post_postmortem_observer,
        _NoopPostPostmortemObserver,
    )

    class _Live:
        def on_terminal_postmortem_persisted(
            self, *, op_id, terminal_phase, has_blocking_failures,
        ):
            pass

    register_post_postmortem_observer(_Live())
    register_post_postmortem_observer(None)
    assert isinstance(
        get_post_postmortem_observer(), _NoopPostPostmortemObserver,
    )


# -----------------------------------------------------------------------
# § D — AutoActionShadowObserver
# -----------------------------------------------------------------------


def test_shadow_observer_master_off_writes_nothing(tmp_path, monkeypatch):
    monkeypatch.delenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", raising=False)
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionShadowObserver, AutoActionProposalLedger,
    )
    ledger = AutoActionProposalLedger(path=tmp_path / "ledger.jsonl")
    obs = AutoActionShadowObserver(ledger=ledger)
    obs.on_terminal_postmortem_persisted(
        op_id="op-1", terminal_phase="VERIFY",
        has_blocking_failures=True,
    )
    assert ledger.read_recent() == ()


def test_shadow_observer_writes_actionable_proposal(tmp_path, monkeypatch):
    """Master on + signal cluster → ledger row written, op_id
    stamped from the trigger event."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionShadowObserver, AutoActionProposalLedger,
        register_op_context, clear_op_context_registry,
        lookup_op_context, RecentOpOutcome,
    )
    clear_op_context_registry()
    register_op_context(
        "op-trigger",
        op_family="doc_staleness",
        risk_tier="SAFE_AUTO",
        route="background",
        posture="EXPLORE",
    )
    ledger = AutoActionProposalLedger(path=tmp_path / "ledger.jsonl")
    obs = AutoActionShadowObserver(
        ledger=ledger,
        ctx_lookup=lookup_op_context,
    )
    # Inject failures into the postmortem reader so the dispatcher
    # has something to act on.
    fake_outcomes = tuple(
        RecentOpOutcome(
            op_id=f"op-{i}", op_family="doc_staleness",
            success=False, risk_tier="SAFE_AUTO",
        )
        for i in range(3)
    )
    with patch(
        "backend.core.ouroboros.governance.auto_action_router"
        ".recent_postmortem_outcomes",
        return_value=fake_outcomes,
    ):
        obs.on_terminal_postmortem_persisted(
            op_id="op-trigger", terminal_phase="VERIFY",
            has_blocking_failures=True,
        )
    rows = ledger.read_recent(limit=5)
    assert len(rows) == 1
    assert rows[0]["action_type"] == "demote_risk_tier"
    assert rows[0]["op_id"] == "op-trigger"
    assert rows[0]["target_op_family"] == "doc_staleness"


def test_shadow_observer_no_signal_writes_nothing(tmp_path, monkeypatch):
    """Master on but no signal → NO_ACTION proposal → ledger
    skipped (NO_ACTION is filtered)."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionShadowObserver, AutoActionProposalLedger,
    )
    ledger = AutoActionProposalLedger(path=tmp_path / "ledger.jsonl")
    obs = AutoActionShadowObserver(ledger=ledger)
    with patch(
        "backend.core.ouroboros.governance.auto_action_router"
        ".recent_postmortem_outcomes",
        return_value=(),
    ):
        obs.on_terminal_postmortem_persisted(
            op_id="op-clean", terminal_phase="VERIFY",
            has_blocking_failures=False,
        )
    assert ledger.read_recent() == ()


def test_shadow_observer_swallows_observer_exceptions(tmp_path, monkeypatch):
    """A misbehaving signal reader must not propagate errors out
    of the observer — that would derail the publish path."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionShadowObserver, AutoActionProposalLedger,
    )
    ledger = AutoActionProposalLedger(path=tmp_path / "ledger.jsonl")
    obs = AutoActionShadowObserver(ledger=ledger)
    with patch(
        "backend.core.ouroboros.governance.auto_action_router"
        ".recent_postmortem_outcomes",
        side_effect=RuntimeError("signal source died"),
    ):
        # Must not raise
        obs.on_terminal_postmortem_persisted(
            op_id="op-x", terminal_phase="VERIFY",
            has_blocking_failures=True,
        )


def test_shadow_observer_propagates_cost_contract_violation(tmp_path, monkeypatch):
    """A cost-contract violation MUST bubble — fatal-by-design."""
    monkeypatch.setenv("JARVIS_AUTO_ACTION_ROUTER_ENABLED", "1")
    from backend.core.ouroboros.governance.auto_action_router import (
        AutoActionShadowObserver, AutoActionProposalLedger,
    )
    from backend.core.ouroboros.governance.cost_contract_assertion import (
        CostContractViolation,
    )
    ledger = AutoActionProposalLedger(path=tmp_path / "ledger.jsonl")
    obs = AutoActionShadowObserver(ledger=ledger)
    with patch(
        "backend.core.ouroboros.governance.auto_action_router"
        ".propose_advisory_action",
        side_effect=CostContractViolation(
            op_id="op-x",
            provider_route="background",
            provider_tier="auto_action",
            is_read_only=False,
            detail="test violation",
        ),
    ):
        with pytest.raises(CostContractViolation):
            obs.on_terminal_postmortem_persisted(
                op_id="op-x", terminal_phase="VERIFY",
                has_blocking_failures=True,
            )


# -----------------------------------------------------------------------
# § E — Per-op ctx enrichment registry
# -----------------------------------------------------------------------


def test_register_and_lookup_op_context():
    from backend.core.ouroboros.governance.auto_action_router import (
        register_op_context, lookup_op_context,
        clear_op_context_registry,
    )
    clear_op_context_registry()
    register_op_context(
        "op-1",
        op_family="test_failure",
        risk_tier="SAFE_AUTO",
        route="immediate",
        posture="EXPLORE",
    )
    ctx = lookup_op_context("op-1")
    assert ctx.op_family == "test_failure"
    assert ctx.risk_tier == "SAFE_AUTO"
    assert ctx.route == "immediate"
    assert ctx.posture == "EXPLORE"


def test_lookup_unknown_op_returns_empty_enrichment():
    from backend.core.ouroboros.governance.auto_action_router import (
        lookup_op_context, clear_op_context_registry,
    )
    clear_op_context_registry()
    ctx = lookup_op_context("op-unknown")
    assert ctx.op_family == ""
    assert ctx.risk_tier == ""


def test_lookup_empty_op_id_returns_empty():
    from backend.core.ouroboros.governance.auto_action_router import (
        lookup_op_context,
    )
    ctx = lookup_op_context("")
    assert ctx.op_family == ""


def test_register_lru_eviction():
    """Registry is LRU-bounded — older entries drop on overflow."""
    from backend.core.ouroboros.governance.auto_action_router import (
        register_op_context, lookup_op_context,
        clear_op_context_registry,
        _CTX_ENRICHMENT_MAXLEN,
    )
    clear_op_context_registry()
    # Register one over the cap
    for i in range(_CTX_ENRICHMENT_MAXLEN + 5):
        register_op_context(f"op-{i}", op_family=f"fam-{i}")
    # Earliest are evicted
    assert lookup_op_context("op-0").op_family == ""
    assert lookup_op_context("op-4").op_family == ""
    # Most recent still there
    last_id = f"op-{_CTX_ENRICHMENT_MAXLEN + 4}"
    assert lookup_op_context(last_id).op_family.startswith("fam-")


# -----------------------------------------------------------------------
# § F — Bridge wirings (bytes pins on producer modules)
# -----------------------------------------------------------------------


def test_postmortem_observability_calls_observer():
    """Bytes-pin: ``publish_terminal_postmortem_persisted`` calls
    ``get_post_postmortem_observer().on_terminal_postmortem_persisted(...)``
    after the broker publish returns."""
    src = pathlib.Path(
        "backend/core/ouroboros/governance/postmortem_observability.py"
    ).read_text()
    fn_idx = src.find("def publish_terminal_postmortem_persisted(")
    assert fn_idx > 0
    # Search forward through this function body
    end_idx = src.find("\ndef ", fn_idx + 1)
    body = src[fn_idx:end_idx if end_idx > fn_idx else fn_idx + 5000]
    assert "get_post_postmortem_observer" in body
    assert "on_terminal_postmortem_persisted" in body
    # Wired AFTER the broker publish (so observer sees a persisted
    # record, not a pending one).
    publish_idx = body.find("broker.publish(")
    observer_idx = body.find("on_terminal_postmortem_persisted")
    assert 0 < publish_idx < observer_idx


def test_confidence_observability_calls_record_verdict():
    """Bytes-pin: ``confidence_observability`` calls the auto-action
    bridge from BOTH P1 (drop) and P2 (approaching) publish sites."""
    src = pathlib.Path(
        "backend/core/ouroboros/governance/verification/"
        "confidence_observability.py"
    ).read_text()
    # Bridge helper exists
    assert "_record_verdict_for_auto_action_router" in src
    # Called from at least 2 sites (P1 + P2)
    helper_uses = src.count(
        "_record_verdict_for_auto_action_router(",
    )
    assert helper_uses >= 3, (
        # 1 def + ≥2 call sites
        f"expected ≥3 references (def + ≥2 call sites), got {helper_uses}"
    )


def test_confidence_observability_drop_event_records_below_floor():
    src = pathlib.Path(
        "backend/core/ouroboros/governance/verification/"
        "confidence_observability.py"
    ).read_text()
    # Find the publish_confidence_drop_event body and check it
    # passes BELOW_FLOOR
    fn_idx = src.find("def publish_confidence_drop_event(")
    end_idx = src.find("\ndef publish_confidence_approaching_event(", fn_idx)
    body = src[fn_idx:end_idx]
    assert 'verdict_str="BELOW_FLOOR"' in body


def test_confidence_observability_approaching_event_records_approaching():
    src = pathlib.Path(
        "backend/core/ouroboros/governance/verification/"
        "confidence_observability.py"
    ).read_text()
    fn_idx = src.find("def publish_confidence_approaching_event(")
    end_idx = src.find(
        "\ndef publish_sustained_low_confidence_event(", fn_idx,
    )
    body = src[fn_idx:end_idx]
    assert 'verdict_str="APPROACHING_FLOOR"' in body


# -----------------------------------------------------------------------
# § G — install_shadow_observer boot wiring
# -----------------------------------------------------------------------


def test_install_shadow_observer_registers_concrete_observer():
    from backend.core.ouroboros.governance.auto_action_router import (
        install_shadow_observer, get_post_postmortem_observer,
        AutoActionShadowObserver, reset_post_postmortem_observer,
    )
    reset_post_postmortem_observer()
    install_shadow_observer()
    obs = get_post_postmortem_observer()
    assert isinstance(obs, AutoActionShadowObserver)
    reset_post_postmortem_observer()


def test_install_shadow_observer_idempotent():
    """Calling install twice is safe — second call replaces first
    cleanly (no leak, no compound observer)."""
    from backend.core.ouroboros.governance.auto_action_router import (
        install_shadow_observer, get_post_postmortem_observer,
        reset_post_postmortem_observer,
    )
    reset_post_postmortem_observer()
    install_shadow_observer()
    first = get_post_postmortem_observer()
    install_shadow_observer()
    second = get_post_postmortem_observer()
    # Two different instances (replacement, not augmentation)
    assert first is not second
    reset_post_postmortem_observer()


# -----------------------------------------------------------------------
# § H — Authority invariant
# -----------------------------------------------------------------------


def test_test_module_authority():
    src = pathlib.Path(__file__).read_text()
    forbidden = (
        "phase_runners", "iron_gate", "change_engine",
        "providers", "orchestrator",
        "candidate_generator",
    )
    for tok in forbidden:
        assert (
            f"from backend.core.ouroboros.governance.{tok}" not in src
        ), f"forbidden: {tok}"

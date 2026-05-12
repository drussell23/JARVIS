"""Regression spine for §32.4 / §40.1 #4 Slice 1 — M10 producer-bridge.

Slice 1 closes the "0 proposals fired end-to-end" gap by shipping
:mod:`m10_producer_bridge` — the composer that fuses the canonical
miner output with the canonical proposal_store. Pre-Slice-1, the
miner had no caller and the store had no producer; this slice
wires them together via an operator-initiated REPL surface.

Pins:

* :class:`MineCycleResult` frozen dataclass present at module
  level (structured return type for both entry points).
* :func:`fire_mining_cycle` is async (canonical composition seam).
* :func:`fire_mining_cycle_sync` is sync (REPL bridge — registry
  signature validator requires sync).
* Master-flag gate (``JARVIS_M10_ARCH_PROPOSER_ENABLED``)
  short-circuits at the bridge boundary AND defense-in-depth at
  the miner.
* Canonical composition: bridge imports ``get_default_miner`` +
  ``append_proposal`` + ``StoredProposal`` +
  ``m10_arch_proposer_enabled``. No parallel state, no duplicate
  ledger, no invented projection shape.
* Authority asymmetry: bridge MUST NOT import orchestrator /
  iron_gate / policy / candidate_generator / etc.
* NEVER-raises: every entry point yields a structured
  :class:`MineCycleResult` even on miner crash / store crash /
  loop-detection error.
* Conversion: ``M10ProposalRecord`` → ``StoredProposal`` preserves
  ``proposal_id`` + ``kind`` + ``phase`` + load-bearing detection
  fields.
* Partial persistence: ``ok=False`` when ``rows_stored <
  proposals_emitted_count``.

Plus the REPL extension:

* ``/m10 fire`` is dispatched (not unknown-subcommand).
* ``/m10 help`` text includes the ``fire`` line.
* ``/m10 fire`` renders the structured MineCycleResult shape.
"""
from __future__ import annotations

import ast
import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Tuple
from unittest.mock import patch

import pytest

from backend.core.ouroboros.governance.m10.m10_producer_bridge import (
    M10_PRODUCER_BRIDGE_SCHEMA_VERSION,
    MineCycleResult,
    fire_mining_cycle,
    fire_mining_cycle_sync,
    register_shipped_invariants,
)
from backend.core.ouroboros.governance.m10.primitives import (
    M10ProposalPhase,
    M10ProposalRecord,
    ProposalKind,
)
from backend.core.ouroboros.governance.m10.proposal_store import (
    proposals_jsonl_path,
    read_all_proposals,
)


_M10_FLAG = "JARVIS_M10_ARCH_PROPOSER_ENABLED"


@pytest.fixture(autouse=True)
def _isolate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """Each test starts with master flag cleared + fresh ledger
    path under tmp_path."""
    monkeypatch.delenv(_M10_FLAG, raising=False)
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "proposals.jsonl"),
    )
    # Reset miner singleton state between tests.
    try:
        from backend.core.ouroboros.governance.m10.unhandled_pattern_miner import (  # noqa: E501
            reset_default_miner_for_tests,
        )
        reset_default_miner_for_tests()
    except Exception:  # noqa: BLE001
        pass
    yield


def _enable(monkeypatch) -> None:
    monkeypatch.setenv(_M10_FLAG, "true")


# ---------------------------------------------------------------------------
# Fake miner — operator-injectable for hermetic tests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _FakeMineResult:
    """Duck-typed shape that matches MineResult.outcome +
    proposals_emitted."""
    outcome: object
    proposals_emitted: Tuple[object, ...] = field(default_factory=tuple)


class _OutcomeEnum:
    """Mimics MineOutcome enum's .value access."""
    def __init__(self, value: str) -> None:
        self.value = value


class _FakeMiner:
    """Operator-injectable miner. Returns a fixed MineResult."""
    def __init__(self, result: _FakeMineResult) -> None:
        self._result = result

    async def mine(self, *, now_unix=None):
        return self._result


class _CrashingMiner:
    """Operator-injectable miner that raises on .mine()."""
    async def mine(self, *, now_unix=None):
        raise RuntimeError("simulated miner crash")


def _make_record(
    pid: str = "m10-test-001",
    kind: ProposalKind = ProposalKind.NEW_OBSERVER,
    phase: M10ProposalPhase = M10ProposalPhase.DETECTING,
) -> M10ProposalRecord:
    return M10ProposalRecord(
        proposal_id=pid,
        kind=kind,
        phase=phase,
        pattern_signature="sig-abc-123",
        detection_evidence=("intake_signal=foo", "op_kind=bar"),
    )


# ---------------------------------------------------------------------------
# Structural taxonomy — MineCycleResult shape
# ---------------------------------------------------------------------------


def test_mine_cycle_result_is_frozen_dataclass():
    r = MineCycleResult(ok=True, outcome="emitted")
    with pytest.raises(Exception):
        r.ok = False  # type: ignore[misc]


def test_mine_cycle_result_to_dict_carries_schema_version():
    r = MineCycleResult(ok=True, outcome="emitted")
    d = r.to_dict()
    assert d["schema_version"] == M10_PRODUCER_BRIDGE_SCHEMA_VERSION
    for key in (
        "ok", "outcome", "proposals_emitted_count",
        "rows_stored", "proposal_ids", "elapsed_s", "diagnostic",
    ):
        assert key in d


# ---------------------------------------------------------------------------
# Master-flag gate
# ---------------------------------------------------------------------------


def test_disabled_when_master_off():
    """Master OFF → bridge short-circuits with outcome=disabled.
    ok=True because gated-off is intentional success, not error.
    No store writes."""
    async def _run():
        result = await fire_mining_cycle()
        assert result.ok is True
        assert result.outcome == "disabled"
        assert result.proposals_emitted_count == 0
        assert result.rows_stored == 0

    asyncio.run(_run())


def test_enabled_calls_miner(monkeypatch):
    """Master ON + fake miner emitting 0 records → outcome
    reflects miner's structured response."""
    _enable(monkeypatch)
    fake_miner = _FakeMiner(_FakeMineResult(
        outcome=_OutcomeEnum("no_patterns"),
    ))

    async def _run():
        result = await fire_mining_cycle(miner=fake_miner)
        assert result.outcome == "no_patterns"
        assert result.ok is True
        assert result.proposals_emitted_count == 0

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# End-to-end: miner → bridge → store
# ---------------------------------------------------------------------------


def test_one_record_persists_to_ledger(monkeypatch):
    """The canonical happy path: miner emits 1 record → bridge
    converts via _record_to_stored → append_proposal succeeds →
    ledger has 1 row → result.ok=True, rows_stored=1."""
    _enable(monkeypatch)
    record = _make_record(pid="m10-happy-001")
    fake_miner = _FakeMiner(_FakeMineResult(
        outcome=_OutcomeEnum("emitted"),
        proposals_emitted=(record,),
    ))

    async def _run():
        result = await fire_mining_cycle(miner=fake_miner)
        assert result.ok is True
        assert result.outcome == "emitted"
        assert result.proposals_emitted_count == 1
        assert result.rows_stored == 1
        assert result.proposal_ids == ("m10-happy-001",)

    asyncio.run(_run())

    # Verify the ledger row.
    rows = read_all_proposals()
    assert len(rows) == 1
    assert rows[0].proposal_id == "m10-happy-001"
    assert rows[0].phase == "detecting"
    assert rows[0].pattern_signature == "sig-abc-123"
    assert "intake_signal=foo" in rows[0].detection_evidence


def test_multiple_records_all_persist(monkeypatch):
    _enable(monkeypatch)
    records = tuple(
        _make_record(pid=f"m10-multi-{i:03d}") for i in range(3)
    )
    fake_miner = _FakeMiner(_FakeMineResult(
        outcome=_OutcomeEnum("emitted"),
        proposals_emitted=records,
    ))

    async def _run():
        result = await fire_mining_cycle(miner=fake_miner)
        assert result.ok is True
        assert result.rows_stored == 3
        assert len(result.proposal_ids) == 3

    asyncio.run(_run())

    # Filter to our prefix to avoid cross-test contamination
    # (the JSONL ledger appends globally per env var; tmp_path
    # is unique per test but the path resolver may pick up the
    # default fallback if env was unset between fixture teardowns).
    rows = read_all_proposals()
    ours = [r for r in rows if r.proposal_id.startswith("m10-multi-")]
    assert len(ours) == 3


# ---------------------------------------------------------------------------
# Conversion: M10ProposalRecord → StoredProposal preserves fields
# ---------------------------------------------------------------------------


def test_record_to_stored_preserves_load_bearing_fields():
    from backend.core.ouroboros.governance.m10.m10_producer_bridge import (  # noqa: E501
        _record_to_stored,
    )
    record = M10ProposalRecord(
        proposal_id="m10-conv-001",
        kind=ProposalKind.NEW_SENSOR,
        phase=M10ProposalPhase.DETECTING,
        pattern_signature="sig-conv",
        detection_evidence=("e1", "e2", "e3"),
    )
    stored = _record_to_stored(record)
    assert stored is not None
    assert stored.proposal_id == "m10-conv-001"
    assert stored.kind == "new_sensor"
    assert stored.phase == "detecting"
    assert stored.pattern_signature == "sig-conv"
    assert stored.detection_evidence == ("e1", "e2", "e3")


def test_record_to_stored_returns_none_on_missing_proposal_id():
    """Required field discipline — empty proposal_id refuses
    silent ID drift."""
    from backend.core.ouroboros.governance.m10.m10_producer_bridge import (  # noqa: E501
        _record_to_stored,
    )

    @dataclass(frozen=True)
    class _BadRecord:
        proposal_id: str = ""
        kind: object = ProposalKind.NEW_OBSERVER
        phase: object = M10ProposalPhase.DETECTING

    assert _record_to_stored(_BadRecord()) is None


def test_record_to_stored_returns_none_on_garbage():
    from backend.core.ouroboros.governance.m10.m10_producer_bridge import (  # noqa: E501
        _record_to_stored,
    )
    # No proposal_id attribute → falls through cleanly.
    assert _record_to_stored("not-a-record") is None
    assert _record_to_stored(None) is None
    assert _record_to_stored(42) is None


# ---------------------------------------------------------------------------
# NEVER-raises contract
# ---------------------------------------------------------------------------


def test_bridge_never_raises_on_miner_crash(monkeypatch):
    _enable(monkeypatch)
    crashing = _CrashingMiner()

    async def _run():
        result = await fire_mining_cycle(miner=crashing)
        assert result.ok is False
        assert result.outcome == "error"
        assert "miner.mine raised" in result.diagnostic

    asyncio.run(_run())


def test_bridge_never_raises_on_store_failure(monkeypatch):
    """If append_proposal returns False for every row, bridge
    surfaces ok=False but doesn't raise. Patches the canonical
    symbol at its source (proposal_store) — the bridge's lazy
    import picks up the patched version."""
    _enable(monkeypatch)
    record = _make_record(pid="m10-store-fail")
    fake_miner = _FakeMiner(_FakeMineResult(
        outcome=_OutcomeEnum("emitted"),
        proposals_emitted=(record,),
    ))

    with patch(
        "backend.core.ouroboros.governance.m10."
        "proposal_store.append_proposal",
        return_value=False,
    ):
        async def _run():
            return await fire_mining_cycle(miner=fake_miner)
        result = asyncio.run(_run())
    assert result.proposals_emitted_count == 1
    assert result.rows_stored == 0
    assert result.ok is False
    assert "failed_ids" in result.diagnostic


def test_bridge_partial_persistence(monkeypatch):
    """One record persists, one fails → rows_stored=1,
    proposals_emitted_count=2, ok=False."""
    _enable(monkeypatch)
    records = (
        _make_record(pid="m10-partial-ok"),
        _make_record(pid="m10-partial-fail"),
    )
    fake_miner = _FakeMiner(_FakeMineResult(
        outcome=_OutcomeEnum("emitted"),
        proposals_emitted=records,
    ))

    call_count = {"n": 0}

    def _flaky_append(proposal, *, path=None):
        call_count["n"] += 1
        return call_count["n"] == 1  # first succeeds, second fails

    with patch(
        "backend.core.ouroboros.governance.m10."
        "proposal_store.append_proposal",
        side_effect=_flaky_append,
    ):
        async def _run():
            return await fire_mining_cycle(miner=fake_miner)
        result = asyncio.run(_run())
    assert result.proposals_emitted_count == 2
    assert result.rows_stored == 1
    assert result.ok is False
    assert result.proposal_ids == ("m10-partial-ok",)


# ---------------------------------------------------------------------------
# Sync bridge — REPL-friendly entry
# ---------------------------------------------------------------------------


def test_fire_mining_cycle_sync_no_running_loop(monkeypatch):
    """Called from sync context (no running loop) → uses
    asyncio.run directly."""
    _enable(monkeypatch)
    fake_miner = _FakeMiner(_FakeMineResult(
        outcome=_OutcomeEnum("no_patterns"),
    ))
    result = fire_mining_cycle_sync(miner=fake_miner)
    assert isinstance(result, MineCycleResult)
    assert result.outcome == "no_patterns"


def test_fire_mining_cycle_sync_inside_running_loop(monkeypatch):
    """Called from inside an event loop → bridges via worker
    thread so the caller's loop isn't disturbed."""
    _enable(monkeypatch)
    fake_miner = _FakeMiner(_FakeMineResult(
        outcome=_OutcomeEnum("emitted"),
        proposals_emitted=(_make_record(pid="m10-loop-test"),),
    ))

    async def _outer():
        # Inside a running loop — sync bridge MUST handle this.
        return fire_mining_cycle_sync(miner=fake_miner)

    result = asyncio.run(_outer())
    assert isinstance(result, MineCycleResult)
    assert result.ok is True
    assert result.proposal_ids == ("m10-loop-test",)


def test_fire_mining_cycle_sync_never_raises():
    """Garbage timeout → structured failure, not exception."""
    result = fire_mining_cycle_sync(timeout_s=0.001)
    assert isinstance(result, MineCycleResult)
    # Master flag off → outcome=disabled (graceful).
    assert result.outcome in ("disabled", "error")


def test_fire_mining_cycle_sync_disabled_returns_cleanly():
    """Master OFF + sync entry → outcome=disabled, ok=True."""
    result = fire_mining_cycle_sync()
    assert result.ok is True
    assert result.outcome == "disabled"


# ---------------------------------------------------------------------------
# AST-pinned authority + composition invariants
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_four_pins():
    pins = register_shipped_invariants()
    names = {p.invariant_name for p in pins}
    assert names == {
        "m10_producer_bridge_result_taxonomy",
        "m10_producer_bridge_composes_canonical",
        "m10_producer_bridge_authority_asymmetry",
        "m10_producer_bridge_dual_entry_points",
    }


def test_all_ast_pins_pass_on_current_source():
    pins = register_shipped_invariants()
    src_path = Path(
        "backend/core/ouroboros/governance/m10/"
        "m10_producer_bridge.py"
    )
    source = src_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    for pin in pins:
        violations = pin.validate(tree, source)
        assert violations == (), (
            f"{pin.invariant_name} drift: {violations}"
        )


def test_authority_asymmetry_forbidden_imports():
    """The bridge MUST NOT import any decision-authority module.
    Composes only m10 substrates + stdlib."""
    src_path = Path(
        "backend/core/ouroboros/governance/m10/"
        "m10_producer_bridge.py"
    )
    source = src_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.iron_gate",
        "backend.core.ouroboros.governance.policy",
        "backend.core.ouroboros.governance.policy_engine",
        "backend.core.ouroboros.governance.candidate_generator",
        "backend.core.ouroboros.governance.urgency_router",
        "backend.core.ouroboros.governance.change_engine",
        "backend.core.ouroboros.governance.semantic_guardian",
        "backend.core.ouroboros.governance.auto_committer",
        "backend.core.ouroboros.governance.risk_tier_floor",
        "backend.core.ouroboros.governance.tool_executor",
        "backend.core.ouroboros.governance.plan_generator",
        "backend.core.ouroboros.governance.providers",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert mod not in forbidden, (
                f"forbidden import: {mod!r} at line "
                f"{getattr(node, 'lineno', '?')}"
            )


# ---------------------------------------------------------------------------
# REPL /m10 fire integration
# ---------------------------------------------------------------------------


def test_repl_fire_dispatched_not_unknown(monkeypatch):
    """``/m10 fire`` is recognized — does NOT fall through to
    the unknown-subcommand path."""
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    _enable(monkeypatch)
    result = dispatch_m10_command("/m10 fire")
    assert result.matched is True
    # Whether ok=True or False depends on miner state (likely no
    # patterns from a fresh fixture); the key invariant is that
    # the dispatcher recognized the subcommand.
    assert "unknown subcommand" not in result.text


def test_repl_fire_master_gate(monkeypatch):
    """Master OFF → /m10 fire still recognized but result
    reflects disabled outcome."""
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    # Master flag NOT set → bridge surfaces gated-off.
    result = dispatch_m10_command("/m10 fire")
    # The /m10 dispatcher gates ALL non-help verbs at the
    # master-flag layer with a friendly "disabled" message,
    # so we see the dispatcher-level gate rather than the
    # bridge-level gate. Both are correct behavior.
    assert result.matched is True
    assert (
        "disabled" in result.text.lower()
        or "JARVIS_M10_ARCH_PROPOSER_ENABLED" in result.text
    )


def test_repl_help_text_includes_fire():
    """Operator discoverability — /m10 help must list the fire
    subcommand."""
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    result = dispatch_m10_command("/m10 help")
    assert result.ok is True
    assert "fire" in result.text
    assert "operator-initiated" in result.text.lower()


def test_repl_fire_renders_structured_result(monkeypatch):
    """The /m10 fire renderer surfaces every load-bearing field
    from MineCycleResult."""
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    _enable(monkeypatch)
    result = dispatch_m10_command("/m10 fire")
    assert result.matched is True
    # Whether ok depends on miner state; structure should be
    # there either way.
    for token in (
        "outcome=",
        "proposals_emitted_count:",
        "rows_stored:",
        "elapsed_s:",
    ):
        assert token in result.text, (
            f"missing {token!r} in:\n{result.text}"
        )


def test_repl_dispatcher_signature_compatible_with_registry():
    """The /m10 dispatcher must satisfy the canonical
    repl_dispatch_registry signature validator (single
    positional ``line: str``)."""
    import inspect
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    sig = inspect.signature(dispatch_m10_command)
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "line"
    # Not async — registry calls sync.
    assert not inspect.iscoroutinefunction(dispatch_m10_command)

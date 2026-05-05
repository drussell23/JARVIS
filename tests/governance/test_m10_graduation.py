"""M10 Slice 5 — graduation regression spine.

Surfaces verified:

  * ``ProposalStore`` (JSONL ledger, cross-process tear-safe)
  * ``GET /observability/m10`` + ``/observability/m10/proposal/{id}``
  * ``/m10`` REPL (5 subcommands)
  * SSE ``m10_proposal_emitted`` event vocabulary
  * 8 ``shipped_code_invariants`` AST pins all PASS
  * 5 FlagRegistry seeds present + correct types/categories
  * **Master flag STAYS default-FALSE** per §30.5.2 operator binding

Per PRD §32.4 Slice 5: master flag JARVIS_M10_ARCH_PROPOSER_ENABLED
does NOT graduate default-true; flips only after a 30+ proposal-
acceptance audit. The graduation arc ships SURFACES (REPL / HTTP /
SSE / pins / seeds) — not the production default.
"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Master flag stays default-FALSE (§30.5.2 operator binding)
# ---------------------------------------------------------------------------


def test_master_flag_default_is_false(monkeypatch):
    """The master flag must remain default-FALSE — Slice 5
    graduates surfaces, NOT the production default."""
    monkeypatch.delenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.m10.primitives import (
        m10_arch_proposer_enabled,
    )
    assert m10_arch_proposer_enabled() is False


@pytest.mark.parametrize(
    "value", ["true", "1", "yes", "on", "TRUE"],
)
def test_master_flag_truthy_values(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", value,
    )
    from backend.core.ouroboros.governance.m10.primitives import (
        m10_arch_proposer_enabled,
    )
    assert m10_arch_proposer_enabled() is True


@pytest.mark.parametrize(
    "value", ["false", "0", "no", "off", ""],
)
def test_master_flag_falsy_values(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", value,
    )
    from backend.core.ouroboros.governance.m10.primitives import (
        m10_arch_proposer_enabled,
    )
    assert m10_arch_proposer_enabled() is False


# ---------------------------------------------------------------------------
# ProposalStore
# ---------------------------------------------------------------------------


def _make_stored(pid="op-1", phase="awaiting_approval"):
    from backend.core.ouroboros.governance.m10.proposal_store import (
        StoredProposal,
    )
    return StoredProposal(
        proposal_id=pid,
        kind="new_sensor",
        phase=phase,
        pattern_signature="sig-abcd",
        detection_evidence=("ev1", "ev2"),
        proposed_module_path="path/to/x.py",
        proposed_class_name="X",
        proposed_ast_pin_name="x_self_pin",
        pr_url="https://example.com/pr/1",
        pr_branch=f"ouroboros/m10/{pid}",
    )


def test_proposal_store_append_and_read(tmp_path, monkeypatch):
    target = tmp_path / "proposals.jsonl"
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH", str(target),
    )
    from backend.core.ouroboros.governance.m10.proposal_store import (
        append_proposal,
        read_all_proposals,
    )
    p1 = _make_stored("op-1")
    p2 = _make_stored("op-2", phase="graduated")
    assert append_proposal(p1) is True
    assert append_proposal(p2) is True
    rows = read_all_proposals(limit=10)
    assert len(rows) == 2
    ids = {r.proposal_id for r in rows}
    assert ids == {"op-1", "op-2"}


def test_proposal_store_read_missing_file(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "absent.jsonl"),
    )
    from backend.core.ouroboros.governance.m10.proposal_store import (
        read_all_proposals,
    )
    assert read_all_proposals(limit=5) == ()


def test_proposal_store_find_by_id(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "p.jsonl"),
    )
    from backend.core.ouroboros.governance.m10.proposal_store import (
        append_proposal,
        find_proposal_by_id,
    )
    append_proposal(_make_stored("op-A"))
    append_proposal(_make_stored("op-B", phase="failed"))
    found = find_proposal_by_id("op-A")
    assert found is not None
    assert found.proposal_id == "op-A"
    assert find_proposal_by_id("nonexistent") is None


def test_proposal_store_find_returns_most_recent(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "p.jsonl"),
    )
    from backend.core.ouroboros.governance.m10.proposal_store import (
        append_proposal,
        find_proposal_by_id,
    )
    append_proposal(_make_stored("op-X", phase="detecting"))
    append_proposal(
        _make_stored("op-X", phase="graduated"),
    )
    found = find_proposal_by_id("op-X")
    assert found is not None
    assert found.phase == "graduated"


def test_proposal_store_phase_histogram(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "p.jsonl"),
    )
    from backend.core.ouroboros.governance.m10.proposal_store import (
        aggregate_phase_histogram,
        append_proposal,
    )
    append_proposal(
        _make_stored("op-1", phase="graduated"),
    )
    append_proposal(_make_stored("op-2", phase="failed"))
    append_proposal(_make_stored("op-3", phase="graduated"))
    hist = aggregate_phase_histogram()
    assert hist.get("graduated") == 2
    assert hist.get("failed") == 1


def test_proposal_store_list_pending(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "p.jsonl"),
    )
    from backend.core.ouroboros.governance.m10.proposal_store import (
        append_proposal,
        list_pending_proposals,
    )
    append_proposal(
        _make_stored("op-1", phase="awaiting_approval"),
    )
    append_proposal(
        _make_stored("op-2", phase="graduated"),
    )
    append_proposal(
        _make_stored("op-3", phase="awaiting_merge"),
    )
    pending = list_pending_proposals(limit=10)
    pending_ids = {p.proposal_id for p in pending}
    assert pending_ids == {"op-1", "op-3"}


def test_proposal_store_corrupt_lines_skipped(
    tmp_path, monkeypatch,
):
    target = tmp_path / "p.jsonl"
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH", str(target),
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "garbage not json\n"
        + json.dumps(_make_stored("op-good").to_dict())
        + "\n"
        + "{}\n",  # missing required fields
        encoding="utf-8",
    )
    from backend.core.ouroboros.governance.m10.proposal_store import (
        read_all_proposals,
    )
    rows = read_all_proposals(limit=10)
    assert len(rows) == 1
    assert rows[0].proposal_id == "op-good"


# ---------------------------------------------------------------------------
# REPL — /m10 dispatcher
# ---------------------------------------------------------------------------


def test_repl_help_bypasses_master_gate(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    r = dispatch_m10_command("/m10 help")
    assert r.matched
    assert r.ok
    assert "Subcommands:" in r.text


def test_repl_disabled_returns_friendly_error(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    r = dispatch_m10_command("/m10 stats")
    assert r.matched
    assert not r.ok
    assert "disabled" in r.text


def test_repl_unmatched_line(monkeypatch):
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    r = dispatch_m10_command("/decisions recent")
    assert r.matched is False


def test_repl_pending_empty(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "p.jsonl"),
    )
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    r = dispatch_m10_command("/m10 pending")
    assert r.ok
    assert "no proposals awaiting" in r.text


def test_repl_show_missing_id(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    r = dispatch_m10_command("/m10 show")
    assert not r.ok
    assert "missing proposal_id" in r.text


def test_repl_show_existing(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "p.jsonl"),
    )
    from backend.core.ouroboros.governance.m10.proposal_store import (
        append_proposal,
    )
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    append_proposal(_make_stored("op-Z", phase="graduated"))
    r = dispatch_m10_command("/m10 show op-Z")
    assert r.ok
    assert "kind:" in r.text
    assert "graduated" in r.text


def test_repl_history_limits(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "p.jsonl"),
    )
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    r = dispatch_m10_command("/m10 history")
    assert r.ok


def test_repl_unknown_subcommand(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    r = dispatch_m10_command("/m10 fnord")
    assert not r.ok
    assert "unknown subcommand" in r.text


def test_repl_stats(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "p.jsonl"),
    )
    from backend.core.ouroboros.governance.m10.proposal_store import (
        append_proposal,
    )
    from backend.core.ouroboros.governance.m10.repl import (
        dispatch_m10_command,
    )
    append_proposal(_make_stored("op-1", phase="graduated"))
    append_proposal(_make_stored("op-2", phase="failed"))
    r = dispatch_m10_command("/m10 stats")
    assert r.ok
    assert "graduated" in r.text
    assert "failed" in r.text


# ---------------------------------------------------------------------------
# Observability — HTTP routes
# ---------------------------------------------------------------------------


def test_observability_register_routes_smoke():
    """Sanity check — register_routes does not raise."""
    pytest.importorskip("aiohttp")
    from aiohttp import web

    from backend.core.ouroboros.governance.m10.observability import (
        register_routes,
    )
    app = web.Application()
    register_routes(app)
    paths = [r.resource.canonical for r in app.router.routes()]  # type: ignore[attr-defined]
    assert "/observability/m10" in paths
    assert any(
        "proposal" in p for p in paths
    )


def test_observability_overview_disabled_returns_503(
    monkeypatch,
):
    pytest.importorskip("aiohttp")
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.m10.observability import (
        _M10RoutesHandler,
    )
    handler = _M10RoutesHandler()

    class FakeRequest:
        query = {}
        match_info = {}

    response = asyncio.run(handler.handle_overview(FakeRequest()))
    assert response.status == 503


def test_observability_overview_enabled_returns_200(
    tmp_path, monkeypatch,
):
    pytest.importorskip("aiohttp")
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "p.jsonl"),
    )
    from backend.core.ouroboros.governance.m10.observability import (
        _M10RoutesHandler,
    )
    handler = _M10RoutesHandler()

    class FakeRequest:
        query = {}
        match_info = {}

    response = asyncio.run(handler.handle_overview(FakeRequest()))
    assert response.status == 200
    body = json.loads(response.body)
    assert body["schema_version"].startswith(
        "m10_proposal_store",
    )
    assert (
        body["sse_event_type"] == "m10_proposal_emitted"
    )


def test_observability_proposal_detail_404(
    tmp_path, monkeypatch,
):
    pytest.importorskip("aiohttp")
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "p.jsonl"),
    )
    from backend.core.ouroboros.governance.m10.observability import (
        _M10RoutesHandler,
    )
    handler = _M10RoutesHandler()

    class FakeRequest:
        query = {}
        match_info = {"proposal_id": "nope"}

    response = asyncio.run(
        handler.handle_proposal_detail(FakeRequest()),
    )
    assert response.status == 404


def test_observability_proposal_detail_200(
    tmp_path, monkeypatch,
):
    pytest.importorskip("aiohttp")
    monkeypatch.setenv(
        "JARVIS_M10_ARCH_PROPOSER_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_M10_PROPOSALS_PATH",
        str(tmp_path / "p.jsonl"),
    )
    from backend.core.ouroboros.governance.m10.observability import (
        _M10RoutesHandler,
    )
    from backend.core.ouroboros.governance.m10.proposal_store import (
        append_proposal,
    )
    append_proposal(_make_stored("op-PR1"))
    handler = _M10RoutesHandler()

    class FakeRequest:
        query = {}
        match_info = {"proposal_id": "op-PR1"}

    response = asyncio.run(
        handler.handle_proposal_detail(FakeRequest()),
    )
    assert response.status == 200


# ---------------------------------------------------------------------------
# SSE event vocabulary
# ---------------------------------------------------------------------------


def test_sse_event_type_constant_present():
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        EVENT_TYPE_M10_PROPOSAL_EMITTED,
    )
    assert (
        EVENT_TYPE_M10_PROPOSAL_EMITTED
        == "m10_proposal_emitted"
    )


def test_sse_publish_master_off_returns_none(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_IDE_STREAM_ENABLED", "false",
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        publish_m10_proposal_event,
    )
    result = publish_m10_proposal_event(
        proposal_id="op-1",
        kind="new_sensor",
        terminal_phase="awaiting_approval",
        ts_unix=1.0,
    )
    assert result is None


def test_sse_publish_master_on_does_not_raise(
    monkeypatch,
):
    """Master-on publish path is best-effort. With no
    subscribers, the broker returns None — assert no
    exception escapes."""
    monkeypatch.setenv(
        "JARVIS_IDE_STREAM_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
        publish_m10_proposal_event,
    )
    # Should not raise; return value is None when no
    # subscribers are connected (best-effort publisher
    # contract — mirrors publish_decision_drift_event).
    publish_m10_proposal_event(
        proposal_id="op-1",
        kind="new_sensor",
        terminal_phase="graduated",
        pr_url="https://example.com/pr/1",
        pr_branch="ouroboros/m10/op-1",
        cost_usd=0.012,
        ts_unix=1.0,
    )


# ---------------------------------------------------------------------------
# AST shipped_code_invariants pins (8 total)
# ---------------------------------------------------------------------------


_EXPECTED_M10_PIN_NAMES = {
    "m10_synthesizer_uses_quorum",
    "m10_lifecycle_uses_orange_pr",
    "m10_forced_risk_tier_constant",
    "m10_master_flag_stays_default_false",
    "m10_primitives_authority_asymmetry",
    "m10_synthesizer_authority_asymmetry",
    "m10_lifecycle_authority_asymmetry",
    "m10_unhandled_pattern_miner_authority_asymmetry",
}


def test_all_m10_pins_registered():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        list_shipped_code_invariants,
    )
    registered_names = {
        inv.invariant_name
        for inv in list_shipped_code_invariants()
        if inv.invariant_name.startswith("m10_")
    }
    missing = _EXPECTED_M10_PIN_NAMES - registered_names
    assert not missing, (
        f"missing M10 pins: {missing}"
    )


def test_all_m10_pins_pass_validation():
    from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
        validate_all,
    )
    violations = validate_all()
    m10_violations = [
        v for v in violations
        if v.invariant_name.startswith("m10_")
    ]
    assert not m10_violations, (
        "M10 pin violations: "
        + "; ".join(
            f"{v.invariant_name}: {v.violation}"
            for v in m10_violations
        )
    )


# ---------------------------------------------------------------------------
# FlagRegistry seeds
# ---------------------------------------------------------------------------


_EXPECTED_M10_FLAGS = {
    "JARVIS_M10_ARCH_PROPOSER_ENABLED",
    "JARVIS_M10_ADAPTIVE_MIN_THRESHOLD",
    "JARVIS_M10_ADAPTIVE_CONFIDENCE",
    "JARVIS_M10_MAX_DAILY",
    "JARVIS_M10_APPROVAL_TIMEOUT_S",
}


def test_m10_flag_seeds_present():
    from backend.core.ouroboros.governance.flag_registry_seed import (
        SEED_SPECS,
    )
    seeded = {spec.name for spec in SEED_SPECS}
    missing = _EXPECTED_M10_FLAGS - seeded
    assert not missing, (
        f"missing M10 FlagRegistry seeds: {missing}"
    )


def test_m10_master_flag_seed_default_is_false():
    from backend.core.ouroboros.governance.flag_registry_seed import (
        SEED_SPECS,
    )
    master = next(
        (
            s for s in SEED_SPECS
            if s.name == "JARVIS_M10_ARCH_PROPOSER_ENABLED"
        ),
        None,
    )
    assert master is not None
    assert master.default is False, (
        "JARVIS_M10_ARCH_PROPOSER_ENABLED MUST be "
        "default-FALSE per §30.5.2 (operator-pinned)"
    )


def test_m10_max_daily_seed_default_is_5():
    from backend.core.ouroboros.governance.flag_registry_seed import (
        SEED_SPECS,
    )
    flag = next(
        (
            s for s in SEED_SPECS
            if s.name == "JARVIS_M10_MAX_DAILY"
        ),
        None,
    )
    assert flag is not None
    assert flag.default == 5


# ---------------------------------------------------------------------------
# /help register_verbs auto-discovery hook
# ---------------------------------------------------------------------------


def test_repl_register_verbs_returns_one():
    """register_verbs must succeed without raising and return
    1 (one verb registered)."""
    from backend.core.ouroboros.governance.m10.repl import (
        register_verbs,
    )

    class FakeRegistry:
        def __init__(self):
            self.entries = []

        def register(self, spec):
            self.entries.append(spec)

    registry = FakeRegistry()
    n = register_verbs(registry)
    assert n == 1
    assert len(registry.entries) == 1
    assert registry.entries[0].name == "/m10"

"""The panel that renders what the organism is NOT doing.

Every other cockpit surface shows activity. This one shows absence, because
absence is this system's characteristic failure: the micro-fix ran 532 times
and repaired nothing, the retry ladder never regenerated once, 43 capability
switches are dark -- and every existing surface showed a healthy organism
throughout.

On its first real data the panel caught `api_grounding_gate` at invoked=48,
effective=0, which turned out to be a gate reading `ctx.plan` (a field that
does not exist) and adjudicating an empty string.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.core.ouroboros.cli.ov_reachability_panel import (
    CapabilityRow,
    ReachabilityModel,
    aggregate,
    load_model,
    max_read_bytes,
    poll_interval_s,
    render_panel,
    render_rows,
    watch,
)


def _rec(cap, tier, detail=""):
    return json.dumps({
        "at": 1.0, "capability": cap, "tier": tier, "op_id": "o", "detail": detail,
    })


def _ledger(tmp_path, records):
    path = tmp_path / "reach.jsonl"
    path.write_text("\n".join(records) + "\n")
    return path


# ---------------------------------------------------------------------------
# The verdicts
# ---------------------------------------------------------------------------


def test_invoked_without_effect_is_inert():
    """The alarm. This exact shape was live at 48 invocations."""
    row = CapabilityRow("gate", registered=0, invoked=48, effective=0)
    assert row.inert is True
    assert row.verdict == "INERT"
    assert row.yield_pct == 0.0


def test_registered_without_invocation_is_dormant():
    row = CapabilityRow("cap", registered=3, invoked=0, effective=0)
    assert row.dormant is True
    assert row.verdict == "DORMANT"


def test_effective_is_live():
    row = CapabilityRow("cap", registered=22, invoked=29, effective=5)
    assert row.verdict == "LIVE"
    assert row.yield_pct == pytest.approx(17.24, abs=0.1)


def test_yield_never_divides_by_zero():
    assert CapabilityRow("cap").yield_pct == 0.0


# ---------------------------------------------------------------------------
# Folding the ledger
# ---------------------------------------------------------------------------


def test_aggregate_counts_each_tier():
    model = aggregate([
        _rec("a", "registered"), _rec("a", "invoked"),
        _rec("a", "invoked"), _rec("a", "effective"),
    ])
    row = model.rows[0]
    assert (row.registered, row.invoked, row.effective) == (1, 2, 1)


def test_malformed_lines_are_skipped_not_fatal():
    """The ledger is appended by a live process; a torn final line is an
    ordinary condition, not a reason to blank the panel."""
    model = aggregate([_rec("a", "invoked"), "{not json", "", _rec("a", "effective")])
    assert model.rows[0].invoked == 1
    assert model.rows[0].effective == 1


def test_records_without_capability_or_tier_are_ignored():
    model = aggregate([json.dumps({"tier": "invoked"}), json.dumps({"capability": "x"})])
    assert model.rows == []


def test_last_detail_is_carried():
    model = aggregate([_rec("a", "effective", detail="ast_similarity=0.4")])
    assert "ast_similarity" in model.rows[0].last_detail


# ---------------------------------------------------------------------------
# Ordering is the message
# ---------------------------------------------------------------------------


def test_inert_sorted_by_invocations_descending():
    """A capability invoked 500 times to no effect wastes more than one
    invoked twice."""
    model = aggregate(
        [_rec("small", "invoked")] * 2 + [_rec("big", "invoked")] * 50
    )
    assert [r.capability for r in model.inert] == ["big", "small"]


def test_inert_comes_before_live_in_the_rendering():
    """A capability that runs and changes nothing is costing compute to do
    nothing, which is worse than one that never runs."""
    model = aggregate([
        _rec("dead", "invoked"),
        _rec("alive", "invoked"), _rec("alive", "effective"),
    ])
    text = "\n".join(render_rows(model))
    assert text.index("dead") < text.index("alive")


# ---------------------------------------------------------------------------
# Reading, decoupled and non-blocking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_load_model_reads_a_real_ledger(tmp_path):
    path = _ledger(tmp_path, [
        _rec("cap", "invoked"), _rec("cap", "invoked"), _rec("cap", "effective"),
    ])
    model = await load_model(path)
    assert model.rows[0].invoked == 2
    assert model.rows[0].effective == 1


@pytest.mark.asyncio
async def test_missing_ledger_reports_rather_than_raises(tmp_path):
    """A panel that dies on a missing file teaches operators to distrust
    the surface that would have warned them."""
    model = await load_model(tmp_path / "absent.jsonl")
    assert model.rows == []
    assert "no ledger" in model.error


@pytest.mark.asyncio
async def test_unset_path_says_so(monkeypatch):
    monkeypatch.delenv("JARVIS_REACHABILITY_LEDGER_PATH", raising=False)
    model = await load_model(None)
    assert "not recording execution provenance" in model.error


@pytest.mark.asyncio
async def test_tail_is_capped_so_a_huge_ledger_is_not_reread(tmp_path, monkeypatch):
    """An append-only file grows without bound; re-reading 100MB every two
    seconds would make the panel a worse I/O offender than anything it
    reports."""
    monkeypatch.setenv("JARVIS_REACHABILITY_PANEL_MAX_BYTES", "400")
    path = _ledger(tmp_path, [_rec("cap", "invoked")] * 200)
    model = await load_model(path)
    assert model.truncated is True
    assert 0 < model.rows[0].invoked < 200


@pytest.mark.asyncio
async def test_a_directory_in_place_of_a_ledger_does_not_raise(tmp_path):
    (tmp_path / "reach.jsonl").mkdir()
    model = await load_model(tmp_path / "reach.jsonl")
    assert model.rows == []
    assert model.error


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_rows_never_raises_on_an_empty_model():
    assert render_rows(ReachabilityModel())


def test_render_panel_never_raises():
    render_panel(aggregate([_rec("cap", "invoked")]))
    render_panel(ReachabilityModel(error="nothing here"))


@pytest.mark.asyncio
async def test_watch_is_bounded_for_tests(tmp_path):
    path = _ledger(tmp_path, [_rec("cap", "invoked")])
    await asyncio.wait_for(watch(path, iterations=1), timeout=10)


def test_poll_interval_has_a_floor(monkeypatch):
    """Below a quarter second the panel is polling the disk harder than the
    organism writes it."""
    monkeypatch.setenv("JARVIS_REACHABILITY_PANEL_POLL_S", "0.001")
    assert poll_interval_s() >= 0.25


def test_nonsense_env_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_REACHABILITY_PANEL_POLL_S", "banana")
    monkeypatch.setenv("JARVIS_REACHABILITY_PANEL_MAX_BYTES", "banana")
    assert poll_interval_s() > 0
    assert max_read_bytes() > 0


def test_panel_imports_nothing_that_can_mutate_governance():
    """Read-only by construction: the isolation is the process boundary and
    the file, not a convention someone has to keep.

    Asserted over the module's IMPORTS rather than its text. The substring
    version of this test matched the word in its own docstring -- the pin
    class ci/string_pin_ratchet exists to stop, written by the person who
    added the ratchet.
    """
    import ast
    from pathlib import Path

    from backend.core.ouroboros.cli import ov_reachability_panel as panel

    tree = ast.parse(Path(panel.__file__).read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)

    forbidden = [
        m for m in imported if "governance" in m or "orchestrator" in m
    ]
    assert not forbidden, f"panel imports mutable governance: {forbidden}"

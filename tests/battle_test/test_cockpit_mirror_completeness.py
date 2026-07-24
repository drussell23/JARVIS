"""Cockpit completeness — every operator-visible daemon surface mirrors
to attached ov terminals (audit 2026-07-23).

Covers the surfaces the audit found dark (class B): op-block chrome +
receipts, synthesis receipt, breadcrumb chokepoints (wiring invariants),
Iron Gate visibility, NOTIFY_APPLY countdown notice, inline-prompt
renderer mirroring.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.battle_test.serpent_flow import SerpentFlow


@pytest.fixture()
def flow():
    sf = SerpentFlow(session_id="t", branch_name="b")
    sf._mirrored = []
    sf.markup_mirror = sf._mirrored.append
    return sf


def test_op_lifecycle_chrome_mirrors(flow):
    """Open header, interior lines, receipt, and close tally all reach
    the mirror — a cockpit sees the op's full story."""
    flow.op_started("op-abc123", "fix the tests", ["a.py"], "SAFE_AUTO",
                    sensor="test_failure")
    flow.op_completed("op-abc123", files_changed=[], cost_usd=0.01)
    joined = "\n".join(str(m) for m in flow._mirrored)
    assert "test_failure" in joined          # open action line
    assert "op-" in joined                   # receipt line
    assert "💰" in joined                    # close tally stats


def test_streaming_receipt_mirrors(flow):
    flow.show_streaming_start("doubleword", op_id="op-abc123")
    for _ in range(1234):
        flow.show_streaming_token("x")
    flow.show_streaming_end()
    joined = "\n".join(str(m) for m in flow._mirrored)
    assert "Generated" in joined and "tokens" in joined


@pytest.mark.asyncio
async def test_notify_apply_countdown_notice_mirrors(flow):
    ok = await flow.show_notify_apply_preview(
        op_id="op-abc123", reason="single_file_small_diff",
        changes=[], delay_s=0.0, cancel_check=None,
    )
    joined = "\n".join(str(m) for m in flow._mirrored)
    assert "NOTIFY_APPLY" in joined
    assert "/reject" in joined
    assert isinstance(ok, bool)


@pytest.mark.asyncio
async def test_iron_gate_headless_auto_approve_is_visible(flow, monkeypatch):
    """§7: the headless gate's auto-approve decision must be visible to
    attached cockpits — never a silent decision."""
    import backend.core.ouroboros.battle_test.serpent_flow as sfm
    monkeypatch.setattr(
        sfm, "_headless_auto_approve_reason", lambda: "no-tty", 
    )
    approved = await flow.request_execution_permission(
        op_id="op-abc123", description="test change",
        target_files=["a.py"], diff_text="",
    )
    assert approved is True
    joined = "\n".join(str(m) for m in flow._mirrored)
    assert "auto-approved" in joined


def test_breadcrumb_chokepoints_mirror_wiring():
    """Wiring invariants: the TWO event chokepoints (registry router +
    provider failover listener) and the inline-prompt renderer all call
    the mirror — one seam each lights every current/future event."""
    src = Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    # registry router mirrors the styled breadcrumb
    router = src.split("def _event_breadcrumb_router")[1].split("\n    async def ")[0]
    assert "_mirror_markup(styled)" in router
    # provider failover listener mirrors
    prov = src.split("def _provider_breadcrumb_listener")[1].split("\n    async def ")[0]
    assert "_mirror_markup(styled)" in prov
    # inline prompt renderer wires a mirroring print_fn
    assert "attach_phase_boundary_renderer(_prompt_print)" in src


def test_intent_narration_mirror_wiring():
    src = Path(
        "backend/core/ouroboros/battle_test/serpent_flow.py"
    ).read_text()
    assert src.count("💭") >= 1
    assert "self._mirror_markup(f\"  {_r.markup}\")" in src

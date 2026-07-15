"""P0.6 — perception graduation: the one-switch profile + the end-to-end proof.

The graduation EVIDENCE for the whole P0 board, reproducible without paid
providers (the isomorphic-local-before-remote discipline): drive the REAL
perception pipeline — WorkOrderSensor + the latent-defect AST detector +
_compute_priority + substance_ledger — against a synthetic Run-25-like repo,
and show the substance ratio lift from ~0 (only trivia) to substantial (real
work orders + real defects entering the dispatched stream and winning the
queue).

Plus the arming profile: it applies the coherent flag-set, preserves operator
overrides, and its preflight catches the classic dead-arming traps.
"""
from __future__ import annotations

import os

import pytest

from backend.core.ouroboros.governance import perception_profile as PP
from backend.core.ouroboros.governance import substance_ledger as SL
from backend.core.ouroboros.governance.intake import unified_intake_router as R
from backend.core.ouroboros.governance.intake.sensors.work_order_sensor import (
    WorkOrderSensor,
)
from backend.core.ouroboros.governance.intake.sensors.deep_analysis_sensor import (
    analyze_latent_defects,
)
from backend.core.ouroboros.governance.intake.intent_envelope import (
    make_envelope,
)


@pytest.fixture
def repo(tmp_path):
    """A Run-25-like repo: a real bug + a roadmap item naming it."""
    (tmp_path / "backend").mkdir()
    # A real latent defect (mutable default arg) O+V should perceive.
    (tmp_path / "backend" / "buggy.py").write_text(
        "def handler(items=[]):\n    items.append(1)\n    return items\n"
    )
    (tmp_path / "requirements.txt").write_text("torch==2.0\n")
    (tmp_path / ".superpowers" / "sdd").mkdir(parents=True)
    (tmp_path / ".superpowers" / "sdd" / "progress.md").write_text(
        "SLICE DONE. NEXT: fix the mutable default in backend/buggy.py\n"
    )
    return tmp_path


class _Cap:
    def __init__(self):
        self.got = []

    async def ingest(self, e):
        self.got.append(e)
        return "ok"


def _trivia_env():
    """What O+V self-selected in Run #25: an annotation-grade op on a
    line-grammar file (cosmetic band)."""
    return make_envelope(
        source="ai_miner", description="append a comment to requirements",
        target_files=("requirements.txt",), repo="jarvis", confidence=0.5,
        urgency="low", evidence={}, requires_human_ack=False,
    )


def _defect_envs(repo):
    """Build the envelopes DeepAnalysisSensor would emit for real defects."""
    out = []
    for d in analyze_latent_defects(repo_root=repo):
        out.append(make_envelope(
            source="exploration",
            description=f"[deep_analysis/{d.category}] {d.description}",
            target_files=(d.file,) if d.file else (), repo="jarvis",
            confidence=d.confidence, urgency=d.urgency,
            evidence={"deep_analysis_category": d.category}, requires_human_ack=False,
        ))
    return out


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("JARVIS_WORK_ORDER_SENSOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_DEEP_ANALYSIS_DEFECT_ENABLED", "1")
    monkeypatch.setenv("JARVIS_INTAKE_VALUE_PRIORITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_INTAKE_VALUE_PRIORITY_SHADOW", "false")  # enforce
    monkeypatch.setenv("JARVIS_SIGNAL_VALUE_ROUTING_ENABLED", "true")
    SL.reset_default_substance_ledger_for_tests()
    yield
    SL.reset_default_substance_ledger_for_tests()


# ── THE graduation proof: substance ratio lift ───────────────────────

@pytest.mark.asyncio
async def test_perception_lifts_substance_ratio(repo, armed):
    def _drive(envs):
        SL.reset_default_substance_ledger_for_tests()
        for e in envs:
            R._compute_priority(e, repo_root=repo)  # stamps value_band, etc.
            SL.record_dispatch(e.evidence)
        return SL.substance_snapshot()

    # BASELINE — O+V self-selects only trivia (the Run #25 signature).
    baseline = _drive([_trivia_env() for _ in range(5)])

    # ARMED — the operator's roadmap + real defects ENTER the stream.
    cap = _Cap()
    work_orders = await WorkOrderSensor(
        repo="jarvis", router=cap, project_root=repo,
        seen_ledger_path=repo / ".jarvis" / "wo_seen.json",
    ).scan_once()
    defects = _defect_envs(repo)
    assert work_orders, "work order sensor found no roadmap item"
    assert defects, "latent-defect detector found no real bug"
    armed_snap = _drive(
        list(work_orders) + defects + [_trivia_env() for _ in range(5)]
    )

    # The proof: near-zero → substantial.
    assert baseline["substance_ratio"] < 0.2
    assert armed_snap["substance_ratio"] > baseline["substance_ratio"]
    assert armed_snap["substantive"] >= 2  # work order(s) + defect(s)


@pytest.mark.asyncio
async def test_substance_signals_outrank_trivia_in_queue(repo, armed):
    """Under enforce, a real work order / defect out-prioritizes trivia — the
    behavior change P0.1 delivers, end-to-end through the real sensors."""
    cap = _Cap()
    work_orders = await WorkOrderSensor(
        repo="jarvis", router=cap, project_root=repo,
        seen_ledger_path=repo / ".jarvis" / "wo_seen.json",
    ).scan_once()
    wo_prio, _ = R._compute_priority(work_orders[0], repo_root=repo)
    defect = _defect_envs(repo)[0]
    def_prio, _ = R._compute_priority(defect, repo_root=repo)
    trivia_prio, _ = R._compute_priority(_trivia_env(), repo_root=repo)
    assert wo_prio < trivia_prio     # roadmap wins
    assert def_prio < trivia_prio    # real defect wins


# ── the arming profile + preflight ───────────────────────────────────

def test_arm_applies_coherent_flags(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("JARVIS_INTAKE_VALUE", "JARVIS_WORK_ORDER",
                         "JARVIS_DEEP_ANALYSIS", "JARVIS_MEMORY_REPUTATION")):
            monkeypatch.delenv(k, raising=False)
    applied = PP.arm_perception_environ(enforce=True, overwrite=True)
    assert applied["JARVIS_INTAKE_VALUE_PRIORITY_SHADOW"] == "false"  # enforce
    assert applied["JARVIS_WORK_ORDER_SENSOR_ENABLED"] == "true"
    assert applied["JARVIS_MEMORY_REPUTATION_BIAS_ENABLED"] == "true"
    assert PP.perception_preflight()["fully_armed"] is True


def test_shadow_variant_arms_value_priority_in_shadow():
    flags = PP.perception_flags(enforce=False)
    assert flags["JARVIS_INTAKE_VALUE_PRIORITY_SHADOW"] == "true"


def test_arm_preserves_operator_override(monkeypatch):
    monkeypatch.setenv("JARVIS_WORK_ORDER_SENSOR_ENABLED", "false")  # operator said no
    applied = PP.arm_perception_environ(enforce=True, overwrite=False)
    assert "JARVIS_WORK_ORDER_SENSOR_ENABLED" not in applied  # preserved
    assert os.environ["JARVIS_WORK_ORDER_SENSOR_ENABLED"] == "false"


def test_preflight_catches_bias_without_write(monkeypatch):
    for k in list(os.environ):
        if k.startswith("JARVIS_MEMORY_REPUTATION"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("JARVIS_MEMORY_REPUTATION_BIAS_ENABLED", "true")  # trap
    pre = PP.perception_preflight()
    assert any("INERT" in w for w in pre["warnings"])
    assert pre["fully_armed"] is False


def test_preflight_warns_when_no_substance_source(monkeypatch):
    for k in list(os.environ):
        if k.startswith(("JARVIS_WORK_ORDER", "JARVIS_DEEP_ANALYSIS")):
            monkeypatch.delenv(k, raising=False)
    pre = PP.perception_preflight()
    assert any("no substance SOURCE" in w for w in pre["warnings"])

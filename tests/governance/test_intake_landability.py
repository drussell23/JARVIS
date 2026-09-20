"""Intake asks VALIDATE's question before paying for GENERATE.

Measured on bt-2026-09-20-081946: 6 of the 7 ops that reached VALIDATE died on
``no_covering_test``, each after a full generation, each then trying to file a
test-synthesis prerequisite the signer refused as ``duplicate_id`` because the
previous soak had filed the identical pair. The one op that landed was the one
whose target had a covering test. The Sentinel's discovery path refuses such
work at selection; the work-order path had no gate at all.

These tests pin the behaviour: what is emitted, in what order, what is
withheld, and — as carefully — everything that must NOT be withheld.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance import operator_goal_sanction as ogs
from backend.core.ouroboros.governance.autonomy import goal_dag
from backend.core.ouroboros.governance.intake import landability as ld
from backend.core.ouroboros.governance.intake.sensors.work_order_sensor import (
    WorkOrderSensor,
)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "backend" / "covered.py").write_text("def f():\n    return 1\n")
    (tmp_path / "backend" / "bare.py").write_text("x = 2\n")
    (tmp_path / "tests" / "test_covered.py").write_text("def test_f():\n    pass\n")
    (tmp_path / ".superpowers" / "sdd").mkdir(parents=True)
    return tmp_path


def _resolver(repo):
    async def resolve(sources):
        return tuple(
            repo / "tests" / f"test_{p.stem}.py" for p in sources
            if (repo / "tests" / f"test_{p.stem}.py").is_file()
        )
    return resolve


@pytest.fixture
def filing(monkeypatch):
    """Let the triage believe it governs the tmp tree, and record every
    attempt to file — nothing here may ever reach a real roadmap."""
    calls = []
    monkeypatch.setattr(ogs, "governs", lambda root, path_override=None: True)
    monkeypatch.setattr(
        goal_dag, "file_substitution",
        lambda plan: calls.append(plan) or (None, None),
    )
    return calls


def _ids(*ids):
    return lambda: frozenset(ids)


A_BARE, B_BARE = "ov-dag-testsynth-bare", "ov-dag-repair-bare"


# ---------------------------------------------------------------------------
# The verdicts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_covered_source_is_landable(repo):
    v = await ld.LandabilityTriage(repo, resolver=_resolver(repo)).assess(
        ["backend/covered.py"],
    )
    assert v.state == ld.LANDABLE
    assert v.covering and v.dispatchable


@pytest.mark.asyncio
async def test_an_existing_test_is_its_own_cover(repo):
    v = await ld.LandabilityTriage(repo, resolver=_resolver(repo)).assess(
        ["tests/test_covered.py"],
    )
    assert v.state == ld.LANDABLE


@pytest.mark.asyncio
async def test_a_test_that_does_not_exist_yet_is_never_deferred(repo, filing):
    """Test creation is what unblocks everything queued behind it."""
    v = await ld.LandabilityTriage(
        repo, resolver=_resolver(repo), roadmap_ids=_ids(A_BARE, B_BARE),
    ).assess(["tests/test_bare.py"])
    assert v.state == ld.CREATES_TEST
    assert v.dispatchable
    assert filing == []


@pytest.mark.asyncio
async def test_uncovered_work_the_roadmap_owns_is_deferred(repo, filing):
    v = await ld.LandabilityTriage(
        repo, resolver=_resolver(repo), roadmap_ids=_ids(A_BARE, B_BARE),
    ).assess(["backend/bare.py"], "make bare better")
    assert v.state == ld.DEFERRED
    assert v.dispatchable is False
    assert filing and filing[0].original_description == "make bare better"


@pytest.mark.asyncio
async def test_uncovered_work_is_EMITTED_when_the_dependent_is_missing(repo, filing):
    """Only the prerequisite is on the roadmap. Withholding the item now
    would lose its work entirely — the repair goal is what carries it."""
    v = await ld.LandabilityTriage(
        repo, resolver=_resolver(repo), roadmap_ids=_ids(A_BARE),
    ).assess(["backend/bare.py"])
    assert v.state == ld.UNCOVERED
    assert v.dispatchable


@pytest.mark.asyncio
async def test_mixed_targets_land_on_the_covered_one(repo, filing):
    """VALIDATE passes a change when ANY changed file resolves to a test."""
    v = await ld.LandabilityTriage(repo, resolver=_resolver(repo)).assess(
        ["backend/bare.py", "backend/covered.py"],
    )
    assert v.state == ld.LANDABLE
    assert filing == []


# ---------------------------------------------------------------------------
# It fails OPEN, and unknown is never treated as uncovered
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_broken_oracle_emits_and_files_nothing(repo, filing):
    async def boom(_sources):
        raise RuntimeError("import map exploded")

    v = await ld.LandabilityTriage(
        repo, resolver=boom, roadmap_ids=_ids(A_BARE, B_BARE),
    ).assess(["backend/bare.py"])
    assert v.state == ld.UNCOVERED and v.dispatchable
    assert "unknown" in v.reason
    assert filing == [], "filed a test-synthesis goal on the strength of an error"


@pytest.mark.asyncio
async def test_a_hung_oracle_times_out_and_emits(repo, filing, monkeypatch):
    monkeypatch.setenv("JARVIS_INTAKE_LANDABILITY_RESOLVE_TIMEOUT_S", "0.05")

    async def hang(_sources):
        await asyncio.sleep(30)

    v = await ld.LandabilityTriage(repo, resolver=hang).assess(["backend/bare.py"])
    assert v.dispatchable and filing == []


@pytest.mark.asyncio
async def test_substitution_disabled_emits(repo, filing, monkeypatch):
    monkeypatch.setattr(goal_dag, "plan_substitution", lambda **_k: None)
    v = await ld.LandabilityTriage(
        repo, resolver=_resolver(repo), roadmap_ids=_ids(A_BARE, B_BARE),
    ).assess(["backend/bare.py"])
    assert v.state == ld.UNCOVERED and filing == []


@pytest.mark.asyncio
async def test_a_tree_the_roadmap_does_not_govern_never_files(repo, monkeypatch):
    """The signer resolves the roadmap from ITS OWN location. A triage rooted
    in a tmp tree or a worktree must not sign that tree's paths into it."""
    calls = []
    monkeypatch.setattr(goal_dag, "file_substitution", lambda p: calls.append(p))
    v = await ld.LandabilityTriage(
        repo, resolver=_resolver(repo), roadmap_ids=_ids(A_BARE, B_BARE),
    ).assess(["backend/bare.py"])
    assert ogs.governs(repo) is False
    assert v.state == ld.UNCOVERED and v.dispatchable
    assert calls == []


@pytest.mark.parametrize("targets", [[], [""], None, ["nope/missing.py"]])
@pytest.mark.asyncio
async def test_degenerate_targets_emit(repo, filing, targets):
    v = await ld.LandabilityTriage(repo, resolver=_resolver(repo)).assess(targets)
    assert v.dispatchable and filing == []


def test_emission_order():
    ranks = [ld.Landability(s).rank for s in
             (ld.LANDABLE, ld.CREATES_TEST, ld.UNCOVERED, ld.DEFERRED)]
    assert ranks == sorted(ranks)
    assert ld.Landability("martian").rank == ld.Landability(ld.UNCOVERED).rank


# ---------------------------------------------------------------------------
# The sensor: what actually reaches the router
# ---------------------------------------------------------------------------


class _Router:
    def __init__(self):
        self.ingested = []

    async def ingest(self, envelope):
        self.ingested.append(envelope)
        return envelope.signal_id


def _sensor(repo, router):
    return WorkOrderSensor(
        repo="jarvis", router=router, project_root=repo,
        seen_ledger_path=repo / ".jarvis" / "wo_seen.json",
    )


def _progress(repo, lines):
    (repo / ".superpowers" / "sdd" / "progress.md").write_text("\n".join(lines) + "\n")


@pytest.fixture
def armed(monkeypatch):
    monkeypatch.setenv("JARVIS_WORK_ORDER_SENSOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WORK_ORDER_RECENT_N", "0")


@pytest.mark.asyncio
async def test_sensor_emits_landable_first_and_withholds_owned_work(
    repo, armed, filing,
):
    _progress(repo, [
        "S1. NEXT: harden backend/bare.py against bad input",
        "S2. NEXT: tidy backend/covered.py",
    ])
    router = _Router()
    sensor = _sensor(repo, router)
    sensor._landability = ld.LandabilityTriage(
        repo, resolver=_resolver(repo), roadmap_ids=_ids(A_BARE, B_BARE),
    )
    out = await sensor.scan_once()
    assert [e.target_files for e in out] == [("backend/covered.py",)]
    assert [e.target_files for e in router.ingested] == [("backend/covered.py",)]

    # The withheld item is SETTLED: the roadmap owns it now, so the next scan
    # neither re-emits it nor pays to re-judge it.
    again = _Router()
    assert await _sensor(repo, again).scan_once() == []
    assert again.ingested == []


@pytest.mark.asyncio
async def test_sensor_orders_but_never_sheds_unowned_work(repo, armed, filing):
    _progress(repo, [
        "S1. NEXT: harden backend/bare.py against bad input",
        "S2. NEXT: tidy backend/covered.py",
    ])
    router = _Router()
    sensor = _sensor(repo, router)
    sensor._landability = ld.LandabilityTriage(
        repo, resolver=_resolver(repo), roadmap_ids=_ids(),
    )
    out = await sensor.scan_once()
    assert [e.target_files[0] for e in out] == ["backend/covered.py", "backend/bare.py"]


@pytest.mark.asyncio
async def test_gate_off_is_document_order(repo, armed, filing, monkeypatch):
    monkeypatch.setenv("JARVIS_INTAKE_LANDABILITY_GATE_ENABLED", "false")
    _progress(repo, [
        "S1. NEXT: harden backend/bare.py against bad input",
        "S2. NEXT: tidy backend/covered.py",
    ])
    out = await _sensor(repo, _Router()).scan_once()
    assert [e.target_files[0] for e in out] == ["backend/bare.py", "backend/covered.py"]
    assert filing == []


# ---------------------------------------------------------------------------
# The seams it needed
# ---------------------------------------------------------------------------


def test_a_duplicate_prerequisite_does_not_orphan_its_dependent(monkeypatch):
    """``duplicate_id`` means the prerequisite EXISTS. Refusing to file the
    dependent behind it left a half-filed pair impossible to complete."""
    filed = []

    def sign(spec):
        filed.append(spec.goal_id)
        if spec.goal_id.startswith("ov-dag-testsynth-"):
            return SimpleNamespace(ok=False, reason="duplicate_id")
        return SimpleNamespace(ok=True, reason="ok")

    monkeypatch.setattr(ogs, "author_and_sign_goal", sign)
    plan = goal_dag.plan_substitution(subject_file="backend/api/thing.py")
    assert plan is not None
    res_a, res_b = goal_dag.file_substitution(plan)
    assert filed == [plan.goal_a_id, plan.goal_b_id]
    assert res_b is not None and res_b.ok


def test_a_prerequisite_that_truly_failed_still_files_no_dependent(monkeypatch):
    filed = []

    def sign(spec):
        filed.append(spec.goal_id)
        return SimpleNamespace(ok=False, reason="secret_unset")

    monkeypatch.setattr(ogs, "author_and_sign_goal", sign)
    plan = goal_dag.plan_substitution(subject_file="backend/api/thing.py")
    _a, res_b = goal_dag.file_substitution(plan)
    assert filed == [plan.goal_a_id] and res_b is None


def test_an_unverifiable_roadmap_owns_nothing(tmp_path):
    bogus = tmp_path / ".jarvis" / "roadmap.yaml"
    bogus.parent.mkdir()
    bogus.write_text("goals:\n  - id: ov-dag-repair-bare\nsignature: forged\n")
    assert ogs.roadmap_goal_ids(bogus) == frozenset()


def test_governs_is_about_the_tree_not_the_name(tmp_path):
    path = tmp_path / ".jarvis" / "roadmap.yaml"
    assert ogs.governs(tmp_path, path) is True
    assert ogs.governs(tmp_path / "elsewhere", path) is False

"""P0.2 — WorkOrderSensor: ingest the operator's OWN roadmap as work orders.

The organism self-selects trivia because nothing feeds it the operator's
actual intent. This sensor reads operator-DECLARED roadmap artifacts
(progress.md NEXT: markers, plan docs), resolves the real files each item
names, and emits it as a source="roadmap" signal so P0.1's value-priority
floats it to the top instead of the queue standing empty of substance.

Proof obligations: emits work orders from markers; resolves ONLY real target
files (so P0.1 can band them); recency-bounds an append-only log; dedups
within AND across sessions; no-ops when disabled; fail-soft on a missing
source; is wired live into the intake layer (guard re-severing); and
under JARVIS_ALLOW_ROADMAP_REVISIT stands suppression down for a
deliberate deep-sampling re-run WITHOUT deleting or rewriting the
ledger on disk.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.intake.sensors.work_order_sensor import (
    WorkOrderSensor,
)


class _CapturingRouter:
    def __init__(self):
        self.ingested = []

    async def ingest(self, envelope):
        self.ingested.append(envelope)
        return envelope.signal_id


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "foo.py").write_text("def f():\n    return 1\n")
    (tmp_path / "backend" / "bar.py").write_text("x = 2\n")
    (tmp_path / ".superpowers" / "sdd").mkdir(parents=True)
    return tmp_path


def _progress(repo: Path, lines):
    p = repo / ".superpowers" / "sdd" / "progress.md"
    p.write_text("\n".join(lines) + "\n")
    return p


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("JARVIS_WORK_ORDER_SENSOR_ENABLED", "true")
    # Deterministic: keep all items (no recency tail) unless a test sets it.
    monkeypatch.setenv("JARVIS_WORK_ORDER_RECENT_N", "0")


def _sensor(repo, router, **kw):
    return WorkOrderSensor(
        repo="jarvis", router=router, project_root=repo,
        seen_ledger_path=repo / ".jarvis" / "wo_seen.json", **kw,
    )


@pytest.mark.asyncio
async def test_emits_work_orders_from_markers(repo, enabled):
    _progress(repo, [
        "SLICE 1 COMPLETE. NEXT: implement the widget in backend/foo.py",
        "SLICE 2 COMPLETE. NEXT: refactor backend/bar.py for clarity",
    ])
    r = _CapturingRouter()
    out = await _sensor(repo, r).scan_once()
    assert len(out) == 2
    assert all(e.source == "roadmap" for e in out)
    assert all(e.urgency == "high" for e in out)  # operator intent = explicit
    assert all(e.evidence.get("work_order") is True for e in out)
    descs = [e.description for e in out]
    assert any("implement the widget" in d for d in descs)


@pytest.mark.asyncio
async def test_resolves_only_real_target_files(repo, enabled):
    _progress(repo, [
        "NEXT: fix `backend/foo.py` and also backend/bar.py "
        "but NOT backend/ghost.py which does not exist",
    ])
    r = _CapturingRouter()
    out = await _sensor(repo, r).scan_once()
    assert len(out) == 1
    targets = set(out[0].target_files)
    assert "backend/foo.py" in targets
    assert "backend/bar.py" in targets
    assert "backend/ghost.py" not in targets  # non-existent → not a target


@pytest.mark.asyncio
async def test_recency_bounds_append_only_log(repo, monkeypatch):
    monkeypatch.setenv("JARVIS_WORK_ORDER_SENSOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WORK_ORDER_RECENT_N", "2")
    _progress(repo, [f"NEXT: step {i} in backend/foo.py" for i in range(5)])
    r = _CapturingRouter()
    out = await _sensor(repo, r).scan_once()
    assert len(out) == 2  # only the live tail
    assert "step 3" in out[0].description
    assert "step 4" in out[1].description


@pytest.mark.asyncio
async def test_targetless_item_is_skipped(repo, enabled):
    """A work order must name a subject O+V can act on — prose-only roadmap
    items (no resolvable file) are skipped, not emitted target-less."""
    _progress(repo, [
        "NEXT: run the graduation soak and report metrics",  # no file → skip
        "NEXT: fix the bug in backend/foo.py",               # has file → emit
    ])
    r = _CapturingRouter()
    out = await _sensor(repo, r).scan_once()
    assert len(out) == 1
    assert "backend/foo.py" in out[0].target_files


@pytest.mark.asyncio
async def test_dedup_within_session(repo, enabled):
    _progress(repo, ["NEXT: do the thing in backend/foo.py"])
    r = _CapturingRouter()
    s = _sensor(repo, r)
    assert len(await s.scan_once()) == 1
    assert len(await s.scan_once()) == 0  # already seen → not re-emitted


@pytest.mark.asyncio
async def test_dedup_across_sessions(repo, enabled):
    _progress(repo, ["NEXT: do the thing in backend/foo.py"])
    r1 = _CapturingRouter()
    assert len(await _sensor(repo, r1).scan_once()) == 1
    # A FRESH sensor sharing the persisted ledger must not re-emit.
    r2 = _CapturingRouter()
    assert len(await _sensor(repo, r2).scan_once()) == 0


@pytest.mark.asyncio
async def test_new_marker_after_dedup_emits(repo, enabled):
    p = _progress(repo, ["NEXT: first task in backend/foo.py"])
    r = _CapturingRouter()
    s = _sensor(repo, r)
    assert len(await s.scan_once()) == 1
    # Operator appends a new NEXT: — only the new one emits.
    p.write_text(p.read_text() + "NEXT: second task in backend/bar.py\n")
    out2 = await s.scan_once()
    assert len(out2) == 1
    assert "second task" in out2[0].description


@pytest.mark.asyncio
async def test_disabled_is_inert(repo, monkeypatch):
    monkeypatch.setenv("JARVIS_WORK_ORDER_SENSOR_ENABLED", "false")
    _progress(repo, ["NEXT: do a thing in backend/foo.py"])
    r = _CapturingRouter()
    assert await _sensor(repo, r).scan_once() == []
    assert r.ingested == []


@pytest.mark.asyncio
async def test_failsoft_missing_source(repo, enabled, monkeypatch):
    monkeypatch.setenv("JARVIS_WORK_ORDER_SOURCES", "does/not/exist.md")
    r = _CapturingRouter()
    assert await _sensor(repo, r).scan_once() == []  # no source, no raise


@pytest.mark.asyncio
async def test_custom_markers_and_urgency(repo, monkeypatch):
    monkeypatch.setenv("JARVIS_WORK_ORDER_SENSOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WORK_ORDER_RECENT_N", "0")
    monkeypatch.setenv("JARVIS_WORK_ORDER_MARKERS", "TODO(ov):,ACTION:")
    monkeypatch.setenv("JARVIS_WORK_ORDER_DEFAULT_URGENCY", "critical")
    _progress(repo, [
        "TODO(ov): handle the edge case in backend/foo.py",
        "ACTION: wire backend/bar.py",
        "NEXT: this marker is not configured, ignore",
    ])
    r = _CapturingRouter()
    out = await _sensor(repo, r).scan_once()
    assert len(out) == 2  # NEXT: not in the configured marker set
    assert all(e.urgency == "critical" for e in out)


@pytest.mark.asyncio
async def test_max_items_cap(repo, monkeypatch):
    monkeypatch.setenv("JARVIS_WORK_ORDER_SENSOR_ENABLED", "true")
    monkeypatch.setenv("JARVIS_WORK_ORDER_RECENT_N", "0")
    monkeypatch.setenv("JARVIS_WORK_ORDER_MAX_ITEMS", "3")
    _progress(repo, [f"NEXT: task {i} in backend/foo.py" for i in range(10)])
    r = _CapturingRouter()
    out = await _sensor(repo, r).scan_once()
    assert len(out) == 3  # bounded


def test_wired_into_intake_layer():
    """Guard against re-severing: the intake layer must construct + register
    the WorkOrderSensor (wired-live, gated-inert — never dead-by-omission)."""
    import inspect
    from backend.core.ouroboros.governance.intake import intake_layer_service
    src = inspect.getsource(intake_layer_service)
    assert "WorkOrderSensor(" in src
    assert "self._sensors.append(_work_order_sensor)" in src


# ── Revisit mode (JARVIS_ALLOW_ROADMAP_REVISIT) ──────────────────────────
#
# The dedup ledger is what makes a stable roadmap emit exactly once, ever.
# That is right for a normal boot and wrong for a deliberate deep-sampling
# run, where the SAME roadmap must produce work again so the corpus grows.
# The fix must not be "delete the ledger": that destroys the operator's
# cross-session record to buy one run, and it cannot be undone.


@pytest.fixture
def revisit(monkeypatch):
    monkeypatch.setenv("JARVIS_ALLOW_ROADMAP_REVISIT", "true")


def _ledger(repo: Path) -> Path:
    return repo / ".jarvis" / "wo_seen.json"


@pytest.mark.asyncio
async def test_revisit_re_emits_across_sessions(repo, enabled, revisit):
    """The whole point: a fresh process re-emits a roadmap it has seen."""
    _progress(repo, ["NEXT: do the thing in backend/foo.py"])
    assert len(await _sensor(repo, _CapturingRouter()).scan_once()) == 1
    # Same ledger, same roadmap, new process -> emits AGAIN under the flag.
    out = await _sensor(repo, _CapturingRouter()).scan_once()
    assert len(out) == 1
    assert "do the thing" in out[0].description


@pytest.mark.asyncio
async def test_revisit_leaves_the_ledger_on_disk_intact(repo, enabled, revisit):
    """Shadowed in memory — never deleted, never truncated."""
    _progress(repo, ["NEXT: do the thing in backend/foo.py"])
    await _sensor(repo, _CapturingRouter()).scan_once()
    before = _ledger(repo).read_text(encoding="utf-8")
    assert json.loads(before), "ledger should hold the hash after run 1"
    await _sensor(repo, _CapturingRouter()).scan_once()
    after = _ledger(repo).read_text(encoding="utf-8")
    assert _ledger(repo).is_file()
    # Re-emitting an already-recorded hash must not duplicate or drop it.
    assert json.loads(after) == json.loads(before)


@pytest.mark.asyncio
async def test_revisit_is_worth_exactly_one_re_emit_per_session(
    repo, enabled, revisit,
):
    """A 2.5h soak polls hourly. The exemption is spent on the first re-emit,
    so the roadmap does not re-flood the queue on every subsequent poll."""
    _progress(repo, ["NEXT: do the thing in backend/foo.py"])
    await _sensor(repo, _CapturingRouter()).scan_once()  # session 1
    s2 = _sensor(repo, _CapturingRouter())               # session 2
    assert len(await s2.scan_once()) == 1                # exemption spent
    assert len(await s2.scan_once()) == 0                # ...and not renewed
    assert len(await s2.scan_once()) == 0


@pytest.mark.asyncio
async def test_revisit_off_is_the_old_behaviour(repo, enabled, monkeypatch):
    """Default-FALSE. The flag absent must be indistinguishable from before."""
    monkeypatch.delenv("JARVIS_ALLOW_ROADMAP_REVISIT", raising=False)
    _progress(repo, ["NEXT: do the thing in backend/foo.py"])
    assert len(await _sensor(repo, _CapturingRouter()).scan_once()) == 1
    assert len(await _sensor(repo, _CapturingRouter()).scan_once()) == 0


@pytest.mark.asyncio
async def test_revisit_flag_flipped_off_restores_suppression(
    repo, enabled, monkeypatch,
):
    """The ledger survived, so suppression comes back in full — the property
    that deleting the file would have thrown away."""
    monkeypatch.setenv("JARVIS_ALLOW_ROADMAP_REVISIT", "true")
    _progress(repo, ["NEXT: do the thing in backend/foo.py"])
    assert len(await _sensor(repo, _CapturingRouter()).scan_once()) == 1
    assert len(await _sensor(repo, _CapturingRouter()).scan_once()) == 1
    monkeypatch.setenv("JARVIS_ALLOW_ROADMAP_REVISIT", "false")
    assert len(await _sensor(repo, _CapturingRouter()).scan_once()) == 0


@pytest.mark.asyncio
async def test_revisit_read_at_init_not_per_scan(repo, enabled, monkeypatch):
    """Flipping the flag mid-session must not retroactively un-suppress what
    this session already emitted — otherwise the next poll re-floods."""
    _progress(repo, ["NEXT: do the thing in backend/foo.py"])
    monkeypatch.delenv("JARVIS_ALLOW_ROADMAP_REVISIT", raising=False)
    s = _sensor(repo, _CapturingRouter())
    assert len(await s.scan_once()) == 1
    monkeypatch.setenv("JARVIS_ALLOW_ROADMAP_REVISIT", "true")
    assert len(await s.scan_once()) == 0  # snapshot was taken at __init__


@pytest.mark.asyncio
async def test_revisit_emits_the_same_envelope_bytes(repo, enabled, revisit):
    """Deterministic re-emission: the description and targets a re-run
    produces are identical to the first run's, so nothing downstream can
    fork on the sensor's account."""
    _progress(repo, [
        "NEXT: fix the widget in `backend/foo.py`",
        "NEXT: tidy `backend/bar.py`",
    ])
    first = await _sensor(repo, _CapturingRouter()).scan_once()
    second = await _sensor(repo, _CapturingRouter()).scan_once()
    assert len(first) == len(second) == 2
    assert [e.description for e in first] == [e.description for e in second]
    assert (
        [tuple(e.target_files) for e in first]
        == [tuple(e.target_files) for e in second]
    )
    assert [e.urgency for e in first] == [e.urgency for e in second]
    assert [e.source for e in first] == [e.source for e in second]


@pytest.mark.asyncio
async def test_revisit_still_emits_genuinely_new_items(repo, enabled, revisit):
    """Revisit widens what emits; it must not narrow it."""
    p = _progress(repo, ["NEXT: first task in backend/foo.py"])
    assert len(await _sensor(repo, _CapturingRouter()).scan_once()) == 1
    p.write_text(p.read_text() + "NEXT: second task in backend/bar.py\n")
    out = await _sensor(repo, _CapturingRouter()).scan_once()
    assert len(out) == 2  # the revisited one AND the brand-new one
    descs = " | ".join(e.description for e in out)
    assert "first task" in descs and "second task" in descs


@pytest.mark.asyncio
async def test_revisit_with_no_ledger_is_a_no_op(repo, enabled, revisit):
    """Nothing to stand down — a first-ever run behaves identically."""
    assert not _ledger(repo).exists()
    _progress(repo, ["NEXT: do the thing in backend/foo.py"])
    assert len(await _sensor(repo, _CapturingRouter()).scan_once()) == 1


@pytest.mark.asyncio
async def test_revisit_survives_a_torn_ledger(repo, enabled, revisit):
    """Fail-soft is unchanged: a corrupt ledger shadows nothing and the scan
    still emits rather than raising."""
    _ledger(repo).parent.mkdir(parents=True, exist_ok=True)
    _ledger(repo).write_text("{not json", encoding="utf-8")
    _progress(repo, ["NEXT: do the thing in backend/foo.py"])
    assert len(await _sensor(repo, _CapturingRouter()).scan_once()) == 1


@pytest.mark.asyncio
async def test_revisit_does_not_grow_the_bounded_ring(
    repo, enabled, revisit, monkeypatch,
):
    """Re-emitting an existing hash must not append a duplicate and push a
    real hash out of the capped ring."""
    monkeypatch.setenv("JARVIS_WORK_ORDER_SEEN_CAP", "4")
    _progress(repo, ["NEXT: do the thing in backend/foo.py"])
    await _sensor(repo, _CapturingRouter()).scan_once()
    for _ in range(5):
        await _sensor(repo, _CapturingRouter()).scan_once()
    recorded = json.loads(_ledger(repo).read_text(encoding="utf-8"))
    assert len(recorded) == 1
    assert len(set(recorded)) == len(recorded)


def test_revisit_gate_is_default_false_and_env_parsed(monkeypatch):
    """The gate itself, independent of any sensor instance."""
    from backend.core.ouroboros.governance.intake.sensors import (
        work_order_sensor as wos,
    )
    monkeypatch.delenv("JARVIS_ALLOW_ROADMAP_REVISIT", raising=False)
    assert wos.revisit_enabled() is False
    for truthy in ("true", "TRUE", "1", "yes", "on", " true "):
        monkeypatch.setenv("JARVIS_ALLOW_ROADMAP_REVISIT", truthy)
        assert wos.revisit_enabled() is True, truthy
    for falsy in ("false", "0", "no", "off", "", "banana"):
        monkeypatch.setenv("JARVIS_ALLOW_ROADMAP_REVISIT", falsy)
        assert wos.revisit_enabled() is False, falsy

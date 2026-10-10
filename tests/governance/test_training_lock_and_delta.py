"""What the last training cycle did, and what `ov` shows while one holds the card.

Training is exclusive: it refuses beside a live organism, and the organism's
lane gate refused to boot beside a live cycle with exit 69 and a paragraph.
Two surfaces close that: the served identity carries the newest cycle's
verdict (toolbar, banner, boot log), and a cold boot while the card is
leased away holds on a static lock block that tails the holder's log and
ignites by itself when the card returns.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, List, Optional

import pytest

from backend.core.ouroboros.governance import lane_admission as la
from backend.core.ouroboros.governance import served_identity as si
from backend.core.ouroboros.governance.observability import training_handoff as th


def _history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rows: List[dict]) -> None:
    monkeypatch.setenv("JARVIS_TRAINING_HANDOFF_DIR", str(tmp_path))
    monkeypatch.setattr(th, "state_dir", lambda: tmp_path)
    (tmp_path / "history.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_the_newest_cycle_verdict_is_read_as_numbers(tmp_path, monkeypatch) -> None:
    _history(tmp_path, monkeypatch, [
        {"ts": 1, "run_id": "handoff-a", "state": "RESTORING", "version": "v1"},
        {"ts": 2, "run_id": "handoff-a", "state": "COMMITTED",
         "verdict": {"tasks": 6, "incumbent": 3, "candidate": 4}},
        {"ts": 3, "run_id": "exclusive-x", "state": "COMPLETED"},       # not a training cycle
    ])
    d = th.last_cycle_delta()
    assert d == {"run_id": "handoff-a", "state": "COMMITTED", "version": "v1", "ts": 2,
                 "tasks": 6, "incumbent": 3, "candidate": 4}


def test_a_row_written_before_the_verdict_existed_is_read_from_its_own_evidence(tmp_path, monkeypatch) -> None:
    """The 2026-10-07 cycle: BASELINE accepted 3/6; COMMITTED smoke 4 ok of 6."""
    smoke = ["0:FAIL:x", "1:ok:candidates:1", "2:ok:candidates:1", "3:FAIL:y",
             "4:ok:candidates:1", "5:ok:candidates:1"]
    _history(tmp_path, monkeypatch, [
        {"ts": 1, "run_id": "handoff-b", "state": "BASELINE", "tasks": 6, "accepted": 3},
        {"ts": 2, "run_id": "handoff-b", "state": "COMMITTED", "smoke": smoke},
    ])
    d = th.last_cycle_delta()
    assert (d["tasks"], d["incumbent"], d["candidate"]) == (6, 3, 4)


def test_no_finished_cycle_is_none(tmp_path, monkeypatch) -> None:
    _history(tmp_path, monkeypatch, [{"ts": 1, "run_id": "handoff-c", "state": "TRAINING"}])
    assert th.last_cycle_delta() is None


@pytest.mark.parametrize("last,label", [
    ({"state": "COMMITTED", "tasks": 6, "incumbent": 3, "candidate": 4}, "last cycle +1 (4/6)"),
    ({"state": "ROLLED_BACK", "tasks": 6, "incumbent": 3, "candidate": 2},
     "last cycle rolled back (2/6 < 3/6)"),
    ({"state": "ROLLED_BACK", "tasks": 6, "incumbent": 3, "candidate": None},
     "last cycle rolled back (did not load)"),
])
def test_the_toolbar_names_the_last_cycle(last: dict, label: str) -> None:
    ident = {"model": "m", "engine": {"name": "J-Prime"}, "last_cycle": last}
    assert si.short_label(ident).endswith(label)
    assert "last training cycle" in si.describe_line(ident)


def test_the_lock_block_names_the_holder_and_tails_its_log(tmp_path) -> None:
    log = tmp_path / "train.log"
    log.write_text("step 1 loss 0.9\nstep 2 loss 0.8\n  50%|#####     |\r 60%|######    |\n")
    adm = la.LaneAdmission(False, "released", cycle={
        "holds_lane": True, "run_id": "handoff-20261010-1", "state": "TRAINING",
        "model": "qwen3-coder-ov:30b", "release_by": 7200.0, "log": str(log)})
    assert la.lock_title(adm) == "System locked: VRAM leased to Reactor training"
    lines = la.lock_lines(adm, tail=2, now=0.0)
    assert any("handoff-20261010-1" in ln for ln in lines)
    assert lines[-4:-2] == ["  step 2 loss 0.8", "   60%|######    |"], \
        "a progress bar's last state is its line"
    excl = la.LaneAdmission(False, "released", cycle={
        "holds_lane": True, "run_id": "exclusive-1", "state": "EXCLUSIVE", "purpose": "rollout profile"})
    assert la.lock_title(excl) == "System locked: VRAM leased to GPU job (rollout profile)"
    assert any("no log to follow" in ln for ln in la.lock_lines(excl, tail=4))


def test_a_cold_boot_holds_on_the_lock_until_the_card_returns(monkeypatch) -> None:
    from backend.core.ouroboros.cli import thin_client as tc
    lent = la.LaneAdmission(False, "released", cycle={"holds_lane": True, "run_id": "handoff-1",
                                                       "state": "TRAINING"})
    back = la.LaneAdmission(True, "serving")
    readings = [lent, lent, back]
    monkeypatch.setattr(la, "read_admission", lambda base, **k: readings.pop(0))
    monkeypatch.setenv("JARVIS_OV_LOCK_POLL_S", "0.25")
    shown: List[Any] = []
    said: List[str] = []
    assert asyncio.run(tc._hold_while_lent(lambda t, l: shown.append(t), said.append)) is True
    assert shown[:2] == ["System locked: VRAM leased to Reactor training"] * 2
    assert shown[-1] is None, "the lock is lifted before the boot continues"
    assert any("back" in s for s in said)


def test_a_serving_lane_never_shows_the_lock(monkeypatch) -> None:
    from backend.core.ouroboros.cli import thin_client as tc
    monkeypatch.setattr(la, "read_admission", lambda base, **k: la.LaneAdmission(True, "serving"))
    shown: List[Any] = []
    assert asyncio.run(tc._hold_while_lent(lambda t, l: shown.append(t), lambda s: None)) is True
    assert shown == []


def test_the_animator_renders_the_lock_in_place_of_the_transcript() -> None:
    from rich.console import Console
    from backend.core.ouroboros.ui.crest_animator import CrestAnimator
    a = CrestAnimator(cols=60, rows=20, frame_count=2, ss=1)
    a.add_log("booting")
    a.set_lock("System locked: VRAM leased to Reactor training", ["  holder  : x", "  step 9"])
    con = Console(width=100, record=True, color_system=None)
    con.print(a.render(0.3))
    out = con.export_text()
    assert "System locked" in out and "step 9" in out and "booting" not in out
    a.set_lock(None)
    con2 = Console(width=100, record=True, color_system=None)
    con2.print(a.render(0.3))
    assert "booting" in con2.export_text()

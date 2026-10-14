"""The vision loop stops spending turns on actions that change nothing.

Live: clicking a sponsored thumbnail that does not navigate, the model nudged
the same no-op click a few pixels each turn for all 20 turns (77 s); the old
guard only caught exact coordinate repeats.
"""
from __future__ import annotations

from backend.vision.lean_loop import LeanVisionLoop, _STAGNATION_WINDOW


def _entry(turn, coord, verification):
    return {"turn": turn, "action": "left_click", "params": {"coordinate": coord},
            "target": "first video", "result": "success", "verification": verification}


def test_no_effect_streak_counts_only_the_trailing_failures():
    log = [_entry(1, [1, 1], "fail"), _entry(2, [2, 2], "success"),
           _entry(3, [3, 3], "fail"), _entry(4, [4, 4], "fail")]
    assert LeanVisionLoop._no_effect_streak(log) == 2
    assert LeanVisionLoop._no_effect_streak([]) == 0


def test_moving_no_op_clicks_are_stagnation():
    log = [_entry(t, [700 + t, 400 - t], "fail") for t in range(_STAGNATION_WINDOW)]
    nxt = {"action": "left_click", "coordinate": [710, 390], "target": "first video"}
    assert LeanVisionLoop._is_stagnant_agentic(log, nxt)


def test_progress_is_not_stagnation():
    log = [_entry(t, [700 + t, 400], "success") for t in range(_STAGNATION_WINDOW)]
    assert not LeanVisionLoop._is_stagnant_agentic(log, {"action": "left_click",
                                                         "coordinate": [1, 2]})


def test_the_model_is_told_plainly_when_its_actions_had_no_effect():
    loop = LeanVisionLoop.__new__(LeanVisionLoop)
    content = loop._build_agentic_content(
        "play the first video", [("AAAA", "")], [_entry(1, [700, 400], "fail")], 2)
    text = content[-1]["text"]
    assert "NO EFFECT: your last 1 action(s) changed nothing" in text
    clean = loop._build_agentic_content("g", [("AAAA", "")], [_entry(1, [1, 1], "success")], 2)
    assert "NO EFFECT" not in clean[-1]["text"]

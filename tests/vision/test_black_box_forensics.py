"""A CONFIRM prompt with no evidence is a coin toss with consequences.

`intent_journal` halts on an EFFECTFUL step whose outcome is UNKNOWN and asks.
Asking without evidence is barely better than replaying blind: "did the click
land before it died?" cannot be answered by looking at the screen afterwards,
because whatever ran next has already overwritten it.

The mandated scenario is `test_a_crash_at_step_2_captures_the_forensic_delta`:
a three-step effectful macro, a crash injected at step 2, and an assertion that
the black box captured the active-window state and packaged it for the UI.

THE LINE THIS SUITE DEFENDS
-----------------------------
`changed` is THREE-valued. `None` means "could not determine", and it must
never render as "no change". A flattened boolean would hand the operator a
confident "nothing happened" that the system never claimed — the failure this
whole arc has been removing, one layer at a time.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

import pytest

from backend.core.ouroboros.battle_test import forensic_delta as fd
from backend.vision import black_box as bb


class _Step:
    def __init__(self, action="click", target="Send", value=""):
        self.action = action
        self.target = target
        self.value = value
        self.description = ""
        self.app_name = "Messages"


def _snap(app="Messages", title="New Message", *, availability="observed",
          t=None) -> bb.Snapshot:
    return bb.Snapshot(t=t or time.time(), app=app, app_pid=42,
                       window_title=title, window_count=3,
                       availability=availability)


class TestTheMandatedScenario:
    @pytest.mark.asyncio
    async def test_a_crash_at_step_2_captures_the_forensic_delta(self, monkeypatch):
        """Three effectful steps, crash at step 2, forensics packaged for the UI."""
        from backend.vision.cu_step_executor import CUStepExecutor

        ex = CUStepExecutor.__new__(CUStepExecutor)
        ran = []

        async def _inner(step, frame=None, step_index=0):
            ran.append(step_index)
            if step_index == 2:
                raise asyncio.TimeoutError("VLA layer stalled")
            return {"ok": True, "step": step_index}

        monkeypatch.setattr(ex, "_execute_step_inner", _inner, raising=False)
        # Mock the OS context so the assertion is about OUR wiring, not about
        # whatever window happens to be frontmost on the test machine.
        seq = [_snap("Messages", "New Message"),
               _snap("Messages", "New Message"),
               _snap("Messages", "Sent")]
        monkeypatch.setattr(bb, "capture",
                            lambda: _pop(seq), raising=False)

        steps = [_Step("click", "Compose"), _Step("type", "hi"),
                 _Step("click", "Send")]
        for i, s in enumerate(steps, start=1):
            try:
                await ex.execute_step(s, None, i)
            except asyncio.TimeoutError:
                break

        assert ran == [1, 2], "the macro did not stop at the crashing step"

        payload = ex.take_forensics()
        assert payload is not None, "no black box was captured"
        assert payload["step_index"] == 2
        assert payload["action"] == "type"
        assert "TimeoutError" in payload["error"]

        # The active-window state at the moment before the step.
        assert payload["before"]["app"] == "Messages"
        assert payload["before"]["window_title"] == "New Message"

        # And the finding: there is no `after`, because it never returned.
        assert payload["after"] is None
        assert payload["post_captured"] is False
        assert payload["changed"] is None, (
            "an uncaptured post-state must read as UNDETERMINED, not 'no change'")

        # Packaged for the UI without the caller doing any work.
        rows = fd.render(payload, width=100)
        assert any("step 2" in r for r in rows)
        assert any("Messages" in r for r in rows)
        assert any("could not determine" in r for r in rows)
        assert any("confirm" in r.lower() for r in rows)

    @pytest.mark.asyncio
    async def test_the_forensics_are_popped_not_left_behind(self, monkeypatch):
        """A later successful run must not surface a stale confirmation."""
        from backend.vision.cu_step_executor import CUStepExecutor
        ex = CUStepExecutor.__new__(CUStepExecutor)

        async def _inner(step, frame=None, step_index=0):
            raise RuntimeError("boom")

        monkeypatch.setattr(ex, "_execute_step_inner", _inner, raising=False)
        monkeypatch.setattr(bb, "capture", lambda: _pop([_snap()]),
                            raising=False)
        with pytest.raises(RuntimeError):
            await ex.execute_step(_Step(), None, 1)
        assert ex.take_forensics() is not None
        assert ex.take_forensics() is None, "forensics were not popped"

    @pytest.mark.asyncio
    async def test_a_non_effectful_step_is_not_instrumented(self, monkeypatch):
        """`wait` cannot change the machine, so it needs no black box and no
        CONFIRM on resume."""
        from backend.vision.cu_step_executor import CUStepExecutor
        ex = CUStepExecutor.__new__(CUStepExecutor)
        captures = {"n": 0}

        async def _inner(step, frame=None, step_index=0):
            return {"ok": True}

        async def _counting():
            captures["n"] += 1
            return _snap()

        monkeypatch.setattr(ex, "_execute_step_inner", _inner, raising=False)
        monkeypatch.setattr(bb, "capture", _counting, raising=False)
        await ex.execute_step(_Step(action="wait"), None, 1)
        assert captures["n"] == 0

    @pytest.mark.asyncio
    async def test_the_post_snapshot_becomes_the_next_pre(self, monkeypatch):
        """Halves the captures, and is more honest — it is the same instant."""
        from backend.vision.cu_step_executor import CUStepExecutor
        ex = CUStepExecutor.__new__(CUStepExecutor)
        captures = {"n": 0}

        async def _inner(step, frame=None, step_index=0):
            return {"ok": True}

        async def _counting():
            captures["n"] += 1
            return _snap()

        monkeypatch.setattr(ex, "_execute_step_inner", _inner, raising=False)
        monkeypatch.setattr(bb, "capture", _counting, raising=False)
        for i in range(1, 4):
            await ex.execute_step(_Step(), None, i)
        # 1 pre + 3 posts, not 6.
        assert captures["n"] == 4, f"{captures['n']} captures for 3 steps"


class TestThreeValuedChange:
    def test_an_unobserved_side_makes_the_verdict_undetermined(self):
        d = bb.delta(_snap(availability="unavailable"), _snap())
        assert d["changed"] is None

    def test_a_real_change_is_reported(self):
        d = bb.delta(_snap("Messages", "New Message"), _snap("Safari", "Docs"))
        assert d["changed"] is True
        assert "Messages" in d["summary"] and "Safari" in d["summary"]

    def test_no_change_is_distinguishable_from_unknown(self):
        assert bb.delta(_snap(), _snap())["changed"] is False

    def test_a_missing_snapshot_is_undetermined(self):
        assert bb.delta(_snap(), None)["changed"] is None
        assert bb.delta(None, None)["changed"] is None


class TestTheRendererNeverLies:
    def test_undetermined_never_renders_as_no_change(self):
        """THE line. A flattened boolean would hand the operator a confident
        'nothing happened' the system never claimed."""
        rows = fd.render({"step_index": 1, "action": "click", "changed": None,
                          "summary": "", "before": None, "after": None})
        joined = " ".join(rows).lower()
        assert "could not determine" in joined
        assert "no observable change" not in joined

    def test_an_absent_after_is_stated_not_skipped(self):
        rows = fd.render({"step_index": 1, "action": "click",
                          "before": _snap().to_dict(), "after": None,
                          "changed": None})
        assert any("never captured" in r for r in rows)

    def test_an_unreadable_snapshot_says_why(self):
        rows = fd.render({"step_index": 1, "action": "click", "changed": None,
                          "before": _snap(availability="timed_out").to_dict(),
                          "after": None})
        assert any("timed_out" in r for r in rows)

    def test_a_missing_title_is_not_an_empty_title(self):
        snap = _snap(title=None).to_dict()
        rows = fd.render({"step_index": 1, "action": "click", "changed": None,
                          "before": snap, "after": None})
        assert any("title unavailable" in r for r in rows)

    def test_empty_payload_renders_nothing(self):
        assert fd.render(None) == []
        assert fd.render({}) == []

    def test_render_never_raises(self):
        for hostile in ({"step_index": object()}, {"before": "not-a-dict"},
                        {"changed": "maybe"}, {"action": None}):
            assert isinstance(fd.render(hostile), list)

    def test_rows_for_bounds_the_strip(self):
        payloads = [{"step_index": i, "action": "click", "changed": None,
                     "before": None, "after": None} for i in range(10)]
        rows = fd.rows_for(payloads, width=90, limit=2)
        assert rows and sum(1 for r in rows if r.startswith("⚠")) == 2

    def test_rows_for_is_empty_when_nothing_pending(self):
        assert fd.rows_for(None) == []
        assert fd.rows_for([]) == []


class TestCaptureIsBounded:
    @pytest.mark.asyncio
    async def test_a_slow_capture_times_out_rather_than_stalling(
            self, monkeypatch):
        """A black box that can stall the automation it observes is worse than
        no black box."""
        monkeypatch.setenv("JARVIS_BLACK_BOX_TIMEOUT_S", "0.05")

        def _slow():
            time.sleep(2.0)
            return _snap()

        monkeypatch.setattr(bb, "_blocking_capture", _slow)
        t0 = time.monotonic()
        snap = await bb.capture()
        assert time.monotonic() - t0 < 1.0
        assert snap.availability == bb.Availability.TIMED_OUT.value

    @pytest.mark.asyncio
    async def test_the_master_switch_disables_capture(self, monkeypatch):
        monkeypatch.setenv("JARVIS_BLACK_BOX_ENABLED", "0")
        assert (await bb.capture()).availability == bb.Availability.DISABLED.value

    @pytest.mark.asyncio
    async def test_capture_never_raises(self, monkeypatch):
        monkeypatch.setattr(bb, "_blocking_capture",
                            lambda: (_ for _ in ()).throw(OSError("nope")))
        assert isinstance(await bb.capture(), bb.Snapshot)

    @pytest.mark.asyncio
    async def test_a_real_capture_returns_a_usable_snapshot(self):
        """Whatever this machine is — GUI, headless, no permissions — the
        snapshot must be usable and honest about what it could not see."""
        snap = await bb.capture()
        assert snap.availability in {a.value for a in bb.Availability}
        assert isinstance(snap.to_dict(), dict)

    def test_titles_can_be_dropped_for_privacy(self, monkeypatch):
        """Window titles carry document names, message text, URLs."""
        monkeypatch.setenv("JARVIS_BLACK_BOX_TITLES", "0")
        assert bb.title_capture_enabled() is False


def _pop(seq):
    """Async stand-in that yields the next mocked snapshot."""
    async def _inner():
        return seq.pop(0) if seq else _snap()
    return _inner()

"""Tier 3: the status-line contract, the clear chord, and a declared waiver.

Three small closures, each with a failure that reads as normal operation:

  * a status-line runner that pipes nothing, so an operator's existing CC
    script runs and prints its fallback forever;
  * a `Ctrl+L` that clears without warning, losing a transcript for someone
    who was only fixing a garbled screen;
  * an unfilled hook that is a DESIGN DECISION reported as a defect, which
    trains people to ignore the audit.
"""
from __future__ import annotations

import json
import os
import types

import pytest

from backend.core.ouroboros.battle_test import status_line as SL
from backend.core.ouroboros.battle_test import transcript_hatches as H


class TestStatuslinePayload:
    def test_it_is_json(self):
        assert isinstance(json.loads(SL._statusline_payload()), dict)

    def test_it_carries_ccs_shape(self):
        """A script written for CC must run here unchanged — that is the
        entire value of adopting a contract rather than inventing one."""
        payload = json.loads(SL._statusline_payload())
        assert "cwd" in payload
        assert payload["workspace"]["current_dir"]
        assert payload["workspace"]["project_dir"]

    def test_ov_specific_state_rides_alongside(self):
        """`phase` and `route` have no CC counterpart and are what an O+V
        operator most wants. Additive, not contorted into CC's shape."""
        snap = SL.StatusSnapshot(phase="GENERATE", route="complex",
                                 provider="claude", cost_spent_usd=0.42,
                                 cost_budget_usd=2.5, primary_op_id="op-9")
        payload = json.loads(SL._statusline_payload(snap))
        assert payload["phase"] == "GENERATE"
        assert payload["route"] == "complex"
        assert payload["model"]["display_name"] == "claude"
        assert payload["cost"]["total_cost_usd"] == 0.42
        assert payload["cost"]["budget_usd"] == 2.5
        assert payload["session_id"] == "op-9"

    def test_unknown_values_are_omitted_not_faked(self):
        """A script reading `.cost.total_cost_usd` gets a real number or
        nothing — never a zero that looks like a free session."""
        payload = json.loads(SL._statusline_payload(SL.StatusSnapshot()))
        assert "route" not in payload
        assert "model" not in payload
        assert "session_id" not in payload

    def test_it_never_raises_on_a_broken_snapshot(self):
        assert json.loads(SL._statusline_payload(object()))

    def test_the_script_actually_receives_it(self, monkeypatch):
        """End to end through the real runner: the contract is only real if
        the bytes arrive on the script's stdin."""
        monkeypatch.setenv(
            "JARVIS_STATUSLINE_CMD",
            "python3 -c 'import sys,json;"
            'd=json.load(sys.stdin);print("got:"+str(sorted(d)[:2]))\'',
        )
        SL._CUSTOM_SEGMENT_CACHE["at"] = 0.0
        out = SL._custom_segment()
        assert out.startswith("got:"), out

    def test_a_script_that_ignores_stdin_still_works(self, monkeypatch):
        """The payload must not break the scripts that predate it."""
        monkeypatch.setenv("JARVIS_STATUSLINE_CMD", "echo hello")
        SL._CUSTOM_SEGMENT_CACHE["at"] = 0.0
        assert SL._custom_segment() == "hello"


class _Buf:
    def __init__(self):
        self.text = ""
        self.cursor_position = 0
        self.handled = []

    def validate_and_handle(self):
        self.handled.append(self.text)


class _App:
    def __init__(self):
        self.current_buffer = _Buf()
        self.redraws = 0
        self.renderer = types.SimpleNamespace(clear=lambda: None)

    def invalidate(self):
        self.redraws += 1


class TestClearChord:
    @pytest.fixture(autouse=True)
    def _fresh(self):
        H._CLEAR_LATCH = None
        yield
        H._CLEAR_LATCH = None

    def test_one_press_redraws_and_does_not_clear(self):
        """An operator reaching for Ctrl+L is usually recovering a garbled
        screen and has no intention of clearing anything."""
        ev = types.SimpleNamespace(app=_App())
        H.force_redraw(ev)
        assert ev.app.redraws == 1
        assert ev.app.current_buffer.handled == []

    def test_two_presses_clear(self):
        ev = types.SimpleNamespace(app=_App())
        H.force_redraw(ev)
        H.force_redraw(ev)
        assert ev.app.current_buffer.handled == ["/clear"]

    def test_the_redraw_happens_on_both_presses(self):
        """Arming is a side effect of a key that already did its job, never
        a mode the operator is put into."""
        ev = types.SimpleNamespace(app=_App())
        H.force_redraw(ev)
        H.force_redraw(ev)
        assert ev.app.redraws == 2

    def test_a_slow_second_press_does_not_clear(self):
        from backend.core.ouroboros.battle_test.confirm_chord import (
            ConfirmLatch,
        )
        clock = [100.0]
        H._CLEAR_LATCH = ConfirmLatch(window_s=2.0, clock=lambda: clock[0])
        ev = types.SimpleNamespace(app=_App())
        H.force_redraw(ev)
        clock[0] += 5.0
        H.force_redraw(ev)
        assert ev.app.current_buffer.handled == []
        assert ev.app.redraws == 2

    def test_the_window_is_ccs_two_seconds_and_clamped(self, monkeypatch):
        """Two seconds, not the three `Ctrl+X Ctrl+K` uses — killing every
        agent deserves a longer think than redrawing a screen."""
        assert H._clear_window_s() == 2.0
        monkeypatch.setenv("JARVIS_CLEAR_DOUBLE_PRESS_S", "0")
        assert H._clear_window_s() >= 0.5
        monkeypatch.setenv("JARVIS_CLEAR_DOUBLE_PRESS_S", "junk")
        assert H._clear_window_s() > 0

    def test_it_reuses_the_shared_latch(self):
        """A second timer would be two answers to 'did they press twice'."""
        import inspect

        assert "ConfirmLatch" in inspect.getsource(H._clear_latch)

    def test_clear_goes_through_the_verb_router(self):
        """`/clear` is a verb every surface already routes; reaching past
        that router would clear on some surfaces and not others."""
        ev = types.SimpleNamespace(app=_App())
        H.force_redraw(ev)
        H.force_redraw(ev)
        assert ev.app.current_buffer.handled == ["/clear"]

    def test_a_broken_app_never_raises(self):
        H.force_redraw(types.SimpleNamespace(app=None))
        H.force_redraw(None)


class TestDaemonHeaderWaiver:
    def test_the_daemon_declares_rather_than_omits(self):
        """An unfilled hook has two causes needing opposite responses:
        'this surface has no use for it' is a decision, and 'nobody noticed'
        is the defect. Silence cannot tell them apart."""
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import serpent_flow

        # `textwrap.dedent`: a METHOD's source is indented, and
        # `ast.parse` rejects it. The same trap bit this arc once
        # already in the inflight-registry tests.
        import textwrap
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(serpent_flow.SerpentREPL._loop)))
        waived = {
            n.arg for n in ast.walk(tree)
            if isinstance(n, ast.keyword)
            and isinstance(n.value, ast.Call)
            and getattr(n.value.func, "id", None) == "waived"
        }
        assert {"header", "header_height"} <= waived

    def test_the_waiver_is_imported_unaliased(self):
        """`capability_handoff` matches the call-site spelling, so `as
        _waived` makes the waiver invisible to the auditor it exists for."""
        import ast
        import inspect

        from backend.core.ouroboros.battle_test import serpent_flow

        # `textwrap.dedent`: a METHOD's source is indented, and
        # `ast.parse` rejects it. The same trap bit this arc once
        # already in the inflight-registry tests.
        import textwrap
        tree = ast.parse(textwrap.dedent(
            inspect.getsource(serpent_flow.SerpentREPL._loop)))
        names = [
            a.asname or a.name
            for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)
            and "capability_handoff" in (n.module or "")
            for a in n.names
        ]
        assert "waived" in names

    def test_the_audit_now_reads_clean(self):
        from backend.core.ouroboros.ui import capability_handoff as C

        sink = ("backend.core.ouroboros.battle_test.bipartite_layout"
                ".build_bipartite_application")
        divergent = [d for d in C.audit().divergence() if d[0] == sink]
        assert divergent == [], divergent


class TestSerpentFlowLogger:
    def test_the_module_defines_the_logger_its_handlers_use(self):
        """SIX `except` handlers called `logger.debug` and nothing defined
        it, so each raised NameError FROM THE HANDLER — turning a swallowed
        degradation into a crash and making every 'NEVER raises' docstring
        above them false."""
        from backend.core.ouroboros.battle_test import serpent_flow

        assert hasattr(serpent_flow, "logger")

    def test_a_degraded_handler_returns_instead_of_raising(self):
        from backend.core.ouroboros.battle_test import serpent_flow

        repl = types.SimpleNamespace(_flow=None, _gls=None)
        serpent_flow.SerpentREPL._serve_diff_fetch(repl, "d-1")   # no raise

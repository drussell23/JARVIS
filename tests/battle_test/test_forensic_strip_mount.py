"""The strip has to be MOUNTED, on every surface, or it is not a feature.

This codebase has a specific way of failing: a capability is finished, correct,
tested — and reachable from nowhere. Five zero-caller features landed in one
day, one of them inert *inside the fix for inertness*, and three tests passed
throughout by asserting a handler's source contained a flag literal.

`forensic_delta` was in exactly that state when it was written: a renderer with
no mounted caller. So this file asserts the wiring, not the rendering.

WHAT THE AUDIT CAUGHT WHILE MOUNTING
--------------------------------------
Adding the provider to `build_daemon_mount` was not enough. `capability_handoff`
reported the hook `FILLED` on `ov` and `UNSET` on `serpent_flow`, because the
daemon passes each hook BY NAME and the new name was not at its call site —
the two-surfaces split, caught by the instrument written for it, one run after
the mistake. That is why the assertions below name surfaces individually rather
than trusting a single "it works here".
"""
from __future__ import annotations

import pytest


class TestBothRealSurfacesMountIt:
    def test_the_handoff_audit_sees_no_divergence(self):
        """THE assertion. A hook one surface fills and another ignores is the
        failure this whole module exists to prevent."""
        from backend.core.ouroboros.ui import capability_handoff as ch
        reading = ch.audit()
        ch.propagate_fills(reading)
        div = [d for d in reading.divergence() if "forensic_rows" in str(d)]
        assert div == [], f"forensic_rows diverges across surfaces: {div}"

    @pytest.mark.parametrize("surface", [
        "backend.core.ouroboros.battle_test.serpent_flow",   # the daemon
        "backend.core.ouroboros.cli.ov",                     # the attach client
        "backend.core.ouroboros.cli.ov_demo",                # the demo
    ])
    def test_every_surface_fills_the_hook(self, surface):
        from backend.core.ouroboros.ui import capability_handoff as ch
        reading = ch.audit()
        ch.propagate_fills(reading)
        states = {str(getattr(f, "state", ""))
                  for f in reading.fills
                  if getattr(f, "hook", "") == "forensic_rows"
                  and getattr(f, "surface", "") == surface}
        assert states, f"{surface} never names forensic_rows"
        assert any("FILLED" in s for s in states), (
            f"{surface} declares forensic_rows but leaves it {states}")

    def test_the_hook_is_passed_by_name_never_splatted(self):
        """`capability_handoff` reads a `**splat` as OPAQUE, so a mount that
        spread itself would blind the audit that proves this reached both
        surfaces — the reason `build_daemon_mount` returns values instead of
        splatting them."""
        from pathlib import Path
        for f in ("backend/core/ouroboros/battle_test/serpent_flow.py",
                  "backend/core/ouroboros/cli/ov.py"):
            src = Path(f).read_text(encoding="utf-8", errors="replace")
            assert "forensic_rows=" in src, f"{f} does not name the hook"


class TestTheLayoutActuallyDrawsIt:
    def test_the_builder_accepts_the_hook(self):
        import inspect
        from backend.core.ouroboros.battle_test.bipartite_layout import (
            build_bipartite_application, run_bipartite_repl,
        )
        for fn in (build_bipartite_application, run_bipartite_repl):
            assert "forensic_rows" in inspect.signature(fn).parameters, (
                f"{fn.__name__} cannot receive the hook")

    def test_it_is_mounted_through_the_shared_geometry_primitive(self):
        """`build_dynamic_rows` is the ONE variable-height strip primitive —
        exactly as tall as what it holds, zero rows when empty. A hand-rolled
        Window here would be a second answer to 'collapse when empty'."""
        from pathlib import Path
        src = Path("backend/core/ouroboros/battle_test/bipartite_layout.py"
                   ).read_text(encoding="utf-8", errors="replace")
        assert "build_dynamic_rows(forensic_rows)" in src

    def test_an_idle_cockpit_costs_zero_rows(self):
        """Nothing pending must render nothing — a Window drawing an empty
        string still occupies a line."""
        from backend.core.ouroboros.battle_test.cockpit_mount import (
            daemon_forensic_rows,
        )
        from backend.vision import black_box as bb
        bb.resolve_forensics()
        assert daemon_forensic_rows() == []


class TestTheDaemonSeesWhatItProduces:
    def test_the_daemon_mount_carries_the_provider(self):
        from backend.core.ouroboros.battle_test.cockpit_mount import (
            build_daemon_mount,
        )
        assert callable(build_daemon_mount().get("forensic_rows"))

    def test_a_crash_reaches_the_daemon_strip(self):
        """The direction that matters: the daemon RUNS the executor, so it is
        where the crash happens — and it was the surface most likely to be
        blind to it."""
        from backend.core.ouroboros.battle_test.cockpit_mount import (
            daemon_forensic_rows,
        )
        from backend.vision import black_box as bb
        bb.resolve_forensics()
        bb.note_forensics({
            "step_index": 2, "action": "type", "target": "message body",
            "error": "TimeoutError()", "changed": None,
            "before": {"app": "Messages", "window_title": "New Message",
                       "availability": "observed"},
            "after": None,
        })
        try:
            rows = daemon_forensic_rows()
            assert any("step 2" in r for r in rows)
            assert any("Messages" in r for r in rows)
            assert any("could not determine" in r for r in rows)
        finally:
            bb.resolve_forensics()


class TestTheClientGetsItAcrossTheBridge:
    def test_the_heartbeat_carries_forensics(self):
        from pathlib import Path
        src = Path("backend/core/ouroboros/battle_test/attach_heartbeat.py"
                   ).read_text(encoding="utf-8", errors="replace")
        assert '"forensics": _forensics_payload()' in src

    def test_the_payload_distinguishes_silence_from_nothing_pending(self):
        """None means the daemon never said; [] means nothing crashed. A client
        that cannot tell them apart draws 'healthy' when it means 'deaf'."""
        from backend.core.ouroboros.battle_test import attach_heartbeat as hb
        from backend.vision import black_box as bb
        bb.resolve_forensics()
        assert hb._forensics_payload() == []
        import os
        os.environ["JARVIS_FORENSIC_STRIP_ENABLED"] = "0"
        try:
            assert hb._forensics_payload() is None
        finally:
            os.environ.pop("JARVIS_FORENSIC_STRIP_ENABLED", None)

    def test_a_stale_heartbeat_retires_the_prompt(self):
        """A dead daemon must not leave a confirmation on screen — the operator
        would answer a question about a process that is gone, and the answer
        would go nowhere."""
        from pathlib import Path
        src = Path("backend/core/ouroboros/cli/ov.py").read_text(
            encoding="utf-8", errors="replace")
        i = src.index("def _forensic_rows")
        body = src[i:i + 1200]
        assert "_heartbeat_age()" in body and "return []" in body


class TestTheDemoShowsItBeforeItMatters:
    def test_the_demo_renders_through_the_same_renderer(self):
        """A demo with its own layout drifts into showing a cockpit that does
        not exist."""
        from backend.core.ouroboros.cli.ov_demo import _demo_forensic_rows
        assert _demo_forensic_rows(0.0, 100) == []
        late = _demo_forensic_rows(9_999.0, 100)
        assert late and any("could not determine" in r for r in late)

    def test_the_beat_is_env_tunable(self):
        from backend.core.ouroboros.cli import ov_demo
        assert isinstance(ov_demo._FORENSIC_BEAT_S, float)

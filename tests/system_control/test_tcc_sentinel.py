"""Refuse to boot blind rather than fail silently mid-DAG.

macOS gates screen capture and UI automation behind grants that attach to a
CODE IDENTITY, not a path. An app launched from Xcode is a different identity
from the same app launched from Finder, so grants do not carry over — and
nothing announces it. The APIs just start returning nothing.

Measured in this very process while writing this:

    AXIsProcessTrusted()             -> False
    CGPreflightScreenCaptureAccess() -> False
    bundleIdentifier                 -> None

Which is exactly why `cg_window_capture` returned no windows and the black box
recorded "window list unavailable" — a permissions problem wearing the costume
of a bug. An empty window list looks, to a model, like a screen with nothing on
it, and it will reason confidently from that.

THE DISTINCTION THIS SUITE DEFENDS
------------------------------------
`NOT_APPLICABLE` is not `DENIED`. A Linux soak node or a headless CI run cannot
obtain these grants and does not need them; blocking there would make the
Sentinel a thing people disable, which is how a safety check dies. Only an
explicit DENIED on a REQUIRED grant blocks a boot.
"""
from __future__ import annotations

import pytest

from backend.system_control import tcc_sentinel as ts
from backend.system_control.tcc_sentinel import (
    Grant, TCCMissingError, TCCReading, Verdict, enforce, probe, render,
)


def _reading(acc=Verdict.GRANTED, scr=Verdict.GRANTED, **kw) -> TCCReading:
    return TCCReading(accessibility=acc.value, screen_recording=scr.value, **kw)


class TestTheMandatedScenario:
    def test_untrusted_accessibility_aborts_the_boot(self, monkeypatch):
        """THE scenario: AXIsProcessTrusted() False → TCC_MISSING, boot blocked."""
        monkeypatch.setattr(ts, "_probe_accessibility", lambda: Verdict.DENIED)
        monkeypatch.setattr(ts, "_probe_screen_recording", lambda: Verdict.GRANTED)
        with pytest.raises(TCCMissingError) as ei:
            enforce([Grant.ACCESSIBILITY])
        assert ei.value.code == "TCC_MISSING"
        assert "accessibility" in ei.value.missing

    def test_the_error_names_what_to_do(self, monkeypatch):
        """A boot-blocking error whose fix is a settings pane should name the
        pane. Otherwise the operator's next move is a search engine."""
        rows = render(_reading(acc=Verdict.DENIED), [Grant.ACCESSIBILITY])
        joined = "\n".join(rows)
        assert "System Settings" in joined
        assert "Accessibility" in joined and "Screen Recording" in joined
        assert "Xcode" in joined, "the identity trap is not explained"

    def test_a_granted_process_boots(self, monkeypatch):
        monkeypatch.setattr(ts, "_probe_accessibility", lambda: Verdict.GRANTED)
        monkeypatch.setattr(ts, "_probe_screen_recording", lambda: Verdict.GRANTED)
        assert enforce([Grant.ACCESSIBILITY, Grant.SCREEN_RECORDING])

    def test_screen_recording_denial_also_blocks(self):
        with pytest.raises(TCCMissingError) as ei:
            enforce([Grant.SCREEN_RECORDING],
                    reading=_reading(scr=Verdict.DENIED))
        assert ei.value.missing == ["screen_recording"]

    def test_both_missing_are_reported_together(self):
        with pytest.raises(TCCMissingError) as ei:
            enforce(reading=_reading(acc=Verdict.DENIED, scr=Verdict.DENIED))
        assert set(ei.value.missing) == {"accessibility", "screen_recording"}


class TestNotApplicableIsNotDenied:
    def test_a_non_macos_host_is_not_a_failure(self):
        """A Linux soak node cannot obtain these and does not need them.
        Blocking there makes the Sentinel a thing people disable."""
        r = _reading(acc=Verdict.NOT_APPLICABLE, scr=Verdict.NOT_APPLICABLE)
        assert r.denied() == []
        assert enforce(reading=r) is r

    def test_a_failed_probe_degrades_to_not_applicable(self, monkeypatch):
        """Unable to ASK is not the same as being told no."""
        monkeypatch.setattr(ts, "_is_macos_gui", lambda: True)
        monkeypatch.setattr(
            ts.ctypes, "CDLL",
            lambda *_a, **_k: (_ for _ in ()).throw(OSError("no framework")))
        assert ts._probe_accessibility() is Verdict.NOT_APPLICABLE
        assert ts._probe_screen_recording() is Verdict.NOT_APPLICABLE

    def test_only_the_REQUIRED_grants_are_enforced(self):
        """A component that needs no screen access must not be blocked by its
        absence."""
        r = _reading(acc=Verdict.GRANTED, scr=Verdict.DENIED)
        assert enforce([Grant.ACCESSIBILITY], reading=r) is r
        with pytest.raises(TCCMissingError):
            enforce([Grant.SCREEN_RECORDING], reading=r)


class TestItProbesRatherThanPrompts:
    def test_it_never_calls_the_requesting_api(self):
        """`CGRequestScreenCaptureAccess` shows a dialog and, on first denial,
        poisons the answer until the app is re-added by hand. A boot probe must
        observe, not prompt."""
        import inspect
        src = inspect.getsource(ts)
        assert "CGPreflightScreenCaptureAccess" in src
        assert "CGRequestScreenCaptureAccess" not in src.split('"""')[-1]

    def test_the_probe_never_raises_on_this_host(self):
        """Whatever this machine is, probing must return a usable reading."""
        r = probe()
        assert r.accessibility in {v.value for v in Verdict}
        assert r.screen_recording in {v.value for v in Verdict}
        assert isinstance(r.as_dict(), dict)

    def test_a_missing_bundle_identity_is_explained(self):
        """The root cause of the Xcode trap, surfaced rather than inferred."""
        r = probe()
        import platform
        if platform.system() != "Darwin":
            pytest.skip("macOS only")
        if not r.bundle_identifier:
            assert any("code identity" in n.lower() for n in r.notes)

    def test_the_master_switch_disables_probing(self, monkeypatch):
        monkeypatch.setenv("JARVIS_TCC_SENTINEL_ENABLED", "0")
        r = probe()
        assert r.denied() == []
        assert any("disabled" in n for n in r.notes)

    def test_render_never_raises(self):
        assert render(TCCReading())
        assert render(_reading(acc=Verdict.DENIED))


class TestAgainstThisMachine:
    def test_it_explains_the_black_box_window_failure(self):
        """The measured link: no screen-recording grant is why
        `cg_window_capture` returned nothing and the black box recorded
        'window list unavailable' — which read as a bug, not a permission."""
        import platform
        if platform.system() != "Darwin":
            pytest.skip("macOS only")
        r = probe()
        if r.verdict(Grant.SCREEN_RECORDING) is Verdict.DENIED:
            from backend.vision import black_box as bb
            snap = bb._blocking_capture()
            assert snap.window_title is None, (
                "titles resolved without a screen-recording grant?")

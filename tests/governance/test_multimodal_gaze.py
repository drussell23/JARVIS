"""Multimodal Vision Plane + VBIA→PAVA + Ignition Reporter spine.

Mandate 4 verbatim (2026-07-19): a semantic visual request ("Jarvis,
what is on my screen?") while thermal state is forced SERIOUS →
capture aborted, thermal lock caught, Daniel verbally warns.
"""
from __future__ import annotations

import numpy as np
import pytest

from backend.core.ouroboros.governance.comms.duplex.semantic_gaze import (
    VERDICT_ANALYZED,
    VERDICT_CACHED,
    VERDICT_DORMANT,
    VERDICT_THERMAL_LOCKED,
    SemanticGaze,
    has_visual_intent,
)
from backend.core.ouroboros.governance.comms.duplex.pava_handoff import (
    IgnitionReporter,
    PavaDriftModulator,
)
from backend.core.ouroboros.governance.comms.duplex import epistemic_bus as _eb


@pytest.fixture(autouse=True)
def _fresh_epistemic_bus():
    # The bus is a process-wide root pointer (by design) — tests must
    # never inherit a previous test's gaze.
    _eb.reset_default_bus()
    yield
    _eb.reset_default_bus()


class TestSemanticGating:
    def test_visual_intent_vocabulary(self):
        assert has_visual_intent("Jarvis, what is on my screen?")
        assert has_visual_intent("look at this code")
        assert has_visual_intent("what's this error")
        assert not has_visual_intent("what time is it")
        assert not has_visual_intent("play some music")

    async def test_dormant_without_lease_or_intent(self):
        captured = {"n": 0}
        gaze = SemanticGaze(
            lease_active=lambda: True, thermal_ok=lambda: True,
            capture=lambda: captured.__setitem__("n", captured["n"] + 1),
            vlm=None,
        )
        # visual intent but... has intent yet no VLM → still no capture
        r = await gaze.request("what time is it")   # no visual intent
        assert r["verdict"] == VERDICT_DORMANT
        assert captured["n"] == 0                    # NEVER captured

    async def test_no_lease_stays_dormant(self):
        captured = {"n": 0}
        gaze = SemanticGaze(
            lease_active=lambda: False, thermal_ok=lambda: True,
            capture=lambda: captured.__setitem__("n", captured["n"] + 1),
        )
        r = await gaze.request("look at my screen")
        assert r["verdict"] == VERDICT_DORMANT
        assert captured["n"] == 0


class TestThermalLock:
    async def test_serious_thermal_aborts_capture_daniel_warns(self):
        """MANDATE 4 VERBATIM."""
        captured = {"n": 0}
        spoken = []

        async def _speak(text, persona="daniel"):
            spoken.append((text, persona))
            return True

        gaze = SemanticGaze(
            lease_active=lambda: True,
            thermal_ok=lambda: False,          # SERIOUS thermal forced
            capture=lambda: captured.__setitem__("n", captured["n"] + 1),
            vlm=None, speak=_speak,
        )
        r = await gaze.request("Jarvis, what is on my screen?")
        assert r["verdict"] == VERDICT_THERMAL_LOCKED
        assert captured["n"] == 0                     # capture ABORTED
        assert len(spoken) == 1                       # Daniel spoke
        text, persona = spoken[0]
        assert persona == "daniel"
        assert "thermally degraded" in text.lower()
        assert gaze.stats["thermal_locks"] == 1

    async def test_dry_thermal_hook_matches_audio_plane_pin(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[2]
            / "backend/core/ouroboros/governance/comms/duplex/semantic_gaze.py"
        ).read_text()
        # Same governor verdict the audio plane consumes (mandate 3).
        assert "evolution_permitted" in src


class TestDeltaPruning:
    async def test_unchanged_screen_bypasses_vlm(self):
        vlm_calls = {"n": 0}

        async def _vlm(frame, cmd):
            vlm_calls["n"] += 1
            return f"analysis-{vlm_calls['n']}"

        gaze = SemanticGaze(
            lease_active=lambda: True, thermal_ok=lambda: True,
            capture=lambda: b"static-frame-bytes",
            frame_hash=lambda f: "STABLE", vlm=_vlm,
        )
        r1 = await gaze.request("look at the screen")
        assert r1["verdict"] == VERDICT_ANALYZED
        r2 = await gaze.request("look at the screen again")  # same hash
        assert r2["verdict"] == VERDICT_CACHED
        assert r2["semantic_state"] == r1["semantic_state"]
        assert vlm_calls["n"] == 1                    # VLM bypassed

    async def test_changed_screen_reanalyzes(self):
        vlm_calls = {"n": 0}
        hashes = iter(["A", "B"])

        async def _vlm(frame, cmd):
            vlm_calls["n"] += 1
            return f"v{vlm_calls['n']}"

        gaze = SemanticGaze(
            lease_active=lambda: True, thermal_ok=lambda: True,
            capture=lambda: b"x", frame_hash=lambda f: next(hashes),
            vlm=_vlm,
        )
        await gaze.request("look")
        await gaze.request("look")
        assert vlm_calls["n"] == 2                    # changed → re-VLM


class TestPavaHandoff:
    async def test_clean_sample_full_alpha(self):
        async def _pava(w):
            return 0.95, True                         # plausible, clean
        mod = PavaDriftModulator(pava_scorer=_pava)
        a = await mod.modulated_alpha(np.ones(1000), 0.02)
        assert a > 0.015                              # near-full teaching
        assert mod.stats["handoffs"] == 1

    async def test_spoof_vetoes_learning(self):
        async def _pava(w):
            return 0.9, False                         # anti-spoof FAILED
        mod = PavaDriftModulator(pava_scorer=_pava)
        a = await mod.modulated_alpha(np.ones(1000), 0.02)
        assert a == 0.0                               # never teach a spoof
        assert mod.stats["spoof_rejected"] == 1

    async def test_drift_slows_learning(self):
        scores = iter([(0.9, True), (0.4, True), (0.9, True), (0.3, True)])
        async def _pava(w):
            return next(scores)
        mod = PavaDriftModulator(pava_scorer=_pava)
        for _ in range(4):
            a = await mod.modulated_alpha(np.ones(1000), 0.02)
        assert mod.stats["drift_slowed"] >= 1         # variance damped alpha

    def test_evolution_consults_pava_pin(self):
        from pathlib import Path
        src = (
            Path(__file__).resolve().parents[2]
            / "backend/core/ouroboros/governance/comms/duplex/sentry_bootstrap.py"
        ).read_text()
        assert "modulated_alpha" in src


class TestIgnitionReporter:
    async def test_boot_report_composed_and_published(self):
        published = []
        rep = IgnitionReporter(publish=published.append)
        rep.record_encoder_load(1.4)
        rep.record_cosine(0.82)
        rep.record_cosine(0.79)
        rep.record_threshold(0.70)
        rep.record_thermal("nominal")
        rep.record_capture_state("ACTIVE")
        report = await rep.finalize()
        assert "SYSTEM_BOOT_REPORT" in report
        assert "1400.0ms" in report and "0.82/0.79" in report
        assert "capture ACTIVE" in report and "thermal nominal" in report
        assert published == [report]                  # silent IPC delivered

    async def test_spoken_digest_optional(self):
        spoken = []
        async def _speak(t, p="daniel"):
            spoken.append((t, p)); return True
        rep = IgnitionReporter(publish=lambda _s: None, speak=_speak)
        rep.record_encoder_load(0.9)
        await rep.finalize(spoken=True)
        assert spoken and spoken[0][1] == "daniel"
        assert "online" in spoken[0][0].lower()

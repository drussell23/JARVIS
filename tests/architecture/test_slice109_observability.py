"""Slice 109 — God-Tier Observability Matrix & Voice Ignition.

Deterministic mock matrix for the decoupled telemetry router. NO TTY, NO audio,
NO live bus required: we intercept the SSE ``publish_task_event`` seam and inject
a recording narrator, then assert with mathematical certainty that

  * the structured Why-Snapshot JSON is correctly shaped (schema + keys + band);
  * the SSE publish fires under the right event type and round-trips through the
    time-travel ledger;
  * TTS triggers fire ONLY when ``JARVIS_KAREN_VOICE_ENABLED`` is active AND
    Karen is unmuted AND the event is high-severity — and the gate is
    fail-closed when the mute state cannot be read.

The real audio path (``KarenPreambleVoice.speak`` → ``safe_say`` → macOS ``say``)
is a LOCAL operator verification on a TTY + audio device; here we prove the
wiring/gating logic that decides whether anything is ever queued to it.
"""

from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import cognitive_observability as CO


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_narrator():
    """Each test starts with no injected narrator + a clean voice/obs gate."""
    CO.set_narrator_for_test(None)
    yield
    CO.set_narrator_for_test(None)


class _RecordingSay:
    """Async say_fn double matching DaemonNarrator's contract."""

    def __init__(self):
        self.calls = []

    async def __call__(self, message, *, voice=None, source=None, skip_dedup=False):
        self.calls.append({"message": message, "voice": voice, "source": source})
        return True


def _real_narrator(say):
    """A REAL DaemonNarrator wired to a recording say_fn (rate-limit off), so we
    exercise the genuine template/format path deterministically."""
    from backend.core.ouroboros.daemon_narrator import DaemonNarrator

    return DaemonNarrator(say_fn=say, rate_limit_s=0.0, enabled=True)


# ===========================================================================
# Phase 1 — the structured Why-Snapshot payload
# ===========================================================================


class TestWhySnapshotPayload:
    def test_schema_version_and_required_keys(self):
        snap = CO.build_why_snapshot(
            kind="post_apply", op_id="op-1",
            payload={"phase": "APPLY", "state": "applied", "confidence": 0.92},
        )
        assert snap["schema_version"] == "cognitive_why_snapshot.v1"
        assert snap["op_id"] == "op-1"
        assert snap["kind"] == "post_apply"
        assert snap["phase"] == "APPLY"
        # The "why" sub-object is the time-travel decision context.
        why = snap["why"]
        for key in (
            "confidence_aura", "confidence_score", "shannon_entropy",
            "decision_prior_distribution", "recursion_depth", "rehearsal_verdict",
        ):
            assert key in why, f"missing why-key: {key}"

    def test_confidence_band_high_medium_low(self):
        hi = CO.build_why_snapshot(kind="post_apply", op_id="o", payload={"confidence": 0.92})
        mid = CO.build_why_snapshot(kind="post_apply", op_id="o", payload={"confidence": 0.6})
        lo = CO.build_why_snapshot(kind="post_apply", op_id="o", payload={"confidence": 0.2})
        assert hi["why"]["confidence_aura"] == "high"
        assert mid["why"]["confidence_aura"] == "medium"
        assert lo["why"]["confidence_aura"] == "low"

    def test_confidence_absent_is_none_not_crash(self):
        snap = CO.build_why_snapshot(kind="post_failure", op_id="o", payload={})
        assert snap["why"]["confidence_aura"] is None
        assert snap["why"]["confidence_score"] is None

    def test_decision_prior_distribution_is_a_dict(self):
        # Whatever the live belief substrate reports, it must be a JSON-safe dict.
        snap = CO.build_why_snapshot(kind="post_apply", op_id="o", payload={})
        assert isinstance(snap["why"]["decision_prior_distribution"], dict)

    def test_target_files_truncated_and_stringified(self):
        snap = CO.build_why_snapshot(
            kind="post_apply", op_id="o",
            payload={"target_files": [f"f{i}.py" for i in range(40)]},
        )
        assert len(snap["target_files"]) == 24
        assert all(isinstance(f, str) for f in snap["target_files"])

    def test_snapshot_is_json_serializable(self):
        import json
        snap = CO.build_why_snapshot(
            kind="post_apply", op_id="o",
            payload={"confidence": 0.7, "risk_tier": "notify_apply"},
        )
        json.dumps(snap)  # must not raise


# ===========================================================================
# Phase 1 — SSE publish + time-travel ledger
# ===========================================================================


class TestSSEPublish:
    def test_publish_emits_correct_event_type_and_structured_payload(self, monkeypatch):
        from backend.core.ouroboros.governance import ide_observability_stream as S
        captured = {}

        def _fake_publish(event_type, op_id, payload):
            captured["event_type"] = event_type
            captured["op_id"] = op_id
            captured["payload"] = payload

        monkeypatch.setattr(S, "publish_task_event", _fake_publish)
        monkeypatch.setenv("JARVIS_COGNITIVE_OBSERVABILITY_ENABLED", "1")

        snap = CO.publish_why_snapshot(
            kind="post_apply", op_id="op-7",
            payload={"phase": "APPLY", "confidence": 0.9},
        )
        assert snap is not None
        assert captured["event_type"] == S.EVENT_TYPE_COGNITIVE_WHY_SNAPSHOT
        assert captured["op_id"] == "op-7"
        # The SSE payload IS the structured snapshot — not a flat string.
        assert isinstance(captured["payload"], dict)
        assert captured["payload"]["schema_version"] == "cognitive_why_snapshot.v1"
        assert captured["payload"]["why"]["confidence_aura"] == "high"

    def test_publish_roundtrips_through_time_travel_ledger(self, monkeypatch):
        from backend.core.ouroboros.governance import ide_observability_stream as S
        monkeypatch.setattr(S, "publish_task_event", lambda *a, **k: None)
        monkeypatch.setenv("JARVIS_COGNITIVE_OBSERVABILITY_ENABLED", "1")

        CO.publish_why_snapshot(kind="post_failure", op_id="op-travel",
                                payload={"phase": "VERIFY", "confidence": 0.3})
        got = CO.why_snapshot_for_op("op-travel")
        assert got is not None
        assert got["op_id"] == "op-travel"
        assert got["why"]["confidence_aura"] == "low"
        assert any(s["op_id"] == "op-travel" for s in CO.recent_why_snapshots(20))

    def test_publish_is_noop_when_observability_disabled(self, monkeypatch):
        from backend.core.ouroboros.governance import ide_observability_stream as S
        called = {"n": 0}
        monkeypatch.setattr(S, "publish_task_event",
                            lambda *a, **k: called.__setitem__("n", called["n"] + 1))
        monkeypatch.setenv("JARVIS_COGNITIVE_OBSERVABILITY_ENABLED", "0")
        out = CO.publish_why_snapshot(kind="post_apply", op_id="x", payload={})
        assert out is None
        assert called["n"] == 0


# ===========================================================================
# Phase 2 — severity classification
# ===========================================================================


class TestSeverityClassification:
    def test_containment_breach_is_high(self):
        et, sev = CO.classify_severity("post_apply", {"containment_breach": True})
        assert sev == "high" and et == "cognitive.containment_breach"

    def test_containment_in_reason_is_high(self):
        et, sev = CO.classify_severity("post_failure", {"reason": "VERIFY containment violation"})
        assert sev == "high" and et == "cognitive.containment_breach"

    def test_graduation_threshold_is_high(self):
        et, sev = CO.classify_severity("post_apply", {"graduation_threshold_met": True})
        assert sev == "high" and et == "cognitive.graduation_threshold_met"

    def test_load_shedding_is_high(self):
        et, sev = CO.classify_severity("post_apply", {"load_shedding_active": True})
        assert sev == "high" and et == "cognitive.load_shedding_active"

    def test_post_failure_is_high(self):
        et, sev = CO.classify_severity("post_failure", {})
        assert sev == "high" and et == "cognitive.post_failure"

    def test_post_apply_is_normal(self):
        et, sev = CO.classify_severity("post_apply", {})
        assert sev == "normal" and et == "cognitive.post_apply"

    def test_unknown_is_low(self):
        et, sev = CO.classify_severity("intake_accept", {})
        assert sev == "low" and et is None


# ===========================================================================
# Phase 2 — voice gating (the load-bearing safety logic)
# ===========================================================================


class TestVoiceGating:
    def _enable_voice(self, monkeypatch):
        monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "1")
        # KarenConfig unmuted defaults.
        monkeypatch.setenv("OUROBOROS_NARRATOR_ENABLED", "1")
        monkeypatch.setenv("JARVIS_KAREN_TOOL_VOICE_ENABLED", "1")

    def test_tts_fires_when_enabled_unmuted_and_high(self, monkeypatch):
        self._enable_voice(monkeypatch)
        say = _RecordingSay()
        CO.set_narrator_for_test(_real_narrator(say))
        fired = asyncio.run(CO.narrate_event(
            kind="post_failure", op_id="op-9", payload={"phase": "VERIFY"}))
        assert fired is True
        assert len(say.calls) == 1
        # Dynamically formatted from the lifecycle payload.
        assert "op-9" in say.calls[0]["message"]
        assert "VERIFY" in say.calls[0]["message"]

    def test_no_tts_when_master_flag_off(self, monkeypatch):
        monkeypatch.delenv("JARVIS_KAREN_VOICE_ENABLED", raising=False)
        monkeypatch.setenv("OUROBOROS_NARRATOR_ENABLED", "1")
        monkeypatch.setenv("JARVIS_KAREN_TOOL_VOICE_ENABLED", "1")
        say = _RecordingSay()
        CO.set_narrator_for_test(_real_narrator(say))
        fired = asyncio.run(CO.narrate_event(
            kind="post_failure", op_id="op-9", payload={}))
        assert fired is False
        assert say.calls == []

    def test_no_tts_when_muted(self, monkeypatch):
        monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "1")
        # Muted: tool-voice sub-switch off.
        monkeypatch.setenv("OUROBOROS_NARRATOR_ENABLED", "1")
        monkeypatch.setenv("JARVIS_KAREN_TOOL_VOICE_ENABLED", "0")
        say = _RecordingSay()
        CO.set_narrator_for_test(_real_narrator(say))
        fired = asyncio.run(CO.narrate_event(
            kind="post_failure", op_id="op-9", payload={}))
        assert fired is False
        assert say.calls == []

    def test_no_tts_for_normal_severity_even_when_enabled(self, monkeypatch):
        self._enable_voice(monkeypatch)
        say = _RecordingSay()
        CO.set_narrator_for_test(_real_narrator(say))
        # post_apply is "normal" severity — must not speak.
        fired = asyncio.run(CO.narrate_event(
            kind="post_apply", op_id="op-9", payload={"confidence": 0.9}))
        assert fired is False
        assert say.calls == []

    def test_voice_gate_is_fail_closed_when_config_unreadable(self, monkeypatch):
        self._enable_voice(monkeypatch)
        # Make KarenConfig construction blow up → mute state unknown → silence.
        import backend.core.ouroboros.governance.comms.karen_voice as KV

        class _Boom:
            def __init__(self, *a, **k):
                raise RuntimeError("audio subsystem unavailable")

        monkeypatch.setattr(KV, "KarenConfig", _Boom)
        say = _RecordingSay()
        CO.set_narrator_for_test(_real_narrator(say))
        fired = asyncio.run(CO.narrate_event(
            kind="post_failure", op_id="op-9", payload={}))
        assert fired is False
        assert say.calls == []


# ===========================================================================
# Phase 1+2 — bus subscribers + boot registration wiring
# ===========================================================================


class _FakeEvent:
    def __init__(self, payload):
        self.payload = payload


class TestBusSubscribers:
    def test_unpack_extracts_kind_op_payload(self):
        ev = _FakeEvent({"lifecycle_kind": "post_apply", "op_id": "z", "phase": "APPLY"})
        kind, op_id, payload = CO._unpack(ev)
        assert kind == "post_apply" and op_id == "z"
        assert payload["phase"] == "APPLY"

    def test_unpack_tolerates_garbage(self):
        assert CO._unpack(object()) == ("", "", {})

    def test_observability_subscriber_publishes_snapshot(self, monkeypatch):
        from backend.core.ouroboros.governance import ide_observability_stream as S
        seen = {}
        monkeypatch.setattr(S, "publish_task_event",
                            lambda et, op, pl: seen.update(et=et, op=op, pl=pl))
        monkeypatch.setenv("JARVIS_COGNITIVE_OBSERVABILITY_ENABLED", "1")
        ev = _FakeEvent({"lifecycle_kind": "post_apply", "op_id": "sub-1",
                         "phase": "APPLY", "confidence": 0.9})
        asyncio.run(CO._on_lifecycle_observability(ev))
        assert seen["et"] == S.EVENT_TYPE_COGNITIVE_WHY_SNAPSHOT
        assert seen["op"] == "sub-1"
        assert seen["pl"]["why"]["confidence_aura"] == "high"

    def test_voice_subscriber_narrates_high_severity(self, monkeypatch):
        monkeypatch.setenv("JARVIS_KAREN_VOICE_ENABLED", "1")
        monkeypatch.setenv("OUROBOROS_NARRATOR_ENABLED", "1")
        monkeypatch.setenv("JARVIS_KAREN_TOOL_VOICE_ENABLED", "1")
        say = _RecordingSay()
        CO.set_narrator_for_test(_real_narrator(say))
        ev = _FakeEvent({"lifecycle_kind": "post_failure", "op_id": "sub-2",
                         "phase": "VERIFY"})
        asyncio.run(CO._on_lifecycle_voice(ev))
        assert len(say.calls) == 1
        assert "sub-2" in say.calls[0]["message"]

    def test_build_default_subscribers_labels(self):
        subs = CO.build_default_observability_subscribers()
        labels = {s.label for s in subs}
        assert labels == {"cognitive_observability_sse", "cognitive_observability_voice"}
        from backend.core.ouroboros.governance.cognitive_bus import lifecycle_pattern
        assert all(s.pattern == lifecycle_pattern() for s in subs)

    def test_register_observability_inert_when_bus_off(self, monkeypatch):
        monkeypatch.delenv("JARVIS_COGNITIVE_BUS_ENABLED", raising=False)
        ids = asyncio.run(CO.register_observability())
        assert ids == []

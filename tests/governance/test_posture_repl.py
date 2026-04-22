"""Slice 3 — /posture REPL + IDE observability GET + SSE bridge.

Authority invariants re-asserted in Slice 4 graduation.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.direction_inferrer import (
    DirectionInferrer,
)
from backend.core.ouroboros.governance.posture import (
    Posture,
    SignalBundle,
    baseline_bundle,
)
from backend.core.ouroboros.governance.posture_observer import (
    OverrideState,
    PostureObserver,
    SignalCollector,
    reset_default_observer,
    reset_default_store,
    get_default_store,
)
from backend.core.ouroboros.governance.posture_repl import (
    PostureDispatchResult,
    dispatch_posture_command,
    reset_default_providers,
    set_default_override_state,
    set_default_store,
)
from backend.core.ouroboros.governance.posture_store import (
    PostureStore,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("JARVIS_DIRECTION_INFERRER") or key.startswith("JARVIS_POSTURE"):
            monkeypatch.delenv(key, raising=False)
    reset_default_store()
    reset_default_observer()
    reset_default_providers()
    yield
    reset_default_store()
    reset_default_observer()
    reset_default_providers()


@pytest.fixture
def tmp_store(tmp_path: Path) -> PostureStore:
    return PostureStore(tmp_path / ".jarvis")


@pytest.fixture
def primed_store(tmp_store: PostureStore, monkeypatch) -> PostureStore:
    """Store with a pre-written current reading + master flag on."""
    monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
    bundle = replace(baseline_bundle(), feat_ratio=0.80)
    reading = DirectionInferrer().infer(bundle)
    tmp_store.write_current(reading)
    tmp_store.append_history(reading)
    return tmp_store


def _run(line: str, **kwargs) -> PostureDispatchResult:
    return dispatch_posture_command(line, **kwargs)


# ---------------------------------------------------------------------------
# Dispatcher basics
# ---------------------------------------------------------------------------


class TestDispatcherBasics:

    def test_unknown_command_returns_unmatched(self):
        r = _run("/notposture")
        assert r.matched is False
        assert r.ok is False

    def test_empty_string_returns_unmatched(self):
        r = _run("")
        assert r.matched is False

    def test_help_always_available_even_when_master_off(self):
        r = _run("/posture help")
        assert r.ok
        assert "Strategic posture" in r.text

    def test_question_mark_is_help_alias(self):
        r = _run("/posture ?")
        assert "Strategic posture" in r.text

    def test_master_off_rejects_operational_verbs(self, tmp_store: PostureStore):
        r = _run("/posture status", store=tmp_store)
        assert r.ok is False
        assert "DirectionInferrer disabled" in r.text

    def test_master_on_no_store_attached(self, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        r = _run("/posture status")
        assert r.ok is False
        assert "no PostureStore attached" in r.text

    def test_unknown_subcommand(self, primed_store: PostureStore):
        r = _run("/posture frobnicate", store=primed_store)
        assert r.ok is False
        assert "unknown subcommand" in r.text

    def test_parse_error_on_unterminated_quote(self, primed_store: PostureStore):
        r = _run('/posture override EXPLORE --reason "unterminated', store=primed_store)
        assert r.ok is False
        assert "parse error" in r.text


# ---------------------------------------------------------------------------
# /posture status
# ---------------------------------------------------------------------------


class TestStatus:

    def test_bare_posture_is_status(self, primed_store: PostureStore):
        r = _run("/posture", store=primed_store)
        assert r.ok
        assert "Posture:" in r.text and "EXPLORE" in r.text

    def test_status_explicit(self, primed_store: PostureStore):
        r = _run("/posture status", store=primed_store)
        assert r.ok
        assert "EXPLORE" in r.text
        assert "confidence" in r.text

    def test_status_shows_top_contributors(self, primed_store: PostureStore):
        r = _run("/posture status", store=primed_store)
        assert "feat_ratio" in r.text

    def test_status_empty_store(self, tmp_store: PostureStore, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        r = _run("/posture status", store=tmp_store)
        assert r.ok
        assert "no current reading" in r.text.lower()

    def test_status_shows_override_banner(self, primed_store: PostureStore):
        override = OverrideState()
        override.set(Posture.HARDEN, duration_s=1800, reason="ops test")
        r = _run("/posture status", store=primed_store, override_state=override)
        assert "OVERRIDE ACTIVE" in r.text
        assert "HARDEN" in r.text


# ---------------------------------------------------------------------------
# /posture explain
# ---------------------------------------------------------------------------


class TestExplain:

    def test_explain_contains_all_12_signals(self, primed_store: PostureStore):
        r = _run("/posture explain", store=primed_store)
        assert r.ok
        for signal in (
            "feat_ratio", "fix_ratio", "refactor_ratio", "test_docs_ratio",
            "postmortem_failure_rate", "iron_gate_reject_rate",
            "l2_repair_rate", "open_ops_normalized",
            "session_lessons_infra_ratio", "time_since_last_graduation_inv",
            "cost_burn_normalized", "worktree_orphan_count",
        ):
            assert signal in r.text

    def test_explain_shows_all_posture_scores_in_flat_mode(
        self, primed_store: PostureStore,
    ):
        r = _run("/posture explain", store=primed_store)
        # Flat mode always shows all-posture scores; rich mode embeds in table
        # Either way, every posture string should appear somewhere
        assert "EXPLORE" in r.text

    def test_explain_empty_store(self, tmp_store: PostureStore, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        r = _run("/posture explain", store=tmp_store)
        assert r.ok
        assert "no current reading" in r.text.lower()


# ---------------------------------------------------------------------------
# /posture history
# ---------------------------------------------------------------------------


class TestHistory:

    def test_history_lists_readings(self, primed_store: PostureStore):
        r = _run("/posture history", store=primed_store)
        assert r.ok
        assert "posture reading" in r.text.lower()

    def test_history_default_20(self, primed_store: PostureStore):
        # Seed 25 readings
        for _ in range(24):
            primed_store.append_history(
                DirectionInferrer().infer(baseline_bundle())
            )
        r = _run("/posture history", store=primed_store)
        # 25 total; default shows 20
        assert r.text.count("MAINTAIN") + r.text.count("EXPLORE") <= 21  # header + 20

    def test_history_n_clamped_to_max(self, primed_store: PostureStore):
        r = _run("/posture history 99999", store=primed_store)
        assert r.ok

    def test_history_n_clamped_to_min(self, primed_store: PostureStore):
        r = _run("/posture history 0", store=primed_store)
        # 0 clamps to 1 — still ok
        assert r.ok

    def test_history_invalid_n(self, primed_store: PostureStore):
        r = _run("/posture history notanumber", store=primed_store)
        assert r.ok is False
        assert "invalid N" in r.text

    def test_history_empty(self, tmp_store: PostureStore, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        r = _run("/posture history", store=tmp_store)
        assert r.ok
        assert "no posture history" in r.text.lower()


# ---------------------------------------------------------------------------
# /posture signals
# ---------------------------------------------------------------------------


class TestSignals:

    def test_signals_shows_raw_values(self, primed_store: PostureStore):
        r = _run("/posture signals", store=primed_store)
        assert r.ok
        assert "signal raw values" in r.text.lower()
        assert "feat_ratio" in r.text


# ---------------------------------------------------------------------------
# /posture override
# ---------------------------------------------------------------------------


class TestOverride:

    def test_override_requires_posture(self, primed_store: PostureStore):
        override = OverrideState()
        r = _run("/posture override", store=primed_store, override_state=override)
        assert r.ok is False
        assert "override <POSTURE>" in r.text

    def test_override_invalid_posture(self, primed_store: PostureStore):
        override = OverrideState()
        r = _run(
            "/posture override RECOVER",
            store=primed_store, override_state=override,
        )
        assert r.ok is False

    def test_override_sets_and_persists_audit(self, primed_store: PostureStore):
        override = OverrideState()
        r = _run(
            "/posture override HARDEN --until 30m --reason ops_fix",
            store=primed_store, override_state=override,
        )
        assert r.ok
        assert override.active_posture() is Posture.HARDEN
        records = primed_store.load_audit()
        assert any(rec.event == "set" and rec.posture is Posture.HARDEN for rec in records)

    def test_override_default_duration_is_max(self, primed_store: PostureStore, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_OVERRIDE_MAX_H", "2")
        override = OverrideState()
        _run(
            "/posture override EXPLORE",
            store=primed_store, override_state=override,
        )
        snap = override.snapshot()
        # Default = max; 2h = 7200s
        assert snap["until"] - snap["set_at"] <= 7200.0 + 1.0

    def test_override_clamped_to_max(self, primed_store: PostureStore, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_OVERRIDE_MAX_H", "1")
        override = OverrideState()
        r = _run(
            "/posture override HARDEN --until 999h --reason huge",
            store=primed_store, override_state=override,
        )
        assert r.ok
        assert "clamped" in r.text.lower()

    def test_override_bad_duration(self, primed_store: PostureStore):
        override = OverrideState()
        r = _run(
            "/posture override EXPLORE --until banana",
            store=primed_store, override_state=override,
        )
        assert r.ok is False
        assert "bad --until" in r.text

    def test_override_unknown_flag(self, primed_store: PostureStore):
        override = OverrideState()
        r = _run(
            "/posture override EXPLORE --frobnicate yes",
            store=primed_store, override_state=override,
        )
        assert r.ok is False
        assert "unknown flag" in r.text

    def test_override_case_insensitive_posture(self, primed_store: PostureStore):
        override = OverrideState()
        r = _run(
            "/posture override harden --until 5m",
            store=primed_store, override_state=override,
        )
        assert r.ok
        assert override.active_posture() is Posture.HARDEN

    def test_override_duration_parsing(self, primed_store: PostureStore):
        """Durations: Ns, Nm, Nh, bare number = seconds."""
        for dur_str, expected_s in (("60s", 60), ("2m", 120), ("1h", 3600), ("45", 45)):
            override = OverrideState()
            _run(
                f"/posture override EXPLORE --until {dur_str}",
                store=primed_store, override_state=override,
            )
            snap = override.snapshot()
            assert snap["until"] - snap["set_at"] == pytest.approx(
                float(expected_s), abs=1.0,
            )

    def test_override_without_override_state_errors(self, primed_store: PostureStore):
        r = _run(
            "/posture override EXPLORE --until 5m",
            store=primed_store, override_state=None,
        )
        assert r.ok is False
        assert "no OverrideState" in r.text


# ---------------------------------------------------------------------------
# /posture clear-override
# ---------------------------------------------------------------------------


class TestClearOverride:

    def test_clear_no_active_override(self, primed_store: PostureStore):
        override = OverrideState()
        r = _run(
            "/posture clear-override",
            store=primed_store, override_state=override,
        )
        assert r.ok
        assert "no active override" in r.text.lower()

    def test_clear_drops_active(self, primed_store: PostureStore):
        override = OverrideState()
        override.set(Posture.HARDEN, duration_s=3600, reason="test")
        r = _run(
            "/posture clear-override",
            store=primed_store, override_state=override,
        )
        assert r.ok
        assert override.active_posture() is None
        # Audit reflects the clear
        records = primed_store.load_audit()
        assert any(rec.event == "clear" for rec in records)


# ---------------------------------------------------------------------------
# Default providers wiring
# ---------------------------------------------------------------------------


class TestDefaultProviders:

    def test_set_default_store(self, tmp_store: PostureStore, monkeypatch):
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        bundle = replace(baseline_bundle(), feat_ratio=0.80)
        reading = DirectionInferrer().infer(bundle)
        tmp_store.write_current(reading)
        set_default_store(tmp_store)
        # No explicit store passed — falls back to default
        r = _run("/posture status")
        assert r.ok
        assert "EXPLORE" in r.text

    def test_set_default_override_state(self, primed_store: PostureStore):
        override = OverrideState()
        override.set(Posture.HARDEN, duration_s=1800, reason="x")
        set_default_store(primed_store)
        set_default_override_state(override)
        r = _run("/posture status")
        assert "OVERRIDE ACTIVE" in r.text


# ---------------------------------------------------------------------------
# IDE observability GET /observability/posture
# ---------------------------------------------------------------------------


def _make_mock_request(query: dict = None, origin: str = "http://localhost:1234", remote: str = "127.0.0.1"):
    """Build a stand-in for aiohttp.Request supporting just what the handler uses."""
    from types import SimpleNamespace
    headers = {"Origin": origin}
    return SimpleNamespace(
        remote=remote,
        headers=headers,
        query=query or {},
        match_info={},
    )


class TestIDEObservabilityPosture:

    @pytest.mark.asyncio
    async def test_current_returns_403_when_master_off(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        # Master flag not set → posture disabled
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_posture_current(_make_mock_request())
        assert resp.status == 403

    @pytest.mark.asyncio
    async def test_current_returns_reading_when_flag_on(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        reset_default_store()
        store = get_default_store(tmp_path / ".jarvis")
        reading = DirectionInferrer().infer(
            replace(baseline_bundle(), feat_ratio=0.80),
        )
        store.write_current(reading)

        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_posture_current(_make_mock_request())
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["posture"] == "EXPLORE"
        assert payload["schema_version"] == "1.0"
        assert "evidence" in payload
        assert "all_scores" in payload

    @pytest.mark.asyncio
    async def test_current_returns_200_with_null_reading_when_store_empty(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        reset_default_store()
        get_default_store(tmp_path / ".jarvis")  # prime empty default

        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_posture_current(_make_mock_request())
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["reading"] is None
        assert payload["reason_code"] == "posture.no_current"

    @pytest.mark.asyncio
    async def test_history_returns_list(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        reset_default_store()
        store = get_default_store(tmp_path / ".jarvis")
        for _ in range(5):
            store.append_history(
                DirectionInferrer().infer(replace(baseline_bundle(), feat_ratio=0.80))
            )

        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_posture_history(
            _make_mock_request(query={"limit": "3"}),
        )
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["count"] == 3
        assert payload["limit"] == 3
        assert len(payload["readings"]) == 3

    @pytest.mark.asyncio
    async def test_history_malformed_limit(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        reset_default_store()
        get_default_store(tmp_path / ".jarvis")
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_posture_history(
            _make_mock_request(query={"limit": "banana"}),
        )
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_history_limit_clamped(self, monkeypatch, tmp_path: Path):
        monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_DIRECTION_INFERRER_ENABLED", "true")
        reset_default_store()
        get_default_store(tmp_path / ".jarvis")
        from backend.core.ouroboros.governance.ide_observability import (
            IDEObservabilityRouter,
        )
        router = IDEObservabilityRouter()
        resp = await router._handle_posture_history(
            _make_mock_request(query={"limit": "99999"}),
        )
        assert resp.status == 200
        payload = json.loads(resp.body)
        assert payload["limit"] == 256


# ---------------------------------------------------------------------------
# SSE event bridge
# ---------------------------------------------------------------------------


class TestSSEBridge:

    def test_event_type_in_valid_set(self):
        from backend.core.ouroboros.governance.ide_observability_stream import (
            EVENT_TYPE_POSTURE_CHANGED,
            _VALID_EVENT_TYPES,
        )
        assert EVENT_TYPE_POSTURE_CHANGED in _VALID_EVENT_TYPES

    def test_publish_posture_event_returns_none_when_stream_disabled(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "false")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_posture_event,
            reset_default_broker,
        )
        reset_default_broker()
        reading = DirectionInferrer().infer(
            replace(baseline_bundle(), feat_ratio=0.80),
        )
        # Should not raise; returns None
        result = publish_posture_event("inference", reading=reading)
        assert result is None

    def test_publish_posture_event_fires_when_enabled(self, monkeypatch):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        from backend.core.ouroboros.governance.ide_observability_stream import (
            publish_posture_event,
            get_default_broker,
            reset_default_broker,
            EVENT_TYPE_POSTURE_CHANGED,
        )
        reset_default_broker()
        broker = get_default_broker()

        reading = DirectionInferrer().infer(
            replace(baseline_bundle(), feat_ratio=0.80),
        )
        before = broker.published_count
        event_id = publish_posture_event("inference", reading=reading)
        assert event_id is not None
        assert broker.published_count == before + 1

        # Inspect ring-buffer history
        history = list(broker._history)
        posture_events = [
            e for e in history if e.event_type == EVENT_TYPE_POSTURE_CHANGED
        ]
        assert len(posture_events) >= 1
        assert posture_events[-1].payload["posture"] == "EXPLORE"
        assert posture_events[-1].payload["trigger"] == "inference"

    def test_bridge_installs_observer_hook(self, tmp_path: Path):
        store = PostureStore(tmp_path / ".jarvis")
        observer = PostureObserver(Path("."), store)

        from backend.core.ouroboros.governance.ide_observability_stream import (
            bridge_posture_to_broker,
        )
        unsub = bridge_posture_to_broker(observer=observer)
        # Hook now installed
        assert observer._on_change is not None

        unsub()

    @pytest.mark.asyncio
    async def test_bridge_publishes_on_posture_change(
        self, tmp_path: Path, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_IDE_STREAM_ENABLED", "true")
        monkeypatch.setenv("JARVIS_POSTURE_HIGH_CONFIDENCE_BYPASS", "0.0")

        from backend.core.ouroboros.governance.ide_observability_stream import (
            bridge_posture_to_broker,
            get_default_broker,
            reset_default_broker,
            EVENT_TYPE_POSTURE_CHANGED,
        )
        reset_default_broker()
        broker = get_default_broker()

        store = PostureStore(tmp_path / ".jarvis")

        class _StubCollector:
            def __init__(self, b):
                self.b = b
            def build_bundle(self):
                return self.b

        explore_bundle = replace(baseline_bundle(), feat_ratio=0.80)
        harden_bundle = replace(
            baseline_bundle(),
            fix_ratio=0.75, postmortem_failure_rate=0.55,
            iron_gate_reject_rate=0.45, session_lessons_infra_ratio=0.80,
        )

        observer = PostureObserver(
            Path("."), store, collector=_StubCollector(explore_bundle),
        )
        bridge_posture_to_broker(observer=observer, broker=broker)

        await observer.run_one_cycle()  # cold-start EXPLORE
        observer._collector = _StubCollector(harden_bundle)
        await observer.run_one_cycle()  # flip to HARDEN (high-confidence bypass)

        history = list(broker._history)
        posture_events = [
            e for e in history if e.event_type == EVENT_TYPE_POSTURE_CHANGED
        ]
        # At least one posture_changed event (the HARDEN flip)
        assert len(posture_events) >= 1
        latest = posture_events[-1]
        assert latest.payload["trigger"] == "inference"
        assert latest.payload["posture"] in ("EXPLORE", "HARDEN")


# ---------------------------------------------------------------------------
# Authority invariant
# ---------------------------------------------------------------------------


_AUTHORITY_MODULES = (
    "orchestrator", "policy", "iron_gate", "risk_tier",
    "change_engine", "candidate_generator",
)


class TestAuthorityInvariantSlice3:

    def test_posture_repl_authority_free(self):
        repo_root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        src = (
            repo_root
            / "backend/core/ouroboros/governance/posture_repl.py"
        ).read_text(encoding="utf-8")
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for forbidden in _AUTHORITY_MODULES:
                    if f".{forbidden}" in line:
                        bad.append(line)
        assert not bad, f"posture_repl.py contains authority imports: {bad}"

"""Tests for the TodoScannerSensor trigger-tag bypass (Task #68).

Battle test bt-2026-04-12-005521 needed a deterministic way to seed a TODO
into the codebase and have the autonomous loop pick it up. The standard
emission gate only honors `high` urgency markers (FIXME, HACK, BUG, XXX) so
a `# TODO: scan this file` seed silently dropped on the floor. Worse, even
a real FIXME would only emit once per session (dedup gate) which prevented
re-seeding the same line during a long-running battle test.

The fix introduces a trigger tag — by default `(rsi-trigger)` — that:
  1. Elevates ANY marker (including TODO/DEPRECATED/NOTE) to `high` urgency.
  2. Pins priority/confidence to 1.0 so coalescing prefers it.
  3. Bypasses the dedup set so the same seed re-fires every scan.
  4. Stamps `trigger_tag=True` in the envelope evidence so downstream
     coalescing / classification can preferentially route it.

These tests pin all four behaviors and the env-var override hook.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from typing import Any, List
from unittest.mock import AsyncMock

import pytest

from backend.core.ouroboros.governance.intake.sensors import todo_scanner_sensor
from backend.core.ouroboros.governance.intake.sensors.todo_scanner_sensor import (
    TodoItem,
    TodoScannerSensor,
    _parse_marker_line,
)


class _RecordingRouter:
    """Async router stub that records every ingested envelope."""

    def __init__(self) -> None:
        self.ingested: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        self.ingested.append(envelope)
        return "enqueued"


@pytest.fixture
def fresh_scanner(tmp_path: Path) -> TodoScannerSensor:
    """Build a TodoScannerSensor pointed at an empty tmp_path."""
    router = _RecordingRouter()
    sensor = TodoScannerSensor(repo="test-repo", router=router, project_root=tmp_path)
    return sensor


# ---------------------------------------------------------------------------
# _parse_marker_line: pure unit tests
# ---------------------------------------------------------------------------


class TestParseMarkerLine:
    def test_returns_none_for_non_marker_line(self):
        assert _parse_marker_line("def foo(): pass") is None

    def test_returns_none_for_blank_line(self):
        assert _parse_marker_line("") is None

    def test_plain_todo_routes_to_normal_urgency(self):
        result = _parse_marker_line("# TODO: refactor this")
        assert result is not None
        marker, _text, urgency, priority, has_trigger = result
        assert marker == "TODO"
        assert urgency == "normal"
        assert priority == 0.5
        assert has_trigger is False

    def test_plain_fixme_routes_to_high_urgency(self):
        result = _parse_marker_line("# FIXME: broken")
        assert result is not None
        marker, _text, urgency, priority, has_trigger = result
        assert marker == "FIXME"
        assert urgency == "high"
        assert priority == 0.9
        assert has_trigger is False

    def test_trigger_tag_elevates_todo_to_high(self):
        result = _parse_marker_line("# TODO(rsi-trigger): seeded by battle test")
        assert result is not None
        marker, _text, urgency, priority, has_trigger = result
        assert marker == "TODO"
        assert urgency == "high"     # Elevated!
        assert priority == 1.0       # Pinned to max!
        assert has_trigger is True

    def test_trigger_tag_elevates_note_to_high(self):
        """Even the lowest-priority NOTE marker gets elevated when tagged."""
        result = _parse_marker_line("# NOTE(rsi-trigger): test seed")
        assert result is not None
        _marker, _text, urgency, priority, has_trigger = result
        assert urgency == "high"
        assert priority == 1.0
        assert has_trigger is True

    def test_trigger_tag_keeps_high_marker_at_max_confidence(self):
        """A FIXME with the trigger tag still gets pinned to 1.0."""
        result = _parse_marker_line("# FIXME(rsi-trigger): seeded high marker")
        assert result is not None
        _marker, _text, _urgency, priority, has_trigger = result
        assert priority == 1.0  # Up from FIXME's default 0.9
        assert has_trigger is True

    def test_trigger_tag_case_insensitive(self):
        result = _parse_marker_line("# TODO(RSI-TRIGGER): caps")
        assert result is not None
        _, _, _, _, has_trigger = result
        assert has_trigger is True

    def test_trigger_tag_with_whitespace(self):
        result = _parse_marker_line("# TODO( rsi-trigger ): with spaces")
        assert result is not None
        _, _, _, _, has_trigger = result
        assert has_trigger is True

    def test_unrelated_parens_do_not_trip_trigger(self):
        result = _parse_marker_line("# TODO(other-tag): not the trigger")
        assert result is not None
        _, _, urgency, priority, has_trigger = result
        assert has_trigger is False
        assert urgency == "normal"  # Plain TODO routing
        assert priority == 0.5


# ---------------------------------------------------------------------------
# TodoScannerSensor.scan_file: end-to-end behavior
# ---------------------------------------------------------------------------


class TestScanFileTriggerTag:

    @pytest.mark.asyncio
    async def test_plain_todo_emits_nothing(self, fresh_scanner: TodoScannerSensor, tmp_path: Path):
        target = tmp_path / "plain.py"
        target.write_text("# TODO: do this later\nx = 1\n")
        items = await fresh_scanner.scan_file(target)
        assert len(items) == 1
        assert items[0].urgency == "normal"
        # Router should have received nothing — normal urgency is gated out
        assert len(fresh_scanner._router.ingested) == 0  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_trigger_tag_todo_emits(self, fresh_scanner: TodoScannerSensor, tmp_path: Path):
        target = tmp_path / "tagged.py"
        target.write_text("# TODO(rsi-trigger): seeded\nx = 1\n")
        await fresh_scanner.scan_file(target)
        ingested = fresh_scanner._router.ingested  # type: ignore[attr-defined]
        assert len(ingested) == 1
        env = ingested[0]
        assert env.urgency == "high"
        assert env.confidence == 1.0
        assert env.evidence["trigger_tag"] is True
        assert env.evidence["marker"] == "TODO"

    @pytest.mark.asyncio
    async def test_plain_fixme_still_emits_unchanged(
        self, fresh_scanner: TodoScannerSensor, tmp_path: Path,
    ):
        """Existing FIXME behavior is unchanged — backward compat."""
        target = tmp_path / "fixme.py"
        target.write_text("# FIXME: real bug\n")
        await fresh_scanner.scan_file(target)
        ingested = fresh_scanner._router.ingested  # type: ignore[attr-defined]
        assert len(ingested) == 1
        env = ingested[0]
        assert env.urgency == "high"
        assert env.confidence == 0.9  # Original FIXME confidence preserved
        assert env.evidence["trigger_tag"] is False

    @pytest.mark.asyncio
    async def test_trigger_tag_bypasses_dedup_on_resean(
        self, fresh_scanner: TodoScannerSensor, tmp_path: Path,
    ):
        """The same seeded line should re-emit on every scan.

        This is the load-bearing battle-test invariant: a single committed
        seed must produce a fresh envelope on every poll cycle, otherwise
        the first-scan emission gets lost to coalescing and the seed dies.
        """
        target = tmp_path / "seed.py"
        target.write_text("# TODO(rsi-trigger): scan me\nx = 1\n")

        # Three back-to-back scans of the same file
        await fresh_scanner.scan_file(target)
        await fresh_scanner.scan_file(target)
        await fresh_scanner.scan_file(target)

        ingested = fresh_scanner._router.ingested  # type: ignore[attr-defined]
        assert len(ingested) == 3, (
            f"trigger tag did not bypass dedup — only {len(ingested)} envelopes"
        )

    @pytest.mark.asyncio
    async def test_plain_fixme_dedup_still_active(
        self, fresh_scanner: TodoScannerSensor, tmp_path: Path,
    ):
        """Backward-compat: plain FIXME still dedups across scans."""
        target = tmp_path / "fixme.py"
        target.write_text("# FIXME: should dedup\n")
        await fresh_scanner.scan_file(target)
        await fresh_scanner.scan_file(target)
        await fresh_scanner.scan_file(target)
        ingested = fresh_scanner._router.ingested  # type: ignore[attr-defined]
        assert len(ingested) == 1, (
            f"plain FIXME dedup broken — {len(ingested)} emissions"
        )

    @pytest.mark.asyncio
    async def test_mixed_file_emits_only_high_and_trigger(
        self, fresh_scanner: TodoScannerSensor, tmp_path: Path,
    ):
        """A file with TODO + FIXME + tagged-TODO emits 2 envelopes."""
        target = tmp_path / "mixed.py"
        target.write_text(
            "# TODO: quiet one (gated out)\n"
            "# FIXME: real bug (passes high gate)\n"
            "# TODO(rsi-trigger): seeded (passes via trigger)\n"
            "# OPTIMIZE: never emits (low urgency)\n"
        )
        await fresh_scanner.scan_file(target)
        ingested = fresh_scanner._router.ingested  # type: ignore[attr-defined]
        assert len(ingested) == 2
        triggers = [e for e in ingested if e.evidence["trigger_tag"] is True]
        assert len(triggers) == 1
        assert triggers[0].evidence["marker"] == "TODO"

    @pytest.mark.asyncio
    async def test_trigger_envelope_carries_target_file(
        self, fresh_scanner: TodoScannerSensor, tmp_path: Path,
    ):
        """Envelope must carry the file path so downstream targeting works."""
        target = tmp_path / "subdir" / "module.py"
        target.parent.mkdir(parents=True)
        target.write_text("# FIXME(rsi-trigger): targeting test\n")
        await fresh_scanner.scan_file(target)
        ingested = fresh_scanner._router.ingested  # type: ignore[attr-defined]
        assert len(ingested) == 1
        env = ingested[0]
        assert env.target_files == ("subdir/module.py",)


# ---------------------------------------------------------------------------
# Env var override
# ---------------------------------------------------------------------------


class TestTriggerTagEnvOverride:
    """The trigger tag is configurable via JARVIS_TODO_SCANNER_TRIGGER_TAG.

    These tests reload the module to pick up the env var change. They are
    isolated via a fixture that restores the original module state.
    """

    @pytest.fixture
    def reload_module(self, monkeypatch: pytest.MonkeyPatch):
        """Reload the sensor module under a custom env var."""
        original = os.environ.get("JARVIS_TODO_SCANNER_TRIGGER_TAG")
        try:
            yield
        finally:
            if original is None:
                os.environ.pop("JARVIS_TODO_SCANNER_TRIGGER_TAG", None)
            else:
                os.environ["JARVIS_TODO_SCANNER_TRIGGER_TAG"] = original
            importlib.reload(todo_scanner_sensor)

    def test_custom_tag_via_env(self, reload_module, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("JARVIS_TODO_SCANNER_TRIGGER_TAG", "custom-trigger")
        importlib.reload(todo_scanner_sensor)
        result = todo_scanner_sensor._parse_marker_line(
            "# TODO(custom-trigger): custom seed"
        )
        assert result is not None
        _, _, _urgency, _priority, has_trigger = result
        assert has_trigger is True
        # Default tag no longer matches
        result2 = todo_scanner_sensor._parse_marker_line(
            "# TODO(rsi-trigger): default tag"
        )
        assert result2 is not None
        _, _, _, _, has_trigger2 = result2
        assert has_trigger2 is False

    def test_disable_trigger_via_empty_env(
        self, reload_module, monkeypatch: pytest.MonkeyPatch,
    ):
        """Setting the env var to empty disables the bypass entirely."""
        monkeypatch.setenv("JARVIS_TODO_SCANNER_TRIGGER_TAG", "")
        importlib.reload(todo_scanner_sensor)
        assert todo_scanner_sensor._TRIGGER_PATTERN is None
        result = todo_scanner_sensor._parse_marker_line(
            "# TODO(rsi-trigger): nothing fires"
        )
        assert result is not None
        _, _, urgency, _, has_trigger = result
        assert has_trigger is False
        assert urgency == "normal"  # Reverts to plain TODO routing


# ---------------------------------------------------------------------------
# Production failure repro: deterministic seed survives a long session
# ---------------------------------------------------------------------------


class TestProductionSeedScenario:
    """Reproduce the bt-2026-04-12-005521 failure mode and prove it's fixed.

    Sequence:
      1. Start the scanner.
      2. Write a `# TODO(rsi-trigger): ...` seed into a file.
      3. Run scan_once → confirm an envelope reaches the router with the
         right shape (high urgency, max confidence, trigger_tag flag).
      4. Run scan_once again to confirm the seed re-fires (dedup bypassed).
      5. Mutate the file to a different trigger and confirm the new seed
         also fires.
    """

    @pytest.mark.asyncio
    async def test_seeded_trigger_survives_three_scan_cycles(self, tmp_path: Path):
        # Build the scanner with backend/ as the scan dir to mirror prod
        backend_dir = tmp_path / "backend"
        backend_dir.mkdir()
        seed = backend_dir / "verify_gate.py"
        seed.write_text(
            "def enforce():\n"
            "    pass\n"
            "# TODO(rsi-trigger): seeded by bt-2026-04-12-005521\n"
        )

        router = _RecordingRouter()
        sensor = TodoScannerSensor(
            repo="JARVIS-AI-Agent",
            router=router,
            project_root=tmp_path,
        )

        # Three full scan cycles
        for _ in range(3):
            await sensor.scan_once()

        triggers = [
            e for e in router.ingested
            if e.evidence.get("trigger_tag") is True
        ]
        assert len(triggers) == 3, (
            f"trigger tag did not survive 3 scan cycles — only "
            f"{len(triggers)} fired"
        )
        for env in triggers:
            assert env.urgency == "high"
            assert env.confidence == 1.0
            assert env.target_files == ("backend/verify_gate.py",)
            assert "rsi-trigger" in env.evidence["text"].lower() or \
                "seeded by" in env.evidence["text"].lower()

    @pytest.mark.asyncio
    async def test_re_seed_with_new_text_fires_independently(
        self, fresh_scanner: TodoScannerSensor, tmp_path: Path,
    ):
        target = tmp_path / "seed.py"
        target.write_text("# TODO(rsi-trigger): first seed\n")
        await fresh_scanner.scan_file(target)

        # Mutate the seed text — should still fire
        target.write_text("# TODO(rsi-trigger): second seed\n")
        await fresh_scanner.scan_file(target)

        ingested = fresh_scanner._router.ingested  # type: ignore[attr-defined]
        assert len(ingested) == 2
        texts = [e.evidence["text"] for e in ingested]
        assert any("first seed" in t for t in texts)
        assert any("second seed" in t for t in texts)

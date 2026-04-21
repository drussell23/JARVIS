"""Slice 1 Production Integration — ContextCompactor scorer path tests.

Pins:
* Env flag default-off
* Backward-compat: scorer-attached but flag-off → legacy path used
* Backward-compat: flag-on but no op_id → legacy path used
* Backward-compat: flag-on + op_id + scorer but scorer raises → legacy fallback
* Scorer path preserves intent-rich entries that legacy would compact away
* Safety floor (preserve_patterns) still applies on scorer path
* Manifest record emitted on scorer path
* compact() signature still accepts positional-only legacy call
* op_id plumbing doesn't affect legacy path
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Dict, List

import pytest

from backend.core.ouroboros.governance.context_compaction import (
    CompactionConfig,
    ContextCompactor,
    context_compactor_scorer_enabled,
)
from backend.core.ouroboros.governance.context_intent import (
    IntentTracker,
    PreservationScorer,
    TurnSource,
    intent_tracker_for,
    reset_default_tracker_registry,
)
from backend.core.ouroboros.governance.context_ledger import (
    reset_default_registry,
)
from backend.core.ouroboros.governance.context_manifest import (
    manifest_for,
    reset_default_manifest_registry,
)
from backend.core.ouroboros.governance.context_pins import (
    reset_default_pin_registries,
)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for key in list(os.environ.keys()):
        if key.startswith("JARVIS_CONTEXT_COMPACTOR_SCORER_"):
            monkeypatch.delenv(key, raising=False)
    reset_default_registry()
    reset_default_tracker_registry()
    reset_default_pin_registries()
    reset_default_manifest_registry()
    yield
    reset_default_registry()
    reset_default_tracker_registry()
    reset_default_pin_registries()
    reset_default_manifest_registry()


def _dialogue_with_intent_rich_oldest() -> List[Dict[str, Any]]:
    """15-entry dialogue; entry 0 mentions backend/hot.py; rest is noise."""
    entries = [
        {"type": "user", "role": "user",
         "content": "focus on backend/hot.py", "id": "m0"},
    ]
    for i in range(1, 15):
        entries.append(
            {"type": "assistant", "role": "assistant",
             "content": f"noise turn {i}", "id": f"m{i}"},
        )
    return entries


# ===========================================================================
# 1. Env flag defaults
# ===========================================================================


def test_env_flag_default_is_true_post_graduation(monkeypatch):
    """Graduated via real-session harness. Explicit =false still reverts."""
    monkeypatch.delenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", raising=False,
    )
    assert context_compactor_scorer_enabled() is True


def test_env_flag_explicit_true(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "true",
    )
    assert context_compactor_scorer_enabled() is True


def test_env_flag_malformed_reads_as_false(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "maybe",
    )
    assert context_compactor_scorer_enabled() is False


def test_env_flag_explicit_false_kill_switch(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "false",
    )
    assert context_compactor_scorer_enabled() is False


# ===========================================================================
# 2. Backward-compat defaults
# ===========================================================================


@pytest.mark.asyncio
async def test_legacy_path_when_flag_off(monkeypatch):
    """Kill switch: explicit =false forces legacy even with scorer attached."""
    monkeypatch.setenv("JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "false")
    entries = _dialogue_with_intent_rich_oldest()
    compactor = ContextCompactor(
        preservation_scorer=PreservationScorer(),
    )
    cfg = CompactionConfig(max_context_entries=5, preserve_count=3)
    intent_tracker_for("op-legacy").ingest_turn(
        "backend/hot.py", source=TurnSource.USER,
    )
    result = await compactor.compact(entries, cfg, op_id="op-legacy")
    assert result.entries_compacted > 0
    from backend.core.ouroboros.governance.context_manifest import (
        get_default_manifest_registry,
    )
    assert get_default_manifest_registry().active_op_ids() == []


@pytest.mark.asyncio
async def test_legacy_path_when_no_op_id(monkeypatch):
    """Flag on but op_id missing → legacy path."""
    monkeypatch.setenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "true",
    )
    entries = _dialogue_with_intent_rich_oldest()
    compactor = ContextCompactor(
        preservation_scorer=PreservationScorer(),
    )
    cfg = CompactionConfig(max_context_entries=5, preserve_count=3)
    # No op_id passed → legacy path
    result = await compactor.compact(entries, cfg)
    # No manifest because scorer path never ran
    from backend.core.ouroboros.governance.context_manifest import (
        get_default_manifest_registry,
    )
    assert get_default_manifest_registry().active_op_ids() == []


@pytest.mark.asyncio
async def test_legacy_path_when_no_scorer(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "true",
    )
    entries = _dialogue_with_intent_rich_oldest()
    # No scorer attached
    compactor = ContextCompactor()
    cfg = CompactionConfig(max_context_entries=5, preserve_count=3)
    result = await compactor.compact(entries, cfg, op_id="op-x")
    # Result still produced (via legacy)
    assert result.entries_before == 15


# ===========================================================================
# 3. Scorer path preserves intent-rich oldest entry
# ===========================================================================


@pytest.mark.asyncio
async def test_scorer_path_keeps_intent_rich_entry(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "true",
    )
    entries = _dialogue_with_intent_rich_oldest()
    compactor = ContextCompactor(
        preservation_scorer=PreservationScorer(),
    )
    cfg = CompactionConfig(max_context_entries=5, preserve_count=6)
    # Push a strong focus signal onto backend/hot.py
    tracker = intent_tracker_for("op-rich")
    for _ in range(5):
        tracker.ingest_turn(
            "keep backend/hot.py in mind", source=TurnSource.USER,
        )
    result = await compactor.compact(entries, cfg, op_id="op-rich")
    # The preserved_keys include chunk-id-style entries on scorer path
    assert any("m0" in k for k in result.preserved_keys), (
        "intent-rich chunk at index 0 must survive; "
        f"got preserved_keys={result.preserved_keys}"
    )


@pytest.mark.asyncio
async def test_scorer_path_records_manifest(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "true",
    )
    entries = _dialogue_with_intent_rich_oldest()
    compactor = ContextCompactor(
        preservation_scorer=PreservationScorer(),
    )
    cfg = CompactionConfig(max_context_entries=5, preserve_count=4)
    await compactor.compact(entries, cfg, op_id="op-manifest")
    # Manifest records exactly one pass
    m = manifest_for("op-manifest")
    recs = m.all_records()
    assert len(recs) == 1
    rec = recs[0]
    assert rec.kept_count + rec.compacted_count + rec.dropped_count == 15


# ===========================================================================
# 4. Safety floor still applies on scorer path
# ===========================================================================


@pytest.mark.asyncio
async def test_preserve_patterns_treated_as_pinned(monkeypatch):
    """Chunks matching the legacy safety regex must still survive the
    scorer path — the scorer treats them as pinned."""
    monkeypatch.setenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "true",
    )
    entries = [
        {"type": "noise", "content": "a", "id": "m0"},
        {"type": "noise", "content": "b", "id": "m1"},
        {"type": "security",  # matches default preserve_pattern "security"
         "content": "security breach noted", "id": "m2"},
    ]
    # Large budget, but scorer should *still* keep m2 since safety-tagged
    compactor = ContextCompactor(
        preservation_scorer=PreservationScorer(),
    )
    # Budget = 1 — only one chunk survives; safety-tagged one wins.
    cfg = CompactionConfig(max_context_entries=0, preserve_count=1)
    result = await compactor.compact(entries, cfg, op_id="op-safety")
    assert any("m2" in k for k in result.preserved_keys)


# ===========================================================================
# 5. Scorer raising → fallback to legacy
# ===========================================================================


@pytest.mark.asyncio
async def test_scorer_raising_falls_back_to_legacy(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "true",
    )
    entries = _dialogue_with_intent_rich_oldest()

    class _BoomScorer:
        def select_preserved(self, *a, **kw):
            raise RuntimeError("intentional boom")

    compactor = ContextCompactor(preservation_scorer=_BoomScorer())
    cfg = CompactionConfig(max_context_entries=5, preserve_count=3)
    # Must not raise — falls back to legacy
    result = await compactor.compact(entries, cfg, op_id="op-boom")
    assert result.entries_before == 15
    assert result.entries_after > 0


# ===========================================================================
# 6. Non-PreservationScorer attached → fall back to legacy
# ===========================================================================


@pytest.mark.asyncio
async def test_non_scorer_object_falls_back(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "true",
    )
    entries = _dialogue_with_intent_rich_oldest()
    # Attach something that isn't a PreservationScorer
    compactor = ContextCompactor(preservation_scorer="not-a-scorer")
    cfg = CompactionConfig(max_context_entries=5, preserve_count=3)
    result = await compactor.compact(entries, cfg, op_id="op-wrongtype")
    # Legacy path still produces a correct result
    assert result.entries_before == 15


# ===========================================================================
# 7. attach_preservation_scorer late-binding
# ===========================================================================


@pytest.mark.asyncio
async def test_late_bind_via_attach(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "true",
    )
    entries = _dialogue_with_intent_rich_oldest()
    compactor = ContextCompactor()  # no scorer at construction
    compactor.attach_preservation_scorer(scorer=PreservationScorer())
    cfg = CompactionConfig(max_context_entries=5, preserve_count=6)
    tracker = intent_tracker_for("op-late")
    for _ in range(5):
        tracker.ingest_turn("backend/hot.py", source=TurnSource.USER)
    result = await compactor.compact(entries, cfg, op_id="op-late")
    # Scorer was attached late → scorer path ran → manifest recorded
    recs = manifest_for("op-late").all_records()
    assert len(recs) == 1


# ===========================================================================
# 8. Legacy signature (no op_id kwarg) still works
# ===========================================================================


@pytest.mark.asyncio
async def test_legacy_call_signature_still_works():
    entries = [{"type": "x", "content": str(i)} for i in range(10)]
    compactor = ContextCompactor()
    cfg = CompactionConfig(max_context_entries=3, preserve_count=2)
    # Same shape every legacy caller uses — no op_id
    result = await compactor.compact(entries, cfg)
    assert result.entries_before == 10
    assert result.entries_after <= 10


# ===========================================================================
# 9. Scorer-path chunk_id derivation
# ===========================================================================


def test_deterministic_chunk_id_prefers_id_field():
    from backend.core.ouroboros.governance.context_compaction import (
        _deterministic_chunk_id,
    )
    assert _deterministic_chunk_id({"id": "abc"}, 0) == "chunk-abc"
    assert _deterministic_chunk_id({"message_id": "xyz"}, 0) == "chunk-xyz"


def test_deterministic_chunk_id_falls_back_to_type_and_index():
    from backend.core.ouroboros.governance.context_compaction import (
        _deterministic_chunk_id,
    )
    assert _deterministic_chunk_id({"type": "user"}, 3) == "chunk-user-3"
    assert _deterministic_chunk_id({}, 7) == "chunk-e-7"


# ===========================================================================
# 10. Repeated compaction on same op_id appends manifest records
# ===========================================================================


@pytest.mark.asyncio
async def test_repeated_compaction_appends_manifest_records(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_CONTEXT_COMPACTOR_SCORER_ENABLED", "true",
    )
    compactor = ContextCompactor(
        preservation_scorer=PreservationScorer(),
    )
    cfg = CompactionConfig(max_context_entries=1, preserve_count=1)
    for i in range(3):
        entries = [{"type": "x", "content": f"round {i}", "id": f"r{i}"}]
        await compactor.compact(entries, cfg, op_id="op-repeat")
    assert len(manifest_for("op-repeat").all_records()) == 3

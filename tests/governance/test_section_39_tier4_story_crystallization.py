"""§39 Tier-4 (PRD v2.73 to v2.74, 2026-05-09) -
session story + memory crystallization timeline regression
spine.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_tier4(monkeypatch):
    for var in (
        "JARVIS_SESSION_STORY_ENABLED",
        "JARVIS_SESSION_STORY_MAX_SESSIONS",
        "JARVIS_MEMORY_CRYSTALLIZATION_ENABLED",
        "JARVIS_MEMORY_CRYSTALLIZATION_MAX_INSIGHTS",
    ):
        monkeypatch.delenv(var, raising=False)
    yield


# ============================================ Surface #10 — session story


def test_story_master_default_false():
    from backend.core.ouroboros.governance.session_story import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_story_master_truthy(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_SESSION_STORY_ENABLED", value,
    )
    from backend.core.ouroboros.governance.session_story import (
        master_enabled,
    )
    assert master_enabled() is True


def test_story_arc_taxonomy_4_values():
    from backend.core.ouroboros.governance.session_story import (
        StoryArc,
    )
    assert {m.name for m in StoryArc} == {
        "DOMINANT_ACTIVITY", "KEY_FINDING",
        "SETBACK", "GROWTH",
    }


def test_story_arc_coerce():
    from backend.core.ouroboros.governance.session_story import (
        StoryArc,
    )
    assert StoryArc.coerce("setback") is StoryArc.SETBACK
    assert StoryArc.coerce("nonsense") is StoryArc.DOMINANT_ACTIVITY


def test_format_duration_human():
    from backend.core.ouroboros.governance.session_story import (
        _format_duration_human,
    )
    assert _format_duration_human(0) == "0s"
    assert _format_duration_human(45) == "45s"
    assert _format_duration_human(120) == "2m"
    assert _format_duration_human(3600) == "1h"
    assert _format_duration_human(3725) == "1h 2m"


def test_format_cost_human():
    from backend.core.ouroboros.governance.session_story import (
        _format_cost_human,
    )
    assert _format_cost_human(0) == "free"
    assert _format_cost_human(0.0042) == "$0.0042"
    assert _format_cost_human(0.42) == "$0.42"


def test_arc_weight_pure():
    from backend.core.ouroboros.governance.session_story import (
        _arc_weight,
    )
    assert _arc_weight(3, 5) == 0.6
    assert _arc_weight(0, 0) == 0.0
    assert _arc_weight(10, 5) == 1.0  # clamped


def test_story_artifact_to_dict():
    from backend.core.ouroboros.governance.session_story import (
        SESSION_STORY_SCHEMA_VERSION, SessionStory,
        StoryArc, StoryBeat,
    )
    s = SessionStory(
        session_id="bt-2026-05-09",
        duration_human="35m",
        cost_human="$0.23",
        stop_reason="idle_timeout",
        beats=(
            StoryBeat(
                arc=StoryArc.GROWTH,
                sentence="convergence: ok",
                weight=0.5,
            ),
        ),
    )
    d = s.to_dict()
    assert d["session_id"] == "bt-2026-05-09"
    assert d["beats"][0]["arc"] == "growth"
    assert d["schema_version"] == SESSION_STORY_SCHEMA_VERSION


def test_aggregate_master_off_empty():
    from backend.core.ouroboros.governance.session_story import (
        aggregate_session_story,
    )
    assert aggregate_session_story() == []


def test_aggregate_master_on_real_lss(monkeypatch):
    """Real LSS may or may not have records; smoke against
    canonical substrate."""
    monkeypatch.setenv(
        "JARVIS_SESSION_STORY_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_LAST_SESSION_SUMMARY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.session_story import (
        aggregate_session_story,
    )
    stories = aggregate_session_story()
    assert isinstance(stories, list)
    for s in stories:
        # Each story has at least DOMINANT_ACTIVITY beat
        assert len(s.beats) >= 1


def test_format_master_off_empty():
    from backend.core.ouroboros.governance.session_story import (
        format_session_story,
    )
    assert format_session_story(None) == ""


def test_format_with_explicit_story(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_STORY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.session_story import (
        SessionStory, StoryArc, StoryBeat,
        format_session_story,
    )
    s = SessionStory(
        session_id="bt-test-123",
        duration_human="2m",
        cost_human="free",
        stop_reason="completed",
        beats=(
            StoryBeat(
                arc=StoryArc.DOMINANT_ACTIVITY,
                sentence="You ran 1 op.",
                weight=1.0,
            ),
            StoryBeat(
                arc=StoryArc.KEY_FINDING,
                sentence="Applied 3 files.",
                weight=0.7,
            ),
        ),
    )
    out = format_session_story(s)
    assert "Session story" in out
    assert "2m" in out
    assert "📖" in out
    assert "✨" in out
    assert "1 op" in out


# ----- Story AST pins


def _story_pins():
    from backend.core.ouroboros.governance.session_story import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _story_src():
    return Path(
        "backend/core/ouroboros/governance/"
        "session_story.py"
    ).read_text()


def test_story_pins_register_4():
    assert len(_story_pins()) == 4


@pytest.mark.parametrize("idx", [0, 1, 2, 3])
def test_story_pin_passes_canonical(idx):
    pins = _story_pins()
    src = _story_src()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_story_pin_master_fires():
    pin = next(
        p for p in _story_pins()
        if "master_default_false" in p.invariant_name
    )
    bad = "def master_enabled():\n    return True\n"
    assert pin.validate(ast.parse(bad), bad)


def test_story_pin_authority_fires():
    pin = next(
        p for p in _story_pins()
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_story_pin_arc_taxonomy_fires():
    pin = next(
        p for p in _story_pins()
        if "arc_taxonomy" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class StoryArc(str, enum.Enum):\n"
        "    GROWTH = 'growth'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_story_pin_composes_lss_fires():
    pin = next(
        p for p in _story_pins()
        if "composes_canonical_last_session_summary"
        in p.invariant_name
    )
    bad = "x = 1\n"
    assert pin.validate(ast.parse(bad), bad)


def test_story_register_flags_count():
    from backend.core.ouroboros.governance.session_story import (
        register_flags,
    )

    class _Mock:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Mock()
    n = register_flags(reg)
    assert n == 2


# ============================================ Surface #18 — crystallization


def test_crystal_master_default_false():
    from backend.core.ouroboros.governance.memory_crystallization import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_crystal_master_truthy(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_MEMORY_CRYSTALLIZATION_ENABLED", value,
    )
    from backend.core.ouroboros.governance.memory_crystallization import (
        master_enabled,
    )
    assert master_enabled() is True


# ----- CrystalAge taxonomy + bucketing


def test_crystal_age_taxonomy_4_values():
    from backend.core.ouroboros.governance.memory_crystallization import (
        CrystalAge,
    )
    assert {m.name for m in CrystalAge} == {
        "NASCENT", "FORMING", "SOLID", "CRYSTALLIZED",
    }


@pytest.mark.parametrize(
    "ev,conf,expected", [
        (0, 0.0, "NASCENT"),
        (1, 0.5, "NASCENT"),
        (2, 0.5, "FORMING"),
        (4, 0.5, "FORMING"),
        (5, 0.6, "SOLID"),
        (5, 0.5, "FORMING"),    # confidence below SOLID threshold
        (10, 0.8, "CRYSTALLIZED"),
        (10, 0.7, "SOLID"),     # below CRYSTALLIZED conf
        (15, 0.9, "CRYSTALLIZED"),
    ],
)
def test_age_for_insight(ev, conf, expected):
    from backend.core.ouroboros.governance.memory_crystallization import (
        CrystalAge, _age_for_insight,
    )
    result = _age_for_insight(
        evidence_count=ev, confidence=conf,
    )
    assert result is getattr(CrystalAge, expected)


def test_age_for_insight_invalid():
    from backend.core.ouroboros.governance.memory_crystallization import (
        CrystalAge, _age_for_insight,
    )
    assert _age_for_insight(
        evidence_count="bad", confidence=0.5,
    ) is CrystalAge.NASCENT


def test_canonical_categories_pinned():
    """Lockstep regression — _CANONICAL_CATEGORIES must
    match consciousness/types.py:MemoryInsight.category
    docstring."""
    from backend.core.ouroboros.governance.memory_crystallization import (
        _CANONICAL_CATEGORIES,
    )
    assert _CANONICAL_CATEGORIES == (
        "failure_pattern",
        "success_pattern",
        "file_fragility",
        "timing_pattern",
    )


# ----- Frozen artifacts


def test_crystal_to_dict():
    from backend.core.ouroboros.governance.memory_crystallization import (
        Crystal, CrystalAge,
        MEMORY_CRYSTALLIZATION_SCHEMA_VERSION,
    )
    c = Crystal(
        insight_id="ins-1",
        category="failure_pattern",
        age=CrystalAge.SOLID,
        content="test",
        confidence=0.7,
        evidence_count=5,
        last_seen_iso="2026-05-09T10:00:00",
        last_seen_unix=1000.0,
    )
    d = c.to_dict()
    assert d["age"] == "solid"
    assert d["category"] == "failure_pattern"
    assert d["schema_version"] == MEMORY_CRYSTALLIZATION_SCHEMA_VERSION


def test_crystal_layer_to_dict():
    from backend.core.ouroboros.governance.memory_crystallization import (
        Crystal, CrystalAge, CrystalLayer,
    )
    cr = Crystal(
        insight_id="x",
        category="success_pattern",
        age=CrystalAge.NASCENT,
        content="t",
        confidence=0.1,
        evidence_count=1,
        last_seen_iso="",
        last_seen_unix=0.0,
    )
    layer = CrystalLayer(
        category="success_pattern",
        crystals=(cr,),
        by_age={"nascent": 1},
    )
    d = layer.to_dict()
    assert d["category"] == "success_pattern"
    assert d["total"] == 1


def test_timeline_layer_for_category():
    from backend.core.ouroboros.governance.memory_crystallization import (
        CrystalLayer, CrystalTimeline,
    )
    layer = CrystalLayer(category="failure_pattern")
    t = CrystalTimeline(layers=(layer,))
    assert t.layer_for_category("failure_pattern") is layer
    assert t.layer_for_category("nonexistent") is None


# ----- Reader against on-disk file


def test_aggregate_master_off_empty():
    from backend.core.ouroboros.governance.memory_crystallization import (
        aggregate_crystal_timeline,
    )
    t = aggregate_crystal_timeline()
    assert t.total_insights == 0
    assert t.layers == ()


def test_aggregate_with_synthetic_jsonl(monkeypatch, tmp_path):
    """Plant a synthetic insights.jsonl + verify reader."""
    monkeypatch.setenv(
        "JARVIS_MEMORY_CRYSTALLIZATION_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))
    insights_dir = (
        tmp_path / ".jarvis" / "ouroboros" / "consciousness"
    )
    insights_dir.mkdir(parents=True, exist_ok=True)
    insights_file = insights_dir / "insights.jsonl"
    rows = [
        {
            "insight_id": "ins-1",
            "category": "failure_pattern",
            "content": "edge case",
            "confidence": 0.9,
            "evidence_count": 12,
            "last_seen_utc": "2026-05-09T10:00:00",
        },
        {
            "insight_id": "ins-2",
            "category": "success_pattern",
            "content": "compose pattern",
            "confidence": 0.7,
            "evidence_count": 6,
            "last_seen_utc": "2026-05-09T11:00:00",
        },
        {
            "insight_id": "ins-3",
            "category": "file_fragility",
            "content": "orchestrator.py risky",
            "confidence": 0.4,
            "evidence_count": 3,
            "last_seen_utc": "2026-05-09T09:00:00",
        },
    ]
    with insights_file.open("w") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")

    from backend.core.ouroboros.governance.memory_crystallization import (
        CrystalAge, aggregate_crystal_timeline,
    )
    t = aggregate_crystal_timeline()
    assert t.total_insights == 3
    # Per-category layers present
    assert len(t.layers) == 3
    # CRYSTALLIZED count = 1 (ins-1 has ev=12 conf=0.9)
    assert t.by_age.get("crystallized", 0) == 1
    # SOLID = 1 (ins-2 ev=6 conf=0.7)
    assert t.by_age.get("solid", 0) == 1
    # FORMING = 1 (ins-3 ev=3 conf=0.4)
    assert t.by_age.get("forming", 0) == 1


def test_aggregate_handles_malformed_jsonl(monkeypatch, tmp_path):
    """Bad lines should be skipped (NEVER raises)."""
    monkeypatch.setenv(
        "JARVIS_MEMORY_CRYSTALLIZATION_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))
    insights_dir = (
        tmp_path / ".jarvis" / "ouroboros" / "consciousness"
    )
    insights_dir.mkdir(parents=True, exist_ok=True)
    f = insights_dir / "insights.jsonl"
    f.write_text(
        '{"insight_id":"good","category":"failure_pattern",'
        '"content":"x","confidence":0.5,'
        '"evidence_count":2,"last_seen_utc":"2026-05-09T10:00:00"}\n'
        'NOT_JSON\n'
        '{"missing_category":"yep"}\n'
        '\n'
    )
    from backend.core.ouroboros.governance.memory_crystallization import (
        aggregate_crystal_timeline,
    )
    t = aggregate_crystal_timeline()
    # Only the one valid row (with valid category) is parsed
    assert t.total_insights == 1


def test_aggregate_missing_file_empty(monkeypatch, tmp_path):
    monkeypatch.setenv(
        "JARVIS_MEMORY_CRYSTALLIZATION_ENABLED", "true",
    )
    monkeypatch.setenv("JARVIS_REPO_PATH", str(tmp_path))
    # NO insights.jsonl created
    from backend.core.ouroboros.governance.memory_crystallization import (
        aggregate_crystal_timeline,
    )
    t = aggregate_crystal_timeline()
    assert t.total_insights == 0


# ----- Renderer


def test_crystal_format_master_off():
    from backend.core.ouroboros.governance.memory_crystallization import (
        format_crystal_timeline,
    )
    assert format_crystal_timeline() == ""


def test_crystal_format_synthetic(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MEMORY_CRYSTALLIZATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.memory_crystallization import (
        Crystal, CrystalAge, CrystalLayer, CrystalTimeline,
        format_crystal_timeline,
    )
    t = CrystalTimeline(
        total_insights=2,
        layers=(
            CrystalLayer(
                category="failure_pattern",
                crystals=(
                    Crystal(
                        insight_id="x",
                        category="failure_pattern",
                        age=CrystalAge.CRYSTALLIZED,
                        content="big finding",
                        confidence=0.9,
                        evidence_count=15,
                        last_seen_iso="",
                        last_seen_unix=0.0,
                    ),
                ),
                by_age={"crystallized": 1},
            ),
        ),
        by_age={"crystallized": 1},
    )
    out = format_crystal_timeline(timeline=t)
    assert "Memory crystallization" in out
    assert "failure_pattern" in out
    assert "█" in out  # CRYSTALLIZED glyph
    assert "big finding" in out


# ----- Crystal AST pins


def _crystal_pins():
    from backend.core.ouroboros.governance.memory_crystallization import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _crystal_src():
    return Path(
        "backend/core/ouroboros/governance/"
        "memory_crystallization.py"
    ).read_text()


def test_crystal_pins_register_5():
    assert len(_crystal_pins()) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_crystal_pin_passes_canonical(idx):
    pins = _crystal_pins()
    src = _crystal_src()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_crystal_pin_master_fires():
    pin = next(
        p for p in _crystal_pins()
        if "master_default_false" in p.invariant_name
    )
    bad = "def master_enabled():\n    return True\n"
    assert pin.validate(ast.parse(bad), bad)


def test_crystal_pin_authority_fires():
    pin = next(
        p for p in _crystal_pins()
        if "authority_asymmetry" in p.invariant_name
    )
    # Importing MemoryEngine is forbidden — substrate must
    # parse on-disk insights.jsonl directly.
    bad = (
        "from backend.core.ouroboros.consciousness.memory_engine "
        "import MemoryEngine\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_crystal_pin_age_taxonomy_fires():
    pin = next(
        p for p in _crystal_pins()
        if "age_taxonomy" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class CrystalAge(str, enum.Enum):\n"
        "    NASCENT = 'nascent'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_crystal_pin_categories_pinned_fires():
    pin = next(
        p for p in _crystal_pins()
        if "canonical_categories_pinned" in p.invariant_name
    )
    bad = "x = 1\n"
    assert pin.validate(ast.parse(bad), bad)


def test_crystal_pin_insights_path_fires():
    pin = next(
        p for p in _crystal_pins()
        if "composes_canonical_insights_path"
        in p.invariant_name
    )
    bad = "x = 1\n"
    assert pin.validate(ast.parse(bad), bad)


def test_crystal_register_flags_count():
    from backend.core.ouroboros.governance.memory_crystallization import (
        register_flags,
    )

    class _Mock:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _Mock()
    n = register_flags(reg)
    assert n == 2


# ============================================ /story REPL


def test_repl_unmatched():
    from backend.core.ouroboros.governance.story_repl import (
        dispatch_story_command,
    )
    r = dispatch_story_command("/something")
    assert r.matched is False


def test_repl_help():
    from backend.core.ouroboros.governance.story_repl import (
        dispatch_story_command,
    )
    r = dispatch_story_command("/story help")
    assert r.ok is True
    assert "session" in r.text.lower()
    assert "crystals" in r.text.lower()


def test_repl_status():
    from backend.core.ouroboros.governance.story_repl import (
        dispatch_story_command,
    )
    r = dispatch_story_command("/story status")
    assert r.ok is True


def test_repl_session_master_off():
    from backend.core.ouroboros.governance.story_repl import (
        dispatch_story_command,
    )
    r = dispatch_story_command("/story session")
    assert r.ok is False
    assert "disabled" in r.text.lower()


def test_repl_session_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_SESSION_STORY_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.story_repl import (
        dispatch_story_command,
    )
    r = dispatch_story_command("/story session")
    assert r.ok is True


def test_repl_crystals_master_off():
    from backend.core.ouroboros.governance.story_repl import (
        dispatch_story_command,
    )
    r = dispatch_story_command("/story crystals")
    assert r.ok is False


def test_repl_crystals_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_MEMORY_CRYSTALLIZATION_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.story_repl import (
        dispatch_story_command,
    )
    r = dispatch_story_command("/story crystals 3")
    assert r.ok is True


def test_repl_unknown():
    from backend.core.ouroboros.governance.story_repl import (
        dispatch_story_command,
    )
    r = dispatch_story_command("/story bogus")
    assert r.ok is False


# ----- Canonical-source smokes


def test_canonical_event_session_story_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_SESSION_STORY_RENDERED, _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_SESSION_STORY_RENDERED == "session_story_rendered"
    assert EVENT_TYPE_SESSION_STORY_RENDERED in _VALID_EVENT_TYPES


def test_canonical_event_crystallization_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_MEMORY_CRYSTALLIZATION_AGGREGATED,
        _VALID_EVENT_TYPES,
    )
    assert (
        EVENT_TYPE_MEMORY_CRYSTALLIZATION_AGGREGATED
        == "memory_crystallization_aggregated"
    )
    assert (
        EVENT_TYPE_MEMORY_CRYSTALLIZATION_AGGREGATED
        in _VALID_EVENT_TYPES
    )


def test_canonical_lss_get_default_summary_callable():
    from backend.core.ouroboros.governance.last_session_summary import (
        get_default_summary,
    )
    assert callable(get_default_summary)

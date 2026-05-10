"""§39 Tier-7 (PRD v2.75 to v2.76, 2026-05-09) -
phase orchestra audio cue regression spine.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    for var in (
        "JARVIS_PHASE_ORCHESTRA_ENABLED",
        "JARVIS_PHASE_ORCHESTRA_BELL_ENABLED",
        "JARVIS_PHASE_ORCHESTRA_RING_SIZE",
    ):
        monkeypatch.delenv(var, raising=False)
    from backend.core.ouroboros.governance import (
        phase_orchestra as p,
    )
    p.reset_ledger_for_tests()
    yield
    p.reset_ledger_for_tests()


def test_master_default_false():
    from backend.core.ouroboros.governance.phase_orchestra import (
        master_enabled,
    )
    assert master_enabled() is False


@pytest.mark.parametrize(
    "value", ["1", "true", "yes", "on", "TRUE"],
)
def test_master_truthy(monkeypatch, value):
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", value,
    )
    from backend.core.ouroboros.governance.phase_orchestra import (
        master_enabled,
    )
    assert master_enabled() is True


def test_bell_master_off():
    """bell sub-flag forced off when master off."""
    import os as _os
    _os.environ["JARVIS_PHASE_ORCHESTRA_BELL_ENABLED"] = "true"
    try:
        from backend.core.ouroboros.governance.phase_orchestra import (
            bell_on_cue_enabled,
        )
        assert bell_on_cue_enabled() is False
    finally:
        _os.environ.pop("JARVIS_PHASE_ORCHESTRA_BELL_ENABLED", None)


def test_note_taxonomy_8():
    from backend.core.ouroboros.governance.phase_orchestra import (
        OrchestraNote,
    )
    assert {m.name for m in OrchestraNote} == {
        "DO", "RE", "MI", "FA",
        "SOL", "LA", "TI", "DO2",
    }


def test_intensity_taxonomy_4():
    from backend.core.ouroboros.governance.phase_orchestra import (
        CueIntensity,
    )
    assert {m.name for m in CueIntensity} == {
        "WHISPER", "SOFT", "NORMAL", "FORTE",
    }


@pytest.mark.parametrize(
    "idx,expected_name", [
        (-1, "DO"),
        (0, "DO"),
        (7, "DO2"),
        (8, "DO"),     # wraps via mod 8
        (15, "DO2"),
    ],
)
def test_note_for_index(idx, expected_name):
    from backend.core.ouroboros.governance.phase_orchestra import (
        OrchestraNote, _note_for_index,
    )
    assert _note_for_index(idx) is getattr(
        OrchestraNote, expected_name,
    )


def test_note_for_invalid_index():
    from backend.core.ouroboros.governance.phase_orchestra import (
        OrchestraNote, _note_for_index,
    )
    assert _note_for_index("nan") is OrchestraNote.DO


@pytest.mark.parametrize(
    "idx,expected_name", [
        (-1, "WHISPER"),
        (0, "WHISPER"),
        (1, "WHISPER"),
        (2, "SOFT"),
        (3, "SOFT"),
        (4, "NORMAL"),
        (6, "NORMAL"),
        (7, "FORTE"),
        (10, "FORTE"),
    ],
)
def test_intensity_for_index(idx, expected_name):
    from backend.core.ouroboros.governance.phase_orchestra import (
        CueIntensity, _intensity_for_index,
    )
    assert _intensity_for_index(idx) is getattr(
        CueIntensity, expected_name,
    )


def test_intensity_invalid():
    from backend.core.ouroboros.governance.phase_orchestra import (
        CueIntensity, _intensity_for_index,
    )
    assert _intensity_for_index("bad") is CueIntensity.WHISPER


def test_orchestra_cue_to_dict():
    from backend.core.ouroboros.governance.phase_orchestra import (
        CueIntensity, OrchestraCue, OrchestraNote,
        PHASE_ORCHESTRA_SCHEMA_VERSION,
    )
    c = OrchestraCue(
        phase_name="GENERATE",
        phase_index=4,
        note=OrchestraNote.SOL,
        intensity=CueIntensity.NORMAL,
        op_id="op-1",
    )
    d = c.to_dict()
    assert d["phase_name"] == "GENERATE"
    assert d["note"] == "sol"
    assert d["intensity"] == "normal"
    assert d["schema_version"] == PHASE_ORCHESTRA_SCHEMA_VERSION


def test_emit_master_off_returns_none():
    from backend.core.ouroboros.governance.phase_orchestra import (
        emit_cue,
    )
    assert emit_cue(phase="GENERATE") is None


def test_emit_master_on_real_pipeline(monkeypatch):
    """Compose canonical pipeline_progress."""
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_PIPELINE_PROGRESS_BAR_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_orchestra import (
        CueIntensity, OrchestraNote, emit_cue,
    )
    cue = emit_cue(phase="GENERATE", op_id="op-1")
    assert cue is not None
    assert cue.phase_name == "GENERATE"
    assert cue.phase_index == 4  # canonical position
    assert cue.note is OrchestraNote.SOL  # idx 4 → sol
    assert cue.intensity is CueIntensity.NORMAL


def test_emit_unknown_phase_returns_none(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_orchestra import (
        emit_cue,
    )
    # POSTMORTEM is in CANONICAL_PHASE_ORDER but NOT in
    # forward_flow_phases (forward-flow is a strict subset).
    assert emit_cue(phase="POSTMORTEM") is None
    assert emit_cue(phase="not_a_phase") is None


def test_emit_records_to_ledger(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_orchestra import (
        emit_cue, get_default_ledger,
    )
    emit_cue(phase="CLASSIFY")
    emit_cue(phase="ROUTE")
    cues = get_default_ledger().recent(limit=10)
    assert len(cues) == 2
    assert cues[0].phase_name == "CLASSIFY"
    assert cues[1].phase_name == "ROUTE"


def test_ledger_ring_bounded(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", "true",
    )
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_RING_SIZE", "8",
    )
    from backend.core.ouroboros.governance.phase_orchestra import (
        emit_cue, get_default_ledger,
        reset_ledger_for_tests,
    )
    reset_ledger_for_tests()
    for _ in range(15):
        emit_cue(phase="GENERATE")
    cues = get_default_ledger().recent(limit=64)
    assert len(cues) == 8


def test_format_recent_master_off():
    from backend.core.ouroboros.governance.phase_orchestra import (
        format_orchestra_recent,
    )
    assert format_orchestra_recent() == ""


def test_format_recent_empty(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_orchestra import (
        format_orchestra_recent,
    )
    out = format_orchestra_recent()
    assert "Phase orchestra" in out
    assert "no recent cues" in out


def test_format_recent_with_cues(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_orchestra import (
        emit_cue, format_orchestra_recent,
    )
    emit_cue(phase="GENERATE")
    out = format_orchestra_recent()
    assert "GENERATE" in out
    assert "♫" in out  # NORMAL glyph for idx 4


def test_format_status(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.phase_orchestra import (
        emit_cue, format_orchestra_status,
    )
    emit_cue(phase="CLASSIFY")
    emit_cue(phase="GENERATE")
    out = format_orchestra_status()
    assert "by intensity" in out
    assert "by note" in out


# ============================================ AST pins


def _pins():
    from backend.core.ouroboros.governance.phase_orchestra import (
        register_shipped_invariants,
    )
    return register_shipped_invariants()


def _src():
    return Path(
        "backend/core/ouroboros/governance/"
        "phase_orchestra.py"
    ).read_text()


def test_pins_register_5():
    assert len(_pins()) == 5


@pytest.mark.parametrize("idx", [0, 1, 2, 3, 4])
def test_pin_passes_canonical(idx):
    pins = _pins()
    src = _src()
    tree = ast.parse(src)
    violations = pins[idx].validate(tree, src)
    assert not violations, (
        f"{pins[idx].invariant_name} fired: {violations}"
    )


def test_pin_master_fires():
    pin = next(
        p for p in _pins()
        if "master_default_false" in p.invariant_name
    )
    bad = "def master_enabled():\n    return True\n"
    assert pin.validate(ast.parse(bad), bad)


def test_pin_note_taxonomy_fires():
    pin = next(
        p for p in _pins()
        if "note_taxonomy" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class OrchestraNote(str, enum.Enum):\n"
        "    DO = 'do'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_intensity_taxonomy_fires():
    pin = next(
        p for p in _pins()
        if "intensity_taxonomy" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class CueIntensity(str, enum.Enum):\n"
        "    WHISPER = 'whisper'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_composes_pipeline_fires():
    pin = next(
        p for p in _pins()
        if "composes_pipeline_progress" in p.invariant_name
    )
    bad = "x = 1\n"
    assert pin.validate(ast.parse(bad), bad)


def test_pin_authority_fires():
    pin = next(
        p for p in _pins()
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.orchestrator "
        "import OrchestratorEngine\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_register_flags_count():
    from backend.core.ouroboros.governance.phase_orchestra import (
        register_flags,
    )

    class _M:
        def __init__(self):
            self.calls = []

        def register(self, **kwargs):
            self.calls.append(kwargs)

    reg = _M()
    assert register_flags(reg) == 3


# ============================================ /orchestra REPL


def test_repl_unmatched():
    from backend.core.ouroboros.governance.orchestra_repl import (
        dispatch_orchestra_command,
    )
    r = dispatch_orchestra_command("/something")
    assert r.matched is False


def test_repl_help():
    from backend.core.ouroboros.governance.orchestra_repl import (
        dispatch_orchestra_command,
    )
    r = dispatch_orchestra_command("/orchestra help")
    assert r.ok is True
    assert "phase orchestra" in r.text.lower()


def test_repl_master_off():
    from backend.core.ouroboros.governance.orchestra_repl import (
        dispatch_orchestra_command,
    )
    r = dispatch_orchestra_command("/orchestra recent")
    assert r.ok is False
    assert "disabled" in r.text.lower()


def test_repl_recent_master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.orchestra_repl import (
        dispatch_orchestra_command,
    )
    r = dispatch_orchestra_command("/orchestra recent 5")
    assert r.ok is True


def test_repl_status(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.orchestra_repl import (
        dispatch_orchestra_command,
    )
    r = dispatch_orchestra_command("/orchestra status")
    assert r.ok is True
    assert "master_enabled" in r.text


def test_repl_cue_known(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.orchestra_repl import (
        dispatch_orchestra_command,
    )
    r = dispatch_orchestra_command(
        "/orchestra cue PLAN",
    )
    assert r.ok is True
    assert "PLAN" in r.text


def test_repl_cue_unknown(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.orchestra_repl import (
        dispatch_orchestra_command,
    )
    r = dispatch_orchestra_command(
        "/orchestra cue not_a_phase",
    )
    assert r.ok is False


def test_repl_cue_no_arg(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.orchestra_repl import (
        dispatch_orchestra_command,
    )
    r = dispatch_orchestra_command("/orchestra cue")
    assert r.ok is False


def test_repl_unknown_subcommand(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_PHASE_ORCHESTRA_ENABLED", "true",
    )
    from backend.core.ouroboros.governance.orchestra_repl import (
        dispatch_orchestra_command,
    )
    r = dispatch_orchestra_command("/orchestra bogus")
    assert r.ok is False


# ============================================ Canonical smokes


def test_canonical_event_orchestra_registered():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        EVENT_TYPE_PHASE_ORCHESTRA_CUE, _VALID_EVENT_TYPES,
    )
    assert EVENT_TYPE_PHASE_ORCHESTRA_CUE == "phase_orchestra_cue"
    assert EVENT_TYPE_PHASE_ORCHESTRA_CUE in _VALID_EVENT_TYPES


def test_canonical_pipeline_forward_flow_11_phases():
    """Lockstep — orchestra depends on canonical 11-phase
    forward-flow."""
    from backend.core.ouroboros.governance.pipeline_progress import (
        forward_flow_length,
    )
    assert forward_flow_length() == 11

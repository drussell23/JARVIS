"""Slice 5 graduation pins — Context Preservation arc."""
from __future__ import annotations

import re
from pathlib import Path
from typing import List

import pytest


# ===========================================================================
# 1. Graduated defaults
# ===========================================================================


def test_observability_default_is_true_post_slice_5(monkeypatch):
    monkeypatch.delenv(
        "JARVIS_CONTEXT_OBSERVABILITY_ENABLED", raising=False,
    )
    from backend.core.ouroboros.governance.context_manifest import (
        context_observability_enabled,
    )
    assert context_observability_enabled() is True


def test_observability_kill_switch_respected(monkeypatch):
    from backend.core.ouroboros.governance.context_manifest import (
        context_observability_enabled,
    )
    monkeypatch.setenv("JARVIS_CONTEXT_OBSERVABILITY_ENABLED", "false")
    assert context_observability_enabled() is False
    monkeypatch.setenv("JARVIS_CONTEXT_OBSERVABILITY_ENABLED", "true")
    assert context_observability_enabled() is True


# ===========================================================================
# 2. Full-revert matrix for every env knob in the arc
# ===========================================================================


_REVERT_MATRIX = [
    ("JARVIS_CONTEXT_OBSERVABILITY_ENABLED",
     "backend.core.ouroboros.governance.context_manifest",
     "context_observability_enabled"),
]


@pytest.mark.parametrize(
    "env,module,predicate", _REVERT_MATRIX,
    ids=[m[0] for m in _REVERT_MATRIX],
)
def test_env_flag_revert_matrix(env: str, module: str, predicate: str, monkeypatch):
    import importlib
    mod = importlib.import_module(module)
    monkeypatch.setenv(env, "true")
    assert getattr(mod, predicate)() is True
    monkeypatch.setenv(env, "false")
    assert getattr(mod, predicate)() is False
    monkeypatch.setenv(env, "nonsense")
    assert getattr(mod, predicate)() is False


# ===========================================================================
# 3. Authority invariants across the arc's 4 modules
# ===========================================================================


_ARC_MODULES = [
    "backend/core/ouroboros/governance/context_ledger.py",
    "backend/core/ouroboros/governance/context_intent.py",
    "backend/core/ouroboros/governance/context_pins.py",
    "backend/core/ouroboros/governance/context_manifest.py",
]

_FORBIDDEN = (
    "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
    "semantic_guardian", "tool_executor", "candidate_generator",
    "change_engine",
)


@pytest.mark.parametrize("rel_path", _ARC_MODULES)
def test_arc_module_has_no_authority_imports(rel_path: str):
    src = Path(rel_path).read_text()
    violations: List[str] = []
    for mod in _FORBIDDEN:
        pattern = re.compile(
            rf"^\s*(from|import)\s+[^#\n]*{re.escape(mod)}",
            re.MULTILINE,
        )
        if pattern.search(src):
            violations.append(mod)
    assert violations == [], (
        f"{rel_path} imports forbidden authority modules: {violations}"
    )


# ===========================================================================
# 4. Docstring bit-rot guards
# ===========================================================================


def test_observability_switch_docstring_pins_graduation():
    from backend.core.ouroboros.governance.context_manifest import (
        context_observability_enabled,
    )
    doc = context_observability_enabled.__doc__ or ""
    assert "graduated" in doc.lower()
    assert "``true``" in doc


# ===========================================================================
# 5. Schema version constants stable
# ===========================================================================


def test_schema_versions_pinned():
    from backend.core.ouroboros.governance.context_ledger import (
        CONTEXT_LEDGER_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.governance.context_intent import (
        INTENT_TRACKER_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.governance.context_pins import (
        PIN_REGISTRY_SCHEMA_VERSION,
    )
    from backend.core.ouroboros.governance.context_manifest import (
        CONTEXT_MANIFEST_SCHEMA_VERSION,
        CONTEXT_OBSERVABILITY_SCHEMA_VERSION,
    )
    assert CONTEXT_LEDGER_SCHEMA_VERSION == "context_ledger.v1"
    assert INTENT_TRACKER_SCHEMA_VERSION == "context_intent.v1"
    assert PIN_REGISTRY_SCHEMA_VERSION == "context_pins.v1"
    assert CONTEXT_MANIFEST_SCHEMA_VERSION == "context_manifest.v1"
    assert CONTEXT_OBSERVABILITY_SCHEMA_VERSION == "1.0"


# ===========================================================================
# 6. Event type allowlist — all 5 context event types pinned
# ===========================================================================


def test_broker_allowlist_contains_all_5_context_event_types():
    from backend.core.ouroboros.governance.ide_observability_stream import (
        _VALID_EVENT_TYPES,
    )
    required = {
        "ledger_entry_added",
        "context_compacted",
        "context_pinned",
        "context_unpinned",
        "context_pin_expired",
    }
    missing = required - set(_VALID_EVENT_TYPES)
    assert not missing, f"broker allowlist missing: {missing}"


# ===========================================================================
# 7. Post-graduation fail-closed invariants still hold
# ===========================================================================


def test_model_source_still_rejected_by_pins():
    """§1: model cannot self-pin."""
    from backend.core.ouroboros.governance.context_pins import (
        ContextPinRegistry, PinError, PinSource,
    )
    reg = ContextPinRegistry("op-1")
    # Build a fake "model" enum member by sneaking an invalid one via str.
    # The writer does a membership check on _AUTHORITATIVE_SOURCES.
    class FakeModelSource(str):
        pass
    fake = FakeModelSource("model")
    with pytest.raises(PinError):
        reg.pin(chunk_id="c", source=fake)  # type: ignore[arg-type]


def test_assistant_turns_still_do_not_shift_intent():
    from backend.core.ouroboros.governance.context_intent import (
        IntentTracker, TurnSource,
    )
    tracker = IntentTracker("op-1")
    tracker.ingest_turn("backend/secrets.py", source=TurnSource.ASSISTANT)
    assert "backend/secrets.py" not in tracker.current_intent().recent_paths

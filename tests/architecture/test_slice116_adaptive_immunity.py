"""Slice 116 — Adaptive Immune Synthesis Engine (SAFE / bounded).

The marquee is the CLOSED-LOOP proof: a bypass escapes → an antibody is
synthesized + validated zero-FP → the operator promotes it → the registry
hot-swaps → the SAME payload is now blocked, clean controls still pass. Plus the
load-bearing safety gate: promotion is fail-closed (refused without explicit
operator approval), and synthesis refuses any FP-prone rule.
"""

from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import antibody_synthesizer as AB
from backend.core.ouroboros.governance.antibody_synthesizer import (
    AntibodyRegistry,
    adaptive_immunity_enabled,
    antibody_matches,
    extract_signature,
    on_escape,
    promote_antibody,
    propose_antibody,
    synthesize_antibody,
)

# A bypass that exploits an introspection attribute (the known-gap escape class
# the static cage can miss): walking __mro__ off a type.
_ESCAPE = "leaked = type(obj).__mro__[1].__name__\n"
# Well-formed PhaseRunner-ish controls — must NEVER be blocked.
_CLEAN = [
    "def run(self, ctx):\n    return self.compute(ctx.value) + 1\n",
    "result = helper.transform(data, depth=2)\n",
    "x = [i for i in range(10) if i % 2 == 0]\n",
]


def test_master_default_false(monkeypatch):
    monkeypatch.delenv("JARVIS_ADAPTIVE_IMMUNITY_ENABLED", raising=False)
    assert adaptive_immunity_enabled() is False
    monkeypatch.setenv("JARVIS_ADAPTIVE_IMMUNITY_ENABLED", "1")
    assert adaptive_immunity_enabled() is True


class TestSynthesis:
    def test_extract_signature_finds_introspection(self):
        sig = extract_signature(_ESCAPE)
        assert "__mro__" in sig["attrs"]

    def test_synthesizes_validated_antibody(self):
        ab = synthesize_antibody(_ESCAPE, _CLEAN)
        assert ab is not None
        assert "__mro__" in ab.attr_block
        assert ab.validation["clean_fp_count"] == 0
        assert ab.validation["blocks_escape"] is True

    def test_refuses_fp_prone_antibody(self):
        # If the escape's introspection attr also appears in a "clean" control,
        # the synthesizer must REFUSE (a false antibody is worse than none).
        clean_with_collision = _CLEAN + ["audit = obj.__mro__\n"]
        assert synthesize_antibody(_ESCAPE, clean_with_collision) is None

    def test_no_introspection_feature_yields_no_antibody(self):
        assert synthesize_antibody("y = a + b\n", _CLEAN) is None


class TestOperatorGate:
    def test_promotion_is_fail_closed(self, tmp_path):
        ab = synthesize_antibody(_ESCAPE, _CLEAN)
        active = tmp_path / "active.jsonl"
        # No operator approval → refused, nothing armed.
        assert promote_antibody(ab, operator_approved=False, path=active) is False
        assert not active.exists()
        # Explicit operator approval → armed.
        assert promote_antibody(ab, operator_approved=True, path=active) is True
        assert active.exists()

    def test_on_escape_only_proposes_never_arms(self, tmp_path, monkeypatch):
        monkeypatch.setenv("JARVIS_ADAPTIVE_IMMUNITY_ENABLED", "1")
        proposals = tmp_path / "proposals.jsonl"
        active = tmp_path / "active.jsonl"
        monkeypatch.setenv("JARVIS_ANTIBODY_ACTIVE_PATH", str(active))
        ab = on_escape(_ESCAPE, _CLEAN, proposals=proposals)
        assert ab is not None
        assert proposals.exists()          # proposed (shadow)
        assert not active.exists()         # NEVER auto-armed


class TestClosedLoopSiege:
    def test_full_adaptive_loop(self, tmp_path):
        active = tmp_path / "active.jsonl"
        reg = AntibodyRegistry(active)

        # 1. The bypass escapes — the (empty) antibody layer does NOT block it.
        assert reg.screen(_ESCAPE) == []

        # 2. Synthesize + validate an antibody from the escape's AST vector.
        ab = synthesize_antibody(_ESCAPE, _CLEAN)
        assert ab is not None
        propose_antibody(ab, path=tmp_path / "proposals.jsonl")  # shadow record

        # 3. OPERATOR promotes it (the gated act) → 4. registry HOT-SWAPS.
        assert promote_antibody(ab, operator_approved=True, path=active) is True
        loaded = reg.reload()
        assert loaded == 1

        # 5. Inject the EXACT same payload → now BLOCKED (mathematical certainty).
        hits = reg.screen(_ESCAPE)
        assert hits == [ab.antibody_id], "the antibody must now block the re-injected escape"

        # 6. Clean controls STILL pass — zero-FP preserved (monotonic tighten).
        for c in _CLEAN:
            assert reg.screen(c) == [], f"antibody false-positived on a clean control: {c!r}"

    def test_antibody_matches_is_deterministic(self):
        ab = synthesize_antibody(_ESCAPE, _CLEAN)
        assert antibody_matches(_ESCAPE, ab) is True
        assert antibody_matches("z = 1 + 2\n", ab) is False
        assert antibody_matches("not valid python (((", ab) is False  # never raises

"""§37 Tier 2 — Posture Aurora regression spine.

Coverage:
  * Master flag default-false + asymmetric explicit semantics
  * 3-band ConfidenceBand frozen taxonomy
  * Threshold env knobs (high + normal + clamping)
  * 4×3 aurora table covers every posture × band cell
  * confidence_band_for handles non-float / NaN / out-of-range
  * aurora_color_for handles None / non-string / unknown postures
  * format_posture_aurora_badge happy path + fallback paths
  * register_flags + register_shipped_invariants pass
  * status_line.py composes aurora when flag on; falls back to
    canonical posture_palette badge when off
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import posture_aurora as aurora
from backend.core.ouroboros.governance.posture_aurora import (
    POSTURE_AURORA_SCHEMA_VERSION,
    ConfidenceBand,
    aurora_color_for,
    aurora_enabled,
    confidence_band_for,
    format_posture_aurora_badge,
    register_flags,
    register_shipped_invariants,
)


# ---------------------------------------------------------------------------
# 1. Master flag
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_false_when_unset(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_POSTURE_AURORA_ENABLED", raising=False,
        )
        assert aurora_enabled() is False

    def test_default_false_when_whitespace(self, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_AURORA_ENABLED", "  ")
        assert aurora_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
    def test_explicit_truthy(self, monkeypatch, value):
        monkeypatch.setenv("JARVIS_POSTURE_AURORA_ENABLED", value)
        assert aurora_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off"])
    def test_explicit_falsy(self, monkeypatch, value):
        monkeypatch.setenv("JARVIS_POSTURE_AURORA_ENABLED", value)
        assert aurora_enabled() is False


# ---------------------------------------------------------------------------
# 2. ConfidenceBand frozen taxonomy
# ---------------------------------------------------------------------------


class TestConfidenceBand:
    def test_three_members_only(self):
        members = list(ConfidenceBand)
        assert len(members) == 3
        names = {m.name for m in members}
        assert names == {"HIGH", "NORMAL", "LOW"}

    def test_string_values(self):
        assert ConfidenceBand.HIGH.value == "high"
        assert ConfidenceBand.NORMAL.value == "normal"
        assert ConfidenceBand.LOW.value == "low"


class TestConfidenceBandResolution:
    def test_high_band_at_default_threshold(self):
        assert confidence_band_for(0.75) == ConfidenceBand.HIGH
        assert confidence_band_for(0.90) == ConfidenceBand.HIGH
        assert confidence_band_for(1.0) == ConfidenceBand.HIGH

    def test_normal_band(self):
        assert confidence_band_for(0.50) == ConfidenceBand.NORMAL
        assert confidence_band_for(0.74) == ConfidenceBand.NORMAL

    def test_low_band(self):
        assert confidence_band_for(0.49) == ConfidenceBand.LOW
        assert confidence_band_for(0.0) == ConfidenceBand.LOW
        assert confidence_band_for(-1.0) == ConfidenceBand.LOW

    def test_non_float_degrades_to_low(self):
        assert confidence_band_for(None) == ConfidenceBand.LOW
        assert confidence_band_for("not a number") == ConfidenceBand.LOW
        assert confidence_band_for([0.8]) == ConfidenceBand.LOW

    def test_nan_degrades_to_low(self):
        assert confidence_band_for(float("nan")) == ConfidenceBand.LOW

    def test_high_threshold_env_override(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_POSTURE_AURORA_HIGH_THRESHOLD", "0.90",
        )
        assert confidence_band_for(0.85) == ConfidenceBand.NORMAL
        assert confidence_band_for(0.90) == ConfidenceBand.HIGH

    def test_normal_threshold_env_override(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_POSTURE_AURORA_NORMAL_THRESHOLD", "0.30",
        )
        assert confidence_band_for(0.35) == ConfidenceBand.NORMAL
        assert confidence_band_for(0.25) == ConfidenceBand.LOW

    def test_threshold_clamping(self, monkeypatch):
        # High clamps to [0, 1].
        monkeypatch.setenv(
            "JARVIS_POSTURE_AURORA_HIGH_THRESHOLD", "5.0",
        )
        assert aurora._high_threshold() == 1.0
        monkeypatch.setenv(
            "JARVIS_POSTURE_AURORA_HIGH_THRESHOLD", "-2.0",
        )
        assert aurora._high_threshold() == 0.0

    def test_normal_threshold_clamped_to_high(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_POSTURE_AURORA_HIGH_THRESHOLD", "0.60",
        )
        monkeypatch.setenv(
            "JARVIS_POSTURE_AURORA_NORMAL_THRESHOLD", "0.80",
        )
        # Normal can't exceed high — clamp to high.
        assert aurora._normal_threshold() == 0.60


# ---------------------------------------------------------------------------
# 3. Aurora table (4×3 = 12 entries, all postures × bands covered)
# ---------------------------------------------------------------------------


class TestAuroraTable:
    def test_table_has_twelve_entries(self):
        table = aurora._aurora_table()
        assert len(table) == 12

    def test_every_posture_has_three_bands(self):
        table = aurora._aurora_table()
        postures = {"EXPLORE", "CONSOLIDATE", "HARDEN", "MAINTAIN"}
        for posture in postures:
            for band in ("high", "normal", "low"):
                assert (posture, band) in table, (
                    f"missing aurora cell ({posture}, {band})"
                )

    def test_high_band_uses_bright_variant(self):
        table = aurora._aurora_table()
        assert table[("EXPLORE", "high")] == "bright_green"
        assert table[("CONSOLIDATE", "high")] == "bright_blue"
        assert table[("HARDEN", "high")] == "bright_yellow"
        assert table[("MAINTAIN", "high")] == "bright_white"

    def test_low_band_uses_dim_variant(self):
        table = aurora._aurora_table()
        assert table[("EXPLORE", "low")] == "dim green"
        assert table[("CONSOLIDATE", "low")] == "dim blue"
        assert table[("HARDEN", "low")] == "dim yellow"
        assert table[("MAINTAIN", "low")] == "bright_black"


class TestAuroraColorFor:
    def test_known_posture_high(self):
        assert aurora_color_for("EXPLORE", 0.95) == "bright_green"

    def test_known_posture_normal(self):
        assert aurora_color_for("HARDEN", 0.60) == "yellow"

    def test_known_posture_low(self):
        assert aurora_color_for("CONSOLIDATE", 0.10) == "dim blue"

    def test_none_posture_dims(self):
        assert aurora_color_for(None, 0.95) == "bright_black"

    def test_unknown_posture_dims(self):
        assert aurora_color_for("BIZARRO", 0.95) == "bright_black"

    def test_enum_with_value_attribute(self):
        class _FakePosture:
            value = "EXPLORE"

        assert aurora_color_for(_FakePosture(), 0.80) == "bright_green"


# ---------------------------------------------------------------------------
# 4. format_posture_aurora_badge
# ---------------------------------------------------------------------------


class TestFormatAuroraBadge:
    def test_master_off_returns_empty(self, monkeypatch):
        monkeypatch.delenv("JARVIS_POSTURE_AURORA_ENABLED", raising=False)
        assert format_posture_aurora_badge() == ""

    def test_no_reading_returns_empty(self, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_AURORA_ENABLED", "true")
        # No store wired — _read_current_reading_safe returns None.
        # The posture_repl module exists, but its _default_store is
        # None until the boot path wires it.
        from backend.core.ouroboros.governance import posture_repl
        original_store = getattr(posture_repl, "_default_store", None)
        try:
            posture_repl._default_store = None  # type: ignore[attr-defined]
            assert format_posture_aurora_badge() == ""
        finally:
            posture_repl._default_store = original_store  # type: ignore[attr-defined]

    def test_happy_path_with_stub_reading(self, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_AURORA_ENABLED", "true")

        class _FakePosture:
            value = "EXPLORE"

        class _FakeReading:
            posture = _FakePosture()
            confidence = 0.85

        class _FakeStore:
            def load_current(self):
                return _FakeReading()

        from backend.core.ouroboros.governance import posture_repl
        original_store = getattr(posture_repl, "_default_store", None)
        try:
            posture_repl._default_store = _FakeStore()  # type: ignore[attr-defined]
            badge = format_posture_aurora_badge(plain=False)
            assert badge == "[bright_green]🐍 EXPLORE[/bright_green]"
            badge_plain = format_posture_aurora_badge(plain=True)
            assert badge_plain == "🐍 EXPLORE"
        finally:
            posture_repl._default_store = original_store  # type: ignore[attr-defined]

    def test_empty_posture_returns_empty(self, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_AURORA_ENABLED", "true")

        class _FakePosture:
            value = "   "

        class _FakeReading:
            posture = _FakePosture()
            confidence = 0.85

        class _FakeStore:
            def load_current(self):
                return _FakeReading()

        from backend.core.ouroboros.governance import posture_repl
        original_store = getattr(posture_repl, "_default_store", None)
        try:
            posture_repl._default_store = _FakeStore()  # type: ignore[attr-defined]
            assert format_posture_aurora_badge() == ""
        finally:
            posture_repl._default_store = original_store  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# 5. register_flags + register_shipped_invariants
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_schema_version_stable(self):
        assert POSTURE_AURORA_SCHEMA_VERSION == "posture_aurora.1"

    def test_register_flags_installs_three(self):
        class _Stub:
            def __init__(self):
                self.specs = []

            def register(self, spec, *, override=False):
                self.specs.append(spec)
                return True

        registry = _Stub()
        installed = register_flags(registry)
        assert installed == 3
        names = {s.name for s in registry.specs}
        assert names == {
            "JARVIS_POSTURE_AURORA_ENABLED",
            "JARVIS_POSTURE_AURORA_HIGH_THRESHOLD",
            "JARVIS_POSTURE_AURORA_NORMAL_THRESHOLD",
        }
        master = next(
            s for s in registry.specs
            if s.name == "JARVIS_POSTURE_AURORA_ENABLED"
        )
        assert master.default is False


class TestShippedInvariants:
    def test_three_invariants_registered(self):
        invs = register_shipped_invariants()
        assert len(invs) == 3
        names = {inv.invariant_name for inv in invs}
        assert names == {
            "posture_aurora_default_false",
            "posture_aurora_taxonomy_frozen",
            "posture_aurora_no_authority_imports",
        }

    def test_all_invariants_pass_against_current_source(self):
        import ast as _ast
        from pathlib import Path
        src_path = Path(
            "backend/core/ouroboros/governance/posture_aurora.py"
        )
        source = src_path.read_text(encoding="utf-8")
        tree = _ast.parse(source)
        invs = register_shipped_invariants()
        for inv in invs:
            violations = inv.validate(tree, source)
            assert violations == (), (
                f"{inv.invariant_name} violated: {violations}"
            )


# ---------------------------------------------------------------------------
# 6. Status-line composition (aurora wins when on; canonical fallback)
# ---------------------------------------------------------------------------


class TestStatusLineComposition:
    def test_aurora_off_falls_back_to_canonical(self, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_AURORA_ENABLED", "false")
        # When canonical posture_palette is also unwired the
        # composer returns empty — we just assert no crash.
        from backend.core.ouroboros.battle_test.status_line import (
            _format_posture_badge_token,
        )
        token = _format_posture_badge_token()
        # token may be empty (unwired) or the legacy plain form;
        # the structural property is "no Rich markup brackets when
        # aurora is off."
        assert "[bright_" not in token

    def test_aurora_on_emits_rich_markup_when_reading_available(self, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_AURORA_ENABLED", "true")

        class _FakePosture:
            value = "HARDEN"

        class _FakeReading:
            posture = _FakePosture()
            confidence = 0.90

        class _FakeStore:
            def load_current(self):
                return _FakeReading()

        from backend.core.ouroboros.governance import posture_repl
        from backend.core.ouroboros.battle_test.status_line import (
            _format_posture_badge_token,
        )
        original_store = getattr(posture_repl, "_default_store", None)
        try:
            posture_repl._default_store = _FakeStore()  # type: ignore[attr-defined]
            token = _format_posture_badge_token()
            assert "bright_yellow" in token
            assert "🐍 HARDEN" in token
        finally:
            posture_repl._default_store = original_store  # type: ignore[attr-defined]

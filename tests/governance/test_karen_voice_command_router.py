"""§37 Tier 2 — Karen voice-command router regression spine.

Coverage:
  * Master flag default-false + asymmetric explicit semantics
  * 11-alias operator-verb taxonomy frozen + closed
  * is_karen_command short-circuit when master off
  * is_karen_command false on non-matching text
  * dispatch_karen_voice_command happy path for every alias
  * Spoken-response synthesis per family
  * voice_repl import failure → graceful structured result
  * register_flags + register_shipped_invariants AST pins pass
"""
from __future__ import annotations

import sys

import pytest

from backend.core.ouroboros.governance import (
    karen_voice_command_router as router,
)
from backend.core.ouroboros.governance.karen_voice_command_router import (
    KAREN_VOICE_COMMAND_ROUTER_SCHEMA_VERSION,
    KarenVoiceCommandResult,
    dispatch_karen_voice_command,
    is_karen_command,
    register_flags,
    register_shipped_invariants,
    voice_command_enabled,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def master_on(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_KAREN_VOICE_COMMAND_ENABLED", "true",
    )


@pytest.fixture
def master_off(monkeypatch):
    monkeypatch.setenv(
        "JARVIS_KAREN_VOICE_COMMAND_ENABLED", "false",
    )


# ---------------------------------------------------------------------------
# 1. Master flag
# ---------------------------------------------------------------------------


class TestMasterFlag:
    def test_default_false_when_unset(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_KAREN_VOICE_COMMAND_ENABLED", raising=False,
        )
        assert voice_command_enabled() is False

    @pytest.mark.parametrize("v", ["1", "true", "TRUE", "yes", "on"])
    def test_explicit_truthy(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_KAREN_VOICE_COMMAND_ENABLED", v,
        )
        assert voice_command_enabled() is True

    @pytest.mark.parametrize("v", ["0", "false", "no", "off"])
    def test_explicit_falsy(self, monkeypatch, v):
        monkeypatch.setenv(
            "JARVIS_KAREN_VOICE_COMMAND_ENABLED", v,
        )
        assert voice_command_enabled() is False


# ---------------------------------------------------------------------------
# 2. Phrase taxonomy frozen
# ---------------------------------------------------------------------------


class TestPhraseTaxonomy:
    def test_eleven_aliases(self):
        # 4 mute + 2 unmute + 3 density + 2 inspection = 11
        assert len(router._KAREN_VERB_TO_REPL_VERB) == 11

    def test_closed_alias_set(self):
        expected = {
            "mute", "off", "quiet", "shush",
            "unmute", "on",
            "verbose", "normal", "default",
            "status", "state",
        }
        assert set(router._KAREN_VERB_TO_REPL_VERB.keys()) == expected

    def test_repl_verb_targets_canonical_subcommands(self):
        # Every value MUST be a /voice subcommand the canonical
        # voice_repl knows about. Graduated /voice supports off /
        # on / status / verbose / etc.
        valid_repl_verbs = {
            "off", "on", "verbose", "status",
        }
        for repl_verb in router._KAREN_VERB_TO_REPL_VERB.values():
            assert repl_verb in valid_repl_verbs, (
                f"unknown /voice subcommand target: {repl_verb}"
            )


# ---------------------------------------------------------------------------
# 3. is_karen_command — closed-set match + master flag
# ---------------------------------------------------------------------------


class TestIsKarenCommand:
    @pytest.mark.parametrize("text", [
        "karen mute", "Karen Mute", "KAREN MUTE",
        "karen unmute", "karen off", "karen on",
        "karen verbose", "karen normal", "karen status",
        "karen quiet", "karen shush", "karen default",
        "karen state",
        "  karen mute  ",  # whitespace-tolerant
    ])
    def test_recognized_phrases(self, master_on, text):
        assert is_karen_command(text) is True

    @pytest.mark.parametrize("text", [
        "",
        "  ",
        "karen",  # bare keyword, no verb
        "karen mute the dog",  # extra trailing text rejected
        "karen mute,",  # punctuation not in regex
        "computer mute",  # wrong wake word
        "karen bogus",  # unknown verb
        "muted karen",  # wrong order
    ])
    def test_rejected_phrases(self, master_on, text):
        assert is_karen_command(text) is False

    def test_master_off_always_false(self, master_off):
        # Even valid phrase returns False when master off — bridge
        # falls through to conversation manager naturally.
        assert is_karen_command("karen mute") is False

    def test_non_string_silent(self, master_on):
        assert is_karen_command(None) is False  # type: ignore[arg-type]
        assert is_karen_command(123) is False  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. dispatch_karen_voice_command — composes voice_repl
# ---------------------------------------------------------------------------


class TestDispatch:
    def test_master_off_returns_unhandled(self, master_off):
        result = dispatch_karen_voice_command("karen mute")
        assert result.handled is False
        assert result.text == ""

    def test_unknown_phrase_unhandled(self, master_on):
        result = dispatch_karen_voice_command("hello karen")
        assert result.handled is False

    def test_mute_family_routes_to_off(self, master_on, monkeypatch):
        captured = []

        def _fake_dispatch(line):
            captured.append(line)

            class _R:
                ok = True
                text = "muted"
                matched = True

            return _R()

        from backend.core.ouroboros.governance import voice_repl
        monkeypatch.setattr(
            voice_repl, "dispatch_voice_command", _fake_dispatch,
        )
        for phrase in ("karen mute", "karen off", "karen quiet", "karen shush"):
            captured.clear()
            result = dispatch_karen_voice_command(phrase)
            assert result.handled is True
            assert result.repl_verb == "off"
            assert captured == ["/voice off"]
            assert "muted" in result.text.lower()

    def test_unmute_family_routes_to_on(self, master_on, monkeypatch):
        from backend.core.ouroboros.governance import voice_repl

        captured = []

        def _fake_dispatch(line):
            captured.append(line)

            class _R:
                ok = True
                text = ""
                matched = True

            return _R()

        monkeypatch.setattr(
            voice_repl, "dispatch_voice_command", _fake_dispatch,
        )
        for phrase in ("karen unmute", "karen on"):
            captured.clear()
            result = dispatch_karen_voice_command(phrase)
            assert result.handled is True
            assert result.repl_verb == "on"
            assert "unmute" in result.text.lower()

    def test_verbose_routes_correctly(self, master_on, monkeypatch):
        from backend.core.ouroboros.governance import voice_repl

        captured = []

        def _fake_dispatch(line):
            captured.append(line)

            class _R:
                ok = True
                text = ""
                matched = True

            return _R()

        monkeypatch.setattr(
            voice_repl, "dispatch_voice_command", _fake_dispatch,
        )
        result = dispatch_karen_voice_command("karen verbose")
        assert result.handled is True
        assert result.repl_verb == "verbose"
        assert "verbose" in result.text.lower()
        assert captured == ["/voice verbose"]

    def test_status_routes_correctly(self, master_on, monkeypatch):
        from backend.core.ouroboros.governance import voice_repl

        captured = []

        def _fake_dispatch(line):
            captured.append(line)

            class _R:
                ok = True
                text = ""
                matched = True

            return _R()

        monkeypatch.setattr(
            voice_repl, "dispatch_voice_command", _fake_dispatch,
        )
        result = dispatch_karen_voice_command("karen status")
        assert result.handled is True
        assert result.repl_verb == "status"
        assert captured == ["/voice status"]

    def test_voice_repl_import_failure_graceful(self, master_on, monkeypatch):
        # Simulate voice_repl module unavailable.
        original = sys.modules.get(
            "backend.core.ouroboros.governance.voice_repl",
        )
        try:
            sys.modules["backend.core.ouroboros.governance.voice_repl"] = None  # type: ignore[assignment]
            result = dispatch_karen_voice_command("karen mute")
            assert result.handled is True  # phrase matched
            assert "unavailable" in result.text.lower()
        finally:
            if original is not None:
                sys.modules["backend.core.ouroboros.governance.voice_repl"] = original

    def test_voice_repl_exception_graceful(self, master_on, monkeypatch):
        from backend.core.ouroboros.governance import voice_repl

        def _exploding_dispatch(line):
            raise RuntimeError("simulated voice_repl error")

        monkeypatch.setattr(
            voice_repl, "dispatch_voice_command", _exploding_dispatch,
        )
        result = dispatch_karen_voice_command("karen mute")
        assert result.handled is True
        assert "failed" in result.text.lower()


# ---------------------------------------------------------------------------
# 5. KarenVoiceCommandResult.to_dict shape
# ---------------------------------------------------------------------------


class TestResultShape:
    def test_to_dict_contains_schema_version(self):
        r = KarenVoiceCommandResult(
            handled=True,
            text="x",
            matched_verb="mute",
            repl_verb="off",
        )
        d = r.to_dict()
        assert d["schema_version"] == (
            "karen_voice_command_router.1"
        )
        assert d["handled"] is True
        assert d["matched_verb"] == "mute"
        assert d["repl_verb"] == "off"

    def test_schema_version_stable(self):
        assert KAREN_VOICE_COMMAND_ROUTER_SCHEMA_VERSION == (
            "karen_voice_command_router.1"
        )


# ---------------------------------------------------------------------------
# 6. register_flags + register_shipped_invariants
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_register_flags_returns_one(self):
        class _Stub:
            def __init__(self):
                self.specs = []

            def register(self, spec, *, override=False):
                self.specs.append(spec)
                return True

        registry = _Stub()
        installed = register_flags(registry)
        assert installed == 1
        assert registry.specs[0].name == (
            "JARVIS_KAREN_VOICE_COMMAND_ENABLED"
        )
        assert registry.specs[0].default is False


class TestShippedInvariants:
    def test_three_invariants_registered(self):
        invs = register_shipped_invariants()
        assert len(invs) == 3
        names = {inv.invariant_name for inv in invs}
        assert names == {
            "karen_voice_command_router_default_false",
            "karen_voice_command_phrase_taxonomy_frozen",
            "karen_voice_command_no_authority_imports",
        }

    def test_all_invariants_pass_against_current_source(self):
        import ast as _ast
        from pathlib import Path
        src_path = Path(
            "backend/core/ouroboros/governance/"
            "karen_voice_command_router.py"
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
# 7. Voice bridge integration (lazy import contract — module
#    importable without backend.voice path)
# ---------------------------------------------------------------------------


class TestVoiceBridgeIntegration:
    def test_router_importable_standalone(self):
        # The router MUST NOT require backend.voice.* at import
        # time — it's lazy-imported by the bridge instead. This
        # test ensures the import graph stays clean (the test
        # module already imported the router at top).
        assert callable(dispatch_karen_voice_command)
        assert callable(is_karen_command)

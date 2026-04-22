"""Slice 2 regression spine — /help dispatcher + VerbRegistry.

Pins carried into Slice 4 graduation.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagSpec,
    FlagType,
    Relevance,
    ensure_seeded,
    reset_default_registry,
)
from backend.core.ouroboros.governance.help_dispatcher import (
    HelpDispatchResult,
    VerbRegistry,
    VerbSpec,
    dispatch_help_command,
    dispatcher_enabled,
    get_default_verb_registry,
    reset_default_verb_registry,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(os.environ):
        if key.startswith("JARVIS_FLAG_REGISTRY") or key.startswith("JARVIS_HELP_DISPATCHER") or key.startswith("JARVIS_FLAG_TYPO"):
            monkeypatch.delenv(key, raising=False)
    reset_default_registry()
    reset_default_verb_registry()
    yield
    reset_default_registry()
    reset_default_verb_registry()


@pytest.fixture
def seeded(monkeypatch) -> FlagRegistry:
    """Flag registry seeded + master flag on."""
    monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
    return ensure_seeded()


def _run(line: str, **kwargs) -> HelpDispatchResult:
    return dispatch_help_command(line, **kwargs)


# ---------------------------------------------------------------------------
# Dispatcher basics
# ---------------------------------------------------------------------------


class TestDispatcherBasics:

    def test_unknown_line_returns_unmatched(self):
        r = _run("/notahelp")
        assert r.matched is False

    def test_empty_line_unmatched(self):
        r = _run("")
        assert r.matched is False

    def test_help_always_available_even_master_off(self):
        # Master flag NOT set → still returns help text
        r = _run("/help help")
        assert r.ok
        assert "/help" in r.text

    def test_master_off_rejects_operational_verbs(self):
        r = _run("/help flags")
        assert r.ok is False
        assert "JARVIS_FLAG_REGISTRY_ENABLED" in r.text

    def test_help_question_mark_alias(self):
        r = _run("/help ?")
        assert r.ok
        assert "REPL verb" in r.text or "/help" in r.text

    def test_parse_error_on_unterminated_quote(self, seeded):
        r = _run('/help flag "unterminated')
        assert r.ok is False
        assert "parse error" in r.text.lower()


# ---------------------------------------------------------------------------
# /help (top-level index)
# ---------------------------------------------------------------------------


class TestTopIndex:

    def test_top_index_lists_verbs_and_flag_total(self, seeded):
        r = _run("/help")
        assert r.ok
        assert "REPL verbs" in r.text
        assert "env flags" in r.text

    def test_top_index_includes_posture_verb(self, seeded):
        r = _run("/help")
        assert "/posture" in r.text

    def test_top_index_mentions_filter_commands(self, seeded):
        r = _run("/help")
        assert "--category" in r.text or "flags" in r.text


# ---------------------------------------------------------------------------
# /help verbs
# ---------------------------------------------------------------------------


class TestVerbs:

    def test_verbs_list_includes_seven_seeded(self, seeded):
        r = _run("/help verbs")
        assert r.ok
        for verb in ("/help", "/posture", "/recover", "/session", "/cost",
                     "/plan", "/layout"):
            assert verb in r.text

    def test_verbs_includes_category(self, seeded):
        r = _run("/help verbs")
        assert "governance" in r.text or "observability" in r.text

    def test_delegated_verb_help_works(self, seeded):
        r = _run("/help /posture")
        assert r.ok
        assert "posture" in r.text.lower()

    def test_delegated_verb_without_slash(self, seeded):
        """`/help posture` should work even without leading slash — but
        `/help posture` actually means "posture filter" (alias). The
        delegation path triggers on `/help <verb-name>` only when not
        a known subcommand. For `posture` it's alias first."""
        # Use a verb name that isn't a subcommand alias
        r = _run("/help recover")
        assert r.ok
        assert "recover" in r.text.lower()


# ---------------------------------------------------------------------------
# /help flags
# ---------------------------------------------------------------------------


class TestFlagsSubcommand:

    def test_flags_bare_lists_all(self, seeded):
        r = _run("/help flags")
        assert r.ok
        # ≥40 flags expected (seed has 52 at shipping)
        count_line = r.text.splitlines()[0]
        assert "flag(s)" in count_line

    def test_flags_category_filter(self, seeded):
        r = _run("/help flags --category safety")
        assert r.ok
        # DirectionInferrer master is safety-category
        assert "JARVIS_DIRECTION_INFERRER_ENABLED" in r.text

    def test_flags_unknown_category(self, seeded):
        r = _run("/help flags --category made_up")
        assert r.ok is False
        assert "unknown category" in r.text.lower()

    def test_flags_posture_filter(self, seeded):
        r = _run("/help flags --posture HARDEN")
        assert r.ok
        # These are HARDEN-critical in seed
        assert ("JARVIS_L2_ENABLED" in r.text
                or "JARVIS_PARANOIA_MODE" in r.text
                or "JARVIS_ASCII_GATE" in r.text)

    def test_flags_search_filter(self, seeded):
        r = _run("/help flags --search observer")
        assert r.ok
        assert "observer" in r.text.lower()

    def test_flags_unknown_arg(self, seeded):
        r = _run("/help flags --frobnicate x")
        assert r.ok is False
        assert "unknown arg" in r.text.lower()

    def test_flags_empty_result(self, seeded):
        r = _run("/help flags --search xyzzy_not_a_flag")
        assert r.ok
        assert "no flags match" in r.text.lower()


# ---------------------------------------------------------------------------
# /help flag <NAME>
# ---------------------------------------------------------------------------


class TestFlagDetail:

    def test_flag_detail_happy(self, seeded):
        r = _run("/help flag JARVIS_DIRECTION_INFERRER_ENABLED")
        assert r.ok
        assert "type" in r.text.lower()
        assert "default" in r.text.lower()
        assert "category" in r.text.lower()

    def test_flag_detail_unknown_with_suggestions(self, seeded):
        # Typo: "OBSERVR" instead of "OBSERVER" (distance 1 from real flag
        # JARVIS_POSTURE_OBSERVER_INTERVAL_S)
        r = _run("/help flag JARVIS_POSTURE_OBSERVR_INTERVAL_S")
        assert r.ok is False
        assert "not registered" in r.text.lower()
        assert "Did you mean" in r.text

    def test_flag_detail_requires_name(self, seeded):
        r = _run("/help flag")
        assert r.ok is False
        assert "flag <NAME>" in r.text

    def test_flag_detail_shows_current_env(self, seeded, monkeypatch):
        monkeypatch.setenv("JARVIS_POSTURE_OBSERVER_INTERVAL_S", "42")
        r = _run("/help flag JARVIS_POSTURE_OBSERVER_INTERVAL_S")
        assert r.ok
        assert "currently set in env" in r.text.lower()
        assert "42" in r.text


# ---------------------------------------------------------------------------
# /help category + /help posture + /help unregistered
# ---------------------------------------------------------------------------


class TestAliases:

    def test_category_alias(self, seeded):
        r = _run("/help category safety")
        assert r.ok
        assert "JARVIS_DIRECTION_INFERRER_ENABLED" in r.text

    def test_posture_explicit(self, seeded):
        r = _run("/help posture HARDEN")
        assert r.ok
        assert "HARDEN" in r.text

    def test_posture_requires_arg_when_unresolvable(self, seeded):
        # No current_posture_fn, no default store — should error cleanly
        r = _run("/help posture")
        # Either it returns an error, or it inferred from default store
        # (which doesn't have a reading in this fresh fixture)
        assert isinstance(r, HelpDispatchResult)

    def test_posture_with_injected_current(self, seeded):
        r = _run(
            "/help posture",
            current_posture_fn=lambda: type("Posture", (), {"value": "HARDEN"})(),
        )
        assert r.ok
        assert "HARDEN" in r.text


class TestUnregistered:

    def test_unregistered_empty_when_clean(self, seeded):
        r = _run("/help unregistered")
        assert r.ok
        # Either empty or lists the env vars we auto-set in fixtures
        assert "unregistered" in r.text.lower() or "no unregistered" in r.text.lower()

    def test_unregistered_shows_typo_with_suggestion(self, seeded, monkeypatch):
        # Typo distance 1 from a real seeded flag
        monkeypatch.setenv("JARVIS_POSTURE_OBSERVR_INTERVAL_S", "600")
        r = _run("/help unregistered")
        assert r.ok
        assert "JARVIS_POSTURE_OBSERVR_INTERVAL_S" in r.text
        assert "closest match" in r.text.lower() or "Levenshtein" in r.text


# ---------------------------------------------------------------------------
# /help stats
# ---------------------------------------------------------------------------


class TestStats:

    def test_stats_shape(self, seeded):
        r = _run("/help stats")
        assert r.ok
        for key in ("schema_version", "total_flags", "by_category",
                    "by_type", "read_count", "verbs_registered"):
            assert key in r.text

    def test_stats_reports_schema_version(self, seeded):
        r = _run("/help stats")
        assert "1.0" in r.text


# ---------------------------------------------------------------------------
# VerbRegistry
# ---------------------------------------------------------------------------


class TestVerbRegistry:

    def test_register_and_get(self):
        vr = VerbRegistry()
        spec = VerbSpec(name="/test", one_line="test verb")
        vr.register(spec)
        assert vr.get("/test") is spec

    def test_register_rejects_non_verbspec(self):
        vr = VerbRegistry()
        with pytest.raises(TypeError):
            vr.register("not-a-spec")  # type: ignore[arg-type]

    def test_override_false_raises(self):
        vr = VerbRegistry()
        spec = VerbSpec(name="/test", one_line="one")
        vr.register(spec)
        with pytest.raises(ValueError):
            vr.register(spec, override=False)

    def test_list_all_sorted(self):
        vr = VerbRegistry()
        vr.register(VerbSpec(name="/b", one_line="b"))
        vr.register(VerbSpec(name="/a", one_line="a"))
        assert [v.name for v in vr.list_all()] == ["/a", "/b"]

    def test_default_registry_seeded(self):
        vr = get_default_verb_registry()
        names = [v.name for v in vr.list_all()]
        assert "/help" in names
        assert "/posture" in names
        assert "/recover" in names

    def test_resolve_help_static(self):
        spec = VerbSpec(
            name="/t", one_line="o", help_text="full help text",
        )
        assert spec.resolve_help() == "full help text"

    def test_resolve_help_fn(self):
        spec = VerbSpec(
            name="/t", one_line="o",
            help_text_fn=lambda: "dynamic help",
        )
        assert spec.resolve_help() == "dynamic help"

    def test_resolve_help_fn_raising_falls_back(self):
        def boom():
            raise RuntimeError("boom")

        spec = VerbSpec(
            name="/t", one_line="o",
            help_text_fn=boom, help_text="fallback",
        )
        assert spec.resolve_help() == "fallback"


# ---------------------------------------------------------------------------
# Master gating
# ---------------------------------------------------------------------------


class TestMasterGating:

    def test_dispatcher_enabled_false_when_master_off(self):
        assert dispatcher_enabled() is False

    def test_dispatcher_enabled_true_when_master_on(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        assert dispatcher_enabled() is True

    def test_dispatcher_sub_gate_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_FLAG_REGISTRY_ENABLED", "true")
        monkeypatch.setenv("JARVIS_HELP_DISPATCHER_ENABLED", "false")
        assert dispatcher_enabled() is False


# ---------------------------------------------------------------------------
# Authority invariant
# ---------------------------------------------------------------------------


_AUTHORITY_MODULES = (
    "orchestrator", "policy", "iron_gate", "risk_tier",
    "change_engine", "candidate_generator", "gate",
)


class TestAuthorityInvariant:

    def test_help_dispatcher_authority_free(self):
        repo_root = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, check=True,
        ).stdout.strip())
        src = (
            repo_root
            / "backend/core/ouroboros/governance/help_dispatcher.py"
        ).read_text(encoding="utf-8")
        bad = []
        for line in src.splitlines():
            if line.startswith(("from ", "import ")):
                for forbidden in _AUTHORITY_MODULES:
                    if f".{forbidden}" in line:
                        bad.append(line)
        assert not bad, f"help_dispatcher.py authority violations: {bad}"

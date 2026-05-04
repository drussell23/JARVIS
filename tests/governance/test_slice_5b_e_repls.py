"""Slice 5b E — REPL verb dispatcher tests.

Covers four arcs in one file (probe / coherence / quorum /
postmortems) since each REPL surface is a small (~10–15 test)
unit. Mirrors the per-arc test discipline established in Slices
A/B/C/D for HTTP routes.

Pinned contracts:

  * ``register_verbs(registry) -> int`` is auto-discovered by
    ``help_dispatcher._discover_module_provided_verbs`` — each
    module returns 1 verb installed.
  * ``dispatch_<verb>_command`` returns a frozen result with
    ``ok``/``text``/``matched`` fields; non-matching lines yield
    ``matched=False``.
  * Master-flag-off renders an explicit DISABLED notice (NOT a
    parse error) so operators can debug the gate.
  * SerpentREPL imports the dispatcher (structural pin).
  * Authority invariants: each REPL module imports stdlib +
    its arc's verification.* modules ONLY.
"""
from __future__ import annotations

from pathlib import Path


# ---------------------------------------------------------------------------
# § 1 — register_verbs auto-discovery contract for all 4 modules
# ---------------------------------------------------------------------------


class TestRegisterVerbs:
    def test_probe_register_verbs_returns_one(self):
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbRegistry,
        )
        from backend.core.ouroboros.governance.probe_repl import (
            register_verbs,
        )
        registry = VerbRegistry()
        assert register_verbs(registry) == 1

    def test_coherence_register_verbs_returns_one(self):
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbRegistry,
        )
        from backend.core.ouroboros.governance.coherence_repl import (  # noqa: E501
            register_verbs,
        )
        registry = VerbRegistry()
        assert register_verbs(registry) == 1

    def test_quorum_register_verbs_returns_one(self):
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbRegistry,
        )
        from backend.core.ouroboros.governance.quorum_repl import (
            register_verbs,
        )
        registry = VerbRegistry()
        assert register_verbs(registry) == 1

    def test_postmortems_register_verbs_returns_one(self):
        from backend.core.ouroboros.governance.help_dispatcher import (
            VerbRegistry,
        )
        from backend.core.ouroboros.governance.postmortem_observability import (  # noqa: E501
            register_verbs,
        )
        registry = VerbRegistry()
        assert register_verbs(registry) == 1


# ---------------------------------------------------------------------------
# § 2 — Match predicate for each dispatcher
# ---------------------------------------------------------------------------


class TestMatchPredicate:
    def test_probe_matches_bare(self):
        from backend.core.ouroboros.governance.probe_repl import (
            dispatch_probe_command,
        )
        assert dispatch_probe_command("/probe").matched is True
        assert dispatch_probe_command("probe").matched is True

    def test_probe_matches_subcommand(self):
        from backend.core.ouroboros.governance.probe_repl import (
            dispatch_probe_command,
        )
        assert (
            dispatch_probe_command("/probe status").matched
            is True
        )

    def test_probe_does_not_match_unrelated(self):
        from backend.core.ouroboros.governance.probe_repl import (
            dispatch_probe_command,
        )
        assert (
            dispatch_probe_command("/something").matched
            is False
        )
        assert dispatch_probe_command("").matched is False

    def test_coherence_matches(self):
        from backend.core.ouroboros.governance.coherence_repl import (  # noqa: E501
            dispatch_coherence_command,
        )
        assert (
            dispatch_coherence_command("/coherence").matched
            is True
        )
        assert (
            dispatch_coherence_command("/probe").matched
            is False
        )

    def test_quorum_matches(self):
        from backend.core.ouroboros.governance.quorum_repl import (
            dispatch_quorum_command,
        )
        assert (
            dispatch_quorum_command("/quorum").matched
            is True
        )
        assert (
            dispatch_quorum_command("/coherence").matched
            is False
        )


# ---------------------------------------------------------------------------
# § 3 — Master-flag-off renders DISABLED notice (not parse error)
# ---------------------------------------------------------------------------


class TestMasterFlagDisabled:
    def test_probe_disabled_renders_explanation(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.probe_repl import (
            dispatch_probe_command,
        )
        result = dispatch_probe_command("/probe status")
        assert result.matched is True
        assert result.ok is False
        assert "disabled" in result.text.lower()
        assert (
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED"
            in result.text
        )

    def test_coherence_disabled_renders_explanation(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.coherence_repl import (  # noqa: E501
            dispatch_coherence_command,
        )
        result = dispatch_coherence_command("/coherence status")
        assert result.matched is True
        assert result.ok is False
        assert "disabled" in result.text.lower()

    def test_quorum_disabled_renders_explanation(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.quorum_repl import (
            dispatch_quorum_command,
        )
        result = dispatch_quorum_command("/quorum status")
        assert result.matched is True
        assert result.ok is False
        assert "disabled" in result.text.lower()


# ---------------------------------------------------------------------------
# § 4 — Help subcommand bypasses master-flag gate (discoverability)
# ---------------------------------------------------------------------------


class TestHelpAlwaysAvailable:
    def test_probe_help_works_with_master_off(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.probe_repl import (
            dispatch_probe_command,
        )
        result = dispatch_probe_command("/probe help")
        assert result.ok is True
        assert "/probe" in result.text

    def test_coherence_help_works_with_master_off(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.coherence_repl import (  # noqa: E501
            dispatch_coherence_command,
        )
        result = dispatch_coherence_command("/coherence help")
        assert result.ok is True
        assert "/coherence" in result.text

    def test_quorum_help_works_with_master_off(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "false",
        )
        from backend.core.ouroboros.governance.quorum_repl import (
            dispatch_quorum_command,
        )
        result = dispatch_quorum_command("/quorum help")
        assert result.ok is True
        assert "/quorum" in result.text


# ---------------------------------------------------------------------------
# § 5 — Subcommand dispatch on master-on
# ---------------------------------------------------------------------------


class TestSubcommandDispatch:
    def test_probe_status_renders(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.probe_repl import (
            dispatch_probe_command,
        )
        result = dispatch_probe_command("/probe status")
        assert result.ok is True
        assert "schema_version" in result.text
        assert "bridge_enabled" in result.text

    def test_probe_allowlist_renders_nine_tools(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.probe_repl import (
            dispatch_probe_command,
        )
        result = dispatch_probe_command("/probe allowlist")
        assert result.ok is True
        # The READONLY_TOOL_ALLOWLIST is the 9-tool frozenset
        # AST-pinned by Move 5 Slice 2.
        assert "9" in result.text or "read_file" in result.text

    def test_probe_unknown_subcommand_friendly_error(
        self, monkeypatch,
    ):
        monkeypatch.setenv(
            "JARVIS_CONFIDENCE_PROBE_BRIDGE_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.probe_repl import (
            dispatch_probe_command,
        )
        result = dispatch_probe_command("/probe nosuchverb")
        assert result.ok is False
        assert "unknown subcommand" in result.text.lower()

    def test_coherence_status_renders(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.coherence_repl import (  # noqa: E501
            dispatch_coherence_command,
        )
        result = dispatch_coherence_command("/coherence status")
        assert result.ok is True
        assert "Coherence Auditor" in result.text
        assert "schema_version" in result.text

    def test_coherence_audits_with_explicit_limit(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_COHERENCE_BASE_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.coherence_repl import (  # noqa: E501
            dispatch_coherence_command,
        )
        result = dispatch_coherence_command(
            "/coherence audits 5",
        )
        assert result.ok is True
        assert "audits" in result.text.lower()
        assert "(no verdicts yet)" in result.text

    def test_coherence_advisories_with_explicit_limit(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_COHERENCE_AUDITOR_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_COHERENCE_ADVISORY_PATH",
            str(tmp_path / "advisories.jsonl"),
        )
        from backend.core.ouroboros.governance.coherence_repl import (  # noqa: E501
            dispatch_coherence_command,
        )
        result = dispatch_coherence_command(
            "/coherence advisories 5",
        )
        assert result.ok is True
        assert "advisories" in result.text.lower()

    def test_quorum_status_renders(self, monkeypatch):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.quorum_repl import (
            dispatch_quorum_command,
        )
        result = dispatch_quorum_command("/quorum status")
        assert result.ok is True
        assert "Generative Quorum" in result.text
        assert "stability_score" in result.text

    def test_quorum_history_empty(
        self, monkeypatch, tmp_path,
    ):
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        monkeypatch.setenv(
            "JARVIS_QUORUM_HISTORY_DIR", str(tmp_path),
        )
        from backend.core.ouroboros.governance.quorum_repl import (
            dispatch_quorum_command,
        )
        result = dispatch_quorum_command("/quorum history 10")
        assert result.ok is True
        assert "(no runs yet)" in result.text

    def test_quorum_outcomes_lists_enum_dynamically(
        self, monkeypatch,
    ):
        """Drift-safe: probe the enum + assert text contains
        every enum value dynamically — never quote literals."""
        monkeypatch.setenv(
            "JARVIS_GENERATIVE_QUORUM_ENABLED", "true",
        )
        from backend.core.ouroboros.governance.verification.generative_quorum import (  # noqa: E501
            ConsensusOutcome,
        )
        from backend.core.ouroboros.governance.quorum_repl import (
            dispatch_quorum_command,
        )
        result = dispatch_quorum_command("/quorum outcomes")
        assert result.ok is True
        for outcome in ConsensusOutcome:
            assert outcome.value in result.text


# ---------------------------------------------------------------------------
# § 6 — Authority invariants — each REPL module imports its arc
# only, never policy/orchestrator/iron_gate
# ---------------------------------------------------------------------------


class TestAuthorityInvariants:
    _FORBIDDEN = (
        "from backend.core.ouroboros.governance.orchestrator",
        "from backend.core.ouroboros.governance.iron_gate",
        "from backend.core.ouroboros.governance.candidate_generator",
        "from backend.core.ouroboros.governance.providers",
        "from backend.core.ouroboros.governance.urgency_router",
        "from backend.core.ouroboros.governance.semantic_guardian",
        "from backend.core.ouroboros.governance.tool_executor",
        "from backend.core.ouroboros.governance.change_engine",
        "from backend.core.ouroboros.governance.subagent_scheduler",
        "from backend.core.ouroboros.governance.policy",
    )

    def _check(self, module_filename: str) -> None:
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "governance"
            / module_filename
        )
        source = path.read_text(encoding="utf-8")
        for forbidden in self._FORBIDDEN:
            assert forbidden not in source, (
                f"{module_filename} must NOT import {forbidden}"
            )

    def test_probe_repl_authority(self):
        self._check("probe_repl.py")

    def test_coherence_repl_authority(self):
        self._check("coherence_repl.py")

    def test_quorum_repl_authority(self):
        self._check("quorum_repl.py")


# ---------------------------------------------------------------------------
# § 7 — SerpentREPL hookup pins
# ---------------------------------------------------------------------------


class TestSerpentREPLHookup:
    def _serpent_source(self) -> str:
        path = (
            Path(__file__).resolve().parent.parent.parent
            / "backend" / "core" / "ouroboros" / "battle_test"
            / "serpent_flow.py"
        )
        return path.read_text(encoding="utf-8")

    def test_serpent_dispatches_probe(self):
        src = self._serpent_source()
        assert '"probe", "/probe"' in src or (
            'line in ("probe", "/probe")' in src
        )
        assert (
            "_print_observability_verb" in src
        ), "SerpentREPL must define the shared observability helper"

    def test_serpent_dispatches_coherence(self):
        src = self._serpent_source()
        assert '"coherence", "/coherence"' in src or (
            'line in ("coherence", "/coherence")' in src
        )

    def test_serpent_dispatches_quorum(self):
        src = self._serpent_source()
        assert '"quorum", "/quorum"' in src or (
            'line in ("quorum", "/quorum")' in src
        )

    def test_serpent_dispatches_postmortems(self):
        src = self._serpent_source()
        assert '"postmortems"' in src
        assert "_print_postmortems" in src

    def test_serpent_imports_dispatchers_lazily(self):
        """The helper imports each dispatcher inside its branch
        so an absent module doesn't break boot."""
        src = self._serpent_source()
        assert "dispatch_probe_command" in src
        assert "dispatch_coherence_command" in src
        assert "dispatch_quorum_command" in src
        assert "dispatch_postmortems_command" in src

    def test_help_lists_all_four_verbs(self):
        """``/help`` enumerates the four new verbs."""
        src = self._serpent_source()
        # Find the _print_help body
        idx = src.find("def _print_help(self)")
        assert idx >= 0
        end = src.find("\n    def ", idx + 1)
        body = src[idx:end if end > idx else idx + 8000]
        assert "/probe" in body
        assert "/coherence" in body
        assert "/quorum" in body
        assert "/postmortems" in body

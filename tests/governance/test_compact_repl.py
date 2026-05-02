"""Q1 Slice 1 — /compact REPL regression suite.

Covers:

  §1   master flag + default-on
  §2   line matching + non-matching short-circuit
  §3   help always works (master-off bypass)
  §4   unknown subcommand surfaces vocabulary
  §5   master-off response (with help-still-works carve-out)
  §6   /compact status renders config + last result
  §7   /compact run requires compactor + dialogue_entries
  §8   /compact run end-to-end via stub compactor + records last result
  §9   defensive: parse error / non-list entries
  §10  AST authority pins
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.compact_repl import (
    COMPACT_REPL_SCHEMA_VERSION,
    CompactDispatchResult,
    compact_repl_enabled,
    dispatch_compact_command,
    reset_default_compactor,
    set_default_compactor,
)
from backend.core.ouroboros.governance.context_compaction import (
    CompactionConfig,
    CompactionResult,
)


_MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "backend"
    / "core"
    / "ouroboros"
    / "governance"
    / "compact_repl.py"
)


class _StubCompactor:
    """Minimal substrate stub. Records the call + returns a
    canned CompactionResult so tests can assert on what the REPL
    surfaces."""

    def __init__(self, result: CompactionResult):
        self.result = result
        self.calls: list = []

    async def compact(self, entries, config=None, *, op_id=None):
        self.calls.append({
            "entries": list(entries),
            "config": config,
            "op_id": op_id,
        })
        return self.result


@pytest.fixture(autouse=True)
def _reset_state():
    reset_default_compactor()
    yield
    reset_default_compactor()


# ============================================================================
# §1 — Master flag
# ============================================================================


class TestMasterFlag:
    def test_default_on(self, monkeypatch):
        monkeypatch.delenv("JARVIS_COMPACT_REPL_ENABLED", raising=False)
        assert compact_repl_enabled() is True

    def test_explicit_false(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "false")
        assert compact_repl_enabled() is False

    def test_garbage_value_treated_as_false(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "maybe")
        assert compact_repl_enabled() is False


# ============================================================================
# §2 — Line matching
# ============================================================================


class TestMatching:
    def test_non_compact_line_returns_unmatched(self):
        r = dispatch_compact_command("/posture status")
        assert r.matched is False
        assert r.ok is False

    def test_empty_line_returns_unmatched(self):
        r = dispatch_compact_command("")
        assert r.matched is False

    def test_compact_bare_runs_status(self):
        r = dispatch_compact_command("/compact")
        assert r.matched is True
        assert r.ok is True
        assert "status" in r.text.lower() or "Config" in r.text


# ============================================================================
# §3 — Help bypasses master flag
# ============================================================================


class TestHelp:
    def test_help_works_when_master_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "false")
        r = dispatch_compact_command("/compact help")
        assert r.ok is True
        assert "/compact run" in r.text
        assert "JARVIS_COMPACT_REPL_ENABLED" in r.text

    def test_question_mark_alias(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "false")
        r = dispatch_compact_command("/compact ?")
        assert r.ok is True


# ============================================================================
# §4 — Unknown subcommand
# ============================================================================


class TestUnknownSubcommand:
    def test_unknown_subcommand_lists_vocabulary(self):
        r = dispatch_compact_command("/compact totally-not-real")
        assert r.ok is False
        assert "unknown subcommand" in r.text
        assert "status" in r.text
        assert "run" in r.text


# ============================================================================
# §5 — Master-off response
# ============================================================================


class TestMasterOffResponse:
    def test_status_blocked_when_master_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "false")
        r = dispatch_compact_command("/compact status")
        assert r.ok is False
        assert "REPL disabled" in r.text
        assert "JARVIS_COMPACT_REPL_ENABLED" in r.text
        # help-still-works carve-out documented
        assert "/compact help" in r.text

    def test_run_blocked_when_master_off(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "false")
        r = dispatch_compact_command("/compact run")
        assert r.ok is False
        assert "REPL disabled" in r.text


# ============================================================================
# §6 — Status rendering
# ============================================================================


class TestStatus:
    def test_status_renders_config(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "true")
        r = dispatch_compact_command("/compact status")
        assert r.ok is True
        assert "max_context_entries" in r.text
        assert "preserve_count" in r.text

    def test_status_shows_compactor_not_wired(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "true")
        r = dispatch_compact_command("/compact status")
        assert "NOT WIRED" in r.text or "wired" in r.text.lower()

    def test_status_shows_compactor_wired_when_set(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "true")
        stub = _StubCompactor(CompactionResult())
        set_default_compactor(stub)
        r = dispatch_compact_command("/compact status")
        assert "wired" in r.text.lower()
        assert "NOT WIRED" not in r.text

    def test_status_shows_no_last_result_initially(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "true")
        r = dispatch_compact_command("/compact status")
        assert "none" in r.text.lower()


# ============================================================================
# §7 — Run argument validation
# ============================================================================


class TestRunValidation:
    def test_run_without_compactor_returns_error(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "true")
        r = dispatch_compact_command("/compact run")
        assert r.ok is False
        assert "no compactor wired" in r.text

    def test_run_with_compactor_but_no_entries(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "true")
        stub = _StubCompactor(CompactionResult())
        r = dispatch_compact_command(
            "/compact run", compactor=stub,
        )
        assert r.ok is False
        assert "no dialogue_entries" in r.text

    def test_run_with_non_list_entries(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "true")
        stub = _StubCompactor(CompactionResult())
        r = dispatch_compact_command(
            "/compact run", compactor=stub,
            dialogue_entries={"not": "a list"},  # type: ignore[arg-type]
        )
        assert r.ok is False
        assert "must be a list" in r.text


# ============================================================================
# §8 — Run end-to-end
# ============================================================================


class TestRunEndToEnd:
    def test_run_dispatches_to_compactor_and_records_result(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "true")
        stub = _StubCompactor(CompactionResult(
            entries_before=10, entries_after=3,
            entries_compacted=7,
            summary="compacted 7 entries into a summary",
            preserved_keys=["a", "b", "c"],
        ))
        entries = [{"phase": f"e{i}", "data": i} for i in range(10)]
        r = dispatch_compact_command(
            "/compact run", compactor=stub,
            dialogue_entries=entries, op_id="op-test-1",
        )
        assert r.ok is True
        # Stub was called exactly once with the entries
        assert len(stub.calls) == 1
        assert len(stub.calls[0]["entries"]) == 10
        assert stub.calls[0]["op_id"] == "op-test-1"
        # Result rendered inline
        assert "entries_before:    10" in r.text
        assert "entries_after:     3" in r.text
        assert "entries_compacted: 7" in r.text
        assert "compacted 7 entries" in r.text

    def test_run_records_last_result_visible_in_status(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "true")
        stub = _StubCompactor(CompactionResult(
            entries_before=5, entries_after=2,
            entries_compacted=3, summary="x",
            preserved_keys=["k1", "k2"],
        ))
        dispatch_compact_command(
            "/compact run", compactor=stub,
            dialogue_entries=[{"i": 0}],
        )
        # Subsequent /compact status reads the last result
        r2 = dispatch_compact_command("/compact status")
        assert "entries_before:    5" in r2.text
        assert "entries_compacted: 3" in r2.text


# ============================================================================
# §9 — Defensive
# ============================================================================


class TestDefensive:
    def test_parse_error_returns_structured_result(self):
        # Unmatched quote — shlex parse error
        r = dispatch_compact_command('/compact "unterminated')
        assert r.ok is False
        assert "parse error" in r.text

    def test_compactor_async_failure_surfaces_cleanly(self, monkeypatch):
        monkeypatch.setenv("JARVIS_COMPACT_REPL_ENABLED", "true")

        class _BoomCompactor:
            async def compact(self, *args, **kwargs):
                raise RuntimeError("simulated substrate failure")

        r = dispatch_compact_command(
            "/compact run", compactor=_BoomCompactor(),
            dialogue_entries=[{"i": 0}],
        )
        assert r.ok is False
        assert "dispatch failed" in r.text
        assert "simulated substrate failure" in r.text


# ============================================================================
# §10 — AST authority pins
# ============================================================================


_FORBIDDEN_AUTH_TOKENS = (
    "orchestrator", "iron_gate", "policy_engine",
    "risk_engine", "change_engine", "tool_executor",
    "providers", "candidate_generator", "semantic_guardian",
    "semantic_firewall", "scoped_tool_backend",
    "subagent_scheduler",
)
_SUBPROC_TOKENS = (
    "subprocess" + ".",
    "os." + "system",
    "popen",
)
_FS_TOKENS = (
    "open(", ".write(", "os.remove",
    "os.unlink", "shutil.",
)
_ENV_MUTATION_TOKENS = (
    "os.environ[", "os.environ.pop", "os.environ.update",
    "os.put" + "env", "os.set" + "env",
)


class TestAuthorityInvariants:
    @pytest.fixture(scope="class")
    def source(self):
        return _MODULE_PATH.read_text(encoding="utf-8")

    def test_no_authority_imports(self, source):
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                for f in _FORBIDDEN_AUTH_TOKENS:
                    assert f not in module, (
                        f"forbidden import contains {f!r}: {module}"
                    )

    def test_governance_imports_in_allowlist(self, source):
        """Q1 Slice 1 may import ONLY:
          * context_compaction (substrate types)"""
        allowed = {
            "backend.core.ouroboros.governance.context_compaction",
            "backend.core.ouroboros.governance.lifecycle_hooks",
        }
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if "governance" in module:
                    assert module in allowed, (
                        f"governance import outside allowlist: {module}"
                    )

    def test_no_filesystem_writes(self, source):
        for tok in _FS_TOKENS:
            assert tok not in source, (
                f"forbidden FS token: {tok}"
            )

    def test_no_subprocess(self, source):
        for token in _SUBPROC_TOKENS:
            assert token not in source, (
                f"forbidden subprocess token: {token}"
            )

    def test_no_env_mutation(self, source):
        for token in _ENV_MUTATION_TOKENS:
            assert token not in source, (
                f"forbidden env mutation token: {token}"
            )

    def test_schema_version_canonical(self):
        assert COMPACT_REPL_SCHEMA_VERSION == "compact_repl.1"

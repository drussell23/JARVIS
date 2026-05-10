"""Regression spine for ``tool_permissions_repl`` — Venom V2
Slice 2 surfaces (REPL verb + ``/expand p-N`` extension).

Validates the §33.3 naming-cage auto-discovery shape +
master-flag gate behaviour + cross-substrate ``p-N`` ref family
membership in :func:`SerpentREPL._handle_expand`.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.tool_permissions_repl import (
    ToolPermissionsReplDispatchResult,
    dispatch_tool_permissions_command,
)
from backend.core.ouroboros.governance.permission_decision_archive import (
    MASTER_FLAG_ENV_VAR,
    maybe_record_decision,
    reset_default_archive_for_tests,
)
from backend.core.ouroboros.governance.tool_permission import (
    AggregatePermissionDecision,
    TOOL_PERMISSION_SCHEMA_VERSION,
    ToolPermissionDecision,
)


_REPL_SRC = Path(
    inspect.getfile(dispatch_tool_permissions_command),
).read_text(encoding="utf-8")


def _make_decision(
    *,
    tool_name: str = "read_file",
    op_id: str = "op-A",
    value: ToolPermissionDecision = ToolPermissionDecision.ALLOW,
    detail: str = "test",
) -> AggregatePermissionDecision:
    return AggregatePermissionDecision(
        schema_version=TOOL_PERMISSION_SCHEMA_VERSION,
        tool_name=tool_name,
        op_id=op_id,
        decision=value,
        total_callbacks=1,
        detail=detail,
    )


@pytest.fixture(autouse=True)
def _isolate(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Clean env + singleton between tests."""
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    reset_default_archive_for_tests()
    yield
    reset_default_archive_for_tests()


# ---------------------------------------------------------------------------
# Match contract — naming-cage auto-discovery shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected_match",
    [
        ("/tool_permissions", True),
        ("tool_permissions", True),
        ("/tool_permissions recent", True),
        ("tool_permissions help", True),
        ("/tool_permissions stats", True),
        ("/tool_permissionsfoo", False),  # subprefix doesn't match
        ("/permissions", False),  # different verb (inline)
        ("/something_else", False),
        ("", False),
        ("   ", False),
    ],
)
def test_dispatcher_match_contract(
    line: str, expected_match: bool,
):
    """The matcher MUST distinguish exact verb invocations from
    similar-but-different verbs (especially ``/permissions`` which
    belongs to the inline-permission observability surface)."""
    r = dispatch_tool_permissions_command(line)
    assert r.matched is expected_match


def test_non_matching_line_returns_empty_payload():
    r = dispatch_tool_permissions_command("/something_else")
    assert r.matched is False
    assert r.text == ""
    assert r.ok is False


# ---------------------------------------------------------------------------
# Master-flag gate — bypassed for help, enforced for everything else
# ---------------------------------------------------------------------------


def test_help_bypasses_master_flag(monkeypatch: pytest.MonkeyPatch):
    """``/tool_permissions help`` MUST work even when master is
    off — discoverability invariant."""
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    r = dispatch_tool_permissions_command("/tool_permissions help")
    assert r.matched and r.ok
    assert "subcommands" in r.text.lower()
    assert "JARVIS_PERMISSION_ARCHIVE_ENABLED" in r.text


def test_recent_returns_disabled_notice_when_master_off(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv(MASTER_FLAG_ENV_VAR, raising=False)
    r = dispatch_tool_permissions_command("/tool_permissions recent")
    assert r.matched and not r.ok
    assert "archive disabled" in r.text.lower()
    assert "JARVIS_PERMISSION_ARCHIVE_ENABLED" in r.text


# ---------------------------------------------------------------------------
# Subcommand behaviours — recent / tool / op / stats
# ---------------------------------------------------------------------------


def test_recent_empty_archive(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    r = dispatch_tool_permissions_command("/tool_permissions recent")
    assert r.matched and r.ok
    assert "no decisions recorded" in r.text.lower()


def test_recent_shows_records_newest_first(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    maybe_record_decision(
        op_id="op-A", tool_name="read_file",
        decision=_make_decision(),
    )
    maybe_record_decision(
        op_id="op-A", tool_name="write_file",
        decision=_make_decision(tool_name="write_file"),
    )
    r = dispatch_tool_permissions_command("/tool_permissions recent")
    assert r.matched and r.ok
    # Both refs visible
    assert "p-1" in r.text and "p-2" in r.text
    # Newest first → p-2 should appear before p-1 in the text
    assert r.text.index("p-2") < r.text.index("p-1")


def test_recent_respects_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    # Distinct op_ids that don't share digit suffixes with p-N
    # refs (avoids substring collision in the assertion).
    for i in range(5):
        maybe_record_decision(
            op_id=f"op-A{i}", tool_name="read_file",
            decision=_make_decision(op_id=f"op-A{i}"),
        )
    r = dispatch_tool_permissions_command(
        "/tool_permissions recent 2",
    )
    assert r.matched and r.ok
    # Only most-recent 2 (p-5 + p-4) — match the rendered "p-N "
    # prefix with trailing space to avoid substring false positives.
    assert "p-5 " in r.text and "p-4 " in r.text
    assert "p-3 " not in r.text and "p-2 " not in r.text


def test_tool_filter_exact_match(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    maybe_record_decision(
        op_id="op-1", tool_name="read_file",
        decision=_make_decision(tool_name="read_file"),
    )
    maybe_record_decision(
        op_id="op-2", tool_name="bash",
        decision=_make_decision(tool_name="bash", op_id="op-2"),
    )
    r = dispatch_tool_permissions_command(
        "/tool_permissions tool read_file",
    )
    assert r.matched and r.ok
    assert "p-1" in r.text and "p-2" not in r.text


def test_tool_filter_missing_arg(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    r = dispatch_tool_permissions_command(
        "/tool_permissions tool",
    )
    assert r.matched and not r.ok
    assert "missing tool name" in r.text.lower()


def test_op_filter_exact_match(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    maybe_record_decision(
        op_id="op-A", tool_name="read_file",
        decision=_make_decision(op_id="op-A"),
    )
    maybe_record_decision(
        op_id="op-B", tool_name="bash",
        decision=_make_decision(op_id="op-B", tool_name="bash"),
    )
    r = dispatch_tool_permissions_command(
        "/tool_permissions op op-A",
    )
    assert r.matched and r.ok
    assert "p-1" in r.text and "p-2" not in r.text


def test_op_filter_missing_arg(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    r = dispatch_tool_permissions_command("/tool_permissions op")
    assert r.matched and not r.ok
    assert "missing op_id" in r.text.lower()


def test_stats_renders_snapshot(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    maybe_record_decision(
        op_id="op-1", tool_name="read_file",
        decision=_make_decision(),
    )
    r = dispatch_tool_permissions_command("/tool_permissions stats")
    assert r.matched and r.ok
    assert "capacity" in r.text
    assert "size" in r.text
    assert "utilization" in r.text
    assert "schema" in r.text


def test_unknown_subcommand_handled(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    r = dispatch_tool_permissions_command(
        "/tool_permissions garbage",
    )
    assert r.matched and not r.ok
    assert "unknown subcommand" in r.text.lower()


def test_bare_invocation_aliases_to_recent(
    monkeypatch: pytest.MonkeyPatch,
):
    """``/tool_permissions`` (no subcommand) MUST alias to
    ``recent`` — ergonomic invariant."""
    monkeypatch.setenv(MASTER_FLAG_ENV_VAR, "true")
    maybe_record_decision(
        op_id="op-1", tool_name="read_file",
        decision=_make_decision(),
    )
    r = dispatch_tool_permissions_command("/tool_permissions")
    assert r.matched and r.ok
    # Should show the recently-recorded entry (p-1)
    assert "p-1" in r.text


# ---------------------------------------------------------------------------
# Authority asymmetry — AST pins
# ---------------------------------------------------------------------------


def test_ast_pin_no_policy_imports():
    """The REPL surface MUST NOT import the substrate's policy
    code — read-only consumer of the archive's projection."""
    tree = ast.parse(_REPL_SRC)
    forbidden = {
        "compute_permission_decision",
        "evaluate_tool_permission",
        "PermissionRegistry",
        "ToolPermissionCallback",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                assert alias.name not in forbidden, (
                    f"tool_permissions_repl must NOT import "
                    f"policy symbol {alias.name!r}"
                )


def test_ast_pin_dispatcher_function_present():
    """The auto-discovery contract REQUIRES a module-level
    ``dispatch_tool_permissions_command(line)`` callable. Drift
    here breaks naming-cage auto-discovery silently."""
    tree = ast.parse(_REPL_SRC)
    found = any(
        isinstance(n, ast.FunctionDef)
        and n.name == "dispatch_tool_permissions_command"
        for n in tree.body
    )
    assert found, (
        "Module-level dispatch_tool_permissions_command is the "
        "load-bearing naming-cage hook — drift breaks "
        "auto-discovery"
    )


def test_ast_pin_master_flag_composition():
    """The local ``_master_enabled`` MUST defer to the canonical
    ``permission_archive_enabled`` from the substrate — drift
    introduces a parallel master flag (forbidden)."""
    assert "permission_archive_enabled" in _REPL_SRC, (
        "tool_permissions_repl MUST defer to canonical "
        "permission_archive_enabled — no parallel master flag"
    )


def test_ast_pin_help_text_documents_canonical_flag_names():
    """Help text MUST mention the canonical env vars — drift
    here makes operators set the wrong flag."""
    assert "JARVIS_PERMISSION_ARCHIVE_ENABLED" in _REPL_SRC
    assert "JARVIS_PERMISSION_ARCHIVE_SIZE" in _REPL_SRC


# ---------------------------------------------------------------------------
# /expand p-N integration — load-bearing cross-substrate ref pin
# ---------------------------------------------------------------------------


def _serpent_flow_src() -> str:
    """Read serpent_flow.py source for AST pins."""
    from backend.core.ouroboros.battle_test import serpent_flow
    return Path(
        inspect.getfile(serpent_flow),
    ).read_text(encoding="utf-8")


def test_expand_dispatcher_branches_p_prefix():
    """The /expand <ref> dispatcher in serpent_flow MUST branch
    on the ``p-`` prefix and route to ``_expand_permission_decision``.
    Drift here silently disables ``/expand p-N`` recovery."""
    src = _serpent_flow_src()
    # Locate _handle_expand body.
    idx = src.index("def _handle_expand")
    end = src.index("\n    def ", idx + 1)
    body = src[idx:end]
    assert 'startswith("p-")' in body, (
        "_handle_expand MUST branch on the p- prefix — Slice 2 "
        "of v2.89 wires the 5th cross-substrate ref family"
    )
    assert "_expand_permission_decision" in body, (
        "_handle_expand MUST route p-N refs to "
        "_expand_permission_decision"
    )


def test_expand_permission_decision_method_present():
    """The ``_expand_permission_decision`` method MUST be defined
    in serpent_flow + MUST compose the canonical archive's
    ``get_default_archive`` accessor (no parallel state)."""
    src = _serpent_flow_src()
    assert "def _expand_permission_decision" in src, (
        "_expand_permission_decision method MUST be present"
    )
    # Locate the method body.
    idx = src.index("def _expand_permission_decision")
    end = src.index("\n    def ", idx + 1)
    body = src[idx:end]
    assert "permission_decision_archive" in body, (
        "_expand_permission_decision MUST compose the canonical "
        "permission_decision_archive module"
    )
    assert "get_default_archive" in body, (
        "_expand_permission_decision MUST call get_default_archive "
        "(no parallel state)"
    )


def test_expand_summary_includes_perm_recent():
    """The ``_print_expand_summary`` method MUST surface ``p-N``
    refs in the recent-refs listing — operator-discoverability
    invariant."""
    src = _serpent_flow_src()
    idx = src.index("def _print_expand_summary")
    end = src.index("\n    def ", idx + 1)
    body = src[idx:end]
    assert "permission_decision_archive" in body, (
        "_print_expand_summary MUST compose the canonical "
        "permission_decision_archive when listing refs"
    )
    assert "permissions" in body, (
        "_print_expand_summary MUST label the p-N section "
        "as 'permissions'"
    )


def test_expand_handle_docstring_documents_p_prefix():
    """The /expand <ref> docstring MUST list the p-N branch so
    operator help is accurate."""
    src = _serpent_flow_src()
    idx = src.index("def _handle_expand")
    end = src.index("\n    def ", idx + 1)
    body = src[idx:end]
    assert "``p-N``" in body or "p-N" in body, (
        "_handle_expand docstring MUST mention p-N — drift "
        "breaks operator discoverability"
    )

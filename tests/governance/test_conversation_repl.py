"""Regression spine for §41.3 Slice 4 — /conversation REPL.

Three mechanical UX items shipped via one auto-discovered REPL
module. Tests cover:

  * Export — JSONL + markdown formats, default path generation,
    explicit path override, empty-bridge handling.
  * Search — keyword substring (default, mechanical) + semantic
    layer (sub-flag gated, optional).
  * Bookmark — persistent JSONL ledger with bk-N refs; survives
    process restart (counter re-initializes from existing
    ledger); ledger reads work even when bridge master is off.
"""
from __future__ import annotations

import ast as _ast
import asyncio
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, List

import pytest

from backend.core.ouroboros.governance.conversation_repl import (
    BOOKMARK_REF_PREFIX,
    Bookmark,
    ConversationReplDispatchResult,
    append_bookmark,
    bookmarks_jsonl_path,
    dispatch_conversation_command,
    find_bookmark_by_ref,
    read_all_bookmarks,
    register_shipped_invariants,
    reset_bookmark_seq_for_tests,
)


_BRIDGE_FLAG = "JARVIS_CONVERSATION_BRIDGE_ENABLED"
_BOOKMARKS_PATH_FLAG = "JARVIS_CONVERSATION_BOOKMARKS_PATH"
_EXPORT_FORMAT_FLAG = "JARVIS_CONVERSATION_EXPORT_FORMAT"


@pytest.fixture(autouse=True)
def _isolate(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Iterator[None]:
    """Isolate per-test bridge state + bookmark ledger."""
    monkeypatch.delenv(_BRIDGE_FLAG, raising=False)
    monkeypatch.delenv(_EXPORT_FORMAT_FLAG, raising=False)
    monkeypatch.setenv(
        _BOOKMARKS_PATH_FLAG,
        str(tmp_path / "bookmarks.jsonl"),
    )
    # Reset bridge + bookmark seq.
    try:
        from backend.core.ouroboros.governance.conversation_bridge import (  # noqa: E501
            reset_default_bridge,
        )
        reset_default_bridge()
    except Exception:  # noqa: BLE001
        pass
    reset_bookmark_seq_for_tests()
    yield


def _enable_bridge(monkeypatch) -> None:
    monkeypatch.setenv(_BRIDGE_FLAG, "true")


def _seed_bridge(
    role: str = "user",
    text: str = "hello world",
    source: str = "tui_user",
    op_id: str = "",
) -> None:
    """Inject one turn into the canonical bridge."""
    from backend.core.ouroboros.governance.conversation_bridge import (
        get_default_bridge,
    )
    get_default_bridge().record_turn(
        role, text, source=source, op_id=op_id,
    )


# ---------------------------------------------------------------------------
# §33.3 naming-cage
# ---------------------------------------------------------------------------


def test_dispatcher_signature_one_string_arg():
    import inspect
    sig = inspect.signature(dispatch_conversation_command)
    params = list(sig.parameters.values())
    assert len(params) == 1
    assert params[0].name == "line"
    assert not inspect.iscoroutinefunction(
        dispatch_conversation_command
    )


def test_does_not_match_unrelated_lines():
    for line in (
        "", "/help", "/expand q-1", "/m10 fire",
        "/conversationX", "conversationX recent",
    ):
        r = dispatch_conversation_command(line)
        assert r.matched is False, f"{line!r} should not match"


def test_matches_canonical_invocations():
    for line in (
        "/conversation", "/conversation recent",
        "/conversation help", "conversation",
        "conversation stats", "  /conversation stats  ",
    ):
        r = dispatch_conversation_command(line)
        assert r.matched is True, f"{line!r} should match"


# ---------------------------------------------------------------------------
# Master gate
# ---------------------------------------------------------------------------


def test_disabled_when_bridge_master_off():
    r = dispatch_conversation_command("/conversation recent")
    assert r.matched is True
    assert r.ok is False
    assert "JARVIS_CONVERSATION_BRIDGE_ENABLED" in r.text


def test_help_bypasses_master_gate():
    r = dispatch_conversation_command("/conversation help")
    assert r.ok is True
    assert "Subcommands" in r.text


def test_stats_bypasses_master_gate():
    """Stats works even when bridge is off so operators see the
    disabled state."""
    r = dispatch_conversation_command("/conversation stats")
    assert r.ok is True
    assert "bridge_enabled" in r.text


def test_bookmarks_read_bypasses_master_gate(tmp_path):
    """The ledger persists independently of the bridge.
    Operators must be able to list bookmarks even after a
    restart with bridge master-off."""
    bm = Bookmark(
        ref="bk-99",
        op_id="op-test",
        turns=({"role": "user", "text": "hi", "ts": 0.0,
                "source": "tui_user"},),
    )
    append_bookmark(bm)
    r = dispatch_conversation_command("/conversation bookmarks")
    assert r.ok is True
    assert "bk-99" in r.text


# ---------------------------------------------------------------------------
# Recent (alias for bare /conversation)
# ---------------------------------------------------------------------------


def test_recent_empty_bridge(monkeypatch):
    _enable_bridge(monkeypatch)
    r = dispatch_conversation_command("/conversation")
    assert r.ok is True
    assert "empty" in r.text.lower()


def test_recent_with_turns(monkeypatch):
    _enable_bridge(monkeypatch)
    _seed_bridge(text="first turn", op_id="op-001")
    _seed_bridge(
        role="assistant", text="second turn", op_id="op-001",
    )
    r = dispatch_conversation_command("/conversation recent")
    assert r.ok is True
    assert "first turn" in r.text
    assert "second turn" in r.text


def test_recent_respects_limit(monkeypatch):
    _enable_bridge(monkeypatch)
    for i in range(10):
        _seed_bridge(text=f"turn-{i}", op_id=f"op-{i}")
    r = dispatch_conversation_command("/conversation recent 3")
    assert r.ok is True
    # Header line + at most 3 turn lines.
    assert r.text.count("turn-") <= 3


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_empty_bridge(monkeypatch):
    _enable_bridge(monkeypatch)
    r = dispatch_conversation_command("/conversation export")
    assert r.ok is True
    assert "nothing to write" in r.text.lower()


def test_export_jsonl_default(monkeypatch, tmp_path):
    _enable_bridge(monkeypatch)
    _seed_bridge(text="export-me", op_id="op-x")
    path = tmp_path / "out.jsonl"
    r = dispatch_conversation_command(
        f"/conversation export {path}",
    )
    assert r.ok is True
    assert path.exists()
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["text"] == "export-me"
    assert obj["op_id"] == "op-x"


def test_export_md_format(monkeypatch, tmp_path):
    _enable_bridge(monkeypatch)
    _seed_bridge(text="markdown-content", op_id="op-md")
    path = tmp_path / "out.md"
    r = dispatch_conversation_command(
        f"/conversation export {path} -f md",
    )
    assert r.ok is True
    content = path.read_text(encoding="utf-8")
    assert "markdown-content" in content
    # Markdown header convention.
    assert "###" in content


def test_export_unknown_format_returns_error(monkeypatch):
    _enable_bridge(monkeypatch)
    _seed_bridge()
    r = dispatch_conversation_command(
        "/conversation export -f xml",
    )
    assert r.ok is False
    assert "unknown format" in r.text


def test_export_default_path_includes_timestamp(monkeypatch, tmp_path):
    """No path argument → auto-generated under
    .jarvis/conversation/. Run in tmp_path cwd so we don't
    pollute the repo's .jarvis/."""
    _enable_bridge(monkeypatch)
    _seed_bridge()
    monkeypatch.chdir(tmp_path)
    r = dispatch_conversation_command("/conversation export")
    assert r.ok is True
    assert ".jarvis/conversation/export-" in r.text


# ---------------------------------------------------------------------------
# Search — keyword
# ---------------------------------------------------------------------------


def test_search_missing_query(monkeypatch):
    _enable_bridge(monkeypatch)
    r = dispatch_conversation_command("/conversation search")
    assert r.ok is False
    assert "missing query" in r.text


def test_search_empty_bridge(monkeypatch):
    _enable_bridge(monkeypatch)
    r = dispatch_conversation_command(
        "/conversation search hello",
    )
    assert r.ok is True
    assert "empty" in r.text.lower()


def test_search_finds_substring_match(monkeypatch):
    _enable_bridge(monkeypatch)
    _seed_bridge(text="the quick brown fox", op_id="o1")
    _seed_bridge(text="lazy dog jumps", op_id="o2")
    r = dispatch_conversation_command(
        "/conversation search FOX",
    )
    assert r.ok is True
    assert "1 match" in r.text
    assert "quick brown fox" in r.text


def test_search_no_matches(monkeypatch):
    _enable_bridge(monkeypatch)
    _seed_bridge(text="hello world")
    r = dispatch_conversation_command(
        "/conversation search nonexistent",
    )
    assert r.ok is True
    assert "no matches" in r.text


def test_search_multi_word_query(monkeypatch):
    _enable_bridge(monkeypatch)
    _seed_bridge(text="the quick brown fox jumps", op_id="o1")
    r = dispatch_conversation_command(
        '/conversation search "brown fox"',
    )
    assert r.ok is True
    assert "1 match" in r.text


# ---------------------------------------------------------------------------
# Bookmark — write
# ---------------------------------------------------------------------------


def test_bookmark_missing_op_id(monkeypatch):
    _enable_bridge(monkeypatch)
    r = dispatch_conversation_command("/conversation bookmark")
    assert r.ok is False
    assert "missing op_id" in r.text


def test_bookmark_no_matching_turns(monkeypatch):
    _enable_bridge(monkeypatch)
    _seed_bridge(text="turn-A", op_id="op-A")
    r = dispatch_conversation_command(
        "/conversation bookmark op-Z",
    )
    assert r.ok is False
    assert "no live-bridge turns" in r.text


def test_bookmark_persists_turns(monkeypatch):
    _enable_bridge(monkeypatch)
    _seed_bridge(text="first", op_id="op-bm")
    _seed_bridge(
        role="assistant", text="second", op_id="op-bm",
    )
    _seed_bridge(text="other-op", op_id="op-other")
    r = dispatch_conversation_command(
        "/conversation bookmark op-bm",
    )
    assert r.ok is True
    assert "saved 2 turn" in r.text
    # Verify ledger row.
    bookmarks = read_all_bookmarks()
    assert len(bookmarks) == 1
    assert bookmarks[0].op_id == "op-bm"
    assert len(bookmarks[0].turns) == 2


def test_bookmark_ref_uses_bk_prefix(monkeypatch):
    _enable_bridge(monkeypatch)
    _seed_bridge(op_id="op-1")
    r = dispatch_conversation_command(
        "/conversation bookmark op-1",
    )
    assert r.ok is True
    assert " bk-" in r.text


def test_bookmark_refs_monotonic(monkeypatch):
    """Successive bookmarks get monotonically increasing
    bk-N refs."""
    _enable_bridge(monkeypatch)
    _seed_bridge(op_id="op-1")
    _seed_bridge(op_id="op-2")
    dispatch_conversation_command(
        "/conversation bookmark op-1",
    )
    dispatch_conversation_command(
        "/conversation bookmark op-2",
    )
    bookmarks = read_all_bookmarks()
    assert len(bookmarks) == 2
    refs = [bm.ref for bm in bookmarks]
    nums = [int(r[len(BOOKMARK_REF_PREFIX):]) for r in refs]
    assert nums == sorted(nums)
    assert nums[1] == nums[0] + 1


def test_bookmark_seq_reinitializes_from_ledger(monkeypatch):
    """On simulated process restart (reset_bookmark_seq), new
    bookmarks pick up after the existing ledger's max ref."""
    _enable_bridge(monkeypatch)
    _seed_bridge(op_id="op-A")
    dispatch_conversation_command(
        "/conversation bookmark op-A",
    )
    # Simulate process restart.
    reset_bookmark_seq_for_tests()
    _seed_bridge(op_id="op-B")
    dispatch_conversation_command(
        "/conversation bookmark op-B",
    )
    bookmarks = read_all_bookmarks()
    assert len(bookmarks) == 2
    refs = [bm.ref for bm in bookmarks]
    nums = sorted(int(r[len(BOOKMARK_REF_PREFIX):]) for r in refs)
    assert nums == [1, 2]


# ---------------------------------------------------------------------------
# Bookmark — read
# ---------------------------------------------------------------------------


def test_bookmarks_empty_ledger():
    r = dispatch_conversation_command("/conversation bookmarks")
    assert r.ok is True
    assert "empty" in r.text.lower()


def test_bookmarks_lists_in_recent_order():
    """Newest first."""
    append_bookmark(Bookmark(
        ref="bk-1", op_id="op-old",
        bookmarked_at_unix=100.0,
    ))
    append_bookmark(Bookmark(
        ref="bk-2", op_id="op-new",
        bookmarked_at_unix=200.0,
    ))
    r = dispatch_conversation_command("/conversation bookmarks")
    assert r.ok is True
    # bk-2 (newer) should appear before bk-1.
    idx_2 = r.text.find("bk-2")
    idx_1 = r.text.find("bk-1")
    assert idx_2 < idx_1


def test_bookmark_show_resolves_ref():
    append_bookmark(Bookmark(
        ref="bk-42", op_id="op-show",
        turns=(
            {"role": "user", "text": "question?",
             "ts": 100.0, "source": "tui_user"},
            {"role": "assistant", "text": "answer.",
             "ts": 101.0, "source": "claude"},
        ),
    ))
    r = dispatch_conversation_command(
        "/conversation bookmark show bk-42",
    )
    assert r.ok is True
    assert "op-show" in r.text
    assert "question?" in r.text
    assert "answer." in r.text


def test_bookmark_show_unknown_ref():
    r = dispatch_conversation_command(
        "/conversation bookmark show bk-999",
    )
    assert r.ok is False
    assert "not found" in r.text


def test_find_bookmark_by_ref_returns_none_on_garbage():
    assert find_bookmark_by_ref("") is None
    assert find_bookmark_by_ref(None) is None  # type: ignore[arg-type]
    assert find_bookmark_by_ref(42) is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Bookmark — JSONL round-trip
# ---------------------------------------------------------------------------


def test_bookmark_to_dict_from_dict_round_trip():
    original = Bookmark(
        ref="bk-rt",
        op_id="op-rt",
        turns=(
            {"role": "user", "text": "hello", "ts": 1.0,
             "source": "tui_user"},
        ),
        note="test note",
        bookmarked_at_unix=12345.0,
    )
    raw = original.to_dict()
    restored = Bookmark.from_dict(raw)
    assert restored is not None
    assert restored.ref == "bk-rt"
    assert restored.op_id == "op-rt"
    assert len(restored.turns) == 1
    assert restored.note == "test note"


def test_bookmark_from_dict_rejects_missing_required():
    assert Bookmark.from_dict({}) is None
    assert Bookmark.from_dict({"ref": "bk-x"}) is None
    assert Bookmark.from_dict({"op_id": "op-x"}) is None


# ---------------------------------------------------------------------------
# Defensive — NEVER raises
# ---------------------------------------------------------------------------


def test_dispatcher_never_raises_on_garbage():
    for line in (None, 42, [], {}, b"bytes"):  # type: ignore
        try:
            r = dispatch_conversation_command(line)  # type: ignore
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"raised on {line!r}: {exc!r}")
        assert isinstance(
            r, ConversationReplDispatchResult,
        )


def test_parse_error_returns_structured_result(monkeypatch):
    _enable_bridge(monkeypatch)
    r = dispatch_conversation_command(
        '/conversation search "unterminated',
    )
    assert r.matched is True
    assert r.ok is False
    assert "parse error" in r.text


# ---------------------------------------------------------------------------
# AST pins
# ---------------------------------------------------------------------------


def test_register_shipped_invariants_returns_five_pins():
    pins = register_shipped_invariants()
    names = {p.invariant_name for p in pins}
    assert names == {
        "conversation_repl_substrate",
        "conversation_repl_authority_asymmetry",
        "conversation_repl_composes_canonical",
        "conversation_repl_ref_prefix_pinned",
        "conversation_resume_resanitizes",
    }


def test_all_ast_pins_pass_on_current_source():
    pins = register_shipped_invariants()
    src_path = Path(
        "backend/core/ouroboros/governance/conversation_repl.py"
    )
    source = src_path.read_text(encoding="utf-8")
    tree = _ast.parse(source)
    for pin in pins:
        violations = pin.validate(tree, source)
        assert violations == (), (
            f"{pin.invariant_name} drift: {violations}"
        )


def test_authority_asymmetry_no_forbidden_imports():
    src_path = Path(
        "backend/core/ouroboros/governance/conversation_repl.py"
    )
    source = src_path.read_text(encoding="utf-8")
    tree = _ast.parse(source)
    forbidden = {
        "backend.core.ouroboros.governance.orchestrator",
        "backend.core.ouroboros.governance.iron_gate",
        "backend.core.ouroboros.governance.policy",
        "backend.core.ouroboros.governance.policy_engine",
        "backend.core.ouroboros.governance.candidate_generator",
        "backend.core.ouroboros.governance.urgency_router",
        "backend.core.ouroboros.governance.change_engine",
        "backend.core.ouroboros.governance.semantic_guardian",
        "backend.core.ouroboros.governance.auto_committer",
        "backend.core.ouroboros.governance.risk_tier_floor",
        "backend.core.ouroboros.governance.tool_executor",
        "backend.core.ouroboros.governance.providers",
    }
    for node in _ast.walk(tree):
        if isinstance(node, _ast.ImportFrom):
            mod = node.module or ""
            assert mod not in forbidden, (
                f"forbidden import: {mod}"
            )


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------


def test_repl_dispatch_registry_auto_discovers():
    """The §33.3 naming-cage contract end-to-end."""
    from backend.core.ouroboros.battle_test.repl_dispatch_registry import (  # noqa: E501
        prime_registry,
    )
    report = prime_registry(force=True)
    assert "conversation" in report.verbs

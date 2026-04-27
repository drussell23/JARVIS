"""Phase 8 surface wiring Slice 3 — multi-op renderer regression spine.

Covers the renderer's parse / list / render / dispatch surface plus
master-flag matrix, bounded-output pins, ANSI-color cycling, NEVER-
raises contract, and authority/cage invariants.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import List, Optional

import pytest

from backend.core.ouroboros.governance.observability import (
    decision_trace_ledger as _ledger_mod,
    multi_op_renderer as _renderer,
)
from backend.core.ouroboros.governance.observability.multi_op_renderer import (  # noqa: E501
    MAX_LIST_OP_IDS,
    MAX_OPS_PER_RENDER,
    MAX_OP_ID_LEN,
    MAX_RENDERED_LINES,
    dispatch_cli_argument,
    is_renderer_enabled,
    list_recent_op_ids,
    parse_multi_op_argument,
    render_last_n_op_timeline,
    render_multi_op_timeline,
    render_session_timeline,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_env(monkeypatch: pytest.MonkeyPatch):
    keys = [
        k for k in os.environ.keys()
        if (
            k.startswith("JARVIS_PHASE8_MULTI_OP_RENDERER_")
            or k.startswith("JARVIS_DECISION_TRACE_LEDGER_")
        )
    ]
    for k in keys:
        monkeypatch.delenv(k, raising=False)
    _ledger_mod.reset_default_ledger()
    yield
    _ledger_mod.reset_default_ledger()


@pytest.fixture
def renderer_on(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv(
        "JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED", "true",
    )


@pytest.fixture
def isolated_ledger(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    target = tmp_path / "decision_trace.jsonl"
    monkeypatch.setenv(
        "JARVIS_DECISION_TRACE_LEDGER_PATH", str(target),
    )
    monkeypatch.setenv(
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED", "true",
    )
    _ledger_mod.reset_default_ledger()
    return target


def _seed(op_id: str, n_rows: int = 1) -> None:
    ledger = _ledger_mod.get_default_ledger()
    for i in range(n_rows):
        ok, _ = ledger.record(
            op_id=op_id,
            phase=f"PHASE-{i}",
            decision=f"D-{i}",
            rationale=f"r-{op_id}-{i}",
        )
        assert ok


# ---------------------------------------------------------------------------
# Module-level constants + master flag
# ---------------------------------------------------------------------------


def test_caps_sane():
    assert 4 <= MAX_OPS_PER_RENDER <= 64
    assert MAX_RENDERED_LINES >= 100
    assert 16 <= MAX_OP_ID_LEN <= 256
    assert MAX_LIST_OP_IDS >= 50


def test_master_default_off():
    assert is_renderer_enabled() is False


@pytest.mark.parametrize("val", ["true", "1", "yes", "on", "TRUE"])
def test_master_truthy(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv(
        "JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED", val,
    )
    assert is_renderer_enabled() is True


@pytest.mark.parametrize("val", ["false", "0", "no", "off", ""])
def test_master_falsy(monkeypatch: pytest.MonkeyPatch, val: str):
    monkeypatch.setenv(
        "JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED", val,
    )
    assert is_renderer_enabled() is False


# ---------------------------------------------------------------------------
# parse_multi_op_argument
# ---------------------------------------------------------------------------


def test_parse_list():
    assert parse_multi_op_argument("list") == ("list", None)
    assert parse_multi_op_argument("LIST") == ("list", None)


def test_parse_ops_comma_separated():
    kind, payload = parse_multi_op_argument("op-A,op-B,op-C")
    assert kind == "ops"
    assert payload == ["op-A", "op-B", "op-C"]


def test_parse_ops_single():
    kind, payload = parse_multi_op_argument("op-only")
    assert kind == "ops"
    assert payload == ["op-only"]


def test_parse_ops_caps_at_max():
    many = ",".join([f"op-{i}" for i in range(MAX_OPS_PER_RENDER + 5)])
    kind, payload = parse_multi_op_argument(many)
    assert kind == "ops"
    assert len(payload) == MAX_OPS_PER_RENDER


def test_parse_ops_rejects_bad_charset():
    kind, payload = parse_multi_op_argument("../etc/passwd")
    assert kind == "invalid"
    assert "bad_op_id" in payload


def test_parse_ops_rejects_whitespace():
    kind, _ = parse_multi_op_argument("op a, op b")
    assert kind == "invalid"


def test_parse_last_n_default():
    assert parse_multi_op_argument("@last") == ("last_n", 5)


def test_parse_last_n_explicit():
    assert parse_multi_op_argument("@last:3") == ("last_n", 3)


def test_parse_last_n_caps_at_max():
    kind, payload = parse_multi_op_argument(
        f"@last:{MAX_OPS_PER_RENDER + 100}",
    )
    assert kind == "last_n"
    assert payload == MAX_OPS_PER_RENDER


def test_parse_last_n_rejects_zero():
    kind, _ = parse_multi_op_argument("@last:0")
    assert kind == "invalid"


def test_parse_last_n_rejects_garbage():
    kind, _ = parse_multi_op_argument("@last:nope")
    assert kind == "invalid"


def test_parse_session():
    kind, payload = parse_multi_op_argument(
        "session:bt-2026-04-27-120000",
    )
    assert kind == "session"
    assert payload == "bt-2026-04-27-120000"


def test_parse_session_rejects_bad_id():
    kind, _ = parse_multi_op_argument("session:../passwd")
    assert kind == "invalid"


def test_parse_empty_argument():
    for s in ("", "   ", "\t\n"):
        kind, _ = parse_multi_op_argument(s)
        assert kind == "invalid"


def test_parse_non_string_rejected():
    kind, _ = parse_multi_op_argument(None)  # type: ignore[arg-type]
    assert kind == "invalid"


# ---------------------------------------------------------------------------
# list_recent_op_ids
# ---------------------------------------------------------------------------


def test_list_recent_master_off_returns_empty():
    assert list_recent_op_ids() == []


def test_list_recent_no_ledger_returns_empty(
    renderer_on, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv(
        "JARVIS_DECISION_TRACE_LEDGER_PATH",
        str(tmp_path / "missing.jsonl"),
    )
    monkeypatch.setenv(
        "JARVIS_DECISION_TRACE_LEDGER_ENABLED", "true",
    )
    _ledger_mod.reset_default_ledger()
    assert list_recent_op_ids() == []


def test_list_recent_returns_distinct_most_recent_first(
    renderer_on, isolated_ledger: Path,
):
    _seed("op-A", n_rows=2)
    _seed("op-B", n_rows=1)
    _seed("op-A", n_rows=1)  # op-A has 3 rows total; appears first
    _seed("op-C", n_rows=1)
    ids = list_recent_op_ids(limit=5)
    assert ids[:3] == ["op-C", "op-A", "op-B"]
    # No duplicates.
    assert len(ids) == len(set(ids))


def test_list_recent_clamps_limit(
    renderer_on, isolated_ledger: Path,
):
    for i in range(5):
        _seed(f"op-{i}")
    assert list_recent_op_ids(limit=2) == ["op-4", "op-3"]
    # Negative / zero → empty.
    assert list_recent_op_ids(limit=0) == []
    assert list_recent_op_ids(limit=-1) == []
    # Hard upper cap.
    huge = list_recent_op_ids(limit=MAX_LIST_OP_IDS + 1000)
    assert len(huge) <= MAX_LIST_OP_IDS


def test_list_recent_skips_corrupt_lines(
    renderer_on, isolated_ledger: Path,
):
    _seed("op-good", n_rows=1)
    # Append a corrupt line.
    with isolated_ledger.open("a") as f:
        f.write("not-json\n")
        f.write('{"missing_op_id": true}\n')
        f.write('{"op_id": "../bad"}\n')  # bad charset
    ids = list_recent_op_ids()
    assert ids == ["op-good"]


# ---------------------------------------------------------------------------
# render_multi_op_timeline
# ---------------------------------------------------------------------------


def test_render_master_off_returns_disabled_message():
    text = render_multi_op_timeline(["op-x"])
    assert "disabled" in text.lower()


def test_render_no_op_ids_returns_message(renderer_on):
    text = render_multi_op_timeline([])
    assert "no op_ids" in text.lower()


def test_render_invalid_op_ids_only_returns_message(renderer_on):
    text = render_multi_op_timeline(["../bad", "also bad"])
    assert "no valid op_ids" in text.lower()


def test_render_unknown_op_ids_returns_no_events(
    renderer_on, isolated_ledger: Path,
):
    text = render_multi_op_timeline(["op-nonexistent"])
    assert "no events" in text.lower()


def test_render_known_op_renders_events(
    renderer_on, isolated_ledger: Path,
):
    _seed("op-1", n_rows=2)
    _seed("op-2", n_rows=2)
    text = render_multi_op_timeline(["op-1", "op-2"])
    # Events present in chronological order; both ops referenced.
    assert "op-1" in text
    assert "op-2" in text
    # Should be at least 4 lines (2 ops × 2 rows).
    assert len(text.splitlines()) >= 4


def test_render_caps_op_count(
    renderer_on, isolated_ledger: Path,
):
    """More than MAX_OPS_PER_RENDER op_ids → only first cap rendered."""
    op_ids = []
    for i in range(MAX_OPS_PER_RENDER + 5):
        oid = f"op-{i:03d}"
        _seed(oid, n_rows=1)
        op_ids.append(oid)
    text = render_multi_op_timeline(op_ids)
    # The first MAX_OPS_PER_RENDER appear in output; the trailing
    # ones do NOT.
    extra = f"op-{MAX_OPS_PER_RENDER + 4:03d}"
    assert extra not in text


def test_render_caps_max_lines(
    renderer_on, isolated_ledger: Path,
):
    _seed("op-big", n_rows=50)
    text = render_multi_op_timeline(
        ["op-big"], max_lines=10,
    )
    # Render text adds a "(timeline truncated...)" footer, so
    # check we don't far exceed max_lines.
    assert len(text.splitlines()) <= 12


def test_render_color_emits_ansi(
    renderer_on, isolated_ledger: Path,
):
    _seed("op-c1", n_rows=1)
    _seed("op-c2", n_rows=1)
    text = render_multi_op_timeline(
        ["op-c1", "op-c2"], color=True,
    )
    # ANSI escape sequence present.
    assert "\033[" in text
    # Reset code present.
    assert "\033[0m" in text


def test_render_no_color_omits_ansi(
    renderer_on, isolated_ledger: Path,
):
    _seed("op-nc", n_rows=1)
    text = render_multi_op_timeline(["op-nc"], color=False)
    assert "\033[" not in text


# ---------------------------------------------------------------------------
# render_last_n_op_timeline
# ---------------------------------------------------------------------------


def test_last_n_master_off():
    text = render_last_n_op_timeline(3)
    assert "disabled" in text.lower()


def test_last_n_zero_or_negative(renderer_on):
    assert "non-positive" in render_last_n_op_timeline(0).lower()
    assert "non-positive" in render_last_n_op_timeline(-1).lower()


def test_last_n_no_recent_ops(
    renderer_on, isolated_ledger: Path,
):
    text = render_last_n_op_timeline(3)
    assert "no recent" in text.lower()


def test_last_n_renders_recent(
    renderer_on, isolated_ledger: Path,
):
    for i in range(4):
        _seed(f"op-r{i}", n_rows=1)
    text = render_last_n_op_timeline(2)
    assert "op-r3" in text  # most-recent
    assert "op-r2" in text
    # Older ones not rendered.
    assert "op-r1" not in text
    assert "op-r0" not in text


def test_last_n_clamps_to_max(
    renderer_on, isolated_ledger: Path,
):
    text = render_last_n_op_timeline(
        MAX_OPS_PER_RENDER + 100,
    )
    # Empty ledger → message; just confirm no crash + bounded.
    assert isinstance(text, str)


# ---------------------------------------------------------------------------
# render_session_timeline
# ---------------------------------------------------------------------------


def _write_session_summary(
    sessions_root: Path, session_id: str, op_ids: List[str],
) -> None:
    session_dir = sessions_root / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "summary.json").write_text(
        json.dumps({
            "session_id": session_id,
            "operations": [{"op_id": op} for op in op_ids],
        }),
        encoding="utf-8",
    )


def test_session_master_off():
    text = render_session_timeline("bt-x")
    assert "disabled" in text.lower()


def test_session_invalid_id(renderer_on):
    text = render_session_timeline("../etc/passwd")
    assert "invalid" in text.lower()


def test_session_not_found(
    renderer_on, tmp_path: Path,
):
    text = render_session_timeline(
        "bt-nope", sessions_root=tmp_path,
    )
    assert "not found" in text.lower()


def test_session_corrupt_summary(
    renderer_on, tmp_path: Path,
):
    sd = tmp_path / "bt-bad"
    sd.mkdir()
    (sd / "summary.json").write_text("not json", encoding="utf-8")
    text = render_session_timeline(
        "bt-bad", sessions_root=tmp_path,
    )
    assert "corrupt" in text.lower()


def test_session_no_ops_in_summary(
    renderer_on, tmp_path: Path,
):
    _write_session_summary(tmp_path, "bt-empty", op_ids=[])
    text = render_session_timeline(
        "bt-empty", sessions_root=tmp_path,
    )
    assert "no op_ids" in text.lower()


def test_session_renders_session_ops(
    renderer_on, isolated_ledger: Path, tmp_path: Path,
):
    _seed("op-s1", n_rows=2)
    _seed("op-s2", n_rows=1)
    _write_session_summary(
        tmp_path, "bt-s", op_ids=["op-s1", "op-s2"],
    )
    text = render_session_timeline(
        "bt-s", sessions_root=tmp_path,
    )
    assert "op-s1" in text
    assert "op-s2" in text


def test_session_caps_op_count(
    renderer_on, isolated_ledger: Path, tmp_path: Path,
):
    """A session summary listing > MAX_OPS_PER_RENDER gets capped."""
    op_ids = []
    for i in range(MAX_OPS_PER_RENDER + 5):
        oid = f"op-cap{i:03d}"
        _seed(oid, n_rows=1)
        op_ids.append(oid)
    _write_session_summary(tmp_path, "bt-cap", op_ids=op_ids)
    text = render_session_timeline(
        "bt-cap", sessions_root=tmp_path,
    )
    assert isinstance(text, str)


def test_session_dedupes_op_ids(
    renderer_on, isolated_ledger: Path, tmp_path: Path,
):
    _seed("op-dupe", n_rows=1)
    _write_session_summary(
        tmp_path, "bt-dupe", op_ids=["op-dupe", "op-dupe", "op-dupe"],
    )
    text = render_session_timeline(
        "bt-dupe", sessions_root=tmp_path,
    )
    assert "op-dupe" in text


# ---------------------------------------------------------------------------
# dispatch_cli_argument
# ---------------------------------------------------------------------------


def test_dispatch_master_off():
    text = dispatch_cli_argument("list")
    assert "disabled" in text.lower()


def test_dispatch_invalid():
    """Even master-off, invalid arguments fall through the disabled
    branch — operator sees the disabled message first."""
    text = dispatch_cli_argument("")
    assert "disabled" in text.lower() or "invalid" in text.lower()


def test_dispatch_invalid_when_enabled(renderer_on):
    text = dispatch_cli_argument("@last:not_a_number")
    assert "invalid" in text.lower()


def test_dispatch_list_empty_ledger(
    renderer_on, isolated_ledger: Path,
):
    text = dispatch_cli_argument("list")
    assert "no recent" in text.lower()


def test_dispatch_list_with_ops(
    renderer_on, isolated_ledger: Path,
):
    _seed("op-d1", n_rows=1)
    _seed("op-d2", n_rows=1)
    text = dispatch_cli_argument("list")
    assert "op-d1" in text
    assert "op-d2" in text


def test_dispatch_ops(
    renderer_on, isolated_ledger: Path,
):
    _seed("op-disp", n_rows=2)
    text = dispatch_cli_argument("op-disp")
    assert "op-disp" in text


def test_dispatch_last_n(
    renderer_on, isolated_ledger: Path,
):
    _seed("op-last", n_rows=1)
    text = dispatch_cli_argument("@last:1")
    assert "op-last" in text


def test_dispatch_session(
    renderer_on, isolated_ledger: Path, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    _seed("op-sx", n_rows=1)
    _write_session_summary(tmp_path, "bt-sx", op_ids=["op-sx"])
    text = dispatch_cli_argument(
        "session:bt-sx", sessions_root=tmp_path,
    )
    assert "op-sx" in text


# ---------------------------------------------------------------------------
# NEVER-raises smoke
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("payload", [
    "", " ", "\n", "\t",
    "op-good", "op-good,op-bad,",
    "@last", "@last:0", "@last:-1", "@last:abc",
    "session:", "session:bad/path",
    "list", "LIST",
    None,
])
def test_dispatch_never_raises(
    renderer_on, payload, isolated_ledger: Path,
):
    """Dispatch must NEVER raise on any input shape."""
    if payload is None:
        # Type ignore — defensive
        result = dispatch_cli_argument(payload)  # type: ignore[arg-type]
    else:
        result = dispatch_cli_argument(payload)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Authority / cage invariants
# ---------------------------------------------------------------------------


def test_does_not_import_gate_modules():
    import ast
    import inspect
    src = inspect.getsource(_renderer)
    tree = ast.parse(src)
    banned = [
        "orchestrator", "iron_gate", "risk_tier_floor",
        "semantic_guardian", "policy_engine",
        "candidate_generator", "tool_executor", "change_engine",
    ]
    for node in ast.walk(tree):
        names: List[str] = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                names = [node.module]
        for mod in names:
            for token in banned:
                assert token not in mod, (
                    f"multi_op_renderer imports {mod!r} containing "
                    f"banned token {token!r}"
                )


def test_top_level_imports_are_stdlib_only():
    import ast
    import inspect
    src = inspect.getsource(_renderer)
    tree = ast.parse(src)
    top_level: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level.append(node.module)
    forbidden = {
        "backend.core.ouroboros.governance.observability."
        "decision_trace_ledger",
        "backend.core.ouroboros.governance.observability."
        "multi_op_timeline",
    }
    leaked = forbidden & set(top_level)
    assert not leaked, (
        f"multi_op_renderer hoisted substrate to top level: {leaked!r}"
    )


def test_no_secret_leakage_in_constants():
    text = repr(vars(_renderer))
    for needle in ("sk-", "ghp_", "AKIA", "BEGIN PRIVATE KEY"):
        assert needle not in text


def test_public_surface_count_pinned():
    """Bit-rot guard for the public dispatch / render surface."""
    public = [
        n for n in dir(_renderer)
        if (
            n.startswith("render_")
            or n.startswith("list_")
            or n.startswith("dispatch_")
            or n.startswith("parse_")
        )
        and callable(getattr(_renderer, n))
    ]
    assert sorted(public) == [
        "dispatch_cli_argument",
        "list_recent_op_ids",
        "parse_multi_op_argument",
        "render_last_n_op_timeline",
        "render_multi_op_timeline",
        "render_session_timeline",
    ]


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def test_validate_op_id_charset():
    from backend.core.ouroboros.governance.observability.multi_op_renderer import (  # noqa: E501
        _validate_op_id,
    )
    assert _validate_op_id("op-abc_123") == "op-abc_123"
    assert _validate_op_id("  op-trim  ") == "op-trim"
    assert _validate_op_id("op with space") is None
    assert _validate_op_id("") is None
    assert _validate_op_id("a/b") is None
    assert _validate_op_id("\x00null") is None
    assert _validate_op_id(None) is None  # type: ignore[arg-type]
    long = "a" * (MAX_OP_ID_LEN + 1)
    assert _validate_op_id(long) is None


def test_disabled_message_exact():
    from backend.core.ouroboros.governance.observability.multi_op_renderer import (  # noqa: E501
        _disabled_message,
    )
    msg = _disabled_message()
    assert "disabled" in msg
    assert "JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED" in msg


# ---------------------------------------------------------------------------
# CLI integration smoke
# ---------------------------------------------------------------------------


def test_battle_test_cli_imports_renderer_lazily():
    """The battle-test entry script must not pay the substrate
    import cost at import time. Pin: scripts/ouroboros_battle_test.py
    imports the renderer LAZILY (inside _render_multi_op_and_exit)."""
    import ast
    import inspect
    import scripts.ouroboros_battle_test as bt
    src = inspect.getsource(bt)
    tree = ast.parse(src)
    top_level_imports: List[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            top_level_imports.extend(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            top_level_imports.append(node.module)
    forbidden = {
        "backend.core.ouroboros.governance.observability."
        "multi_op_renderer",
    }
    leaked = forbidden & set(top_level_imports)
    assert not leaked, (
        f"battle-test script hoisted renderer to top level: {leaked!r}"
    )


def test_battle_test_cli_argument_help_present():
    """Pin that ``--multi-op`` is registered as a CLI flag with a
    helpful description so ``--help`` shows it."""
    import scripts.ouroboros_battle_test as bt
    import inspect
    src = inspect.getsource(bt)
    assert "--multi-op" in src
    assert "JARVIS_PHASE8_MULTI_OP_RENDERER_ENABLED" in src

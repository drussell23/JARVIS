"""Tests for the Repair Context Bridge — Slice 1 traceback->node mapper.

Covers the pure mapper (`repair_traceback.py`):
1. Parse `--tb=short` FAILURES into per-test frames (print order).
2. Correlate a `file.py::Class::test` id to its block.
3. AST-map line -> innermost Oracle node via stored line_number + line_count.
4. fault_node_keys are deepest-first, de-duplicated, repo-internal only.
5. External frames (site-packages / stdlib) are recorded but never mapped.
6. Fail-soft: garbage input / no resolver degrade cleanly (never raise).
"""
from __future__ import annotations

import textwrap
from typing import Any, Dict, List, Optional

import pytest

from backend.core.ouroboros.governance.intent.repair_traceback import (
    Frame,
    bridge_enabled,
    build_traceback_map,
    map_frames_to_nodes,
    parse_pytest_tracebacks,
)


# --------------------------------------------------------------------------- fixtures
_TB_SHORT = textwrap.dedent("""\
    =================================== FAILURES ===================================
    _______________________________ test_parse ____________________________________
    tests/test_core.py:12: in test_parse
        assert parse("x") == 3
    src/calc.py:42: in parse
        return _inner(s)
    src/calc.py:88: in _inner
        raise ValueError("boom")
    E   ValueError: boom
    ________________________________ test_timeout __________________________________
    tests/test_net.py:20: in test_timeout
        connect()
    /usr/lib/python3.11/socket.py:833: in connect
        raise TimeoutError
    E   TimeoutError: connection timed out
    =========================== short test summary info ============================
    FAILED tests/test_core.py::test_parse - ValueError: boom
    FAILED tests/test_net.py::test_timeout - TimeoutError: connection timed out
""")


class _FakeResolver:
    """Minimal GraphBackend-shaped resolver: file -> node keys, key -> attrs."""

    def __init__(self, nodes: Dict[str, Dict[str, Any]]) -> None:
        # nodes: key -> {"file": rel, "line_number": int, "line_count": int}
        self._nodes = nodes
        self._by_file: Dict[str, List[str]] = {}
        for key, attrs in nodes.items():
            self._by_file.setdefault(attrs["file"], []).append(key)

    def nodes_in_file(self, file_path: str) -> List[str]:
        return list(self._by_file.get(file_path, []))

    def get_node(self, key: str) -> Optional[Dict[str, Any]]:
        a = self._nodes.get(key)
        if a is None:
            return None
        return {"node_id": {"line_number": a["line_number"]}, "line_count": a["line_count"]}


def _resolver() -> _FakeResolver:
    return _FakeResolver({
        "src/calc.py::parse": {"file": "src/calc.py", "line_number": 40, "line_count": 10},
        "src/calc.py::_inner": {"file": "src/calc.py", "line_number": 85, "line_count": 6},
        "src/calc.py": {"file": "src/calc.py", "line_number": 1, "line_count": 300},
        "tests/test_core.py::test_parse": {"file": "tests/test_core.py", "line_number": 10, "line_count": 5},
    })


# --------------------------------------------------------------------------- parse
class TestParse:
    def test_parses_two_blocks(self) -> None:
        blocks = parse_pytest_tracebacks(_TB_SHORT)
        assert set(blocks) == {"test_parse", "test_timeout"}

    def test_frames_in_print_order(self) -> None:
        blocks = parse_pytest_tracebacks(_TB_SHORT)
        frames = blocks["test_parse"]
        assert [(f.file, f.line, f.func) for f in frames] == [
            ("tests/test_core.py", 12, "test_parse"),
            ("src/calc.py", 42, "parse"),
            ("src/calc.py", 88, "_inner"),
        ]

    def test_garbage_returns_empty(self) -> None:
        assert parse_pytest_tracebacks("no failures here\njust noise") == {}


# --------------------------------------------------------------------------- map
class TestMap:
    def test_innermost_span_wins(self) -> None:
        # line 88 is inside both the file node (span 300) and _inner (span 6) -> _inner
        frames = [Frame(file="src/calc.py", line=88, func="_inner")]
        map_frames_to_nodes(frames, ["."], _resolver())
        assert frames[0].node_key == "src/calc.py::_inner"
        assert frames[0].in_repo is True
        assert frames[0].rel_path == "src/calc.py"

    def test_line_maps_to_enclosing_function(self) -> None:
        frames = [Frame(file="src/calc.py", line=42, func="parse")]
        map_frames_to_nodes(frames, ["."], _resolver())
        assert frames[0].node_key == "src/calc.py::parse"

    def test_external_frame_skipped(self) -> None:
        frames = [Frame(file="/usr/lib/python3.11/socket.py", line=833, func="connect")]
        map_frames_to_nodes(frames, ["."], _resolver())
        assert frames[0].in_repo is False
        assert frames[0].node_key is None

    def test_no_resolver_records_frames_only(self) -> None:
        frames = [Frame(file="src/calc.py", line=88, func="_inner")]
        map_frames_to_nodes(frames, ["."], None)
        assert frames[0].in_repo is True
        assert frames[0].node_key is None


# --------------------------------------------------------------------------- end-to-end
class TestBuildTracebackMap:
    def test_fault_keys_deepest_first_repo_internal(self) -> None:
        tb = build_traceback_map(_TB_SHORT, "tests/test_core.py::test_parse", ["."], _resolver())
        # deepest in-repo frame first; the test-file frame's node included; no None entries
        assert tb.fault_node_keys[0] == "src/calc.py::_inner"
        assert "src/calc.py::parse" in tb.fault_node_keys
        assert all(k for k in tb.fault_node_keys)

    def test_evidence_shape(self) -> None:
        tb = build_traceback_map(_TB_SHORT, "tests/test_core.py::test_parse", ["."], _resolver())
        ev = tb.to_evidence()
        assert set(ev) == {"traceback_frames", "fault_node_keys"}
        assert isinstance(ev["traceback_frames"], list)
        assert ev["traceback_frames"][0]["func"] == "test_parse"

    def test_external_only_block_has_no_fault_keys(self) -> None:
        tb = build_traceback_map(_TB_SHORT, "tests/test_net.py::test_timeout", ["."], _resolver())
        # test_timeout's only repo frame is the test file (unmapped here) + a stdlib frame
        assert "src/calc.py::_inner" not in tb.fault_node_keys

    def test_unknown_test_id_empty_map(self) -> None:
        tb = build_traceback_map(_TB_SHORT, "tests/test_x.py::test_missing", ["."], _resolver())
        assert tb.frames == []
        assert tb.fault_node_keys == []

    def test_failsoft_on_bad_input(self) -> None:
        # None stdout would raise inside; build_traceback_map must swallow and return empty
        tb = build_traceback_map(None, "x::y", ["."], _resolver())  # type: ignore[arg-type]
        assert tb.frames == []


# --------------------------------------------------------------------------- flag
class TestFlag:
    def test_default_on_graduated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # graduated 2026-06-18 → default ON when env unset
        monkeypatch.delenv("JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED", raising=False)
        assert bridge_enabled() is True

    @pytest.mark.parametrize("val", ["1", "true", "TRUE", "yes", "on"])
    def test_truthy(self, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
        monkeypatch.setenv("JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED", val)
        assert bridge_enabled() is True

    def test_falsey(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JARVIS_REPAIR_CONTEXT_BRIDGE_ENABLED", "0")
        assert bridge_enabled() is False

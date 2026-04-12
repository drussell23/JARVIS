"""Tests for the docstring multi-line collapse Iron Gate."""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.docstring_collapse_gate import (
    _is_collapsed,
    _walk_docstrings,
    check_candidate,
    check_python_source,
)


# ----------------------------------------------------------------------
# AST helpers
# ----------------------------------------------------------------------


class TestWalkDocstrings:
    def test_module_docstring_keyed_under_module(self) -> None:
        src = '"""hi there"""\n'
        tree = ast.parse(src)
        out = _walk_docstrings(tree)
        assert "<module>" in out

    def test_function_docstring_qualname(self) -> None:
        src = 'def foo():\n    """body"""\n    return 1\n'
        tree = ast.parse(src)
        out = _walk_docstrings(tree)
        assert "foo" in out

    def test_class_method_qualname(self) -> None:
        src = (
            "class Outer:\n"
            "    class Inner:\n"
            "        def method(self):\n"
            "            \"\"\"deep\"\"\"\n"
            "            return 1\n"
        )
        tree = ast.parse(src)
        out = _walk_docstrings(tree)
        assert "Outer.Inner.method" in out

    def test_no_docstring_returns_empty(self) -> None:
        src = "x = 1\n"
        out = _walk_docstrings(ast.parse(src))
        assert out == {}

    def test_async_function_docstring(self) -> None:
        src = 'async def foo():\n    """async hello"""\n    return 1\n'
        out = _walk_docstrings(ast.parse(src))
        assert "foo" in out


# ----------------------------------------------------------------------
# Collapse detection on individual nodes
# ----------------------------------------------------------------------


class TestIsCollapsed:
    def _first(self, src: str) -> tuple:
        tree = ast.parse(src)
        docs = _walk_docstrings(tree)
        return next(iter(docs.values()))

    def test_collapsed_module_docstring(self) -> None:
        src = '"""\\nHello\\nWorld\\n"""\n'
        expr, val = self._first(src)
        assert _is_collapsed(expr, val) is True

    def test_proper_multiline_docstring_passes(self) -> None:
        src = '"""\nHello\nWorld\n"""\n'  # actual newlines in source
        expr, val = self._first(src)
        assert _is_collapsed(expr, val) is False

    def test_genuine_single_line_passes(self) -> None:
        src = '"""one liner"""\n'
        expr, val = self._first(src)
        assert _is_collapsed(expr, val) is False

    def test_single_line_with_no_newline_in_value_passes(self) -> None:
        # No \n escape — this is a legitimate one-liner
        src = '"""short docstring"""\n'
        expr, val = self._first(src)
        assert _is_collapsed(expr, val) is False


# ----------------------------------------------------------------------
# check_python_source — main logic
# ----------------------------------------------------------------------


class TestCheckPythonSource:
    def test_clean_multiline_passes(self) -> None:
        new = '"""\nProper docstring\n\nWith blank line.\n"""\n\ndef foo():\n    return 1\n'
        assert check_python_source(new, source_content=None) is None

    def test_collapsed_module_docstring_in_new_file_rejected(self) -> None:
        new = '"""\\nNew module\\n\\nMulti paragraph.\\n"""\nimport os\n'
        result = check_python_source(new, source_content=None)
        assert result is not None
        reason, offenders = result
        assert "docstring_collapse" in reason
        assert any("<module>" in o for o in offenders)

    def test_collapsed_when_original_was_multiline_rejected(self) -> None:
        old = '"""\nOriginal\n\nMulti-line docstring.\n"""\n'
        new = '"""\\nOriginal\\n\\nMulti-line docstring.\\n"""\n'
        result = check_python_source(new, source_content=old)
        assert result is not None
        reason, offenders = result
        assert "docstring_collapse" in reason
        assert any("<module>" in o for o in offenders)

    def test_collapsed_when_original_was_single_line_passes(self) -> None:
        # Legacy tolerance: don't punish a candidate for mirroring an
        # already-collapsed docstring on disk.
        old = '"""\\nLegacy\\nalready collapsed\\n"""\n'
        new = '"""\\nLegacy\\nalready collapsed\\n"""\n'
        assert check_python_source(new, source_content=old) is None

    def test_function_docstring_collapse_rejected(self) -> None:
        old = (
            "def foo():\n"
            "    \"\"\"\n"
            "    Original multi-line.\n"
            "\n"
            "    Args: nothing.\n"
            "    \"\"\"\n"
            "    return 1\n"
        )
        new = (
            "def foo():\n"
            '    """\\nOriginal multi-line.\\n\\nArgs: nothing.\\n"""\n'
            "    return 1\n"
        )
        result = check_python_source(new, source_content=old)
        assert result is not None
        _reason, offenders = result
        assert any("foo" in o for o in offenders)

    def test_class_method_docstring_collapse_rejected(self) -> None:
        old = (
            "class Foo:\n"
            "    def bar(self):\n"
            "        \"\"\"\n"
            "        Multi-line.\n"
            "\n"
            "        With paragraphs.\n"
            "        \"\"\"\n"
            "        return 1\n"
        )
        new = (
            "class Foo:\n"
            "    def bar(self):\n"
            '        """\\nMulti-line.\\n\\nWith paragraphs.\\n"""\n'
            "        return 1\n"
        )
        result = check_python_source(new, source_content=old)
        assert result is not None
        _reason, offenders = result
        assert any("Foo.bar" in o for o in offenders)

    def test_genuine_one_liner_in_new_file_passes(self) -> None:
        new = '"""Short summary."""\n\ndef foo():\n    """Also short."""\n    return 1\n'
        assert check_python_source(new, source_content=None) is None

    def test_syntax_error_content_returns_none(self) -> None:
        # We don't compete with the AST validator — broken syntax is its job
        assert check_python_source("def foo(:\n    pass\n", source_content=None) is None

    def test_empty_content_returns_none(self) -> None:
        assert check_python_source("", source_content=None) is None

    def test_no_docstrings_at_all_passes(self) -> None:
        assert check_python_source("import os\n\nx = 1\n", source_content=None) is None


# ----------------------------------------------------------------------
# Real-world regression: bt-2026-04-11-211131 headless_cli.py
# ----------------------------------------------------------------------


class TestRealRegression:
    def test_headless_cli_collapse_pattern_rejected(self) -> None:
        # Reproduce the exact shape from the failing battle test
        old = (
            '"""\n'
            "Headless CLI — One-shot Ouroboros governance operations.\n"
            "\n"
            "Gap 4: Run Ouroboros without the full supervisor boot.\n"
            '"""\n'
            "from __future__ import annotations\n"
        )
        new = (
            '"""\\nHeadless CLI — One-shot Ouroboros governance operations.\\n\\n'
            'Gap 4: Run Ouroboros without the full supervisor boot.\\n"""\n'
            "from __future__ import annotations\n"
        )
        result = check_python_source(new, source_content=old)
        assert result is not None
        reason, offenders = result
        assert "docstring_collapse" in reason
        assert any("<module>" in o for o in offenders)


# ----------------------------------------------------------------------
# check_candidate — orchestrator entry point shape
# ----------------------------------------------------------------------


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path


class TestCheckCandidate:
    def test_legacy_single_file_shape_with_collapse(self, workspace: Path) -> None:
        target = workspace / "module.py"
        target.write_text('"""\nOriginal\n\nMulti.\n"""\n')
        cand = {
            "file_path": "module.py",
            "full_content": '"""\\nOriginal\\n\\nMulti.\\n"""\n',
        }
        result = check_candidate(cand, workspace)
        assert result is not None

    def test_multi_file_shape_with_collapse(self, workspace: Path) -> None:
        target = workspace / "a.py"
        target.write_text('"""\nA\n\nA.\n"""\n')
        (workspace / "b.py").write_text('"""B."""\n')
        cand = {
            "files": [
                {"file_path": "b.py", "full_content": '"""B."""\n'},
                {
                    "file_path": "a.py",
                    "full_content": '"""\\nA\\n\\nA.\\n"""\n',
                },
            ],
        }
        result = check_candidate(cand, workspace)
        assert result is not None

    def test_non_py_file_skipped(self, workspace: Path) -> None:
        cand = {
            "file_path": "README.md",
            "full_content": '"""\\nNot python\\n"""\n',
        }
        assert check_candidate(cand, workspace) is None

    def test_clean_candidate_passes(self, workspace: Path) -> None:
        target = workspace / "ok.py"
        target.write_text('"""\nOriginal\n\nclean.\n"""\n')
        cand = {
            "file_path": "ok.py",
            "full_content": '"""\nOriginal\n\nclean.\n"""\n',
        }
        assert check_candidate(cand, workspace) is None

    def test_new_file_collapse_rejected(self, workspace: Path) -> None:
        # File doesn't exist on disk yet — new file with collapse
        cand = {
            "file_path": "newmod.py",
            "full_content": '"""\\nNew\\nmodule\\n"""\nimport os\n',
        }
        assert check_candidate(cand, workspace) is not None

    def test_disabled_via_env(
        self, workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("JARVIS_DOCSTRING_COLLAPSE_GATE_ENABLED", "false")
        # Re-import to pick up the env change
        import importlib
        import backend.core.ouroboros.governance.docstring_collapse_gate as mod
        importlib.reload(mod)
        try:
            target = workspace / "module.py"
            target.write_text('"""\nOriginal\n\nMulti.\n"""\n')
            cand = {
                "file_path": "module.py",
                "full_content": '"""\\nOriginal\\n\\nMulti.\\n"""\n',
            }
            assert mod.check_candidate(cand, workspace) is None
        finally:
            monkeypatch.delenv("JARVIS_DOCSTRING_COLLAPSE_GATE_ENABLED", raising=False)
            importlib.reload(mod)

    def test_empty_candidate_returns_none(self, workspace: Path) -> None:
        assert check_candidate({}, workspace) is None
        assert check_candidate({"file_path": "x.py"}, workspace) is None
        assert check_candidate({"full_content": "x = 1\n"}, workspace) is None

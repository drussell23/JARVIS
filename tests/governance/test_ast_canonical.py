"""Move 6 Slice 2 — AST canonical signature regression spine.

Coverage tracks the four canonicalization invariants:

  * **Identity** — same source code → same hash
  * **Noise-invariance** — whitespace + comments + (optional)
    docstrings don't change hash
  * **Literal normalization** — when normalize_literals=True
    (default), same structure with different literal values →
    same hash; literal TYPE (int vs str vs bool) preserved
  * **Semantic preservation** — symbol names + control flow +
    type annotations + import paths all change hash when changed

Plus:
  * compute_multi_file_signature — order-stable, empty handling,
    per-file isolation
  * Defensive contract — never raises on garbage input
  * Authority invariants — AST-pinned (stdlib only, no governance,
    no async, no disk writes)
  * Env knobs — strip_docstrings_default + normalize_literals_default
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.verification import (
    ast_canonical as canon_mod,
)
from backend.core.ouroboros.governance.verification.ast_canonical import (  # noqa: E501
    AST_CANONICAL_SCHEMA_VERSION,
    compute_ast_signature,
    compute_multi_file_signature,
    normalize_literals_default,
    strip_docstrings_default,
)


# ---------------------------------------------------------------------------
# 1. Identity — same source → same hash
# ---------------------------------------------------------------------------


class TestIdentity:
    def test_same_source_same_hash(self):
        s = "def foo():\n    return 42\n"
        assert compute_ast_signature(s) == compute_ast_signature(s)

    def test_returns_64_char_hex(self):
        sig = compute_ast_signature("x = 1")
        assert len(sig) == 64
        # Verify it's hex
        int(sig, 16)  # raises ValueError if not hex

    def test_schema_version_pinned(self):
        assert AST_CANONICAL_SCHEMA_VERSION == "ast_canonical.1"


# ---------------------------------------------------------------------------
# 2. Noise-invariance — whitespace + comments + docstrings
# ---------------------------------------------------------------------------


class TestNoiseInvariance:
    def test_whitespace_invariant(self):
        a = "def foo():\n    return 42"
        b = "def  foo():\n        return    42"
        assert compute_ast_signature(a) == compute_ast_signature(b)

    def test_blank_lines_invariant(self):
        a = "def foo():\n    return 42"
        b = "\n\ndef foo():\n\n    return 42\n\n"
        assert compute_ast_signature(a) == compute_ast_signature(b)

    def test_comment_invariant(self):
        a = "def foo():\n    return 42"
        b = "def foo():\n    # this is a comment\n    return 42"
        assert compute_ast_signature(a) == compute_ast_signature(b)

    def test_inline_comment_invariant(self):
        a = "x = 1"
        b = "x = 1  # inline comment"
        assert compute_ast_signature(a) == compute_ast_signature(b)

    def test_docstring_kept_by_default(self):
        # Default: docstrings kept (conservative)
        a = 'def foo():\n    """docstring"""\n    return 42'
        b = "def foo():\n    return 42"
        # Different — docstring is part of AST
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_docstring_stripped_when_flag_set(self):
        a = 'def foo():\n    """docstring"""\n    return 42'
        b = "def foo():\n    return 42"
        # With strip_docstrings=True, hashes match
        assert compute_ast_signature(
            a, strip_docstrings=True,
        ) == compute_ast_signature(b, strip_docstrings=True)

    def test_module_level_docstring_stripped(self):
        a = '"""module doc"""\nx = 1'
        b = "x = 1"
        assert compute_ast_signature(
            a, strip_docstrings=True,
        ) == compute_ast_signature(b, strip_docstrings=True)

    def test_class_docstring_stripped(self):
        a = 'class C:\n    """class doc"""\n    pass'
        b = "class C:\n    pass"
        assert compute_ast_signature(
            a, strip_docstrings=True,
        ) == compute_ast_signature(b, strip_docstrings=True)


# ---------------------------------------------------------------------------
# 3. Literal normalization (the Move 6 core invariant)
# ---------------------------------------------------------------------------


class TestLiteralNormalization:
    def test_int_literals_normalized(self):
        # Two functions returning different ints — same structure
        a = "def foo(): return 42"
        b = "def foo(): return 99"
        assert compute_ast_signature(a) == compute_ast_signature(b)

    def test_str_literals_normalized(self):
        a = 'def foo(): return "hello"'
        b = 'def foo(): return "world"'
        assert compute_ast_signature(a) == compute_ast_signature(b)

    def test_float_literals_normalized(self):
        a = "x = 3.14"
        b = "x = 2.71"
        assert compute_ast_signature(a) == compute_ast_signature(b)

    def test_bool_literals_normalized(self):
        a = "x = True"
        b = "x = False"
        assert compute_ast_signature(a) == compute_ast_signature(b)

    def test_none_normalized(self):
        a = "x = None"
        b = "x = None"
        assert compute_ast_signature(a) == compute_ast_signature(b)

    def test_int_vs_str_distinct(self):
        # Critical: same lexical "42" but different types
        a = "x = 42"
        b = 'x = "42"'
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_int_vs_float_distinct(self):
        a = "x = 42"
        b = "x = 42.0"
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_int_vs_bool_distinct(self):
        # Python bool is subclass of int — but our type-tag mapping
        # keeps them distinct for semantic clarity
        a = "x = 1"
        b = "x = True"
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_int_vs_none_distinct(self):
        a = "x = 0"
        b = "x = None"
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_bytes_normalized(self):
        a = "x = b'foo'"
        b = "x = b'bar'"
        assert compute_ast_signature(a) == compute_ast_signature(b)

    def test_quine_detection_scenario(self):
        # The CRITICAL Move 6 invariant: three rolls with same
        # structural shape but different literal values converge
        roll_1 = "def helper(x): return x * 2"
        roll_2 = "def helper(x): return x * 3"
        roll_3 = "def helper(x): return x * 5"
        s1 = compute_ast_signature(roll_1)
        s2 = compute_ast_signature(roll_2)
        s3 = compute_ast_signature(roll_3)
        assert s1 == s2 == s3

    def test_literals_strict_when_disabled(self):
        # normalize_literals=False — distinct values produce
        # distinct hashes
        a = compute_ast_signature("x = 42", normalize_literals=False)
        b = compute_ast_signature("x = 99", normalize_literals=False)
        assert a != b


# ---------------------------------------------------------------------------
# 4. Semantic preservation — names + control flow + types
# ---------------------------------------------------------------------------


class TestSemanticPreservation:
    def test_function_name_preserved(self):
        a = "def foo(): return 42"
        b = "def bar(): return 42"
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_class_name_preserved(self):
        a = "class Foo: pass"
        b = "class Bar: pass"
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_method_name_preserved(self):
        a = "class C:\n    def foo(self): pass"
        b = "class C:\n    def bar(self): pass"
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_attribute_access_preserved(self):
        a = "x.foo"
        b = "x.bar"
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_import_module_preserved(self):
        a = "import os"
        b = "import sys"
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_from_import_name_preserved(self):
        a = "from os import path"
        b = "from os import getcwd"
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_if_else_distinct_from_no_branch(self):
        a = "def foo():\n    return 1"
        b = (
            "def foo():\n"
            "    if True:\n"
            "        return 1\n"
            "    return 2"
        )
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_for_loop_distinct(self):
        a = "def foo(items):\n    return items"
        b = (
            "def foo(items):\n"
            "    for x in items:\n"
            "        pass\n"
            "    return items"
        )
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_try_except_distinct(self):
        a = "def foo():\n    do_thing()"
        b = (
            "def foo():\n"
            "    try:\n"
            "        do_thing()\n"
            "    except Exception:\n"
            "        pass"
        )
        assert compute_ast_signature(a) != compute_ast_signature(b)

    def test_type_annotation_preserved(self):
        a = "def foo(x: int) -> str: return str(x)"
        b = "def foo(x: str) -> int: return int(x)"
        # Different annotations → different hash
        assert compute_ast_signature(a) != compute_ast_signature(b)


# ---------------------------------------------------------------------------
# 5. Defensive contract — never raises on garbage
# ---------------------------------------------------------------------------


class TestDefensive:
    def test_syntax_error_returns_empty(self):
        assert compute_ast_signature("def foo(:") == ""
        assert compute_ast_signature(":::::") == ""
        # "def foo" is a bare reference (parses as ast.Name) on some
        # Python versions; we don't pin its exact outcome — only that
        # the call NEVER raises. Either empty or a 64-char digest is
        # acceptable.
        result = compute_ast_signature("def foo")
        assert result == "" or len(result) == 64

    def test_empty_string_returns_empty(self):
        assert compute_ast_signature("") == ""
        assert compute_ast_signature("   \n\t  ") == ""

    def test_none_returns_empty(self):
        assert compute_ast_signature(None) == ""  # type: ignore[arg-type]

    def test_non_string_returns_empty(self):
        assert compute_ast_signature(42) == ""  # type: ignore[arg-type]
        assert compute_ast_signature([]) == ""  # type: ignore[arg-type]
        assert compute_ast_signature({}) == ""  # type: ignore[arg-type]

    def test_huge_source_does_not_raise(self):
        # 10K lines — should compute without raising
        big = "\n".join(f"x{i} = {i}" for i in range(10000))
        sig = compute_ast_signature(big)
        # Either valid hash or empty (defensive); never raises
        assert isinstance(sig, str)

    def test_unicode_source(self):
        a = "x = 'héllo'"
        b = "x = '世界'"
        # Both string literals; with normalize_literals=True same hash
        assert compute_ast_signature(a) == compute_ast_signature(b)


# ---------------------------------------------------------------------------
# 6. compute_multi_file_signature
# ---------------------------------------------------------------------------


class TestMultiFile:
    def test_order_stable(self):
        # Different dict insertion order → same hash
        a = {"a.py": "x = 1", "b.py": "y = 2"}
        b = {"b.py": "y = 2", "a.py": "x = 1"}
        assert compute_multi_file_signature(a) == \
            compute_multi_file_signature(b)

    def test_empty_returns_empty(self):
        assert compute_multi_file_signature({}) == ""

    def test_non_mapping_returns_empty(self):
        assert compute_multi_file_signature("not a dict") == ""  # type: ignore[arg-type]
        assert compute_multi_file_signature([]) == ""  # type: ignore[arg-type]

    def test_single_file_distinct_from_double(self):
        a = compute_multi_file_signature({"a.py": "x = 1"})
        b = compute_multi_file_signature({
            "a.py": "x = 1", "b.py": "y = 2",
        })
        assert a != b

    def test_per_file_path_matters(self):
        # Same content, different paths → different hash
        a = compute_multi_file_signature({"foo.py": "x = 1"})
        b = compute_multi_file_signature({"bar.py": "x = 1"})
        assert a != b

    def test_per_file_literal_normalization(self):
        # Same structure, different literal values → same hash
        a = compute_multi_file_signature({
            "a.py": "x = 1", "b.py": "y = 2",
        })
        b = compute_multi_file_signature({
            "a.py": "x = 99", "b.py": "y = 100",
        })
        assert a == b

    def test_syntax_error_in_one_file_does_not_break_others(self):
        # File with syntax error contributes empty hash; others
        # contribute their hashes; combined still computes
        sig = compute_multi_file_signature({
            "good.py": "x = 1", "bad.py": "def foo(:",
        })
        assert sig != ""

    def test_returns_64_char_hex(self):
        sig = compute_multi_file_signature({"a.py": "x = 1"})
        assert len(sig) == 64
        int(sig, 16)


# ---------------------------------------------------------------------------
# 7. Env knob defaults
# ---------------------------------------------------------------------------


class TestEnvKnobs:
    def test_normalize_literals_default_true(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_AST_CANONICAL_NORMALIZE_LITERALS",
            raising=False,
        )
        assert normalize_literals_default() is True

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("", True),  # default true
            ("0", False), ("false", False), ("no", False),
            ("garbage", False),
            ("1", True), ("true", True), ("YES", True),
            ("on", True),
        ],
    )
    def test_normalize_literals_env_matrix(
        self, monkeypatch, value, expected,
    ):
        monkeypatch.setenv(
            "JARVIS_AST_CANONICAL_NORMALIZE_LITERALS", value,
        )
        assert normalize_literals_default() is expected

    def test_strip_docstrings_default_false(self, monkeypatch):
        monkeypatch.delenv(
            "JARVIS_AST_CANONICAL_STRIP_DOCSTRINGS",
            raising=False,
        )
        assert strip_docstrings_default() is False

    @pytest.mark.parametrize(
        "value,expected",
        [
            ("", False),  # default false
            ("0", False), ("false", False),
            ("1", True), ("true", True),
        ],
    )
    def test_strip_docstrings_env_matrix(
        self, monkeypatch, value, expected,
    ):
        monkeypatch.setenv(
            "JARVIS_AST_CANONICAL_STRIP_DOCSTRINGS", value,
        )
        assert strip_docstrings_default() is expected


# ---------------------------------------------------------------------------
# 8. Authority invariants — AST-pinned
# ---------------------------------------------------------------------------


_FORBIDDEN_AUTHORITY_SUBSTRINGS = (
    "orchestrator",
    "phase_runners",
    "candidate_generator",
    "iron_gate",
    "change_engine",
    "policy",
    "semantic_guardian",
    "semantic_firewall",
    "providers",
    "doubleword_provider",
    "urgency_router",
    "auto_action_router",
    "subagent_scheduler",
    "tool_executor",
)


_FORBIDDEN_MUTATION_TOOL_NAMES = (
    "edit_file",
    "write_file",
    "delete_file",
    "run_tests",
    "bash",
)


def _module_path() -> Path:
    here = Path(__file__).resolve()
    cur = here
    while cur != cur.parent:
        if (cur / "CLAUDE.md").exists():
            return (
                cur / "backend" / "core" / "ouroboros"
                / "governance" / "verification"
                / "ast_canonical.py"
            )
        cur = cur.parent
    raise RuntimeError("repo root not found")


class TestAuthorityInvariants:
    def test_no_forbidden_authority_imports(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                        if fb in alias.name:
                            offenders.append(alias.name)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for fb in _FORBIDDEN_AUTHORITY_SUBSTRINGS:
                    if fb in mod:
                        offenders.append(mod)
        assert offenders == [], (
            f"ast_canonical imports forbidden modules: {offenders}"
        )

    def test_no_governance_imports(self):
        # Slice 2 is pure-stdlib; no governance modules
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                assert not mod.startswith(
                    "backend.core.ouroboros.governance",
                ), (
                    f"Slice 2 must be stdlib-only; found "
                    f"governance import: {mod}"
                )

    def test_no_mutation_tool_name_references(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        offenders = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                for fb in _FORBIDDEN_MUTATION_TOOL_NAMES:
                    if node.id == fb:
                        offenders.append(node.id)
            elif isinstance(node, ast.Attribute):
                for fb in _FORBIDDEN_MUTATION_TOOL_NAMES:
                    if node.attr == fb:
                        offenders.append(node.attr)
        assert offenders == [], (
            f"ast_canonical references mutation tool names: "
            f"{offenders}"
        )

    def test_no_async_functions(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        async_defs = [
            n.name for n in ast.walk(tree)
            if isinstance(n, ast.AsyncFunctionDef)
        ]
        assert async_defs == [], (
            f"Slice 2 must be sync; found async: {async_defs}"
        )

    def test_no_disk_writes(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        forbidden_tokens = (
            ".write_text(",
            ".write_bytes(",
            "os.replace(",
            "NamedTemporaryFile",
        )
        for tok in forbidden_tokens:
            assert tok not in source, (
                f"forbidden disk-write token: {tok!r}"
            )

    def test_does_not_execute_candidate_code(self):
        # Critical safety pin: AST canonical must NEVER exec/eval
        # candidate code. Only parse it.
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Name):
                    assert func.id not in ("exec", "eval", "compile"), (
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"forbidden code-execution call: {func.id}"
                    )
                elif isinstance(func, ast.Attribute):
                    assert func.attr not in ("exec", "eval"), (
                        f"line {getattr(node, 'lineno', '?')}: "
                        f"forbidden code-execution attribute call: "
                        f"{func.attr}"
                    )

    def test_public_api_exported(self):
        expected = {
            "AST_CANONICAL_SCHEMA_VERSION",
            "compute_ast_signature",
            "compute_multi_file_signature",
            "normalize_literals_default",
            "strip_docstrings_default",
        }
        assert set(canon_mod.__all__) == expected

    def test_module_is_pure_stdlib(self):
        path = _module_path()
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.append(node.module)
        stdlib_top = {
            "ast", "hashlib", "logging", "os", "typing",
            "__future__",
        }
        for name in imports:
            top_level = name.split(".")[0]
            assert top_level in stdlib_top, (
                f"unexpected non-stdlib import: {name}"
            )

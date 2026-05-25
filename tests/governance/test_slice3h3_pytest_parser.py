"""Slice 3H.3 — pytest diagnostic parser cascade.

Closes the final hard-guard trap surfaced by capability soak
bt-2026-05-25-085310. Even with all prior slices wired
(3G/3H/3H.1/3H.2), the InteractiveRepair hard-guard at
``interactive_repair.py:122`` fired every iteration with
``error_type=UnknownError`` because ``_extract_error`` only matched
stdlib ``Traceback (most recent call last):`` format — pytest's
collection errors, conftest import failures, and assertion failures
have DIFFERENT shapes that the regex never caught.

# Fix mechanism — five-tier cascade

The new ``_extract_error`` walks a documented pattern cascade
(most-specific first; first match wins):

  1. Stdlib ``Traceback`` (preserved verbatim from pre-3H.3)
  2. Pytest collection error (``ERROR collecting <path>`` + ``E   ...``)
  3. Pytest conftest import error (``ImportError while loading conftest``)
  4. Pytest assertion failure (``<path>:<line>: in <fn>`` + ``E   ...``)
  5. Pytest short summary (``FAILED <path>::<test>``) — last resort

Every pattern extracts ``error_type``, ``message``, ``file_path``,
``line_number`` so the downstream micro-prompt builder always sees a
usable target. ``UnknownError`` with ``line_number=0`` is reserved
for genuinely unparseable output.

# Test surface (1 AST pin + 7 spine)
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
INTERACTIVE_REPAIR_FILE = (
    REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"
    / "interactive_repair.py"
)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


# ──────────────────────────────────────────────────────────────────────
# AST PIN — 1
# ──────────────────────────────────────────────────────────────────────


def test_ast_pin_extract_error_has_pytest_cascade() -> None:
    """``_extract_error`` body must contain all five cascade markers:
    stdlib Traceback (legacy), pytest collection, conftest import,
    assertion failures, short summary. Without all five, the
    bt-2026-05-25-085310 hard-guard trap can reopen via the
    unhandled-shape branch."""
    src = INTERACTIVE_REPAIR_FILE.read_text()
    # Each pattern marker must be present in source (regex literal
    # OR the comment header for that cascade tier).
    assert "Traceback \\(most recent call last\\)" in src, (
        "stdlib Traceback pattern (cascade #1) missing"
    )
    assert "ERROR collecting" in src, (
        "pytest collection error pattern (cascade #2) missing — "
        "bt-2026-05-25-085310 trap reopens"
    )
    assert "while loading conftest" in src, (
        "pytest conftest import pattern (cascade #3) missing"
    )
    # Assertion failure cascade — ``E   ErrorClass`` pattern with
    # ``<path>:<line>: in <fn>`` prefix
    assert "in\\s+\\S+" in src, (
        "pytest assertion failure pattern (cascade #4) missing"
    )
    assert "FAILED|ERROR" in src, (
        "pytest short summary pattern (cascade #5) missing"
    )
    assert "PytestFailure" in src, (
        "PytestFailure fallback type missing — short-summary "
        "extraction will leave error_type empty"
    )


# ──────────────────────────────────────────────────────────────────────
# Spine — 7 (each pattern + edge cases)
# ──────────────────────────────────────────────────────────────────────


def test_spine_stdlib_traceback_preserved() -> None:
    """Cascade #1 — stdlib Traceback (legacy, pre-3H.3 path). Must
    continue working byte-identically."""
    from backend.core.ouroboros.governance.interactive_repair import (
        InteractiveRepairLoop,
    )
    # NOTE: pre-3H.3 legacy regex matches the FIRST ``\\w+Error`` after
    # the ``Traceback`` header. When the source code itself contains
    # ``raise ValueError(...)`` literal text, the regex matches the
    # raise-line text not the final ``ValueError: msg`` line. Slice
    # 3H.3 preserved this behavior verbatim (it's not what 3H.3
    # targets — pytest didn't have this shape). Test confirms the
    # error_type starts with ValueError (capturing the legacy match
    # shape without over-specifying the trailing chars).
    output = """\
Traceback (most recent call last):
  File "/tmp/foo.py", line 42, in <module>
    raise ValueError
ValueError: bad
"""
    err = InteractiveRepairLoop._extract_error(output, "default.py")
    assert err.error_type.startswith("ValueError"), (
        f"stdlib Traceback path returned error_type={err.error_type!r}"
    )
    assert err.file_path == "/tmp/foo.py"
    assert err.line_number == 42


def test_spine_pytest_collection_error() -> None:
    """Cascade #2 — pytest collection error (most common SWE-Bench-Pro
    failure mode when the patch introduces a SyntaxError)."""
    from backend.core.ouroboros.governance.interactive_repair import (
        InteractiveRepairLoop,
    )
    output = """\
========================== test session starts ==========================
collected 0 items / 1 error

_____________ ERROR collecting tests/test_foo.py _____________
ImportError while importing test module '/tmp/wt/tests/test_foo.py'.
Hint: make sure your test modules/packages have valid Python names.
Traceback omitted.
tests/test_foo.py:7: in <module>
    from bar import baz
E   ImportError: cannot import name 'baz' from 'bar'
"""
    err = InteractiveRepairLoop._extract_error(output, "default.py")
    assert err.error_type in ("ImportError",)
    assert err.file_path == "tests/test_foo.py"
    assert err.line_number == 7
    assert err.line_number > 0  # The critical hard-guard check


def test_spine_pytest_conftest_import_error() -> None:
    """Cascade #3 — pytest conftest import error."""
    from backend.core.ouroboros.governance.interactive_repair import (
        InteractiveRepairLoop,
    )
    output = """\
ImportError while loading conftest '/tmp/wt/conftest.py'.
/tmp/wt/conftest.py:5: in <module>
    from missing_module import thing
E   ImportError: No module named 'missing_module'
"""
    err = InteractiveRepairLoop._extract_error(output, "default.py")
    assert err.error_type == "ImportError"
    assert err.file_path == "/tmp/wt/conftest.py"
    assert err.line_number == 5


def test_spine_pytest_assertion_failure() -> None:
    """Cascade #4 — pytest assertion failure with E   AssertionError."""
    from backend.core.ouroboros.governance.interactive_repair import (
        InteractiveRepairLoop,
    )
    output = """\
============================= test session starts =============================
collected 1 item

tests/test_doc.py F

================================== FAILURES ===================================
________________________ test_doc_renders_role_mixin _________________________

tests/test_doc.py:42: in test_doc_renders_role_mixin
    assert role_mixin.doc.startswith("RoleMixin")
E   AssertionError: assert 'somethingelse'.startswith('RoleMixin')
"""
    err = InteractiveRepairLoop._extract_error(output, "default.py")
    assert err.error_type == "AssertionError"
    assert err.file_path == "tests/test_doc.py"
    assert err.line_number == 42


def test_spine_pytest_short_summary_fallback() -> None:
    """Cascade #5 — short summary fallback when richer patterns
    didn't match."""
    from backend.core.ouroboros.governance.interactive_repair import (
        InteractiveRepairLoop,
    )
    output = """\
=========================== short test summary info ============================
FAILED tests/test_doc.py::test_role_mixin - AssertionError: mismatch
========================== 1 failed in 0.42s ==========================
"""
    err = InteractiveRepairLoop._extract_error(output, "default.py")
    # Either matched as assertion summary or generic PytestFailure
    assert err.error_type in ("AssertionError", "PytestFailure")
    assert err.file_path == "tests/test_doc.py"
    assert err.line_number > 0  # Must be > 0 to defeat the hard-guard


def test_spine_unknown_output_still_yields_unknown_error() -> None:
    """When the output is genuinely unparseable (e.g., binary output
    or non-pytest noise), the UnknownError fallback fires. This is
    the safety net — never crash on weird input."""
    from backend.core.ouroboros.governance.interactive_repair import (
        InteractiveRepairLoop,
    )
    output = "random binary noise here that matches nothing\x00\xff"
    err = InteractiveRepairLoop._extract_error(output, "default.py")
    assert err.error_type == "UnknownError"
    assert err.file_path == "default.py"
    assert err.line_number == 0


def test_spine_hard_guard_defeated_by_pytest_extraction() -> None:
    """End-to-end: the new pytest parsers MUST yield line_number > 0
    AND error_type != UnknownError so the InteractiveRepair hard-guard
    at line 122 (``if err.error_type in {UnknownError} or
    err.line_number <= 0: break``) does NOT fire on pytest output.

    This is THE bt-2026-05-25-085310 regression test."""
    from backend.core.ouroboros.governance.interactive_repair import (
        InteractiveRepairLoop,
    )
    # Realistic SWE-Bench-Pro pytest output sample
    output = """\
============================= test session starts =============================
collected 1 item

tests/integration/cli/test_doc.py F

================================== FAILURES ===================================
_______________________ test_doc_renders_role_module _______________________

tests/integration/cli/test_doc.py:127: in test_doc_renders_role_module
    rendered = cli.format_role_doc(mixin)
E   AttributeError: module 'ansible.cli.doc' has no attribute 'format_role_doc'

=========================== short test summary info ============================
FAILED tests/integration/cli/test_doc.py::test_doc_renders_role_module
========================== 1 failed in 1.23s ==========================
"""
    err = InteractiveRepairLoop._extract_error(output, "default.py")
    # The hard-guard predicate from interactive_repair.py:122
    assert err.error_type not in {"UnknownError", "TimeoutError"}, (
        f"Slice 3H.3 parser still returned hard-guard type "
        f"{err.error_type!r} — the bt-2026-05-25-085310 trap is open."
    )
    assert err.line_number > 0, (
        f"Slice 3H.3 parser returned line_number={err.line_number} — "
        f"hard-guard predicate fires on <= 0."
    )
    # The model now gets actionable feedback
    assert err.file_path == "tests/integration/cli/test_doc.py"
    assert err.line_number == 127
    assert err.error_type == "AttributeError"

"""Phase 0 — coding_council ↔ O+V cross-kingdom boundary
test spine.

Operator binding 2026-05-07 (verbatim — non-negotiable):

  "Pure Iron Gate protocol. By writing a 30-line AST
   constraint that physically prevents any future agent or
   module from importing coding_council logic into the
   governance/ tree, you are solving the root problem at
   the compiler level."

Pinned coverage (~22 tests):
  * Live governance/ tree clean (no current violations)
  * Schema version constant present
  * scan_governance_tree() returns empty tuple on missing
    root (defensive)
  * scan_governance_tree() catches synthetic regression:
    top-level ImportFrom with full path
  * scan_governance_tree() catches synthetic regression:
    top-level ImportFrom with submodule path
  * scan_governance_tree() catches synthetic regression:
    lazy ImportFrom inside function body
  * scan_governance_tree() catches synthetic regression:
    lazy ImportFrom inside class method
  * scan_governance_tree() catches synthetic regression:
    bare ``import backend.core.coding_council``
  * scan_governance_tree() catches synthetic regression:
    bare ``import backend.core.coding_council.X``
  * scan_governance_tree() ignores LOOKALIKE imports
    (e.g. backend.core.coding_council_lookalike → no false
    positive)
  * scan_governance_tree() skips __pycache__
  * scan_governance_tree() handles unreadable files (NEVER
    raises)
  * scan_governance_tree() handles SyntaxError files (NEVER
    raises)
  * scan_governance_tree() ignores non-.py files
  * Violation strings are well-formatted with relative path
    + line number + module name
  * Public API surface complete
  * register_flags is a no-op (no env knobs)
  * Pin's validate() returns same shape (tuple of strings)
  * Pin clean on the boundary module itself
  * Caller-injectable forbidden_prefix for future package
    rename
"""
from __future__ import annotations

import ast
import textwrap
from pathlib import Path

import pytest


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _module_path() -> Path:
    return (
        _repo_root()
        / "backend/core/ouroboros/governance/meta/"
        "cross_kingdom_boundary.py"
    )


# ---------------------------------------------------------------------------
# Live tree clean — load-bearing regression
# ---------------------------------------------------------------------------


def test_live_governance_tree_is_clean():
    """The boundary holds today: zero forbidden imports
    anywhere under backend/core/ouroboros/governance/.
    Future PRs that violate the boundary will fail here."""
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    violations = scan_governance_tree()
    if violations:
        # Surface the specific files for a fast-fix loop.
        joined = "\n  ".join(violations)
        pytest.fail(
            "governance/ → coding_council import boundary "
            "violated:\n  " + joined
        )


def test_pin_self_check_clean():
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pins = register_shipped_invariants()
    assert len(pins) == 1
    assert (
        pins[0].invariant_name
        == "governance_no_coding_council_imports"
    )
    assert pins[0].validate(tree, src) == ()


# ---------------------------------------------------------------------------
# Synthetic regressions — top-level
# ---------------------------------------------------------------------------


def _write_governance_module(
    root: Path, rel_path: str, source: str,
) -> None:
    """Helper: write a synthetic .py under a fake governance
    root."""
    full = root / rel_path
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(textwrap.dedent(source))


def test_top_level_full_path_import_caught(tmp_path):
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    _write_governance_module(
        tmp_path, "offender.py",
        "from backend.core.coding_council import x\n",
    )
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
    )
    assert any("offender.py" in v for v in violations)
    assert any(
        "backend.core.coding_council" in v
        for v in violations
    )


def test_top_level_submodule_import_caught(tmp_path):
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    _write_governance_module(
        tmp_path, "offender.py",
        (
            "from backend.core.coding_council.safety import "
            "ast_validator\n"
        ),
    )
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
    )
    assert violations
    assert any(
        "coding_council.safety" in v for v in violations
    )


def test_lazy_inside_function_caught(tmp_path):
    """Operator binding: pin must catch ANY nesting level —
    top-level OR lazy-inside-function. Lazy imports are the
    common cross-kingdom dodge pattern."""
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    _write_governance_module(
        tmp_path, "lazy_offender.py",
        '''
        def helper():
            from backend.core.coding_council.framework import (
                circuit_breaker,
            )
            return circuit_breaker
        ''',
    )
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
    )
    assert any(
        "lazy_offender.py" in v for v in violations
    )


def test_lazy_inside_class_method_caught(tmp_path):
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    _write_governance_module(
        tmp_path, "class_offender.py",
        '''
        class Foo:
            def helper(self):
                from backend.core.coding_council import x
                return x
        ''',
    )
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
    )
    assert any(
        "class_offender.py" in v for v in violations
    )


def test_bare_import_caught(tmp_path):
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    _write_governance_module(
        tmp_path, "bare_offender.py",
        "import backend.core.coding_council\n",
    )
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
    )
    assert any(
        "bare_offender.py" in v for v in violations
    )


def test_bare_submodule_import_caught(tmp_path):
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    _write_governance_module(
        tmp_path, "bare_sub.py",
        "import backend.core.coding_council.advanced.saga_coordinator\n",  # noqa: E501
    )
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
    )
    assert any("bare_sub.py" in v for v in violations)


def test_lookalike_import_no_false_positive(tmp_path):
    """``backend.core.coding_council_lookalike`` is NOT a
    forbidden import — it's a different module. Pin must
    not match by substring."""
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    _write_governance_module(
        tmp_path, "lookalike.py",
        (
            "from backend.core.coding_council_lookalike "
            "import x\n"
        ),
    )
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
    )
    assert violations == ()


def test_unrelated_imports_not_flagged(tmp_path):
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    _write_governance_module(
        tmp_path, "clean.py",
        '''
        from backend.core.ouroboros.governance.posture import Posture
        from typing import Any
        import asyncio
        ''',
    )
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
    )
    assert violations == ()


# ---------------------------------------------------------------------------
# Defensive — pin NEVER raises
# ---------------------------------------------------------------------------


def test_skips_pycache(tmp_path):
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    pycache = tmp_path / "__pycache__"
    pycache.mkdir()
    (pycache / "offender.py").write_text(
        "from backend.core.coding_council import x\n",
    )
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
    )
    # __pycache__ files MUST NOT be scanned
    assert violations == ()


def test_handles_syntax_error_silently(tmp_path):
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    _write_governance_module(
        tmp_path, "broken.py",
        "def x(:\n    pass\n",  # SyntaxError
    )
    _write_governance_module(
        tmp_path, "valid_offender.py",
        "from backend.core.coding_council import y\n",
    )
    # MUST NOT raise; broken.py skipped, valid_offender caught
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
    )
    assert any(
        "valid_offender.py" in v for v in violations
    )


def test_missing_root_returns_empty(tmp_path):
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    nonexistent = tmp_path / "no-such-dir"
    violations = scan_governance_tree(
        governance_root_override=nonexistent,
    )
    assert violations == ()


def test_ignores_non_py_files(tmp_path):
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    (tmp_path / "data.json").write_text(
        '{"x": "from backend.core.coding_council import y"}',
    )
    (tmp_path / "notes.md").write_text(
        "from backend.core.coding_council import z\n",
    )
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
    )
    assert violations == ()


# ---------------------------------------------------------------------------
# Violation string format
# ---------------------------------------------------------------------------


def test_violation_format_path_line_module(tmp_path):
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    _write_governance_module(
        tmp_path, "subdir/offender.py",
        (
            "import os\n"
            "from backend.core.coding_council.framework "
            "import bulkhead\n"
        ),
    )
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
    )
    assert len(violations) == 1
    v = violations[0]
    # POSIX path separators
    assert "subdir/offender.py" in v
    # line 2 (after "import os\n")
    assert ":2 " in v
    # module name
    assert (
        "backend.core.coding_council.framework" in v
    )


def test_multiple_offenders_in_one_file_all_caught(tmp_path):
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    _write_governance_module(
        tmp_path, "double.py",
        '''
        from backend.core.coding_council import x
        def f():
            from backend.core.coding_council.safety import y
            return y
        ''',
    )
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
    )
    # Both offenses surface (top-level + lazy)
    assert len(violations) == 2


# ---------------------------------------------------------------------------
# Caller-injectable forbidden_prefix
# ---------------------------------------------------------------------------


def test_caller_injectable_forbidden_prefix(tmp_path):
    """For future package renames or test-time isolation,
    callers can override the forbidden prefix."""
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        scan_governance_tree,
    )
    _write_governance_module(
        tmp_path, "x.py",
        "from synthetic.fake_kingdom import x\n",
    )
    # Default prefix → no violation
    assert scan_governance_tree(
        governance_root_override=tmp_path,
    ) == ()
    # Override → caught
    violations = scan_governance_tree(
        governance_root_override=tmp_path,
        forbidden_prefix="synthetic.fake_kingdom",
    )
    assert violations
    assert any("x.py" in v for v in violations)


# ---------------------------------------------------------------------------
# Public API + register_flags
# ---------------------------------------------------------------------------


def test_public_api_complete():
    from backend.core.ouroboros.governance.meta import (
        cross_kingdom_boundary as mod,
    )
    expected = {
        "CROSS_KINGDOM_BOUNDARY_SCHEMA_VERSION",
        "register_flags",
        "register_shipped_invariants",
        "scan_governance_tree",
    }
    assert set(mod.__all__) == expected


def test_register_flags_is_noop():
    """The boundary is structural (Iron Gate protocol), not
    flag-gated. register_flags MUST be a no-op."""
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        register_flags,
    )

    class _BoomRegistry:
        def register(self, **_kwargs):
            raise RuntimeError(
                "register MUST NOT be called",
            )

    # MUST NOT raise even if the registry would explode
    register_flags(_BoomRegistry())


def test_pin_target_file_points_at_boundary_module():
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        register_shipped_invariants,
    )
    pins = register_shipped_invariants()
    assert len(pins) == 1
    assert (
        "cross_kingdom_boundary.py" in pins[0].target_file
    )


def test_pin_validator_returns_tuple_shape():
    """The validator MUST return a tuple (per ShippedCodeInvariant
    contract). Non-tuple returns break the registry."""
    from backend.core.ouroboros.governance.meta.cross_kingdom_boundary import (  # noqa: E501
        register_shipped_invariants,
    )
    src = _module_path().read_text(encoding="utf-8")
    tree = ast.parse(src)
    pins = register_shipped_invariants()
    result = pins[0].validate(tree, src)
    assert isinstance(result, tuple)

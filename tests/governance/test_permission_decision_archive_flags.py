"""Regression spine for Venom V2 Slice 5 — FlagRegistry seed.

Pins the load-bearing structural invariants for the two
``JARVIS_PERMISSION_ARCHIVE_*`` flags:

* ``register_flags`` is auto-discovered by
  ``flag_registry_seed._discover_module_provided_flags`` via the
  ``backend.core.ouroboros.governance`` provider package walk —
  zero edits to ``flag_registry_seed.SEED_SPECS``.
* The two FlagSpecs have the canonical shapes the registry
  contract demands (correct FlagType, default values, Category
  slots, source_file pointer to the substrate).
* The master flag default is FALSE (§33.1 graduation contract —
  drift here would silently graduate the surface without an
  evidence ladder).
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import Iterator

import pytest

from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagType,
    ensure_seeded,
    reset_default_registry,
)
from backend.core.ouroboros.governance.permission_decision_archive import (
    ARCHIVE_SIZE_ENV_VAR,
    MASTER_FLAG_ENV_VAR,
    register_flags,
)


_MODULE_SRC = Path(
    inspect.getfile(register_flags),
).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _isolate_registry() -> Iterator[None]:
    """Each test starts with a fresh default registry. Reset on
    exit so the next test's auto-discovery sees a clean slate."""
    reset_default_registry()
    yield
    reset_default_registry()


# ---------------------------------------------------------------------------
# Direct registration — substrate-owned register_flags
# ---------------------------------------------------------------------------


def test_register_flags_installs_two_specs():
    """The substrate's ``register_flags`` MUST install exactly
    the two Venom V2 flags. Drift here (e.g. silently dropping
    one) is operator-visible via this count assertion."""
    registry = FlagRegistry()
    count = register_flags(registry)
    assert count == 2, f"expected 2 specs, got {count}"


def test_master_flag_spec_shape():
    """The master flag MUST be BOOL / SAFETY / default-FALSE.
    Drift here is the §33.1 graduation-contract violation."""
    registry = FlagRegistry()
    register_flags(registry)
    spec = registry.get_spec(MASTER_FLAG_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.BOOL
    assert spec.default is False, (
        f"{MASTER_FLAG_ENV_VAR} MUST default FALSE per §33.1 "
        "graduation contract — drift would silently graduate "
        "the surface without an evidence ladder"
    )
    assert spec.category == Category.SAFETY, (
        "Master kill switch belongs in the SAFETY category"
    )
    assert "permission_decision_archive" in spec.source_file


def test_size_flag_spec_shape():
    """The size flag MUST be INT / CAPACITY / default 50."""
    registry = FlagRegistry()
    register_flags(registry)
    spec = registry.get_spec(ARCHIVE_SIZE_ENV_VAR)
    assert spec is not None
    assert spec.type == FlagType.INT
    assert spec.default == 50
    assert spec.category == Category.CAPACITY, (
        "Capacity tuning belongs in the CAPACITY category"
    )
    assert "permission_decision_archive" in spec.source_file


def test_register_flags_idempotent():
    """Re-registering on the same registry MUST be a no-op
    (override-in-place); count stays 2. Mirrors the
    ``override=True`` default contract from FlagRegistry.register."""
    registry = FlagRegistry()
    register_flags(registry)
    register_flags(registry)  # second call — override-in-place
    register_flags(registry)  # third call
    specs = registry.list_all()
    matching = [
        s for s in specs
        if s.name in (MASTER_FLAG_ENV_VAR, ARCHIVE_SIZE_ENV_VAR)
    ]
    assert len(matching) == 2, (
        f"Idempotent re-registration broken — got "
        f"{len(matching)} matching specs"
    )


def test_register_flags_never_raises_on_malformed_registry():
    """The substrate contract is fail-open — graduation soak
    paths swallow our return value. We verify by passing a
    deliberately-broken registry shim that raises on .register."""

    class _BrokenRegistry:
        def register(self, _spec):
            raise RuntimeError("registry exploded")

    # Should not raise — defensive try/except per the seed
    # pattern (mirrors tool_render_view.register_flags discipline).
    count = register_flags(_BrokenRegistry())
    assert count == 0, (
        "fail-open contract: when every register() raises, count "
        "is 0 but the function MUST NOT propagate"
    )


# ---------------------------------------------------------------------------
# Auto-discovery via canonical seed walker
# ---------------------------------------------------------------------------


def test_auto_discovery_picks_up_both_specs():
    """``ensure_seeded()`` walks the canonical
    ``_FLAG_PROVIDER_PACKAGES`` for ``register_flags`` callables.
    The Venom V2 substrate MUST be discovered zero-edit (no
    additions to ``flag_registry_seed.SEED_SPECS`` required)."""
    registry = ensure_seeded()
    master = registry.get_spec(MASTER_FLAG_ENV_VAR)
    size = registry.get_spec(ARCHIVE_SIZE_ENV_VAR)
    assert master is not None, (
        f"{MASTER_FLAG_ENV_VAR} MUST be auto-discovered via the "
        "canonical seed walker — drift here means the §33.3 "
        "naming-cage discipline for flags regressed"
    )
    assert size is not None, (
        f"{ARCHIVE_SIZE_ENV_VAR} MUST be auto-discovered"
    )


# ---------------------------------------------------------------------------
# Authority asymmetry + structural invariants — AST pins
# ---------------------------------------------------------------------------


def test_ast_pin_register_flags_function_present():
    """The auto-discovery contract REQUIRES a module-level
    ``register_flags(registry) -> int`` callable. Drift here
    breaks the naming-cage discovery silently."""
    tree = ast.parse(_MODULE_SRC)
    found = any(
        isinstance(n, ast.FunctionDef)
        and n.name == "register_flags"
        for n in tree.body
    )
    assert found, (
        "Module-level register_flags(registry) is the load-bearing "
        "naming-cage hook for FlagRegistry auto-discovery — "
        "drift breaks the §33.3 discipline"
    )


def test_ast_pin_register_flags_uses_canonical_envvar_constants():
    """The ``register_flags`` body MUST use the canonical
    ``MASTER_FLAG_ENV_VAR`` + ``ARCHIVE_SIZE_ENV_VAR`` constants
    (NOT raw string literals). Drift here breaks the single-
    source-of-truth invariant — a future rename would silently
    desync the env-var name from the flag spec."""
    tree = ast.parse(_MODULE_SRC)
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "register_flags"
        ),
        None,
    )
    assert fn is not None
    src = ast.get_source_segment(_MODULE_SRC, fn)
    assert src is not None
    assert "name=MASTER_FLAG_ENV_VAR" in src, (
        "register_flags MUST reference the canonical "
        "MASTER_FLAG_ENV_VAR constant — single-source-of-truth"
    )
    assert "name=ARCHIVE_SIZE_ENV_VAR" in src, (
        "register_flags MUST reference the canonical "
        "ARCHIVE_SIZE_ENV_VAR constant — single-source-of-truth"
    )


def test_ast_pin_register_flags_never_raises():
    """The ``register_flags`` body MUST wrap each ``register()``
    call in a try/except (fail-open contract). Drift here means
    a single bad FlagSpec construction could block the entire
    ensure_seeded() walk."""
    tree = ast.parse(_MODULE_SRC)
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "register_flags"
        ),
        None,
    )
    assert fn is not None
    src = ast.get_source_segment(_MODULE_SRC, fn)
    assert src is not None
    assert "try:" in src, (
        "register_flags MUST contain try/except per fail-open "
        "contract — graduation soak paths swallow exceptions"
    )
    assert "except" in src, (
        "register_flags MUST handle exceptions — drift would "
        "let a single bad FlagSpec block ensure_seeded()"
    )


def test_ast_pin_default_false_in_source_for_master():
    """Bytes-pin: ``default=False`` MUST appear in the master
    flag's FlagSpec construction. Drift to ``default=True``
    silently graduates the surface — caught here."""
    tree = ast.parse(_MODULE_SRC)
    fn = next(
        (
            n for n in ast.walk(tree)
            if isinstance(n, ast.FunctionDef)
            and n.name == "register_flags"
        ),
        None,
    )
    assert fn is not None
    src = ast.get_source_segment(_MODULE_SRC, fn)
    assert src is not None
    # The master spec MUST have default=False.
    assert "default=False" in src, (
        "Master flag default MUST be False per §33.1 graduation "
        "contract — bytes-pinned in source"
    )

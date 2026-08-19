"""`is_ready` is a method on one oracle and a property on another.

CAUGHT BY A LIVE SOAK, not by review. `context_expander.py:117` spelled it as
a method::

    if self._oracle is None or not self._oracle.is_ready():
    TypeError: 'bool' object is not callable

Production injects an `InProcessOracleAdapter`, whose `is_ready` is a
`@property`. So EVERY operation raised there, the CONTEXT_EXPANSION runner's
`except Exception` swallowed it, and generation proceeded with UNEXPANDED
context — silently, for as long as the adapter has existed. The only visible
symptom was one warning naming a type and no site, which is why the earlier
fix in this session was to make that handler carry `exc_info`; the traceback
it produced is what located this.

A third site had the same split in a third spelling: `native_integration`
`await`ed the result, and no `is_ready` in this codebase is async.

NEITHER SURFACE IS CHANGED. Both spellings are pinned by tests on their own
type — `test_oracle_deferred_boot.py:148` asserts `o.is_ready() is False`,
`test_slice112_oracle_ipc.py` asserts `proxy.is_ready is False` — so unifying
the protocol would break whichever side lost. The split is real and per-type.
What was missing is a reader that survives it.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from backend.core.ouroboros.oracle_adapter import oracle_is_ready


def _root() -> Path:
    return Path(__file__).resolve().parents[2]


class TestTotality:
    def test_none_is_not_ready(self):
        """No oracle is not a ready oracle."""
        assert oracle_is_ready(None) is False

    def test_method_surface_theoracle_shape(self):
        class _M:
            def is_ready(self): return True
        class _MF:
            def is_ready(self): return False
        assert oracle_is_ready(_M()) is True
        assert oracle_is_ready(_MF()) is False

    def test_property_surface_adapter_shape(self):
        """The shape that produced the production TypeError."""
        class _P:
            @property
            def is_ready(self): return True
        class _PF:
            @property
            def is_ready(self): return False
        assert oracle_is_ready(_P()) is True
        assert oracle_is_ready(_PF()) is False

    def test_absent_attribute_assumes_ready(self):
        """Mirrors the adapters' own documented fallback.

        Treating a probe-less legacy oracle as permanently unready would
        silently disable context expansion — the outcome this resolver exists
        to end."""
        class _Bare: pass
        assert oracle_is_ready(_Bare()) is True

    def test_a_raising_probe_is_not_a_verdict(self):
        class _Boom:
            @property
            def is_ready(self): raise RuntimeError("probe exploded")
        assert oracle_is_ready(_Boom()) is True

    def test_a_raising_method_is_not_a_verdict(self):
        class _Boom:
            def is_ready(self): raise RuntimeError("probe exploded")
        assert oracle_is_ready(_Boom()) is True

    def test_a_coroutine_is_closed_not_leaked(self):
        """No `is_ready` here is async, so an awaitable is UNKNOWN — and must
        not leave a 'coroutine was never awaited' warning behind."""
        class _Async:
            async def is_ready(self): return False
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("error", RuntimeWarning)
            assert oracle_is_ready(_Async()) is True

    def test_non_bool_values_are_coerced(self):
        class _Str:
            is_ready = "yes"
        class _Empty:
            is_ready = ""
        assert oracle_is_ready(_Str()) is True
        assert oracle_is_ready(_Empty()) is False

    def test_a_hostile_getattr_never_raises(self):
        class _Hostile:
            def __getattr__(self, name): raise RuntimeError("no attributes here")
        assert oracle_is_ready(_Hostile()) is True


class TestRealSurfacesBothResolve:
    """The two shipped implementations, through one reader."""

    def test_in_process_adapter_property_resolves(self):
        from backend.core.ouroboros.oracle_adapter import InProcessOracleAdapter
        assert isinstance(
            inspect.getattr_static(InProcessOracleAdapter, "is_ready"), property
        ), "adapter surface changed — this test's premise is stale"

    def test_theoracle_method_surface_is_unchanged(self):
        """Pinned so a future 'unification' cannot silently break
        `test_oracle_deferred_boot`, which asserts `o.is_ready() is False`."""
        src = (_root() / "backend/core/ouroboros/oracle.py").read_text(
            encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if (isinstance(node, ast.FunctionDef) and node.name == "is_ready"
                    and node.lineno > 4600):
                decs = [getattr(d, "id", getattr(d, "attr", "")) for d in node.decorator_list]
                assert "property" not in decs, (
                    "TheOracle.is_ready became a property — "
                    "test_oracle_deferred_boot asserts it is callable"
                )
                return
        pytest.skip("TheOracle.is_ready not found at the expected location")


class TestConsumersUseTheReader:
    """Wiring pins. A resolver nobody calls is the defect it replaced."""

    @pytest.mark.parametrize("rel", [
        "backend/core/ouroboros/governance/context_expander.py",
        "backend/core/ouroboros/native_integration.py",
    ])
    def test_consumer_uses_the_resolver(self, rel):
        src = (_root() / rel).read_text(encoding="utf-8")
        assert "_oracle_is_ready(" in src, f"{rel} must use the shared reader"

    @pytest.mark.parametrize("rel", [
        "backend/core/ouroboros/governance/context_expander.py",
        "backend/core/ouroboros/native_integration.py",
    ])
    def test_no_consumer_calls_is_ready_directly(self, rel):
        """AST, not substring — the comments here legitimately mention the
        old spelling, and a substring check cannot tell code from prose about
        code."""
        tree = ast.parse((_root() / rel).read_text(encoding="utf-8"))
        offenders = [
            getattr(n, "lineno", "?") for n in ast.walk(tree)
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "is_ready"
        ]
        assert not offenders, (
            f"{rel} calls .is_ready() directly at {offenders}; it is a "
            "@property on the adapters and will raise TypeError"
        )

    def test_native_integration_no_longer_awaits_a_bool(self):
        src = (_root() / "backend/core/ouroboros/native_integration.py").read_text(
            encoding="utf-8")
        assert "await self._oracle.is_ready()" not in src

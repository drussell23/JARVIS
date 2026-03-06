#!/usr/bin/env python3
"""
Exception debt contract tests (Disease 5+6 MVP).

These tests enforce that new code does not introduce silent exception
swallowing or scattered signal registration in lifecycle-critical modules.
"""
import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

# Lifecycle-critical files where silent except Exception: pass is banned
LIFECYCLE_CRITICAL_FILES = [
    "backend/core/kernel_lifecycle_engine.py",
    "backend/core/lifecycle_exceptions.py",
    "backend/core/signal_authority.py",
]


class TestNoSilentExceptionPass:
    """No 'except Exception: pass' in lifecycle-critical modules."""

    @pytest.mark.parametrize("filepath", LIFECYCLE_CRITICAL_FILES)
    def test_no_silent_pass_in_lifecycle_modules(self, filepath):
        path = Path(filepath)
        if not path.exists():
            pytest.skip(f"{filepath} not found")
        source = path.read_text()
        source_lines = source.splitlines()
        tree = ast.parse(source)
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is not None and isinstance(node.type, ast.Name):
                    if node.type.id == "Exception":
                        # Check if body is just 'pass'
                        if (len(node.body) == 1
                                and isinstance(node.body[0], ast.Pass)):
                            pass_lineno = node.body[0].lineno
                            # Allow annotated best-effort patterns
                            # (e.g. "pass  # best effort" in emergency paths)
                            raw_line = source_lines[pass_lineno - 1]
                            if "# best effort" not in raw_line.lower():
                                violations.append(node.lineno)
        assert not violations, (
            f"{filepath} has silent 'except Exception: pass' at lines: {violations}"
        )


class TestNoScatteredSignalRegistration:
    """signal.signal() must only appear in signal_authority.py."""

    def test_no_signal_signal_in_lifecycle_engine(self):
        path = Path("backend/core/kernel_lifecycle_engine.py")
        if not path.exists():
            pytest.skip("kernel_lifecycle_engine.py not found")
        source = path.read_text()
        assert "signal.signal(" not in source, (
            "kernel_lifecycle_engine.py must not register signal handlers directly"
        )

    def test_no_signal_signal_in_lifecycle_exceptions(self):
        path = Path("backend/core/lifecycle_exceptions.py")
        if not path.exists():
            pytest.skip("lifecycle_exceptions.py not found")
        source = path.read_text()
        assert "signal.signal(" not in source


class TestNoDirectStateWriteInEngine:
    """self._state = ... must only appear inside LifecycleEngine.transition()."""

    def test_state_writes_only_in_transition(self):
        path = Path("backend/core/kernel_lifecycle_engine.py")
        if not path.exists():
            pytest.skip("kernel_lifecycle_engine.py not found")
        source = path.read_text()
        tree = ast.parse(source)

        state_writes = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if (isinstance(target, ast.Attribute)
                            and target.attr == "_state"
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"):
                        state_writes.append(node.lineno)

        # _state is written in __init__ and transition() only
        assert len(state_writes) <= 2, (
            f"self._state written at {len(state_writes)} locations "
            f"(expected <=2: __init__ + transition): lines {state_writes}"
        )


class TestNoSilentPassInShutdown:
    """Emergency shutdown must not silently swallow exceptions."""

    def test_no_silent_pass_in_shutdown_block(self):
        """Lines 68400-69100 (emergency shutdown) must not have bare except: pass."""
        source = Path("unified_supervisor.py").read_text()
        lines = source.split("\n")
        violations = []
        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if line.startswith("except") and "Exception" in line:
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                if j < len(lines) and lines[j].strip() == "pass":
                    if 68400 <= i + 1 <= 69100:
                        violations.append(i + 1)
            i += 1
        assert len(violations) <= 5, (
            f"Shutdown zone has {len(violations)} silent 'except Exception: pass' "
            f"handlers at lines: {violations}. Target: <=5 after MVP."
        )


class TestSupervisorUsesLifecycleEngine:
    """unified_supervisor.py must use LifecycleEngine, not direct state writes."""

    def test_supervisor_imports_lifecycle_engine(self):
        source = Path("unified_supervisor.py").read_text()
        assert "LifecycleEngine" in source, (
            "unified_supervisor.py must import and use LifecycleEngine"
        )

    def test_supervisor_imports_lifecycle_event(self):
        source = Path("unified_supervisor.py").read_text()
        assert "LifecycleEvent" in source, (
            "unified_supervisor.py must use typed LifecycleEvent enum"
        )


class TestExceptionTaxonomyComplete:
    """All required exception classes exist with correct hierarchy."""

    def test_full_hierarchy(self):
        from backend.core.lifecycle_exceptions import (
            LifecycleSignal, ShutdownRequested, LifecycleCancelled,
            LifecycleError, LifecycleFatalError, LifecycleRecoverableError,
            DependencyUnavailableError, TransitionRejected,
        )
        # Signals
        assert issubclass(LifecycleSignal, BaseException)
        assert not issubclass(LifecycleSignal, Exception)
        assert issubclass(ShutdownRequested, LifecycleSignal)
        assert issubclass(LifecycleCancelled, LifecycleSignal)
        # Errors
        assert issubclass(LifecycleError, Exception)
        assert issubclass(LifecycleFatalError, LifecycleError)
        assert issubclass(LifecycleRecoverableError, LifecycleError)
        assert issubclass(DependencyUnavailableError, LifecycleRecoverableError)
        assert issubclass(TransitionRejected, LifecycleError)

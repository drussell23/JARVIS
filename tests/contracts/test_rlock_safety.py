"""Guard test: verify threading.RLock usage is only in synchronous call paths.

Specifically targets threading.RLock (not asyncio.Lock) to prevent blocking
the event loop with synchronous lock acquisition in async functions.
"""
import ast
from pathlib import Path
import pytest

# Variable name patterns that indicate threading.RLock (not asyncio.Lock)
_RLOCK_PATTERNS = ("_rlock", "rlock", "_threading_lock", "_sync_lock")


class TestRLockSafety:
    def test_no_threading_rlock_in_async_functions(self):
        """threading.RLock.acquire() must not be called inside async def functions.

        asyncio.Lock.acquire() IS allowed in async functions (it's awaitable).
        This test only catches threading.RLock patterns that would block the event loop.
        """
        backend = Path("backend")
        if not backend.exists():
            pytest.skip("backend directory not found")

        violations = []
        for py_file in backend.rglob("*.py"):
            try:
                source = py_file.read_text(encoding="utf-8", errors="replace")
                tree = ast.parse(source)
            except (SyntaxError, ValueError):
                continue

            # First pass: find files that import threading.RLock
            has_rlock_import = "RLock" in source

            for node in ast.walk(tree):
                if isinstance(node, ast.AsyncFunctionDef):
                    for child in ast.walk(node):
                        if (
                            isinstance(child, ast.Call)
                            and isinstance(child.func, ast.Attribute)
                            and child.func.attr == "acquire"
                            and isinstance(child.func.value, ast.Name)
                        ):
                            var_name = child.func.value.id.lower()
                            # Only flag if it matches RLock naming patterns
                            if any(pat in var_name for pat in _RLOCK_PATTERNS):
                                violations.append(
                                    f"{py_file.relative_to('.')}:{child.lineno} "
                                    f"-- {child.func.value.id}.acquire() "
                                    f"in async def {node.name}"
                                )

        assert not violations, (
            f"threading.RLock acquire in async contexts:\n" + "\n".join(violations)
        )

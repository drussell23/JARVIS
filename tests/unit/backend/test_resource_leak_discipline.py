#!/usr/bin/env python3
"""
Resource leak and cleanup discipline tests for unified_supervisor.py (Phase 4).

Run: python3 -m pytest tests/unit/backend/test_resource_leak_discipline.py -v
"""
import ast
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent.parent.parent))


class TestSubprocessCleanupPattern:
    """4C: Subprocess code must kill on timeout and cancel."""

    def test_memory_pressure_subprocess_has_cleanup(self):
        """memory_pressure subprocess must have kill/wait on TimeoutError."""
        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())

        for node in ast.walk(tree):
            if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                body_dump = ast.dump(node)
                if "memory_pressure" in body_dump and "create_subprocess_exec" in body_dump:
                    assert "kill" in body_dump or "terminate" in body_dump, (
                        f"Method {node.name} spawns memory_pressure subprocess "
                        f"but has no kill/terminate cleanup"
                    )
                    return
        pytest.skip("memory_pressure subprocess method not found")


class TestChromaDBCleanup:
    """4D: ChromaDB client must be explicitly closed in cleanup()."""

    def test_cleanup_closes_client_and_nullifies_refs(self):
        """cleanup() must close/reset ChromaDB client AND set references to None."""
        import ast

        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "SemanticVoiceCacheManager":
                for item in node.body:
                    if isinstance(item, ast.AsyncFunctionDef) and item.name == "cleanup":
                        body = ast.dump(item)
                        has_reset = "reset" in body
                        has_nullify_client = False
                        has_nullify_collection = False
                        # Check for _client = None and _collection = None assignments
                        for sub in ast.walk(item):
                            if isinstance(sub, ast.Assign):
                                for target in sub.targets:
                                    if isinstance(target, ast.Attribute):
                                        if target.attr == "_client" and isinstance(sub.value, ast.Constant) and sub.value.value is None:
                                            has_nullify_client = True
                                        if target.attr == "_collection" and isinstance(sub.value, ast.Constant) and sub.value.value is None:
                                            has_nullify_collection = True
                        assert has_reset, "cleanup() must call reset() on ChromaDB client"
                        assert has_nullify_client, "cleanup() must set self._client = None"
                        assert has_nullify_collection, "cleanup() must set self._collection = None"
                        return
        pytest.fail("SemanticVoiceCacheManager.cleanup() not found")


class TestTerminalAtexitSafetyNet:
    """4B: atexit handler must be registered when keyboard listener starts."""

    def test_keyboard_listener_references_atexit(self):
        """_keyboard_listener must register an atexit handler for terminal safety."""
        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_keyboard_listener":
                body = ast.dump(node)
                assert "atexit" in body, (
                    "_keyboard_listener must reference atexit for terminal restoration safety"
                )
                return
        pytest.fail("_keyboard_listener not found")


class TestAtomicStatePersistence:
    """4F: State persistence must use atomic write pattern."""

    @pytest.mark.parametrize("class_name,method_name", [
        ("CostTracker", "_save_state"),
        ("VMSessionTracker", "_save_registry"),
        ("GlobalSessionManager", "_register_global_session"),
        ("GlobalSessionManager", "_save_registry_async"),
    ])
    def test_state_writer_uses_atomic_pattern(self, class_name, method_name):
        """State write methods must use _atomic_write_json, not raw write_text."""
        import ast

        with open("unified_supervisor.py", "r") as f:
            tree = ast.parse(f.read())

        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for item in node.body:
                    if isinstance(item, (ast.AsyncFunctionDef, ast.FunctionDef)) and item.name == method_name:
                        body = ast.dump(item)
                        assert "write_text" not in body, (
                            f"{class_name}.{method_name} uses raw write_text — must use _atomic_write_json"
                        )
                        assert "_atomic_write_json" in body, (
                            f"{class_name}.{method_name} must call _atomic_write_json"
                        )
                        return
        pytest.fail(f"{class_name}.{method_name} not found")

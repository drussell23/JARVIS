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

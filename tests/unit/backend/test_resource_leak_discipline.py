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

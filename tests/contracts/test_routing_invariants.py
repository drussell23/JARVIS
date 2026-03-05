"""
AST-based invariant tests for routing contracts.

These tests enforce structural rules that prevent drift.
Some tests are expected to FAIL initially — they define the target state.
"""
import ast
import os
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent

FACTORY_ALLOWLIST = {
    str(REPO_ROOT / "backend" / "intelligence" / "unified_model_serving.py"),
}

PROHIBITED_CONSTRUCTORS = {"PrimeAPIClient", "PrimeCloudRunClient", "PrimeLocalClient"}


class TestNoDirectClientConstruction:
    """Enforce: no direct client construction outside factory module."""

    def _scan_file(self, filepath: str) -> list:
        violations = []
        try:
            with open(filepath) as f:
                tree = ast.parse(f.read(), filename=filepath)
        except SyntaxError:
            return []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in PROHIBITED_CONSTRUCTORS:
                    violations.append(
                        f"{filepath}:{node.lineno} — direct {node.func.id}()"
                    )
        return violations

    def test_no_bypass_construction(self):
        """No direct PrimeAPIClient/PrimeCloudRunClient outside factory."""
        violations = []
        backend_dir = REPO_ROOT / "backend"
        for py_file in backend_dir.rglob("*.py"):
            filepath = str(py_file)
            if filepath in FACTORY_ALLOWLIST:
                continue
            if "test" in filepath.lower() or "__pycache__" in filepath:
                continue
            violations.extend(self._scan_file(filepath))

        assert not violations, (
            f"Direct client construction found outside factory:\n"
            + "\n".join(violations)
        )


class TestCapabilityTaxonomyConsistency:
    """Enforce: no hardcoded capability sets in routing code."""

    def test_no_hardcoded_vision_providers(self):
        """The hardcoded vision_providers set must not exist."""
        serving_path = REPO_ROOT / "backend" / "intelligence" / "unified_model_serving.py"
        with open(serving_path) as f:
            content = f.read()

        assert "vision_providers = {" not in content, (
            "Hardcoded vision_providers set found in unified_model_serving.py. "
            "Vision routing must use manifest-driven capability checks."
        )

    def test_capability_registry_imported(self):
        """Contract package must be importable."""
        from backend.contracts.capability_taxonomy import CAPABILITY_REGISTRY
        assert "vision" in CAPABILITY_REGISTRY
        assert "chat" in CAPABILITY_REGISTRY


class TestContractVersioning:
    """Enforce: contract versions are declared and compatible."""

    def test_local_contract_valid(self):
        from backend.contracts.contract_version import LOCAL_CONTRACT
        assert LOCAL_CONTRACT.current >= LOCAL_CONTRACT.min_supported
        assert LOCAL_CONTRACT.current <= LOCAL_CONTRACT.max_supported

    def test_self_compatibility(self):
        from backend.contracts.contract_version import LOCAL_CONTRACT
        compatible, reason = LOCAL_CONTRACT.is_compatible(LOCAL_CONTRACT.current)
        assert compatible, f"Contract not self-compatible: {reason}"

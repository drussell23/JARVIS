"""Contract test: no datetime.now() for elapsed time calculations in GCP controller."""
import ast
from pathlib import Path
import pytest


class TestNoDatetimeDurations:
    def test_no_datetime_now_in_duration_calculations(self):
        """supervisor_gcp_controller.py must not use datetime.now() for elapsed/duration math."""
        target = Path("backend/core/supervisor_gcp_controller.py")
        if not target.exists():
            pytest.skip("File not found")

        source = target.read_text()
        tree = ast.parse(source)

        # Find all datetime.now() calls and check if they're in subtraction expressions
        violations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
                # Check if either side calls datetime.now()
                for operand in (node.left, node.right):
                    if _is_datetime_now(operand):
                        violations.append(
                            f"Line {node.lineno}: datetime.now() used in subtraction "
                            f"(duration calculation)"
                        )

        assert not violations, (
            f"Found datetime.now() duration calculations in GCP controller:\n"
            + "\n".join(violations)
        )


def _is_datetime_now(node: ast.AST) -> bool:
    """Check if an AST node is a call to datetime.now()."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == "now":
            if isinstance(func.value, ast.Name) and func.value.id == "datetime":
                return True
    return False

"""Contract tests: readiness authority is the live health endpoint, never APARS.

INV-1: ready_for_inference is ONLY determined by J-Prime's live response.
INV-2: APARS progress file is observational metadata -- progress display only.

These are AST-based structural guards that catch regressions if someone
re-adds progress-based readiness or middleware readiness propagation.
"""
import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.parent
GCP_VM_MANAGER = REPO_ROOT / "backend" / "core" / "gcp_vm_manager.py"


def _read_gcp_vm_manager_source() -> str:
    """Read gcp_vm_manager.py source once (shared across tests)."""
    return GCP_VM_MANAGER.read_text()


def _extract_apars_launcher_heredoc(src: str) -> str:
    """Extract the APARS launcher Python code embedded in the startup script heredoc.

    The _build_apars_payload function and APARSEnrichmentMiddleware class live
    inside a heredoc (cat > "$APARS_LAUNCHER" << 'EOFLAUNCHER' ... EOFLAUNCHER)
    within the startup script string, not as top-level Python in the module.
    We must extract and parse them separately.
    """
    match = re.search(
        r"cat > .{1,40}APARS_LAUNCHER.{1,5} << 'EOFLAUNCHER'\n(.*?)\nEOFLAUNCHER",
        src,
        re.DOTALL,
    )
    assert match, "APARS launcher heredoc not found in gcp_vm_manager.py"
    return match.group(1)


class TestReadinessAuthority:
    """Structural guards for readiness authority invariants."""

    def test_ping_health_does_not_use_apars_for_readiness(self):
        """INV-1: _ping_health_endpoint must not use total_progress for readiness decisions."""
        src = _read_gcp_vm_manager_source()
        tree = ast.parse(src)

        found = False
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_ping_health_endpoint"
            ):
                found = True
                func_src = ast.get_source_segment(src, node)
                assert func_src is not None, (
                    "_ping_health_endpoint source not extractable"
                )
                # Must not contain total_progress readiness logic
                assert "total_progress" not in func_src, (
                    "INV-1 violation: _ping_health_endpoint must not use "
                    "total_progress for readiness"
                )
                break

        assert found, "_ping_health_endpoint function not found in gcp_vm_manager.py"

    def test_apars_middleware_does_not_set_readiness(self):
        """INV-2: APARSEnrichmentMiddleware must not set ready_for_inference or model_loaded."""
        src = _read_gcp_vm_manager_source()
        launcher_code = _extract_apars_launcher_heredoc(src)
        tree = ast.parse(launcher_code)

        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == "APARSEnrichmentMiddleware":
                found = True
                class_src = ast.get_source_segment(launcher_code, node)
                assert class_src is not None, (
                    "APARSEnrichmentMiddleware source not extractable"
                )
                assert "ready_for_inference" not in class_src, (
                    "INV-2 violation: APARSEnrichmentMiddleware must not "
                    "touch ready_for_inference"
                )
                assert 'setdefault("model_loaded"' not in class_src, (
                    "INV-2 violation: APARSEnrichmentMiddleware must not "
                    "set model_loaded"
                )
                break

        assert found, (
            "APARSEnrichmentMiddleware class not found in APARS launcher heredoc"
        )

    def test_build_apars_payload_excludes_readiness(self):
        """INV-2: _build_apars_payload return value must not include readiness fields."""
        src = _read_gcp_vm_manager_source()
        launcher_code = _extract_apars_launcher_heredoc(src)
        tree = ast.parse(launcher_code)

        found = False
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_build_apars_payload":
                found = True
                func_src = ast.get_source_segment(launcher_code, node)
                assert func_src is not None, (
                    "_build_apars_payload source not extractable"
                )

                # Find the return { ... } block
                return_match = re.search(r"return\s*\{(.*?)\}", func_src, re.DOTALL)
                assert return_match, "return dict not found in _build_apars_payload"
                return_body = return_match.group(1)

                # These readiness fields must NOT be in the return dict
                assert '"ready_for_inference"' not in return_body, (
                    "INV-2 violation: _build_apars_payload must not return "
                    "ready_for_inference"
                )
                assert '"model_loaded"' not in return_body, (
                    "INV-2 violation: _build_apars_payload must not return "
                    "model_loaded"
                )

                # Progress fields MUST still be present (sanity check)
                assert '"total_progress"' in return_body, (
                    "_build_apars_payload must still return total_progress"
                )
                assert '"boot_session_id"' in return_body, (
                    "_build_apars_payload must return boot_session_id"
                )
                break

        assert found, (
            "_build_apars_payload function not found in APARS launcher heredoc"
        )

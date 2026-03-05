"""Phase 1 Exit Gate: all readiness hardening invariants must hold."""
import ast
import re
from pathlib import Path


class TestPhase1ExitGate:
    def test_no_progress_readiness_coupling(self):
        """INV-5: total_progress must never determine readiness."""
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ping_health_endpoint":
                func_src = ast.get_source_segment(src, node)
                assert "total_progress" not in func_src

    def test_health_verdict_used_not_bool(self):
        """_ping_health_endpoint must return HealthVerdict, not bool."""
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ping_health_endpoint":
                func_src = ast.get_source_segment(src, node)
                assert "HealthVerdict" in func_src
                assert "return True," not in func_src
                assert "return False," not in func_src

    def test_correlation_id_sent(self):
        """_ping_health_endpoint must send X-Correlation-ID."""
        src = Path("backend/core/gcp_vm_manager.py").read_text()
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_ping_health_endpoint":
                func_src = ast.get_source_segment(src, node)
                assert "Correlation-ID" in func_src or "correlation_id" in func_src

    def test_process_epoch_in_startup_script(self):
        """Startup script must include process_epoch."""
        from backend.core.gcp_vm_manager import VMManagerConfig, GCPVMManager
        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr.config = VMManagerConfig()
        script = mgr._generate_golden_startup_script()
        assert "PROCESS_EPOCH=" in script
        assert '"process_epoch"' in script

    def test_hysteresis_config_exists(self):
        """VMManagerConfig must have hysteresis fields."""
        from backend.core.gcp_vm_manager import VMManagerConfig
        config = VMManagerConfig()
        assert hasattr(config, "readiness_hysteresis_up")
        assert config.readiness_hysteresis_up >= 2

    def test_timeout_profiles_exist(self):
        """Timeout profiles must be defined."""
        from backend.core.gcp_vm_manager import TIMEOUT_PROFILES
        assert len(TIMEOUT_PROFILES) >= 4

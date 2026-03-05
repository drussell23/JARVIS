"""Tests for synthetic progress behavior on service_start_timeout."""
import re
from pathlib import Path


class TestSyntheticProgressReset:
    def test_synthetic_handles_service_start_timeout_checkpoint(self):
        """Synthetic progress code must check for service_start_timeout checkpoint."""
        src = Path("unified_supervisor.py").read_text()
        assert "service_start_timeout" in src, \
            "Synthetic progress generator must handle service_start_timeout checkpoint"

    def test_synthetic_caps_strictly_on_timeout(self):
        """On service_start_timeout, cap must be strict (no +buffer)."""
        src = Path("unified_supervisor.py").read_text()
        # Find the block that handles service_start_timeout in synthetic progress
        # It should set _syn_apars_cap = int(_syn_apars_last) without adding buffer
        match = re.search(
            r'if.*service_start_timeout.*_syn_checkpoint.*\n\s*_syn_apars_cap\s*=\s*(.*)',
            src,
        )
        if not match:
            # Try alternate pattern
            match = re.search(
                r'"service_start_timeout"\s+in\s+_syn_checkpoint.*\n\s*_syn_apars_cap\s*=\s*(.*)',
                src,
            )
        assert match, "service_start_timeout strict cap logic not found"
        cap_expr = match.group(1).strip()
        # Must NOT have + 5 or + 2 in the cap expression for timeout case
        assert "+ 5" not in cap_expr and "+ 2" not in cap_expr, \
            f"Timeout cap must be strict (no buffer), got: {cap_expr}"

    def test_synthetic_normal_buffer_reduced_to_2(self):
        """Normal (non-timeout) synthetic buffer should be +2, not +5."""
        src = Path("unified_supervisor.py").read_text()
        # Find the normal case buffer
        matches = re.findall(r'_syn_apars_cap\s*=\s*min\(95,\s*int\(_syn_apars_last\)\s*\+\s*(\d+)\)', src)
        assert len(matches) >= 1, "Normal synthetic cap formula not found"
        for buffer_val in matches:
            assert int(buffer_val) <= 2, \
                f"Normal synthetic buffer should be <=2, got +{buffer_val}"

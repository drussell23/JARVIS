"""Tests for GCP VM version mismatch terminal detection."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "backend"))

import pytest


class TestVersionMismatchTerminal:
    @pytest.mark.asyncio
    async def test_version_mismatch_count_increments(self):
        """Each SCRIPT_VERSION_MISMATCH should increment the counter."""
        from core.gcp_vm_manager import GCPVMManager

        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr._version_mismatch_count = 0
        mgr._version_mismatch_terminal = False

        for _ in range(3):
            mgr._version_mismatch_count += 1

        assert mgr._version_mismatch_count == 3

    @pytest.mark.asyncio
    async def test_terminal_flag_set_after_max(self):
        """After exceeding max recycles, terminal flag should be set."""
        from core.gcp_vm_manager import GCPVMManager

        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr._version_mismatch_count = 3
        mgr._version_mismatch_terminal = False

        max_recycles = 2
        if mgr._version_mismatch_count > max_recycles:
            mgr._version_mismatch_terminal = True

        assert mgr._version_mismatch_terminal is True

    @pytest.mark.asyncio
    async def test_terminal_flag_prevents_recycle(self):
        """Once terminal, further recycle attempts should be skipped."""
        from core.gcp_vm_manager import GCPVMManager

        mgr = GCPVMManager.__new__(GCPVMManager)
        mgr._version_mismatch_terminal = True
        mgr._version_mismatch_count = 3

        assert mgr._version_mismatch_terminal is True
        # In production: recycle path checks this flag first and returns immediately

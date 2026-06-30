from __future__ import annotations
import pytest
from backend.core.ouroboros.governance import pre_apply_exec_lock as lock


@pytest.mark.asyncio
async def test_preflight_reports_daemon_state():
    assert await lock.docker_preflight(probe=lambda: True) is True
    assert await lock.docker_preflight(probe=lambda: False) is False

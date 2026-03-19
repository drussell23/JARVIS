# backend/tests/test_gcp_operation_poller.py
"""Hermetic tests for GCPOperationPoller — no GCP network calls."""
from __future__ import annotations
import asyncio
import dataclasses
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch
import pytest


# ---------------------------------------------------------------------------
# Helpers — fake GCP Operation objects
# ---------------------------------------------------------------------------

def _make_op(
    name: str = "operation-1234",
    status: str = "RUNNING",        # PENDING | RUNNING | DONE | ABORTING
    error: Any = None,
    zone_url: str = "https://www.googleapis.com/compute/v1/projects/proj/zones/us-central1-b",
    self_link: str = "",
    region_url: str = "",
) -> MagicMock:
    op = MagicMock()
    op.name = name
    op.status = status
    op.error = error
    op.zone = zone_url
    op.region = region_url
    op.self_link = self_link or f"{zone_url}/operations/{name}"
    return op


# ---------------------------------------------------------------------------
# Task 1: OperationScope
# ---------------------------------------------------------------------------

class TestOperationScope:
    def test_extracts_zone_from_zone_url(self):
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(zone_url="https://www.googleapis.com/compute/v1/projects/proj/zones/us-central1-b")
        scope = OperationScope.from_operation(op, fallback_project="proj")
        assert scope.zone == "us-central1-b"
        assert scope.project == "proj"
        assert scope.scope_type == "zonal"

    def test_extracts_project_from_self_link(self):
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(
            zone_url="",
            self_link="https://www.googleapis.com/compute/v1/projects/other-proj/zones/us-east1-b/operations/op-1",
        )
        scope = OperationScope.from_operation(op, fallback_project="fallback")
        assert scope.project == "other-proj"
        assert scope.zone == "us-east1-b"

    def test_uses_fallback_project_when_self_link_absent(self):
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(
            zone_url="https://www.googleapis.com/compute/v1/projects/proj/zones/us-central1-b",
            self_link="",
        )
        scope = OperationScope.from_operation(op, fallback_project="fallback-proj")
        assert scope.project == "proj"  # extracted from zone url, not fallback

    def test_raises_contract_error_when_no_scope(self):
        from backend.core.gcp_operation_poller import OperationScope, ScopeContractError
        op = _make_op(zone_url="", self_link="", region_url="")
        with pytest.raises(ScopeContractError):
            OperationScope.from_operation(op, fallback_project="proj")

    def test_zone_mismatch_regression_no_config_zone_fallback(self):
        """Old path: poller used config.zone even when op was in a different zone.
        New contract: scope ALWAYS comes from the operation object."""
        from backend.core.gcp_operation_poller import OperationScope
        op = _make_op(zone_url="https://www.googleapis.com/compute/v1/projects/proj/zones/us-east1-c")
        scope = OperationScope.from_operation(op, fallback_project="proj")
        # The scope must reflect the operation's actual zone, not any external config
        assert scope.zone == "us-east1-c"
        # There is no "config zone" parameter to from_operation — the old path is simply gone

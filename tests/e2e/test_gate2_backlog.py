"""Gate 2 — Real backlog acceptance E2E tests. Require live J-Prime.

These tests are stubs that skip by default. They will be filled in when
the full pipeline is ready for live J-Prime integration testing.

Run manually with: JARVIS_PRIME_ENDPOINT=http://... pytest tests/e2e/test_gate2_backlog.py -v
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.conftest import jprime

pytestmark = jprime


@pytest.fixture
def synthetic_backlog() -> dict:
    fixture = Path(__file__).parent / "fixtures" / "synthetic_backlog.json"
    return json.loads(fixture.read_text())


class TestG2RealBacklogE2E:
    async def test_real_generation_and_apply(self, e2e_repo_roots, synthetic_backlog) -> None:
        pytest.skip("Gate 2: run manually after Gate 1 passes with live J-Prime")


class TestG2GenerationVariability:
    async def test_two_runs_both_succeed(self, e2e_repo_roots, synthetic_backlog) -> None:
        pytest.skip("Gate 2: run manually after Gate 1 passes with live J-Prime")


class TestG2FailureTransparency:
    async def test_no_silent_failures(self, e2e_repo_roots, synthetic_backlog) -> None:
        pytest.skip("Gate 2: run manually after Gate 1 passes with live J-Prime")

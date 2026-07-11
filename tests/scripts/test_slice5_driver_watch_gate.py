"""Slice 5 T6 — driver WATCH ACTIVE gate + chaos-evidence verification."""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "isomorphic_a1_local",
    Path(__file__).resolve().parents[2] / "scripts" / "isomorphic_a1_local.py",
)
iso = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("isomorphic_a1_local", iso)
_SPEC.loader.exec_module(iso)


class _Proc:
    def poll(self):
        return None


class TestAwaitLogPredicate:
    @pytest.mark.asyncio
    async def test_predicate_found(self, tmp_path):
        log = tmp_path / "debug.log"
        log.write_text("boot...\n[FSEventBridge] WATCH ACTIVE — pipeline verified live (sentinel observed after 92.6s)\n")
        ok = await iso._await_log_predicate(
            _Proc(), str(log), lambda l: iso._WATCH_ACTIVE_MARKER in l,
            timeout_s=2.0, label="watch-active",
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_timeout_returns_false(self, tmp_path):
        log = tmp_path / "debug.log"
        log.write_text("nothing relevant\n")
        ok = await iso._await_log_predicate(
            _Proc(), str(log), lambda l: "NOPE" in l, timeout_s=0.7, label="x",
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_await_soak_boot_delegates(self, tmp_path):
        log = tmp_path / "debug.log"
        log.write_text(iso._TESTWATCHER_READY_MARKER + "\n")
        assert await iso._await_soak_boot(_Proc(), str(log), timeout_s=2.0) is True


class TestEvidencePredicate:
    def test_evidence_line_matches(self):
        line = ("TestFailureSensor: scoped 1 test target(s) for 1 changed "
                "path(s): ['backend/core/ouroboros/a1_ignition_vector/leaf_predicates.py']")
        assert iso._chaos_evidence_predicate(["leaf_predicates.py"])(line) is True

    def test_unrelated_sensor_line_no_match(self):
        line = "TestFailureSensor: scoped 1 test target(s) for 1 changed path(s): ['x.py']"
        assert iso._chaos_evidence_predicate(["leaf_predicates.py"])(line) is False

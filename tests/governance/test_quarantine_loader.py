"""Dynamic Fault-Recovery Loader spine (Supervisor Campaign Step 1).

Mandate 4 verbatim (2026-07-19): a runtime call to a quarantined
capability must be trapped, hot-loaded from the quarantine namespace,
and the FSM event loop must keep running — no crash.
"""
from __future__ import annotations

import asyncio
import importlib
import json
import logging
import sys
from pathlib import Path

import pytest

from backend.core import quarantine_loader as ql

_REPO = Path(__file__).resolve().parents[2]
_QDIR = _REPO / "backend" / "core" / "quarantine"


@pytest.fixture()
def migrated_capability(monkeypatch):
    """Simulate a pruning slice: a capability module lives ONLY in
    quarantine; the manifest maps its original name."""
    mod_name = "backend.core._campaign_test_zone"
    qmod_file = _QDIR / "_campaign_test_zone.py"
    qmod_file.write_text(
        "CAPABILITY = 'revived'\n"
        "def execute_payload():\n"
        "    return 'payload-ran-from-quarantine'\n"
    )
    manifest = _QDIR / "manifest.json"
    original = manifest.read_text()
    manifest.write_text(json.dumps({
        "schema_version": "quarantine.1",
        "modules": {mod_name: "backend.core.quarantine._campaign_test_zone"},
    }))
    monkeypatch.setenv("JARVIS_QUARANTINE_LOADER_ENABLED", "true")
    yield mod_name
    manifest.write_text(original)
    qmod_file.unlink(missing_ok=True)
    sys.modules.pop(mod_name, None)
    sys.modules.pop("backend.core.quarantine._campaign_test_zone", None)
    ql.uninstall_quarantine_loader()


class TestFaultRecoveryLoader:
    async def test_runtime_call_to_quarantined_capability_revives(
        self, migrated_capability, caplog,
    ):
        """MANDATE 4 VERBATIM: ImportError trapped → hot-loaded from
        quarantine → payload executes → event loop alive → breach
        telemetry emitted."""
        mod_name = migrated_capability
        # Without the loader the module is GONE (proves quarantine):
        with pytest.raises(ImportError):
            importlib.import_module(mod_name)
        assert ql.install_quarantine_loader() is True
        with caplog.at_level(logging.CRITICAL, logger="Ouroboros.Quarantine"):
            revived = importlib.import_module(mod_name)   # the FSM's call
        assert revived.execute_payload() == "payload-ran-from-quarantine"
        assert revived.__name__ == mod_name               # identity intact
        # High-priority beacon:
        assert any("[QUARANTINE_BREACH]" in r.message for r in caplog.records)
        finder = ql.get_installed_finder()
        assert finder is not None and finder.stats["breaches"] == 1
        # The event loop is provably alive (we're awaiting on it):
        await asyncio.sleep(0.01)
        assert asyncio.get_running_loop().is_running()

    def test_healthy_imports_never_touch_the_finder(
        self, migrated_capability,
    ):
        ql.install_quarantine_loader()
        finder = ql.get_installed_finder()
        importlib.import_module("backend.core.quarantine_loader")
        assert finder.stats["breaches"] == 0              # tail position

    def test_master_gate_down_finder_never_installs(self, monkeypatch):
        monkeypatch.setenv("JARVIS_QUARANTINE_LOADER_ENABLED", "false")
        ql.uninstall_quarantine_loader()
        assert ql.install_quarantine_loader() is False
        assert ql.get_installed_finder() is None

    def test_broken_manifest_reads_empty_never_breaks_imports(
        self, monkeypatch, tmp_path,
    ):
        bad = tmp_path / "manifest.json"
        bad.write_text("{not json")
        monkeypatch.setattr(ql, "manifest_path", lambda: bad)
        assert ql.load_manifest() == {}

    def test_manifest_mapping_to_missing_module_is_a_miss_not_a_crash(
        self, monkeypatch,
    ):
        monkeypatch.setenv("JARVIS_QUARANTINE_LOADER_ENABLED", "true")
        ql.uninstall_quarantine_loader()
        monkeypatch.setattr(ql, "load_manifest", lambda: {
            "backend.core._ghost_zone": "backend.core.quarantine._nonexistent",
        })
        ql.install_quarantine_loader()
        try:
            with pytest.raises(ImportError):
                importlib.import_module("backend.core._ghost_zone")
            assert ql.get_installed_finder().stats["misses"] == 1
        finally:
            ql.uninstall_quarantine_loader()

    def test_install_is_idempotent(self, migrated_capability):
        assert ql.install_quarantine_loader() is True
        assert ql.install_quarantine_loader() is True
        assert sum(
            1 for f in sys.meta_path if isinstance(f, ql.QuarantineFinder)
        ) == 1

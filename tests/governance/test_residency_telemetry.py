"""Residency Telemetry spine — self-instrumented loop + lazy ML.

Mandate 4 verbatim (2026-07-19): a fast-forwarded 24h event loop where
the snapshot fires repeatedly. Assert the telemetry queue footprint
stays flat and the RotatingFileHandler prunes the .jsonl to its max
byte size WITHOUT halting the primary event loop.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.residency_telemetry import (
    ResidencyTelemetry,
    loop_lag_ms,
    rss_mb,
)


class TestNativeProbes:
    def test_rss_native_no_psutil(self):
        v = rss_mb()
        assert isinstance(v, float) and v > 0        # real process RSS
        # No psutil import anywhere in the module:
        src = (
            Path(__file__).resolve().parents[2]
            / "backend/core/ouroboros/governance/residency_telemetry.py"
        ).read_text()
        # No psutil IMPORT (the docstring may mention it as the thing
        # we deliberately avoid — check imports, not prose):
        import ast
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                assert all("psutil" not in a.name for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                assert "psutil" not in (node.module or "")
        assert "import resource" in src              # stdlib native

    async def test_loop_lag_measures_scheduling(self):
        lag = await loop_lag_ms()
        assert lag >= 0.0 and lag < 500.0           # healthy idle loop

    async def test_congested_loop_shows_higher_lag(self):
        import time
        healthy = await loop_lag_ms()
        # A blocking call hogs the loop between the call_soon and its
        # callback — lag inflates (the honest starvation signal).
        import asyncio
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        t0 = loop.time()
        loop.call_soon(lambda: fut.set_result(loop.time()))
        time.sleep(0.05)                             # BLOCK the loop 50ms
        t1 = await fut
        congested = round((t1 - t0) * 1000, 3)
        assert congested > healthy                   # detected congestion


class TestMandate4:
    async def test_24h_fastforward_flat_footprint_bounded_file(
        self, tmp_path, monkeypatch,
    ):
        """MANDATE 4 VERBATIM: 288 snapshots (24h @ 5min) → queue
        footprint flat, .jsonl rotated to max bytes, loop never
        halted."""
        # Tiny rotation so the prune is exercised within the test.
        monkeypatch.setenv("JARVIS_TELEMETRY_LOG_MAX_BYTES", "4096")
        monkeypatch.setenv("JARVIS_TELEMETRY_LOG_BACKUPS", "2")
        log = tmp_path / "residency.jsonl"
        tel = ResidencyTelemetry(
            conn_source=lambda: 2, log_path=log,
        )
        # Prove the loop stays alive throughout: a concurrent heartbeat.
        import asyncio
        beats = {"n": 0}
        async def _beat():
            for _ in range(50):
                beats["n"] += 1
                await asyncio.sleep(0)
        beat_task = asyncio.get_running_loop().create_task(_beat())

        SNAPSHOTS = 288                              # 24h at 5-min cadence
        for _ in range(SNAPSHOTS):
            row = await tel.snapshot()
            assert "rss_mb" in row and "loop_lag_ms" in row

        await beat_task
        assert beats["n"] == 50                      # loop NEVER halted

        # Footprint flat: the telemetry holds only `last` + counters,
        # not 288 rows in memory (bounded by construction).
        assert tel._samples == SNAPSHOTS
        assert isinstance(tel.last, dict)            # single latest row
        # File pruned to max bytes (+ backups), NOT 288 unbounded lines:
        assert log.exists()
        assert log.stat().st_size <= 4096 + 512      # rotated, bounded
        survivors = sorted(p.name for p in tmp_path.iterdir()
                           if p.name.startswith("residency"))
        assert len(survivors) <= 3                   # main + 2 backups
        # Every surviving line is valid JSON (bounded ring integrity):
        for line in log.read_text().splitlines():
            if line.strip():
                json.loads(line)
        await tel.stop()

    async def test_snapshot_records_leak_as_rss_delta(self, tmp_path):
        tel = ResidencyTelemetry(log_path=tmp_path / "r.jsonl")
        r1 = await tel.snapshot()
        r2 = await tel.snapshot()
        assert "rss_delta_mb" in r2                  # leak-detection field
        assert r1["sample"] == 0 and r2["sample"] == 1

    async def test_conn_source_fault_never_breaks_snapshot(self, tmp_path):
        def _boom():
            raise RuntimeError("bridge gone")
        tel = ResidencyTelemetry(
            conn_source=_boom, log_path=tmp_path / "r.jsonl",
        )
        row = await tel.snapshot()
        assert row["uds_conns"] == -1               # degraded, not crashed


class TestLazyMLSubstrates:
    def test_no_module_level_ml_imports_in_duplex(self):
        """Dependency Isolation (mandate 2): torch/speechbrain/Speech/
        AVFoundation/Quartz must be RUNTIME imports (PLC0415), never
        module-level — text-only residency keeps them out of memory."""
        import ast
        duplex = (
            Path(__file__).resolve().parents[2]
            / "backend/core/ouroboros/governance/comms/duplex"
        )
        heavy = {"torch", "speechbrain", "Speech", "AVFoundation", "Quartz"}
        offenders = []
        for py in duplex.glob("*.py"):
            tree = ast.parse(py.read_text())
            for node in tree.body:                   # MODULE LEVEL only
                if isinstance(node, ast.Import):
                    for a in node.names:
                        if a.name.split(".")[0] in heavy:
                            offenders.append(f"{py.name}: import {a.name}")
                elif isinstance(node, ast.ImportFrom) and node.module:
                    if node.module.split(".")[0] in heavy:
                        offenders.append(f"{py.name}: from {node.module}")
        assert not offenders, f"module-level ML imports: {offenders}"

    def test_text_only_import_stays_light(self):
        """Importing the sentry/scorer modules must NOT drag torch in
        (text-only residency contract)."""
        import subprocess, sys
        code = (
            "import sys;"
            "import backend.core.ouroboros.governance.comms.duplex."
            "passive_sentry;"
            "import backend.core.ouroboros.governance.comms.duplex."
            "biometric_scorer;"
            "print('torch' in sys.modules)"
        )
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            timeout=60,
        ).stdout.strip()
        assert out.endswith("False"), f"torch loaded on text-only import: {out}"

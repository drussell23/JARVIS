"""Tests for OpportunityMinerSensor (Sensor D) — observe-only."""
import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from backend.core.ouroboros.governance.intake.sensors.opportunity_miner_sensor import (
    OpportunityMinerSensor,
    StaticCandidate,
    _cyclomatic_complexity,
)


def test_cyclomatic_complexity_simple_function():
    src = "def foo():\n    return 1\n"
    tree = ast.parse(src)
    assert _cyclomatic_complexity(tree) == 1


def test_cyclomatic_complexity_branchy_function():
    src = """
def foo(x):
    if x > 0:
        for i in range(x):
            if i % 2 == 0:
                pass
    elif x < 0:
        while x < 0:
            x += 1
    return x
"""
    tree = ast.parse(src)
    cc = _cyclomatic_complexity(tree)
    assert cc >= 4  # if + for + if + elif + while = 5 branches


async def test_sensor_produces_pending_ack_envelope(tmp_path):
    # Write a complex Python file
    src_file = tmp_path / "backend" / "core" / "complex.py"
    src_file.parent.mkdir(parents=True, exist_ok=True)
    # High-complexity code
    lines = ["def foo(x):\n"]
    for i in range(12):
        lines.append(f"    if x == {i}:\n        return {i}\n")
    lines.append("    return -1\n")
    src_file.write_text("".join(lines))

    router = MagicMock()
    router.ingest = AsyncMock(return_value="pending_ack")
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path,
        router=router,
        scan_paths=["backend/core/"],
        complexity_threshold=5,
    )
    candidates = await sensor.scan_once()
    assert len(candidates) >= 1
    router.ingest.assert_called()
    # All D envelopes must have requires_human_ack=True
    for call in router.ingest.call_args_list:
        env = call.args[0]
        assert env.requires_human_ack is True
        assert env.source == "ai_miner"


async def test_sensor_skips_low_complexity_files(tmp_path):
    src_file = tmp_path / "simple.py"
    src_file.write_text("def foo():\n    return 1\n")
    router = MagicMock()
    router.ingest = AsyncMock()
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path,
        router=router,
        scan_paths=["."],
        complexity_threshold=10,
    )
    candidates = await sensor.scan_once()
    assert candidates == []
    router.ingest.assert_not_called()


async def test_sensor_skips_syntax_error_files(tmp_path):
    bad_file = tmp_path / "bad.py"
    bad_file.write_text("def broken(:\n    pass\n")
    router = MagicMock()
    router.ingest = AsyncMock()
    sensor = OpportunityMinerSensor(
        repo_root=tmp_path,
        router=router,
        scan_paths=["."],
        complexity_threshold=1,
    )
    candidates = await sensor.scan_once()
    # Syntax error file should be skipped, not crash
    assert candidates == []

"""Regression spine — compression-aware memory probe (2026-07-11 OOM RCA).

Live-fire evidence that forced this upgrade: an orphaned Oracle spawn
worker held a 33.9 GB phys_footprint while every rss-based monitor
(psutil, ps, ProcessMemoryWatchdog, MemoryPressureGate) read it as
7 MB — macOS had compressed its cold pages, and compressed pages leave
the resident set. Jetsam (and the user-facing "out of application
memory" dialog) charge the FULL footprint. The probe therefore must
measure ``ri_phys_footprint`` (proc_pid_rusage RUSAGE_INFO_V4) on
darwin, falling back to rss per-pid and to the legacy cascade
elsewhere — same single-source function, both consumers inherit it.
"""
from __future__ import annotations

import os
import sys

import pytest

from backend.core.ouroboros.governance import process_tree_probe as ptp


_DARWIN = sys.platform == "darwin"


# ---------------------------------------------------------------------------
# Per-pid darwin footprint primitive
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DARWIN, reason="phys_footprint is a darwin metric")
def test_pid_footprint_probe_self_positive() -> None:
    mb = ptp._probe_pid_footprint_mb(os.getpid())
    assert mb is not None
    assert mb > 1.0  # a live CPython interpreter is never under 1 MB


@pytest.mark.skipif(not _DARWIN, reason="phys_footprint is a darwin metric")
def test_pid_footprint_probe_dead_pid_returns_none() -> None:
    # PID 0 is kernel_task — proc_pid_rusage on it is denied for us; a
    # clearly-invalid pid must fail soft (None), never raise.
    assert ptp._probe_pid_footprint_mb(-1) is None


@pytest.mark.skipif(not _DARWIN, reason="phys_footprint is a darwin metric")
def test_pid_footprint_sees_dirty_allocation() -> None:
    """Dirty (touched) memory lands ~1:1 in phys_footprint — the exact
    signal rss loses once the compressor takes the pages."""
    before = ptp._probe_pid_footprint_mb(os.getpid())
    assert before is not None
    blob = bytearray(200 * 1024 * 1024)  # 200 MB, touched by bytearray zero-fill
    try:
        after = ptp._probe_pid_footprint_mb(os.getpid())
    finally:
        del blob
    assert after is not None
    assert after - before >= 150.0, (
        f"footprint delta {after - before:.1f} MB did not reflect a "
        "200 MB dirty allocation"
    )


# ---------------------------------------------------------------------------
# Metric resolution — env-driven, adaptive, no hardcoding
# ---------------------------------------------------------------------------


def test_metric_auto_resolves_by_platform(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JARVIS_MEMORY_PROBE_METRIC", raising=False)
    expected = "footprint" if _DARWIN else "rss"
    assert ptp._resolve_probe_metric() == expected


@pytest.mark.parametrize("raw,expected", [
    ("rss", "rss"),
    ("footprint", "footprint"),
    ("RSS", "rss"),
    ("  footprint  ", "footprint"),
    ("bogus", "footprint" if _DARWIN else "rss"),  # unknown → auto
])
def test_metric_env_override(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str,
) -> None:
    monkeypatch.setenv("JARVIS_MEMORY_PROBE_METRIC", raw)
    assert ptp._resolve_probe_metric() == expected


def test_metric_rss_never_calls_footprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("JARVIS_MEMORY_PROBE_METRIC", "rss")

    def _boom(pid: int) -> None:  # noqa: ARG001
        raise AssertionError("footprint probe must not run under metric=rss")

    monkeypatch.setattr(ptp, "_probe_pid_footprint_mb", _boom)
    total = ptp.probe_process_tree_memory_mb()
    assert total is not None and total > 0


# ---------------------------------------------------------------------------
# Tree probe — footprint path, per-pid rss fallback, legacy cascade
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DARWIN, reason="phys_footprint is a darwin metric")
def test_tree_probe_uses_footprint_on_darwin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("JARVIS_MEMORY_PROBE_METRIC", raising=False)
    calls: list = []
    real = ptp._probe_pid_footprint_mb

    def _spy(pid: int):
        calls.append(pid)
        return real(pid)

    monkeypatch.setattr(ptp, "_probe_pid_footprint_mb", _spy)
    total = ptp.probe_process_tree_memory_mb()
    assert total is not None and total > 0
    assert os.getpid() in calls


def test_tree_probe_footprint_failure_falls_back_to_rss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A broken/unavailable footprint primitive (non-darwin, denied,
    struct drift) must degrade per-pid to rss — never to None."""
    monkeypatch.setenv("JARVIS_MEMORY_PROBE_METRIC", "footprint")
    monkeypatch.setattr(ptp, "_probe_pid_footprint_mb", lambda pid: None)
    total = ptp.probe_process_tree_memory_mb()
    assert total is not None and total > 0


# ---------------------------------------------------------------------------
# Back-compat surface — both existing consumers + their monkeypatch seams
# ---------------------------------------------------------------------------


def test_legacy_name_is_alias_of_memory_probe() -> None:
    assert ptp.probe_process_tree_rss_mb is ptp.probe_process_tree_memory_mb

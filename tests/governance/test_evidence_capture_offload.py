from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.verification import evidence_capture as ec


def _make_tree(tmp_path: Path, n: int = 5) -> Path:
    tdir = tmp_path / "tests"
    tdir.mkdir()
    for i in range(n):
        (tdir / f"test_mod_{i}.py").write_text("def test_x():\n    pass\n")
    return tmp_path


class _Ctx(SimpleNamespace):
    """Plain mutable ctx — object.__setattr__ works."""


@pytest.mark.asyncio
async def test_pre_async_stamps_inventory(tmp_path):
    _make_tree(tmp_path)
    ctx = _Ctx()
    n = await ec.stamp_test_files_pre_async(ctx, target_dir=str(tmp_path))
    assert n == 5
    assert len(ctx.test_files_pre) == 5
    assert all(p.startswith("tests/") for p in ctx.test_files_pre)


@pytest.mark.asyncio
async def test_pre_async_crawl_runs_off_loop_thread(tmp_path, monkeypatch):
    _make_tree(tmp_path)
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = ec.capture_test_files_inventory

    def spy(*a, **kw):
        seen.append(threading.get_ident())
        return real(*a, **kw)

    monkeypatch.setattr(ec, "capture_test_files_inventory", spy)
    await ec.stamp_test_files_pre_async(_Ctx(), target_dir=str(tmp_path))
    assert seen and seen[0] != loop_thread


@pytest.mark.asyncio
async def test_pre_async_loop_stays_responsive(tmp_path, monkeypatch):
    import time as _time
    _make_tree(tmp_path)

    def slow_crawl(*a, **kw):
        _time.sleep(0.4)
        return ("tests/test_mod_0.py",)

    monkeypatch.setattr(ec, "capture_test_files_inventory", slow_crawl)
    ticks: list[float] = []

    async def heartbeat():
        t0 = asyncio.get_event_loop().time()
        while len(ticks) < 8:
            await asyncio.sleep(0.05)
            ticks.append(asyncio.get_event_loop().time() - t0)

    hb = asyncio.ensure_future(heartbeat())
    await ec.stamp_test_files_pre_async(_Ctx(), target_dir=str(tmp_path))
    await asyncio.wait_for(hb, timeout=2.0)
    # heartbeat must have ticked DURING the 0.4s crawl, not been frozen
    gaps = [b - a for a, b in zip(ticks, ticks[1:])]
    assert max(gaps) < 0.3, f"loop starved: gaps={gaps}"


@pytest.mark.asyncio
async def test_pre_async_idempotent_no_crawl_when_stamped(tmp_path, monkeypatch):
    ctx = _Ctx(test_files_pre=("tests/test_already.py",))

    def boom(*a, **kw):
        raise AssertionError("crawl must not run when pre already stamped")

    monkeypatch.setattr(ec, "capture_test_files_inventory", boom)
    n = await ec.stamp_test_files_pre_async(ctx, target_dir=str(tmp_path))
    assert n == 1
    assert ctx.test_files_pre == ("tests/test_already.py",)


@pytest.mark.asyncio
async def test_pre_async_offload_error_neutral(tmp_path, monkeypatch):
    """OffloadError → neutral () stamp, never an exception."""
    from backend.core.ouroboros.governance import cooperative_fs_io as cfio

    async def fake_offload(fn, /, *args, cpu_bound=False, **kwargs):
        return cfio.OffloadError(
            fn_name="capture_test_files_inventory",
            exc_type="OSError", message="synthetic", cpu_bound=False,
        )

    monkeypatch.setattr(cfio, "offload", fake_offload)
    ctx = _Ctx()
    n = await ec.stamp_test_files_pre_async(ctx, target_dir=str(tmp_path))
    assert n == 0
    assert ctx.test_files_pre == ()


@pytest.mark.asyncio
async def test_pre_async_master_off_inline_identical(tmp_path, monkeypatch):
    _make_tree(tmp_path)
    monkeypatch.setenv("JARVIS_COOPERATIVE_FS_IO_ENABLED", "false")
    ctx = _Ctx()
    n = await ec.stamp_test_files_pre_async(ctx, target_dir=str(tmp_path))
    sync_inv = ec.capture_test_files_inventory(str(tmp_path))
    assert n == 5 and ctx.test_files_pre == sync_inv


def test_sync_pre_accepts_precomputed_inventory(tmp_path):
    ctx = _Ctx()
    n = ec.stamp_test_files_pre(
        ctx, target_dir=str(tmp_path), inventory=("tests/test_a.py",),
    )
    assert n == 1 and ctx.test_files_pre == ("tests/test_a.py",)


def test_sync_target_pre_snapshot_is_keyword_only(tmp_path):
    """Plan constraint: snapshot must be keyword-only — a positional
    second arg must raise TypeError, never be silently accepted."""
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    ctx = _Ctx(target_files=[str(f)])
    with pytest.raises(TypeError):
        ec.stamp_target_files_pre(ctx, ())  # positional snapshot forbidden


def test_sync_pre_none_inventory_crawls_inline(tmp_path):
    _make_tree(tmp_path)
    ctx = _Ctx()
    n = ec.stamp_test_files_pre(ctx, target_dir=str(tmp_path))
    assert n == 5  # legacy behavior byte-identical


@pytest.mark.asyncio
async def test_target_pre_async_snapshots_off_loop(tmp_path, monkeypatch):
    f = tmp_path / "mod.py"
    f.write_text("x = 1\n")
    ctx = _Ctx(target_files=[str(f)])
    loop_thread = threading.get_ident()
    seen: list[int] = []
    real = ec.snapshot_target_files

    def spy(*a, **kw):
        seen.append(threading.get_ident())
        return real(*a, **kw)

    monkeypatch.setattr(ec, "snapshot_target_files", spy)
    n = await ec.stamp_target_files_pre_async(ctx)
    assert n == 1 and seen and seen[0] != loop_thread
    assert ctx.target_files_pre[0]["content"] == "x = 1\n"


@pytest.mark.asyncio
async def test_apply_evidence_post_async_composite(tmp_path):
    _make_tree(tmp_path)
    f = tmp_path / "mod.py"
    f.write_text("x = 2\n")
    ctx = _Ctx(
        target_files=[str(f)],
        target_files_pre=({"path": str(f), "content": "x = 1\n", "exists": True},),
    )
    diag = await ec.stamp_apply_evidence_post_async(
        ctx, target_dir=str(tmp_path),
    )
    assert diag["enabled"] == 1
    assert diag["test_files_post"] == 5
    assert diag["target_files_post"] == 1
    assert diag["diff_text_bytes"] > 0  # x=1 → x=2 produced a diff

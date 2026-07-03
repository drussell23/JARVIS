"""fs-hot-tier Batch 3 (row 16, 2026-07-03) — MultiFileRefactorEngine
``_scan_files_python`` regression spine.

``_scan_files_python`` (the grep-subprocess FALLBACK path only — rare)
did a whole-repo ``Path.glob`` + per-file ``read_text`` + substring
scan synchronously inside an ``async def``, directly on the asyncio
loop thread (audit row 16, ``multi_file_refactor.py:752``).

Routes the scan through ``cooperative_fs_io.offload(cpu_bound=False)``
— a thread pool, not a process pool: reads release the GIL and the
substring check is cheap, so a process pool would only add spawn/
pickling overhead for no benefit.

No test file previously existed for ``multi_file_refactor.py`` — this
is a new regression spine.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import cooperative_fs_io
from backend.core.ouroboros.governance.multi_file_refactor import (
    MultiFileRefactorEngine,
)


def _engine(tmp_path) -> MultiFileRefactorEngine:
    return MultiFileRefactorEngine(project_root=tmp_path)


# ---------------------------------------------------------------------------
# (a) Substrate routing
# ---------------------------------------------------------------------------


class TestSubstrateRouting:
    @pytest.mark.asyncio
    async def test_scan_routes_through_offload_thread_pool(
        self, tmp_path, monkeypatch,
    ):
        (tmp_path / "a.py").write_text("needle_token\n")
        eng = _engine(tmp_path)

        calls = {"n": 0, "cpu_bound": None}
        real_offload = cooperative_fs_io.offload

        async def _spy_offload(fn, *args, cpu_bound=False, **kwargs):
            calls["n"] += 1
            calls["cpu_bound"] = cpu_bound
            return await real_offload(fn, *args, cpu_bound=cpu_bound, **kwargs)

        monkeypatch.setattr(cooperative_fs_io, "offload", _spy_offload)
        result = await eng._scan_files_python("needle_token", "*.py")

        assert calls["n"] == 1, "_scan_files_python must route through offload"
        assert calls["cpu_bound"] is False, (
            "read + substring scan releases the GIL — must use the "
            "thread pool, not a process pool"
        )
        assert result == ["a.py"]


# ---------------------------------------------------------------------------
# (b) Parity — matches a direct synchronous scan on a planted tree
# ---------------------------------------------------------------------------


class TestCorrectness:
    @pytest.mark.asyncio
    async def test_parity_matches_sync_scan(self, tmp_path):
        (tmp_path / "sub").mkdir()
        (tmp_path / "a.py").write_text("TARGET_TEXT here\n")
        (tmp_path / "sub" / "b.py").write_text("TARGET_TEXT here too\n")
        (tmp_path / "c.py").write_text("nothing interesting\n")

        eng = _engine(tmp_path)
        result = set(await eng._scan_files_python("TARGET_TEXT", "*.py"))
        assert result == {"a.py", "sub/b.py"}

    @pytest.mark.asyncio
    async def test_no_matches_returns_empty(self, tmp_path):
        (tmp_path / "a.py").write_text("nothing\n")
        eng = _engine(tmp_path)
        assert await eng._scan_files_python("ABSENT_TOKEN", "*.py") == []

    @pytest.mark.asyncio
    async def test_unreadable_file_is_skipped_not_raised(self, tmp_path):
        (tmp_path / "good.py").write_text("TARGET\n")
        # A binary/undecodable file must be skipped, not crash the scan.
        (tmp_path / "bad.py").write_bytes(b"\xff\xfe\x00TARGET\x00")
        eng = _engine(tmp_path)
        result = await eng._scan_files_python("TARGET", "*.py")
        assert "good.py" in result


# ---------------------------------------------------------------------------
# (c) Fail-soft
# ---------------------------------------------------------------------------


class TestFailSoft:
    @pytest.mark.asyncio
    async def test_offload_error_degrades_to_empty_no_raise(
        self, tmp_path, monkeypatch,
    ):
        from backend.core.ouroboros.governance.cooperative_fs_io import (
            OffloadError,
        )
        (tmp_path / "a.py").write_text("TARGET\n")
        eng = _engine(tmp_path)

        async def _boom_offload(fn, *args, **kwargs):
            return OffloadError(
                fn_name="_scan_files_python_worker",
                exc_type="OSError",
                message="synthetic offload-layer fault",
                cpu_bound=False,
            )

        monkeypatch.setattr(cooperative_fs_io, "offload", _boom_offload)
        result = await eng._scan_files_python("TARGET", "*.py")
        assert result == []


# ---------------------------------------------------------------------------
# (d) Await-guard on the real caller chain
# (_find_files_containing falls back to _scan_files_python when the
# grep subprocess is unavailable/fails)
# ---------------------------------------------------------------------------


class TestAwaitGuard:
    @pytest.mark.asyncio
    async def test_find_files_containing_falls_back_and_awaits(
        self, tmp_path, monkeypatch,
    ):
        (tmp_path / "a.py").write_text("FALLBACK_TARGET\n")
        eng = _engine(tmp_path)

        async def _boom_subprocess(*args, **kwargs):
            raise OSError("grep unavailable in this sandbox")

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", _boom_subprocess,
        )
        result = await eng._find_files_containing("FALLBACK_TARGET", "*.py")
        assert result == ["a.py"]

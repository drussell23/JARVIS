"""fs-hot-tier Batch 3 (row 24, 2026-07-03) — CppAdapter._default_abi_probe
regression spine.

``_default_abi_probe`` (already ``async def``, native/C++ VALIDATE
target only) did ``install_tmp.rglob("*.so")`` synchronously inside
the coroutine, directly on the asyncio loop thread (audit row 24,
``test_runner.py:543``).

Routes the scan through ``cooperative_fs_io.offload(cpu_bound=False)``
— a thread pool, not a process pool: a scoped install-dir rglob for
one native build is small and IO-bound.

No test file previously exercised this specific rglob call — this is
a new regression spine (existing CppAdapter tests exercise other
surfaces).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.core.ouroboros.governance import cooperative_fs_io
from backend.core.ouroboros.governance.test_runner import (
    CppAdapter,
    _rglob_so_files_worker,
)


def _adapter(tmp_path: Path) -> CppAdapter:
    return CppAdapter(
        repo_root=tmp_path / "repo", scratch_root=tmp_path / "scratch",
    )


def _fake_successful_install_proc() -> MagicMock:
    """A fake asyncio subprocess for 'cmake --install' that succeeds
    immediately (communicate() resolves with no output)."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"", None))
    proc.returncode = 0
    return proc


class TestSubstrateRouting:
    @pytest.mark.asyncio
    async def test_probe_routes_through_offload_thread_pool(
        self, tmp_path, monkeypatch,
    ):
        adapter = _adapter(tmp_path)
        build_dir = tmp_path / "build"
        install_tmp = build_dir / "_abi_probe_install"
        install_tmp.mkdir(parents=True)
        (install_tmp / "lib.so").write_bytes(b"\x00")

        calls = {"n": 0, "cpu_bound": None}
        real_offload = cooperative_fs_io.offload

        async def _spy_offload(fn, *args, cpu_bound=False, **kwargs):
            calls["n"] += 1
            calls["cpu_bound"] = cpu_bound
            return await real_offload(fn, *args, cpu_bound=cpu_bound, **kwargs)

        monkeypatch.setattr(cooperative_fs_io, "offload", _spy_offload)

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[
                _fake_successful_install_proc(),  # cmake --install
                _fake_successful_install_proc(),  # ctypes.CDLL probe
            ]),
        ):
            ok, err = await adapter._default_abi_probe(build_dir, None)

        assert calls["n"] == 1, "_default_abi_probe must route the rglob through offload"
        assert calls["cpu_bound"] is False, (
            "a scoped install-dir rglob for one native build is small "
            "and IO-bound — must use the thread pool, not a process pool"
        )
        assert ok is True


class TestCorrectness:
    def test_worker_parity_with_direct_sync_rglob(self, tmp_path):
        install_tmp = tmp_path / "install"
        (install_tmp / "sub").mkdir(parents=True)
        (install_tmp / "a.so").write_bytes(b"\x00")
        (install_tmp / "sub" / "b.so").write_bytes(b"\x00")
        (install_tmp / "c.txt").write_bytes(b"\x00")

        expected = {str(p) for p in install_tmp.rglob("*.so")}
        got = set(_rglob_so_files_worker(str(install_tmp)))
        assert got == expected
        assert len(got) == 2

    def test_worker_missing_dir_returns_empty(self, tmp_path):
        assert _rglob_so_files_worker(str(tmp_path / "nope")) == []


class TestFailSoft:
    @pytest.mark.asyncio
    async def test_offload_error_degrades_to_skip_probe_no_raise(
        self, tmp_path, monkeypatch,
    ):
        from backend.core.ouroboros.governance.cooperative_fs_io import (
            OffloadError,
        )
        adapter = _adapter(tmp_path)
        build_dir = tmp_path / "build"
        install_tmp = build_dir / "_abi_probe_install"
        install_tmp.mkdir(parents=True)
        (install_tmp / "lib.so").write_bytes(b"\x00")

        async def _boom_offload(fn, *args, **kwargs):
            return OffloadError(
                fn_name="_rglob_so_files_worker",
                exc_type="OSError",
                message="synthetic offload-layer fault",
                cpu_bound=False,
            )

        monkeypatch.setattr(cooperative_fs_io, "offload", _boom_offload)

        with patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_fake_successful_install_proc()),
        ):
            ok, err = await adapter._default_abi_probe(build_dir, None)

        # Degrades to "no .so files found" -- probe skipped, never raises,
        # never fails the build over an offload-layer fault.
        assert ok is True
        assert err == ""

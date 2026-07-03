"""fs-hot-tier Batch 3 (row 22, 2026-07-03) — GoAdapter.resolve
regression spine.

``GoAdapter.resolve`` (already ``async def``, via
``LanguageRouter.run`` in VALIDATE for non-Python changed files) did a
per-directory ``d.iterdir()`` scan for ``_test.go`` siblings
synchronously inside the coroutine, directly on the asyncio loop
thread (audit row 22, ``language_adapters.py:143``).

Routes the scan through ``cooperative_fs_io.offload(cpu_bound=False)``
— a thread pool, not a process pool: ``iterdir`` is IO-bound
(syscall-dominated).

No test file previously existed for ``language_adapters.py`` — this
is a new regression spine.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance import cooperative_fs_io
from backend.core.ouroboros.governance.language_adapters import (
    GoAdapter,
    JavaScriptAdapter,
    RustAdapter,
)


def _adapter() -> GoAdapter:
    return GoAdapter()


class TestSubstrateRouting:
    @pytest.mark.asyncio
    async def test_resolve_routes_through_offload_thread_pool(
        self, tmp_path, monkeypatch,
    ):
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "foo.go").write_text("package pkg\n")
        (tmp_path / "pkg" / "foo_test.go").write_text("package pkg\n")

        calls = {"n": 0, "cpu_bound": None}
        real_offload = cooperative_fs_io.offload

        async def _spy_offload(fn, *args, cpu_bound=False, **kwargs):
            calls["n"] += 1
            calls["cpu_bound"] = cpu_bound
            return await real_offload(fn, *args, cpu_bound=cpu_bound, **kwargs)

        monkeypatch.setattr(cooperative_fs_io, "offload", _spy_offload)
        adapter = _adapter()
        result = await adapter.resolve(["pkg/foo.go"], tmp_path)

        assert calls["n"] == 1, "GoAdapter.resolve must route through offload"
        assert calls["cpu_bound"] is False, (
            "iterdir is IO-bound (syscall-dominated) — must use the "
            "thread pool, not a process pool"
        )
        assert result == (tmp_path / "pkg",)


class TestCorrectness:
    @pytest.mark.asyncio
    async def test_parity_with_direct_sync_scan(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "x.go").write_text("package a\n")
        (tmp_path / "a" / "x_test.go").write_text("package a\n")
        (tmp_path / "b").mkdir()
        (tmp_path / "b" / "y.go").write_text("package b\n")
        # no _test.go sibling in b/

        adapter = _adapter()
        result = await adapter.resolve(
            ["a/x.go", "b/y.go"], tmp_path,
        )
        assert set(result) == {tmp_path / "a"}

    @pytest.mark.asyncio
    async def test_non_go_files_ignored(self, tmp_path):
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "x_test.go").write_text("package a\n")
        adapter = _adapter()
        result = await adapter.resolve(["a/x.py", "a/x.md"], tmp_path)
        assert result == ()

    @pytest.mark.asyncio
    async def test_missing_dir_returns_empty(self, tmp_path):
        adapter = _adapter()
        result = await adapter.resolve(["nope/x.go"], tmp_path)
        assert result == ()


class TestFailSoft:
    @pytest.mark.asyncio
    async def test_offload_error_degrades_to_empty_no_raise(
        self, tmp_path, monkeypatch,
    ):
        from backend.core.ouroboros.governance.cooperative_fs_io import (
            OffloadError,
        )
        (tmp_path / "a").mkdir()
        (tmp_path / "a" / "x_test.go").write_text("package a\n")

        async def _boom_offload(fn, *args, **kwargs):
            return OffloadError(
                fn_name="_go_resolve_test_dirs_worker",
                exc_type="OSError",
                message="synthetic offload-layer fault",
                cpu_bound=False,
            )

        monkeypatch.setattr(cooperative_fs_io, "offload", _boom_offload)
        adapter = _adapter()
        result = await adapter.resolve(["a/x.go"], tmp_path)
        assert result == ()


class TestSiblingAdaptersUnaffected:
    """Row 22 is scoped to GoAdapter only — JS/Rust adapters must
    remain byte-identical (no rglob/iterdir in their resolve())."""

    @pytest.mark.asyncio
    async def test_javascript_adapter_resolve_unaffected(self, tmp_path):
        (tmp_path / "src").mkdir()
        test_file = tmp_path / "src" / "foo.test.js"
        test_file.write_text("test('x', () => {})\n")
        adapter = JavaScriptAdapter()
        result = await adapter.resolve(["src/foo.js"], tmp_path)
        assert result == (test_file,)

    @pytest.mark.asyncio
    async def test_rust_adapter_resolve_unaffected(self, tmp_path):
        (tmp_path / "Cargo.toml").write_text("[package]\nname = 'x'\n")
        adapter = RustAdapter()
        result = await adapter.resolve(["src/main.rs"], tmp_path)
        assert result == (tmp_path / "Cargo.toml",)

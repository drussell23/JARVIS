"""Priority-3 regression spine — Developer-Memory injection into the
StrategicDirection digest.

Surfaces the operator's curated repo ``memory/*.md`` into every
GENERATE prompt during a soak by composing the EXISTING
``roadmap.source_crawlers.crawl_memory`` crawler (no glob
duplication), recency-ranked + budget-capped, behind an env master
flag, fail-silent, authority-free.

Pins:
  * default-False master flag → empty section (zero behaviour change)
  * enabled → '## Recent Developer Memory' with fragment content
  * recency-ranked (newest mtime first)
  * char + file-count budgets enforced (env-tunable)
  * fail-silent if the crawler raises
  * authority-free disclaimer present
  * wired into format_for_prompt()
  * AST: composes crawl_memory, does NOT re-glob memory/ itself
"""
from __future__ import annotations

import ast
import asyncio
import logging
import os
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.strategic_direction import (
    StrategicDirectionService,
)

_SRC = (
    Path(__file__).resolve().parents[2]
    / "backend/core/ouroboros/governance/strategic_direction.py"
)
_FLAG = "JARVIS_STRATEGIC_DEV_MEMORY_ENABLED"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        _FLAG,
        "JARVIS_STRATEGIC_DEV_MEMORY_MAX_CHARS",
        "JARVIS_STRATEGIC_DEV_MEMORY_MAX_FILES",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def _repo_with_memory(tmp_path: Path, files: dict[str, tuple[str, float]]) -> Path:
    """files: name -> (content, mtime)."""
    mem = tmp_path / "memory"
    mem.mkdir(parents=True, exist_ok=True)
    for name, (content, mtime) in files.items():
        p = mem / name
        p.write_text(content, encoding="utf-8")
        os.utime(p, (mtime, mtime))
    return tmp_path


def _svc(root: Path) -> StrategicDirectionService:
    s = StrategicDirectionService(root)
    s._digest = "PRINCIPLES"  # bypass load(); format_for_prompt needs non-empty
    return s


# --------------------------------------------------------------------------

async def test_graduated_default_true_injects_without_flag(tmp_path):
    # P3 GRADUATED (soak bt-2026-05-18-185740 PASS): with NO env set
    # the section now renders by default (was empty pre-graduation).
    root = _repo_with_memory(tmp_path, {"a.md": ("# A\nbody", 1000.0)})
    assert "## Recent Developer Memory" in (
        await _svc(root)._render_dev_memory_section()
    )


async def test_explicit_disable_hot_reverts_to_empty(monkeypatch, tmp_path):
    # Hot-revert is env=false ONLY (no code path deleted).
    monkeypatch.setenv(_FLAG, "false")
    root = _repo_with_memory(tmp_path, {"a.md": ("# A\nbody", 1000.0)})
    assert await _svc(root)._render_dev_memory_section() == ""


def test_ast_pin_graduated_default_true_persists():
    """Graduation must not silently revert: the in-code env default
    for JARVIS_STRATEGIC_DEV_MEMORY_ENABLED must be 'true'."""
    src = _SRC.read_text()
    tree = ast.parse(src)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_render_dev_memory_section"
    )
    for call in ast.walk(fn):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "get"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == "JARVIS_STRATEGIC_DEV_MEMORY_ENABLED"
        ):
            assert (
                len(call.args) >= 2
                and isinstance(call.args[1], ast.Constant)
                and call.args[1].value == "true"
            ), (
                "graduated default must persist as 'true' (env=false "
                "hot-reverts; no code-path deletion)"
            )
            return
    pytest.fail("env-default read for the dev-memory flag not found")


async def test_enabled_injects_memory(monkeypatch, tmp_path):
    monkeypatch.setenv(_FLAG, "true")
    root = _repo_with_memory(
        tmp_path, {"plan.md": ("# Big Plan\nthe summary text", 1000.0)}
    )
    block = await _svc(root)._render_dev_memory_section()
    assert "## Recent Developer Memory" in block
    assert "Big Plan" in block
    assert "the summary text" in block
    assert "memory/plan.md" in block


async def test_recency_ranked_newest_first(monkeypatch, tmp_path):
    monkeypatch.setenv(_FLAG, "true")
    root = _repo_with_memory(tmp_path, {
        "old.md": ("# OLD\nold body", 1_000.0),
        "new.md": ("# NEW\nnew body", 9_000.0),
        "mid.md": ("# MID\nmid body", 5_000.0),
    })
    block = await _svc(root)._render_dev_memory_section()
    assert block.index("NEW") < block.index("MID") < block.index("OLD")


async def test_max_files_cap(monkeypatch, tmp_path):
    monkeypatch.setenv(_FLAG, "true")
    monkeypatch.setenv("JARVIS_STRATEGIC_DEV_MEMORY_MAX_FILES", "2")
    root = _repo_with_memory(tmp_path, {
        f"f{i}.md": (f"# T{i}\nb{i}", float(1000 + i)) for i in range(6)
    })
    block = await _svc(root)._render_dev_memory_section()
    assert block.count("### ") == 2  # only 2 most-recent folded in


async def test_char_budget_cap(monkeypatch, tmp_path):
    monkeypatch.setenv(_FLAG, "true")
    monkeypatch.setenv("JARVIS_STRATEGIC_DEV_MEMORY_MAX_CHARS", "40")
    root = _repo_with_memory(tmp_path, {
        "a.md": ("# A\n" + "x" * 500, 2000.0),
        "b.md": ("# B\n" + "y" * 500, 1000.0),
    })
    block = await _svc(root)._render_dev_memory_section()
    # Tiny budget: at most the single newest entry, never both.
    assert block.count("### ") <= 1


async def test_fail_silent_on_crawler_error(monkeypatch, tmp_path):
    monkeypatch.setenv(_FLAG, "true")
    import backend.core.ouroboros.roadmap.source_crawlers as sc

    def _boom(_root):
        raise RuntimeError("crawler exploded")

    monkeypatch.setattr(sc, "crawl_memory", _boom)
    root = _repo_with_memory(tmp_path, {"a.md": ("# A\nb", 1.0)})
    assert await _svc(root)._render_dev_memory_section() == ""  # no raise


async def test_authority_free_disclaimer(monkeypatch, tmp_path):
    monkeypatch.setenv(_FLAG, "true")
    root = _repo_with_memory(tmp_path, {"a.md": ("# A\nbody", 1.0)})
    block = await _svc(root)._render_dev_memory_section()
    low = block.lower()
    assert "advisory" in low
    assert "no authority" in low or "carries no authority" in low


async def test_wired_into_format_for_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv(_FLAG, "true")
    root = _repo_with_memory(
        tmp_path, {"wired.md": ("# WiredDoc\ncontent here", 1234.0)}
    )
    out = await _svc(root).format_for_prompt()
    assert "## Recent Developer Memory" in out
    assert "WiredDoc" in out


async def test_empty_when_no_memory_dir(monkeypatch, tmp_path):
    monkeypatch.setenv(_FLAG, "true")
    assert await _svc(tmp_path)._render_dev_memory_section() == ""  # no memory/ dir


# --------------------------------------------------------------------------
# AST pin — composes crawl_memory, never re-globs memory/ itself
# --------------------------------------------------------------------------

def test_ast_pin_composes_crawler_no_duplicate_glob():
    src = _SRC.read_text()
    tree = ast.parse(src)
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_render_dev_memory_section"
    )
    body = ast.unparse(node)
    assert "crawl_memory" in body, "must compose the existing crawler"
    # Must NOT duplicate the glob logic the crawler already owns.
    assert '"memory"' not in body and "'memory'" not in body, (
        "must not re-derive the memory dir / re-glob — that is "
        "crawl_memory's single responsibility"
    )
    assert ".glob(" not in body and ".rglob(" not in body

    fmt = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "format_for_prompt"
    )
    assert "_render_dev_memory_section" in ast.unparse(fmt), (
        "dev-memory section must be wired into format_for_prompt"
    )


# ---------------------------------------------------------------------------
# Slice 0 — injection telemetry (graduation observability, counts-only)
# ---------------------------------------------------------------------------

_STRAT_LOGGER = "backend.core.ouroboros.governance.strategic_direction"


async def test_telemetry_info_fires_when_injected(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv(_FLAG, "true")
    root = _repo_with_memory(
        tmp_path, {"p.md": ("# Plan\nSECRET_BODY_TOKEN summary", 1000.0)}
    )
    with caplog.at_level(logging.INFO, logger=_STRAT_LOGGER):
        await _svc(root)._render_dev_memory_section(op_id="op-T1")
    line = next(
        (r.getMessage() for r in caplog.records
         if "dev-memory injected" in r.getMessage()), None,
    )
    assert line is not None, "INFO telemetry must fire on injection"
    assert "op=op-T1" in line
    assert "files=1" in line and "chars=" in line


async def test_telemetry_counts_only_no_body_text(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv(_FLAG, "true")
    root = _repo_with_memory(
        tmp_path, {"p.md": ("# TitleTok\nSECRET_BODY_TOKEN", 1000.0)}
    )
    with caplog.at_level(logging.INFO, logger=_STRAT_LOGGER):
        await _svc(root)._render_dev_memory_section(op_id="op-T2")
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRET_BODY_TOKEN" not in blob, "must not log summary body"
    assert "TitleTok" not in blob, "must not log fragment title"
    assert "p.md" not in blob, "must not log fragment uri/path"


async def test_telemetry_silent_when_flag_off(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv(_FLAG, "false")  # graduated default-true now
    root = _repo_with_memory(tmp_path, {"a.md": ("# A\nb", 1.0)})
    with caplog.at_level(logging.INFO, logger=_STRAT_LOGGER):
        await _svc(root)._render_dev_memory_section(op_id="op-T3")
    assert not any(
        "dev-memory injected" in r.getMessage() for r in caplog.records
    )


async def test_telemetry_silent_when_no_memory(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv(_FLAG, "true")
    with caplog.at_level(logging.INFO, logger=_STRAT_LOGGER):
        await _svc(tmp_path)._render_dev_memory_section(op_id="op-T4")
    assert not any(
        "dev-memory injected" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# fs-hot-tier Phase 2 (2026-07-02) — crawl_memory routes through
# cooperative_fs_io.offload(cpu_bound=True) instead of running
# synchronously on the loop thread (per-file read + SHA-256 + title
# scan -- CPU-after-scan work).
# ---------------------------------------------------------------------------


def _slow_crawl_memory(repo_root: Path):
    """Module-level (picklable) slow crawl stand-in -- see the
    analogous helper in test_strategic_rust_map.py for why this
    can't be a test-local closure under cpu_bound=True."""
    import time as _time_mod

    from backend.core.ouroboros.roadmap.source_crawlers import (
        crawl_memory as _real_crawl_memory,
    )
    _time_mod.sleep(0.3)
    return _real_crawl_memory(repo_root)


async def test_dev_memory_section_routes_crawl_through_offload_substrate(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(_FLAG, "true")
    root = _repo_with_memory(tmp_path, {"a.md": ("# A\nbody", 1000.0)})
    from backend.core.ouroboros.governance import cooperative_fs_io

    calls = {"n": 0, "cpu_bound": None}
    real_offload = cooperative_fs_io.offload

    async def _spy_offload(fn, *args, cpu_bound=False, **kwargs):
        import backend.core.ouroboros.roadmap.source_crawlers as sc
        if fn is sc.crawl_memory:
            calls["n"] += 1
            calls["cpu_bound"] = cpu_bound
        return await real_offload(fn, *args, cpu_bound=cpu_bound, **kwargs)

    monkeypatch.setattr(cooperative_fs_io, "offload", _spy_offload)
    block = await _svc(root)._render_dev_memory_section()

    assert calls["n"] == 1, (
        "crawl_memory must route through cooperative_fs_io.offload"
    )
    assert calls["cpu_bound"] is True, (
        "crawl_memory does CPU work (SHA-256 + title scan) per file "
        "after the glob -- must use the process pool"
    )
    assert "## Recent Developer Memory" in block


async def test_dev_memory_section_offload_error_degrades_to_empty_no_raise(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv(_FLAG, "true")
    root = _repo_with_memory(tmp_path, {"a.md": ("# A\nbody", 1.0)})
    from backend.core.ouroboros.governance import cooperative_fs_io
    from backend.core.ouroboros.governance.cooperative_fs_io import (
        OffloadError,
    )

    async def _boom_offload(fn, *args, **kwargs):
        return OffloadError(
            fn_name="crawl_memory",
            exc_type="PermissionError",
            message="synthetic offload-layer fault",
            cpu_bound=True,
        )

    monkeypatch.setattr(cooperative_fs_io, "offload", _boom_offload)
    block = await _svc(root)._render_dev_memory_section()
    assert block == ""


async def test_dev_memory_loop_stays_responsive_during_offloaded_crawl(
    monkeypatch, tmp_path,
):
    """End-to-end loop-responsiveness proof: a concurrent 50ms-cadence
    heartbeat must keep ticking while the offloaded memory crawl runs,
    even when it is artificially slow."""
    monkeypatch.setenv(_FLAG, "true")
    root = _repo_with_memory(tmp_path, {"a.md": ("# A\nbody", 1.0)})
    import backend.core.ouroboros.roadmap.source_crawlers as sc

    monkeypatch.setattr(sc, "crawl_memory", _slow_crawl_memory)

    ticks = 0
    running = True

    async def heartbeat():
        nonlocal ticks
        while running:
            ticks += 1
            await asyncio.sleep(0.05)

    hb_task = asyncio.create_task(heartbeat())
    await asyncio.sleep(0.01)
    before = ticks

    block = await _svc(root)._render_dev_memory_section()

    running = False
    await asyncio.sleep(0.01)
    hb_task.cancel()
    try:
        await hb_task
    except asyncio.CancelledError:
        pass

    during = ticks - before
    assert during >= 2, (
        f"Heartbeat starved during the dev-memory crawl: "
        f"ticks={during} over a 0.3s slow crawl"
    )
    assert "## Recent Developer Memory" in block


def test_ast_pin_telemetry_is_counts_only():
    """The logger.info call inside _render_dev_memory_section must
    carry only op_id + counts — never the block / joined / per-frag
    title / summary / uri (operator memory/ may be sensitive)."""
    tree = ast.parse(_SRC.read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_render_dev_memory_section"
    )
    info_calls = [
        c for c in ast.walk(fn)
        if isinstance(c, ast.Call)
        and isinstance(c.func, ast.Attribute)
        and c.func.attr == "info"
    ]
    assert info_calls, "expected a logger.info telemetry call"
    args_src = " ".join(ast.unparse(a) for c in info_calls for a in c.args)
    for forbidden in ("block", "joined", "summary", "title", "entry"):
        assert forbidden not in args_src, (
            "telemetry must not log fragment body: " + repr(forbidden)
        )
    assert "op_id" in args_src and "len(entries)" in args_src

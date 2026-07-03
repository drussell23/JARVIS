"""Priority-4 regression spine — Rust subsystem awareness map.

Option-1 (awareness only): make O+V *know* the native Rust crates
exist so it reaches for the language-agnostic Venom tools on `.rs`.
The Oracle structural graph stays Python-only — explicitly NOT
changed here. Composes the new
``roadmap.source_crawlers.crawl_rust_subsystems`` crawler (dynamic
Cargo.toml discovery, no hardcoded crate list) and surfaces it as an
advisory, authority-free ``## Rust Subsystems`` section.

Pins:
  * crawler: dynamic discovery, build-artifact/worktree exclusion,
    dedup-by-name, env cap, summary precedence, workspace-skip
  * section: default-False → empty; enabled → crate names + the
    mandated Oracle-Python-only note + authority-free disclaimer
  * budget caps; fail-silent if the crawler raises
  * wired into format_for_prompt (after dev-memory, before causal)
  * AST: composes crawl_rust_subsystems, NO Cargo.toml glob in
    strategic_direction.py
"""
from __future__ import annotations

import ast
import asyncio
import logging
import time
from pathlib import Path

import pytest

from backend.core.ouroboros.roadmap.source_crawlers import (
    crawl_rust_subsystems,
)
from backend.core.ouroboros.governance.strategic_direction import (
    StrategicDirectionService,
)

_SRC = (
    Path(__file__).resolve().parents[2]
    / "backend/core/ouroboros/governance/strategic_direction.py"
)
_FLAG = "JARVIS_STRATEGIC_RUST_MAP_ENABLED"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in (
        _FLAG,
        "JARVIS_STRATEGIC_RUST_MAX_CHARS",
        "JARVIS_STRATEGIC_RUST_MAX_CRATES",
        "JARVIS_STRATEGIC_RUST_SEARCH_ROOT",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def _crate(root: Path, rel: str, name: str, *, desc: str = "",
           readme: str | None = None) -> None:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    body = f'[package]\nname = "{name}"\n'
    if desc:
        body += f'description = "{desc}"\n'
    (d / "Cargo.toml").write_text(body, encoding="utf-8")
    if readme is not None:
        (d / "README.md").write_text(readme, encoding="utf-8")


def _svc(root: Path) -> StrategicDirectionService:
    s = StrategicDirectionService(root)
    s._digest = "PRINCIPLES"
    return s


def _slow_crawl_rust_subsystems(repo_root: Path):
    """Module-level (picklable-by-reference) slow crawl stand-in for
    the loop-responsiveness test below. MUST be module-level, not a
    test-local closure -- cpu_bound=True dispatches through a
    'spawn'-based ProcessPoolExecutor, which requires the target
    callable to be picklable by reference; a local closure would trip
    OffloadPicklingError before ever reaching the pool, which would
    make the responsiveness assertion vacuously true instead of
    actually proving the fix."""
    time.sleep(0.3)  # runs in a SEPARATE OS process -- never the loop thread
    return crawl_rust_subsystems(repo_root)


# ---------------------------------------------------------------------------
# Crawler
# ---------------------------------------------------------------------------

def test_crawler_empty_when_no_search_root(tmp_path):
    assert crawl_rust_subsystems(tmp_path) == []


def test_crawler_discovers_crates_dynamically(tmp_path):
    _crate(tmp_path, "backend/a", "crate_a", desc="Crate A desc")
    _crate(tmp_path, "backend/sub/b", "crate_b",
           readme="# B\nB readme first line")
    frags = crawl_rust_subsystems(tmp_path)
    names = {f.title for f in frags}
    assert names == {"crate_a", "crate_b"}
    assert all(f.fragment_type == "rust_crate" for f in frags)
    assert all(f.source_id.startswith("rust:") for f in frags)
    b = next(f for f in frags if f.title == "crate_b")
    assert b.summary == "B readme first line"  # README precedence
    a = next(f for f in frags if f.title == "crate_a")
    assert a.summary == "Crate A desc"  # description fallback


def test_crawler_skips_build_artifacts_and_worktrees(tmp_path):
    _crate(tmp_path, "backend/real", "real_crate")
    _crate(tmp_path, "backend/real/target/debug/build/x", "artifact")
    _crate(tmp_path, "backend/.worktrees/wt/c", "wt_crate")
    names = {f.title for f in crawl_rust_subsystems(tmp_path)}
    assert names == {"real_crate"}


def test_crawler_dedup_by_name(tmp_path):
    _crate(tmp_path, "backend/x", "dup")
    _crate(tmp_path, "backend/y", "dup")
    frags = crawl_rust_subsystems(tmp_path)
    assert [f.title for f in frags] == ["dup"]


def test_crawler_workspace_only_manifest_skipped(tmp_path):
    d = tmp_path / "backend" / "ws"
    d.mkdir(parents=True)
    (d / "Cargo.toml").write_text(
        '[workspace]\nmembers = ["a"]\n', encoding="utf-8"
    )
    assert crawl_rust_subsystems(tmp_path) == []


def test_crawler_max_crates_env_cap(monkeypatch, tmp_path):
    for i in range(5):
        _crate(tmp_path, f"backend/c{i}", f"crate{i}")
    monkeypatch.setenv("JARVIS_STRATEGIC_RUST_MAX_CRATES", "2")
    assert len(crawl_rust_subsystems(tmp_path)) == 2


def test_crawler_search_root_env(monkeypatch, tmp_path):
    _crate(tmp_path, "native/n", "native_crate")
    monkeypatch.setenv("JARVIS_STRATEGIC_RUST_SEARCH_ROOT", "native")
    assert {f.title for f in crawl_rust_subsystems(tmp_path)} == {
        "native_crate"
    }


# ---------------------------------------------------------------------------
# Strategic-direction section
# ---------------------------------------------------------------------------

async def test_graduated_default_true_injects_without_flag(tmp_path):
    # P4 GRADUATED (soak bt-2026-05-18-194040 PASS): with NO env set
    # the section now renders by default (was empty pre-graduation).
    _crate(tmp_path, "backend/a", "crate_a")
    assert "## Rust Subsystems" in (
        await _svc(tmp_path)._render_rust_subsystems_section()
    )


async def test_explicit_disable_hot_reverts_to_empty(monkeypatch, tmp_path):
    # Hot-revert is env=false ONLY (no code path deleted).
    monkeypatch.setenv(_FLAG, "false")
    _crate(tmp_path, "backend/a", "crate_a")
    assert await _svc(tmp_path)._render_rust_subsystems_section() == ""


def test_ast_pin_graduated_default_true_persists():
    """Graduation must not silently revert: the in-code env default
    for JARVIS_STRATEGIC_RUST_MAP_ENABLED must be 'true'."""
    tree = ast.parse(_SRC.read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_render_rust_subsystems_section"
    )
    for call in ast.walk(fn):
        if (
            isinstance(call, ast.Call)
            and isinstance(call.func, ast.Attribute)
            and call.func.attr == "get"
            and call.args
            and isinstance(call.args[0], ast.Constant)
            and call.args[0].value == "JARVIS_STRATEGIC_RUST_MAP_ENABLED"
        ):
            assert (
                len(call.args) >= 2
                and isinstance(call.args[1], ast.Constant)
                and call.args[1].value == "true"
            ), "graduated default must persist as 'true'"
            return
    pytest.fail("env-default read for the rust-map flag not found")


async def test_section_enabled_lists_crates_with_disclaimers(monkeypatch, tmp_path):
    monkeypatch.setenv(_FLAG, "true")
    _crate(tmp_path, "backend/core", "jarvis_core", desc="perf core")
    block = await _svc(tmp_path)._render_rust_subsystems_section()
    assert "## Rust Subsystems" in block
    assert "jarvis_core" in block
    low = block.lower()
    assert "advisory" in low and "no authority" in low
    # The mandated explicit Oracle-Python-only / Venom line.
    assert "oracle structural graph is python-only" in low
    assert "venom" in low and ".rs" in block


async def test_section_char_budget_cap(monkeypatch, tmp_path):
    monkeypatch.setenv(_FLAG, "true")
    monkeypatch.setenv("JARVIS_STRATEGIC_RUST_MAX_CHARS", "30")
    _crate(tmp_path, "backend/a", "aaaaa", desc="x" * 200)
    _crate(tmp_path, "backend/b", "bbbbb", desc="y" * 200)
    block = await _svc(tmp_path)._render_rust_subsystems_section()
    assert block.count("\n- **") <= 1


async def test_section_fail_silent_on_crawler_error(monkeypatch, tmp_path):
    monkeypatch.setenv(_FLAG, "true")
    import backend.core.ouroboros.roadmap.source_crawlers as sc
    monkeypatch.setattr(
        sc, "crawl_rust_subsystems",
        lambda _r: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    assert await _svc(tmp_path)._render_rust_subsystems_section() == ""


async def test_wired_into_format_for_prompt(monkeypatch, tmp_path):
    monkeypatch.setenv(_FLAG, "true")
    _crate(tmp_path, "backend/w", "wired_crate", desc="d")
    out = await _svc(tmp_path).format_for_prompt()
    assert "## Rust Subsystems" in out
    assert "wired_crate" in out


# ---------------------------------------------------------------------------
# AST pin
# ---------------------------------------------------------------------------

def test_ast_pin_composes_crawler_no_glob_in_strategic_direction():
    src = _SRC.read_text()
    tree = ast.parse(src)
    node = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_render_rust_subsystems_section"
    )
    body = ast.unparse(node)
    assert "crawl_rust_subsystems" in body, "must compose the crawler"
    # The real invariant: NO crate-discovery glob CALL here — the
    # crawler owns that. (A docstring may mention 'Cargo.toml' as
    # prose; the pin must test the call pattern, not the word.)
    assert ".rglob(" not in body and ".glob(" not in body
    for bad in (
        'rglob("Cargo.toml")', "rglob('Cargo.toml')",
        'glob("Cargo.toml")', "glob('Cargo.toml')",
    ):
        assert bad not in src, (
            f"strategic_direction.py must not {bad} — that is "
            f"crawl_rust_subsystems' single responsibility"
        )

    fmt = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "format_for_prompt"
    )
    fmt_src = ast.unparse(fmt)
    assert "_render_rust_subsystems_section" in fmt_src
    # Recency design: rust map after dev-memory, before causal lineage.
    assert (
        fmt_src.index("_render_dev_memory_section")
        < fmt_src.index("_render_rust_subsystems_section")
        < fmt_src.index("_render_causal_lineage_section")
    )


# ---------------------------------------------------------------------------
# Slice 0 — injection telemetry (graduation observability, counts-only)
# ---------------------------------------------------------------------------

_STRAT_LOGGER = "backend.core.ouroboros.governance.strategic_direction"


async def test_telemetry_info_fires_when_injected(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv(_FLAG, "true")
    _crate(tmp_path, "backend/c", "secret_crate_name",
           desc="SECRET_RUST_TOKEN")
    with caplog.at_level(logging.INFO, logger=_STRAT_LOGGER):
        await _svc(tmp_path)._render_rust_subsystems_section(op_id="op-R1")
    line = next(
        (r.getMessage() for r in caplog.records
         if "rust-map injected" in r.getMessage()), None,
    )
    assert line is not None, "INFO telemetry must fire on injection"
    assert "op=op-R1" in line
    assert "crates=1" in line and "chars=" in line


async def test_telemetry_counts_only_no_body_text(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv(_FLAG, "true")
    _crate(tmp_path, "backend/c", "secret_crate_name",
           desc="SECRET_RUST_TOKEN")
    with caplog.at_level(logging.INFO, logger=_STRAT_LOGGER):
        await _svc(tmp_path)._render_rust_subsystems_section(op_id="op-R2")
    blob = "\n".join(r.getMessage() for r in caplog.records)
    assert "SECRET_RUST_TOKEN" not in blob, "must not log crate summary"
    assert "secret_crate_name" not in blob, "must not log crate name"
    assert "backend/c" not in blob, "must not log crate path"


async def test_telemetry_silent_when_flag_off(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv(_FLAG, "false")  # graduated default-true now
    _crate(tmp_path, "backend/c", "crate_a")
    with caplog.at_level(logging.INFO, logger=_STRAT_LOGGER):
        await _svc(tmp_path)._render_rust_subsystems_section(op_id="op-R3")
    assert not any(
        "rust-map injected" in r.getMessage() for r in caplog.records
    )


async def test_telemetry_silent_when_no_crates(monkeypatch, tmp_path, caplog):
    monkeypatch.setenv(_FLAG, "true")
    with caplog.at_level(logging.INFO, logger=_STRAT_LOGGER):
        await _svc(tmp_path)._render_rust_subsystems_section(op_id="op-R4")
    assert not any(
        "rust-map injected" in r.getMessage() for r in caplog.records
    )


# ---------------------------------------------------------------------------
# fs-hot-tier Phase 2 (2026-07-02) — THE #1 FIX. crawl_rust_subsystems
# routes through cooperative_fs_io.offload(cpu_bound=True) instead of
# running synchronously on the loop thread (the CONFIRMED 41s
# blocker). Substrate routing, correctness parity, fail-soft, and an
# end-to-end loop-responsiveness proof on this — the HOTTEST — path.
# ---------------------------------------------------------------------------


async def test_rust_section_routes_crawl_through_offload_substrate(
    monkeypatch, tmp_path,
):
    """Spy on cooperative_fs_io.offload — the crawl must be dispatched
    through the substrate with cpu_bound=True (process pool, since the
    walk is followed by tomllib/regex parsing — CPU work a thread
    wouldn't free the GIL for)."""
    monkeypatch.setenv(_FLAG, "true")
    _crate(tmp_path, "backend/a", "crate_a", desc="d")
    from backend.core.ouroboros.governance import cooperative_fs_io

    calls = {"n": 0, "cpu_bound": None}
    real_offload = cooperative_fs_io.offload

    async def _spy_offload(fn, *args, cpu_bound=False, **kwargs):
        if fn is crawl_rust_subsystems:
            calls["n"] += 1
            calls["cpu_bound"] = cpu_bound
        return await real_offload(fn, *args, cpu_bound=cpu_bound, **kwargs)

    monkeypatch.setattr(cooperative_fs_io, "offload", _spy_offload)
    block = await _svc(tmp_path)._render_rust_subsystems_section()

    assert calls["n"] == 1, (
        "crawl_rust_subsystems must route through "
        "cooperative_fs_io.offload"
    )
    assert calls["cpu_bound"] is True, (
        "crawl_rust_subsystems does CPU work (tomllib/regex parse) "
        "after the walk -- must use the process pool, not a thread"
    )
    assert "crate_a" in block


async def test_rust_section_offload_error_degrades_to_empty_no_raise(
    monkeypatch, tmp_path,
):
    """Fail-soft: an OffloadError from the substrate must degrade to
    the same empty-section result as 'no crates found' -- never
    raise into prompt composition."""
    monkeypatch.setenv(_FLAG, "true")
    _crate(tmp_path, "backend/a", "crate_a")
    from backend.core.ouroboros.governance import cooperative_fs_io
    from backend.core.ouroboros.governance.cooperative_fs_io import (
        OffloadError,
    )

    async def _boom_offload(fn, *args, **kwargs):
        return OffloadError(
            fn_name="crawl_rust_subsystems",
            exc_type="PermissionError",
            message="synthetic offload-layer fault",
            cpu_bound=True,
        )

    monkeypatch.setattr(cooperative_fs_io, "offload", _boom_offload)
    block = await _svc(tmp_path)._render_rust_subsystems_section()
    assert block == ""


async def test_rust_section_loop_stays_responsive_during_offloaded_crawl(
    monkeypatch, tmp_path,
):
    """End-to-end loop-responsiveness proof on the HOTTEST path: a
    concurrent 50ms-cadence heartbeat must keep ticking while the
    offloaded rust crawl runs, even when the crawl is artificially
    slow. This is the direct regression pin for the CONFIRMED 41s
    event-loop stall (crawl_rust_subsystems on the loop thread)."""
    monkeypatch.setenv(_FLAG, "true")
    _crate(tmp_path, "backend/a", "crate_a")

    import backend.core.ouroboros.roadmap.source_crawlers as sc

    monkeypatch.setattr(
        sc, "crawl_rust_subsystems", _slow_crawl_rust_subsystems,
    )

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

    block = await _svc(tmp_path)._render_rust_subsystems_section()

    running = False
    await asyncio.sleep(0.01)
    hb_task.cancel()
    try:
        await hb_task
    except asyncio.CancelledError:
        pass

    during = ticks - before
    assert during >= 2, (
        f"Heartbeat starved during the rust-subsystems crawl: "
        f"ticks={during} over a 0.3s slow crawl -- the offload fix "
        "failed and the crawl is still blocking the event loop"
    )
    assert "crate_a" in block


def test_ast_pin_telemetry_is_counts_only():
    tree = ast.parse(_SRC.read_text())
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and n.name == "_render_rust_subsystems_section"
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

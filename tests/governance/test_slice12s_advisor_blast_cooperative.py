"""Slice 12S — Advisor Blast-Radius Scan Async Hardening.

bt-2026-05-23-171810 surfaced a fresh wedge after Slice 12P+12R proved
in production: the Advisor's blast scan, even when dispatched to its
dedicated ``advisor-blast`` ThreadPoolExecutor, starved the asyncio
loop because the substring-match step is pure-Python and held the GIL
between read() syscalls. ``ControlPlaneStarvation lag_ms=32896.7`` was
recorded during an 84s scan; ``LoopDeadman`` tripped at 302.2s.

Slice 12S refactors the scan to be GENUINELY cooperative on the
asyncio loop using the existing
:func:`event_loop_governance.cooperative_yield_every_n_async` +
:func:`event_loop_governance.offload_blocking` primitives — the single
source of truth for cooperative yielding (Task #102). No new threading
mechanism, no new bounding primitive, no truncation, no timeout
tweaks; the actual *blocking nature* of the work is solved.

This test file pins:

* **Non-blocking proof.** A heartbeat coroutine running concurrently
  with the cooperative scan increments a counter every 1ms. Across
  the scan duration the counter must increase by a non-trivial
  margin — proving the loop ticked while the scan ran. Contrasts
  with the legacy thread-pool path where the heartbeat is starved.
* **Result parity.** For the same ``(target_files, scan_root)``
  inputs over the same on-disk files, the cooperative async scan
  returns the same integer as the legacy sync scan.
* **Cache parity.** The cooperative path uses the same module-level
  shared cache + TTL semantics as the sync path — a sync-computed
  result becomes a cooperative-path cache hit and vice versa.
* **Oracle shortcut parity.** When the Oracle-graph blast path is
  enabled and a count is available, the cooperative method returns
  the Oracle count without scanning — same behavior as sync.
* **Empty / no-target shortcut.** Same fast-path return as sync.
* **Cap shortcut.** When the importer count reaches the
  conservative cap mid-scan, the cooperative method breaks early
  with the actual count (NOT exhaustion bias).
* **Master-switch off.** ``advise_async`` falls back to the legacy
  ``run_in_executor`` path verbatim — byte-identical rollback.
* **AST pins.** The new async method must import + call
  ``cooperative_yield_every_n_async`` and ``offload_blocking`` from
  the canonical ``event_loop_governance`` module. The
  ``_precomputed_blast_radius`` injection seam must live on
  :meth:`advise`. The cooperative dispatch must be the FIRST
  executable statement in :meth:`advise_async` (so a legacy-path
  refactor cannot silently bypass it).
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import time
from pathlib import Path
from typing import Tuple

import pytest

from backend.core.ouroboros.governance import operation_advisor
from backend.core.ouroboros.governance.operation_advisor import (
    ADVISOR_BLAST_COOPERATIVE_ENABLED_ENV_VAR,
    OperationAdvisor,
    _advisor_blast_cooperative_enabled,
    _BLAST_RADIUS_CACHE_SHARED,
)


# ──────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_blast_cache():
    """The module-level shared cache must not leak between tests."""
    _BLAST_RADIUS_CACHE_SHARED.clear()
    yield
    _BLAST_RADIUS_CACHE_SHARED.clear()


@pytest.fixture(autouse=True)
def _reset_oracle():
    """Oracle module-level reference must not leak between tests."""
    saved = operation_advisor._active_oracle
    operation_advisor._active_oracle = None
    yield
    operation_advisor._active_oracle = saved


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Build a small repo: 30 .py files, half of which import the
    target module. Enough to span >1 yield cadence (default N=64 is
    higher, but the test forces N=4 via the env knob)."""
    target = tmp_path / "mypkg" / "target.py"
    target.parent.mkdir(parents=True)
    target.write_text("def hello(): return 42\n")

    # 15 importers
    for i in range(15):
        src = tmp_path / f"importer_{i:02d}.py"
        src.write_text(f"from mypkg.target import hello\n# importer {i}\n")
    # 15 non-importers
    for i in range(15):
        src = tmp_path / f"unrelated_{i:02d}.py"
        src.write_text(f"# unrelated file {i}\nprint('hi')\n")

    return tmp_path


def _make_advisor(repo: Path) -> OperationAdvisor:
    """Build an advisor pinned to ``repo`` as its scan root."""
    return OperationAdvisor(project_root=repo)


# ──────────────────────────────────────────────────────────────────────
# Master switch + module shape pins
# ──────────────────────────────────────────────────────────────────────


class TestMasterSwitch:
    def test_default_is_true(self, monkeypatch):
        """Slice 12S default-on per graduation policy."""
        monkeypatch.delenv(
            ADVISOR_BLAST_COOPERATIVE_ENABLED_ENV_VAR, raising=False,
        )
        assert _advisor_blast_cooperative_enabled() is True

    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("true", True), ("True", True), ("1", True),
            ("yes", True), ("on", True),
            ("false", False), ("0", False), ("no", False),
            ("off", False), ("garbage", False),
        ],
    )
    def test_truthy_values(self, monkeypatch, raw, expected):
        monkeypatch.setenv(
            ADVISOR_BLAST_COOPERATIVE_ENABLED_ENV_VAR, raw,
        )
        assert _advisor_blast_cooperative_enabled() is expected


class TestModuleShape:
    """Pin the public surface the harness depends on."""

    def test_async_methods_exist_and_are_coroutines(self):
        adv = OperationAdvisor(project_root=Path("."))
        assert hasattr(adv, "_advise_async_cooperative")
        assert hasattr(adv, "_compute_blast_radius_async")
        assert asyncio.iscoroutinefunction(
            adv._advise_async_cooperative,
        )
        assert asyncio.iscoroutinefunction(
            adv._compute_blast_radius_async,
        )

    def test_advise_accepts_precomputed_blast_kwarg(self):
        """The composition seam between cooperative + sync must
        exist. Pinned because external callers must not depend on
        this internal kwarg, but the cooperative path needs it."""
        sig = inspect.signature(OperationAdvisor.advise)
        assert "_precomputed_blast_radius" in sig.parameters
        # Must default to None to preserve byte-identical legacy
        # behavior when external callers omit it.
        assert (
            sig.parameters["_precomputed_blast_radius"].default
            is None
        )


# ──────────────────────────────────────────────────────────────────────
# Result + cache + shortcut parity
# ──────────────────────────────────────────────────────────────────────


class TestResultParity:
    """The cooperative path must produce the same integer as the
    sync path for the same inputs."""

    @pytest.mark.asyncio
    async def test_sync_and_async_return_same_count(
        self, fake_repo, monkeypatch,
    ):
        # Make sure the cache doesn't short-circuit one of them.
        _BLAST_RADIUS_CACHE_SHARED.clear()
        adv = _make_advisor(fake_repo)
        target_files: Tuple[str, ...] = ("mypkg/target.py",)

        sync_count = adv._compute_blast_radius(
            target_files, root=fake_repo,
        )
        _BLAST_RADIUS_CACHE_SHARED.clear()
        async_count = await adv._compute_blast_radius_async(
            target_files, root=fake_repo,
        )
        assert sync_count == async_count
        # 15 importers planted in the fixture.
        assert sync_count == 15

    @pytest.mark.asyncio
    async def test_async_writes_cache_visible_to_sync(self, fake_repo):
        adv = _make_advisor(fake_repo)
        target_files: Tuple[str, ...] = ("mypkg/target.py",)
        async_count = await adv._compute_blast_radius_async(
            target_files, root=fake_repo,
        )
        # Sync call should now be a cache hit — same value, no
        # re-scan. We can't directly assert it didn't scan, but the
        # cache contract is identical so the value MUST match
        # without bypass.
        sync_count = adv._compute_blast_radius(
            target_files, root=fake_repo,
        )
        assert sync_count == async_count

    @pytest.mark.asyncio
    async def test_sync_writes_cache_visible_to_async(self, fake_repo):
        adv = _make_advisor(fake_repo)
        target_files: Tuple[str, ...] = ("mypkg/target.py",)
        sync_count = adv._compute_blast_radius(
            target_files, root=fake_repo,
        )
        async_count = await adv._compute_blast_radius_async(
            target_files, root=fake_repo,
        )
        assert sync_count == async_count

    @pytest.mark.asyncio
    async def test_empty_targets_fast_path(self, fake_repo):
        adv = _make_advisor(fake_repo)
        # No .py files in target_files → target_modules empty → 0.
        result = await adv._compute_blast_radius_async(
            ("README.md",), root=fake_repo,
        )
        assert result == 0


# ──────────────────────────────────────────────────────────────────────
# Non-blocking proof — the load-bearing claim of Slice 12S
# ──────────────────────────────────────────────────────────────────────


class TestNonBlockingBehavior:
    """The whole point of Slice 12S: the asyncio loop must tick
    *during* the scan, not just before/after."""

    @pytest.mark.asyncio
    async def test_heartbeat_ticks_during_cooperative_scan(
        self, fake_repo, monkeypatch,
    ):
        """A concurrent heartbeat coroutine must accumulate ticks
        while the scan runs. With the legacy thread-pool path this
        wedged for the duration of the scan; cooperative_yield
        guarantees the loop gets scheduling slots throughout."""

        # Force aggressive yield cadence so a small fixture
        # (30 files) still triggers multiple yields. Without this
        # the default 64 would never trigger on a 30-file scan.
        monkeypatch.setenv("JARVIS_EVENT_LOOP_YIELD_EVERY_N", "4")

        ticks = 0
        scan_running = True

        async def heartbeat():
            nonlocal ticks
            while scan_running:
                ticks += 1
                await asyncio.sleep(0.001)

        adv = _make_advisor(fake_repo)
        hb_task = asyncio.create_task(heartbeat())
        # Give the heartbeat one scheduling slot before the scan.
        await asyncio.sleep(0.01)
        ticks_before_scan = ticks

        result = await adv._compute_blast_radius_async(
            ("mypkg/target.py",), root=fake_repo,
        )

        scan_running = False
        await asyncio.sleep(0.01)
        hb_task.cancel()
        try:
            await hb_task
        except asyncio.CancelledError:
            pass

        ticks_during_scan = ticks - ticks_before_scan
        # The cooperative path MUST yield enough that at least a few
        # heartbeat ticks land during the scan. On a 30-file fixture
        # with N=4 yield cadence we expect ~7 yields → at least
        # several heartbeat ticks. Even a single tick proves the
        # loop wasn't wedged.
        assert ticks_during_scan >= 2, (
            f"Heartbeat starved during cooperative scan: "
            f"ticks={ticks_during_scan} result={result}"
        )

    @pytest.mark.asyncio
    async def test_cooperative_yield_primitive_invoked(
        self, fake_repo, monkeypatch,
    ):
        """Direct telemetry: count how many times the canonical
        ``cooperative_yield_every_n_async`` primitive is called from
        ``_compute_blast_radius_async``. Must be >=1."""
        # Force the cooperative cadence small enough that any scan
        # over the fake repo crosses a yield boundary.
        monkeypatch.setenv("JARVIS_EVENT_LOOP_YIELD_EVERY_N", "2")

        from backend.core.ouroboros.governance import (
            event_loop_governance as elg,
        )
        call_count = {"n": 0}
        original = elg.cooperative_yield_every_n_async

        def _spy(iterable, **kwargs):
            call_count["n"] += 1
            return original(iterable, **kwargs)

        # Re-bind at the call site (operation_advisor imports it
        # at function scope) — patch on the source module so the
        # next import picks up the spy.
        monkeypatch.setattr(
            "backend.core.ouroboros.governance.event_loop_governance."
            "cooperative_yield_every_n_async",
            _spy,
        )

        adv = _make_advisor(fake_repo)
        await adv._compute_blast_radius_async(
            ("mypkg/target.py",), root=fake_repo,
        )
        assert call_count["n"] >= 1, (
            "cooperative_yield_every_n_async was never invoked — "
            "Slice 12S scan is NOT using the governance primitive"
        )

    @pytest.mark.asyncio
    async def test_offload_blocking_primitive_invoked(
        self, fake_repo, monkeypatch,
    ):
        """Direct telemetry: every per-file read in the cooperative
        scan must go through ``offload_blocking`` so the GIL is
        released during the read+decode work."""
        from backend.core.ouroboros.governance import (
            event_loop_governance as elg,
        )
        call_count = {"n": 0}
        original = elg.offload_blocking

        async def _spy(fn, *args, **kwargs):
            call_count["n"] += 1
            return await original(fn, *args, **kwargs)

        monkeypatch.setattr(
            "backend.core.ouroboros.governance.event_loop_governance."
            "offload_blocking",
            _spy,
        )

        adv = _make_advisor(fake_repo)
        await adv._compute_blast_radius_async(
            ("mypkg/target.py",), root=fake_repo,
        )
        # 30 .py files in fixture — every read goes through
        # offload_blocking. (Some non-.py paths may also walk past
        # but they're filtered before the read call.)
        assert call_count["n"] >= 15, (
            f"offload_blocking called only {call_count['n']} times — "
            "Slice 12S scan is bypassing the governance primitive"
        )


# ──────────────────────────────────────────────────────────────────────
# Conservative-cap shortcut + budget-exhaustion preservation
# ──────────────────────────────────────────────────────────────────────


class TestCapAndBudget:
    @pytest.mark.asyncio
    async def test_cap_hit_returns_actual_count_not_exhausted(
        self, fake_repo, monkeypatch,
    ):
        """When importers reach the conservative cap mid-scan the
        method must break early with the actual cap value — NOT
        the budget-exhausted bias path. This is the same semantic
        the sync method preserves; bug-prone because the loop
        condition + post-loop bias detection are intertwined."""
        # Lower the conservative cap so 15 importers triggers it.
        monkeypatch.setenv(
            "JARVIS_BLAST_RADIUS_CONSERVATIVE_CAP", "5",
        )
        adv = _make_advisor(fake_repo)
        result = await adv._compute_blast_radius_async(
            ("mypkg/target.py",), root=fake_repo,
        )
        # 5 was the cap; method must return exactly that, not 50
        # (the legacy default) or a higher exhaustion bias.
        assert result == 5

    @pytest.mark.asyncio
    async def test_repeated_call_uses_cache(self, fake_repo):
        adv = _make_advisor(fake_repo)
        target_files: Tuple[str, ...] = ("mypkg/target.py",)
        first = await adv._compute_blast_radius_async(
            target_files, root=fake_repo,
        )
        # Drop the fixture files mid-test — if the cache works the
        # second call returns the same value despite the FS being
        # gone (the call shouldn't even hit disk).
        for p in fake_repo.iterdir():
            if p.is_file():
                p.unlink()
        second = await adv._compute_blast_radius_async(
            target_files, root=fake_repo,
        )
        assert first == second


# ──────────────────────────────────────────────────────────────────────
# Oracle shortcut parity
# ──────────────────────────────────────────────────────────────────────


class TestOracleShortcut:
    @pytest.mark.asyncio
    async def test_oracle_count_returned_when_available(
        self, fake_repo, monkeypatch,
    ):
        """When Oracle is active + flag is on + Oracle reports a
        count, the cooperative path returns it without scanning
        (same as sync). Pinned because the Oracle shortcut sits
        before the heavy scan in both paths."""
        monkeypatch.setenv(
            "JARVIS_ADVISOR_ORACLE_BLAST_ENABLED", "true",
        )

        # Match the real Oracle ``get_blast_radius`` contract:
        # returns a BlastResult-shaped object with risk_level +
        # directly_affected + transitively_affected lists of NodeID
        # objects carrying a ``file_path`` attribute.
        class _FakeNodeID:
            def __init__(self, fp: str) -> None:
                self.file_path = fp

        class _FakeBlast:
            def __init__(self) -> None:
                self.risk_level = "low"
                self.directly_affected = [
                    _FakeNodeID(f"file_direct_{i}.py")
                    for i in range(4)
                ]
                self.transitively_affected = [
                    _FakeNodeID(f"file_trans_{i}.py")
                    for i in range(3)
                ]

        class _FakeOracle:
            def get_blast_radius(self, candidate):
                return _FakeBlast()

        operation_advisor._active_oracle = _FakeOracle()
        adv = _make_advisor(fake_repo)
        result = await adv._compute_blast_radius_async(
            ("mypkg/target.py",), root=fake_repo,
        )
        # 7 from Oracle — NOT 15 from FS scan. Proves the Oracle
        # shortcut fires before the cooperative iteration.
        assert result == 7


# ──────────────────────────────────────────────────────────────────────
# Master-switch off → legacy rollback path
# ──────────────────────────────────────────────────────────────────────


class TestLegacyRollback:
    """When the master switch is FALSE, ``advise_async`` must NOT
    invoke the cooperative path — it must fall through to the legacy
    ``run_in_executor`` path verbatim. This is the byte-identical
    rollback contract."""

    @pytest.mark.asyncio
    async def test_master_off_skips_cooperative_dispatch(
        self, fake_repo, monkeypatch,
    ):
        monkeypatch.setenv(
            ADVISOR_BLAST_COOPERATIVE_ENABLED_ENV_VAR, "false",
        )

        adv = _make_advisor(fake_repo)
        cooperative_calls = {"n": 0}
        original = adv._advise_async_cooperative

        async def _spy(*args, **kwargs):
            cooperative_calls["n"] += 1
            return await original(*args, **kwargs)

        # Patch the bound method via __dict__ assignment so the
        # spy intercepts only this advisor instance.
        adv._advise_async_cooperative = _spy  # type: ignore[method-assign]

        await adv.advise_async(
            ("mypkg/target.py",),
            "test description",
            "op-test",
        )
        assert cooperative_calls["n"] == 0, (
            "Master flag FALSE — cooperative path must NOT fire; "
            "legacy run_in_executor path must own the dispatch"
        )

    @pytest.mark.asyncio
    async def test_master_on_routes_through_cooperative(
        self, fake_repo, monkeypatch,
    ):
        monkeypatch.setenv(
            ADVISOR_BLAST_COOPERATIVE_ENABLED_ENV_VAR, "true",
        )

        adv = _make_advisor(fake_repo)
        cooperative_calls = {"n": 0}
        original = adv._advise_async_cooperative

        async def _spy(*args, **kwargs):
            cooperative_calls["n"] += 1
            return await original(*args, **kwargs)

        adv._advise_async_cooperative = _spy  # type: ignore[method-assign]

        await adv.advise_async(
            ("mypkg/target.py",),
            "test description",
            "op-test",
        )
        assert cooperative_calls["n"] == 1, (
            "Master flag TRUE — cooperative path must own the "
            "dispatch; legacy run_in_executor path must NOT fire"
        )


# ──────────────────────────────────────────────────────────────────────
# AST pins — drift-prevention
# ──────────────────────────────────────────────────────────────────────


class TestASTPins:
    """Structural pins prevent silent regressions from refactors.

    The async path's whole point is composition over the canonical
    governance primitives. If a future PR replaces those calls with
    homegrown sleeps or direct threads, these tests fail loudly."""

    def _read_source(self) -> str:
        return Path(
            "backend/core/ouroboros/governance/operation_advisor.py"
        ).read_text()

    def test_compute_blast_radius_async_uses_cooperative_yield(self):
        src = self._read_source()
        # Both the import and the call must appear inside
        # _compute_blast_radius_async — pinned via AST walk.
        tree = ast.parse(src)
        target_fn = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_compute_blast_radius_async"
            ):
                target_fn = node
                break
        assert target_fn is not None, (
            "_compute_blast_radius_async missing — Slice 12S core "
            "method was renamed/removed"
        )
        # Look for the call inside the function body.
        found = False
        for inner in ast.walk(target_fn):
            if isinstance(inner, ast.Call):
                fn = inner.func
                if (
                    isinstance(fn, ast.Name)
                    and fn.id == "cooperative_yield_every_n_async"
                ):
                    found = True
                    break
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "cooperative_yield_every_n_async"
                ):
                    found = True
                    break
        assert found, (
            "_compute_blast_radius_async does not call "
            "cooperative_yield_every_n_async — Slice 12S broken: "
            "the scan is no longer cooperative on the loop"
        )

    def test_compute_blast_radius_async_uses_offload_blocking(self):
        src = self._read_source()
        tree = ast.parse(src)
        target_fn = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "_compute_blast_radius_async"
            ):
                target_fn = node
                break
        assert target_fn is not None
        found = False
        for inner in ast.walk(target_fn):
            if isinstance(inner, ast.Call):
                fn = inner.func
                if (
                    isinstance(fn, ast.Name)
                    and fn.id == "offload_blocking"
                ):
                    found = True
                    break
                if (
                    isinstance(fn, ast.Attribute)
                    and fn.attr == "offload_blocking"
                ):
                    found = True
                    break
        assert found, (
            "_compute_blast_radius_async does not call "
            "offload_blocking — Slice 12S broken: per-file reads "
            "are running on the loop and holding the GIL"
        )

    def test_advise_async_dispatches_cooperative_first(self):
        """The cooperative dispatch block must be the FIRST
        executable statement inside ``advise_async``. Pinned so a
        future legacy-path refactor cannot silently move it below
        ``run_in_executor`` and re-introduce the wedge."""
        src = self._read_source()
        tree = ast.parse(src)
        target_fn = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.AsyncFunctionDef)
                and node.name == "advise_async"
            ):
                target_fn = node
                break
        assert target_fn is not None
        # Skip docstring if present (ast.Expr-Constant-str at idx 0).
        body = target_fn.body
        idx = 0
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            idx = 1
        # First non-docstring stmt must be the cooperative dispatch
        # `if _advisor_blast_cooperative_enabled(): return await ...`
        first_stmt = body[idx]
        assert isinstance(first_stmt, ast.If), (
            "First executable statement in advise_async is not a "
            "guard — Slice 12S cooperative dispatch displaced"
        )
        # The test condition must reference the master-flag accessor.
        cond_src = ast.unparse(first_stmt.test)
        assert "_advisor_blast_cooperative_enabled" in cond_src, (
            "First-statement guard does not consult the Slice 12S "
            "master flag — dispatch path broken"
        )
        # Body must contain `_advise_async_cooperative`.
        body_src = "".join(
            ast.unparse(stmt) for stmt in first_stmt.body
        )
        assert "_advise_async_cooperative" in body_src, (
            "Slice 12S guard does not delegate to "
            "_advise_async_cooperative on master-on"
        )

    def test_advise_kwarg_seam_exists(self):
        """The ``_precomputed_blast_radius`` injection seam on
        ``advise`` is the load-bearing composition point between
        the cooperative scan and the rest of the advisory math.
        Pinned because external callers must NOT depend on it
        (underscore prefix) but the cooperative path needs it."""
        src = self._read_source()
        assert "_precomputed_blast_radius" in src, (
            "Slice 12S composition seam removed from advise()"
        )
        # And the legacy in-line scan path must still exist for
        # external callers / master-off rollback.
        tree = ast.parse(src)
        advise_fn = None
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "advise"
            ):
                advise_fn = node
                break
        assert advise_fn is not None
        # The body must still contain a `_compute_blast_radius`
        # call (the sync scan), guarded by the precomputed check.
        body_text = ast.unparse(advise_fn)
        assert "_compute_blast_radius" in body_text, (
            "advise() no longer calls _compute_blast_radius even "
            "as a fallback — byte-identical rollback broken"
        )

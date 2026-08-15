"""Regression spine for `/reach` — the unmounted-feature detector's verb.

The detector itself is `surface_reachability` and is not re-tested here.
What is tested is the ratchet: that steady state is silent, that a
regression is loud, and that the failure modes of the baseline file cannot
turn a signal into a flood.
"""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance import reach_repl as rr


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_REACH_BASELINE_PATH",
                       str(tmp_path / "baseline.json"))
    yield


def _as_audit(reading):
    """Wrap any reading in the async shape `_audit` now has."""
    async def _run():
        return reading
    return _run


def _fake_audit(**kw):
    """An async stand-in for `_audit`, which is now off-loop.

    Mirrors the real contract rather than the old one: a sync fake against
    an async seam raises TypeError inside the code under test, and every
    assertion then fails for a reason unrelated to what it measures.
    """
    async def _run():
        return _reading(**kw)
    return _run


def _reading(asym=(), orph=(), scanned=100):
    class _M:
        def __init__(self, name): self.module = name; self.reached_by = ("attach",)
    class _R:
        surface_labels = ("daemon", "attach", "cockpit")
        modules = []
        def __init__(self): self.scanned = scanned
        def asymmetric(self): return [_M(n) for n in asym]
        def orphans(self): return [_M(n) for n in orph]
    return _R()


# -- the ratchet -----------------------------------------------------------


async def test_no_baseline_reports_nothing_rather_than_everything(monkeypatch):
    """A first run has not regressed — it has not yet been measured.
    Reporting the standing list as new is the 4:1 false-positive flood this
    design exists to avoid."""
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a", "b", "c")))
    d = (await rr.drift())
    assert d.baseline_exists is False
    assert d.new_asymmetric == () and d.new_orphans == ()


async def test_steady_state_is_silent(monkeypatch):
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a",)))
    (await rr.accept_baseline())
    d = (await rr.drift())
    assert d.regressed is False
    assert "unchanged" in (await rr.dispatch_reach_command("/reach")).text


async def test_a_newly_asymmetric_module_is_loud(monkeypatch):
    """The shape every unmounted feature had."""
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a",)))
    (await rr.accept_baseline())
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a", "newcomer")))
    d = (await rr.drift())
    assert d.regressed is True
    assert d.new_asymmetric == ("newcomer",)


async def test_a_newly_orphaned_module_is_loud(monkeypatch):
    monkeypatch.setattr(rr, "_audit", _fake_audit())
    (await rr.accept_baseline())
    monkeypatch.setattr(rr, "_audit", _fake_audit(orph=("dead",)))
    assert (await rr.drift()).new_orphans == ("dead",)


async def test_a_fixed_module_is_reported_as_resolved(monkeypatch):
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a", "b")))
    (await rr.accept_baseline())
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a",)))
    d = (await rr.drift())
    assert d.resolved == ("b",) and d.regressed is False


async def test_accepting_moves_the_ratchet(monkeypatch):
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a",)))
    (await rr.accept_baseline())
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a", "b")))
    assert (await rr.drift()).regressed is True
    (await rr.accept_baseline())
    assert (await rr.drift()).regressed is False


# -- baseline failure modes ------------------------------------------------


async def test_a_corrupt_baseline_is_treated_as_absent_not_empty(monkeypatch):
    """An empty baseline would report every asymmetric module as newly
    regressed, burying a real regression under a hundred false ones on the
    first boot after a disk fault."""
    rr.baseline_path().parent.mkdir(parents=True, exist_ok=True)
    rr.baseline_path().write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a", "b", "c")))
    d = (await rr.drift())
    assert d.baseline_exists is False
    assert d.new_asymmetric == ()


async def test_a_baseline_that_is_not_an_object_is_treated_as_absent(monkeypatch):
    rr.baseline_path().parent.mkdir(parents=True, exist_ok=True)
    rr.baseline_path().write_text("[1,2,3]", encoding="utf-8")
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a",)))
    assert (await rr.drift()).baseline_exists is False


async def test_the_baseline_is_published_atomically(monkeypatch):
    """A baseline half-written by a crash reads as corrupt on the next
    boot, which degrades to 'no baseline' and floods the operator exactly
    when they are already recovering.

    OBSERVED, not spelt. This asserted that `accept_baseline`'s SOURCE
    mentions `atomic_replace` — a SOURCE_ONLY test in the exact sense
    `source_assertion_audit` measures. It passed while saying nothing about
    whether the write was atomic, and it broke when the implementation
    correctly moved to the shared ratchet even though the property held.
    """
    from backend.core.ouroboros.governance import durable_io

    seen = []
    real = durable_io.atomic_replace
    monkeypatch.setattr(durable_io, "atomic_replace",
                        lambda tmp, dst: (seen.append((tmp, dst)),
                                          real(tmp, dst))[1])
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a",)))
    await rr.accept_baseline()
    assert seen, "the baseline was published without atomic_replace"
    tmp, dst = seen[0]
    assert dst == rr.baseline_path()
    assert not tmp.exists(), "the temp file survived the publish"
    assert json.loads(dst.read_text())["buckets"]["asymmetric"] == ["a"]


async def test_a_failing_audit_degrades_rather_than_raising(monkeypatch):
    async def _boom():
        raise RuntimeError("tree walk exploded")
    monkeypatch.setattr(rr, "_audit", _boom)
    d = (await rr.drift())
    assert d.error and d.regressed is False
    assert (await rr.dispatch_reach_command("/reach")).ok is False


# -- the watchdog ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_watchdog_is_silent_at_steady_state(monkeypatch, caplog):
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a",)))
    (await rr.accept_baseline())
    caplog.clear()
    await rr.run_watchdog()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


@pytest.mark.asyncio
async def test_the_watchdog_warns_on_regression(monkeypatch, caplog):
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a",)))
    (await rr.accept_baseline())
    monkeypatch.setattr(rr, "_audit", _fake_audit(asym=("a", "newcomer")))
    caplog.clear()
    await rr.run_watchdog()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings and "newcomer" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_the_watchdog_runs_off_the_event_loop():
    """The audit walks the module tree and parses every file — far too much
    work for the loop that also runs the organism.

    MEASURED, not spelt. The old assertion looked for `to_thread` in the
    source and would have kept passing had the call been deleted from a
    branch it never took — and it broke when the offload correctly moved to
    `surface_reachability.audit_async`, though the property was intact.
    Here the loop must actually keep ticking.
    """
    import asyncio

    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    hb = asyncio.create_task(heartbeat())
    await rr.run_watchdog()
    hb.cancel()
    assert ticks > 5, "the loop stalled for the length of the audit"


@pytest.mark.asyncio
async def test_the_watchdog_never_raises(monkeypatch):
    def _boom():
        raise RuntimeError("nope")
    monkeypatch.setattr(rr, "_audit", _boom)
    assert (await rr.run_watchdog()).error


@pytest.mark.asyncio
async def test_the_watchdog_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("JARVIS_REACH_WATCHDOG_ENABLED", "0")
    assert rr.watchdog_enabled() is False


# -- the verb --------------------------------------------------------------


def test_the_verb_is_auto_discoverable():
    """Named for the cage `repl_dispatch_registry` reads, so it becomes a
    verb without a registration table anyone can forget to update."""
    assert callable(rr.dispatch_reach_command)
    assert "reach" in rr.__verb_help__


async def test_help_is_reachable():
    assert "reachability" in (await rr.dispatch_reach_command("/reach help")).text


async def test_a_module_query_names_its_surfaces(monkeypatch):
    class _M:
        module = "backend.x.menu_bindings"
        reached_by = ("attach", "cockpit")
        def asymmetric(self, n): return True
        orphan = False
    class _R:
        surface_labels = ("daemon", "attach", "cockpit")
        modules = [_M()]
        scanned = 1
        def asymmetric(self): return [_M()]
        def orphans(self): return []
    monkeypatch.setattr(rr, "_audit", _as_audit(_R()))
    out = (await rr.dispatch_reach_command("/reach menu_bindings"))
    assert out.ok and "attach" in out.text and "cockpit" in out.text


async def test_an_unknown_module_refuses_rather_than_returning_nothing(monkeypatch):
    monkeypatch.setattr(rr, "_audit", _fake_audit())
    out = (await rr.dispatch_reach_command("/reach no_such_module_xyz"))
    assert out.ok is False and "no module matching" in out.text


def test_the_verb_adds_no_analysis_of_its_own():
    """Every number comes from surface_reachability.audit — a second
    implementation would be a second opinion about the same tree."""
    import pathlib
    src = pathlib.Path(rr.__file__).read_text(encoding="utf-8")
    # Structural, not spelt: the delegation is that `_audit` — the ONE seam
    # every path here reaches the numbers through — resolves out of
    # `surface_reachability`. Asserting the exact import line made this a
    # test of spelling, and it failed the moment the seam went async while
    # the property it names stayed true.
    assert "surface_reachability" in src
    import inspect
    assert "surface_reachability" in inspect.getsource(rr._audit)
    for banned in ("ast.parse", "importlib", "os.walk"):
        assert banned not in src


# -- the fix this detector was built to catch ------------------------------


def test_menu_bindings_is_now_reachable_from_the_attach_surface():
    """§28 C2, closed: the capability existed and was mounted only on the
    bipartite surface. This is the detector confirming its own finding is
    fixed."""
    from backend.core.ouroboros.battle_test.surface_reachability import audit
    reading = audit()
    hit = next((m for m in reading.modules
                if m.module.endswith("menu_bindings")), None)
    assert hit is not None, "menu_bindings vanished from the module tree"
    assert "attach" in hit.reached_by, (
        "menu_bindings is unreachable from the surface `ov attach` mounts")

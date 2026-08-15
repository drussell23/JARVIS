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


def test_no_baseline_reports_nothing_rather_than_everything(monkeypatch):
    """A first run has not regressed — it has not yet been measured.
    Reporting the standing list as new is the 4:1 false-positive flood this
    design exists to avoid."""
    monkeypatch.setattr(rr, "_audit", lambda: _reading(asym=("a", "b", "c")))
    d = rr.drift()
    assert d.baseline_exists is False
    assert d.new_asymmetric == () and d.new_orphans == ()


def test_steady_state_is_silent(monkeypatch):
    monkeypatch.setattr(rr, "_audit", lambda: _reading(asym=("a",)))
    rr.accept_baseline()
    d = rr.drift()
    assert d.regressed is False
    assert "unchanged" in rr.dispatch_reach_command("/reach").text


def test_a_newly_asymmetric_module_is_loud(monkeypatch):
    """The shape every unmounted feature had."""
    monkeypatch.setattr(rr, "_audit", lambda: _reading(asym=("a",)))
    rr.accept_baseline()
    monkeypatch.setattr(rr, "_audit", lambda: _reading(asym=("a", "newcomer")))
    d = rr.drift()
    assert d.regressed is True
    assert d.new_asymmetric == ("newcomer",)


def test_a_newly_orphaned_module_is_loud(monkeypatch):
    monkeypatch.setattr(rr, "_audit", lambda: _reading())
    rr.accept_baseline()
    monkeypatch.setattr(rr, "_audit", lambda: _reading(orph=("dead",)))
    assert rr.drift().new_orphans == ("dead",)


def test_a_fixed_module_is_reported_as_resolved(monkeypatch):
    monkeypatch.setattr(rr, "_audit", lambda: _reading(asym=("a", "b")))
    rr.accept_baseline()
    monkeypatch.setattr(rr, "_audit", lambda: _reading(asym=("a",)))
    d = rr.drift()
    assert d.resolved == ("b",) and d.regressed is False


def test_accepting_moves_the_ratchet(monkeypatch):
    monkeypatch.setattr(rr, "_audit", lambda: _reading(asym=("a",)))
    rr.accept_baseline()
    monkeypatch.setattr(rr, "_audit", lambda: _reading(asym=("a", "b")))
    assert rr.drift().regressed is True
    rr.accept_baseline()
    assert rr.drift().regressed is False


# -- baseline failure modes ------------------------------------------------


def test_a_corrupt_baseline_is_treated_as_absent_not_empty(monkeypatch):
    """An empty baseline would report every asymmetric module as newly
    regressed, burying a real regression under a hundred false ones on the
    first boot after a disk fault."""
    rr.baseline_path().parent.mkdir(parents=True, exist_ok=True)
    rr.baseline_path().write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(rr, "_audit", lambda: _reading(asym=("a", "b", "c")))
    d = rr.drift()
    assert d.baseline_exists is False
    assert d.new_asymmetric == ()


def test_a_baseline_that_is_not_an_object_is_treated_as_absent(monkeypatch):
    rr.baseline_path().parent.mkdir(parents=True, exist_ok=True)
    rr.baseline_path().write_text("[1,2,3]", encoding="utf-8")
    monkeypatch.setattr(rr, "_audit", lambda: _reading(asym=("a",)))
    assert rr.drift().baseline_exists is False


def test_the_baseline_is_published_atomically():
    """A baseline half-written by a crash reads as corrupt on the next
    boot, which degrades to 'no baseline' and floods the operator exactly
    when they are already recovering."""
    import inspect
    assert "atomic_replace" in inspect.getsource(rr.accept_baseline)


def test_a_failing_audit_degrades_rather_than_raising(monkeypatch):
    def _boom():
        raise RuntimeError("tree walk exploded")
    monkeypatch.setattr(rr, "_audit", _boom)
    d = rr.drift()
    assert d.error and d.regressed is False
    assert rr.dispatch_reach_command("/reach").ok is False


# -- the watchdog ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_watchdog_is_silent_at_steady_state(monkeypatch, caplog):
    monkeypatch.setattr(rr, "_audit", lambda: _reading(asym=("a",)))
    rr.accept_baseline()
    caplog.clear()
    await rr.run_watchdog()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


@pytest.mark.asyncio
async def test_the_watchdog_warns_on_regression(monkeypatch, caplog):
    monkeypatch.setattr(rr, "_audit", lambda: _reading(asym=("a",)))
    rr.accept_baseline()
    monkeypatch.setattr(rr, "_audit", lambda: _reading(asym=("a", "newcomer")))
    caplog.clear()
    await rr.run_watchdog()
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert warnings and "newcomer" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_the_watchdog_runs_off_the_event_loop():
    """The audit walks the module tree and parses every file — far too much
    work for the loop that also runs the organism."""
    import inspect
    assert "to_thread" in inspect.getsource(rr.run_watchdog)


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


def test_help_is_reachable():
    assert "reachability" in rr.dispatch_reach_command("/reach help").text


def test_a_module_query_names_its_surfaces(monkeypatch):
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
    monkeypatch.setattr(rr, "_audit", lambda: _R())
    out = rr.dispatch_reach_command("/reach menu_bindings")
    assert out.ok and "attach" in out.text and "cockpit" in out.text


def test_an_unknown_module_refuses_rather_than_returning_nothing(monkeypatch):
    monkeypatch.setattr(rr, "_audit", lambda: _reading())
    out = rr.dispatch_reach_command("/reach no_such_module_xyz")
    assert out.ok is False and "no module matching" in out.text


def test_the_verb_adds_no_analysis_of_its_own():
    """Every number comes from surface_reachability.audit — a second
    implementation would be a second opinion about the same tree."""
    import pathlib
    src = pathlib.Path(rr.__file__).read_text(encoding="utf-8")
    assert "surface_reachability import audit" in src
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

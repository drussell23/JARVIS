"""The organism finds its own work — from evidence, and inside the cage's limits.

A goal the organism invented is a goal nobody can verify, so every candidate
must trace to a fact some other machinery already recorded. And a sensor that
can discover work inside the governance substrate is a sensor that can propose
edits to its own brakes — the Sentinel floor would still force those to a
human, but the honest place to stop is before the goal exists.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.autonomy.goal_discovery import (
    DiscoveredWork,
    discover,
    discovery_enabled,
    synthesize_and_sign,
)
from backend.core.ouroboros.governance.autonomy.sentinel_cooldown import (
    TargetCooldownLedger,
)


class _Failure:
    def __init__(self, test_id, file_path, error_text=""):
        self.test_id = test_id
        self.file_path = file_path
        self.error_text = error_text


class _Watcher:
    """A watcher discovery must NEVER await — only hand to the store."""

    def __init__(self, failures=()):
        self._failures = list(failures)
        self.census_calls = 0

    async def run_census(self):
        self.census_calls += 1
        return self._failures, [], []


class _HangingWatcher:
    async def run_census(self):
        await asyncio.sleep(3600)


class _Store:
    """A census store: reads are instant, refreshes are somebody else's job."""

    def __init__(self, failures=None, fresh=True, raises=False):
        self._failures = list(failures or ())
        self._fresh = fresh
        self._raises = raises
        self.refresh_requests = 0

    def snapshot(self, **_kw):
        if self._raises:
            raise RuntimeError("store exploded")
        if not self._fresh:
            return None

        class _Snap:
            failures = tuple(self._failures)

        return _Snap()

    def ensure_refreshing(self, watcher):
        self.refresh_requests += 1
        return True


@pytest.fixture
def repo(tmp_path):
    """A miniature tree: one production module, one test, one cage file."""
    (tmp_path / "backend" / "api").mkdir(parents=True)
    (tmp_path / "backend" / "core" / "ouroboros" / "governance").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "backend" / "api" / "widget.py").write_text("x = 1\n" * 200)
    (tmp_path / "backend" / "api" / "covered.py").write_text("y = 2\n" * 200)
    (tmp_path / "tests" / "test_covered.py").write_text("def test_ok(): pass\n")
    (tmp_path / "backend" / "core" / "ouroboros" / "governance" / "risk_engine.py"
     ).write_text("z = 3\n" * 200)
    return tmp_path


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# The switch
# --------------------------------------------------------------------------

def test_discovery_is_off_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_GOAL_DISCOVERY_ENABLED", raising=False)
    assert discovery_enabled() is False


def test_synthesis_refuses_while_discovery_is_off(monkeypatch):
    monkeypatch.delenv("JARVIS_GOAL_DISCOVERY_ENABLED", raising=False)
    work = DiscoveredWork("backend/api/widget.py", "uncovered_module", "no tests")
    res = synthesize_and_sign(work)
    assert res is not None and res.ok is False
    assert res.reason == "discovery_disabled"


# --------------------------------------------------------------------------
# Evidence, and its ranking
# --------------------------------------------------------------------------

def test_a_red_test_is_mapped_to_the_code_under_test(repo):
    """The fix belongs in the production file, never in the assertion."""
    store = _Store([_Failure("tests/test_widget.py::test_a",
                             "tests/test_widget.py", "AssertionError")])
    found = _run(discover(repo_root=repo, census=store, cooldown=None))
    reds = [f for f in found if f.kind == "ambient_red"]
    assert reds, "the red produced no candidate"
    assert reds[0].target_file == "backend/api/widget.py"
    assert "Do not weaken, skip or delete the test" in reds[0].describe()


def test_a_red_whose_subject_is_ambiguous_is_skipped(repo):
    """Guessing a target is how an autonomous loop edits the wrong file with
    confidence."""
    (repo / "backend" / "api" / "dup.py").write_text("a = 1\n" * 200)
    (repo / "backend" / "core" / "dup.py").write_text("a = 1\n" * 200)
    store = _Store([_Failure("tests/test_dup.py::t", "tests/test_dup.py")])
    found = _run(discover(repo_root=repo, census=store, cooldown=None))
    assert not [f for f in found if f.kind == "ambient_red"]


def test_reds_outrank_uncovered_modules(repo):
    store = _Store([_Failure("tests/test_widget.py::t", "tests/test_widget.py")])
    found = _run(discover(repo_root=repo, census=store, cooldown=None))
    assert found[0].kind == "ambient_red", [f.kind for f in found]


def test_uncovered_modules_are_found_when_nothing_is_red(repo):
    found = _run(discover(repo_root=repo, watcher=_Watcher(), cooldown=None))
    subjects = {f.subject_file for f in found}
    assert "backend/api/widget.py" in subjects
    assert "backend/api/covered.py" not in subjects, "a covered module is not a gap"


def test_an_uncovered_goal_declares_the_file_it_will_WRITE(repo):
    """The cage checks an edit against the goal's declared scope. Declaring
    the subject and then creating a test file is refused live as
    `self_modification_unsanctioned_source` — the op writes a path its own
    mandate never covered."""
    found = _run(discover(repo_root=repo, watcher=_Watcher(), cooldown=None))
    work = next(f for f in found if f.subject_file == "backend/api/widget.py")
    assert work.target_file == "tests/test_widget.py", (
        "the goal must be scoped to the file that gets written"
    )
    text = work.describe()
    assert "CREATE `tests/test_widget.py`" in text
    assert "do\n    NOT modify it" in text or "NOT modify it" in text


def test_the_goal_id_is_keyed_on_the_subject_not_the_test_path(repo):
    """'tests for X' is the same work however the test file is named."""
    found = _run(discover(repo_root=repo, watcher=_Watcher(), cooldown=None))
    work = next(f for f in found if f.subject_file == "backend/api/widget.py")
    assert work.goal_id == "ov-auto-uncovered-module-widget"


# --------------------------------------------------------------------------
# The exclusions
# --------------------------------------------------------------------------

def test_the_governance_substrate_is_never_a_target(repo):
    """The cage the organism runs inside."""
    found = _run(discover(repo_root=repo, watcher=_Watcher(), cooldown=None))
    assert not [f for f in found if "governance" in f.target_file], (
        "discovery reached into the cage"
    )


def test_a_cooling_target_is_skipped(repo, tmp_path):
    led = TargetCooldownLedger(tmp_path / "cd.json")
    led.record_failure("backend/api/widget.py", reason="failed twice")
    found = _run(discover(repo_root=repo, watcher=_Watcher(), cooldown=led))
    assert "backend/api/widget.py" not in {f.target_file for f in found}


def test_the_same_target_is_never_offered_twice_in_one_pass(repo):
    store = _Store([
        _Failure("tests/test_widget.py::a", "tests/test_widget.py"),
        _Failure("tests/test_widget.py::b", "tests/test_widget.py"),
    ])
    found = _run(discover(repo_root=repo, census=store, cooldown=None))
    targets = [f.target_file for f in found]
    assert len(targets) == len(set(targets))


# --------------------------------------------------------------------------
# Resilience
# --------------------------------------------------------------------------

def test_a_broken_census_degrades_to_the_coverage_source(repo):
    """One dead source must not blind the sensor."""
    found = _run(discover(repo_root=repo, census=_Store(raises=True), cooldown=None))
    assert found, "a failing census store killed discovery entirely"
    assert all(f.kind == "uncovered_module" for f in found)


def test_no_fresh_census_still_ranks_on_coverage(repo):
    """Tier 2 absent is a precision cost for ONE pass, never a stall."""
    store = _Store(fresh=False)
    found = _run(discover(repo_root=repo, census=store, cooldown=None))
    assert found
    assert all(f.kind == "uncovered_module" for f in found)
    assert store.refresh_requests == 1, "a stale store must be asked to refresh"


def test_no_watcher_at_all_still_discovers(repo):
    found = _run(discover(repo_root=repo, watcher=None, cooldown=None))
    assert found


def test_discovery_never_awaits_the_census(repo):
    """The root cause of the first live stall. A timeout cannot save you here:
    `wait_for` only cancels at an await boundary, and pytest subprocesses do
    not yield. So the census must never be ON the critical path at all — the
    store is READ, and a hanging watcher is simply handed to it and forgotten."""
    import time

    watcher = _HangingWatcher()
    store = _Store(fresh=False)
    started = time.monotonic()
    found = _run(discover(repo_root=repo, watcher=watcher, census=store,
                          cooldown=None))
    assert time.monotonic() - started < 20, "discovery waited on the census"
    assert found, "discovery returned nothing instead of cheap evidence"
    assert store.refresh_requests == 1


def test_the_watcher_is_never_run_by_discovery(repo):
    """Proof by counter: discovery hands the watcher to the store and never
    calls run_census itself."""
    watcher = _Watcher([_Failure("tests/test_widget.py::t", "tests/test_widget.py")])
    store = _Store(fresh=False)
    _run(discover(repo_root=repo, watcher=watcher, census=store, cooldown=None))
    assert watcher.census_calls == 0


def test_the_census_budget_is_derived_from_the_pipeline(monkeypatch):
    from backend.core.ouroboros.governance.autonomy.goal_discovery import (
        _census_budget_s,
    )

    monkeypatch.delenv("JARVIS_GOAL_DISCOVERY_CENSUS_BUDGET_S", raising=False)
    monkeypatch.setenv("JARVIS_PIPELINE_TIMEOUT_S", "4000")
    assert _census_budget_s() == 500.0
    monkeypatch.setenv("JARVIS_GOAL_DISCOVERY_CENSUS_BUDGET_S", "77")
    assert _census_budget_s() == 77.0


def test_a_nonexistent_repo_returns_nothing_rather_than_raising(tmp_path):
    found = _run(discover(repo_root=tmp_path / "nope", watcher=None, cooldown=None))
    assert found == ()


def test_the_limit_is_honoured(repo):
    for i in range(12):
        (repo / "backend" / "api" / f"mod{i}.py").write_text("q = 1\n" * 200)
    found = _run(discover(repo_root=repo, watcher=_Watcher(), cooldown=None, limit=3))
    assert len(found) <= 3


# --------------------------------------------------------------------------
# Identity + authority
# --------------------------------------------------------------------------

def test_the_goal_id_is_derived_so_rediscovery_collides(repo):
    """The same latent problem found twice must produce the same id, so the
    signer's duplicate-id refusal stops it being filed again."""
    a = DiscoveredWork("backend/api/widget.py", "ambient_red", "e1")
    b = DiscoveredWork("backend/api/widget.py", "ambient_red", "e2-different")
    assert a.goal_id == b.goal_id
    assert a.goal_id.startswith("ov-auto-")


def test_synthesis_composes_the_one_signer_not_a_second_path():
    import inspect

    from backend.core.ouroboros.governance.autonomy import goal_discovery

    src = inspect.getsource(goal_discovery.synthesize_and_sign)
    assert "author_and_sign_goal" in src, "a second signing path would be unverifiable"
    assert "hmac" not in src.lower(), "crypto must be composed, never reinvented here"

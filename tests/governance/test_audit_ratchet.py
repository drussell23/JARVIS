"""Regression spine for the generic audit ratchet and `/evidence`.

Two instruments now share one ratchet. The assertions here are about the
properties that made the ratchet worth extracting — degradation direction,
per-repository state, and above all REACHABILITY, since the module being
mounted was itself one of the two real orphans `/reach` found.
"""
from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance import audit_ratchet as ar
from backend.core.ouroboros.governance import evidence_repl as ev
from backend.core.ouroboros.governance import reach_repl as rr
from tests.source_probe import code_of


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for k in ("JARVIS_EVIDENCE_BASELINE_PATH", "JARVIS_REACH_BASELINE_PATH",
              "JARVIS_AUDIT_WATCHDOGS_ENABLED",
              "JARVIS_EVIDENCE_WATCHDOG_ENABLED",
              "JARVIS_REACH_WATCHDOG_ENABLED"):
        monkeypatch.delenv(k, raising=False)
    # EVERY instrument, not just the two named ones. The ad-hoc `probe`
    # instrument below has no path override of its own, so without this it
    # resolves against the operator's real repository — writing
    # `.jarvis/probe_baseline.json` and leaking an accepted floor into the
    # next test, which is why the "no baseline" case passed alone and failed
    # in a full run. Redirecting the ROOT covers instruments this fixture
    # has never heard of, which is the only version that stays correct when
    # the third instrument lands.
    monkeypatch.setenv("JARVIS_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setenv("JARVIS_EVIDENCE_BASELINE_PATH",
                       str(tmp_path / "evidence.json"))
    monkeypatch.setenv("JARVIS_REACH_BASELINE_PATH",
                       str(tmp_path / "reach.json"))
    yield


def _instrument(name="probe", buckets=None, scanned=7, **kw):
    payload = buckets if buckets is not None else {"a": ("x",), "b": ()}

    async def _run():
        return payload

    return ar.Instrument(name=name, run=_run,
                         findings=lambda r: r, scanned=lambda r: scanned, **kw)


def _ratchet_for(**kw):
    return ar.AuditRatchet(_instrument(**kw))


# -- the ratchet -----------------------------------------------------------


async def test_no_baseline_reports_nothing_rather_than_everything():
    """A fresh checkout has not regressed — it has not been measured.

    Reporting the standing list as new is how the previous four detectors
    taught people to ignore them.
    """
    d = await _ratchet_for().drift()
    assert d.baseline_exists is False
    assert d.regressed is False and not d.resolved


async def test_accept_then_drift_is_silent():
    r = _ratchet_for()
    await r.accept()
    d = await r.drift()
    assert d.baseline_exists is True and d.regressed is False


async def test_a_new_finding_is_loud_and_named_by_bucket():
    r = _ratchet_for()
    await r.accept()
    grown = _ratchet_for(buckets={"a": ("x", "newcomer"), "b": ()})
    d = await grown.drift()
    assert d.regressed is True
    assert d.bucket("a") == ("newcomer",)
    assert d.bucket("b") == ()


async def test_buckets_are_compared_independently():
    """"a new orphan" and "a newly asymmetric module" are different news."""
    r = _ratchet_for(buckets={"a": ("x",), "b": ()})
    await r.accept()
    d = await _ratchet_for(buckets={"a": ("x",), "b": ("fresh",)}).drift()
    assert d.bucket("a") == () and d.bucket("b") == ("fresh",)


async def test_a_fix_is_reported_as_resolved():
    r = _ratchet_for(buckets={"a": ("x", "y")})
    await r.accept()
    d = await _ratchet_for(buckets={"a": ("x",)}).drift()
    assert d.resolved == ("y",) and d.regressed is False


async def test_a_bucket_that_vanishes_entirely_still_counts_as_resolved():
    """Dropping it would silently forget a fix."""
    r = _ratchet_for(buckets={"a": ("x",), "gone": ("z",)})
    await r.accept()
    d = await _ratchet_for(buckets={"a": ("x",)}).drift()
    assert "z" in d.resolved


async def test_a_corrupt_baseline_reads_as_absent_not_empty(tmp_path):
    """Empty would report every standing finding as newly regressed —
    a hundred false positives arriving mid disk-fault recovery."""
    r = _ratchet_for()
    await r.accept()
    r.baseline_path().write_text("{not json", encoding="utf-8")
    d = await r.drift()
    assert d.baseline_exists is False and d.regressed is False


async def test_an_unrecognised_shape_reads_as_absent():
    r = _ratchet_for()
    r.baseline_path().parent.mkdir(parents=True, exist_ok=True)
    r.baseline_path().write_text(json.dumps({"something": "else"}),
                                 encoding="utf-8")
    assert (await r.drift()).baseline_exists is False


async def test_an_exploding_audit_degrades_rather_than_raising():
    async def _boom():
        raise RuntimeError("scan exploded")

    r = ar.AuditRatchet(ar.Instrument(
        name="probe", run=_boom, findings=lambda r: {}))
    d = await r.drift()
    assert d.error and d.regressed is False


async def test_the_baseline_is_published_atomically(monkeypatch):
    from backend.core.ouroboros.governance import durable_io

    seen = []
    real = durable_io.atomic_replace
    monkeypatch.setattr(durable_io, "atomic_replace",
                        lambda t, d: (seen.append((t, d)), real(t, d))[1])
    r = _ratchet_for()
    await r.accept()
    assert seen, "published without atomic_replace"
    assert not seen[0][0].exists(), "the temp file survived the publish"


def test_state_is_per_repository_never_global(monkeypatch, tmp_path):
    """An accepted floor is a judgement about ONE checkout's code."""
    monkeypatch.delenv("JARVIS_EVIDENCE_BASELINE_PATH", raising=False)
    here = tmp_path / "checkout"
    monkeypatch.setenv("JARVIS_PROJECT_ROOT", str(here))
    assert ev.RATCHET.baseline_path().is_relative_to(here)


# -- the legacy adapter: adopting the ratchet must not lose a floor --------


async def test_a_pre_ratchet_baseline_is_still_honoured(tmp_path):
    """`/reach` had a flat on-disk format before the ratchet existed.

    Reading it as unrecognised would tell the operator "no baseline
    recorded" and return every standing finding as new — silently
    discarding a floor they had accepted.
    """
    rr.baseline_path().parent.mkdir(parents=True, exist_ok=True)
    rr.baseline_path().write_text(json.dumps({
        "schema_version": "reach_repl.1",
        "asymmetric": ["mod.a"], "orphans": ["mod.b"],
        "surfaces": ["daemon"], "scanned": 3,
    }), encoding="utf-8")

    class _M:
        def __init__(self, name): self.module = name

    class _R:
        scanned = 3
        surface_labels = ("daemon",)
        modules = []
        def asymmetric(self): return [_M("mod.a")]
        def orphans(self): return [_M("mod.b")]

    async def _run():
        return _R()

    ratchet = ar.AuditRatchet(ar.Instrument(
        name="reach", run=_run, findings=rr._findings,
        scanned=lambda r: r.scanned,
        baseline_filename="surface_reachability_baseline.json",
        legacy_buckets=rr._legacy_buckets))
    d = await ratchet.drift()
    assert d.baseline_exists is True, "the accepted floor was discarded"
    assert d.regressed is False


def test_the_legacy_reader_does_not_adopt_surfaces_as_a_bucket():
    """Inferring buckets from "any key holding a list of strings" would
    report a renamed surface label as a regression."""
    got = rr._legacy_buckets({"asymmetric": ["a"], "orphans": [],
                              "surfaces": ["daemon", "attach"]})
    assert set(got) == {"asymmetric", "orphans"}


# -- the mount: this module WAS the finding --------------------------------


async def test_evidence_is_reachable_by_typing_it():
    """`source_assertion_audit` shipped complete, tested, and uncallable."""
    from backend.core.ouroboros.governance import repl_verb_cage as cage

    result = await cage.dispatch_async("/evidence help")
    assert result is not None, "/evidence is not mounted"
    assert "source assertion" in result.text.lower()


async def test_the_verb_measures_the_real_repository():
    out = await ev.dispatch_evidence_command("/evidence rate")
    assert out.ok and "RATE" in out.text and "source_only" in out.text


async def test_the_verb_adds_no_analysis_of_its_own():
    """Every number comes from source_assertion_audit — a second
    implementation would be a second opinion about the same tree."""
    code = code_of(ev)
    assert "source_assertion_audit" in code
    for banned in ("ast.parse", "os.walk"):
        assert banned not in code


async def test_an_unknown_query_refuses_rather_than_returning_nothing():
    out = await ev.dispatch_evidence_command("/evidence no_such_thing_xyz")
    assert out.ok is False and "no source-only test matching" in out.text


async def test_the_finding_key_is_stable_under_line_shifts():
    """Including the line number would report a whole file as newly
    source-only after one insertion above it."""
    class _V:
        module, name, lineno = "tests/x.py", "test_a", 10
    a = ev._key(_V())
    _V.lineno = 400
    assert ev._key(_V()) == a


# -- declaring a ratchet mounts its watchdog -------------------------------


def test_both_instruments_are_registered_by_declaration():
    names = {r.instrument.name for r in ar.registered_ratchets()}
    assert {"reach", "evidence"} <= names


def test_registration_is_discovered_not_listed():
    """A hardcoded list is a thing to forget — which is how
    `reach_repl.run_watchdog` shipped with zero production callers."""
    code = code_of(ar, "registered_ratchets")
    assert "repl_verb_cage" in code
    assert "reach" not in code and "evidence" not in code


async def test_one_exploding_instrument_does_not_cancel_the_others(
        monkeypatch):
    """A diagnostic that can take down the boot it reports on is a
    liability rather than an asset."""
    good = _ratchet_for(name="good")

    async def _boom():
        raise RuntimeError("nope")

    bad = ar.AuditRatchet(ar.Instrument(
        name="bad", run=_boom, findings=lambda r: {}))
    monkeypatch.setattr(ar, "registered_ratchets", lambda: [bad, good])
    out = await ar.run_registered_watchdogs()
    assert set(out) == {"bad", "good"}
    assert out["bad"].error and out["good"].baseline_exists is False


def test_the_master_switch_silences_the_whole_sweep(monkeypatch):
    monkeypatch.setenv("JARVIS_AUDIT_WATCHDOGS_ENABLED", "false")
    assert ar.spawn_registered_watchdogs() is None


def test_spawning_outside_a_loop_is_not_an_error():
    """A synchronous context has nothing to schedule onto."""
    assert ar.spawn_registered_watchdogs() is None


async def test_spawning_inside_a_loop_returns_a_task(monkeypatch):
    monkeypatch.setattr(ar, "registered_ratchets", lambda: [])
    task = ar.spawn_registered_watchdogs()
    assert task is not None
    await task


# -- the boot seam: the defect this whole arc exists to stop ---------------


def test_the_boot_seam_spawns_the_sweep():
    """`reach_repl.run_watchdog` was correct, tested and never called."""
    from backend.core.ouroboros.governance import governed_loop_service as gls

    code = code_of(gls)
    assert "spawn_registered_watchdogs" in code, (
        "the audit watchdogs have no production caller — again")


def test_the_boot_seam_names_no_instrument():
    """Wiring two by name would re-create the defect for the third."""
    from backend.core.ouroboros.governance import governed_loop_service as gls

    code = code_of(gls)
    assert "reach_repl" not in code and "evidence_repl" not in code


def test_teardown_cancels_the_sweep_on_both_paths():
    """A scan outliving its service parses a tree nobody is serving."""
    from backend.core.ouroboros.governance import governed_loop_service as gls

    for seam in ("stop", "_teardown_partial"):
        assert "_cancel_audit_watchdogs" in code_of(gls, seam), seam
    assert "cancel" in code_of(gls, "_cancel_audit_watchdogs")


def test_the_sweep_is_never_awaited_by_boot():
    """A watchdog that delayed startup by its own scan gets deleted the
    first time somebody profiles boot — and then it is gone again."""
    from backend.core.ouroboros.governance import governed_loop_service as gls

    code = code_of(gls)
    assert "await _spawn_audits" not in code
    assert "await spawn_registered_watchdogs" not in code

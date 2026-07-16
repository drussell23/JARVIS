"""Capability Liveness Audit — self-perception spine (autonomy pivot Gap 2).

Proves the organism can now SEE its own inert capabilities: the severed-driver
class (the merkle archetype — a public callable defined but invoked by no
production code) is DETECTED, a genuinely-called one is ALIVE, and every
zero-output / fault path returns a clean snapshot rather than raising.

Tests drive the REAL code path: synthetic capability modules on disk under a
tmp ``backend/`` so both AST symbol-parsing and the ripgrep-free caller-index
walk exercise production logic; only the FlagRegistry is faked (to control
which capabilities are declared).
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from aiohttp.test_utils import make_mocked_request

import backend.core.ouroboros.governance.capability_liveness as cl
from backend.core.ouroboros.governance.flag_registry import FlagType


# ---------------------------------------------------------------------------
# Fake FlagRegistry (control which capabilities are declared) + tmp repo
# ---------------------------------------------------------------------------


class _FakeSpec:
    def __init__(self, name, source_file, category="OBSERVABILITY", default=True):
        self.name = name
        self.source_file = source_file
        self.type = FlagType.BOOL
        self.category = category
        self.default = default


class _FakeRegistry:
    def __init__(self, specs, on=True):
        self._specs = specs
        self._on = on

    def list_all(self):
        return list(self._specs)

    def get_bool(self, name, **kw):
        return self._on


def _install_fake_registry(monkeypatch, specs, on=True):
    import backend.core.ouroboros.governance.flag_registry as fr
    monkeypatch.setattr(fr, "ensure_seeded", lambda: _FakeRegistry(specs, on=on))


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "backend" / "gov").mkdir(parents=True)
    return tmp_path


def _write(repo: Path, rel: str, body: str) -> str:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return rel


def _agg(repo, specs, monkeypatch, on=True):
    _install_fake_registry(monkeypatch, specs, on=on)
    cl.reset_cache_for_tests()
    return cl.aggregate_capability_liveness(repo_root=repo, now=1000.0)


# ---------------------------------------------------------------------------
# Severed-driver DETECTION (the merkle archetype) — the load-bearing test
# ---------------------------------------------------------------------------


def test_severed_driver_is_detected(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    # A driver defined but invoked by NO production code = the merkle bug.
    sev = _write(repo, "backend/gov/sev_mod.py", "def hydrate_driver():\n    return 1\n")
    specs = [_FakeSpec("JARVIS_SEV_ENABLED", sev)]
    snap = _agg(repo, specs, monkeypatch)
    verdicts = {v["flag"]: v for v in snap.severance_candidates + snap.fully_severed}
    assert "JARVIS_SEV_ENABLED" in verdicts
    v = verdicts["JARVIS_SEV_ENABLED"]
    assert v["verdict"] == "FULLY_SEVERED"          # its only callable is severed
    assert "hydrate_driver" in v["severed_symbols"]


def test_called_capability_is_alive(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    live = _write(repo, "backend/gov/live_mod.py", "def entry():\n    return 1\n")
    # A production consumer that actually calls it.
    _write(repo, "backend/gov/consumer.py",
           "from backend.gov.live_mod import entry\n\ndef use():\n    return entry()\n")
    specs = [_FakeSpec("JARVIS_LIVE_ENABLED", live)]
    snap = _agg(repo, specs, monkeypatch)
    assert snap.counts.get("ALIVE") == 1
    assert not snap.severance_candidates and not snap.fully_severed


def test_definition_line_is_not_a_caller(tmp_path, monkeypatch):
    """The merkle regression: ``def NAME(`` / ``class NAME(`` must NOT be
    counted as a call site, else a defined-but-never-called driver looks
    reachable."""
    repo = _repo(tmp_path)
    sev = _write(repo, "backend/gov/only_def.py",
                 "def lonely():\n    return 2\n\nclass Holder:\n    def m(self):\n        return 3\n")
    snap = _agg(repo, specs=[_FakeSpec("JARVIS_ONLYDEF_ENABLED", sev)], monkeypatch=monkeypatch)
    v = (snap.severance_candidates + snap.fully_severed)[0]
    # Both the function and the method are defined-only → severed.
    assert v["verdict"] == "FULLY_SEVERED"
    assert set(v["severed_symbols"]) == {"lonely", "m"}


def test_method_call_site_marks_alive(tmp_path, monkeypatch):
    """A method invoked via ``.name(`` counts as a caller (the merkle
    ``update_full`` post-fix shape)."""
    repo = _repo(tmp_path)
    mod = _write(repo, "backend/gov/svc.py",
                 "class Svc:\n    def update_full(self):\n        return 1\n")
    _write(repo, "backend/gov/driver.py",
           "def boot(s):\n    return s.update_full()\n")
    snap = _agg(repo, [_FakeSpec("JARVIS_SVC_ENABLED", mod)], monkeypatch)
    assert snap.counts.get("ALIVE") == 1


def test_tests_dir_callers_excluded(tmp_path, monkeypatch):
    """A driver called ONLY from tests is still severed (production dead)."""
    repo = _repo(tmp_path)
    sev = _write(repo, "backend/gov/tonly.py", "def driver():\n    return 1\n")
    _write(repo, "backend/gov/tests/test_x.py",
           "from backend.gov.tonly import driver\n\ndef test_d():\n    assert driver()\n")
    snap = _agg(repo, [_FakeSpec("JARVIS_TONLY_ENABLED", sev)], monkeypatch)
    assert snap.fully_severed and snap.fully_severed[0]["flag"] == "JARVIS_TONLY_ENABLED"


def test_partial_severance_is_a_candidate_not_fully(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    mod = _write(repo, "backend/gov/mixed.py",
                 "def used():\n    return 1\n\ndef dead():\n    return 2\n")
    _write(repo, "backend/gov/c.py", "from backend.gov.mixed import used\n\ndef go():\n    return used()\n")
    snap = _agg(repo, [_FakeSpec("JARVIS_MIXED_ENABLED", mod)], monkeypatch)
    cand = snap.severance_candidates
    assert cand and cand[0]["verdict"] == "SEVERED"
    assert cand[0]["severed_symbols"] == ["dead"]
    assert 0.0 < cand[0]["fraction_severed"] < 1.0


# ---------------------------------------------------------------------------
# Enumeration + disabled + unresolved
# ---------------------------------------------------------------------------


def test_disabled_capability_not_audited(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    sev = _write(repo, "backend/gov/off_mod.py", "def d():\n    return 1\n")
    # Flag registered but live-off → not our concern (constellation-DARK axis).
    snap = _agg(repo, [_FakeSpec("JARVIS_OFF_ENABLED", sev)], monkeypatch, on=False)
    assert snap.capabilities_declared == 0
    assert not snap.severance_candidates and not snap.fully_severed


def test_seed_sourced_flag_is_unresolved(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    specs = [_FakeSpec("JARVIS_SEED_ENABLED",
                       "backend/core/ouroboros/governance/flag_registry_seed.py")]
    snap = _agg(repo, specs, monkeypatch)
    assert snap.counts.get("UNRESOLVED") == 1


def test_non_enabled_flags_ignored(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    m = _write(repo, "backend/gov/x.py", "def d():\n    return 1\n")
    specs = [_FakeSpec("JARVIS_SOME_TUNING", m)]  # not *_ENABLED
    snap = _agg(repo, specs, monkeypatch)
    assert snap.capabilities_declared == 0


# ---------------------------------------------------------------------------
# Zero-output safety + never-raises
# ---------------------------------------------------------------------------


def test_no_capabilities_is_clean(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    snap = _agg(repo, [], monkeypatch)
    assert snap.capabilities_declared == 0
    assert snap.callable_reachability_ratio is None
    d = snap.to_dict()
    assert d["capabilities"]["callable_reachability_ratio"] is None
    assert d["fully_severed"] == [] and d["severance_candidates"] == []


def test_flag_registry_unavailable_is_clean(tmp_path, monkeypatch):
    import backend.core.ouroboros.governance.flag_registry as fr

    def _boom():
        raise RuntimeError("registry down")

    monkeypatch.setattr(fr, "ensure_seeded", _boom)
    cl.reset_cache_for_tests()
    snap = cl.aggregate_capability_liveness(repo_root=tmp_path, now=1.0)
    assert snap.reason_code == "flag_registry_unavailable"


def test_aggregator_never_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(cl, "_build_caller_index", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    _install_fake_registry(monkeypatch, [_FakeSpec("JARVIS_X_ENABLED", "backend/gov/x.py")])
    cl.reset_cache_for_tests()
    snap = cl.aggregate_capability_liveness(repo_root=tmp_path, now=1.0)
    assert snap.reason_code == "aggregation_error"


def test_master_flag_default_true(monkeypatch):
    monkeypatch.delenv("JARVIS_CAPABILITY_LIVENESS_ENABLED", raising=False)
    assert cl.master_enabled() is True
    monkeypatch.setenv("JARVIS_CAPABILITY_LIVENESS_ENABLED", "0")
    assert cl.master_enabled() is False


def test_snapshot_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_CAPABILITY_LIVENESS_ENABLED", "false")
    cl.reset_cache_for_tests()
    d = cl.snapshot(force=True)
    assert d["enabled"] is False and d["reason_code"] == "disabled"


# ---------------------------------------------------------------------------
# Endpoint — mirrors the dashboard/observability conventions
# ---------------------------------------------------------------------------


def _req(path="/observability/liveness"):
    r = make_mocked_request("GET", path, headers={})
    r._transport_peername = ("127.0.0.1", 0)  # type: ignore[attr-defined]
    return r


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _router():
    from backend.core.ouroboros.governance.ide_observability import IDEObservabilityRouter
    return IDEObservabilityRouter()


def test_endpoint_disabled_when_master_off(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "false")
    resp = _run(_router()._handle_capability_liveness(_req()))
    assert resp.status == 403
    assert json.loads(resp.body.decode())["reason_code"] == "ide_observability.disabled"


def test_endpoint_substrate_off(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CAPABILITY_LIVENESS_ENABLED", "false")
    resp = _run(_router()._handle_capability_liveness(_req()))
    assert resp.status == 403
    assert json.loads(resp.body.decode())["reason_code"] == "ide_observability.liveness_disabled"


def test_endpoint_returns_snapshot(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CAPABILITY_LIVENESS_ENABLED", "true")
    # Fake a trivial registry so the endpoint's real aggregation is instant.
    _install_fake_registry(monkeypatch, [])
    cl.reset_cache_for_tests()
    resp = _run(_router()._handle_capability_liveness(_req()))
    assert resp.status == 200
    body = json.loads(resp.body.decode())
    assert body["schema_version"] == cl.CAPABILITY_LIVENESS_SCHEMA_VERSION
    assert "capabilities" in body and "fully_severed" in body
    assert resp.headers.get("Cache-Control") == "no-store"


def test_endpoint_never_500s(monkeypatch):
    monkeypatch.setenv("JARVIS_IDE_OBSERVABILITY_ENABLED", "true")
    monkeypatch.setenv("JARVIS_CAPABILITY_LIVENESS_ENABLED", "true")
    monkeypatch.setattr(cl, "snapshot", lambda **k: (_ for _ in ()).throw(RuntimeError("boom")))
    resp = _run(_router()._handle_capability_liveness(_req()))
    assert resp.status == 200
    assert json.loads(resp.body.decode())["reason_code"] == "liveness.unavailable"

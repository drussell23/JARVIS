"""Regression spine for §41 Phase 1 — RoadmapReader."""
from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import roadmap_reader as rr
from backend.core.ouroboros.governance.roadmap_reader import (
    ROADMAP_READER_SCHEMA_VERSION,
    GoalEmitOutcome,
    GoalPriority,
    RoadmapDocument,
    RoadmapGoal,
    RoadmapReport,
    RoadmapVerdict,
    _ENV_DEFAULT_URGENCY,
    _ENV_HMAC_SECRET,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MAX_GOALS,
    _ENV_PERSIST,
    _ENV_REPO_NAME,
    _ENV_REQUIRE_SIG,
    _ENV_ROADMAP_PATH,
    _coerce_priority,
    _make_envelope_for_goal,
    compute_signature,
    default_urgency,
    emit_roadmap_envelopes,
    format_roadmap_panel,
    hmac_secret,
    ledger_path,
    master_enabled,
    max_goals,
    parse_roadmap,
    persistence_enabled,
    priority_glyph,
    process_roadmap,
    process_roadmap_sync,
    read_roadmap,
    register_flags,
    register_shipped_invariants,
    repo_name,
    require_signature,
    roadmap_path,
    verdict_glyph,
    verify_signature,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER, _ENV_PERSIST, _ENV_REQUIRE_SIG,
        _ENV_HMAC_SECRET, _ENV_ROADMAP_PATH, _ENV_MAX_GOALS,
        _ENV_LEDGER_PATH, _ENV_DEFAULT_URGENCY, _ENV_REPO_NAME,
    ):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setenv(
        _ENV_ROADMAP_PATH, str(tmp_path / "roadmap.yaml"),
    )
    monkeypatch.setenv(
        _ENV_LEDGER_PATH, str(tmp_path / "ledger.jsonl"),
    )
    yield


def _run(coro):
    return asyncio.run(coro)


def _write_roadmap(
    path: Path, payload: dict, *, secret: str = "",
) -> None:
    """Helper — sign-and-write a roadmap to disk."""
    body = {
        "version": payload.get("version", 1),
        "operator_id": payload.get("operator_id", "test@op"),
        "signed_at": payload.get("signed_at", "2026-05-11T00:00:00Z"),
        "goals": payload.get("goals", []),
    }
    if secret:
        sig = compute_signature(body, secret)
        body["signature"] = sig
    elif "signature" in payload:
        body["signature"] = payload["signature"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, indent=2), encoding="utf-8")


# Defaults


def test_schema():
    assert ROADMAP_READER_SCHEMA_VERSION == "roadmap_reader.1"


def test_master_default_false():
    assert master_enabled() is False


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_require_signature_default_true():
    assert require_signature() is True


def test_max_goals_default():
    assert max_goals() == 100


def test_max_goals_clamped(monkeypatch):
    monkeypatch.setenv(_ENV_MAX_GOALS, "99999")
    assert max_goals() == 10_000


def test_hmac_secret_unset():
    assert hmac_secret() is None


def test_hmac_secret_from_env(monkeypatch):
    monkeypatch.setenv(_ENV_HMAC_SECRET, "supersecret")
    assert hmac_secret() == "supersecret"


def test_default_urgency():
    assert default_urgency() == "normal"


def test_repo_name_default():
    assert repo_name() == "jarvis"


def test_roadmap_path_env(monkeypatch, tmp_path):
    target = tmp_path / "custom.yaml"
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(target))
    assert roadmap_path() == target


# Taxonomies


def test_verdict_taxonomy_closed():
    assert {v.value for v in RoadmapVerdict} == {
        "no_roadmap", "valid", "invalid_signature", "malformed",
    }


def test_priority_taxonomy_closed():
    assert {p.value for p in GoalPriority} == {
        "critical", "high", "medium", "low",
    }


@pytest.mark.parametrize("v", list(RoadmapVerdict))
def test_verdict_glyph(v):
    assert verdict_glyph(v) != "?"


@pytest.mark.parametrize("p", list(GoalPriority))
def test_priority_glyph(p):
    assert priority_glyph(p) != "?"


# Priority coercion


def test_coerce_priority_enum():
    assert _coerce_priority(GoalPriority.HIGH) is GoalPriority.HIGH


def test_coerce_priority_string():
    assert _coerce_priority("critical") is GoalPriority.CRITICAL


def test_coerce_priority_unknown_defaults_medium():
    assert _coerce_priority("garbage") is GoalPriority.MEDIUM


def test_coerce_priority_none_defaults_medium():
    assert _coerce_priority(None) is GoalPriority.MEDIUM


# HMAC


def test_compute_signature_empty_secret():
    assert compute_signature({"x": 1}, "") == ""


def test_compute_signature_deterministic():
    payload = {"a": 1, "b": [1, 2, 3]}
    s1 = compute_signature(payload, "secret")
    s2 = compute_signature(payload, "secret")
    assert s1 == s2
    assert len(s1) == 64  # sha256 hex


def test_compute_signature_different_keys():
    payload = {"x": 1}
    assert compute_signature(payload, "a") != compute_signature(payload, "b")


def test_verify_signature_valid():
    payload = {"x": 1}
    sig = compute_signature(payload, "secret")
    assert verify_signature(payload, sig, "secret") is True


def test_verify_signature_invalid():
    payload = {"x": 1}
    sig = compute_signature(payload, "secret")
    assert verify_signature(payload, sig, "wrong-secret") is False


def test_verify_signature_empty_sig():
    assert verify_signature({"x": 1}, "", "secret") is False


def test_verify_signature_constant_time():
    """sanity — same inputs should match."""
    payload = {"x": 1}
    sig = compute_signature(payload, "secret")
    assert verify_signature(payload, sig, "secret") is True
    assert verify_signature(payload, "0" * 64, "secret") is False


# Parser


def test_parse_empty():
    doc, goals = parse_roadmap("")
    assert doc is None
    assert goals == ()


def test_parse_malformed():
    doc, goals = parse_roadmap("{not valid yaml or json")
    assert doc is None


def test_parse_valid_json():
    raw = json.dumps({
        "version": 1,
        "operator_id": "x",
        "signed_at": "now",
        "signature": "abc",
        "goals": [
            {
                "id": "g1",
                "title": "Goal 1",
                "description": "d",
                "priority": "high",
                "target_files": ["x.py"],
            }
        ],
    })
    doc, goals = parse_roadmap(raw)
    assert doc is not None
    assert len(goals) == 1
    assert goals[0].goal_id == "g1"
    assert goals[0].priority is GoalPriority.HIGH


def test_parse_skips_invalid_goals():
    raw = json.dumps({
        "goals": [
            {"id": "g1", "title": "ok"},
            {"id": "", "title": "no id"},
            {"title": "no id field"},
            "not a dict",
            {"id": "g2", "title": "ok2"},
        ],
    })
    _, goals = parse_roadmap(raw)
    ids = {g.goal_id for g in goals}
    assert ids == {"g1", "g2"}


def test_parse_cap_max_goals(monkeypatch):
    monkeypatch.setenv(_ENV_MAX_GOALS, "3")
    raw = json.dumps({
        "goals": [
            {"id": f"g{i}", "title": f"Goal {i}"}
            for i in range(10)
        ],
    })
    _, goals = parse_roadmap(raw)
    assert len(goals) == 3


# read_roadmap


def test_read_no_file_returns_no_roadmap(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(tmp_path / "absent.yaml"))
    verdict, doc, diag = read_roadmap()
    assert verdict is RoadmapVerdict.NO_ROADMAP
    assert doc is None


def test_read_malformed_yaml(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    target = tmp_path / "rm.yaml"
    target.write_text("garbage: {{{ not valid", encoding="utf-8")
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(target))
    verdict, doc, diag = read_roadmap()
    assert verdict is RoadmapVerdict.MALFORMED


def test_read_unsigned_when_required(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_REQUIRE_SIG, "true")
    target = tmp_path / "rm.yaml"
    _write_roadmap(target, {"goals": [{"id": "g1", "title": "t"}]})
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(target))
    verdict, doc, diag = read_roadmap()
    assert verdict is RoadmapVerdict.INVALID_SIGNATURE
    assert "signature" in diag.lower()


def test_read_signed_valid(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_HMAC_SECRET, "topsecret")
    target = tmp_path / "rm.yaml"
    _write_roadmap(
        target,
        {"goals": [{"id": "g1", "title": "t", "target_files": ["x.py"]}]},
        secret="topsecret",
    )
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(target))
    verdict, doc, diag = read_roadmap()
    assert verdict is RoadmapVerdict.VALID
    assert doc is not None
    assert doc.signature_valid is True


def test_read_signed_wrong_secret(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_HMAC_SECRET, "wrong-secret")
    target = tmp_path / "rm.yaml"
    _write_roadmap(
        target, {"goals": [{"id": "g1", "title": "t"}]},
        secret="actual-secret",
    )
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(target))
    verdict, doc, diag = read_roadmap()
    assert verdict is RoadmapVerdict.INVALID_SIGNATURE


def test_read_unsigned_mode_permits(monkeypatch, tmp_path):
    """REQUIRE_SIGNATURE=false allows unsigned roadmap."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_REQUIRE_SIG, "false")
    target = tmp_path / "rm.yaml"
    _write_roadmap(target, {"goals": [{"id": "g1", "title": "t"}]})
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(target))
    verdict, doc, diag = read_roadmap()
    assert verdict is RoadmapVerdict.VALID
    assert doc.signature_valid is False  # signature not verified


# emit_roadmap_envelopes


def test_emit_no_router_dry_run():
    doc = RoadmapDocument(
        version=1, operator_id="x", signed_at_iso="",
        signature_hex="", signature_valid=True,
        goals=(
            RoadmapGoal(
                goal_id="g1",
                title="t",
                description="d",
                priority=GoalPriority.MEDIUM,
                target_files=("x.py",),
                success_criteria="",
                depends_on=(),
                max_duration_s=0,
            ),
        ),
        raw_bytes=10,
    )
    outcomes = _run(emit_roadmap_envelopes(doc, router=None))
    assert len(outcomes) == 1
    assert outcomes[0].emitted is False
    assert "dry-run" in outcomes[0].error.lower()


def test_emit_empty_document():
    doc = RoadmapDocument(
        version=1, operator_id="x", signed_at_iso="",
        signature_hex="", signature_valid=True,
        goals=(), raw_bytes=0,
    )
    outcomes = _run(emit_roadmap_envelopes(doc))
    assert outcomes == ()


def test_emit_with_mock_router():
    """Verify router.ingest is awaited + outcomes populated."""
    class _MockRouter:
        def __init__(self):
            self.calls = []

        async def ingest(self, envelope):
            self.calls.append(envelope)
            return f"key-{envelope.signal_id}"

    router = _MockRouter()
    doc = RoadmapDocument(
        version=1, operator_id="x", signed_at_iso="",
        signature_hex="", signature_valid=True,
        goals=(
            RoadmapGoal(
                goal_id="g1",
                title="t",
                description="d",
                priority=GoalPriority.HIGH,
                target_files=("x.py",),
                success_criteria="",
                depends_on=(),
                max_duration_s=0,
            ),
            RoadmapGoal(
                goal_id="g2",
                title="t2",
                description="d2",
                priority=GoalPriority.LOW,
                target_files=("y.py",),
                success_criteria="",
                depends_on=(),
                max_duration_s=0,
            ),
        ),
        raw_bytes=10,
    )
    outcomes = _run(emit_roadmap_envelopes(doc, router=router))
    assert len(outcomes) == 2
    assert all(o.emitted for o in outcomes)
    assert len(router.calls) == 2
    # Verify envelopes have correct source + priority mapping
    sources = {e.source for e in router.calls}
    assert sources == {"roadmap"}
    urgencies = {e.urgency for e in router.calls}
    assert "high" in urgencies  # HIGH → high
    assert "low" in urgencies  # LOW → low


def test_emit_router_exception_recorded():
    class _BrokenRouter:
        async def ingest(self, envelope):
            raise RuntimeError("simulated failure")

    doc = RoadmapDocument(
        version=1, operator_id="x", signed_at_iso="",
        signature_hex="", signature_valid=True,
        goals=(
            RoadmapGoal(
                goal_id="g1",
                title="t",
                description="d",
                priority=GoalPriority.MEDIUM,
                target_files=("x.py",),
                success_criteria="",
                depends_on=(),
                max_duration_s=0,
            ),
        ),
        raw_bytes=10,
    )
    outcomes = _run(emit_roadmap_envelopes(doc, router=_BrokenRouter()))
    assert len(outcomes) == 1
    assert outcomes[0].emitted is False
    assert "simulated failure" in outcomes[0].error


def test_envelope_construction_has_correct_evidence():
    """Verify make_envelope is called with all metadata."""
    goal = RoadmapGoal(
        goal_id="g-critical",
        title="Critical",
        description="d",
        priority=GoalPriority.CRITICAL,
        target_files=("foo.py",),
        success_criteria="passes tests",
        depends_on=("g-dep1", "g-dep2"),
        max_duration_s=3600,
    )
    env = _make_envelope_for_goal(goal)
    assert env is not None
    assert env.source == "roadmap"
    assert env.urgency == "critical"  # CRITICAL → critical
    ev = env.evidence
    assert ev["goal_id"] == "g-critical"
    assert ev["priority"] == "critical"
    assert ev["depends_on"] == ["g-dep1", "g-dep2"]
    assert ev["max_duration_s"] == 3600


# process_roadmap (top-level)


def test_process_master_off():
    report = _run(process_roadmap())
    assert report.master_enabled is False
    assert report.verdict is RoadmapVerdict.NO_ROADMAP


def test_process_no_file(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(tmp_path / "absent.yaml"))
    report = _run(process_roadmap())
    assert report.verdict is RoadmapVerdict.NO_ROADMAP


def test_process_valid_with_router(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_HMAC_SECRET, "k")
    target = tmp_path / "rm.yaml"
    _write_roadmap(
        target,
        {"goals": [{
            "id": "g1", "title": "t",
            "target_files": ["x.py"],
        }]},
        secret="k",
    )
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(target))

    class _MockRouter:
        def __init__(self):
            self.calls = []

        async def ingest(self, envelope):
            self.calls.append(envelope)
            return "ok-key"

    router = _MockRouter()
    report = _run(process_roadmap(router=router))
    assert report.verdict is RoadmapVerdict.VALID
    assert len(report.emit_outcomes) == 1
    assert report.emit_outcomes[0].emitted is True
    assert len(router.calls) == 1


def test_process_invalid_signature_no_emit(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    target = tmp_path / "rm.yaml"
    _write_roadmap(target, {"goals": [{"id": "g1", "title": "t"}]})
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(target))

    class _MockRouter:
        def __init__(self):
            self.calls = []

        async def ingest(self, envelope):
            self.calls.append(envelope)
            return "k"

    router = _MockRouter()
    report = _run(process_roadmap(router=router))
    assert report.verdict is RoadmapVerdict.INVALID_SIGNATURE
    # NO envelopes emitted when signature invalid
    assert len(router.calls) == 0
    assert report.emit_outcomes == ()


# Sync wrapper


def test_sync_wrapper_outside_loop(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = process_roadmap_sync()
    assert isinstance(report, RoadmapReport)


def test_sync_wrapper_inside_loop_returns_malformed(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    async def inner():
        return process_roadmap_sync()
    report = asyncio.run(inner())
    assert report.verdict is RoadmapVerdict.MALFORMED
    assert "event loop" in report.diagnostic.lower()


# Persistence


def test_persist_writes_on_valid(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    monkeypatch.setenv(_ENV_HMAC_SECRET, "k")
    target = tmp_path / "rm.yaml"
    _write_roadmap(
        target,
        {"goals": [{"id": "g1", "title": "t", "target_files": ["x.py"]}]},
        secret="k",
    )
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(target))
    _run(process_roadmap())
    assert ledger_path().exists()


def test_persist_skips_no_roadmap(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(tmp_path / "absent.yaml"))
    _run(process_roadmap())
    assert not ledger_path().exists()


def test_persist_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "false")
    target = tmp_path / "rm.yaml"
    _write_roadmap(target, {"goals": []})
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(target))
    _run(process_roadmap())
    assert not ledger_path().exists()


# Renderer


def test_format_panel_master_off():
    out = format_roadmap_panel()
    assert "disabled" in out


def test_format_panel_with_report(monkeypatch, tmp_path):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_HMAC_SECRET, "k")
    target = tmp_path / "rm.yaml"
    _write_roadmap(
        target,
        {"goals": [{
            "id": "g1", "title": "Test goal",
            "target_files": ["x.py"], "priority": "high",
        }]},
        secret="k",
    )
    monkeypatch.setenv(_ENV_ROADMAP_PATH, str(target))
    report = _run(process_roadmap())
    out = format_roadmap_panel(report)
    assert "Roadmap Reader" in out
    assert "valid" in out
    assert "g1" in out


# to_dict


def test_goal_to_dict():
    g = RoadmapGoal(
        goal_id="g", title="t", description="d",
        priority=GoalPriority.HIGH,
        target_files=("x.py",), success_criteria="",
        depends_on=(), max_duration_s=0,
    )
    d = g.to_dict()
    assert d["schema_version"] == ROADMAP_READER_SCHEMA_VERSION


def test_document_to_dict():
    d = RoadmapDocument(
        version=1, operator_id="x", signed_at_iso="",
        signature_hex="abc", signature_valid=True,
        goals=(), raw_bytes=0,
    )
    out = d.to_dict()
    assert out["schema_version"] == ROADMAP_READER_SCHEMA_VERSION


def test_outcome_to_dict():
    o = GoalEmitOutcome(
        goal_id="g", emitted=True, idempotency_key="k", error="",
    )
    d = o.to_dict()
    assert d["kind"] == "emit"


def test_report_to_dict():
    r = RoadmapReport(
        evaluated_at_unix=1.0, master_enabled=True,
        verdict=RoadmapVerdict.VALID, document=None,
        emit_outcomes=(), diagnostic="x", elapsed_s=0.0,
    )
    d = r.to_dict()
    assert d["schema_version"] == ROADMAP_READER_SCHEMA_VERSION


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/roadmap_reader.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "verdict_taxonomy_closed",
        "priority_taxonomy_closed",
        "authority_asymmetry",
        "master_default_false",
        "composes_canonical",
    ],
)
def test_pin_canonical(_canonical, name_part):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(p for p in pins if name_part in p.invariant_name)
    assert pin.validate(tree, src) == ()


def test_pin_verdict_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "verdict_taxonomy_closed" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class RoadmapVerdict(str, enum.Enum):\n"
        "    NO_ROADMAP = 'no_roadmap'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_priority_drift():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "priority_taxonomy_closed" in p.invariant_name
    )
    bad = (
        "import enum\n"
        "class GoalPriority(str, enum.Enum):\n"
        "    CRITICAL = 'critical'\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_authority_forbids_tool_executor():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.tool_executor "
        "import x\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_composes_synthetic():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    bad = "# no canonical surfaces\n"
    assert pin.validate(ast.parse(bad), bad)


# Flag registry


class _CapturingRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_seed_count():
    reg = _CapturingRegistry()
    count = register_flags(reg)
    assert count == 8


def test_flag_master_default_false():
    reg = _CapturingRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False


def test_flag_require_signature_default_true():
    reg = _CapturingRegistry()
    register_flags(reg)
    req = next(
        s for s in reg.registered if s.name == _ENV_REQUIRE_SIG
    )
    assert req.default is True


# SSE


def test_sse_event_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert (
        ios.EVENT_TYPE_ROADMAP_PROCESSED == "roadmap_processed"
    )
    assert "roadmap_processed" in ios._VALID_EVENT_TYPES


# End-to-end with REAL UnifiedIntakeRouter (smoke)


def test_real_envelope_construction_passes_validation(monkeypatch):
    """make_envelope is canonical → constructed envelopes
    must pass the canonical IntentEnvelope validator (source=
    'roadmap' is in _VALID_SOURCES, all required fields)."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    goal = RoadmapGoal(
        goal_id="real-test",
        title="Test",
        description="real validation",
        priority=GoalPriority.MEDIUM,
        target_files=("backend/foo.py",),
        success_criteria="passes",
        depends_on=(),
        max_duration_s=0,
    )
    env = _make_envelope_for_goal(goal)
    # If make_envelope didn't accept our shape, this would be None.
    assert env is not None
    # Verify it has all required fields per IntentEnvelope schema.
    from backend.core.ouroboros.governance.intake.intent_envelope import (
        IntentEnvelope,
    )
    assert isinstance(env, IntentEnvelope)
    assert env.source == "roadmap"
    assert env.urgency in ("normal", "critical", "high", "low")
    assert env.target_files == ("backend/foo.py",)
    assert env.confidence == 0.95

"""Slice 102 — the Autonomous Graduation Soak (Core Ignition proof).

Proves the Slice 96 Autonomous Graduation Engine can EARN and execute a master-
flag flip from telemetry — without ever being manually flipped, and without ever
violating the Tiered Authority invariant.

The fabric is fully hermetic: every graduation + cognitive ledger is redirected
to a tmp path, the LLM provider is never touched (the cognitive substrates run on
synthetic data via injectable seams), and the "ignited state" is asserted against
a FAKE environ dict. NOTHING in real production state is mutated.

HONEST FRAMING: this proves the engine's MECHANISM is correct (it can process
telemetry, auto-flip a STANDARD cognitive master, and structurally refuse to flip
a SAFETY/governance master). It does NOT shortcut real production graduation,
which still requires genuine wall-clock empirical evidence (§41). The synthetic
epochs stand in for that evidence so the mechanism can be verified deterministically.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from backend.core.ouroboros.governance.flag_registry import (
    Category,
    FlagRegistry,
    FlagSpec,
    FlagType,
)
from backend.core.ouroboros.governance.autonomous_graduation_engine import (
    GraduationDisposition,
    GraduationTier,
    evaluate_graduations,
    execute_graduations,
)
from backend.core.ouroboros.governance import graduation_override_ledger as GOL
from backend.core.ouroboros.governance import domain_entropy_engine as DEE
from backend.core.ouroboros.governance.adversarial_autobiography import (
    AutobiographyFinding,
    audit_autobiography,
)
from backend.core.ouroboros.governance.auto_committer import ov_signature_substring
from backend.core.ouroboros.governance.belief_revision_ledger import (
    EvidenceKind,
    evaluate_recent_beliefs,
    record_claim,
    record_evidence,
)

# The dormant cognitive substrates the engine should EARN the right to flip
# (STANDARD tier — observational/proactive, never grant new mutation authority).
_STANDARD_CANDIDATES = {
    "JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED": "backend/core/ouroboros/governance/domain_entropy_engine.py",
    "JARVIS_SLEEP_DAEMON_ENABLED": "backend/core/ouroboros/governance/sleep_daemon.py",
    "JARVIS_COUNTERFACTUAL_REHEARSAL_ENABLED": "backend/core/ouroboros/governance/counterfactual_rehearsal_mode.py",
}
# Core governance/safety flags the engine must NEVER auto-flip (Tiered Authority).
_SAFETY_CANDIDATES = {
    "JARVIS_SEMANTIC_GUARD_ENABLED": "backend/core/ouroboros/governance/semantic_guardian.py",
    "JARVIS_GOVERNANCE_BOUNDARY_GATE_ENABLED": "backend/core/ouroboros/governance/governance_boundary_gate.py",
}
_REQUIRED_CLEAN = 3


class _SoakLedger:
    """A graduation ledger driven by EARNED clean epochs. A flag is eligible only
    after >= required clean, regression-free sessions — mirroring the real cadence
    policy (clean >= required AND runner == 0)."""

    def __init__(self, flags, required=_REQUIRED_CLEAN):
        self._required = required
        self._clean = {f: 0 for f in flags}
        self._runner = {f: 0 for f in flags}

    def record_clean_epoch(self):
        for f in self._clean:
            self._clean[f] += 1

    def record_runner_failure(self, flag):
        self._runner[flag] = self._runner.get(flag, 0) + 1

    def is_eligible(self, flag):
        return self._clean.get(flag, 0) >= self._required and self._runner.get(flag, 0) == 0

    def eligible_flags(self):
        return sorted(f for f in self._clean if self.is_eligible(f))

    def progress(self, flag):
        return {
            "clean": self._clean.get(flag, 0),
            "infra": 0,
            "runner": self._runner.get(flag, 0),
            "migration": 0,
            "unique_sessions": self._clean.get(flag, 0),
            "required": self._required,
        }


def _soak_registry():
    reg = FlagRegistry()
    for name, src in _STANDARD_CANDIDATES.items():
        reg.register(FlagSpec(
            name=name, type=FlagType.BOOL, default=False,
            description="cognitive substrate (Slice 101)",
            category=Category.OBSERVABILITY, source_file=src,
        ))
    for name, src in _SAFETY_CANDIDATES.items():
        reg.register(FlagSpec(
            name=name, type=FlagType.BOOL, default=False,
            description="core cage / safety gate",
            category=Category.SAFETY, source_file=src,
        ))
    return reg


def _no_ast_drift():
    return ()  # zero invariant violations — 100% AST stability


class _FakeRouter:
    def __init__(self):
        self.ingested = []

    async def ingest(self, envelope):
        self.ingested.append(envelope)
        return "enqueued"


def _ov_clean_commit_log():
    body = "feat(governance): clean synthetic op\n\n" + ov_signature_substring()
    return f"cafe1234\n1700000000\n{body}__END_HEADER__\n__OV_AUTOBIO__\n"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """All masters ON; every graduation + cognitive ledger redirected to tmp.
    The boot applier is enabled so we can assert the ignited state — but the
    override ledger is tmp, so NO real flag is touched."""
    # Graduation machinery
    monkeypatch.setenv("JARVIS_AUTONOMOUS_GRADUATION_ENGINE_ENABLED", "true")
    monkeypatch.setenv("JARVIS_GRADUATION_OVERRIDE_APPLY_ENABLED", "true")
    # Slice 103: this soak proves the ACTUATION path, so explicitly un-shadow.
    # (Production now defaults to shadow-mode TRUE — see test_slice103_shadow_mode.)
    monkeypatch.setenv("JARVIS_GRADUATION_SHADOW_MODE", "false")
    monkeypatch.setenv("JARVIS_GRADUATION_OVERRIDE_LEDGER_PATH", str(tmp_path / "overrides.jsonl"))
    monkeypatch.setenv("JARVIS_GRADUATION_ADVISORY_LEDGER_PATH", str(tmp_path / "advisories.jsonl"))
    monkeypatch.setenv("JARVIS_GRADUATION_LEDGER_PATH", str(tmp_path / "grad_ledger.jsonl"))
    # Cognitive substrates (exercised to produce clean telemetry)
    monkeypatch.setenv("JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED", "1")
    monkeypatch.setenv("JARVIS_ADVERSARIAL_AUTOBIOGRAPHY_ENABLED", "1")
    monkeypatch.setenv("JARVIS_AUTOBIOGRAPHY_LEDGER_PATH", str(tmp_path / "autobio.jsonl"))
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_ENABLED", "1")
    monkeypatch.setenv("JARVIS_BELIEF_REVISION_LEDGER_PATH", str(tmp_path / "belief.jsonl"))
    yield


# === Phase 1+2: the epoch fabric generates clean, regression-free telemetry ==

def test_synthetic_epochs_produce_clean_cage_compliant_telemetry(tmp_path):
    # Entropy engine: proactive exploration emits ONLY governed ops (cage intact).
    router = _FakeRouter()
    clusters = [
        {"cluster_id": "well_known", "kind": "git_commit", "size": 80},
        {"cluster_id": "uncharted", "kind": "goal", "size": 2},
    ]
    scan = asyncio.run(DEE.run_curiosity_scan_once(
        router=router, clusters=clusters,
        load_report=SimpleNamespace(verdict=SimpleNamespace(value="normal")),
    ))
    assert scan.emitted >= 1
    for env in router.ingested:
        assert env.source == "exploration" and env.urgency == "low"  # cage-eligible

    # Adversarial autobiography over clean synthetic commits → NO escape.
    audit = audit_autobiography(
        force_refresh=True,
        git_log_runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout=_ov_clean_commit_log()),
        git_show_runner=lambda *a, **k: SimpleNamespace(returncode=0, stdout="+++ b/x.py\n+print('ok')\n"),
    )
    assert audit.finding is AutobiographyFinding.CORPUS_CLEAN
    assert audit.escape_count == 0

    # Synthetic Soul: clean belief telemetry is recorded + retrievable.
    c = record_claim("synthetic op succeeds", "soak/clean", target_files=["a.py"])
    record_evidence(c.claim_id, EvidenceKind.AFFIRMING)
    reports = evaluate_recent_beliefs()
    assert len(reports) >= 1


# === Phase 3a: graduation must be EARNED — no premature ignition =============

def test_graduation_is_earned_not_premature():
    ledger = _SoakLedger(list(_STANDARD_CANDIDATES) + list(_SAFETY_CANDIDATES))
    registry = _soak_registry()
    # Below threshold: nothing is eligible → nothing graduates.
    ledger.record_clean_epoch()  # 1 of 3
    report = evaluate_graduations(
        ledger=ledger, registry=registry, validate_all_fn=_no_ast_drift,
    )
    assert report.auto_flipped == ()
    assert report.advisories == ()


# === Phase 3b: at threshold, the engine auto-flips STANDARD, advises SAFETY ==

def test_engine_autoflips_standard_and_advises_safety(tmp_path):
    ledger = _SoakLedger(list(_STANDARD_CANDIDATES) + list(_SAFETY_CANDIDATES))
    registry = _soak_registry()
    for _ in range(_REQUIRED_CLEAN):       # earn eligibility
        ledger.record_clean_epoch()

    report = evaluate_graduations(
        ledger=ledger, registry=registry, validate_all_fn=_no_ast_drift,
    )
    # STANDARD cognitive substrates earned an AUTO_FLIP.
    assert set(report.auto_flipped) == set(_STANDARD_CANDIDATES)
    # SAFETY/governance flags routed to APPROVAL_ADVISORY — never auto-flipped.
    assert set(report.advisories) == set(_SAFETY_CANDIDATES)

    # Per-decision tier/disposition correctness.
    by_flag = {d.flag_name: d for d in report.decisions}
    for f in _STANDARD_CANDIDATES:
        assert by_flag[f].tier is GraduationTier.STANDARD
        assert by_flag[f].disposition is GraduationDisposition.AUTO_FLIP
    for f in _SAFETY_CANDIDATES:
        assert by_flag[f].tier is GraduationTier.SAFETY
        assert by_flag[f].disposition is GraduationDisposition.APPROVAL_ADVISORY

    # Execute → immutable receipts; SAFETY structurally refused at the override ledger.
    result = execute_graduations(report)
    assert set(result.recorded_overrides) == set(_STANDARD_CANDIDATES)
    assert set(result.advisories_emitted) == set(_SAFETY_CANDIDATES)
    overrides = {r.flag_name for r in GOL.all_overrides()}
    assert overrides == set(_STANDARD_CANDIDATES)
    for f in _SAFETY_CANDIDATES:
        assert f not in overrides   # Tiered Authority: never in the auto-flip ledger


# === Phase 4: the Ignited State ============================================

def test_ignited_state_activates_cognitive_substrates_only(tmp_path):
    ledger = _SoakLedger(list(_STANDARD_CANDIDATES) + list(_SAFETY_CANDIDATES))
    registry = _soak_registry()
    for _ in range(_REQUIRED_CLEAN):
        ledger.record_clean_epoch()
    report = evaluate_graduations(
        ledger=ledger, registry=registry, validate_all_fn=_no_ast_drift,
    )
    execute_graduations(report)

    # Boot applier on a FAKE environ — the "next boot" ignited state.
    fake_env = {}
    applied = GOL.apply_overrides_to_environ(fake_env)

    # The cognitive substrates are now active...
    for f in _STANDARD_CANDIDATES:
        assert fake_env.get(f) == "true"
        assert f in applied
    # ...while every core governance/safety flag remains dormant (advisory only).
    for f in _SAFETY_CANDIDATES:
        assert f not in fake_env
        assert f not in applied


def test_operator_env_precedence_is_respected(tmp_path):
    """If the operator has explicitly set a flag, the auto-flip does NOT override
    it — the human remains the structural authority (zero-order doll)."""
    ledger = _SoakLedger(list(_STANDARD_CANDIDATES))
    registry = _soak_registry()
    for _ in range(_REQUIRED_CLEAN):
        ledger.record_clean_epoch()
    execute_graduations(evaluate_graduations(
        ledger=ledger, registry=registry, validate_all_fn=_no_ast_drift,
    ))
    # Operator pinned one flag OFF — the applier must not clobber it.
    fake_env = {"JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED": "false"}
    GOL.apply_overrides_to_environ(fake_env)
    assert fake_env["JARVIS_DOMAIN_ENTROPY_ENGINE_ENABLED"] == "false"

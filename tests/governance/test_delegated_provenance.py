"""Slice 20 — Delegated Provenance: cryptographic operator intent → the cage.

Adversarial proof matrix from the spec
(docs/superpowers/specs/2026-07-14-slice20-delegated-provenance.md §9):
a VERIFIED operator-signed roadmap goal raises a governance self-modification
from BLOCKED to APPROVAL_REQUIRED (the human-gated ceiling — never auto-apply);
every forgery/decay path — hallucinated goal, tampered document, expired or
missing signature, out-of-scope target, kernel/security surface, feature off —
fails closed to the exact pre-Slice-20 block.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from backend.core.ouroboros.governance import delegated_provenance as DP
from backend.core.ouroboros.governance.risk_engine import (
    ChangeType,
    OperationProfile,
    RiskEngine,
    RiskTier,
)
from backend.core.ouroboros.governance.roadmap_reader import (
    _build_signing_payload,
    compute_signature,
)

SECRET = "test-operator-secret-slice20"
GOV_FILE = "backend/core/ouroboros/governance/plan_generator.py"
KERNEL_FILE = "backend/core/unified_supervisor.py"
SECURITY_FILE = "backend/auth/session_tokens.py"
PLAIN_FILE = "backend/vision/frame_math.py"


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_signed_roadmap(
    path: Path,
    *,
    goals,
    secret: str = SECRET,
    signed_at: str | None = None,
    tamper_after_signing: bool = False,
) -> None:
    doc = {
        "version": 1,
        "operator_id": "derek",
        "signed_at": signed_at or _iso(datetime.now(timezone.utc)),
        "goals": goals,
    }
    doc["signature"] = compute_signature(_build_signing_payload(doc), secret)
    if tamper_after_signing:
        doc["goals"][0]["title"] = "tampered-title"
    path.write_text(json.dumps(doc), encoding="utf-8")


def _goal(goal_id: str, targets) -> dict:
    return {
        "id": goal_id,
        "title": f"goal {goal_id}",
        "description": "operator-authored work",
        "priority": "high",
        "target_files": list(targets),
    }


@pytest.fixture()
def armed(tmp_path, monkeypatch):
    """Armed provenance + a valid signed roadmap covering GOV_FILE."""
    roadmap = tmp_path / "roadmap.json"
    _write_signed_roadmap(roadmap, goals=[_goal("g-gov", [GOV_FILE])])
    monkeypatch.setenv("JARVIS_ROADMAP_READER_PATH", str(roadmap))
    monkeypatch.setenv("JARVIS_ROADMAP_READER_HMAC_SECRET", SECRET)
    monkeypatch.setenv("JARVIS_ROADMAP_READER_REQUIRE_SIGNATURE", "true")
    monkeypatch.setenv("JARVIS_DELEGATED_PROVENANCE_ENABLED", "true")
    monkeypatch.delenv("JARVIS_PROVENANCE_MAX_AGE_S", raising=False)
    monkeypatch.delenv("JARVIS_DELEGATED_PROVENANCE_SOURCES", raising=False)
    DP.reset_provenance_cache_for_tests()
    yield roadmap
    DP.reset_provenance_cache_for_tests()


def _classify(files, *, source="roadmap", provenance=None):
    profile = OperationProfile(
        files_affected=[Path(f) for f in files],
        change_type=ChangeType.MODIFY,
        blast_radius=1,
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
        test_scope_confidence=0.9,
        source=source,
        provenance=provenance,
    )
    return RiskEngine().classify(profile)


def _claim(goal_id="g-gov"):
    return {
        "schema_version": DP.DELEGATED_PROVENANCE_SCHEMA_VERSION,
        "kind": DP.CLAIM_KIND_ROADMAP_READER,
        "goal_id": goal_id,
    }


# ── 1. The happy path — and its hard ceiling ─────────────────────────


def test_verified_claim_raises_block_to_approval_required(armed):
    c = _classify([GOV_FILE], provenance=_claim())
    assert c.tier is RiskTier.APPROVAL_REQUIRED
    assert c.reason_code == "delegated_provenance_self_modification"


def test_verified_claim_never_reaches_safe_auto(armed):
    """The ceiling: a valid token yields APPROVAL_REQUIRED, nothing looser."""
    c = _classify([GOV_FILE], provenance=_claim())
    assert c.tier is not RiskTier.SAFE_AUTO
    assert c.tier is not RiskTier.NOTIFY_APPLY


def test_without_claim_stays_blocked(armed):
    """The presence of a signed roadmap alone delegates nothing —
    the op must carry the claim."""
    c = _classify([GOV_FILE], provenance=None)
    assert c.tier is RiskTier.BLOCKED


# ── 2. Forgery / decay paths — every one fails closed ────────────────


def test_hallucinated_goal_blocked(armed):
    """A goal_id O+V invented has no entry in the signed document."""
    c = _classify([GOV_FILE], provenance=_claim("g-hallucinated"))
    assert c.tier is RiskTier.BLOCKED


def test_tampered_document_blocked(armed, monkeypatch):
    """Any post-signing edit breaks the HMAC over the canonical payload."""
    _write_signed_roadmap(
        armed, goals=[_goal("g-gov", [GOV_FILE])], tamper_after_signing=True,
    )
    DP.reset_provenance_cache_for_tests()
    c = _classify([GOV_FILE], provenance=_claim())
    assert c.tier is RiskTier.BLOCKED


def test_wrong_secret_blocked(armed, monkeypatch):
    monkeypatch.setenv("JARVIS_ROADMAP_READER_HMAC_SECRET", "attacker-secret")
    DP.reset_provenance_cache_for_tests()
    c = _classify([GOV_FILE], provenance=_claim())
    assert c.tier is RiskTier.BLOCKED


def test_missing_secret_blocked(armed, monkeypatch):
    monkeypatch.delenv("JARVIS_ROADMAP_READER_HMAC_SECRET", raising=False)
    DP.reset_provenance_cache_for_tests()
    c = _classify([GOV_FILE], provenance=_claim())
    assert c.tier is RiskTier.BLOCKED


def test_expired_signature_blocked(armed, monkeypatch):
    stale = _iso(datetime.now(timezone.utc) - timedelta(days=90))
    _write_signed_roadmap(
        armed, goals=[_goal("g-gov", [GOV_FILE])], signed_at=stale,
    )
    DP.reset_provenance_cache_for_tests()
    c = _classify([GOV_FILE], provenance=_claim())
    assert c.tier is RiskTier.BLOCKED


def test_unparseable_signed_at_blocked(armed):
    _write_signed_roadmap(
        armed, goals=[_goal("g-gov", [GOV_FILE])], signed_at="not-a-date",
    )
    DP.reset_provenance_cache_for_tests()
    c = _classify([GOV_FILE], provenance=_claim())
    assert c.tier is RiskTier.BLOCKED


def test_missing_roadmap_blocked(armed):
    armed.unlink()
    DP.reset_provenance_cache_for_tests()
    c = _classify([GOV_FILE], provenance=_claim())
    assert c.tier is RiskTier.BLOCKED


def test_unsigned_dev_mode_never_delegates(armed, monkeypatch):
    """REQUIRE_SIGNATURE=false lets the reader emit goals, but delegation
    demands the cryptographic property itself — signature_valid must be
    True, not merely a permissive verdict."""
    doc = {
        "version": 1, "operator_id": "derek",
        "signed_at": _iso(datetime.now(timezone.utc)),
        "goals": [_goal("g-gov", [GOV_FILE])],
    }  # no signature field at all
    armed.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("JARVIS_ROADMAP_READER_REQUIRE_SIGNATURE", "false")
    DP.reset_provenance_cache_for_tests()
    c = _classify([GOV_FILE], provenance=_claim())
    assert c.tier is RiskTier.BLOCKED


# ── 3. Scope binding — a signed goal delegates ONLY its declared files ─


def test_out_of_scope_target_blocked(armed):
    """A goal scoped to README.md can never authorize a cage edit."""
    _write_signed_roadmap(armed, goals=[_goal("g-readme", ["README.md"])])
    DP.reset_provenance_cache_for_tests()
    c = _classify([GOV_FILE], provenance=_claim("g-readme"))
    assert c.tier is RiskTier.BLOCKED


def test_partial_scope_blocked(armed):
    """EVERY touched file must be inside the goal's scope."""
    c = _classify([GOV_FILE, PLAIN_FILE], provenance=_claim())
    assert c.tier is RiskTier.BLOCKED


def test_goal_without_scope_blocked(armed):
    _write_signed_roadmap(armed, goals=[_goal("g-noscope", [])])
    DP.reset_provenance_cache_for_tests()
    c = _classify([GOV_FILE], provenance=_claim("g-noscope"))
    assert c.tier is RiskTier.BLOCKED


def test_parent_escape_never_matches_scope():
    assert DP._file_in_goal_scope("../" + GOV_FILE, (GOV_FILE,)) is False
    assert DP._file_in_goal_scope("a/../b.py", ("b.py",)) is False


# ── 4. The unconditional floors are untouchable ──────────────────────


def test_valid_token_kernel_file_still_blocked(armed):
    _write_signed_roadmap(armed, goals=[_goal("g-kern", [KERNEL_FILE])])
    DP.reset_provenance_cache_for_tests()
    c = _classify([KERNEL_FILE], provenance=_claim("g-kern"))
    assert c.tier is RiskTier.BLOCKED
    assert "kernel" in c.reason_code


def test_valid_token_security_file_still_blocked(armed):
    _write_signed_roadmap(armed, goals=[_goal("g-sec", [SECURITY_FILE])])
    DP.reset_provenance_cache_for_tests()
    c = _classify([SECURITY_FILE], provenance=_claim("g-sec"))
    assert c.tier is RiskTier.BLOCKED
    assert "security" in c.reason_code


# ── 5. The Delegated Authority Matrix (source axis) ──────────────────


def test_unlisted_source_cannot_present_token(armed):
    c = _classify([GOV_FILE], source="exploration", provenance=_claim())
    assert c.tier is RiskTier.BLOCKED


def test_matrix_is_env_configurable(armed, monkeypatch):
    monkeypatch.setenv("JARVIS_DELEGATED_PROVENANCE_SOURCES", "exploration")
    c = _classify([GOV_FILE], source="exploration", provenance=_claim())
    assert c.tier is RiskTier.APPROVAL_REQUIRED
    # ...and the source that was removed loses the privilege.
    c2 = _classify([GOV_FILE], source="roadmap", provenance=_claim())
    assert c2.tier is RiskTier.BLOCKED


# ── 6. Master flag — byte-identical when off ─────────────────────────


def test_flag_off_is_byte_identical(armed, monkeypatch):
    monkeypatch.setenv("JARVIS_DELEGATED_PROVENANCE_ENABLED", "false")
    c = _classify([GOV_FILE], provenance=_claim())
    assert c.tier is RiskTier.BLOCKED
    assert DP.claim_for_goal("g-gov") is None
    assert DP.claim_for_targets([GOV_FILE]) is None
    assert DP.extract_claim_from_evidence_json(
        json.dumps({"provenance": _claim()})
    ) is None


def test_non_selfmod_files_unaffected(armed):
    """Provenance only participates in the governance self-mod branch —
    an ordinary file classifies exactly as before, claim or not."""
    with_claim = _classify([PLAIN_FILE], provenance=_claim())
    without = _classify([PLAIN_FILE], provenance=None)
    assert with_claim.tier is without.tier
    assert with_claim.reason_code == without.reason_code


# ── 7. Claim minting + evidence pipe helpers ─────────────────────────


def test_claim_for_targets_matches_signed_goal(armed):
    claim = DP.claim_for_targets([GOV_FILE])
    assert claim is not None and claim["goal_id"] == "g-gov"
    assert claim["kind"] == DP.CLAIM_KIND_ROADMAP_READER


def test_claim_for_targets_refuses_uncovered(armed):
    assert DP.claim_for_targets([PLAIN_FILE]) is None
    assert DP.claim_for_targets([GOV_FILE, PLAIN_FILE]) is None


def test_claim_for_targets_refuses_unsigned(armed, monkeypatch):
    monkeypatch.setenv("JARVIS_ROADMAP_READER_HMAC_SECRET", "wrong")
    DP.reset_provenance_cache_for_tests()
    assert DP.claim_for_targets([GOV_FILE]) is None


def test_evidence_json_roundtrip(armed):
    blob = json.dumps({"work_order": True, "provenance": _claim()})
    got = DP.extract_claim_from_evidence_json(blob)
    assert got == _claim()
    assert DP.extract_claim_from_evidence_json("") is None
    assert DP.extract_claim_from_evidence_json("not-json{") is None
    assert DP.extract_claim_from_evidence_json(
        json.dumps({"provenance": "a-string-not-a-dict"})
    ) is None


# ── 8. The verifier never raises ─────────────────────────────────────


@pytest.mark.parametrize("garbage", [
    None, 42, "claim", [], {}, {"kind": "evil"}, {"goal_id": ""},
    {"kind": DP.CLAIM_KIND_ROADMAP_READER},  # no goal_id
    {"kind": DP.CLAIM_KIND_ROADMAP_READER, "goal_id": 7e9},
])
def test_verifier_never_raises_on_garbage(armed, garbage):
    v = DP.verify_provenance_claim(
        garbage, source="roadmap", file_strs=[GOV_FILE],
    )
    assert v.valid is False


def test_verifier_refuses_empty_files(armed):
    v = DP.verify_provenance_claim(_claim(), source="roadmap", file_strs=[])
    assert v.valid is False


# ── 9. Cache correctness — a roadmap edit is seen ────────────────────


def test_cache_invalidates_on_file_change(armed):
    v1 = DP.verify_provenance_claim(
        _claim(), source="roadmap", file_strs=[GOV_FILE],
    )
    assert v1.valid is True
    # Operator re-signs with the goal REMOVED — next verify must see it.
    time.sleep(0.01)  # ensure mtime tick
    _write_signed_roadmap(armed, goals=[_goal("g-other", ["README.md"])])
    v2 = DP.verify_provenance_claim(
        _claim(), source="roadmap", file_strs=[GOV_FILE],
    )
    assert v2.valid is False

"""Slice 212 — Boot-Time Runtime Attestation & Integrity Gate.

The lived failure this hardens against: the 211 rebuild silently used STALE
code — a dirty compose file blocked the ff-merge, Docker rebuilt Slice-208
sources, and a buggy monitor false-positived. Nothing in the runtime itself
could tell us the image didn't contain the code we thought it did.

DESIGN (corrected from the plan):
- The image deliberately ships no .git, and checking live origin/main on every
  boot would FALSE-TRIP under restart:always after any later legitimate merge.
- So: the image is STAMPED at build time (build args -> .build_attestation.json
  with {commit, dirty}), and at boot the stamp is compared against an
  OPERATOR-PINNED expected commit (env, set by the launch path at launch time).
  Restarts keep the same pin -> no false trips; a stale/dirty build trips it.
- Strict mode (default ON when enabled) -> DeploymentIntegrityMismatch raised
  BEFORE the GLS fail-soft boot block: state -> FAILED, loop never runs.
- Gated JARVIS_RUNTIME_ATTESTATION_ENABLED default-FALSE (OFF = byte-identical).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.runtime_attestation import (
    AttestationVerdict,
    DeploymentIntegrityMismatch,
    attestation_enabled,
    enforce,
    verify,
)

_GOV = Path(__file__).resolve().parents[2] / "backend" / "core" \
    / "ouroboros" / "governance"


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in (
        "JARVIS_RUNTIME_ATTESTATION_ENABLED",
        "JARVIS_RUNTIME_ATTESTATION_STRICT",
        "JARVIS_ATTESTATION_EXPECTED_COMMIT",
        "JARVIS_BUILD_ATTESTATION_PATH",
    ):
        monkeypatch.delenv(k, raising=False)
    yield


def _stamp(tmp_path, commit="a6e72d4aad" + "0" * 30, dirty="false"):
    p = tmp_path / ".build_attestation.json"
    p.write_text(json.dumps({"commit": commit, "dirty": dirty}), encoding="utf-8")
    return p


# ===========================================================================
# A — gate + disabled behavior
# ===========================================================================

def test_disabled_by_default():
    assert attestation_enabled() is False


def test_disabled_verdict_short_circuits(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_BUILD_ATTESTATION_PATH", str(_stamp(tmp_path)))
    v, _ = verify()
    assert v is AttestationVerdict.DISABLED


# ===========================================================================
# B — verdicts
# ===========================================================================

def test_match_on_pinned_prefix(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_ATTESTATION_ENABLED", "1")
    monkeypatch.setenv("JARVIS_BUILD_ATTESTATION_PATH", str(_stamp(tmp_path)))
    monkeypatch.setenv("JARVIS_ATTESTATION_EXPECTED_COMMIT", "a6e72d4aad")
    v, _ = verify()
    assert v is AttestationVerdict.MATCH


def test_mismatch_on_stale_image(tmp_path, monkeypatch):
    """THE failure we lived: image stamped with the stale commit while the
    operator pinned the freshly-merged one."""
    monkeypatch.setenv("JARVIS_RUNTIME_ATTESTATION_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_BUILD_ATTESTATION_PATH",
        str(_stamp(tmp_path, commit="bb0aab05ef" + "0" * 30)),
    )
    monkeypatch.setenv("JARVIS_ATTESTATION_EXPECTED_COMMIT", "a6e72d4aad")
    v, detail = verify()
    assert v is AttestationVerdict.MISMATCH
    assert "bb0aab05ef" in detail and "a6e72d4aad" in detail


def test_dirty_build_is_flagged(tmp_path, monkeypatch):
    """The OTHER half of the lived failure: building from a dirty tree."""
    monkeypatch.setenv("JARVIS_RUNTIME_ATTESTATION_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_BUILD_ATTESTATION_PATH", str(_stamp(tmp_path, dirty="true")),
    )
    monkeypatch.setenv("JARVIS_ATTESTATION_EXPECTED_COMMIT", "a6e72d4aad")
    v, _ = verify()
    assert v is AttestationVerdict.DIRTY_BUILD


def test_unstamped_image_detected(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_ATTESTATION_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_BUILD_ATTESTATION_PATH", str(tmp_path / "missing.json"),
    )
    monkeypatch.setenv("JARVIS_ATTESTATION_EXPECTED_COMMIT", "a6e72d4aad")
    v, _ = verify()
    assert v is AttestationVerdict.UNSTAMPED


def test_no_pin_is_unpinned_not_mismatch(tmp_path, monkeypatch):
    """No expected commit set -> UNPINNED (warn), NOT a fail — otherwise every
    casual `docker compose up` without the launch path would brick the soak."""
    monkeypatch.setenv("JARVIS_RUNTIME_ATTESTATION_ENABLED", "1")
    monkeypatch.setenv("JARVIS_BUILD_ATTESTATION_PATH", str(_stamp(tmp_path)))
    v, _ = verify()
    assert v is AttestationVerdict.UNPINNED


# ===========================================================================
# C — enforce(): strict fail-closed vs warn
# ===========================================================================

def test_enforce_raises_on_mismatch_strict(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_ATTESTATION_ENABLED", "1")
    monkeypatch.setenv(
        "JARVIS_BUILD_ATTESTATION_PATH",
        str(_stamp(tmp_path, commit="bb0aab05ef" + "0" * 30)),
    )
    monkeypatch.setenv("JARVIS_ATTESTATION_EXPECTED_COMMIT", "a6e72d4aad")
    with pytest.raises(DeploymentIntegrityMismatch) as ei:
        enforce()
    assert "DEPLOYMENT_INTEGRITY_MISMATCH" in str(ei.value)


def test_enforce_warn_mode_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_ATTESTATION_ENABLED", "1")
    monkeypatch.setenv("JARVIS_RUNTIME_ATTESTATION_STRICT", "0")
    monkeypatch.setenv(
        "JARVIS_BUILD_ATTESTATION_PATH",
        str(_stamp(tmp_path, commit="bb0aab05ef" + "0" * 30)),
    )
    monkeypatch.setenv("JARVIS_ATTESTATION_EXPECTED_COMMIT", "a6e72d4aad")
    enforce()  # must not raise


def test_enforce_match_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_RUNTIME_ATTESTATION_ENABLED", "1")
    monkeypatch.setenv("JARVIS_BUILD_ATTESTATION_PATH", str(_stamp(tmp_path)))
    monkeypatch.setenv("JARVIS_ATTESTATION_EXPECTED_COMMIT", "a6e72d4aad")
    enforce()  # no raise


def test_enforce_disabled_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "JARVIS_BUILD_ATTESTATION_PATH",
        str(_stamp(tmp_path, commit="deadbeef" + "0" * 32)),
    )
    monkeypatch.setenv("JARVIS_ATTESTATION_EXPECTED_COMMIT", "a6e72d4aad")
    enforce()  # disabled -> no raise


# ===========================================================================
# D — wiring pins
# ===========================================================================

def test_gls_enforces_before_failsoft_boot():
    """The gate must sit OUTSIDE the swallow-all boot block — a strict
    mismatch must set FAILED and halt, not be logged-and-continued."""
    src = (_GOV / "governed_loop_service.py").read_text(encoding="utf-8")
    assert "runtime_attestation" in src
    assert "DeploymentIntegrityMismatch" in src


def test_dockerfile_stamps_build():
    root = _GOV.parents[3]
    df = (root / "docker" / "Dockerfile.soak").read_text(encoding="utf-8")
    assert "GIT_COMMIT" in df and ".build_attestation.json" in df

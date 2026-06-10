"""Slice 202 — Autonomous Strategic Bootstrapper (HONEST variant).

The authorized plan asked the organism to mint its own HMAC secret and sign
its own strategic roadmap headless. That is REFUSED by design: a signature
the organism generates over its own goals is a FALSE authenticity claim — it
defeats the only purpose of the signature (attesting OPERATOR authorship) and
is the self-authorization anti-pattern the cage forbids (operator = zero-order
doll, §41.2).

What this slice delivers honestly instead:
  * ``strategy_bootstrapper`` — seeds ``.jarvis/roadmap.yaml`` from the PRD
    §41.6 north-star objectives, transparently ``signed: false`` /
    ``authority: advisory``. Because the RoadmapReader is intake-only (goals
    are DIRECTION; every emitted op still passes Iron Gate / SemanticGuardian
    / risk-tier / human approval), advisory direction grants NO authority.
    NEVER overwrites an operator-authored file.
  * ``strategy_signer`` — an OPERATOR utility (CLI) to ELEVATE the advisory
    roadmap to signed authenticity, reusing the reader's own
    ``compute_signature``. There is NO boot wiring that auto-invokes it; the
    operator runs it deliberately.
  * ``progress_ledger`` — a git-tracked, human-readable progress.txt (the
    Ralph legibility pattern).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from backend.core.ouroboros.governance.strategy_bootstrapper import (
    bootstrap_enabled,
    compile_roadmap,
    extract_northstar_goals,
    write_roadmap_if_absent,
)
from backend.core.ouroboros.governance.strategy_signer import (
    generate_secret,
    sign_roadmap_doc,
)
from backend.core.ouroboros.governance.progress_ledger import (
    render_progress,
    update_progress,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOV = _REPO_ROOT / "backend" / "core" / "ouroboros" / "governance"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    for v in (
        "JARVIS_STRATEGY_BOOTSTRAP_ENABLED", "JARVIS_PROGRESS_LEDGER_ENABLED",
    ):
        monkeypatch.delenv(v, raising=False)
    yield


# ===========================================================================
# A — bootstrapper: gate + honest advisory roadmap
# ===========================================================================

def test_bootstrap_disabled_by_default():
    assert bootstrap_enabled() is False


def test_extract_northstar_goals_are_grounded():
    goals = extract_northstar_goals()
    assert len(goals) >= 3
    ids = {g["id"] for g in goals}
    # the PRD §41.6 Tier-A→B objectives
    assert any("m10" in i.lower() or "proposal" in i.lower() for i in ids)
    assert any("unsupervised" in i.lower() or "soak" in i.lower() for i in ids)
    for g in goals:
        assert g["id"] and g["title"] and g["priority"] in (
            "critical", "high", "medium", "low",
        )


def test_compiled_roadmap_is_transparently_unsigned():
    roadmap = compile_roadmap(extract_northstar_goals())
    assert roadmap["signed"] is False
    assert roadmap["authority"] == "advisory"
    assert "prd" in roadmap["source"].lower()
    assert "signature" not in roadmap or not roadmap.get("signature")
    assert isinstance(roadmap["goals"], list) and roadmap["goals"]


def test_write_roadmap_creates_file_when_absent(tmp_path):
    p = tmp_path / "roadmap.yaml"
    out = write_roadmap_if_absent(p)
    assert out == p and p.exists()
    assert "goals" in p.read_text()


def test_write_roadmap_NEVER_overwrites_operator_file(tmp_path):
    p = tmp_path / "roadmap.yaml"
    p.write_text("operator: hand-authored\ngoals: []\n")
    out = write_roadmap_if_absent(p)
    assert out is None  # refused — operator's file is sacrosanct
    assert "hand-authored" in p.read_text()


# ===========================================================================
# B — signer is an OPERATOR tool, reuses the reader's own primitive
# ===========================================================================

def test_generate_secret_is_strong_and_unique():
    s1, s2 = generate_secret(), generate_secret()
    assert len(s1) >= 32 and s1 != s2


def test_sign_roadmap_produces_reader_verifiable_signature():
    from backend.core.ouroboros.governance.roadmap_reader import (
        _build_signing_payload, verify_signature,
    )
    secret = generate_secret()
    doc = {
        "version": 1, "operator_id": "op@x", "signed_at": "2026-06-10",
        "goals": [{"id": "g1", "title": "T", "priority": "high"}],
    }
    signed = sign_roadmap_doc(doc, secret)
    assert signed["signature"]
    # the reader's own verifier must accept it
    assert verify_signature(
        _build_signing_payload(signed), signed["signature"], secret,
    ) is True


def test_sign_with_wrong_secret_fails_verification():
    from backend.core.ouroboros.governance.roadmap_reader import (
        _build_signing_payload, verify_signature,
    )
    doc = {"version": 1, "goals": [{"id": "g", "title": "t", "priority": "low"}]}
    signed = sign_roadmap_doc(doc, generate_secret())
    assert verify_signature(
        _build_signing_payload(signed), signed["signature"], "WRONG",
    ) is False


def test_signer_has_no_boot_autoinvocation():
    """The signer must never be IMPORTED or CALLED from a boot path — only
    operator-run. A prose mention in a comment is fine; an import/call is not."""
    for fname in ("governed_loop_service.py", "harness.py"):
        for d in (_GOV, _GOV.parent / "battle_test"):
            f = d / fname
            if f.exists():
                src = f.read_text(encoding="utf-8")
                assert "import strategy_signer" not in src
                assert "from backend.core.ouroboros.governance.strategy_signer" \
                    not in src
                assert "sign_roadmap_doc(" not in src
                assert "strategy_signer._main" not in src


# ===========================================================================
# C — progress ledger (Ralph legibility, git-tracked)
# ===========================================================================

def test_progress_render_is_human_readable():
    text = render_progress(
        completed=[("g1", "shipped registry")],
        next_targets=[("g2", "activate roadmap")],
    )
    assert "g1" in text and "shipped registry" in text
    assert "g2" in text and "activate roadmap" in text
    assert "COMPLETED" in text.upper() and "NEXT" in text.upper()


def test_update_progress_writes_file(tmp_path):
    p = tmp_path / "progress.txt"
    update_progress(
        path=p, completed=[("g1", "done")], next_targets=[("g2", "todo")],
    )
    assert p.exists() and "g1" in p.read_text()


def test_update_progress_never_raises_on_bad_path():
    update_progress(
        path=Path("/nonexistent-x9/progress.txt"),
        completed=[], next_targets=[],
    )


# ===========================================================================
# D — doctrine pins
# ===========================================================================

def test_bootstrapper_never_self_signs():
    """The whole point: the bootstrapper produces an UNSIGNED roadmap. It must
    not IMPORT or CALL the signer / signature primitive (no self-signature) —
    a prose mention in the rationale is fine; an import is not."""
    src = (_GOV / "strategy_bootstrapper.py").read_text(encoding="utf-8")
    assert "import strategy_signer" not in src
    assert "import compute_signature" not in src
    assert "compute_signature(" not in src
    assert "sign_roadmap_doc(" not in src


def test_boundary_gate_not_weakened():
    src = (_GOV / "governance_boundary_gate.py").read_text(encoding="utf-8")
    assert "APPROVAL_REQUIRED" in src

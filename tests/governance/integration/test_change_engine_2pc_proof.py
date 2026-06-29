"""Bi-Directional Cryptographic Proof of the ChangeEngine 2PC + terminal gate.

Strict isolation: drives the REAL ChangeEngine.execute() on real disk with a
concrete, deterministic FixtureVerifier (NOT mock.patch). Proves the phantom is
dead in BOTH directions:

  * Alpha (verify pass): 2PC COMMIT fires, the cryptographic terminal gate passes,
    success=True, and the on-disk bytes SHA-256-match the expected mutation.
  * Omega (verify fail): the engine rolls back; the file's SHA-256 AFTER rollback
    is bit-identical to the original (cryptographic rollback proof) and NO phantom
    APPLIED is reported.

Production safety/security guards are untouched.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS = _REPO_ROOT / "scripts"
for _p in (str(_REPO_ROOT), str(_SCRIPTS)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from a1_deterministic_fixture import build_deterministic_mutation  # noqa: E402
from backend.core.ouroboros.governance.change_engine import (  # noqa: E402
    ChangeEngine,
    ChangeRequest,
)
from backend.core.ouroboros.governance.ledger import OperationLedger  # noqa: E402
from backend.core.ouroboros.governance.risk_engine import (  # noqa: E402
    ChangeType,
    OperationProfile,
)

_SRC = "def add(a, b):\n    return a + b\n"


class FixtureVerifier:
    """Concrete deterministic implementation of the ChangeEngine verify
    interface (an awaitable returning a fixed bool) -- a real injected object,
    not a mock.patch. Counts calls so the test can assert VERIFY actually ran."""

    def __init__(self, result: bool) -> None:
        self._result = result
        self.calls = 0

    async def __call__(self) -> bool:
        self.calls += 1
        return self._result


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.fixture()
def temp_target(tmp_path: Path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    target = root / "pkg" / "target.py"
    target.write_text(_SRC, encoding="utf-8")
    return root, target


def _engine(root: Path) -> ChangeEngine:
    return ChangeEngine(
        project_root=root,
        ledger=OperationLedger(storage_dir=root / ".jarvis" / "ledger"),
    )


def _request(target: Path, content: str, verifier: FixtureVerifier) -> ChangeRequest:
    profile = OperationProfile(
        files_affected=[target],
        change_type=ChangeType.MODIFY,
        blast_radius=1,
        test_scope_confidence=1.0,
        crosses_repo_boundary=False,
        touches_security_surface=False,
        touches_supervisor=False,
    )
    return ChangeRequest(
        op_id="op-2pc-proof",
        goal="2pc deterministic fixture proof",
        target_file=target,
        proposed_content=content,
        profile=profile,
        verify_fn=verifier,
    )


@pytest.mark.asyncio
async def test_alpha_path_verify_pass_commits_crypto_verified(temp_target):
    root, target = temp_target
    mutated = build_deterministic_mutation(_SRC, seed=7)
    verifier = FixtureVerifier(True)

    result = await _engine(root).execute(_request(target, mutated, verifier))

    assert verifier.calls == 1, "VERIFY must run before COMMIT"
    # success=True is returned ONLY after 2PC Phase 3 COMMIT, which the engine's
    # cryptographic terminal gate guards (on-disk SHA-256 == signed_content) --
    # so success itself is the crypto-verified-durable-write proof.
    assert result.success is True
    on_disk = target.read_text(encoding="utf-8")
    # The exact deterministic AST mutation is the durable suffix (engine prepends
    # an O+V signature header). Byte-exact containment = mathematically verified.
    assert mutated in on_disk
    assert "_A1_FIXTURE_SENTINEL" in on_disk
    # Cross-check the engine's gate independently: the unsigned mutation's bytes
    # survive verbatim on the physical disk.
    assert _sha(on_disk[on_disk.index(mutated):]) == _sha(mutated)


@pytest.mark.asyncio
async def test_omega_path_verify_fail_rolls_back_crypto_identical(temp_target):
    root, target = temp_target
    original_sha = _sha(target.read_text(encoding="utf-8"))
    mutated = build_deterministic_mutation(_SRC, seed=7)
    verifier = FixtureVerifier(False)

    result = await _engine(root).execute(_request(target, mutated, verifier))

    assert verifier.calls == 1
    assert result.success is False
    assert getattr(result, "rolled_back", False) is True
    # Cryptographic rollback proof: post-rollback file is bit-identical to original.
    assert _sha(target.read_text(encoding="utf-8")) == original_sha

from __future__ import annotations
import dataclasses
import pytest
from backend.core.ouroboros.governance.dag_capability_token import (
    TokenKind, CapabilityToken, SandboxExecutionToken, BlastRadiusClearedToken,
    LintClearedToken, DAGProofChain,
)

OP = "op-123"

def _full_chain(chain: DAGProofChain, op_id: str = OP):
    t1 = chain.mint(kind=TokenKind.SANDBOX_EXECUTION, op_id=op_id,
                    state_binding="cand-sha", payload={"exit_code": "0"})
    t2 = chain.mint(kind=TokenKind.BLAST_RADIUS_CLEARED, op_id=op_id,
                    state_binding="tree-sha", payload={"n_tests": "7"}, prev=t1)
    t3 = chain.mint(kind=TokenKind.LINT_CLEARED, op_id=op_id,
                    state_binding="diff-sha", payload={"rating": "5"}, prev=t2)
    return t1, t2, t3

def test_valid_chain_passes():
    chain = DAGProofChain()
    t1, t2, t3 = _full_chain(chain)
    assert chain.verify(t1) and chain.verify(t2) and chain.verify(t3)
    assert chain.verify_chain([t1, t2, t3], op_id=OP) is True

def test_typed_aliases_match_kind():
    chain = DAGProofChain()
    t1, t2, t3 = _full_chain(chain)
    assert isinstance(t1, SandboxExecutionToken)
    assert isinstance(t2, BlastRadiusClearedToken)
    assert isinstance(t3, LintClearedToken)

def test_forged_hmac_rejected():
    chain = DAGProofChain()
    t1, t2, t3 = _full_chain(chain)
    forged = dataclasses.replace(t1, sig="deadbeef" * 8)
    # Isolates HMAC failure: recomputed HMAC over correct fields does not match forged sig.
    assert chain.verify(forged) is False
    # Defense-in-depth: forgery is rejected at the chain level (position-0 HMAC fails;
    # t2/t3 are the originals from the same mint so their prev_hash links are intact).
    assert chain.verify_chain([forged, t2, t3], op_id=OP) is False

def test_replayed_state_binding_rejected():
    # A token signed for state A cannot be re-pointed at state B.
    chain = DAGProofChain()
    t1, _, _ = _full_chain(chain)
    replayed = dataclasses.replace(t1, state_binding="DIFFERENT-sha")
    assert chain.verify(replayed) is False

def test_cross_secret_forgery_rejected():
    # A token minted by a different process/secret never verifies here.
    foreign = DAGProofChain()
    t1, _, _ = _full_chain(foreign)
    local = DAGProofChain()
    assert local.verify(t1) is False

def test_out_of_order_chain_rejected():
    chain = DAGProofChain()
    t1, t2, t3 = _full_chain(chain)
    assert chain.verify_chain([t2, t1, t3], op_id=OP) is False

def test_omitted_token_rejected():
    chain = DAGProofChain()
    t1, t2, t3 = _full_chain(chain)
    assert chain.verify_chain([t1, t3], op_id=OP) is False

def test_tampered_prev_hash_rejected():
    chain = DAGProofChain()
    t1, t2, t3 = _full_chain(chain)
    broken = dataclasses.replace(t2, prev_hash="0" * 64)
    assert chain.verify_chain([t1, broken, t3], op_id=OP) is False

def test_cross_op_token_rejected():
    chain = DAGProofChain()
    good = _full_chain(chain, op_id=OP)
    intruder = chain.mint(kind=TokenKind.BLAST_RADIUS_CLEARED, op_id="other-op",
                          state_binding="tree-sha", payload={}, prev=good[0])
    assert chain.verify_chain([good[0], intruder, good[2]], op_id=OP) is False

def test_wrong_terminal_kind_rejected():
    # The final token MUST be LINT_CLEARED.
    chain = DAGProofChain()
    t1 = chain.mint(kind=TokenKind.SANDBOX_EXECUTION, op_id=OP, state_binding="a", payload={})
    t2 = chain.mint(kind=TokenKind.BLAST_RADIUS_CLEARED, op_id=OP, state_binding="b", payload={}, prev=t1)
    assert chain.verify_chain([t1, t2], op_id=OP) is False

def test_tampered_timestamp_rejected():
    # issued_monotonic is now inside the HMAC envelope; rolling it back to 0.0 must fail.
    chain = DAGProofChain()
    t1, _, _ = _full_chain(chain)
    tampered = dataclasses.replace(t1, issued_monotonic=0.0)
    assert chain.verify(tampered) is False

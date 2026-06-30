from __future__ import annotations
import json
from backend.core.ouroboros.governance import token_audit
from backend.core.ouroboros.governance.dag_capability_token import DAGProofChain, TokenKind

def test_append_mint_writes_durable_record(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TOKEN_AUDIT_ENABLED", "true")
    p = tmp_path / "token_audit.jsonl"
    chain = DAGProofChain()
    tok = chain.mint(kind=TokenKind.SANDBOX_EXECUTION, op_id="op-9",
                     state_binding="cand-sha", payload={"exit_code": "0"})
    token_audit.append_mint(tok, path=str(p))
    rows = [json.loads(line) for line in p.read_text().splitlines()]
    assert rows[-1]["op_id"] == "op-9"
    assert rows[-1]["kind"] == "sandbox_execution"
    assert rows[-1]["state_binding"] == "cand-sha"
    assert "sig" in rows[-1]
    assert "secret" not in json.dumps(rows[-1])  # secret never persisted

def test_append_mint_fail_soft_on_bad_path(tmp_path):
    chain = DAGProofChain()
    tok = chain.mint(kind=TokenKind.LINT_CLEARED, op_id="op-9",
                     state_binding="x", payload={})
    # Unwritable path must NOT raise.
    token_audit.append_mint(tok, path="/nonexistent-dir/deep/none.jsonl")

def test_disabled_is_noop(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_TOKEN_AUDIT_ENABLED", "false")
    p = tmp_path / "audit.jsonl"
    chain = DAGProofChain()
    tok = chain.mint(kind=TokenKind.SANDBOX_EXECUTION, op_id="op-1",
                     state_binding="a", payload={})
    token_audit.append_mint(tok, path=str(p))
    assert not p.exists()

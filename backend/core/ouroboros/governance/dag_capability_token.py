from __future__ import annotations
import dataclasses
import enum
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence


class TokenKind(str, enum.Enum):
    SANDBOX_EXECUTION = "sandbox_execution"
    BLAST_RADIUS_CLEARED = "blast_radius_cleared"
    LINT_CLEARED = "lint_cleared"
    # STANDALONE authority -- NOT part of the autonomous 3-token chain. A
    # non-autonomous (human-initiated/operator-tooling) caller mints one of
    # these to declare, with the same unforgeable HMAC + WAL audit, that it is
    # opening a PR outside the autonomous gate chain. There is no bypass flag:
    # the enforcer demands EITHER the full chain OR a signed HUMAN_OVERRIDE.
    HUMAN_OVERRIDE = "human_override"


# Canonical order of the gate chain. The terminal token MUST be LINT_CLEARED.
_CHAIN_ORDER = (
    TokenKind.SANDBOX_EXECUTION,
    TokenKind.BLAST_RADIUS_CLEARED,
    TokenKind.LINT_CLEARED,
)


def _canonical(kind: TokenKind, op_id: str, state_binding: str,
               prev_hash: str, payload: Mapping[str, str],
               issued_monotonic: float, branch_context: str) -> bytes:
    return json.dumps(
        {
            "issued_monotonic": format(issued_monotonic, ".9f"),
            "kind": kind.value,
            "op_id": op_id,
            "state_binding": state_binding,
            "branch_context": branch_context,
            "prev_hash": prev_hash,
            "payload": {str(k): str(v) for k, v in payload.items()},
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


@dataclass(frozen=True)
class CapabilityToken:
    kind: TokenKind
    op_id: str
    state_binding: str
    prev_hash: str
    payload: Mapping[str, str]
    issued_monotonic: float
    sig: str
    # Git branch/worktree context the token was minted in. Defaults to "" so
    # pre-existing (unbound) callers and tokens stay valid; when set, it is
    # folded into the signed envelope and enforced uniform across a chain.
    branch_context: str = ""

    def digest(self) -> str:
        """Identity hash used as the next token's ``prev_hash`` (chain link)."""
        body = _canonical(self.kind, self.op_id, self.state_binding,
                          self.prev_hash, self.payload, self.issued_monotonic,
                          self.branch_context)
        return hashlib.sha256(body + self.sig.encode("utf-8")).hexdigest()


# Typed aliases -- frozen subclasses add no fields, so the parent __init__ is
# inherited. A function can demand the SPECIFIC type as a mandatory argument.
class SandboxExecutionToken(CapabilityToken):
    pass


class BlastRadiusClearedToken(CapabilityToken):
    pass


class LintClearedToken(CapabilityToken):
    pass


# Standalone (NOT chain-linked). Minted with prev=None by a non-autonomous
# caller as an auditable declaration of intent. Verified by chain.verify (HMAC)
# -- it is deliberately absent from _CHAIN_ORDER so verify_chain never accepts
# it as a substitute for an autonomous gate token.
class HumanOverrideToken(CapabilityToken):
    pass


_KIND_CLS = {
    TokenKind.SANDBOX_EXECUTION: SandboxExecutionToken,
    TokenKind.BLAST_RADIUS_CLEARED: BlastRadiusClearedToken,
    TokenKind.LINT_CLEARED: LintClearedToken,
    TokenKind.HUMAN_OVERRIDE: HumanOverrideToken,
}


class DAGProofChain:
    """Per-op accumulator that mints/verifies unforgeable capability tokens."""

    def __init__(self, *, secret: Optional[bytes] = None) -> None:
        # Each instance gets its own secret by default so tokens from one
        # DAGProofChain cannot be verified by a different one (cross-secret
        # forgery guard). The secret is in-memory only -- never logged,
        # persisted, or returned.
        self._secret = secret if secret is not None else secrets.token_bytes(32)

    def _sign(self, kind: TokenKind, op_id: str, state_binding: str,
              prev_hash: str, payload: Mapping[str, str],
              issued_monotonic: float, branch_context: str) -> str:
        return hmac.new(
            self._secret,
            _canonical(kind, op_id, state_binding, prev_hash, payload,
                       issued_monotonic, branch_context),
            hashlib.sha256,
        ).hexdigest()

    def mint(self, *, kind: TokenKind, op_id: str, state_binding: str,
             payload: Mapping[str, str],
             prev: Optional[CapabilityToken] = None,
             branch_context: str = "") -> CapabilityToken:
        prev_hash = prev.digest() if prev is not None else ""
        norm = {str(k): str(v) for k, v in payload.items()}
        ts = time.monotonic()
        sig = self._sign(kind, op_id, state_binding, prev_hash, norm, ts,
                         branch_context)
        cls = _KIND_CLS[kind]
        token = cls(kind, op_id, state_binding, prev_hash, norm, ts, sig,
                    branch_context)
        from . import token_audit  # local import avoids a module cycle
        token_audit.append_mint(token)
        return token

    def verify(self, token: CapabilityToken) -> bool:
        expected = self._sign(token.kind, token.op_id, token.state_binding,
                              token.prev_hash, token.payload,
                              token.issued_monotonic, token.branch_context)
        return hmac.compare_digest(expected, token.sig)

    def verify_chain(self, tokens: Sequence[CapabilityToken], *, op_id: str) -> bool:
        if len(tokens) != len(_CHAIN_ORDER):
            return False
        # Anti-injection invariant: every token in the chain MUST have been
        # minted in the SAME branch/worktree context. A token from worktree A
        # cannot be spliced into a chain rooted in worktree B.
        expected_ctx = tokens[0].branch_context
        prev_hash = ""
        for token, expected_kind in zip(tokens, _CHAIN_ORDER):
            if token.kind != expected_kind:
                return False
            if not isinstance(token, _KIND_CLS[expected_kind]):
                return False
            if token.op_id != op_id:
                return False
            if token.branch_context != expected_ctx:
                return False
            if token.prev_hash != prev_hash:
                return False
            if not self.verify(token):
                return False
            prev_hash = token.digest()
        return True

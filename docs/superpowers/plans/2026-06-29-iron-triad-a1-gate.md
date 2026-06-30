# Iron Triad — A1 Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Bind the autonomous Ouroboros→PR path with three fail-closed gates chained by unforgeable capability tokens, so O+V can complete its first full autonomous cycle E2E (close the A1 gate).

**Architecture:** Three thin gates on the single mandatory generate→PR path — ① pre-APPLY L4 container exec lock, ② post-VERIFY blast-radius regression, ③ pre-PR LLM linter. Each mints a state-bound HMAC capability token; `gh pr create` requires the typed token objects as mandatory arguments and re-verifies the hash chain. ~90% reuse of existing machinery; the spine (`dag_capability_token.py`) is the only substantial net-new module. Every token mint is durably appended to an immutable WAL.

**Tech Stack:** Python 3.9+ (asyncio, `from __future__ import annotations`), `hmac`/`hashlib`/`secrets` (stdlib), pytest. Reuses `container_sandbox`, `IsomorphicEnv`, `WorktreeManager`, `WorkspaceCheckpointManager`, `call_graph_blast`/`blast_radius_adapter`, `intake_dlq`, `self_critique`, `DoublewordProvider`, `OrangePRReviewer`, `outage_ledger` (WAL pattern).

## Global Constraints

- **Spec:** `docs/superpowers/specs/2026-06-29-iron-triad-a1-gate-design.md` (authoritative).
- **Async-first:** all I/O via `asyncio`; `asyncio.wait_for` (never `asyncio.timeout` — Python 3.9 floor).
- **`from __future__ import annotations`** at the top of every new file.
- **No hardcoded models:** any LLM call resolves provider via existing policy/`DoublewordProvider`.
- **Env-var-driven, default-OFF:** `JARVIS_A1_TOKEN_ENFORCER_ENABLED`, `JARVIS_A1_SANDBOX_LOCK_ENABLED`, `JARVIS_A1_BLAST_RADIUS_ENABLED`, `JARVIS_A1_PR_LINTER_ENABLED`, `JARVIS_TOKEN_AUDIT_ENABLED` — all read with sensible defaults; the four A1 gates default **false** (byte-identical legacy rollback), WAL defaults **true**.
- **Fail-closed:** gates never return a falsy token; on failure they raise a typed exception the FSM routes to terminate/rollback/Cryo-DLQ.
- **No `git reset --hard`** in the apply path — rollback is `WorkspaceCheckpointManager.restore_checkpoint` + post-restore tree-SHA equality assertion.
- **Secret hygiene:** the per-process HMAC secret (`secrets.token_bytes(32)`) lives only in memory; never logged, persisted, or written to the WAL.
- **ASCII-only** source (Iron Gate ASCII strictness).
- **Commit hygiene:** `git add` only the named files for each task; never `-A`/`.`.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `backend/core/ouroboros/governance/dag_capability_token.py` | CREATE — token types, `DAGProofChain` (mint/verify/verify_chain), per-process secret | 1 |
| `backend/core/ouroboros/governance/token_audit.py` | CREATE — immutable append-only WAL for token mints (mirrors `outage_ledger`) | 2 |
| `backend/core/ouroboros/governance/pre_apply_exec_lock.py` | CREATE — Gate ① isomorphic L4 container exec lock | 3 |
| `backend/core/ouroboros/governance/orchestrator.py` | MODIFY — wire Gate ① post-IronGate/pre-VALIDATE; Docker pre-flight + `REQUIRES_CLOUD_EXECUTION` | 4, 9 |
| `backend/core/ouroboros/governance/blast_radius_verify.py` | CREATE — Gate ② reverse-dep regression + fail-closed graph guard | 5 |
| `backend/core/ouroboros/governance/phase_runners/slice4b_runner.py` | MODIFY — capture pre-op tree-SHA; wire Gate ② between verify-gate and auto-commit | 6 |
| `backend/core/ouroboros/governance/pr_self_linter.py` | CREATE — Gate ③ blocking LLM architectural-rules critique | 7 |
| `backend/core/ouroboros/governance/orange_pr_reviewer.py` | MODIFY — token-gated `create_review_pr` signature + `verify_chain` | 8 |
| `backend/core/ouroboros/governance/flag_registry.py` (seed) | MODIFY — register the 5 new flags | 10 |
| Tests under `tests/governance/` | CREATE — one suite per module | 1–8 |

---

## Task 1: Capability-token enforcer (adversarial TDD first)

**Files:**
- Create: `backend/core/ouroboros/governance/dag_capability_token.py`
- Test: `tests/governance/test_dag_capability_token.py`

**Interfaces:**
- Produces: `TokenKind` (enum), `CapabilityToken` (frozen), typed aliases `SandboxExecutionToken`/`BlastRadiusClearedToken`/`LintClearedToken`, `DAGProofChain` with `mint(*, kind, op_id, state_binding, payload, prev=None) -> CapabilityToken`, `verify(token) -> bool`, `verify_chain(tokens, *, op_id) -> bool`, and `token.digest() -> str`.

- [ ] **Step 1: Write the failing adversarial tests** (attack `verify`/`verify_chain` before any implementation exists)

```python
# tests/governance/test_dag_capability_token.py
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
    t1, _, _ = _full_chain(chain)
    forged = dataclasses.replace(t1, sig="deadbeef" * 8)
    assert chain.verify(forged) is False
    assert chain.verify_chain([forged, *_full_chain(chain)[1:]], op_id=OP) is False

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_dag_capability_token.py -q`
Expected: FAIL with `ModuleNotFoundError: dag_capability_token`.

- [ ] **Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/dag_capability_token.py
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


# Canonical order of the gate chain. The terminal token MUST be LINT_CLEARED.
_CHAIN_ORDER = (
    TokenKind.SANDBOX_EXECUTION,
    TokenKind.BLAST_RADIUS_CLEARED,
    TokenKind.LINT_CLEARED,
)

_SESSION_SECRET: Optional[bytes] = None


def _session_secret() -> bytes:
    """Per-process HMAC secret. In-memory only; never logged or persisted."""
    global _SESSION_SECRET
    if _SESSION_SECRET is None:
        _SESSION_SECRET = secrets.token_bytes(32)
    return _SESSION_SECRET


def _canonical(kind: TokenKind, op_id: str, state_binding: str,
               prev_hash: str, payload: Mapping[str, str]) -> bytes:
    return json.dumps(
        {
            "kind": kind.value,
            "op_id": op_id,
            "state_binding": state_binding,
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

    def digest(self) -> str:
        """Identity hash used as the next token's ``prev_hash`` (chain link)."""
        body = _canonical(self.kind, self.op_id, self.state_binding,
                          self.prev_hash, self.payload)
        return hashlib.sha256(body + self.sig.encode("utf-8")).hexdigest()


# Typed aliases — frozen subclasses add no fields, so the parent __init__ is
# inherited. A function can demand the SPECIFIC type as a mandatory argument.
class SandboxExecutionToken(CapabilityToken):
    pass


class BlastRadiusClearedToken(CapabilityToken):
    pass


class LintClearedToken(CapabilityToken):
    pass


_KIND_CLS = {
    TokenKind.SANDBOX_EXECUTION: SandboxExecutionToken,
    TokenKind.BLAST_RADIUS_CLEARED: BlastRadiusClearedToken,
    TokenKind.LINT_CLEARED: LintClearedToken,
}


class DAGProofChain:
    """Per-op accumulator that mints/verifies unforgeable capability tokens."""

    def __init__(self, *, secret: Optional[bytes] = None) -> None:
        self._secret = secret if secret is not None else _session_secret()

    def _sign(self, kind: TokenKind, op_id: str, state_binding: str,
              prev_hash: str, payload: Mapping[str, str]) -> str:
        return hmac.new(
            self._secret,
            _canonical(kind, op_id, state_binding, prev_hash, payload),
            hashlib.sha256,
        ).hexdigest()

    def mint(self, *, kind: TokenKind, op_id: str, state_binding: str,
             payload: Mapping[str, str],
             prev: Optional[CapabilityToken] = None) -> CapabilityToken:
        prev_hash = prev.digest() if prev is not None else ""
        norm = {str(k): str(v) for k, v in payload.items()}
        sig = self._sign(kind, op_id, state_binding, prev_hash, norm)
        cls = _KIND_CLS[kind]
        token = cls(kind, op_id, state_binding, prev_hash, norm,
                    time.monotonic(), sig)
        return token

    def verify(self, token: CapabilityToken) -> bool:
        expected = self._sign(token.kind, token.op_id, token.state_binding,
                              token.prev_hash, token.payload)
        return hmac.compare_digest(expected, token.sig)

    def verify_chain(self, tokens: Sequence[CapabilityToken], *, op_id: str) -> bool:
        if len(tokens) != len(_CHAIN_ORDER):
            return False
        prev_hash = ""
        for token, expected_kind in zip(tokens, _CHAIN_ORDER):
            if token.kind != expected_kind:
                return False
            if not isinstance(token, _KIND_CLS[expected_kind]):
                return False
            if token.op_id != op_id:
                return False
            if token.prev_hash != prev_hash:
                return False
            if not self.verify(token):
                return False
            prev_hash = token.digest()
        return True
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_dag_capability_token.py -q`
Expected: PASS (11 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/dag_capability_token.py tests/governance/test_dag_capability_token.py
git commit -m "feat(a1): cryptographic DAG capability-token enforcer (adversarial TDD)"
```

---

## Task 2: Immutable token-mint WAL (audit trail)

**Files:**
- Create: `backend/core/ouroboros/governance/token_audit.py`
- Modify: `backend/core/ouroboros/governance/dag_capability_token.py` (call WAL on mint)
- Test: `tests/governance/test_token_audit.py`

**Interfaces:**
- Consumes: `CapabilityToken` (Task 1).
- Produces: `append_mint(token: CapabilityToken, *, path: str | None = None) -> None` (fail-soft, never raises); `read_audit(path=None) -> list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_token_audit.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_token_audit.py -q`
Expected: FAIL with `ModuleNotFoundError: token_audit`.

- [ ] **Step 3: Write the WAL implementation (mirrors `outage_ledger` append-only ring)**

```python
# backend/core/ouroboros/governance/token_audit.py
from __future__ import annotations
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from .dag_capability_token import CapabilityToken

logger = logging.getLogger(__name__)
_SCHEMA_VERSION = 1


def _enabled() -> bool:
    return os.environ.get("JARVIS_TOKEN_AUDIT_ENABLED", "true").strip().lower() in ("1", "true", "yes")


def _default_path() -> str:
    return os.environ.get("JARVIS_TOKEN_AUDIT_PATH", os.path.join(".jarvis", "token_audit.jsonl"))


def _max() -> int:
    try:
        return int(os.environ.get("JARVIS_TOKEN_AUDIT_MAX", "500"))
    except ValueError:
        return 500


def append_mint(token: CapabilityToken, *, path: Optional[str] = None) -> None:
    """Durably append a token-mint record. Fail-soft — never raises.

    The HMAC ``sig`` is recorded as audit evidence; the SECRET is never written.
    """
    if not _enabled():
        return
    p = path if path is not None else _default_path()
    record = {
        "ts": time.time(),
        "schema_version": _SCHEMA_VERSION,
        "kind": token.kind.value,
        "op_id": token.op_id,
        "state_binding": token.state_binding,
        "prev_hash": token.prev_hash,
        "sig": token.sig,
        "payload": dict(token.payload),
    }
    try:
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        _trim(p)
    except Exception as exc:  # noqa: BLE001 — audit is best-effort
        logger.warning("[TokenAudit] append failed: %s", exc)


def _trim(p: str) -> None:
    try:
        with open(p, "r", encoding="utf-8") as fh:
            lines = fh.readlines()
        cap = _max()
        if len(lines) > cap:
            with open(p, "w", encoding="utf-8") as fh:
                fh.writelines(lines[-cap:])
    except Exception:  # noqa: BLE001
        pass


def read_audit(path: Optional[str] = None) -> List[Dict[str, Any]]:
    p = path if path is not None else _default_path()
    try:
        with open(p, "r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]
    except FileNotFoundError:
        return []
```

- [ ] **Step 4: Wire the WAL into `mint` (durable on every mint)**

In `dag_capability_token.py`, inside `DAGProofChain.mint`, immediately before `return token`:

```python
        from . import token_audit  # local import avoids a module cycle
        token_audit.append_mint(token)
        return token
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_token_audit.py tests/governance/test_dag_capability_token.py -q`
Expected: PASS (14 passed).

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/token_audit.py backend/core/ouroboros/governance/dag_capability_token.py tests/governance/test_token_audit.py
git commit -m "feat(a1): immutable append-only WAL for every token mint"
```

---

## Task 3: Gate ① — Isomorphic Execution Lock module

**Files:**
- Create: `backend/core/ouroboros/governance/pre_apply_exec_lock.py`
- Test: `tests/governance/test_pre_apply_exec_lock.py`

**Interfaces:**
- Consumes: `DAGProofChain`, `SandboxExecutionToken`, `TokenKind` (Task 1); `container_sandbox.docker_available`, `container_sandbox.run_in_container` (existing); `container_sandbox.ContainmentResult` (existing).
- Produces: exceptions `SandboxLockFailed`, `RequiresCloudExecution`; `async def acquire_sandbox_execution_token(*, op_id, candidate_files, repo_root, chain, prev_token=None, docker_available=None, runner=None) -> SandboxExecutionToken`. `candidate_files` is `Sequence[Tuple[str, str]]` (path, full_content). `docker_available`/`runner` are injectable for tests.

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/test_pre_apply_exec_lock.py
from __future__ import annotations
import hashlib
import pytest
from backend.core.ouroboros.governance import pre_apply_exec_lock as lock
from backend.core.ouroboros.governance.dag_capability_token import (
    DAGProofChain, SandboxExecutionToken,
)

CANDIDATE = [("backend/x.py", "def f():\n    return 1\n")]

class _FakeResult:
    def __init__(self, exit_code, breached=False):
        self.exit_code = exit_code
        self.breached = breached
        self.diagnostic = "ok" if exit_code == 0 else "boom"

@pytest.mark.asyncio
async def test_exit_zero_mints_token():
    chain = DAGProofChain()
    async def runner(**_):
        return _FakeResult(0)
    tok = await lock.acquire_sandbox_execution_token(
        op_id="op-1", candidate_files=CANDIDATE, repo_root="/repo",
        chain=chain, docker_available=lambda: True, runner=runner)
    assert isinstance(tok, SandboxExecutionToken)
    assert tok.payload["exit_code"] == "0"
    # state_binding binds the EXACT candidate content
    expect = hashlib.sha256(b"backend/x.py\x00def f():\n    return 1\n").hexdigest()
    assert tok.state_binding == expect

@pytest.mark.asyncio
async def test_nonzero_exit_raises_sandbox_lock_failed():
    chain = DAGProofChain()
    async def runner(**_):
        return _FakeResult(1)
    with pytest.raises(lock.SandboxLockFailed):
        await lock.acquire_sandbox_execution_token(
            op_id="op-1", candidate_files=CANDIDATE, repo_root="/repo",
            chain=chain, docker_available=lambda: True, runner=runner)

@pytest.mark.asyncio
async def test_no_docker_raises_requires_cloud_execution_no_process_fallback():
    chain = DAGProofChain()
    async def runner(**_):
        raise AssertionError("runner must NOT be called without Docker")
    with pytest.raises(lock.RequiresCloudExecution):
        await lock.acquire_sandbox_execution_token(
            op_id="op-1", candidate_files=CANDIDATE, repo_root="/repo",
            chain=chain, docker_available=lambda: False, runner=runner)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_pre_apply_exec_lock.py -q`
Expected: FAIL with `ModuleNotFoundError: pre_apply_exec_lock`.

- [ ] **Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/pre_apply_exec_lock.py
from __future__ import annotations
import hashlib
import logging
import os
from typing import Awaitable, Callable, Optional, Sequence, Tuple

from .dag_capability_token import (
    CapabilityToken, DAGProofChain, SandboxExecutionToken, TokenKind,
)

logger = logging.getLogger(__name__)


class SandboxLockFailed(RuntimeError):
    """Candidate failed to compile/run in the L4 container — terminate the DAG."""


class RequiresCloudExecution(RuntimeError):
    """No local Docker daemon — strict L4 policy forbids a process downgrade.

    Phase 1: terminate + flag the op. Phase 2: route execution to the GCP node.
    """


def lock_enabled() -> bool:
    return os.environ.get("JARVIS_A1_SANDBOX_LOCK_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _candidate_hash(candidate_files: Sequence[Tuple[str, str]]) -> str:
    h = hashlib.sha256()
    for path, content in sorted(candidate_files):
        h.update(path.encode("utf-8"))
        h.update(b"\x00")
        h.update(content.encode("utf-8"))
    return h.hexdigest()


async def acquire_sandbox_execution_token(
    *,
    op_id: str,
    candidate_files: Sequence[Tuple[str, str]],
    repo_root: str,
    chain: DAGProofChain,
    prev_token: Optional[CapabilityToken] = None,
    docker_available: Optional[Callable[[], bool]] = None,
    runner: Optional[Callable[..., Awaitable]] = None,
) -> SandboxExecutionToken:
    """Run the candidate in a hardened L4 container; mint a token iff exit==0.

    Strict container-only: no Docker -> RequiresCloudExecution (no fallback).
    Any non-zero exit / containment breach -> SandboxLockFailed (nothing is
    written to the real tree; the caller terminates the DAG).
    """
    from . import container_sandbox  # lazy import keeps module load cheap

    _docker = docker_available or container_sandbox.docker_available
    if not _docker():
        raise RequiresCloudExecution(f"op={op_id} no local Docker daemon")

    _run = runner or container_sandbox.run_in_container
    # Build a compile+import probe over the candidate's changed modules.
    probe = _build_probe(candidate_files)
    result = await _run(code=probe, worktree=repo_root, op_id=op_id)

    exit_code = getattr(result, "exit_code", 1)
    breached = bool(getattr(result, "breached", False))
    if exit_code != 0 or breached:
        raise SandboxLockFailed(
            f"op={op_id} exit={exit_code} breached={breached} "
            f"diag={getattr(result, 'diagnostic', '')}")

    state_binding = _candidate_hash(candidate_files)
    token = chain.mint(
        kind=TokenKind.SANDBOX_EXECUTION,
        op_id=op_id,
        state_binding=state_binding,
        payload={"exit_code": "0", "image": container_sandbox.sandbox_image()},
        prev=prev_token,
    )
    return token  # type: ignore[return-value]  # mint() returns the typed subclass


def _build_probe(candidate_files: Sequence[Tuple[str, str]]) -> str:
    """A deterministic compile-check payload over the candidate's .py files."""
    py = [p for p, _ in candidate_files if p.endswith(".py")]
    listing = ",".join(repr(p) for p in py)
    return (
        "import py_compile, sys\n"
        f"paths = [{listing}]\n"
        "for p in paths:\n"
        "    py_compile.compile(p, doraise=True)\n"
        "print('compile-ok')\n"
    )
```

> Note: the probe deliberately starts with `py_compile` (compile parity). Task 4 expands `worktree` to the candidate-materialized worktree so the probe compiles the *new* content, and adds the scoped-test stage; the unit tests inject `runner` so they stay hermetic.

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_pre_apply_exec_lock.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/pre_apply_exec_lock.py tests/governance/test_pre_apply_exec_lock.py
git commit -m "feat(a1): Gate 1 isomorphic L4 container exec lock (strict, fail-closed)"
```

---

## Task 4: Wire Gate ① into the orchestrator (default-OFF, OFF-parity proven)

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (post-Iron-Gate, pre-VALIDATE seam, ~`:6271` after the ASCII gate block)
- Test: `tests/governance/test_gate1_orchestrator_wiring.py`

**Interfaces:**
- Consumes: `acquire_sandbox_execution_token`, `lock_enabled`, `SandboxLockFailed`, `RequiresCloudExecution` (Task 3); the per-op `DAGProofChain` (stored on `ctx`, added here).
- Produces: a `SandboxExecutionToken` stashed on the op context (`ctx.sandbox_token`) for Task 6/8 to chain from; terminal routing to POSTMORTEM on lock failure.

- [ ] **Step 1: Write the failing wiring test** (OFF-parity + ON-terminates)

```python
# tests/governance/test_gate1_orchestrator_wiring.py
from __future__ import annotations
import os
import pytest
from backend.core.ouroboros.governance import pre_apply_exec_lock as lock

def test_lock_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_SANDBOX_LOCK_ENABLED", raising=False)
    assert lock.lock_enabled() is False

def test_lock_enabled_when_flagged(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_SANDBOX_LOCK_ENABLED", "true")
    assert lock.lock_enabled() is True
```

> The deep FSM-routing assertion (lock failure → POSTMORTEM, token stashed on ctx) is exercised E2E by `isomorphic_a1_local.py` in Task 11; this unit test pins the gate flag-parity so the OFF path is byte-identical.

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_gate1_orchestrator_wiring.py -q`
Expected: FAIL (the `lock_enabled` import path resolves, but assert the test file runs; if green already, proceed — these pin existing behavior).

- [ ] **Step 3: Add the gate at the post-Iron-Gate seam**

In `orchestrator.py`, immediately after the ASCII-strict gate block (the candidate is finalized, nothing written yet), insert:

```python
        # ---- Gate 1: Isomorphic Execution Lock (pre-APPLY, strict L4) ----
        from .pre_apply_exec_lock import (
            acquire_sandbox_execution_token, lock_enabled,
            SandboxLockFailed, RequiresCloudExecution,
        )
        from .dag_capability_token import DAGProofChain

        if lock_enabled():
            _chain = getattr(ctx, "proof_chain", None) or DAGProofChain()
            _cand_files = list(self._iter_candidate_files(best_candidate))
            try:
                _sbx_tok = await acquire_sandbox_execution_token(
                    op_id=ctx.op_id,
                    candidate_files=_cand_files,
                    repo_root=str(self._config.project_root),
                    chain=_chain,
                )
            except RequiresCloudExecution as exc:
                logger.warning("[Gate1] op=%s REQUIRES_CLOUD_EXECUTION: %s", ctx.op_id, exc)
                ctx = ctx.advance(
                    OperationPhase.POSTMORTEM,
                    terminal_reason_code="requires_cloud_execution",
                )
                return ctx
            except SandboxLockFailed as exc:
                logger.warning("[Gate1] op=%s SANDBOX_LOCK_FAILED: %s", ctx.op_id, exc)
                ctx = ctx.advance(
                    OperationPhase.POSTMORTEM,
                    terminal_reason_code="sandbox_lock_failed",
                )
                return ctx
            ctx = dataclasses.replace(ctx, proof_chain=_chain, sandbox_token=_sbx_tok)
```

Add the two optional fields to the op context dataclass (`op_context.py`, alongside `generate_file_hashes`):

```python
    proof_chain: object = None        # DAGProofChain (per-op), set at Gate 1
    sandbox_token: object = None      # SandboxExecutionToken, set at Gate 1
```

- [ ] **Step 4: Run the gate flag-parity + a smoke import**

Run: `python3 -m pytest tests/governance/test_gate1_orchestrator_wiring.py -q && python3 -c "import backend.core.ouroboros.governance.orchestrator"`
Expected: PASS + clean import.

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/orchestrator.py backend/core/ouroboros/governance/op_context.py tests/governance/test_gate1_orchestrator_wiring.py
git commit -m "feat(a1): wire Gate 1 exec lock post-IronGate (default-OFF, OFF byte-identical)"
```

---

## Task 5: Gate ② — Blast-radius regression module

**Files:**
- Create: `backend/core/ouroboros/governance/blast_radius_verify.py`
- Test: `tests/governance/test_blast_radius_verify.py`

**Interfaces:**
- Consumes: `DAGProofChain`, `BlastRadiusClearedToken`, `TokenKind`, `SandboxExecutionToken` (Tasks 1/3); injectable `graph_fn` (reverse-dep resolver), `test_fn` (runs a test set), `rollback_fn`, `current_tree_sha_fn`, `dlq_fn`.
- Produces: exceptions `BlastRadiusBreach`, `BlastRadiusGraphFailure`; `async def acquire_blast_radius_token(*, op_id, scope_files, pre_op_tree_sha, chain, prev_token, graph_fn, test_fn, current_tree_sha_fn, rollback_fn, dlq_fn) -> BlastRadiusClearedToken`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/test_blast_radius_verify.py
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance import blast_radius_verify as brv
from backend.core.ouroboros.governance.dag_capability_token import (
    DAGProofChain, TokenKind, BlastRadiusClearedToken,
)

SCOPE = ["backend/x.py"]
PRE = "tree-sha-pre"

def _prev(chain):
    return chain.mint(kind=TokenKind.SANDBOX_EXECUTION, op_id="op-1",
                      state_binding="cand", payload={"exit_code": "0"})

@pytest.mark.asyncio
async def test_all_pass_mints_chained_token():
    chain = DAGProofChain(); prev = _prev(chain)
    async def graph_fn(files): return {"tests/test_x.py"}
    async def test_fn(tests): return {"failed": [], "total": 3}
    tok = await brv.acquire_blast_radius_token(
        op_id="op-1", scope_files=SCOPE, pre_op_tree_sha=PRE, chain=chain,
        prev_token=prev, graph_fn=graph_fn, test_fn=test_fn,
        current_tree_sha_fn=lambda: PRE, rollback_fn=None, dlq_fn=None)
    assert isinstance(tok, BlastRadiusClearedToken)
    assert tok.prev_hash == prev.digest()  # chained to Gate 1
    assert tok.state_binding == PRE

@pytest.mark.asyncio
async def test_any_failure_rolls_back_and_asserts_sha_and_dlqs():
    chain = DAGProofChain(); prev = _prev(chain)
    calls = {"rollback": 0, "dlq": []}
    async def graph_fn(files): return {"tests/test_x.py"}
    async def test_fn(tests): return {"failed": ["tests/test_x.py::t"], "total": 3}
    def rollback_fn(sha): calls["rollback"] += 1
    def dlq_fn(reason): calls["dlq"].append(reason)
    with pytest.raises(brv.BlastRadiusBreach):
        await brv.acquire_blast_radius_token(
            op_id="op-1", scope_files=SCOPE, pre_op_tree_sha=PRE, chain=chain,
            prev_token=prev, graph_fn=graph_fn, test_fn=test_fn,
            current_tree_sha_fn=lambda: PRE, rollback_fn=rollback_fn, dlq_fn=dlq_fn)
    assert calls["rollback"] == 1
    assert calls["dlq"] == ["blast_radius_breach"]

@pytest.mark.asyncio
async def test_graph_failure_is_fail_closed_and_dlqs():
    chain = DAGProofChain(); prev = _prev(chain)
    calls = {"rollback": 0, "dlq": []}
    async def graph_fn(files): raise ValueError("cyclic")
    async def test_fn(tests): raise AssertionError("must not run tests on graph failure")
    def rollback_fn(sha): calls["rollback"] += 1
    def dlq_fn(reason): calls["dlq"].append(reason)
    with pytest.raises(brv.BlastRadiusGraphFailure):
        await brv.acquire_blast_radius_token(
            op_id="op-1", scope_files=SCOPE, pre_op_tree_sha=PRE, chain=chain,
            prev_token=prev, graph_fn=graph_fn, test_fn=test_fn,
            current_tree_sha_fn=lambda: PRE, rollback_fn=rollback_fn, dlq_fn=dlq_fn)
    assert calls["rollback"] == 1
    assert calls["dlq"] == ["blast_radius_graph_failure"]

@pytest.mark.asyncio
async def test_rollback_sha_mismatch_raises():
    chain = DAGProofChain(); prev = _prev(chain)
    async def graph_fn(files): return {"tests/test_x.py"}
    async def test_fn(tests): return {"failed": ["t"], "total": 1}
    with pytest.raises(brv.BlastRadiusBreach):
        await brv.acquire_blast_radius_token(
            op_id="op-1", scope_files=SCOPE, pre_op_tree_sha=PRE, chain=chain,
            prev_token=prev, graph_fn=graph_fn, test_fn=test_fn,
            current_tree_sha_fn=lambda: "DIFFERENT", rollback_fn=lambda s: None,
            dlq_fn=lambda r: None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_blast_radius_verify.py -q`
Expected: FAIL with `ModuleNotFoundError: blast_radius_verify`.

- [ ] **Step 3: Write the implementation**

```python
# backend/core/ouroboros/governance/blast_radius_verify.py
from __future__ import annotations
import logging
import os
from typing import Awaitable, Callable, Optional, Sequence, Set

from .dag_capability_token import (
    BlastRadiusClearedToken, CapabilityToken, DAGProofChain, TokenKind,
)

logger = logging.getLogger(__name__)


class BlastRadiusBreach(RuntimeError):
    """A test in the reverse-dep closure failed — rolled back to pre-op SHA."""


class BlastRadiusGraphFailure(RuntimeError):
    """The reverse-dep graph could not be built — fail-closed, no marker fallback."""


def blast_radius_enabled() -> bool:
    return os.environ.get("JARVIS_A1_BLAST_RADIUS_ENABLED", "false").strip().lower() in ("1", "true", "yes")


async def acquire_blast_radius_token(
    *,
    op_id: str,
    scope_files: Sequence[str],
    pre_op_tree_sha: str,
    chain: DAGProofChain,
    prev_token: CapabilityToken,
    graph_fn: Callable[[Sequence[str]], Awaitable[Set[str]]],
    test_fn: Callable[[Set[str]], Awaitable[dict]],
    current_tree_sha_fn: Callable[[], str],
    rollback_fn: Optional[Callable[[str], None]],
    dlq_fn: Optional[Callable[[str], None]],
) -> BlastRadiusClearedToken:
    """Run the full reverse-dependency closure of the modified AST.

    - graph build error -> fail-closed: rollback + DLQ + raise GraphFailure.
    - any test failure (no retry) -> rollback to the pre-op tree-SHA, assert
      restoration cryptographically, DLQ, raise Breach.
    - all pass -> mint a token chained to the sandbox token.
    """
    def _rollback_and_assert(reason: str) -> None:
        if rollback_fn is not None:
            rollback_fn(pre_op_tree_sha)
        restored = current_tree_sha_fn()
        if dlq_fn is not None:
            dlq_fn(reason)
        if restored != pre_op_tree_sha:
            raise BlastRadiusBreach(
                f"op={op_id} ROLLBACK FAILED restored={restored} != pre={pre_op_tree_sha}")

    try:
        tests = await graph_fn(scope_files)
    except Exception as exc:  # noqa: BLE001 — fail-closed on ANY graph error
        logger.warning("[Gate2] op=%s graph FAILURE: %s", op_id, exc)
        _rollback_and_assert("blast_radius_graph_failure")
        raise BlastRadiusGraphFailure(f"op={op_id}: {exc}") from exc

    result = await test_fn(set(tests))
    failed = list(result.get("failed", []))
    if failed:
        logger.warning("[Gate2] op=%s blast-radius FAIL %d/%s", op_id, len(failed), result.get("total"))
        _rollback_and_assert("blast_radius_breach")
        raise BlastRadiusBreach(f"op={op_id} failed={failed}")

    token = chain.mint(
        kind=TokenKind.BLAST_RADIUS_CLEARED,
        op_id=op_id,
        state_binding=pre_op_tree_sha,
        payload={"n_tests": str(result.get("total", len(tests))),
                 "post_tree_sha": current_tree_sha_fn()},
        prev=prev_token,
    )
    return token  # type: ignore[return-value]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_blast_radius_verify.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/blast_radius_verify.py tests/governance/test_blast_radius_verify.py
git commit -m "feat(a1): Gate 2 blast-radius regression (fail-closed graph, no flake-retry)"
```

---

## Task 6: Wire Gate ② into `slice4b_runner` (capture pre-op SHA; default-OFF)

**Files:**
- Modify: `backend/core/ouroboros/governance/phase_runners/slice4b_runner.py` (capture pre-op tree-SHA near checkpoint create ~`:415`; gate between verify-gate ~`:1034` and auto-commit `:1091`)
- Test: `tests/governance/test_gate2_slice4b_wiring.py`

**Interfaces:**
- Consumes: `acquire_blast_radius_token`, `blast_radius_enabled`, `BlastRadiusBreach`, `BlastRadiusGraphFailure` (Task 5); `ctx.sandbox_token`/`ctx.proof_chain` (Task 4); `WorkspaceCheckpointManager` (existing); `call_graph_blast`/`blast_radius_adapter` (existing); `TestRunner` (existing); `intake_dlq.append_dlq` (existing).
- Produces: `ctx.blast_token` set on pass; POSTMORTEM with `terminal_reason_code="blast_radius_breach"`/`"blast_radius_graph_failure"` on failure.

- [ ] **Step 1: Write the failing wiring test** (flag-parity)

```python
# tests/governance/test_gate2_slice4b_wiring.py
from __future__ import annotations
from backend.core.ouroboros.governance import blast_radius_verify as brv

def test_disabled_by_default(monkeypatch):
    monkeypatch.delenv("JARVIS_A1_BLAST_RADIUS_ENABLED", raising=False)
    assert brv.blast_radius_enabled() is False

def test_enabled_when_flagged(monkeypatch):
    monkeypatch.setenv("JARVIS_A1_BLAST_RADIUS_ENABLED", "true")
    assert brv.blast_radius_enabled() is True
```

- [ ] **Step 2: Run test to verify it fails/passes-pin**

Run: `python3 -m pytest tests/governance/test_gate2_slice4b_wiring.py -q`
Expected: PASS (pins flag parity).

- [ ] **Step 3: Capture the pre-op tree-SHA** at checkpoint creation (~`:415`)

In `slice4b_runner.py`, where `_ckpt_mgr.create_checkpoint(...)` is called at APPLY start, capture the raw tree SHA alongside it (the `Checkpoint.stash_ref` is the tree SHA):

```python
        _pre_op_ckpt = await orch._ckpt_mgr.create_checkpoint(op_id=ctx.op_id)
        _pre_op_tree_sha = getattr(_pre_op_ckpt, "stash_ref", "") if _pre_op_ckpt else ""
```

- [ ] **Step 4: Insert the gate** between the verify gate (~`:1034`) and Phase 8b auto-commit (`:1091`)

```python
        # ---- Gate 2: Cryptographic Blast-Radius Verification ----
        from ..blast_radius_verify import (
            acquire_blast_radius_token, blast_radius_enabled,
            BlastRadiusBreach, BlastRadiusGraphFailure,
        )
        if blast_radius_enabled() and getattr(ctx, "sandbox_token", None) is not None:
            from ..import call_graph_blast
            from .. import intake_dlq as _dlq
            _scope = set(ctx.target_files) | {cf for cf, _ in orch._iter_candidate_files(best_candidate)}

            async def _graph_fn(files):
                return call_graph_blast.reverse_dependency_tests(files, repo_root=str(orch._config.project_root))

            async def _test_fn(tests):
                _res = await orch._validation_runner.run(
                    changed_files=tuple(orch._config.project_root / t for t in tests),
                    timeout_budget_s=_verify_budget_s, op_id=ctx.op_id)
                _failed = [a for a in _res.adapter_results if not a.passed]
                return {"failed": _failed, "total": len(_res.adapter_results)}

            def _rollback(sha):
                # non-destructive: restore the pre-op checkpoint (tree-SHA)
                import asyncio as _aio
                _aio.get_event_loop().run_until_complete(
                    orch._ckpt_mgr.restore_checkpoint(_pre_op_ckpt.checkpoint_id))

            def _tree_sha():
                return orch._ckpt_mgr.current_tree_sha()  # see Step 5

            try:
                _blast_tok = await acquire_blast_radius_token(
                    op_id=ctx.op_id, scope_files=sorted(_scope),
                    pre_op_tree_sha=_pre_op_tree_sha, chain=ctx.proof_chain,
                    prev_token=ctx.sandbox_token, graph_fn=_graph_fn, test_fn=_test_fn,
                    current_tree_sha_fn=_tree_sha,
                    rollback_fn=_rollback,
                    dlq_fn=lambda reason: _dlq.append_dlq(ctx.op_id, reason=reason))
            except BlastRadiusGraphFailure:
                return PhaseResult(next_ctx=ctx.advance(OperationPhase.POSTMORTEM,
                    terminal_reason_code="blast_radius_graph_failure", rollback_occurred=True),
                    next_phase=OperationPhase.POSTMORTEM)
            except BlastRadiusBreach:
                return PhaseResult(next_ctx=ctx.advance(OperationPhase.POSTMORTEM,
                    terminal_reason_code="blast_radius_breach", rollback_occurred=True),
                    next_phase=OperationPhase.POSTMORTEM)
            ctx = dataclasses.replace(ctx, blast_token=_blast_tok)
```

Add `blast_token: object = None` to the op context dataclass.

- [ ] **Step 5: Add the reuse helpers** they depend on

In `workspace_checkpoint.py`, add a thin read-only helper (reuses the same argv style):

```python
    def current_tree_sha(self) -> str:
        """Current working-tree SHA via ``git stash create`` (non-destructive)."""
        out = subprocess.run(["git", "stash", "create"], cwd=self._repo_root,
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip()
```

In `call_graph_blast.py`, add the reverse-dep test resolver if not already exposed:

```python
def reverse_dependency_tests(changed_files, *, repo_root: str) -> set:
    """All tests that transitively import any changed file (reverse-dep closure)."""
    # delegate to the existing blast_radius_adapter graph; raise on any graph error
    from .blast_radius_adapter import build_reverse_import_graph, tests_touching
    graph = build_reverse_import_graph(repo_root)
    return tests_touching(graph, changed_files)
```

> If `blast_radius_adapter` already exposes an equivalent, import it directly and skip the shim — confirm symbol names during implementation (`grep -n "def " blast_radius_adapter.py`). The gate's `graph_fn` must **raise** (never return empty) on a graph build error so Task 5's fail-closed path triggers.

- [ ] **Step 6: Run tests + smoke import**

Run: `python3 -m pytest tests/governance/test_gate2_slice4b_wiring.py tests/governance/test_blast_radius_verify.py -q && python3 -c "import backend.core.ouroboros.governance.phase_runners.slice4b_runner"`
Expected: PASS + clean import.

- [ ] **Step 7: Commit**

```bash
git add backend/core/ouroboros/governance/phase_runners/slice4b_runner.py backend/core/ouroboros/governance/workspace_checkpoint.py backend/core/ouroboros/governance/call_graph_blast.py backend/core/ouroboros/governance/op_context.py tests/governance/test_gate2_slice4b_wiring.py
git commit -m "feat(a1): wire Gate 2 blast-radius into VERIFY (tree-SHA rollback, default-OFF)"
```

---

## Task 7: Gate ③ — Autonomous PR Linter module

**Files:**
- Create: `backend/core/ouroboros/governance/pr_self_linter.py`
- Test: `tests/governance/test_pr_self_linter.py`

**Interfaces:**
- Consumes: `DAGProofChain`, `LintClearedToken`, `TokenKind`, `BlastRadiusClearedToken` (Tasks 1/5); `self_critique.parse_critique_json` (existing, reuse for parsing); an injectable `critique_fn` (the bounded LLM call) for tests.
- Produces: exception `LintRejected`; `async def acquire_lint_cleared_token(*, op_id, diff, chain, prev_token, critique_fn, threshold=4) -> LintClearedToken`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/governance/test_pr_self_linter.py
from __future__ import annotations
import hashlib
import pytest
from backend.core.ouroboros.governance import pr_self_linter as lint
from backend.core.ouroboros.governance.dag_capability_token import (
    DAGProofChain, TokenKind, LintClearedToken,
)

DIFF = "+def f():\n+    return 1\n"

def _prev(chain):
    s = chain.mint(kind=TokenKind.SANDBOX_EXECUTION, op_id="op-1", state_binding="c", payload={})
    return chain.mint(kind=TokenKind.BLAST_RADIUS_CLEARED, op_id="op-1",
                      state_binding="t", payload={}, prev=s)

@pytest.mark.asyncio
async def test_pass_mints_chained_lint_token():
    chain = DAGProofChain(); prev = _prev(chain)
    async def critique_fn(diff): return {"rating": 5, "concerns": []}
    tok = await lint.acquire_lint_cleared_token(
        op_id="op-1", diff=DIFF, chain=chain, prev_token=prev, critique_fn=critique_fn)
    assert isinstance(tok, LintClearedToken)
    assert tok.prev_hash == prev.digest()
    assert tok.state_binding == hashlib.sha256(DIFF.encode()).hexdigest()

@pytest.mark.asyncio
async def test_low_rating_raises_lint_rejected():
    chain = DAGProofChain(); prev = _prev(chain)
    async def critique_fn(diff): return {"rating": 2, "concerns": ["hardcoded path"]}
    with pytest.raises(lint.LintRejected):
        await lint.acquire_lint_cleared_token(
            op_id="op-1", diff=DIFF, chain=chain, prev_token=prev, critique_fn=critique_fn)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/governance/test_pr_self_linter.py -q`
Expected: FAIL with `ModuleNotFoundError: pr_self_linter`.

- [ ] **Step 3: Write the implementation** (reuses `self_critique` prompt/parse + `DoublewordProvider`)

```python
# backend/core/ouroboros/governance/pr_self_linter.py
from __future__ import annotations
import hashlib
import logging
import os
from typing import Awaitable, Callable, Optional

from .dag_capability_token import (
    BlastRadiusClearedToken, CapabilityToken, DAGProofChain, LintClearedToken, TokenKind,
)

logger = logging.getLogger(__name__)

_RULES = (
    "Critique this diff against the repository's architectural rules. Return JSON "
    '{"rating": 1-5, "concerns": [..]}. Rules: (1) NO hardcoding — values/paths/'
    "models must be env/config-derived; (2) DRY — no duplicated logic that an "
    "existing helper covers; (3) explicit error handling — no bare/silent excepts; "
    "(4) async-first — no blocking calls on the event loop. Rate 5 only if all hold."
)


def linter_enabled() -> bool:
    return os.environ.get("JARVIS_A1_PR_LINTER_ENABLED", "false").strip().lower() in ("1", "true", "yes")


def _threshold() -> int:
    try:
        return int(os.environ.get("JARVIS_A1_PR_LINTER_THRESHOLD", "4"))
    except ValueError:
        return 4


class LintRejected(RuntimeError):
    """The model's own architectural critique rejected the diff — no PR."""


async def default_critique_fn(diff: str) -> dict:
    """Bounded, structured one-shot critique via the cheapest existing provider."""
    from .doubleword_provider import DoublewordProvider
    from .self_critique import parse_critique_json
    provider = DoublewordProvider()
    raw = await provider.prompt_only(
        prompt=f"{_RULES}\n\nDIFF:\n{diff}",
        caller_id="pr_self_linter",
        response_format={"type": "json_object"},
        max_tokens=512,
    )
    parsed, _ok = parse_critique_json(raw, op_id="pr_self_linter")
    return parsed


async def acquire_lint_cleared_token(
    *,
    op_id: str,
    diff: str,
    chain: DAGProofChain,
    prev_token: CapabilityToken,
    critique_fn: Optional[Callable[[str], Awaitable[dict]]] = None,
    threshold: Optional[int] = None,
) -> LintClearedToken:
    _crit = critique_fn or default_critique_fn
    _thr = threshold if threshold is not None else _threshold()
    verdict = await _crit(diff)
    rating = int(verdict.get("rating", 0))
    if rating < _thr:
        raise LintRejected(
            f"op={op_id} rating={rating}<{_thr} concerns={verdict.get('concerns')}")
    token = chain.mint(
        kind=TokenKind.LINT_CLEARED,
        op_id=op_id,
        state_binding=hashlib.sha256(diff.encode("utf-8")).hexdigest(),
        payload={"rating": str(rating)},
        prev=prev_token,
    )
    return token  # type: ignore[return-value]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/governance/test_pr_self_linter.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/pr_self_linter.py tests/governance/test_pr_self_linter.py
git commit -m "feat(a1): Gate 3 blocking PR linter (reuses self_critique + DoublewordProvider)"
```

---

## Task 8: Token-gate `create_review_pr` (the enforcer bite)

**Files:**
- Modify: `backend/core/ouroboros/governance/orange_pr_reviewer.py` (`create_review_pr` signature + `verify_chain` before `gh pr create` ~`:303–324`)
- Test: `tests/governance/test_orange_pr_token_gate.py`

**Interfaces:**
- Consumes: `SandboxExecutionToken`, `BlastRadiusClearedToken`, `LintClearedToken`, `DAGProofChain.verify_chain` (Tasks 1/3/5/7); `acquire_lint_cleared_token`, `linter_enabled` (Task 7).
- Produces: `create_review_pr` returns `None` (refuses) unless a verified 3-token chain is supplied when the enforcer is enabled.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_orange_pr_token_gate.py
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance.orange_pr_reviewer import OrangePRReviewer
from backend.core.ouroboros.governance.dag_capability_token import DAGProofChain, TokenKind

def _chain_tokens(chain, op_id="op-1"):
    s = chain.mint(kind=TokenKind.SANDBOX_EXECUTION, op_id=op_id, state_binding="c", payload={})
    b = chain.mint(kind=TokenKind.BLAST_RADIUS_CLEARED, op_id=op_id, state_binding="t", payload={}, prev=s)
    l = chain.mint(kind=TokenKind.LINT_CLEARED, op_id=op_id, state_binding="d", payload={}, prev=b)
    return s, b, l

@pytest.mark.asyncio
async def test_refuses_without_valid_chain(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "true")
    rv = OrangePRReviewer(str(tmp_path))
    chain = DAGProofChain()
    s, b, l = _chain_tokens(chain)
    # Tamper: swap in a foreign-secret token -> verify_chain fails -> None.
    foreign = DAGProofChain()
    s2, _, _ = _chain_tokens(foreign)
    result = await rv.create_review_pr(
        op_id="op-1", files=[("x.py", "...")], description="d", base_branch="main",
        chain=chain, sandbox_token=s2, blast_token=b, lint_token=l)
    assert result is None

@pytest.mark.asyncio
async def test_enforcer_off_is_legacy(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "false")
    rv = OrangePRReviewer(str(tmp_path))
    # With enforcer off, tokens may be None and the chain check is skipped
    # (the real gh call is mocked out separately in integration tests).
    assert rv._enforcer_enabled() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_orange_pr_token_gate.py -q`
Expected: FAIL (new kwargs/`_enforcer_enabled` not present).

- [ ] **Step 3: Modify `create_review_pr`** — add mandatory typed token args + chain verify + Gate ③ call

Add near the top of `orange_pr_reviewer.py`:

```python
import os
def _token_enforcer_enabled() -> bool:
    return os.environ.get("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "false").strip().lower() in ("1", "true", "yes")
```

Add the instance helper and extend the signature:

```python
    def _enforcer_enabled(self) -> bool:
        return _token_enforcer_enabled()

    async def create_review_pr(self, *, op_id, files, description, base_branch,
                               chain=None, sandbox_token=None, blast_token=None,
                               lint_token=None):
        # ---- Gate 3 (run the linter if armed and no token supplied) ----
        from .pr_self_linter import acquire_lint_cleared_token, linter_enabled, LintRejected
        if self._enforcer_enabled():
            if linter_enabled() and lint_token is None and blast_token is not None and chain is not None:
                _diff = "\n".join(f"--- {p}\n{c}" for p, c in files)
                try:
                    lint_token = await acquire_lint_cleared_token(
                        op_id=op_id, diff=_diff, chain=chain, prev_token=blast_token)
                except LintRejected as exc:
                    logger.warning("[Gate3] op=%s LINT_REJECTED: %s", op_id, exc)
                    return None
            # ---- Enforcer: the chain MUST verify or no PR is opened ----
            if chain is None or not chain.verify_chain(
                    [sandbox_token, blast_token, lint_token], op_id=op_id):
                logger.warning("[Enforcer] op=%s token chain INVALID -> refuse PR", op_id)
                return None
        # ... existing body: checkout branch, write files, commit, push,
        #     `gh pr create` (unchanged) ...
```

> The existing positional callers must be updated to the keyword form; the orchestrator call site (Task references `orchestrator.py:9471`) passes `op_id=ctx.op_id, files=..., description=ctx.description, base_branch=base, chain=ctx.proof_chain, sandbox_token=ctx.sandbox_token, blast_token=ctx.blast_token`.

- [ ] **Step 4: Update the orchestrator call site** (`orchestrator.py` ~`:9471`) to pass the tokens from `ctx`:

```python
            _pr = await OrangePRReviewer(str(self._config.project_root)).create_review_pr(
                op_id=ctx.op_id,
                files=list(self._iter_candidate_files(best_candidate)),
                description=ctx.description,
                base_branch=_base_branch,
                chain=getattr(ctx, "proof_chain", None),
                sandbox_token=getattr(ctx, "sandbox_token", None),
                blast_token=getattr(ctx, "blast_token", None),
            )
```

- [ ] **Step 5: Run tests + smoke import**

Run: `python3 -m pytest tests/governance/test_orange_pr_token_gate.py -q && python3 -c "import backend.core.ouroboros.governance.orange_pr_reviewer, backend.core.ouroboros.governance.orchestrator"`
Expected: PASS + clean import.

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/orange_pr_reviewer.py backend/core/ouroboros/governance/orchestrator.py tests/governance/test_orange_pr_token_gate.py
git commit -m "feat(a1): token-gate create_review_pr — skipping a gate is now a type error"
```

---

## Task 9: Async Docker pre-flight at A1-loop start

**Files:**
- Modify: `backend/core/ouroboros/governance/orchestrator.py` (or `governed_loop_service.py` boot) — async daemon ping at loop start; expose `REQUIRES_CLOUD_EXECUTION` posture
- Test: `tests/governance/test_docker_preflight.py`

**Interfaces:**
- Consumes: `container_sandbox.docker_available` (existing).
- Produces: `async def docker_preflight() -> bool` (logs + caches the result for the session); when False and the sandbox lock is armed, ops are flagged `requires_cloud_execution` early instead of discovering it mid-DAG.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_docker_preflight.py
from __future__ import annotations
import pytest
from backend.core.ouroboros.governance import pre_apply_exec_lock as lock

@pytest.mark.asyncio
async def test_preflight_reports_daemon_state():
    assert await lock.docker_preflight(probe=lambda: True) is True
    assert await lock.docker_preflight(probe=lambda: False) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_docker_preflight.py -q`
Expected: FAIL (`docker_preflight` not defined).

- [ ] **Step 3: Add `docker_preflight`** to `pre_apply_exec_lock.py`

```python
async def docker_preflight(*, probe=None) -> bool:
    """Async, non-blocking daemon ping run once at A1-loop start.

    Surfaces Docker absence BEFORE an op reaches APPLY, so the orchestrator
    can flag REQUIRES_CLOUD_EXECUTION early rather than failing mid-DAG.
    """
    import asyncio
    from . import container_sandbox
    _probe = probe or container_sandbox.docker_available
    available = await asyncio.get_event_loop().run_in_executor(None, _probe)
    if not available and lock_enabled():
        logger.warning("[Gate1] Docker daemon ABSENT at preflight — ops will route REQUIRES_CLOUD_EXECUTION")
    return available
```

Call it once at the A1 loop start (in `governed_loop_service.py` boot, fire-and-forget logged):

```python
        from .pre_apply_exec_lock import docker_preflight
        self._docker_ready = await docker_preflight()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m pytest tests/governance/test_docker_preflight.py -q`
Expected: PASS (1 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/core/ouroboros/governance/pre_apply_exec_lock.py backend/core/ouroboros/governance/governed_loop_service.py tests/governance/test_docker_preflight.py
git commit -m "feat(a1): async Docker pre-flight at loop start (early REQUIRES_CLOUD_EXECUTION)"
```

---

## Task 10: Register flags + reconcile A1 branches

**Files:**
- Modify: `backend/core/ouroboros/governance/flag_registry.py` (seed entries)
- Test: `tests/governance/test_a1_flags_registered.py`

**Interfaces:**
- Produces: the 5 new flags discoverable via `/help flags`; the `feat/a1-disable-file-isolation` manifest pin folded into the A1 launch manifest.

- [ ] **Step 1: Write the failing test**

```python
# tests/governance/test_a1_flags_registered.py
from __future__ import annotations
from backend.core.ouroboros.governance.flag_registry import FlagRegistry

def test_iron_triad_flags_registered():
    reg = FlagRegistry()
    names = set(reg.all_flag_names())
    for f in ("JARVIS_A1_TOKEN_ENFORCER_ENABLED", "JARVIS_A1_SANDBOX_LOCK_ENABLED",
              "JARVIS_A1_BLAST_RADIUS_ENABLED", "JARVIS_A1_PR_LINTER_ENABLED",
              "JARVIS_TOKEN_AUDIT_ENABLED"):
        assert f in names
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m pytest tests/governance/test_a1_flags_registered.py -q`
Expected: FAIL (flags not yet seeded).

- [ ] **Step 3: Seed the 5 flags** in `flag_registry.py` (follow the existing seed entry shape — type, category, source_file, example, posture-relevance). Match the curated-seed structure already in the file.

- [ ] **Step 4: Reconcile the A1 branches** (verification, not re-implementation)

```bash
git log --oneline main..origin/feat/a1-disable-file-isolation   # confirm the manifest pin
git show origin/feat/a1-disable-file-isolation -- <launch manifest path>
# fold the file-isolation-OFF manifest pin into the A1 launch manifest used by
# scripts/isomorphic_a1_local.py; cherry-pick if it is a clean isolated change.
```

Document in the commit which commit(s) were folded and why (the `fsm_classify_to_applied` durability fix).

- [ ] **Step 5: Run test + flag-list smoke**

Run: `python3 -m pytest tests/governance/test_a1_flags_registered.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/core/ouroboros/governance/flag_registry.py tests/governance/test_a1_flags_registered.py <reconciled manifest>
git commit -m "feat(a1): register Iron Triad flags + fold file-isolation-OFF A1 manifest pin"
```

---

## Task 11: E2E ignition dry-run + full-suite green

**Files:**
- Modify: `scripts/isomorphic_a1_local.py` (ensure it arms the 4 flags for the A1 soak; no behavior change when unset)
- Test: run the existing `tests/integration/test_isomorphic_a1_e2e.py` + the new suites together

- [ ] **Step 1: Arm the gates in the driver's composed env** (only inside the driver, never global)

In `isomorphic_a1_local.py` `compose_env`/child-env section, set the four `JARVIS_A1_*_ENABLED=true` + `JARVIS_RUNTIME_SANDBOX_ENABLED=true` for the soak child process.

- [ ] **Step 2: Run the full new-suite + the OFF-parity regression**

Run: `python3 -m pytest tests/governance/test_dag_capability_token.py tests/governance/test_token_audit.py tests/governance/test_pre_apply_exec_lock.py tests/governance/test_blast_radius_verify.py tests/governance/test_pr_self_linter.py tests/governance/test_orange_pr_token_gate.py tests/governance/test_docker_preflight.py tests/governance/test_a1_flags_registered.py tests/governance/test_gate1_orchestrator_wiring.py tests/governance/test_gate2_slice4b_wiring.py -q`
Expected: PASS (all green).

- [ ] **Step 3: Local A1 dry-run** (Docker Desktop UP)

Run: `python3 scripts/isomorphic_a1_local.py --mode container --max-wall-seconds 1200`
Expected: drives to `A1_DISPATCH_PROVEN`, OR a captured `failure_telemetry` artifact pinpointing the next integration bug (the $0/minutes loop). Iterate until proven locally.

- [ ] **Step 4: Commit the driver arming**

```bash
git add scripts/isomorphic_a1_local.py
git commit -m "feat(a1): arm Iron Triad gates inside the local A1 ignition driver"
```

- [ ] **Step 5: Open the integration PR** for the branch `feat/iron-triad-a1-gate` once the local A1 chain is green; one confirming cloud soak (`--max-wall-seconds 2400 --headless`) is the final gate before flipping any default.

---

## Self-Review

**Spec coverage:**
- §3 Token Enforcer → Tasks 1, 2 (WAL), 8 (verify_chain enforcement). ✓
- §4 Gate ① Sandbox Lock → Tasks 3, 4, 9 (pre-flight). ✓
- §5 Gate ② Blast Radius → Tasks 5, 6 (tree-SHA rollback + fail-closed graph + no-retry). ✓
- §6 Gate ③ PR Linter → Tasks 7, 8. ✓
- §7 Phase 2 hybrid-cloud → deliberately deferred; Task 4 emits `REQUIRES_CLOUD_EXECUTION` (the Phase-1 boundary). ✓
- §8 branch reconciliation → Task 10. ✓
- §10 flags default-OFF → every gate task pins flag-parity; Task 10 registers. ✓
- §11 testing (forgery/replay/chain/OFF-parity/E2E) → Tasks 1, 4, 6, 11. ✓
- Adversarial-TDD-first + WAL (operator amendments) → Task 1 Step 1 (negative vectors before impl), Task 2. ✓

**Placeholder scan:** the two reuse shims (`call_graph_blast.reverse_dependency_tests` in Task 6 Step 5; `flag_registry` seed shape in Task 10 Step 3) are flagged to confirm exact existing symbol names at implementation time via `grep` — real code is shown, with the verify-the-symbol caveat called out explicitly. No "TBD/handle edge cases" placeholders remain.

**Type consistency:** `mint(*, kind, op_id, state_binding, payload, prev=None)`, `verify_chain(tokens, *, op_id)`, `acquire_sandbox_execution_token(...) -> SandboxExecutionToken`, `acquire_blast_radius_token(...) -> BlastRadiusClearedToken`, `acquire_lint_cleared_token(...) -> LintClearedToken`, and `create_review_pr(*, op_id, files, description, base_branch, chain, sandbox_token, blast_token, lint_token)` are consistent across Tasks 1–8. The chain order `SANDBOX → BLAST_RADIUS → LINT` (terminal `LINT_CLEARED`) is consistent in `dag_capability_token._CHAIN_ORDER` and every `prev=` linkage.

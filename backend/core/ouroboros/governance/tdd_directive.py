"""TDD-shaped generation — V1 minimum slice (NOT red-green proof).

**Honest scope label**: this module declares a TDD-shaped intent and
injects a prompt directive instructing the model to emit tests + impl
together (multi-file candidate, test file FIRST). It does NOT:

  * Run the tests to confirm they fail before accepting them (red).
  * Validate that the impl transitions red→green.
  * Gate on red-green proof before APPLY.

True test-first orchestration — two sequential GENERATE calls with red
capture between them and green proof after — is a separate orchestrator
sub-phase project scoped in ``docs/governance/tdd_red_green_plan.md``
(V1.1). V1 is a **prompt contract, not a proof obligation**.

Why ship the minimum anyway:

  * Operators who know they want TDD-style output can declare it at
    intake (``evidence["tdd_mode"] = True``) or via ``/tdd <op-id>``.
  * The multi-file coverage gate + VALIDATE phase already run tests
    against the candidate bundle, so in practice the impl must make
    the tests pass or VALIDATE fails and L2 kicks in.
  * The declarative layer ships the intent semantics. When the full
    red-green FSM lands in V1.1, the same flag flips from "prompt hint"
    to "pipeline sub-phase trigger" without client-side changes.

Env gates:

    JARVIS_TDD_MODE_ENABLED (default 1)
        Master switch. When 0, even flagged intents generate normally.

Authority invariant: this module writes prompt text only. It never
mutates risk classification, routing law, guardian findings, or any
deterministic engine input.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger("Ouroboros.TDDDirective")

_ENV_ENABLED = "JARVIS_TDD_MODE_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

_TDD_EVIDENCE_KEY = "tdd_mode"


def tdd_enabled() -> bool:
    """Master switch. Default ON — honors the intent flag when set."""
    return os.environ.get(_ENV_ENABLED, "1").strip().lower() in _TRUTHY


def is_tdd_op(ctx: Any) -> bool:
    """Return True when this op should receive the TDD prompt directive.

    Detection walks the context's ``evidence`` dict (where intake
    envelopes stash signal metadata). Missing evidence / wrong shape
    returns False — fail-closed to normal generation.
    """
    if not tdd_enabled():
        return False
    try:
        evidence = getattr(ctx, "evidence", None)
        if evidence is None:
            # Some contexts carry evidence in strategic_memory_digest
            # or similar — try the dict-shaped keys we know about.
            return False
        if isinstance(evidence, dict):
            val = evidence.get(_TDD_EVIDENCE_KEY)
        else:
            val = getattr(evidence, _TDD_EVIDENCE_KEY, None)
        return bool(val)
    except Exception:  # noqa: BLE001
        return False


def tdd_prompt_directive() -> str:
    """The TDD-shaped prompt text injected at CONTEXT_EXPANSION.

    Deliberately concise (~150 words) so it adds ≤500 prompt tokens.
    Tells the model to structure output as a multi-file candidate
    with test file FIRST, uses explicit ``files: [...]`` contract
    (already enforced by the multi-file coverage gate).
    """
    return (
        "## Test-First Generation (TDD mode)\n\n"
        "This op is marked TDD. Your ``files: [...]`` candidate MUST "
        "include both a test file AND the implementation file(s). The "
        "test file is the authoritative specification of desired "
        "behavior; the impl exists to make the tests pass.\n\n"
        "Required output shape:\n\n"
        "1. **First entry** in ``files: [...]`` is a test file "
        "(e.g. ``tests/test_<feature>.py``). It must import the symbol(s) "
        "under test and assert concrete behavior. Prefer pytest idioms.\n"
        "2. **Subsequent entries** are the implementation file(s) the "
        "tests exercise. The impl must satisfy every assertion in the "
        "test file.\n"
        "3. Each file's ``rationale`` should explain how it relates "
        "to the TDD contract: the test file says \"this is what correct "
        "looks like\"; the impl files say \"this is how I make it so\".\n\n"
        "Do NOT:\n"
        "  * Emit impl without tests (VALIDATE will run the tests and "
        "the op will fail).\n"
        "  * Write tests that only assert trivially-true things (the "
        "SemanticGuardian's test_assertion_inverted pattern catches "
        "common failure modes).\n"
        "  * Add tests that already pass against the pre-candidate code "
        "— tests must describe the NEW behavior.\n\n"
        "Honest caveat: V1 of TDD mode is a prompt contract. The "
        "orchestrator does not yet execute a red-green proof (run tests "
        "before impl, confirm they fail, then run after impl, confirm "
        "they pass). V1.1 will add that FSM. For now, VALIDATE runs "
        "the tests once against the final bundle — if impl doesn't "
        "make them pass, VALIDATE fails and L2 Repair engages.\n"
    )


def stamp_tdd_evidence(evidence: Any, *, on: bool = True) -> dict:
    """Return an ``evidence`` dict with ``tdd_mode`` set.

    Helper for ``/tdd <op-id>`` slash command and sensors that want to
    mark an intent as TDD-shaped without building a full envelope.
    Treats the input defensively — None / non-dict inputs yield a
    fresh dict with just the flag.
    """
    if isinstance(evidence, dict):
        out = dict(evidence)
    else:
        out = {}
    out[_TDD_EVIDENCE_KEY] = bool(on)
    return out

"""Isolation for the phase-runner suite.

Two different hazards live here, and conflating them is why
`test_phase_dispatcher_terminals.py` behaved differently in isolation than in
a full run.

Leaking OUT vs depending IN
---------------------------
The usual test-pollution shape is state leaking *out* of a test and breaking a
later one — isolation passes, the suite fails. This file exhibited the
inverse: 15 failures alone, 2 in-suite. It was not leaking; it *depended* on
ambient state something else happened to establish. A blanket
"reset everything to pristine" fixture would have made it fail MORE, which is
worth stating plainly because that is the reflex the symptom invites.

The actual cause was a fake, not a fixture: `_build_stack` modelled the legacy
`is_cancel_requested` cancel surface and never learned about the per-op
`CancelToken` registry added by W3(7) Slice 2, so `is_cancelled` auto-vivified
truthy and every op looked cancelled. That is fixed at the fake.

What genuinely can leak
-----------------------
`cancel_token_var` is a module-level `ContextVar` the dispatcher binds for the
duration of a run and resets in a `finally`. Under `asyncio` task isolation
that reset is usually redundant — which is exactly why a gap here would stay
invisible for a long time. But a test that raises between `set()` and the
reset, or one that drives the dispatcher outside a task boundary, leaves a
stale token bound for whatever runs next. The next test then sees an op that
some *other* test cancelled.

So this snapshots and restores it around every test in the package. It is
deliberately narrow: it pins the one piece of ambient state this suite is
known to share, rather than asserting a pristine world the suite does not
actually want.
"""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_cancel_token_var():
    """Snapshot + restore the ambient cancel ContextVar around each test.

    Restores by TOKEN where possible — `ContextVar.reset(token)` returns the
    variable to the exact state it had before this test's `set()`, including
    "was never set at all", which assigning `None` cannot express. A variable
    explicitly bound to None is not the same as an unbound one, and the
    dispatcher's `pctx.cancel_token is None` check is sensitive to which of
    those it meets.
    """
    try:
        from backend.core.ouroboros.governance.cancel_token import (
            cancel_token_var,
        )
    except Exception:  # noqa: BLE001 — never block the suite on an import
        yield
        return

    # `set()` here so we hold a token that can restore the *unset* state too.
    sentinel = cancel_token_var.set(None)
    try:
        yield
    finally:
        try:
            cancel_token_var.reset(sentinel)
        except (ValueError, RuntimeError):
            # Reset across a different Context — the token is not valid here.
            # Fall back to clearing, which is still strictly better than
            # leaving another test's cancelled op bound.
            try:
                cancel_token_var.set(None)
            except Exception:  # noqa: BLE001
                pass

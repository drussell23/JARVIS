"""Which session a decision record belongs to — resolved once, for everyone.

WHY THIS EXISTS
---------------
Nine modules resolved the session id with the same three lines::

    os.environ.get("OUROBOROS_BATTLE_SESSION_ID", "").strip() or "default"

Nine copies of one decision is the defect this repository keeps shipping, and
here it had a consequence beyond tidiness. ``OUROBOROS_BATTLE_SESSION_ID`` is
set by the battle-test harness and by nothing else, so under pytest it is
unset — and every test that exercises a real write path landed in the
PRODUCTION ledger, ``.jarvis/determinism/default/decisions.jsonl``.

Measured on this repository:

    39,934 records in the production ledger
     5,619 (14%) written by 19 synthetic fixture op_ids
           op-test-001 (2004), op-off (1931), op-on (1079), op-det-1 (109)…
           2,184 property_claim · 1,486 terminal_postmortem
           546 route_assignment · 523 advisor_verdict · 440 generate

That was harmless while nothing read the ledger. It stopped being harmless
when the MetaSensor was mounted: its dormancy detectors compute rates over
exactly this file, so every verdict about whether the organism is learning
was being taken over a population that is 14% test fixtures. The fixtures
also reuse their op_ids across months of runs, which manufactures
relationships that never existed — a postmortem from one run matching claims
written 89 days later by a different process, purely because both said
``op-off``.

THE FIX IS TO ASK, NOT TO GUESS
-------------------------------
``PYTEST_CURRENT_TEST`` is set by pytest itself, for the duration of every
test, and unset everywhere else. It is the same discipline
``progress_board._is_test_path`` uses when it reads ``testpaths`` out of
``pytest.ini``: the question "is this a test?" already has an authoritative
answer, and restating a convention is how the wrong answer gets shipped.

So an unqualified resolution under pytest yields a test-scoped session
instead of ``default``. Nothing is blocked, nothing raises, and a test that
WANTS the production session still gets it by naming it — being explicit is
the whole difference between a deliberate write and an accidental one.

Isolation is per-test-module rather than per-test: a test that writes in one
function and reads in another is an ordinary shape, and per-function scoping
would break it while fixing nothing. Two modules cannot collide, and that is
the boundary that matters.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Optional

logger = logging.getLogger("Ouroboros.SessionIdentity")

SESSION_IDENTITY_SCHEMA_VERSION = "session_identity.v1"

#: Set by the battle-test harness. The only signal that a real session is
#: running, and the reason an unset value cannot simply mean "production".
SESSION_ENV = "OUROBOROS_BATTLE_SESSION_ID"

#: Set by pytest for the duration of every test, in the form
#: ``path/to/test_file.py::TestClass::test_name (call)``. Unset outside a
#: test run — which is exactly the discrimination this module needs and the
#: reason it does not have to guess.
PYTEST_ENV = "PYTEST_CURRENT_TEST"

#: What an unqualified production resolution has always returned. Preserved
#: exactly so that nothing outside a test observes any change.
DEFAULT_SESSION = "default"

#: Prefix marking a session that exists only because a test asked for one.
#: Greppable, and obvious in a directory listing next to real session ids.
TEST_SESSION_PREFIX = "pytest"

__all__ = [
    "SESSION_IDENTITY_SCHEMA_VERSION",
    "SESSION_ENV",
    "PYTEST_ENV",
    "DEFAULT_SESSION",
    "TEST_SESSION_PREFIX",
    "resolve_session_id",
    "test_isolation_enabled",
    "under_pytest",
]

#: Anything outside this set is replaced. A session id becomes a DIRECTORY
#: NAME, so a test id containing '/' would silently write outside the ledger
#: root — the one way this module could make things worse than it found them.
_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]+")


def test_isolation_enabled() -> bool:
    """``JARVIS_LEDGER_TEST_ISOLATION`` (default ``true``).

    Off restores the previous behaviour exactly — tests write to
    ``default`` — which is the escape hatch for a test that is deliberately
    asserting on cross-session behaviour and would rather opt out wholesale
    than name a session.
    """
    raw = os.environ.get("JARVIS_LEDGER_TEST_ISOLATION", "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def under_pytest() -> bool:
    """True iff pytest is currently executing a test in this process."""
    return bool(os.environ.get(PYTEST_ENV, "").strip())


def _test_session_id() -> str:
    """A session id scoped to the currently-running test MODULE.

    ``PYTEST_CURRENT_TEST`` looks like::

        tests/governance/test_meta_sensor.py::test_scan_once (call)

    Only the module part is used. Per-function scoping would isolate more and
    break the ordinary shape of a test that writes in one function and reads
    in another; per-module is the boundary where collisions actually happen.

    Falls back to a constant rather than to ``default`` if the variable is
    malformed — an unparseable test id is still a test, and the one outcome
    to avoid is quietly resuming writes to production.
    """
    raw = os.environ.get(PYTEST_ENV, "").strip()
    module = raw.split("::", 1)[0].strip() if raw else ""
    if not module:
        return f"{TEST_SESSION_PREFIX}-unknown"
    stem = module.rsplit("/", 1)[-1]
    if stem.endswith(".py"):
        stem = stem[:-3]
    cleaned = _UNSAFE.sub("-", stem).strip("-")
    return f"{TEST_SESSION_PREFIX}-{cleaned or 'unknown'}"


def resolve_session_id(session_id: Optional[str] = None) -> str:
    """The session a decision record belongs to. NEVER raises.

    Precedence, strictest first:

    1. An explicitly supplied ``session_id`` — always wins, including inside
       a test. Naming a session is a deliberate act and this must not
       second-guess it, or a test written to exercise cross-session behaviour
       becomes untestable.
    2. ``OUROBOROS_BATTLE_SESSION_ID`` — a real session is running.
    3. Under pytest with neither of the above: a test-scoped session, so an
       accidental write cannot reach the production ledger.
    4. ``default``.

    Sanitised in every branch, because the return value becomes a directory
    name and a caller-supplied id containing a path separator would write
    outside the ledger root.
    """
    try:
        if session_id is not None and str(session_id).strip():
            return _sanitise(str(session_id).strip())
        from_env = os.environ.get(SESSION_ENV, "").strip()
        if from_env:
            return _sanitise(from_env)
        if test_isolation_enabled() and under_pytest():
            return _test_session_id()
        return DEFAULT_SESSION
    except Exception:  # noqa: BLE001 — a path resolver must not be the thing
        logger.debug("[SessionIdentity] resolution degraded", exc_info=True)
        return DEFAULT_SESSION


def _sanitise(raw: str) -> str:
    cleaned = _UNSAFE.sub("-", raw).strip("-.")
    return cleaned or DEFAULT_SESSION

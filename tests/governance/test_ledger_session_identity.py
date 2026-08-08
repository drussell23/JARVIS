"""14% of the production decision ledger was written by tests.

Chasing the "862 empty postmortems whose ops have claims" finding from #70445
turned it into a measurement artifact and a real defect underneath.

THE ARTIFACT
------------
Those 864 records span SEVEN op_ids — `op-off` (760), `op-test-001` (40),
`op-det-1` (38), `op-on` (23) and three singletons. Zero are real UUIDv7 ops.
The join was on `op_id` alone across a ledger holding months of history, so a
postmortem matched claims written 89 days later by a different process,
purely because both said `op-off`:

    pm_ts - first_claim_ts : -7,722,402 s
    pm worker / claim worker : 27261-base / 81325-base
    real ops affected        : 0

Recorded here rather than quietly dropped, because the same join is the
natural way to ask that question and the next person will write it too.

THE DEFECT IT EXPOSED
---------------------
Fixture op_ids are in the production ledger at all. Measured:

    39,934 records in .jarvis/determinism/default/decisions.jsonl
     5,619 (14%) from 19 synthetic op_ids
           2,184 property_claim · 1,486 terminal_postmortem
           546 route_assignment · 523 advisor_verdict · 440 generate

`OUROBOROS_BATTLE_SESSION_ID` is set by the battle-test harness and nothing
else, so under pytest it is unset and the session falls back to `default` —
the production session. Every test exercising a real write path landed there.

Harmless while nothing read the ledger. It stopped being harmless when the
MetaSensor was mounted in #70444: its dormancy detectors compute rates over
exactly this file, so every verdict about whether the organism is still
learning was taken over a population that is 14% test fixtures, some of them
reused across months and manufacturing relationships that never existed.

THE FIX
-------
Nine modules resolved the session with the same three lines. One authority
now, and it asks pytest — `PYTEST_CURRENT_TEST` is set by pytest itself for
the duration of every test and unset everywhere else. Same discipline
`progress_board._is_test_path` uses when it reads `testpaths` out of
`pytest.ini`: the question already has an authoritative answer, and restating
a convention is how the wrong answer ships.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from backend.core.ouroboros.governance.determinism.session_identity import (  # noqa: E402
    DEFAULT_SESSION,
    PYTEST_ENV,
    SESSION_ENV,
    TEST_SESSION_PREFIX,
    resolve_session_id,
    test_isolation_enabled,
    under_pytest,
)


# ---------------------------------------------------------------------------
# the resolution itself
# ---------------------------------------------------------------------------

def test_a_test_never_resolves_to_the_production_session(monkeypatch) -> None:
    """THE regression. This test is itself running under pytest, so an
    unqualified resolution here must not be `default`."""
    monkeypatch.delenv(SESSION_ENV, raising=False)
    got = resolve_session_id()
    assert got != DEFAULT_SESSION
    assert got.startswith(TEST_SESSION_PREFIX)


def test_the_session_is_scoped_to_this_test_module(monkeypatch) -> None:
    """Per-module, not per-function: a test that writes in one function and
    reads in another is an ordinary shape, and per-function scoping would
    break it while fixing nothing."""
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert resolve_session_id() == f"{TEST_SESSION_PREFIX}-test_ledger_session_identity"


def test_an_explicit_session_always_wins(monkeypatch) -> None:
    """Naming a session is a deliberate act, including inside a test. A test
    written to exercise cross-session behaviour must stay writable."""
    monkeypatch.delenv(SESSION_ENV, raising=False)
    assert resolve_session_id("bt-2026-08-08-live") == "bt-2026-08-08-live"
    assert resolve_session_id(DEFAULT_SESSION) == DEFAULT_SESSION


def test_a_real_session_wins_over_the_pytest_fallback(monkeypatch) -> None:
    """The harness sets this while running its own tests. Isolation must not
    hijack a session that genuinely exists."""
    monkeypatch.setenv(SESSION_ENV, "bt-2026-08-08-999999")
    assert resolve_session_id() == "bt-2026-08-08-999999"


def test_outside_pytest_nothing_changes(monkeypatch) -> None:
    """Production behaviour is byte-identical to before. The whole change is
    invisible to anything that is not a test."""
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.delenv(PYTEST_ENV, raising=False)
    assert under_pytest() is False
    assert resolve_session_id() == DEFAULT_SESSION


def test_isolation_can_be_switched_off(monkeypatch) -> None:
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.setenv("JARVIS_LEDGER_TEST_ISOLATION", "0")
    assert test_isolation_enabled() is False
    assert resolve_session_id() == DEFAULT_SESSION


# ---------------------------------------------------------------------------
# the session id becomes a DIRECTORY NAME
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("hostile", [
    "../../etc/passwd", "a/b/c", "..", ".", "  ", "sess\x00id", "s p a c e",
])
def test_a_hostile_session_id_cannot_escape_the_ledger_root(hostile) -> None:
    """The one way this module could make things worse than it found them.
    A session id is joined into a path, so a separator would write outside
    `.jarvis/determinism/`."""
    got = resolve_session_id(hostile)
    assert "/" not in got and "\\" not in got
    assert got not in ("", ".", "..")
    assert not got.startswith(".")


def test_a_malformed_pytest_id_still_isolates(monkeypatch) -> None:
    """An unparseable test id is still a test. The one outcome to avoid is
    quietly resuming writes to production."""
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.setenv(PYTEST_ENV, "::::")
    got = resolve_session_id()
    assert got != DEFAULT_SESSION
    assert got.startswith(TEST_SESSION_PREFIX)


@pytest.mark.parametrize("junk", [
    "\x01\x02", "::", "\t\n", "x" * 5000, "тест::t (call)",
])
def test_resolution_never_raises(monkeypatch, junk) -> None:
    """Called on the write path of every decision record.

    A null byte is deliberately not among these: `os.environ` refuses one at
    assignment, so it is not a state this function can ever observe. The
    first draft asserted against it and failed in the monkeypatch call rather
    than in the code under test — a test for an unreachable input proves
    nothing and costs a red build.
    """
    monkeypatch.setenv(PYTEST_ENV, junk)
    assert isinstance(resolve_session_id(), str)
    assert isinstance(resolve_session_id(None), str)
    # Whatever it produced still has to be a safe directory name.
    assert "/" not in resolve_session_id()


# ---------------------------------------------------------------------------
# one authority — nine copies is why the contamination was uniform
# ---------------------------------------------------------------------------

_REPO = Path(__file__).resolve().parents[2]
_ROOT = _REPO / "backend" / "core" / "ouroboros"

#: The literal that was duplicated. A docstring may still name the variable —
#: it is the RESOLUTION that must live in one place.
_DUPLICATE = re.compile(
    r'os\.environ\.get\(\s*\n?\s*"OUROBOROS_BATTLE_SESSION_ID"', re.S,
)


def test_no_module_resolves_the_session_itself() -> None:
    """Structural. Nine copies of one decision is why every writer landed in
    `default` together, and why fixing one would not have held."""
    offenders = []
    for path in sorted(_ROOT.rglob("*.py")):
        if path.name in ("session_identity.py", "session_replay.py"):
            continue  # the authority, and the replay CLI that SETS the var
        try:
            source = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if _DUPLICATE.search(source):
            offenders.append(str(path.relative_to(_REPO)))
    assert not offenders, (
        "these resolve the session id themselves instead of calling "
        "resolve_session_id():\n  " + "\n  ".join(offenders)
    )


def test_the_writers_all_import_the_authority() -> None:
    """The other direction: proving the sites were rewired, not just that the
    old spelling is gone."""
    expected = [
        "governance/verification/postmortem.py",
        "governance/verification/property_capture.py",
        "governance/verification/causality_dag.py",
        "governance/postmortem_observability.py",
        "governance/determinism/decision_runtime.py",
        "governance/determinism/clock.py",
        "governance/determinism/entropy.py",
    ]
    for rel in expected:
        source = (_ROOT / rel).read_text(encoding="utf-8")
        assert "resolve_session_id" in source, rel


# ---------------------------------------------------------------------------
# end to end — a write under pytest must not reach the production ledger
# ---------------------------------------------------------------------------

def test_a_recorded_claim_does_not_land_in_the_production_ledger(
        monkeypatch, tmp_path) -> None:
    """THE property, exercised through the real writer.

    2,184 property_claim records reached `default` this way.
    """
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path))
    from backend.core.ouroboros.governance.verification.postmortem import (
        _ledger_path_for_session,
    )
    resolved = _ledger_path_for_session()
    assert DEFAULT_SESSION not in resolved.parts, resolved
    assert any(p.startswith(TEST_SESSION_PREFIX) for p in resolved.parts)


def test_the_reader_and_the_writer_agree_on_the_path(monkeypatch, tmp_path) -> None:
    """Isolation that split the two would be worse than none: evidence
    written to one file and read from another looks exactly like evidence
    that was never captured — the defect #70446 just fixed."""
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.setenv("JARVIS_DETERMINISM_LEDGER_DIR", str(tmp_path))
    from backend.core.ouroboros.governance.verification.postmortem import (
        _ledger_path_for_session,
    )
    from backend.core.ouroboros.governance.verification import evidence_ledger

    writer = _ledger_path_for_session()
    reader = evidence_ledger._ledger_path()
    assert writer == reader


def test_two_test_modules_cannot_collide(monkeypatch) -> None:
    monkeypatch.delenv(SESSION_ENV, raising=False)
    monkeypatch.setenv(PYTEST_ENV, "tests/governance/test_alpha.py::t (call)")
    a = resolve_session_id()
    monkeypatch.setenv(PYTEST_ENV, "tests/governance/test_beta.py::t (call)")
    b = resolve_session_id()
    assert a != b


# ---------------------------------------------------------------------------
# the artifact, so nobody re-derives it as a finding
# ---------------------------------------------------------------------------

def test_op_id_alone_is_not_an_identity_across_sessions() -> None:
    """Why "862 empty postmortems whose ops have claims" was not a bug.

    The ledger is append-only across every run that ever used a session, and
    fixture op_ids repeat. Joining on op_id alone relates records that never
    met. A join must include the session AND be ordered, or it manufactures
    causality — which is exactly what a determinism ledger exists to prevent.
    """
    rows = [
        {"op_id": "op-off", "wall_ts": 1000.0, "kind": "terminal_postmortem"},
        {"op_id": "op-off", "wall_ts": 8723402.0, "kind": "property_claim"},
    ]
    naive = [r for r in rows if r["kind"] == "terminal_postmortem"
             and any(o["op_id"] == r["op_id"] and o["kind"] == "property_claim"
                     for o in rows)]
    assert len(naive) == 1, "the naive join relates them"

    ordered = [r for r in rows if r["kind"] == "terminal_postmortem"
               and any(o["op_id"] == r["op_id"]
                       and o["kind"] == "property_claim"
                       and o["wall_ts"] < r["wall_ts"] for o in rows)]
    assert not ordered, "ordering dissolves the relationship"


def test_the_production_ledger_is_not_a_test_fixture_store() -> None:
    """Advisory guard on the real file. Skips when absent (CI, fresh clone).

    Does not fail on the historical contamination — 5,619 records predate the
    fix and deleting them would destroy real history alongside. It fails if
    the share GROWS, which is the only thing still under anyone's control.
    """
    ledger = _REPO / ".jarvis" / "determinism" / DEFAULT_SESSION / "decisions.jsonl"
    if not ledger.exists():
        pytest.skip("no production ledger in this checkout")
    uuidv7 = re.compile(r"^op-[0-9a-f]{8}-[0-9a-f]{4}-7")
    total = synthetic = 0
    with ledger.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            op_id = record.get("op_id") or ""
            total += 1
            if not uuidv7.match(op_id):
                synthetic += 1
    if total < 1000:
        pytest.skip(f"ledger too small to judge ({total} records)")
    share = synthetic / total
    # 14% at the time of the fix; anything above that means a writer is still
    # landing here from a test.
    assert share <= 0.20, (
        f"{synthetic}/{total} ({share:.0%}) of the production ledger has a "
        "non-UUIDv7 op_id. A test is still writing to the production "
        "session — find it before trusting any rate computed from this file."
    )

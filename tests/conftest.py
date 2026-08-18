"""
Pytest configuration and shared fixtures for JARVIS AI Agent tests.

This file contains:
- Shared fixtures available to all tests
- Test hooks and configuration
- Common test utilities
"""

import asyncio
import logging as _logging
import pytest
import re
import sys
import os
from pathlib import Path

# Register additional fixture modules
pytest_plugins = [
    "tests.conftest_gmd_ferrari",
    "tests.ouroboros_pytest_plugin",
]

# Add project root to Python path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "backend"))


@pytest.fixture(scope="session")
def project_root_path():
    """Return the project root directory path."""
    return project_root


@pytest.fixture(scope="session")
def backend_path():
    """Return the backend directory path."""
    return project_root / "backend"


@pytest.fixture(scope="session")
def frontend_path():
    """Return the frontend directory path."""
    return project_root / "frontend"


@pytest.fixture(scope="function")
def mock_env_vars(monkeypatch):
    """Fixture to set mock environment variables for testing."""
    test_vars = {
        "ANTHROPIC_API_KEY": "test_api_key_placeholder",
        "JARVIS_ENV": "test",
    }
    for key, value in test_vars.items():
        monkeypatch.setenv(key, value)
    return test_vars


@pytest.fixture(scope="function")
def temp_test_dir(tmp_path):
    """Provide a temporary directory for test file operations."""
    return tmp_path


def pytest_configure(config):
    """Configure pytest with custom settings."""
    # Arm the live-state tripwire ONCE, at the top of the session. An audit
    # hook cannot be removed, so it must not be installed per-test.
    _install_live_state_audit_hook(Path(__file__).resolve().parents[1])
    # Register custom markers
    config.addinivalue_line(
        "markers", "unit: mark test as a unit test"
    )
    config.addinivalue_line(
        "markers", "integration: mark test as an integration test"
    )
    config.addinivalue_line(
        "markers", "functional: mark test as a functional test"
    )
    config.addinivalue_line(
        "markers", "performance: mark test as a performance test"
    )
    config.addinivalue_line(
        "markers", "e2e: mark test as an end-to-end test"
    )
    config.addinivalue_line(
        "markers", "vision: mark test as vision system related"
    )
    config.addinivalue_line(
        "markers", "voice: mark test as voice system related"
    )
    config.addinivalue_line(
        "markers", "backend: mark test as backend related"
    )
    config.addinivalue_line(
        "markers", "frontend: mark test as frontend related"
    )
    config.addinivalue_line(
        "markers", "slow: mark test as slow running"
    )
    config.addinivalue_line(
        "markers", "api: mark test as requiring API keys"
    )
    config.addinivalue_line(
        "markers", "permissions: mark test as requiring system permissions"
    )
    # Applied by COLLECTION, not by hand -- see `tests/pty_gate` for why a
    # decorator was the wrong mechanism (seven of eight files never wore one).
    # Registered here so `-m pty` / `-m "not pty"` select correctly and no
    # PytestUnknownMarkWarning is emitted for a marker nobody typed.
    config.addinivalue_line(
        "markers",
        "pty: test drives a real pseudo-terminal (auto-applied; "
        "see JARVIS_PTY_TESTS_REQUIRED)",
    )


def _apply_path_markers(items):
    """Path-derived markers (`unit`, `vision`, `slow`, ...).

    WAS a `pytest_collection_modifyitems` hook and therefore DEAD: a second
    definition of that name later in this module rebound it at import time,
    so Python kept only the last one and pytest never saw this body. Every
    path marker below has been silently absent -- `-m unit` selected nothing.
    Now a named helper called from the single composed hook, which is the
    only arrangement in which adding a third concern cannot delete a second."""
    # Add markers based on test location
    for item in items:
        # Add markers based on path
        test_path = str(item.fspath)

        if "/unit/" in test_path:
            item.add_marker(pytest.mark.unit)
        if "/integration/" in test_path:
            item.add_marker(pytest.mark.integration)
        if "/functional/" in test_path:
            item.add_marker(pytest.mark.functional)
        if "/performance/" in test_path:
            item.add_marker(pytest.mark.performance)
            item.add_marker(pytest.mark.slow)
        if "/e2e/" in test_path:
            item.add_marker(pytest.mark.e2e)
            item.add_marker(pytest.mark.slow)

        # Component markers
        if "/vision/" in test_path:
            item.add_marker(pytest.mark.vision)
        if "/voice/" in test_path:
            item.add_marker(pytest.mark.voice)
        if "/backend/" in test_path:
            item.add_marker(pytest.mark.backend)


def pytest_report_header(config):
    """Add custom header to pytest report."""
    return [
        "JARVIS AI Agent Test Suite",
        f"Project Root: {project_root}",
    ]


def _report_live_state_writes(terminalreporter) -> None:
    """Name every real-`.jarvis/` write the session made. NEVER raises.

    Reported rather than merely counted, for the same reason the skip summary
    below exists: a leak nobody can see is how `chat_spend.json` reached
    `spent_usd: 1.0` across six runs, and how fixture values sat in the live
    liquidity ledger long enough to produce a wrong root-cause diagnosis.
    """
    try:
        if not _LIVE_STATE_WRITES:
            return
        terminalreporter.write_sep("=", "LIVE STATE WRITTEN BY TESTS")
        for p in sorted(_LIVE_STATE_WRITES):
            terminalreporter.write_line(f"  {p}")
        terminalreporter.write_line(
            f"  {len(_LIVE_STATE_WRITES)} path(s) under the operator's real "
            ".jarvis/ were written during this run."
        )
        terminalreporter.write_line(
            "  Set JARVIS_TEST_STATE_TRIPWIRE=strict to make this a failure."
        )
    except Exception:  # noqa: BLE001 — a report must never break the summary
        pass


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Say out loud when the terminal-dependent suite did not run.

    Thirty-one tests — the whole proof that the cockpit answers a keystroke —
    skip when ``pty.openpty()`` raises, which it does in every sandboxed or
    containerised runner. The run then reports green, and the cockpit's
    interactive behaviour gets described as "never verified" while a suite
    proving it sits in the repository passing in 80 seconds on any machine
    with a terminal.

    A skip is an honest answer. A skip nobody can see is not, and this repo has
    already paid for that once: a suite that cannot collect reports no
    failures. So the summary states the count, the consequence, and the way to
    make it fatal.
    """
    # FIRST — the PTY summary below returns early in the common case, and a
    # leak report that only prints when a terminal was missing is a leak
    # report nobody reads.
    _report_live_state_writes(terminalreporter)

    try:
        from tests.pty_gate import (
            DISABLE_ENV_VAR, REQUIRED_ENV_VAR, SKIPPED, UNDECIDABLE,
            automark_enabled,
        )
    except Exception:  # noqa: BLE001 — a reporting aid must never break a run
        return

    write = terminalreporter.write_line

    # A DISARMED detector is louder than a skip, because a skip at least
    # names itself. If automarking is off, every terminal-driving test in the
    # repository is ungated and the run says nothing about it -- which is the
    # precise state this whole mechanism exists to make impossible.
    if not automark_enabled():
        write("")
        write(f"⚠️  pty auto-detection is DISABLED ({DISABLE_ENV_VAR}) — "
              f"terminal-driving tests are UNGATED.", yellow=True, bold=True)

    # "We could not read the file" is not "the file needs no terminal".
    # Reported separately from skips so an unreadable path can never be
    # mistaken for a clean verdict.
    if UNDECIDABLE:
        write("")
        write(f"⚠️  {len(UNDECIDABLE)} file(s) could not be inspected for pty "
              f"affinity — gating status UNKNOWN, not clear.",
              yellow=True, bold=True)
        for path, why in sorted(set(UNDECIDABLE))[:10]:
            write(f"   {path}: {why}", yellow=True)

    if not SKIPPED:
        return
    write("")
    write(f"⚠️  {len(SKIPPED)} terminal-dependent test(s) DID NOT RUN — "
          f"no pseudo-terminal was available.", yellow=True, bold=True)
    write("   The cockpit's interactive behaviour is UNPROVEN by this run: "
          "keybindings, mouse", yellow=True)
    write("   negotiation, alt-screen handling and the slash palette were all "
          "skipped, not passed.", yellow=True)
    write(f"   Make this fatal where a terminal is expected:  "
          f"{REQUIRED_ENV_VAR}=1", yellow=True)
    reasons = {reason for _, reason in SKIPPED}
    for reason in sorted(reasons):
        write(f"   cause: {reason}", yellow=True)
    write("")


@pytest.fixture(autouse=True)
def _isolate_fsm_checkpoint_dir(tmp_path, monkeypatch):
    """Point the FSM suspend/resume checkpoint ledger at a per-test tmp dir.

    The REAL ``.ouroboros/checkpoints`` (the CWD-relative default) can hold
    live PENDING checkpoints between soak windows; any test that starts an
    intake router would otherwise hydrate + CONSUME them (``mark_resumed``
    deletes the file) and re-inject heavy resume ops into the test's router --
    the exact contamination that ate window-2's pending checkpoints and hung
    the intake suite on 2026-07-01. Tests that need a specific dir still
    override via their own ``monkeypatch.setenv`` (applied after this
    autouse fixture).
    """
    monkeypatch.setenv(
        "JARVIS_CHECKPOINT_DIR", str(tmp_path / "_fsm_checkpoints"),
    )
    # Same isolation for the cross-run latency-physics ledger (Amnesia Cure):
    # tests must never read/poison the repo's real measured physics.
    monkeypatch.setenv(
        "JARVIS_LATENCY_LEDGER_PATH", str(tmp_path / "_latency_physics.json"),
    )


@pytest.fixture(autouse=True)
async def _cancel_pending_async_tasks():
    """
    Cancel any tasks left pending after each async test completes.

    Prevents tests that spawn background asyncio tasks (audio capture,
    socket listeners, etc.) from hanging the process indefinitely.
    This is the structural fix for zombie pytest processes.
    """
    yield
    loop = asyncio.get_event_loop()
    tasks = [t for t in asyncio.all_tasks(loop) if not t.done() and t is not asyncio.current_task()]
    if tasks:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.fixture(autouse=True)
def _isolate_durable_sensor_state(tmp_path, monkeypatch):
    """Keep test runs out of the operator's real ``.jarvis/`` state.

    `CUExecutionSensor` gained an append-only journal so failure evidence
    survives a crash during governance boot. That made the sensor stateful
    ACROSS PROCESS RUNS — which is the point, and which also means a test run
    starting from the repo root would read and write the operator's live
    journal.

    It is not a hypothetical: the first full run after the journal landed wrote
    29KB of synthetic failures into `.jarvis/cu_failure_journal.jsonl`, and the
    emission cooldowns it recorded then suppressed three unrelated tests in the
    same session. Two distinct harms — polluting real state, and one test
    silently deciding another's outcome.

    Fixed HERE rather than in the sensor: a production module that behaves
    differently under pytest is the kind of shortcut that makes green
    meaningless. Test isolation is the test layer's job.

    PER TEST, not per session. A session-scoped journal was tried first and was
    not enough: the spine tests all use one failure signature, so the emission
    cooldown written by the first still silenced the rest. Durable state has to
    be reset on the same cadence as the singleton it belongs to.

    A test that sets ``JARVIS_CU_JOURNAL_PATH`` itself still wins — `monkeypatch`
    applies in fixture order, and module-level fixtures run after this one — so
    the durability suite can point at its own file and exercise real restarts.
    """
    monkeypatch.setenv(
        "JARVIS_CU_JOURNAL_PATH",
        str(tmp_path / "cu_journal" / "cu_failure_journal.jsonl"),
    )
    # The voice-intent WAL is the same hazard with a different name: it is
    # durable, it lives under `.ouroboros/intents/`, and `unfinished()` is a
    # replay queue — so a test run against the real path could hand the
    # operator's next boot a queue of synthetic commands to resume.
    monkeypatch.setenv(
        "JARVIS_INTENT_JOURNAL_PATH",
        str(tmp_path / "intents" / "journal.jsonl"),
    )
    yield


# ---------------------------------------------------------------------------
# Global state VFS — tests must never touch the operator's live `.jarvis/`
# ---------------------------------------------------------------------------
#
# The fixture above is the right idea enumerated twice by hand, and the hazard
# it names kept recurring in state it did not cover. Two instances found on
# 2026-08-08 alone:
#
#   * `.jarvis/chat_spend.json` — the chat cost breaker's REAL ledger. Six
#     suite runs drove it to `spent_usd: 1.0, trips: 6`; the breaker then
#     correctly fail-closed and two tests started failing in a way that read
#     exactly like a Claude wiring bug. Pointing `JARVIS_CHAT_SPEND_LEDGER` at
#     a temp file → 51/51 green.
#   * `.jarvis/provider_liquidity.json` — carried `recorded_unix: 1010.0`
#     (→ 1969-12-31) and `tokens_remaining: 5000000`, the exact fixture pair
#     from `test_header_refresh_never_clears_quota_state`, left by a run
#     predating that test's isolation. It silently poisoned every reset-horizon
#     subtraction that read it, and cost a wrong root-cause diagnosis
#     ("monotonic/epoch mismatch") before the residue was identified.
#
# So this is DISCOVERED, not listed — the same discipline the module docstring
# already states for root-test collection: "The rule is computed, not a
# hand-maintained denylist, so it stays correct as files are added or gutted."

_STATE_VAR_RE = re.compile(
    r"""environ\.get\(\s*["'](JARVIS_[A-Z0-9_]+)["']\s*,\s*"""
    r"""["']([^"']*\.jarvis[^"']*)["']""",
    re.VERBOSE,
)

#: Bounded scan roots. A full sweep of backend/ is ~23k files; state lives in
#: these. Bounded for speed, not correctness — the audit tripwire below is what
#: makes coverage gaps loud rather than silent.
_STATE_SCAN_ROOTS = ("backend/core/ouroboros", "backend/core/secret_manager.py")

#: Seed set — vars the regex CANNOT find, because their `.jarvis` default is a
#: module constant rather than a literal inside `environ.get(...)`:
#:
#:     _DEFAULT_LEDGER = ".jarvis/chat_spend.json"
#:     ...
#:     explicit = os.environ.get(LEDGER_PATH_ENV_VAR, "")     # <- regex misses
#:
#: Verified empirically, not guessed: with discovery alone,
#: `test_chat_repl_claude_executor` still read the operator's real
#: `chat_spend.json` (`spent_usd: 1.0, trips: 6`) and two tests failed in a way
#: that reads exactly like a Claude wiring bug.
#:
#: A seed alongside discovery is the same shape FlagRegistry uses (52 curated
#: entries against a discovered universe). The seed is for what discovery
#: provably cannot reach — NOT a substitute for it.
_STATE_VAR_SEED = {
    "JARVIS_CHAT_SPEND_LEDGER": ".jarvis/chat_spend.json",
    "JARVIS_PROVIDER_LIQUIDITY_PATH": ".jarvis/provider_liquidity.json",
}


@pytest.fixture(scope="session")
def _discovered_state_vars():
    """Every ``JARVIS_*`` env var whose DEFAULT names a `.jarvis` path.

    Computed once per session by reading source — never executed, so a scan
    cannot itself trigger a write.
    """
    found: dict = dict(_STATE_VAR_SEED)
    root = Path(__file__).resolve().parents[1]
    try:
        for rel in _STATE_SCAN_ROOTS:
            base = root / rel
            files = [base] if base.is_file() else base.rglob("*.py")
            for f in files:
                try:
                    for var, default in _STATE_VAR_RE.findall(
                        f.read_text(encoding="utf-8", errors="ignore")
                    ):
                        found.setdefault(var, default)
                except (OSError, ValueError):
                    continue
    except Exception:  # noqa: BLE001 — discovery must never break collection
        pass
    return found


@pytest.fixture(autouse=True)
def _isolate_jarvis_state_vfs(tmp_path, monkeypatch, _discovered_state_vars):
    """Redirect every discovered state path into an ephemeral per-test VFS.

    PER TEST, matching the cadence of the singletons this state belongs to —
    a session-scoped dir was already tried for the CU journal above and was not
    enough, because durable state has to reset when its owner does.

    A test that sets one of these itself still wins: `monkeypatch` applies in
    fixture order and module-level fixtures run after this one, so suites that
    deliberately exercise real restarts keep working.
    """
    vfs = tmp_path / "_jarvis_vfs"
    for var, default in (_discovered_state_vars or {}).items():
        if os.environ.get(var):
            continue                     # an explicit outer setting wins
        monkeypatch.setenv(var, str(vfs / Path(default).name))
    yield


#: ``strict`` turns a live-state write into a test failure. Default is REPORT:
#: this tripwire is new, the suite is 3,308 files, and detonating every
#: pre-existing offender in one commit would bury the signal it exists to
#: raise. Flip to strict once the reported set is empty.
_TRIPWIRE_MODE = os.environ.get("JARVIS_TEST_STATE_TRIPWIRE", "report").lower()

_LIVE_STATE_WRITES: set = set()


def _install_live_state_audit_hook(repo_root: Path) -> None:
    """Record any write-open of the REAL `.jarvis/` from THIS process.

    An mtime diff cannot be used: the operator's daemon writes `.jarvis/`
    continuously, so a snapshot comparison would attribute daemon activity to
    whichever test happened to straddle it. An audit hook sees only this
    interpreter, so attribution is exact.
    """
    live = str((repo_root / ".jarvis").resolve())

    def _hook(event: str, args) -> None:
        if event != "open" or not args:
            return
        try:
            path, mode = args[0], (args[1] or "")
        except (IndexError, TypeError):
            return
        if not isinstance(path, (str, bytes)) or not mode:
            return
        if not any(m in str(mode) for m in ("w", "a", "x", "+")):
            return
        try:
            p = str(path)
        except Exception:  # noqa: BLE001
            return
        if p.startswith(live):
            _LIVE_STATE_WRITES.add(p)

    try:
        sys.addaudithook(_hook)
    except Exception:  # noqa: BLE001 — hook is diagnostic, never load-bearing
        pass


@pytest.fixture(autouse=True)
def _live_state_tripwire(request):
    """Fail (strict) or report (default) when a test writes real `.jarvis/`.

    The redirect above covers every path it could DISCOVER. This covers the
    rest — CWD-relative writers like
    ``Path(".jarvis/provider_liquidity.json")``, which no env var governs and
    which is exactly the shape that produced the 1969 residue.
    """
    before = len(_LIVE_STATE_WRITES)
    yield
    new = len(_LIVE_STATE_WRITES) - before
    if new and _TRIPWIRE_MODE == "strict":
        offenders = sorted(list(_LIVE_STATE_WRITES))[-new:]
        pytest.fail(
            "test wrote the operator's live .jarvis/ state: "
            + ", ".join(offenders)
        )


# ===========================================================================
# Git-aware collection boundary
# ===========================================================================
#
# THE ROOT CAUSE. Untracked OS artifacts bleed into the execution
# environment. Finder and iCloud resolve a name collision by appending
# " 2" — producing `tests/functional/vision 2/`, which the repository
# already refuses to track (`.gitignore: * [0-9].*`) but pytest happily
# walked, because pytest reads the FILESYSTEM and git reads the INDEX.
#
# The cost was never noise. A duplicate shares its BASENAME with the
# original, so importing `test_jarvis_vision` became ambiguous and pytest
# errored BOTH copies — the artifact deleted the real file's coverage.
#
# WHY NOT A STRING PATTERN. Two mechanisms were tried and both are
# structurally incapable:
#
#   * `norecursedirs` splits its value on WHITESPACE, so a pattern
#     containing a space is impossible to express — `* [0-9]` parsed as two
#     patterns, and the bare `*` matched every directory, collapsing
#     collection to zero tests.
#   * a regex hook encodes today's artifact spelling. " 2" is Finder's;
#     another tool picks another suffix and the filter silently stops
#     working, in the direction that looks like success.
#
# So the boundary is the INDEX ITSELF. "Is this file part of the
# repository" has exactly one authority, and it is git. That is exact
# rather than heuristic, immune to spaces and to any future artifact
# naming, and it needs no maintenance when the next tool invents a suffix.

_TRACKED_PATHS: "Optional[frozenset]" = None
_TRACKED_ROOT: "Optional[Path]" = None


def _tracked_files(root: "Path") -> "Optional[frozenset]":
    """Every path git tracks, as repo-relative POSIX strings. NEVER raises.

    ONE subprocess per session, cached: the alternative — `git check-ignore`
    per collected path — is thousands of forks across a 50k-test suite.
    NUL-delimited (`-z`) because a path containing a space is the entire
    reason this exists, and a newline-delimited reader would corrupt on the
    first quoted name.

    ``None`` means "git could not answer" — no repository, git absent, a
    timeout. The caller then ABSTAINS and collects everything, because a
    test runner that silently collects nothing because git was unavailable
    is a far worse failure than one that collects an artifact.
    """
    global _TRACKED_PATHS, _TRACKED_ROOT
    if _TRACKED_PATHS is not None and _TRACKED_ROOT == root:
        return _TRACKED_PATHS
    try:
        import subprocess
        out = subprocess.run(
            ["git", "ls-files", "-z"], cwd=str(root), timeout=60,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
        if out.returncode != 0:
            return None
        names = out.stdout.decode("utf-8", errors="replace").split("\0")
        tracked = frozenset(n for n in names if n)
    except Exception:  # noqa: BLE001 — collection must never die here
        return None
    if not tracked:
        return None
    _TRACKED_PATHS, _TRACKED_ROOT = tracked, root
    return tracked


def pytest_ignore_collect(collection_path=None, path=None, config=None):
    """Refuse anything git does not track. NEVER raises.

    Directories are kept when they CONTAIN a tracked file, so an untracked
    `__pycache__` sibling of real tests cannot prune the tests with it.

    Both parameter spellings are accepted: pytest renamed ``path`` to
    ``collection_path``, and hard-coding one name would disable this hook
    silently on the other version — failing in the direction that looks
    like everything is fine, which is the failure mode this whole hook
    exists to eliminate.
    """
    from pathlib import Path as _P

    target = collection_path if collection_path is not None else path
    if target is None or config is None:
        return None
    try:
        root = _P(str(config.rootpath))
        tracked = _tracked_files(root)
        if tracked is None:
            return None                      # git unavailable → abstain
        # `os.path.relpath` over REALPATHS, not `Path.relative_to`.
        #
        # This checkout exists twice — `JARVIS-AI-Agent.nosync` and a
        # symlinked `JARVIS-AI-Agent` — so `rootpath` and the collected path
        # can resolve through different links. `relative_to` raises on that
        # mismatch, the hook abstains, and every artifact is collected while
        # the code looks correct. `relpath` computes the relation without
        # requiring one to be a textual prefix of the other.
        import os as _os
        p = _P(str(target))
        rel = _os.path.relpath(_os.path.realpath(str(p)),
                               _os.path.realpath(str(root)))
        rel = _P(rel).as_posix()
        if rel.startswith(".."):
            return None                      # genuinely outside the repo
    except Exception:  # noqa: BLE001 — unresolvable
        return None
    if not rel or rel == ".":
        return None
    if p.is_dir():
        prefix = rel + "/"
        return None if any(t.startswith(prefix) for t in tracked) else True
    return None if rel in tracked else True


def _amputate_untracked(config, items):
    """Amputate untracked items after traversal. NEVER raises.

    `pytest_ignore_collect` above is the right hook and its logic is proven
    correct in isolation, but it was observed NOT to fire in this repo —
    plugin hierarchy or pluggy ordering preempts it. Rather than depend on a
    hook that may or may not be invoked, this runs at a point pytest
    guarantees: after the whole tree is walked, with the final item list in
    hand and mutable.

    Belt AND braces, deliberately. The two hooks are not redundant, they fail
    differently: `ignore_collect` prevents an untracked file from ever being
    IMPORTED (which is what stops a basename collision from erroring the real
    file), while this one guarantees nothing untracked EXECUTES even when the
    first hook is bypassed. Keeping only the second would leave the collision
    class open; keeping only the first leaves it dependent on hook wiring.

    Mutates `items` IN PLACE — pytest reads the same list object afterwards,
    so rebinding the name would be silently ignored.
    """
    if not items:
        return
    try:
        from pathlib import Path as _P
        import os as _os

        root = _P(str(config.rootpath))
        tracked = _tracked_files(root)
        if tracked is None:
            return                            # git unavailable → abstain
        real_root = _os.path.realpath(str(root))

        def _is_tracked(item) -> bool:
            try:
                raw = getattr(item, "path", None) or item.fspath
                rel = _os.path.relpath(_os.path.realpath(str(raw)), real_root)
                rel = _P(rel).as_posix()
                # Outside the repo entirely: not this hook's business. Keep,
                # so an intentional out-of-tree suite is never amputated.
                return True if rel.startswith("..") else rel in tracked
            except Exception:  # noqa: BLE001
                return True                   # unresolvable → keep
        keep = [i for i in items if _is_tracked(i)]
        dropped = len(items) - len(keep)
        if dropped:
            # MUTATE FIRST, report second. The reverse order cost this hook
            # its entire effect once already: `logger` was undefined in this
            # module, the NameError fired before the assignment, and the
            # blanket `except` below swallowed it — leaving a hook that ran,
            # failed, and reported nothing. Diagnostics must never sit on the
            # path of the thing they describe.
            items[:] = keep
            _logging.getLogger(__name__).warning(
                "[tests-conftest] dropped %d collected item(s) from untracked "
                "paths — OS artifacts are not part of the repository", dropped)
    except Exception:  # noqa: BLE001 — collection must never die here
        return


# ---------------------------------------------------------------------------
# The ONE collection hook
# ---------------------------------------------------------------------------


def pytest_collection_modifyitems(session, config, items):
    """The single `modifyitems` hook, composing every collection concern.

    THIS SHAPE IS THE FIX, NOT A STYLE CHOICE. This module previously defined
    `pytest_collection_modifyitems` TWICE at module scope. Python does not
    merge those -- the second binding replaced the first, so the path-marker
    pass (`unit`, `integration`, `vision`, `voice`, `slow`, ...) never ran and
    `-m unit` had been selecting nothing. Nothing reported it, because a hook
    that does not exist raises no error.

    A hook name that can be silently overwritten is a place where adding a
    feature deletes one. So there is exactly one definition, it delegates to
    named helpers, and the next concern is a fourth line here rather than a
    third `def` that eats the second.

    Order is deliberate:
      1. amputate untracked paths -- do not spend analysis on items that are
         about to be dropped;
      2. path markers -- pure string work on what survived;
      3. pty affinity -- the only pass that reads and parses files, so it runs
         on the smallest possible set.

    Each stage is independently fail-safe: a stage that raises is contained
    here, and the stages after it still run. Collection dying is worse than
    any one stage being skipped.
    """
    for stage, call in (
        ("amputate_untracked", lambda: _amputate_untracked(config, items)),
        ("path_markers", lambda: _apply_path_markers(items)),
        ("pty_affinity", lambda: _mark_pty_items(config, items)),
    ):
        try:
            call()
        except Exception:  # noqa: BLE001 — one bad stage must not kill collection
            _logging.getLogger(__name__).warning(
                "[tests-conftest] collection stage %r failed; continuing",
                stage, exc_info=True,
            )


def _mark_pty_items(config, items):
    """Tag terminal-driving tests so the gate can enforce them at setup.

    Delegates wholly to `tests.pty_gate`; this function owns no detection
    logic of its own, because a second opinion about what needs a terminal is
    how the first one drifts.
    """
    try:
        from tests.pty_gate import mark_pty_items
    except Exception:  # noqa: BLE001 — gate absent → no automarking, suite runs
        return
    marked = mark_pty_items(items)
    if marked:
        _logging.getLogger(__name__).debug(
            "[tests-conftest] pty affinity: marked %d item(s)", marked)


def pytest_runtest_setup(item):
    """Enforce the terminal requirement BEFORE the test body allocates one.

    Placement is the whole point. `test_region_layout_mount.py` calls
    `pty.openpty()` in its own body, so on a machine without `/dev/ptmx` it
    ERRORED with a raw `OSError` while its gated neighbours skipped cleanly --
    one cause, two verdicts, and only the skips were legible. Deciding at
    setup makes the verdict identical for every terminal-driving test in the
    repository regardless of how each one happens to allocate.
    """
    try:
        from tests.pty_gate import gate_item
    except Exception:  # noqa: BLE001
        return
    gate_item(item)

"""Hermetic Local Matrix Runner — the $0 in-memory A1 iteration harness.

Drives the REAL Boot-Hydration -> Scoped-Pytest -> Chaos-Detect -> Dispatch
pipeline locally, async, in milliseconds, with ZERO cloud calls (no GCP, no
IAP, no soak, no manual file mutation of the live repo). It replaces the
~50-min / ~$0.25 cloud run for harness-integration bugs.

What is REAL here (not mocked):

* A real ``git init`` fixture repo in a tmp dir, with a real pure-leaf source
  function + its real GREEN pytest, committed.
* A real chaos mutation (``a + b`` -> ``a - b``) that turns the fixture's test
  RED — equivalent to ``chaos_injector_ast``'s pure-leaf bug, planted directly
  for determinism against the tmp repo.
* The real ``WorkspaceResolver`` anchoring repo_root at the fixture's ``.git``.
* The real ``TestWatcher`` (``diff_working_tree`` / ``poll_once`` /
  ``run_pytest``) + real ``TestFailureSensor`` (``hydrate_on_boot`` /
  ``_resolve_scoped_targets`` / ``_on_fs_event``).
* The real ``TestRunner.resolve_affected_tests`` deterministic scoping.
* A real (``start()``-ed) ``TrinityEventBus`` + the real-shape ``fs.changed.*``
  payload from the ``EventSimulator`` (Component 2).

The ONLY bypassed boundary is the cloud / OS file-watch edge — never the
detection logic.

ACCEPTANCE BAR: this file GREEN == the chaos bug is detected via BOTH the
boot-hydration path AND the simulated ``fs.changed.*`` event, SCOPED to the
chaos file's own test (never the whole ``tests/`` suite, never "outside repo
root"), with the resolver anchoring to the fixture's real ``.git`` root.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any, Iterator, List

import pytest

from backend.core.ouroboros.governance.workspace_resolver import (
    clear_cache,
    resolve_repo_root,
)
from backend.core.ouroboros.governance.intent.test_watcher import TestWatcher
from backend.core.ouroboros.governance.intake.sensors.test_failure_sensor import (
    TestFailureSensor,
)
from backend.core.ouroboros.governance.test_runner import (
    BlockedPathError,
    LanguageRouter,
    PythonAdapter,
)
from tests.integration.event_simulator import EventSimulator

# Reuse the absolute-parity fixture (deep non-sandbox path + cwd mismatch +
# node allowlist) that reproduces the run-#13 post-apply scoped-verify bug.
# NO duplication: the same fixture drives BOTH the boot-hydration path (above)
# and the scoped-verify path (below), all local, async, ms, $0.
from tests.integration.test_scoped_verify_parity import parity_repo  # noqa: F401


# ---------------------------------------------------------------------------
# Fakes — only the router sink + the cloud boundary. Detection is REAL.
# ---------------------------------------------------------------------------


class _RecordingRouter:
    """Real router contract: ``ingest(envelope) -> "enqueued"`` and records."""

    def __init__(self) -> None:
        self.ingested: List[Any] = []

    async def ingest(self, envelope: Any) -> str:
        self.ingested.append(envelope)
        return "enqueued"


# ---------------------------------------------------------------------------
# Fixture repo — real git init, real green test, real chaos mutation
# ---------------------------------------------------------------------------

_SRC_GREEN = "def add(a, b):\n    return a + b\n"
# Equivalent of a chaos_injector_ast pure-leaf mutation: + -> - turns the
# green assertion RED while keeping the function pure (no imports/side effects).
_SRC_CHAOS = "def add(a, b):\n    return a - b\n"
_TEST_SRC = (
    "from pkg.foo import add\n"
    "\n"
    "\n"
    "def test_add():\n"
    "    assert add(2, 3) == 5\n"
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(root), check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def chaos_fixture_repo(tmp_path: Path) -> Iterator[dict]:
    """A real committed git repo whose test is GREEN, then chaos-mutated RED.

    Yields a dict with ``root`` (repo root), ``src`` (the chaos source file),
    ``rel`` (its repo-relative POSIX path), and ``test_rel`` (its test path).
    """
    root = tmp_path / "hermetic_fixture"
    (root / "pkg" / "tests").mkdir(parents=True)
    # ``conftest.py`` so ``from pkg.foo import add`` resolves with cwd=root.
    (root / "conftest.py").write_text("")
    (root / "pkg" / "__init__.py").write_text("")
    src = root / "pkg" / "foo.py"
    src.write_text(_SRC_GREEN)
    test_file = root / "pkg" / "tests" / "test_foo.py"
    test_file.write_text(_TEST_SRC)

    _git(root, "init", "-q")
    _git(root, "config", "user.email", "hermetic@matrix.local")
    _git(root, "config", "user.name", "Hermetic Matrix")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "green baseline")

    # Plant the chaos mutation (uncommitted -> git diff HEAD sees it).
    src.write_text(_SRC_CHAOS)

    yield {
        "root": root,
        "src": src,
        "rel": str(src.relative_to(root)).replace(os.sep, "/"),
        "test_rel": str(test_file.relative_to(root)).replace(os.sep, "/"),
    }


@pytest.fixture(autouse=True)
def _resolver_isolation() -> Iterator[None]:
    saved = os.environ.pop("JARVIS_REPO_PATH", None)
    clear_cache()
    try:
        yield
    finally:
        clear_cache()
        if saved is not None:
            os.environ["JARVIS_REPO_PATH"] = saved


def _make_watcher_and_sensor(repo_root: Path) -> "tuple[TestWatcher, TestFailureSensor, _RecordingRouter]":
    # Anchor explicitly at the fixture's .git root (the run-#12 fix in action).
    watcher = TestWatcher(
        repo="hermetic",
        test_dir="pkg/tests",
        repo_path=str(repo_root),
        poll_interval_s=0.0,
        pytest_timeout_s=30.0,
    )
    router = _RecordingRouter()
    sensor = TestFailureSensor("hermetic", router, test_watcher=watcher)
    return watcher, sensor, router


# ---------------------------------------------------------------------------
# (a) BOOT-HYDRATION PATH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_boot_hydration_scopes_and_detects_chaos(chaos_fixture_repo: dict) -> None:
    """git diff -> scoped test (NOT whole suite, NOT 'outside repo root') -> RED."""
    root = chaos_fixture_repo["root"]
    repo_root = resolve_repo_root(start=chaos_fixture_repo["src"])
    assert repo_root == root.resolve(), "resolver must anchor at the fixture .git"

    watcher, sensor, router = _make_watcher_and_sensor(repo_root)

    # 1. diff_working_tree sees the chaos file (cwd=repo_root, repo-relative).
    changed = await watcher.diff_working_tree()
    assert chaos_fixture_repo["rel"] in changed, (
        f"chaos file not seen by git diff: {changed}"
    )

    # 2. The changed source scopes to its OWN test — never the whole suite,
    #    never a BlockedPathError. THIS is the run-#12 path-bug regression.
    targets = await sensor._resolve_scoped_targets(chaos_fixture_repo["rel"])
    assert targets, "scoped targets empty (would fall back to whole suite)"
    target_names = {Path(t).name for t in targets}
    assert "test_foo.py" in target_names
    assert all(Path(t).name == "test_foo.py" for t in targets), (
        f"scoping leaked beyond the chaos test (whole-suite risk): {targets}"
    )

    # 3. The scoped pytest run sees RED.
    signals_first = await watcher.poll_once(target_paths=targets)
    # First failure -> streak 1 (not yet stable). The chaos IS detected as a
    # failure; stability needs a second consecutive RED (streak >= 2).
    assert watcher._failure_streak, "no failure recorded — chaos went undetected"

    # 4. hydrate_on_boot drives the SAME scoped path and, on this second
    #    consecutive RED, emits the stable TestFailure IntentSignal -> dispatch.
    ingested = await asyncio.wait_for(sensor.hydrate_on_boot(), timeout=30.0)
    assert ingested >= 1, "boot hydration emitted no stable signal"
    assert router.ingested, "TestFailure never dispatched to the router"
    env = router.ingested[0]
    # The dispatched envelope points at the chaos source file.
    target_files = getattr(env, "target_files", ())
    assert any("foo.py" in str(tf) for tf in target_files), (
        f"dispatched envelope not scoped to the chaos file: {target_files}"
    )


# ---------------------------------------------------------------------------
# (b) EVENT PATH — real fs.changed.* through the real TrinityEventBus
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_event_path_scopes_and_detects_chaos(chaos_fixture_repo: dict) -> None:
    """EventSimulator injects a real-shape fs.changed.* -> same scoped detect."""
    from backend.core.trinity_event_bus import TrinityEventBus, RepoType

    root = chaos_fixture_repo["root"]
    repo_root = resolve_repo_root(start=chaos_fixture_repo["src"])
    watcher, sensor, router = _make_watcher_and_sensor(repo_root)

    # Prime one RED so the event-path run produces a STABLE (streak>=2) signal.
    targets = await sensor._resolve_scoped_targets(chaos_fixture_repo["rel"])
    await watcher.poll_once(target_paths=targets)

    # Real, started, in-memory bus + real subscription.
    os.environ["JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED"] = "true"
    bus = TrinityEventBus(local_repo=RepoType.JARVIS)
    await bus.start()
    try:
        await sensor.subscribe_to_bus(bus)  # real handler wired to fs.changed.*

        sim = EventSimulator(bus, repo_root=repo_root)
        # Inject the EXACT real-shape fs.changed.* for the chaos file.
        topic_payload_proof = await _assert_real_payload_shape(
            sim, chaos_fixture_repo
        )
        assert topic_payload_proof  # payload shape validated below

        await sim.inject_change(chaos_fixture_repo["src"])

        # The real subscription debounces 2s then runs the scoped pytest.
        # Poll the router (no sync sleep) until the dispatch lands.
        await _wait_until(lambda: bool(router.ingested), timeout=20.0)
    finally:
        os.environ.pop("JARVIS_TEST_FAILURE_FS_EVENTS_ENABLED", None)
        await bus.stop()

    assert router.ingested, "event path did not dispatch the chaos TestFailure"
    env = router.ingested[0]
    target_files = getattr(env, "target_files", ())
    assert any("foo.py" in str(tf) for tf in target_files)


async def _assert_real_payload_shape(sim: EventSimulator, fx: dict) -> bool:
    """Prove the simulator builds the bridge's exact field set + values."""
    from tests.integration.event_simulator import build_fs_changed_payload

    topic, payload = build_fs_changed_payload(
        abs_path=fx["src"], repo_root=fx["root"].resolve(),
    )
    assert topic == "fs.changed.modified"
    # Field set must match fs_event_bridge._on_file_event exactly.
    assert set(payload) == {
        "path", "relative_path", "extension", "checksum",
        "is_test_file", "is_config_file", "is_directory", "timestamp",
    }
    assert payload["relative_path"] == fx["rel"]
    assert payload["extension"] == ".py"
    assert payload["is_test_file"] is False  # pkg/foo.py is a source file
    assert payload["is_config_file"] is False
    assert payload["is_directory"] is False
    assert isinstance(payload["checksum"], str) and payload["checksum"]
    return True


# ---------------------------------------------------------------------------
# Async helpers — no sync sleeps
# ---------------------------------------------------------------------------


async def _wait_until(pred, *, timeout: float, interval: float = 0.05) -> None:
    """Await until *pred()* is truthy or *timeout* elapses (fully async)."""
    async def _spin() -> None:
        while not pred():
            await asyncio.sleep(interval)
    try:
        await asyncio.wait_for(_spin(), timeout=timeout)
    except asyncio.TimeoutError:
        pass  # caller asserts on the predicate; surfaces a clear failure


# ---------------------------------------------------------------------------
# Speed + zero-cloud guard
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_anchors_no_cloud_no_hardcode(chaos_fixture_repo: dict) -> None:
    """The whole matrix anchors at .git with no literal paths + no cloud SDK."""
    repo_root = resolve_repo_root(start=chaos_fixture_repo["src"])
    assert (repo_root / ".git").exists()

    # Structural: none of the modules the matrix exercises import a cloud SDK.
    import ast

    for mod_rel in (
        Path("backend/core/ouroboros/governance/workspace_resolver.py"),
        Path("backend/core/ouroboros/governance/intent/test_watcher.py"),
        Path("backend/core/ouroboros/governance/intake/sensors/test_failure_sensor.py"),
        Path("tests/integration/event_simulator.py"),
    ):
        text = (resolve_repo_root() / mod_rel).read_text()
        tree = ast.parse(text)
        imported: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported += [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.append(node.module)
        for name in imported:
            assert not name.startswith("google"), (
                f"{mod_rel} pulls in a cloud SDK ({name}) — not hermetic"
            )


# ---------------------------------------------------------------------------
# (c) POST-APPLY SCOPED-VERIFY PATH — the run-#13 fidelity-gap closure
# ---------------------------------------------------------------------------
#
# The boot-hydration path (a) anchors via the resolver already. The scoped-
# verify path (the orchestrator's LanguageRouter / PythonAdapter post-APPLY)
# was a SEPARATE root-resolution site that the clean-tmpdir harness could not
# reproduce (its /tmp shape is whitelisted by _ALLOWED_SANDBOX_PREFIXES). The
# ``parity_repo`` fixture forces the node's exact mismatch (deep non-sandbox
# repo + cwd != repo_root). This section proves: (1) the PRE-FIX disjoint root
# reproduces the run-#13 "outside repo root" rejection, and (2) routing through
# the authoritative ``resolve_repo_root`` scopes + detects the chaos test.


@pytest.mark.asyncio
async def test_scoped_verify_buggy_root_reproduces_run13(parity_repo: dict) -> None:
    """Hermetic Matrix now exercises scoped-verify: the buggy (cwd-style,
    non-.git) root hits the SAME 'outside repo root' security gate as run #13."""
    src = parity_repo["src"]
    buggy_root = parity_repo["buggy_root"]
    router = LanguageRouter(
        repo_root=buggy_root,
        adapters={"python": PythonAdapter(repo_root=buggy_root)},
    )
    with pytest.raises(BlockedPathError, match="outside repo root"):
        await asyncio.wait_for(
            router.run(
                changed_files=(src,), sandbox_dir=None,
                timeout_budget_s=30.0, op_id="hermetic-scoped-buggy",
            ),
            timeout=35.0,
        )


@pytest.mark.asyncio
async def test_scoped_verify_resolved_root_scopes_and_detects(
    parity_repo: dict,
) -> None:
    """Hermetic Matrix scoped-verify under the FIX: resolve_repo_root anchors
    at the parity .git -> the chaos test is scoped + RED, no rejection, even
    with cwd != repo_root (the live node condition)."""
    src = parity_repo["src"]
    root = parity_repo["root"]
    fixed_root = resolve_repo_root(start=src)
    assert fixed_root == root.resolve()

    router = LanguageRouter(
        repo_root=fixed_root,
        adapters={"python": PythonAdapter(repo_root=fixed_root)},
    )
    multi = await asyncio.wait_for(
        router.run(
            changed_files=(src,), sandbox_dir=None,
            timeout_budget_s=30.0, op_id="hermetic-scoped-fixed",
        ),
        timeout=35.0,
    )
    total = sum(ar.test_result.total for ar in multi.adapter_results)
    assert total >= 1, "scoped-verify discovered no tests (degradation present)"
    assert multi.passed is False, "chaos bug not detected by scoped-verify"

"""cross_repo_first_surgery.py -- "First Cross-Repo Surgery" chaos demonstration.

A CONTROLLED-SCENARIO harness (NOT the full autonomous GLS loop) that drives the
REAL Sovereign Cross-Repo Mutator machinery through a chaos-injected mutation to
prove the FRACTURE -> rollback path. It exercises the real components on a temp
two-repo fixture (jarvis + reactor) so an operator can WATCH the organism heal:

    arming handshake  (real cross_repo_master_flag.run_arming_handshake -- Docker)
        |
    blast-radius trace + visualizer  (real cross_repo_blast_context.trace_cross_repo_blast
        |                              + blast_radius_visualizer.render_blast_tree)
        |
    chaos candidate applied across both repos  (real SagaApplyStrategy.execute -- writes
        |                                        the broken reactor file, preimage captured)
        |
    air-gapped sandbox gate -> FRACTURE  (real trinity_integration_gate.run_trinity_sandbox_gate
        |                                  with an injected runner whose handshake FAILS)
        |
    compensating rollback  (real SagaApplyStrategy.compensate_after_verify_failure ->
        |                    restores BOTH repos from preimage)
        |
    VERIFY: both files byte-identical to the pre-surgery snapshots.

WHAT IS REAL vs SIMULATED (be honest -- see also the report artifact):
  * REAL: SagaApplyStrategy multi-repo apply + compensating rollback (the actual
    preimage-restore + git-add/restore logic, on real git repos in the temp dir).
  * REAL: trinity_integration_gate.run_trinity_sandbox_gate decision logic, the
    air-gap assertion, the FRACTURE verdict, and the
    `[SOVEREIGN YIELD: CROSS-REPO FRACTURE]` emission.
  * REAL: cross_repo_blast_context.trace_cross_repo_blast + render_blast_tree --
    the Body file is genuinely traced as the dependent of the Nerves symbol.
  * REAL: cross_repo_master_flag.run_arming_handshake (real `docker info` probe).
  * SIMULATED (honestly, by necessity -- no real 3-repo docker-compose exists on
    this host): the air-gapped Docker network + the broken-handshake detection are
    provided by an injected ``_FractureRunner`` whose `health_handshake` returns a
    non-zero rc -- EXACTLY as a real broken cross-repo contract would. The runner is
    the injection seam the gate is DESIGNED for (DockerRunner protocol).
  * SIMULATED: the Oracle blast graph is a small controlled fixture
    (``_FixtureOracle``) returning a REAL ``BlastRadius`` whose dependent is the
    real jarvis ``metrics_caller`` of the reactor symbol. The graph data is
    fixture; the trace/visualizer machinery consuming it is real.

Gated: does NOTHING unless ``JARVIS_CHAOS_INJECTOR_ENABLED`` is explicitly truthy.
Fail-soft: any error is caught, reported, and the temp dir is always torn down.

Run:  JARVIS_CHAOS_INJECTOR_ENABLED=1 python3 scripts/cross_repo_first_surgery.py --run
"""
from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# --------------------------------------------------------------------------- #
# Repo import bootstrap (standalone script -> ensure repo root on sys.path)
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from backend.core.ouroboros.oracle import BlastRadius, NodeID, NodeType  # noqa: E402
from backend.core.ouroboros.governance.multi_repo.registry import (  # noqa: E402
    RepoConfig,
    RepoRegistry,
)
from backend.core.ouroboros.governance.multi_repo.cross_repo_blast_context import (  # noqa: E402
    trace_cross_repo_blast,
)
from backend.core.ouroboros.governance.multi_repo.blast_radius_visualizer import (  # noqa: E402
    render_blast_tree,
)
from backend.core.ouroboros.governance.cross_repo_master_flag import (  # noqa: E402
    ArmingHandshake,
    run_arming_handshake,
)
from backend.core.ouroboros.governance.op_context import OperationContext  # noqa: E402
from backend.core.ouroboros.governance.saga.saga_apply_strategy import (  # noqa: E402
    SagaApplyStrategy,
)
from backend.core.ouroboros.governance.saga.saga_types import (  # noqa: E402
    FileOp,
    PatchedFile,
    RepoPatch,
    SagaTerminalState,
)
from backend.core.ouroboros.governance.saga.trinity_integration_gate import (  # noqa: E402
    RunResult,
    SandboxVerdict,
    run_trinity_sandbox_gate,
)


_ENV_CHAOS = "JARVIS_CHAOS_INJECTOR_ENABLED"
_TRUTHY = {"1", "true", "yes", "on"}

# The cross-repo dependency the surgery exercises:
#   reactor/telemetry_adapter.py :: TelemetryAdapter.emit  (the mutated Nerves symbol)
#       <- jarvis/metrics_caller.py :: record_metric        (the Body dependent)
_REACTOR_FILE = "telemetry_adapter.py"
_JARVIS_FILE = "metrics_caller.py"
_TARGET_SYMBOL = "emit"
_TARGET_CLASS = "TelemetryAdapter"

# --------------------------------------------------------------------------- #
# The fixture source -- a utility-grade telemetry adapter + a real Body caller.
# --------------------------------------------------------------------------- #
_REACTOR_ORIGINAL = '''"""reactor/telemetry_adapter.py -- utility-grade telemetry adapter (Nerves)."""
from __future__ import annotations


class TelemetryAdapter:
    """Emits a single metric reading as a structured record."""

    def emit(self, metric: str, value: float) -> dict:
        """Return a structured telemetry record for `metric` = `value`."""
        return {"metric": metric, "value": float(value), "unit": "raw"}
'''

# CHAOS: the method is renamed AND its signature drops `value`. The file still
# COMPILES (ast.parse OK), so the earlier compilation gate passes -- but the
# jarvis caller's `.emit("x", 1.0)` would now break the cross-repo handshake.
# This is the right chaos to exercise the SANDBOX HANDSHAKE (a pure syntax error
# would trip the compilation gate, never reaching G2).
_REACTOR_CHAOS = '''"""reactor/telemetry_adapter.py -- CHAOS MUTATION (contract-breaking)."""
from __future__ import annotations


class TelemetryAdapter:
    """Emits a single metric reading as a structured record."""

    def emit_metric(self, metric: str) -> dict:
        """CHAOS: renamed emit->emit_metric and dropped the `value` parameter.

        Compiles fine, but breaks the Body caller's `emit("x", 1.0)` contract --
        the cross-repo handshake fractures.
        """
        return {"metric": metric, "unit": "raw"}
'''

_JARVIS_ORIGINAL = '''"""jarvis/metrics_caller.py -- Body code that calls the Nerves telemetry adapter."""
from __future__ import annotations

from telemetry_adapter import TelemetryAdapter


def record_metric() -> dict:
    """Record one metric via the reactor TelemetryAdapter (Body->Nerves call)."""
    adapter = TelemetryAdapter()
    return adapter.emit("x", 1.0)
'''


# --------------------------------------------------------------------------- #
# Controlled Oracle fixture -- returns a REAL BlastRadius whose dependent is the
# real jarvis caller of the reactor symbol. The graph DATA is fixture; the
# trace/visualizer machinery that consumes it is the real production code.
# --------------------------------------------------------------------------- #
class _FixtureOracle:
    """Minimal Oracle stand-in exposing the real ``compute_blast_radius`` shape.

    Returns a real :class:`BlastRadius` (oracle.py) so the production
    ``trace_cross_repo_blast`` consumes a genuine report -- not a mock.
    """

    def __init__(self, dependent: NodeID) -> None:
        self._dependent = dependent

    def compute_blast_radius(self, node_id: NodeID, max_depth: int = 3) -> BlastRadius:
        return BlastRadius(
            source_node=node_id,
            directly_affected={self._dependent},
            transitively_affected=set(),
            broken_imports=[],
            broken_calls=[],
            risk_level="high",
        )


# --------------------------------------------------------------------------- #
# Injected sandbox runner -- simulates the air-gapped 3-repo Docker network and
# detects the broken cross-repo handshake. This is the seam the gate is DESIGNED
# for (DockerRunner protocol). The handshake FAILS for the chaos candidate,
# exactly as a real broken-handshake would -> the gate returns fracture=True.
# --------------------------------------------------------------------------- #
class _FractureRunner:
    """Fake DockerRunner: air-gap asserts cleanly, handshake FRACTURES.

    * ``compose_up`` / ``compose_down`` -> rc 0 (containers "spin"/"teardown").
    * ``inspect_network`` -> "true" (network is internal: air-gap holds).
    * ``probe_provider_host`` -> rc 7 (UNREACHABLE: the required air-gap outcome).
    * ``health_handshake`` -> rc 11 (the Body<->Nerves handshake is BROKEN by the
      contract-breaking chaos mutation) -> FRACTURE.
    """

    def __init__(self) -> None:
        self.compose_down_calls = 0

    async def compose_up(self, compose_path: str) -> RunResult:
        return RunResult(0, stdout="Created 4 containers", stderr="")

    async def compose_down(self, compose_path: str) -> RunResult:
        self.compose_down_calls += 1
        return RunResult(0, stdout="Removed", stderr="")

    async def inspect_network(self, network: str) -> RunResult:
        return RunResult(0, stdout="true\n", stderr="")

    async def probe_provider_host(self, service: str, host: str) -> RunResult:
        # Non-zero rc == host UNREACHABLE == air-gap holds (the required outcome).
        return RunResult(7, stdout="", stderr="connection refused (air-gapped)")

    async def health_handshake(self, service: str) -> RunResult:
        # rc != 0 -> all_green is False -> the Trinity handshake FRACTURED.
        return RunResult(
            11,
            stdout="",
            stderr="trinity handshake failed: jarvis.metrics_caller.record_metric "
            "called TelemetryAdapter.emit('x', 1.0) but reactor now exposes "
            "emit_metric(metric) -- contract broken",
        )


# --------------------------------------------------------------------------- #
# Fixture construction
# --------------------------------------------------------------------------- #
@dataclass
class _Fixture:
    root: Path
    jarvis_root: Path
    reactor_root: Path
    jarvis_original: bytes
    reactor_original: bytes


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "surgery",
            "GIT_AUTHOR_EMAIL": "surgery@jarvis.local",
            "GIT_COMMITTER_NAME": "surgery",
            "GIT_COMMITTER_EMAIL": "surgery@jarvis.local",
        },
    )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", "fixture: initial", "--allow-empty")


def build_fixture(base: Optional[Path] = None) -> _Fixture:
    """Create the temp two-repo fixture (jarvis + reactor) with the real graph."""
    root = base or Path(tempfile.mkdtemp(prefix="cross_repo_surgery_"))
    jarvis_root = root / "jarvis"
    reactor_root = root / "reactor"
    jarvis_root.mkdir(parents=True, exist_ok=True)
    reactor_root.mkdir(parents=True, exist_ok=True)

    reactor_original = _REACTOR_ORIGINAL.encode("utf-8")
    jarvis_original = _JARVIS_ORIGINAL.encode("utf-8")
    (reactor_root / _REACTOR_FILE).write_bytes(reactor_original)
    (jarvis_root / _JARVIS_FILE).write_bytes(jarvis_original)

    _init_repo(jarvis_root)
    _init_repo(reactor_root)

    return _Fixture(
        root=root,
        jarvis_root=jarvis_root,
        reactor_root=reactor_root,
        jarvis_original=jarvis_original,
        reactor_original=reactor_original,
    )


def make_fixture_oracle() -> _FixtureOracle:
    """Build the controlled Oracle whose dependent is the real jarvis caller."""
    dependent = NodeID(
        repo="jarvis",
        file_path=_JARVIS_FILE,
        name="record_metric",
        node_type=NodeType.FUNCTION,
    )
    return _FixtureOracle(dependent)


def make_target_node() -> NodeID:
    """The mutated Nerves symbol: reactor::TelemetryAdapter.emit."""
    return NodeID(
        repo="reactor",
        file_path=_REACTOR_FILE,
        name=f"{_TARGET_CLASS}.{_TARGET_SYMBOL}",
        node_type=NodeType.METHOD,
    )


def make_registry(fixture: _Fixture) -> RepoRegistry:
    """RepoRegistry pointed at the fixture's two repos."""
    return RepoRegistry(
        configs=(
            RepoConfig(name="jarvis", local_path=fixture.jarvis_root, canary_slices=("tests/",)),
            RepoConfig(name="reactor", local_path=fixture.reactor_root, canary_slices=("tests/",)),
        )
    )


def make_chaos_patch_map(fixture: _Fixture) -> Dict[str, RepoPatch]:
    """The cross-repo patch: a contract-breaking MODIFY of the reactor file.

    Preimage = the original reactor bytes (so the REAL compensating rollback can
    restore it). The jarvis file is unchanged by the chaos mutation -- the break
    is that jarvis's caller now references a contract the mutated reactor dropped.
    """
    reactor_patch = RepoPatch(
        repo="reactor",
        files=(
            PatchedFile(
                path=_REACTOR_FILE,
                op=FileOp.MODIFY,
                preimage=fixture.reactor_original,
            ),
        ),
        new_content=((_REACTOR_FILE, _REACTOR_CHAOS.encode("utf-8")),),
    )
    return {"reactor": reactor_patch}


def make_cross_repo_ctx(fixture: _Fixture) -> OperationContext:
    """A real cross-repo OperationContext (repo_scope spans jarvis + reactor)."""
    return OperationContext.create(
        target_files=(f"reactor/{_REACTOR_FILE}",),
        description="first cross-repo surgery: chaos mutation of TelemetryAdapter.emit",
        op_id="surgery-001",
        primary_repo="reactor",
        repo_scope=("jarvis", "reactor"),
        # reactor must apply before jarvis would (dependency edge: jarvis depends on reactor)
        dependency_edges=(("jarvis", "reactor"),),
    )


def render_blast_tree_if_available(blast) -> str:
    """Thin pass-through to the real visualizer (kept for test ergonomics)."""
    return render_blast_tree(blast)


def make_saga_strategy(fixture: _Fixture) -> SagaApplyStrategy:
    """The REAL SagaApplyStrategy pointed at the fixture repo roots.

    branch_isolation=False -> legacy direct-to-HEAD apply with preimage
    compensation (the path whose rollback we verify byte-for-byte). ledger=None
    -> sub-event emits are skipped (best-effort, fail-soft by design).
    """
    return SagaApplyStrategy(
        repo_roots={"jarvis": fixture.jarvis_root, "reactor": fixture.reactor_root},
        ledger=None,
        branch_isolation=False,
    )


# --------------------------------------------------------------------------- #
# The surgery (async driver)
# --------------------------------------------------------------------------- #
@dataclass
class SurgeryResult:
    handshake: ArmingHandshake
    blast_tree: str
    blast_dependent_count: int
    chaos_applied: bool
    sandbox_verdict: SandboxVerdict
    rollback_verified: bool
    reactor_restored: bool
    jarvis_restored: bool
    narrative: List[str]


def _say(narrative: List[str], line: str) -> None:
    narrative.append(line)
    print(line, flush=True)


async def run_surgery(*, base: Optional[Path] = None) -> SurgeryResult:
    """Drive the full controlled cross-repo surgery scenario.

    Builds the fixture, runs the real components, and VERIFIES the rollback
    restored both repos byte-for-byte. Tears down the temp dir in `finally`.
    """
    narrative: List[str] = []
    fixture = build_fixture(base)
    runner = _FractureRunner()
    try:
        _say(narrative, "=" * 72)
        _say(narrative, "  FIRST CROSS-REPO SURGERY -- chaos -> FRACTURE -> rollback")
        _say(narrative, "=" * 72)
        _say(narrative, f"fixture: {fixture.root}")
        _say(narrative, f"  reactor (Nerves): {_REACTOR_FILE} :: {_TARGET_CLASS}.{_TARGET_SYMBOL}")
        _say(narrative, f"  jarvis  (Body):   {_JARVIS_FILE} :: record_metric -> emit('x', 1.0)")
        _say(narrative, "")

        # --- 1. ARMING HANDSHAKE (real Docker probe) -----------------------
        _say(narrative, "[1] ARMING HANDSHAKE (real cross_repo_master_flag.run_arming_handshake)")
        # Force the flag truthy for the demo regardless of the master flag's
        # process state (we still run the REAL docker probe underneath).
        prev_flag = os.environ.get("JARVIS_CROSS_REPO_MUTATION_ENABLED")
        os.environ["JARVIS_CROSS_REPO_MUTATION_ENABLED"] = "1"
        try:
            handshake = await run_arming_handshake()
        finally:
            if prev_flag is None:
                os.environ.pop("JARVIS_CROSS_REPO_MUTATION_ENABLED", None)
            else:
                os.environ["JARVIS_CROSS_REPO_MUTATION_ENABLED"] = prev_flag
        _say(narrative, f"    armed={handshake.armed} docker_alive={handshake.docker_alive} "
                        f"sinkhole_configurable={handshake.sinkhole_configurable}")
        _say(narrative, f"    reason: {handshake.reason}")
        if not handshake.armed:
            _say(narrative, "    -> handshake degraded; proceeding in FORCED-DEMO mode "
                            "(scenario still exercises the real apply/gate/rollback).")
        _say(narrative, "")

        # --- 2. BLAST-RADIUS TRACE + VISUALIZER (real) ---------------------
        _say(narrative, "[2] BLAST-RADIUS TRACE (real trace_cross_repo_blast + render_blast_tree)")
        oracle = make_fixture_oracle()
        registry = make_registry(fixture)
        target = make_target_node()
        blast = await trace_cross_repo_blast(
            target_node_id=target, oracle=oracle, registry=registry
        )
        blast_tree = render_blast_tree(blast)
        _say(narrative, "")
        for line in blast_tree.splitlines():
            _say(narrative, "    " + line)
        _say(narrative, "")
        _say(narrative, f"    -> {blast.total_dependents} Body dependent(s) traced for the "
                        "Nerves mutation.")
        _say(narrative, "")

        # --- 3. CHAOS CANDIDATE applied across repos (real SagaApplyStrategy) -
        _say(narrative, "[3] CHAOS CANDIDATE (real SagaApplyStrategy.execute)")
        _say(narrative, "    Mutation: TelemetryAdapter.emit(metric, value) -> "
                        "emit_metric(metric)")
        _say(narrative, "    (compiles -- ast.parse OK -- but BREAKS the Body caller's contract;")
        _say(narrative, "     a pure syntax error would trip the compilation gate, never reaching G2)")
        ctx = make_cross_repo_ctx(fixture)
        strategy = make_saga_strategy(fixture)
        patch_map = make_chaos_patch_map(fixture)
        apply_result = await strategy.execute(ctx, patch_map)
        reactor_on_disk = (fixture.reactor_root / _REACTOR_FILE).read_bytes()
        chaos_applied = (
            apply_result.terminal_state == SagaTerminalState.SAGA_APPLY_COMPLETED
            and b"emit_metric" in reactor_on_disk
        )
        _say(narrative, f"    saga terminal_state={apply_result.terminal_state.value} "
                        f"chaos_on_disk={b'emit_metric' in reactor_on_disk}")
        _say(narrative, "")

        # --- 4. AIR-GAPPED SANDBOX GATE -> FRACTURE (real gate) -------------
        _say(narrative, "[4] AIR-GAPPED SANDBOX GATE (real run_trinity_sandbox_gate)")
        _say(narrative, "    injected runner: air-gap holds, Trinity handshake BROKEN by the chaos")
        prev_gate = os.environ.get("JARVIS_TRINITY_SANDBOX_GATE_ENABLED")
        os.environ["JARVIS_TRINITY_SANDBOX_GATE_ENABLED"] = "true"
        try:
            sandbox_verdict = await run_trinity_sandbox_gate(
                candidate_root=str(fixture.root),
                op_id=ctx.op_id,
                runner=runner,
                # Build the air-gap overlay from a tiny in-fixture base compose so
                # the real render path runs (no real 3-repo compose on this host).
                base_compose_path=_write_base_compose(fixture.root),
                services=("jarvis", "reactor"),
            )
        finally:
            if prev_gate is None:
                os.environ.pop("JARVIS_TRINITY_SANDBOX_GATE_ENABLED", None)
            else:
                os.environ["JARVIS_TRINITY_SANDBOX_GATE_ENABLED"] = prev_gate
        _say(narrative, f"    verdict: passed={sandbox_verdict.passed} "
                        f"fracture={sandbox_verdict.fracture} "
                        f"air_gapped={sandbox_verdict.air_gapped} "
                        f"handshake_ok={sandbox_verdict.handshake_ok}")
        _say(narrative, f"    reason: {sandbox_verdict.reason}")
        if sandbox_verdict.fracture:
            _say(narrative, "    [SOVEREIGN YIELD: CROSS-REPO FRACTURE] emitted "
                            "(see WARNING log) -> abort + rollback")
        _say(narrative, "")

        # --- 5. COMPENSATING ROLLBACK (real saga rollback) -----------------
        _say(narrative, "[5] COMPENSATING ROLLBACK (real SagaApplyStrategy.compensate_after_verify_failure)")
        reactor_restored = False
        jarvis_restored = False
        rollback_verified = False
        if sandbox_verdict.fracture and chaos_applied:
            all_ok = await strategy.compensate_after_verify_failure(
                saga_result=apply_result,
                patch_map=patch_map,
                op_id=ctx.op_id,
                reason_code="cross_repo_fracture",
            )
            reactor_now = (fixture.reactor_root / _REACTOR_FILE).read_bytes()
            jarvis_now = (fixture.jarvis_root / _JARVIS_FILE).read_bytes()
            reactor_restored = reactor_now == fixture.reactor_original
            jarvis_restored = jarvis_now == fixture.jarvis_original
            rollback_verified = bool(all_ok) and reactor_restored and jarvis_restored
        _say(narrative, f"    compensation reactor_restored={reactor_restored} "
                        f"jarvis_restored={jarvis_restored}")
        _say(narrative, "")

        # --- 6. VERDICT ----------------------------------------------------
        _say(narrative, "=" * 72)
        if rollback_verified:
            _say(narrative, "  ROLLBACK VERIFIED: reactor + jarvis restored to pre-surgery "
                            "state, organism intact.")
            _say(narrative, "  VERDICT: PASS")
        else:
            _say(narrative, "  ROLLBACK FAILED: organism NOT fully restored.")
            _say(narrative, "  VERDICT: FAIL")
        _say(narrative, "=" * 72)

        return SurgeryResult(
            handshake=handshake,
            blast_tree=blast_tree,
            blast_dependent_count=blast.total_dependents,
            chaos_applied=chaos_applied,
            sandbox_verdict=sandbox_verdict,
            rollback_verified=rollback_verified,
            reactor_restored=reactor_restored,
            jarvis_restored=jarvis_restored,
            narrative=narrative,
        )
    finally:
        try:
            shutil.rmtree(fixture.root, ignore_errors=True)
        except Exception:
            pass


def _write_base_compose(root: Path) -> str:
    """Write a tiny base docker-compose so the gate's real overlay render runs.

    The gate derives an air-gapped overlay from a base compose; on this host
    there is no real 3-repo compose, so we provide a minimal one to drive the
    REAL render path. The injected runner never actually spins these.
    """
    path = root / "docker-compose.soak.yml"
    path.write_text(
        "services:\n"
        "  jarvis:\n"
        "    image: python:3.11-slim\n"
        "  reactor:\n"
        "    image: python:3.11-slim\n",
        encoding="utf-8",
    )
    return str(path)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _chaos_enabled() -> bool:
    raw = os.environ.get(_ENV_CHAOS, "").strip().lower()
    return raw in _TRUTHY


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--run",
        action="store_true",
        help="Execute the surgery scenario (requires JARVIS_CHAOS_INJECTOR_ENABLED).",
    )
    args = parser.parse_args(argv)

    if not _chaos_enabled():
        print(
            f"[cross_repo_first_surgery] REFUSING: {_ENV_CHAOS} is not enabled. "
            f"Set {_ENV_CHAOS}=1 to run the chaos surgery.",
            file=sys.stderr,
        )
        return 2

    if not args.run:
        print("[cross_repo_first_surgery] pass --run to execute the surgery.", file=sys.stderr)
        return 2

    try:
        result = asyncio.run(run_surgery())
    except Exception as exc:  # noqa: BLE001 -- fail-soft top-level
        print(f"[cross_repo_first_surgery] surgery raised: {exc}", file=sys.stderr)
        return 1

    return 0 if result.rollback_verified else 1


if __name__ == "__main__":
    raise SystemExit(main())

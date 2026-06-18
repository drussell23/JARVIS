"""Sovereign Chaos Injection & Verification Fabric.

An automated, sandboxed, cross-repo fault-induction harness that drives the full Repair-Context-Bridge
→ Cross-Repo-Scope-Promoter → StructuralValidationGate → multi-file pipeline **end-to-end under zero
production risk**, satisfying the headless-execution evidence gap *deterministically* — i.e. without a
live model and without touching canonical source.

Honest scope (load-bearing): a deterministic fabric proves the **plumbing** — fault ingestion → fault
coordinates → cross-boundary scope elevation → structural delta verdict → topologically-ordered
multi-file apply — by injecting a *known* candidate fix where a live model would generate one. It does
NOT exercise live model-driven GENERATE, nor the multi-iteration signals (divergence escape, progress
v1.1) that only emerge from real L2 loops. So it is strong **integration/regression evidence** for the
deterministic subsystems, not a substitute for the live-model graduation soak. The fabric harvests
telemetry and reports exactly which subsystems fired; graduation decisions stay with that honest line.

Composition, not duplication: drives the shipped `repair_traceback`, `cross_repo_scope_promoter`,
`structural_validation_gate`, and `repair_multifile` primitives against a controlled in-memory unified
graph fixture (jarvis↔reactor cross-boundary edge) + an ephemeral file mirror.

Phase 1 — Virtual workspace mirroring (`ChaosWorkspace`): ephemeral scratchpad mirror of target
cross-boundary files across jarvis + reactor; canonical source is never written.
Phase 2 — Structural fault induction (`FaultSeeder`): deterministic interface-contract-break and
call-chain-severance into the mirror.
Phase 3 — Closed-loop convergence (`ChaosSimulationFabric.run`): ingest fault → promote scope →
structural-gate a candidate fix → topo multi-file apply in the mirror → harvest convergence telemetry.
"""
from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


# --------------------------------------------------------------------------- report
@dataclass
class ChaosReport:
    stages: Dict[str, bool] = field(default_factory=dict)
    detail: Dict[str, Any] = field(default_factory=dict)
    converged: bool = False

    def mark(self, stage: str, ok: bool, **detail: Any) -> None:
        self.stages[stage] = ok
        if detail:
            self.detail[stage] = detail

    def render(self) -> str:
        lines = ["## CHAOS SIMULATION REPORT"]
        for s, ok in self.stages.items():
            lines.append(f"  [{'✅' if ok else '⛔'}] {s}")
        lines.append(f"  CONVERGED: {self.converged}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- Phase 1: mirror
class ChaosWorkspace:
    """Ephemeral scratchpad mirror of cross-boundary files. Canonical source is never touched."""

    def __init__(self, base: Optional[Path] = None) -> None:
        self._base = Path(base) if base else Path(tempfile.mkdtemp(prefix="chaos_fabric_"))
        self.files: Dict[str, Path] = {}   # "repo:relpath" -> mirrored absolute path

    @property
    def root(self) -> Path:
        return self._base

    def mirror(self, repo: str, rel_path: str, content: str, *, source: Optional[Path] = None) -> Path:
        """Mirror a file into ``<root>/<repo>/<rel_path>`` — from real ``source`` if given, else from
        the provided synthetic ``content``. Returns the mirrored path."""
        dst = self._base / repo / rel_path
        dst.parent.mkdir(parents=True, exist_ok=True)
        if source is not None and Path(source).is_file():
            shutil.copy2(source, dst)
        else:
            dst.write_text(content, encoding="utf-8")
        self.files[f"{repo}:{rel_path}"] = dst
        return dst

    def read(self, repo: str, rel_path: str) -> str:
        return self.files[f"{repo}:{rel_path}"].read_text(encoding="utf-8")

    def cleanup(self) -> None:
        shutil.rmtree(self._base, ignore_errors=True)


# --------------------------------------------------------------------------- Phase 2: fault seeder
class FaultSeeder:
    """Deterministic, non-destructive structural fault injection into mirrored files."""

    @staticmethod
    def interface_contract_break(path: Path, symbol: str = "compute") -> str:
        """Alter a function signature — simulates a reactor endpoint changing its contract."""
        src = path.read_text(encoding="utf-8")
        broken = src.replace(f"def {symbol}(payload, mode):", f"def {symbol}(payload):")
        path.write_text(broken, encoding="utf-8")
        return f"signature of {symbol} changed (payload, mode)->(payload)"

    @staticmethod
    def call_chain_severance(path: Path, symbol: str = "shared_entry") -> str:
        """Comment out an active export/def — trips the path-aware reachability matrix."""
        src = path.read_text(encoding="utf-8")
        severed = src.replace(f"def {symbol}(", f"def _DISABLED_{symbol}(")
        path.write_text(severed, encoding="utf-8")
        return f"export {symbol} severed (renamed/disabled)"


# --------------------------------------------------------------------------- unified graph fixture
class _SyntheticUnifiedGraph:
    """In-memory jarvis↔reactor unified graph implementing the primitives the shipped subsystems use.

    Models: jarvis test → jarvis:app.py:handler → reactor:api.py:compute (cross-boundary CALLS edge).
    Enough for the promoter (cross-boundary detect), the gate's OracleConeReader, and the traceback
    resolver — without indexing real repos (deterministic + fast + zero-risk)."""

    _SIGS = {"reactor:reactor_core/api.py:compute": "compute(payload, mode)"}
    _NODES_IN_FILE = {
        "app.py": ["jarvis:app.py:handler"],
        "reactor_core/api.py": ["reactor:reactor_core/api.py:compute"],
    }
    _DEPS = {  # who each node depends on (outgoing)
        "jarvis:app.py:handler": ["reactor:reactor_core/api.py:compute"],
    }
    _DEPENDENTS = {  # who depends on each node (incoming)
        "reactor:reactor_core/api.py:compute": ["jarvis:app.py:handler"],
    }

    class _Blast:
        def __init__(self, trans):
            self.directly_affected = set()
            self.transitively_affected = set(trans)
            self.risk_level = "medium"

    class _N:
        def __init__(self, k): self._k = k
        def __str__(self): return self._k

    # --- promoter + traceback resolver primitives ---
    def find_nodes_in_file(self, f: str):
        return [self._N(k) for k in self._NODES_IN_FILE.get(f, [])]

    def nodes_in_file(self, f: str):  # traceback resolver alias
        return list(self._NODES_IN_FILE.get(f, []))

    def get_dependencies(self, k: str):
        return [self._N(x) for x in self._DEPS.get(str(k), [])]

    def get_dependents(self, k: str):
        return [self._N(x) for x in self._DEPENDENTS.get(str(k), [])]

    def compute_blast_radius(self, k: str, max_depth: int = 2):
        return self._Blast(self._DEPENDENTS.get(str(k), []))

    def find_nodes_by_name(self, name: str):
        out = []
        for keys in self._NODES_IN_FILE.values():
            for k in keys:
                if k.split(":")[-1] == name:
                    out.append(self._N(k))
        return out

    # --- gate's OracleConeReader primitives ---
    def get_edges_from(self, k: str):
        return [(t, {"edge_type": "calls"}) for t in self._DEPS.get(str(k), [])]

    def get_edges_to(self, k: str):
        return [(s, {"edge_type": "calls"}) for s in self._DEPENDENTS.get(str(k), [])]

    def get_node(self, k: str):
        return {"signature": self._SIGS.get(str(k)), "node_id": {"line_number": 1}, "line_count": 20}


# --------------------------------------------------------------------------- Phase 3: fabric
_JARVIS_APP = """\
def handler(request):
    # jarvis side calls into the reactor boundary
    return compute(request.payload, request.mode)


def shared_entry(x):
    return handler(x)
"""

_REACTOR_API = """\
def compute(payload, mode):
    return {"result": payload, "mode": mode}
"""


class ChaosSimulationFabric:
    """Drives the deterministic cross-repo self-healing loop end-to-end over a mirrored workspace."""

    def __init__(self, *, graph: Any = None, workspace: Optional[ChaosWorkspace] = None) -> None:
        self._graph = graph or _SyntheticUnifiedGraph()
        self._ws = workspace or ChaosWorkspace()

    @property
    def workspace(self) -> ChaosWorkspace:
        return self._ws

    def _candidate_fix(self, file_path: str) -> Dict[str, Any]:
        """A topologically-correct multi-file candidate 'fix' a live model would otherwise produce:
        restore the reactor signature AND update the jarvis caller — coordinated cross-repo patch."""
        return {
            "files": [
                {"file_path": "reactor_core/api.py",
                 "full_content": _REACTOR_API, "rationale": "restore compute(payload, mode) contract"},
                {"file_path": "app.py",
                 "full_content": _JARVIS_APP, "rationale": "caller stays in sync with the contract"},
            ],
        }

    async def run(self, fault: str = "interface_contract_break") -> ChaosReport:
        """Drive the full deterministic loop; return a harvested ChaosReport. Never touches canonical
        source — all writes land in the ephemeral mirror."""
        report = ChaosReport()
        # ---- Phase 1: mirror cross-boundary files (ephemeral) ----
        app = self._ws.mirror("jarvis", "app.py", _JARVIS_APP)
        api = self._ws.mirror("reactor", "reactor_core/api.py", _REACTOR_API)
        report.mark("phase1_mirror", app.is_file() and api.is_file(),
                    root=str(self._ws.root))

        # ---- Phase 2: inject a deterministic structural fault ----
        if fault == "call_chain_severance":
            desc = FaultSeeder.call_chain_severance(app, "shared_entry")
        else:
            desc = FaultSeeder.interface_contract_break(api, "compute")
        report.mark("phase2_fault", "DISABLED" in self._ws.read("jarvis", "app.py")
                    or "def compute(payload):" in self._ws.read("reactor", "reactor_core/api.py"),
                    fault=fault, desc=desc)

        # ---- Phase 3a: fault ingestion → fault coordinates (Repair Context Bridge Slice 1) ----
        from backend.core.ouroboros.governance.intent.repair_traceback import build_traceback_map
        pytest_out = (
            "=================================== FAILURES ===================================\n"
            "_______________________________ test_handler __________________________________\n"
            "app.py:3: in handler\n"
            "    return compute(request.payload, request.mode)\n"
            "reactor_core/api.py:1: in compute\n"
            "    return {\"result\": payload}\n"
            "E   TypeError: compute() takes 1 positional argument but 2 were given\n"
            "=========================== short test summary info ============================\n"
            "FAILED app.py::test_handler - TypeError: compute() takes 1 positional argument\n"
        )
        tb = build_traceback_map(pytest_out, "app.py::test_handler", [str(self._ws.root / "jarvis")],
                                 self._graph)
        report.mark("phase3a_fault_coords", bool(tb.frames),
                    frames=len(tb.frames), fault_node_keys=tb.fault_node_keys)

        # ---- Phase 3b: cross-repo scope promotion (the ignition) ----
        from backend.core.ouroboros.governance.cross_repo_scope_promoter import (
            CrossRepoScopePromoter,
        )
        promoter = CrossRepoScopePromoter(graph=self._graph, primary_repo="jarvis")
        promo = promoter.analyze(("app.py",), "jarvis")
        report.mark("phase3b_promotion", promo.promoted and "reactor" in promo.cross_repos,
                    elevated_scope=list(promo.elevated_scope), boundary_edges=len(promo.boundary_edges),
                    sharded=promo.sharded)

        # ---- Phase 3c: structural validation gate on the candidate fix ----
        # Enable the gate only for the duration of this stage (save/restore — no env leak to
        # sibling tests in the same session).
        _prev_gate = os.environ.get("JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED")
        os.environ["JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED"] = "true"
        from backend.core.ouroboros.governance.structural_validation_gate import (
            StructuralValidationGate, OracleConeReader,
        )
        candidate = self._candidate_fix("reactor_core/api.py")

        async def _analyzer(caller, source, *, filename="", repo_name="", relative_path=""):  # noqa: ANN001
            # deterministic analyzer: the fix restores the contract → no structural regression
            class _OK:
                outcome = type("O", (), {"value": "ok"})()
                edges = ()
                nodes = ()
            return _OK()

        cone = type("Cone", (), {"fault_keys": tb.fault_node_keys or ["reactor:reactor_core/api.py:compute"],
                                 "dependents": [], "dependencies": [], "call_chain": []})()
        reader = OracleConeReader(self._graph, cone, ("app.py::test_handler",))
        gate = StructuralValidationGate(analyzer=_analyzer)
        verdict = await gate.validate(
            candidate_source=_REACTOR_API, file_path="reactor_core/api.py",
            repo_name="reactor", reader=reader,
        )
        report.mark("phase3c_structural_gate", verdict.accepted,
                    analyzed=verdict.analyzed, divergences=len(verdict.divergences))
        # restore the gate env
        if _prev_gate is None:
            os.environ.pop("JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED", None)
        else:
            os.environ["JARVIS_REPAIR_STRUCTURAL_GATE_ENABLED"] = _prev_gate

        # ---- Phase 3d: topologically-ordered multi-file apply (into the mirror) ----
        from backend.core.ouroboros.governance.repair_multifile import (
            extract_candidate_files, topo_sort_files,
        )
        files = extract_candidate_files(candidate)

        def _depends_on(a: str, b: str) -> bool:
            # app.py depends on reactor_core/api.py (jarvis caller → reactor endpoint)
            return a == "app.py" and b == "reactor_core/api.py"

        ordered = topo_sort_files(files, _depends_on)
        applied = []
        for rel, content in ordered:
            repo = "reactor" if rel.startswith("reactor_core/") else "jarvis"
            self._ws.mirror(repo, rel, content)   # apply the fix into the mirror
            applied.append(f"{repo}:{rel}")
        # dependency-first ordering: reactor endpoint applied before the jarvis caller
        topo_ok = ordered and ordered[0][0] == "reactor_core/api.py"
        fix_landed = "def compute(payload, mode):" in self._ws.read("reactor", "reactor_core/api.py")
        report.mark("phase3d_multifile_apply", bool(topo_ok and fix_landed),
                    apply_order=applied)

        # ---- convergence verdict ----
        report.converged = all(report.stages.values())
        return report


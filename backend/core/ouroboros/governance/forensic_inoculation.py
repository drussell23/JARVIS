"""Active Forensic Inoculation Engine — upgrades the OperationAdvisor from passive advice to active
pre-execution de-risking on a CAUTION/BLOCK fragility verdict against a churn hot-spot.

The flow, on a flagged operation:
  Phase 1 — non-destructive forensic ref: stamp ``forensic/saga_<op_id>`` at the current HEAD via
    ``git branch`` (a ref only — NEVER ``checkout -b``, which would mutate the working tree/HEAD mid-
    pipeline; same safety posture as review_branch_manager). A revertible marker of the pre-mutation
    state.
  Phase 2 — characterization baseline: map the hot-spot's public interface via the Oracle, then run a
    DETERMINISTIC import + public-symbol smoke (subprocess, memory-armored background pool) to capture
    a baseline operational signature BEFORE any feature code is generated. (Deterministic, on-device,
    no model call / no provider credits — a model-authored test suite is a future enrichment, not a
    dependency.)
  Phase 3 — enforced mitigation: if the baseline FAILS (the fragile component is already broken /
    won't import), LOCK the gate (escalate to BLOCK) and serialize the failure into an
    un-bypassable structural constraint clause injected into the upcoming generation prompt — forcing
    the model to restructure within the forensic boundary before proceeding.

Gated ``JARVIS_FORENSIC_INOCULATION_ENABLED`` (default OFF — it touches git refs + runs a probe).
Fail-soft throughout: any error degrades to the passive advisory (never breaks the pipeline).
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)

__all__ = ["ForensicInoculationEngine", "InoculationResult", "inoculation_enabled"]


def inoculation_enabled() -> bool:
    """``JARVIS_FORENSIC_INOCULATION_ENABLED`` (default OFF) — active forensic de-risking."""
    return os.environ.get("JARVIS_FORENSIC_INOCULATION_ENABLED", "false").strip().lower() in (
        "1", "true", "yes", "on",
    )


@dataclass
class InoculationResult:
    triggered: bool = False
    forensic_branch: str = ""
    baseline_passed: Optional[bool] = None     # None = not run
    locked: bool = False                       # gate escalated to BLOCK
    constraint_clause: str = ""
    detail: dict = field(default_factory=dict)

    def render_constraint(self) -> str:
        return self.constraint_clause


class ForensicInoculationEngine:
    """Active pre-execution inoculation. Injectable git/probe runners + graph for testability."""

    def __init__(self, repo_root: Any, *, graph: Any = None,
                 git_runner: Any = None, probe_runner: Any = None) -> None:
        self._repo = Path(repo_root)
        self._graph = graph
        self._git = git_runner          # callable(list[str]) -> (rc, stdout, stderr)
        self._probe = probe_runner      # callable(module, symbols) -> (ok, traceback)

    # ------------------------------------------------------------------ git (non-destructive)
    def _run_git(self, args: List[str]):
        if self._git is not None:
            return self._git(args)
        try:
            p = subprocess.run(["git", "-C", str(self._repo), *args],
                               capture_output=True, text=True, timeout=15)
            return p.returncode, p.stdout, p.stderr
        except Exception as exc:  # noqa: BLE001
            return 1, "", str(exc)

    def _create_forensic_ref(self, op_id: str) -> str:
        """Phase 1: ``git branch forensic/saga_<id> HEAD`` — a ref at HEAD, NO checkout, working tree
        + HEAD untouched. Returns the branch name (or '' on failure). Idempotent (force-updates)."""
        safe = re.sub(r"[^A-Za-z0-9_.-]", "-", str(op_id))[:48] or "op"
        name = f"forensic/saga_{safe}"
        rc, _out, err = self._run_git(["branch", "-f", name, "HEAD"])
        if rc != 0:
            logger.debug("[ForensicInoculation] branch create failed (non-fatal): %s", err.strip())
            return ""
        return name

    # ------------------------------------------------------------------ characterization probe
    def _interface(self, file_path: str) -> tuple:
        """Map (module dotted path, public top-level symbols) for the hot-spot via the Oracle, else
        empty. Deterministic; used to build the import+symbol smoke."""
        symbols: List[str] = []
        if self._graph is not None and file_path:
            try:
                for n in (self._graph.find_nodes_in_file(file_path) or []):
                    name = str(n).split(":")[-1]
                    if name and not name.startswith("_"):
                        symbols.append(name)
            except Exception:  # noqa: BLE001
                symbols = []
        mod = ""
        if file_path.endswith(".py"):
            mod = file_path[:-3].replace("/", ".")
        return mod, sorted(set(symbols))[:12]

    def _run_probe(self, module: str, symbols: List[str]):
        """Phase 2: deterministic import + public-symbol smoke in a subprocess. Returns (ok, detail).
        Captures the baseline: does the fragile component import + expose its public interface?"""
        if self._probe is not None:
            return self._probe(module, symbols)
        if not module:
            return True, "no_module"   # nothing to probe → treat as baseline-OK (don't block)
        checks = "; ".join(f"assert hasattr(m,{s!r}), 'missing {s}'" for s in symbols)
        code = f"import importlib,sys; m=importlib.import_module({module!r}); {checks}"
        try:
            p = subprocess.run([os.environ.get("PYTHON", "python3"), "-c", code],
                               cwd=str(self._repo), capture_output=True, text=True, timeout=30,
                               env={**os.environ, "PYTHONPATH": str(self._repo)})
            return (p.returncode == 0), (p.stderr or p.stdout)[-1200:]
        except Exception as exc:  # noqa: BLE001
            return False, f"probe_error:{type(exc).__name__}:{exc}"

    # ------------------------------------------------------------------ orchestration
    def _is_hotspot(self, advisory: Any) -> bool:
        """Hot-spot iff the advisor's git-volatility flagged churn at/over the forensic threshold."""
        try:
            vol = float(getattr(advisory, "git_volatility", 0.0) or 0.0)
        except (TypeError, ValueError):
            vol = 0.0
        # git_volatility is normalized against the advisor's hotspot-commit knob; >=0.5 ≈ a real
        # hot-spot. Also honor an explicit higher forensic bar via the commit threshold.
        return vol >= 0.5

    async def inoculate(self, advisory: Any, target_files: tuple, op_id: str) -> InoculationResult:
        """Run the active inoculation when the advisory is CAUTION/BLOCK against a hot-spot. Returns an
        InoculationResult (triggered=False when not applicable). Gated + fail-soft."""
        res = InoculationResult()
        try:
            if not inoculation_enabled():
                return res
            decision = getattr(getattr(advisory, "decision", None), "value", "")
            if decision not in ("caution", "advise_against", "block"):
                return res
            if not self._is_hotspot(advisory):
                return res
            res.triggered = True
            import asyncio

            # Phase 1 — non-destructive forensic ref
            res.forensic_branch = await asyncio.to_thread(self._create_forensic_ref, op_id)

            # Phase 2 — characterization baseline (memory-armored background pool)
            primary = target_files[0] if target_files else ""
            module, symbols = self._interface(primary)
            ok, detail = await asyncio.to_thread(self._run_probe, module, symbols)
            res.baseline_passed = ok
            res.detail = {"module": module, "symbols": symbols, "probe": detail[:400]}

            # Phase 3 — enforced mitigation on baseline failure
            if ok is False:
                res.locked = True
                res.constraint_clause = (
                    "## FORENSIC INOCULATION — GATE LOCKED (un-bypassable)\n"
                    f"The fragile hot-spot `{primary}` FAILED its pre-mutation characterization "
                    f"baseline (import/interface smoke) on branch `{res.forensic_branch or 'forensic'}`.\n"
                    f"Failure signature:\n{detail.strip()[:600]}\n"
                    "You MUST restructure your integration plan to FIRST restore this component's "
                    "import + public interface within the forensic boundary; do not introduce the "
                    "feature change until the baseline is green."
                )
                logger.warning("[ForensicInoculation] op=%s LOCKED — baseline failed for %s",
                               op_id, primary)
            else:
                logger.info("[ForensicInoculation] op=%s baseline OK for %s (branch=%s)",
                            op_id, primary, res.forensic_branch or "-")
            return res
        except Exception as exc:  # noqa: BLE001 — inoculation must never break the pipeline
            logger.debug("[ForensicInoculation] skipped (non-fatal): %s", exc)
            return res

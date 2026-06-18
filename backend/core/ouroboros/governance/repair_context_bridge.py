"""Repair Context Bridge — Slice 2: graph-informed cognitive context (the steer).

L2's repair GENERATE has a ``repair_context`` slot but nothing populates it with graph topology, so
the model fixes blind to what its patch could break. This bridge builds a **dependency cone** around
the fault coordinates (downstream dependents, upstream dependencies, the causal call-chain from the
failing test) using the shipped 16 GB-safe lazy GraphBackend, and renders it as an explicit boundary
clause injected into ``RepairContext.dependency_cone``.

Honest framing (ADD §2): a prompt clause **steers**, it does not enforce — the model *can* still
wander outside the cone. This makes staying-in-cone the path of least resistance; the Slice 3
structural gate is what actually enforces it.

Design discipline:
- **No duplication** — composes the Oracle's ``compute_blast_radius`` / ``get_dependencies`` /
  ``find_call_chain`` primitives; the only new logic is cone assembly + rendering.
- **Adaptive fault-key resolution** — prefers Slice 1's precise ``fault_node_keys`` (from the signal
  evidence), then the file being repaired, then the failing tests. Works standalone *and* sharper
  when Slice 1 is on. No hardcoded targets.
- **Asynchronous** — the lazy graph query API is synchronous (load-bearing constraint), so the whole
  cone build is offloaded via ``asyncio.to_thread`` — zero block on the L2 loop.
- **Bounded** — caps are env-tunable (no hardcoding); memory rides the proven 7 MB lazy footprint.
- **Fail-soft** — any error → ``None`` cone → L2 degrades to today's graph-blind repair.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, List, Optional, Tuple

from .intent.repair_traceback import bridge_enabled  # single master flag (no dup)

logger = logging.getLogger(__name__)

__all__ = ["RepairCone", "RepairContextBridge", "bridge_enabled"]


def _cone_max_symbols() -> int:
    """``JARVIS_REPAIR_CONE_MAX_SYMBOLS`` — top-K cap on dependents/dependencies/symbols by
    proximity (token-budget discipline). Default 50 (ADD §8 Q1 starting guess)."""
    try:
        return max(1, int(os.environ.get("JARVIS_REPAIR_CONE_MAX_SYMBOLS", "50")))
    except ValueError:
        return 50


def _cone_blast_depth() -> int:
    """``JARVIS_REPAIR_CONE_BLAST_DEPTH`` — blast-radius BFS depth for the cone. Default 2
    (direct dependents + 1 hop), the ADD §8 Q1 lean."""
    try:
        return max(1, int(os.environ.get("JARVIS_REPAIR_CONE_BLAST_DEPTH", "2")))
    except ValueError:
        return 2


@dataclass
class RepairCone:
    """A structured, ordered node/edge set bounding a repair (NOT an embedding vector — see ADD §8
    Q3). Ordered by proximity to the fault so truncation drops the least-relevant first."""

    fault_keys: List[str] = field(default_factory=list)
    files: List[str] = field(default_factory=list)
    symbols: List[str] = field(default_factory=list)
    call_chain: List[str] = field(default_factory=list)
    dependents: List[str] = field(default_factory=list)     # downstream — what the fix could break
    dependencies: List[str] = field(default_factory=list)   # upstream — what it relies on
    risk_level: str = "unknown"
    truncated: bool = False

    def is_empty(self) -> bool:
        return not (self.fault_keys or self.dependents or self.dependencies or self.call_chain)

    def to_dict(self) -> dict:
        return {
            "fault_keys": list(self.fault_keys),
            "files": list(self.files),
            "symbols": list(self.symbols),
            "call_chain": list(self.call_chain),
            "dependents": list(self.dependents),
            "dependencies": list(self.dependencies),
            "risk_level": self.risk_level,
            "truncated": self.truncated,
        }


class RepairContextBridge:
    """Builds + renders the dependency cone for an L2 repair iteration.

    Parameters
    ----------
    oracle_graph:
        The Oracle's ``CodebaseKnowledgeGraph`` (exposes ``compute_blast_radius`` /
        ``get_dependencies`` / ``find_call_chain`` / ``find_nodes_in_file`` / ``find_nodes_by_name``).
        ``None`` → lazily fetched from the global Oracle (fail-soft). Tests inject a fake.
    max_symbols / blast_depth:
        Overrides for the env-tuned caps (tests pin them).
    """

    def __init__(
        self,
        oracle_graph: Optional[Any] = None,
        max_symbols: Optional[int] = None,
        blast_depth: Optional[int] = None,
    ) -> None:
        self._graph = oracle_graph
        self._max_symbols = max_symbols
        self._blast_depth = blast_depth

    # ------------------------------------------------------------------ graph access
    def _get_graph(self) -> Optional[Any]:
        if self._graph is not None:
            return self._graph
        try:
            from backend.core.ouroboros.oracle import get_oracle

            g = getattr(get_oracle(), "_graph", None)
            if g is not None and hasattr(g, "compute_blast_radius"):
                self._graph = g
                return g
        except Exception as exc:  # noqa: BLE001 — graph is best-effort
            logger.debug("[RepairBridge] oracle graph unavailable: %s", exc)
        return None

    # ------------------------------------------------------------------ fault keys
    def resolve_fault_keys(
        self,
        evidence_json: str,
        target_file: str,
        failing_tests: Tuple[str, ...],
        graph: Optional[Any] = None,
    ) -> List[str]:
        """Adaptive fault-key resolution (most-precise source wins, no hardcoding):
        1. Slice 1's ``fault_node_keys`` from the signal evidence (function-level precision).
        2. The file being repaired → its Oracle nodes (file-level — works without Slice 1).
        3. The failing test functions → resolved by name (last-resort entry seed)."""
        # 1) Slice 1 precise coordinates
        keys = _parse_fault_keys(evidence_json)
        if keys:
            return keys
        g = graph if graph is not None else self._get_graph()
        if g is None:
            return []
        # 2) file-level nodes
        if target_file:
            try:
                nodes = g.find_nodes_in_file(target_file)
                if nodes:
                    return [str(n) for n in nodes]
            except Exception as exc:  # noqa: BLE001
                logger.debug("[RepairBridge] find_nodes_in_file failed: %s", exc)
        # 3) failing-test function names
        out: List[str] = []
        for tid in failing_tests:
            name = tid.split("::")[-1].split("[")[0]
            try:
                for n in (g.find_nodes_by_name(name) or [])[:1]:
                    out.append(str(n))
            except Exception:  # noqa: BLE001
                continue
        return out

    # ------------------------------------------------------------------ sync cone build
    def _build_sync(
        self,
        evidence_json: str,
        target_file: str,
        failing_tests: Tuple[str, ...],
    ) -> Optional[RepairCone]:
        g = self._get_graph()
        if g is None:
            return None
        fault_keys = self.resolve_fault_keys(evidence_json, target_file, failing_tests, graph=g)
        if not fault_keys:
            return None

        cap = self._max_symbols if self._max_symbols is not None else _cone_max_symbols()
        depth = self._blast_depth if self._blast_depth is not None else _cone_blast_depth()

        files: List[str] = []
        symbols: List[str] = []
        dependents: List[str] = []
        dependencies: List[str] = []
        call_chain: List[str] = []
        risk_rank = {"low": 1, "medium": 2, "high": 3, "critical": 4, "unknown": 0}
        worst_risk = "unknown"
        truncated = False

        def _add(seq: List[str], val: str) -> None:
            if val and val not in seq:
                seq.append(val)

        # Resolve failing-test entry nodes once (for call chains).
        test_entries: List[str] = []
        for tid in failing_tests[:5]:
            name = tid.split("::")[-1].split("[")[0]
            try:
                for n in (g.find_nodes_by_name(name) or [])[:1]:
                    _add(test_entries, str(n))
            except Exception:  # noqa: BLE001
                continue

        for fkey in fault_keys[:3]:                       # bound the seed set
            _add(symbols, _short_symbol(fkey))
            _add(files, _file_of(fkey))
            # downstream dependents (proximity-ordered: direct first) ────────
            try:
                blast = g.compute_blast_radius(fkey, max_depth=depth)
                if risk_rank.get(getattr(blast, "risk_level", "unknown"), 0) > risk_rank.get(worst_risk, 0):
                    worst_risk = getattr(blast, "risk_level", "unknown")
                for n in sorted(getattr(blast, "directly_affected", set()) or set(), key=str):
                    _add(dependents, str(n))
                    _add(files, getattr(n, "file_path", "") or _file_of(str(n)))
                for n in sorted(getattr(blast, "transitively_affected", set()) or set(), key=str):
                    _add(dependents, str(n))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[RepairBridge] blast_radius failed for %s: %s", fkey, exc)
            # upstream dependencies ──────────────────────────────────────────
            try:
                for n in (g.get_dependencies(fkey) or []):
                    _add(dependencies, str(n))
            except Exception as exc:  # noqa: BLE001
                logger.debug("[RepairBridge] get_dependencies failed for %s: %s", fkey, exc)
            # causal call-chain test→fault ───────────────────────────────────
            for entry in test_entries:
                try:
                    path = g.find_call_chain(entry, fkey)
                    if path:
                        _add(call_chain, " → ".join(str(p) for p in path))
                except Exception:  # noqa: BLE001
                    continue

        # Top-K-by-proximity truncation (dependents are already proximity-ordered).
        if len(dependents) > cap:
            dependents, truncated = dependents[:cap], True
        if len(dependencies) > cap:
            dependencies, truncated = dependencies[:cap], True
        if len(symbols) > cap:
            symbols, truncated = symbols[:cap], True

        cone = RepairCone(
            fault_keys=fault_keys[:3],
            files=files[:cap],
            symbols=symbols,
            call_chain=call_chain[:5],
            dependents=dependents,
            dependencies=dependencies,
            risk_level=worst_risk,
            truncated=truncated,
        )
        return None if cone.is_empty() else cone

    # ------------------------------------------------------------------ async entry
    async def build(
        self,
        *,
        evidence_json: str = "",
        target_file: str = "",
        failing_tests: Tuple[str, ...] = (),
    ) -> Optional[RepairCone]:
        """Build the cone off-loop (the lazy graph query API is synchronous). Fail-soft → None."""
        if not bridge_enabled():
            return None
        import asyncio

        try:
            return await asyncio.to_thread(
                self._build_sync, evidence_json, target_file, tuple(failing_tests)
            )
        except Exception as exc:  # noqa: BLE001 — cone is advisory; never break L2
            logger.debug("[RepairBridge] cone build failed (non-fatal): %s", exc)
            return None

    # ------------------------------------------------------------------ render
    @staticmethod
    def render_clause(cone: RepairCone) -> str:
        """Render the cone as an explicit boundary clause for the repair prompt's REPAIR MODE.

        Honest: this is a *boundary clause* (steer), not enforcement — Slice 3's structural gate is
        what rejects out-of-cone structural damage."""
        if cone is None or cone.is_empty():
            return ""
        lines: List[str] = [
            "## DEPENDENCY CONE (graph-derived boundary — STAY INSIDE)",
            f"Fault coordinates: {', '.join(cone.symbols[:8]) or '(file-level)'}",
        ]
        if cone.files:
            lines.append(f"Files in scope (modify ONLY these): {', '.join(cone.files[:12])}")
        if cone.dependents:
            lines.append(
                "Downstream dependents — these call/import the fault; your fix MUST NOT break "
                f"their contract: {', '.join(cone.dependents[:20])}"
            )
        if cone.dependencies:
            lines.append(
                "Upstream dependencies the fix relies on (do not sever): "
                f"{', '.join(cone.dependencies[:20])}"
            )
        if cone.call_chain:
            lines.append("Causal path(s) from failing test → fault:")
            lines.extend(f"  {c}" for c in cone.call_chain[:3])
        suffix = " (cone truncated to top-K by proximity)" if cone.truncated else ""
        lines.append(
            f"Blast-radius risk={cone.risk_level}{suffix}. Keep the patch inside this cone; "
            "changing structure outside it will be rejected by the structural gate."
        )
        return "\n".join(lines)


# --------------------------------------------------------------------------- helpers
def _parse_fault_keys(evidence_json: str) -> List[str]:
    """Extract Slice 1's ``fault_node_keys`` from a serialized signal-evidence JSON. Fail-soft."""
    if not evidence_json:
        return []
    try:
        data = json.loads(evidence_json)
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(data, dict):
        return []
    keys = data.get("fault_node_keys")
    if isinstance(keys, list):
        return [str(k) for k in keys if k]
    return []


def _file_of(node_key: str) -> str:
    """NodeID str is ``repo:file_path:name`` → the file_path segment (best-effort)."""
    parts = node_key.split(":")
    return parts[1] if len(parts) >= 3 else node_key


def _short_symbol(node_key: str) -> str:
    """NodeID str ``repo:file_path:name`` → ``file_path:name`` (drop the repo prefix for brevity)."""
    parts = node_key.split(":")
    if len(parts) >= 3:
        return f"{parts[1]}:{parts[2]}"
    return node_key

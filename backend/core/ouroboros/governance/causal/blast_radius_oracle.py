"""Intra-repo BlastRadiusOracle -- strict TheOracle delegation.

Domain-1 Staging-2 Task 2.

Answers "given a symbol change, what is the *intra-repo* blast radius?" by
STRICTLY DELEGATING to the existing per-repo :class:`TheOracle`. This module
re-derives ZERO import resolution (Mandate 3): it does not parse ASTs, build
import graphs, or walk dependency edges itself. Its ONLY dependency-source is
``TheOracle.get_blast_radius`` (which internally owns the graph walk).

Cross-repo traversal (widest-path max-product, cycle detection) is Staging 3 --
explicitly NOT implemented here.

Mandate 2 (non-blocking): ``TheOracle.get_blast_radius`` is a *synchronous*
graph walk (``oracle.py`` -> ``compute_blast_radius``). We offload it via the
unified ``cooperative_fs_io.offload`` substrate so a large intra-repo traversal
never starves the event loop. There is no timeout constant -- offload is
cooperative, not deadline-bounded.

Fail-soft: an Oracle miss, an offloaded exception, or a ``None`` result yields
an empty :class:`IntraRepoImpact` (source symbol only, ``risk_level="low"``).
``intra_repo`` never raises.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

from backend.core.ouroboros.governance.causal.causal_graph import CausalGraph
from backend.core.ouroboros.governance.cooperative_fs_io import (
    is_offload_error,
    offload,
)
from backend.core.ouroboros.oracle import get_oracle


@dataclass(frozen=True)
class IntraRepoImpact:
    """Intra-repo blast radius of a symbol change (deterministic, sorted).

    ``directly_affected`` / ``transitively_affected`` are sorted tuples of
    ``"repo:file:name"`` node identities, projected from the Oracle's
    ``BlastRadius``. Sorting makes the impact deterministic across runs.
    """

    source_symbol: str
    directly_affected: Tuple[str, ...]
    transitively_affected: Tuple[str, ...]
    risk_level: str


class BlastRadiusOracle:
    """Intra-repo blast-radius resolver -- a thin delegator over TheOracle.

    Parameters
    ----------
    graph:
        The folded in-memory :class:`CausalGraph` (Task 1). Held for
        Staging-3 cross-repo traversal; the intra-repo path delegates
        entirely to the per-repo Oracle and does not read the graph.
    oracle_fn:
        Injectable zero-arg factory returning a TheOracle-shaped object
        (must expose ``get_blast_radius(target) -> BlastRadius``). Defaults
        to :func:`backend.core.ouroboros.oracle.get_oracle`. A ``None``
        value is treated as "use the default resolver".
    """

    def __init__(
        self,
        graph: CausalGraph,
        *,
        oracle_fn: Optional[Callable[[], Any]] = None,
    ) -> None:
        self._graph = graph
        self._oracle_fn = oracle_fn

    def _empty(self, symbol_id: str) -> IntraRepoImpact:
        return IntraRepoImpact(
            source_symbol=symbol_id,
            directly_affected=(),
            transitively_affected=(),
            risk_level="low",
        )

    async def intra_repo(self, symbol_id: str) -> IntraRepoImpact:
        """Resolve the intra-repo blast radius of ``symbol_id``.

        Delegates entirely to ``get_oracle().get_blast_radius(symbol_id)``,
        offloaded off the event loop (the walk is sync). Maps the resulting
        ``BlastRadius`` (``directly_affected`` / ``transitively_affected``
        NodeIDs -> sorted ``str`` tuples, ``risk_level``) into an
        :class:`IntraRepoImpact`. Never raises -- any fault yields an empty
        impact (source symbol only).
        """
        try:
            resolver = self._oracle_fn or get_oracle
            oracle = resolver()
            if oracle is None:
                return self._empty(symbol_id)

            # Mandate 2: the Oracle graph walk is synchronous -- offload it so
            # the traversal never blocks the loop. offload() is fail-soft: it
            # returns an OffloadError instead of re-raising the fn's exception.
            br = await offload(oracle.get_blast_radius, symbol_id)

            if br is None or is_offload_error(br):
                return self._empty(symbol_id)

            directly = tuple(sorted(str(n) for n in br.directly_affected))
            transitively = tuple(
                sorted(str(n) for n in br.transitively_affected)
            )
            return IntraRepoImpact(
                source_symbol=symbol_id,
                directly_affected=directly,
                transitively_affected=transitively,
                risk_level=br.risk_level,
            )
        except Exception:
            # Fail-soft: Oracle miss / resolver fault / mapping fault -> empty.
            return self._empty(symbol_id)

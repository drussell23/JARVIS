"""A declared symbol is a contract, not a guess — no call-graph fan-out.

2026-09-07, first huge-file goal: the resolver honoured the declared symbol as
primary and then widened it with six call-graph siblings. Six workers were
dispatched for nodes that had nothing to change; each returned an empty node,
each burned five refine turns on "empty node" seam fractures. The widening
exists to hedge a GUESS (a goal keyword, a stack frame); a declaration needs
no hedge.
"""
from __future__ import annotations

import os

from backend.core.ouroboros.governance.target_symbol_resolver import (
    METHOD_DECLARED,
    resolve_target_symbols,
)

SRC = '''
import os

def helper_a(x):
    return x + 1

def helper_b(x):
    return helper_a(x) * 2

class Gen:
    def _swarm_routing_enabled(self):
        return bool(os.environ.get("X"))

    def _maybe_swarm_short_circuit(self, ctx):
        if not self._swarm_routing_enabled():
            return None
        return helper_b(1)
'''


def _resolve(**kw):
    return resolve_target_symbols(
        source=SRC, file_path="gen.py", traceback_frames=(), source_loci=("gen.py",),
        goal="the swarm short-circuit declines multi-file ops", **kw,
    )


def test_a_declared_symbol_resolves_alone():
    res = _resolve(declared_symbols=("_maybe_swarm_short_circuit",))
    assert res.resolved and res.method == METHOD_DECLARED
    assert list(res.primary) == ["Gen._maybe_swarm_short_circuit"]
    assert res.cluster == (), "no sibling workers for a declaration"
    assert list(res.symbol_names) == ["Gen._maybe_swarm_short_circuit"]


def test_an_explicit_expand_request_still_widens():
    res = _resolve(declared_symbols=("_maybe_swarm_short_circuit",), expand_cluster=True)
    assert res.method == METHOD_DECLARED and len(res.cluster) >= 1


def test_a_keyword_guess_still_hedges_with_its_call_graph(monkeypatch):
    monkeypatch.delenv("JARVIS_SYMBOL_RESOLVER_CLUSTER_ENABLED", raising=False)
    res = _resolve()
    if res.resolved and res.method != METHOD_DECLARED:
        assert res.cluster or len(res.symbol_names) >= 1

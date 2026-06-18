from __future__ import annotations


class _FakeBackend:
    """Duck-typed graph backend. degree map: file -> total degree."""
    def __init__(self, file_degrees):
        # file_degrees: {file_path: [(node, succ_count, pred_count), ...]}
        self._fd = file_degrees
    def nodes_in_file(self, file_path):
        return [n for (n, _s, _p) in self._fd.get(file_path, [])]
    def successor_keys(self, key):
        for nodes in self._fd.values():
            for (n, s, _p) in nodes:
                if n == key:
                    return [f"{key}_s{i}" for i in range(s)]
        return []
    def predecessor_keys(self, key):
        for nodes in self._fd.values():
            for (n, _s, p) in nodes:
                if n == key:
                    return [f"{key}_p{i}" for i in range(p)]
        return []


def test_no_pruning_when_under_ceiling():
    from backend.core.ouroboros.governance.topological_file_pruner import prune_files_by_centrality
    files = ["a.py", "b.py"]
    toks = {"a.py": 100, "b.py": 100}
    res = prune_files_by_centrality(files, file_tokens=toks, graph_backend=None, ceiling_tokens=1000)
    assert res.kept_files == files
    assert res.discarded_files == []
    assert res.tokens_before == 200 and res.tokens_after == 200


def test_prunes_lowest_centrality_first():
    from backend.core.ouroboros.governance.topological_file_pruner import prune_files_by_centrality
    # central.py is highly connected; orphan.py is peripheral (0 degree)
    backend = _FakeBackend({
        "central.py": [("central.fn", 5, 5)],   # degree 10
        "mid.py": [("mid.fn", 1, 1)],           # degree 2
        "orphan.py": [("orphan.fn", 0, 0)],     # degree 0
    })
    files = ["central.py", "mid.py", "orphan.py"]
    toks = {"central.py": 600, "mid.py": 600, "orphan.py": 600}
    res = prune_files_by_centrality(files, file_tokens=toks, graph_backend=backend, ceiling_tokens=1000)
    # ceiling 1000: keep highest-centrality until over -> central.py(600) fits; +mid(1200>1000) stop
    assert "central.py" in res.kept_files
    assert "orphan.py" in res.discarded_files      # peripheral dropped first
    assert res.tokens_after <= 1000
    assert set(res.kept_files) | set(res.discarded_files) == set(files)


def test_always_keeps_at_least_one_file_even_if_over_ceiling():
    from backend.core.ouroboros.governance.topological_file_pruner import prune_files_by_centrality
    backend = _FakeBackend({"big.py": [("big.fn", 9, 9)], "small.py": [("small.fn", 0, 0)]})
    files = ["big.py", "small.py"]
    toks = {"big.py": 5000, "small.py": 5000}    # each alone exceeds ceiling
    res = prune_files_by_centrality(files, file_tokens=toks, graph_backend=backend, ceiling_tokens=1000)
    assert len(res.kept_files) == 1               # never drop everything
    assert res.kept_files == ["big.py"]            # the most central survives


def test_fallback_size_order_when_no_backend():
    from backend.core.ouroboros.governance.topological_file_pruner import prune_files_by_centrality
    files = ["a.py", "b.py", "c.py"]
    toks = {"a.py": 400, "b.py": 400, "c.py": 400}
    res = prune_files_by_centrality(files, file_tokens=toks, graph_backend=None, ceiling_tokens=1000)
    # no centrality signal -> keep smaller/earlier greedily under ceiling (2 fit: 800<=1000)
    assert res.tokens_after <= 1000
    assert len(res.kept_files) == 2 and len(res.discarded_files) == 1

from __future__ import annotations
import importlib.util, pathlib
spec = importlib.util.spec_from_file_location(
    "rbl", pathlib.Path("scripts/resource_blackbox_local.py"))
rbl = importlib.util.module_from_spec(spec); spec.loader.exec_module(rbl)


def test_sample_has_required_keys():
    s = rbl.sample()
    for k in ("rss_tree_mb", "free_pct", "cpu_pct", "ctx_rate",
              "swap_used_mb", "disk_free_pct", "ts"):
        assert k in s
    assert isinstance(s["ts"], float)

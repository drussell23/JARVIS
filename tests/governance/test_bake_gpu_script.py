"""Smoke test for the GPU bake CLI -- dry-run must run with ZERO network."""
from __future__ import annotations

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bake_gpu_golden_image.py"


def _load():
    spec = importlib.util.spec_from_file_location("bake_gpu_golden_image", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dry_run_prints_build_and_no_network(capsys):
    mod = _load()
    rc = mod.main(["--project", "myproj", "--image-family", "jarvis-prime-coder-32b", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "jarvis-prime-coder-32b" in out
    assert "packer" in out.lower()          # the build steps are shown
    assert "base64 -d" in out               # spec inlined (no GCS)


def test_dry_run_is_default(capsys):
    mod = _load()
    rc = mod.main(["--project", "p"])       # no --execute -> dry-run
    assert rc == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_missing_spec_errors():
    mod = _load()
    rc = mod.main(["--project", "p", "--spec", "/nonexistent/x.hcl", "--dry-run"])
    assert rc == 2

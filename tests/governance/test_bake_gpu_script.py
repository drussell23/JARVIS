"""Smoke test for the GPU bake CLI -- dry-run must run with ZERO network and
resolve the SOVEREIGN jarvis-prime spec (cross-repo)."""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bake_gpu_golden_image.py"


def _load():
    spec = importlib.util.spec_from_file_location("bake_gpu_golden_image", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake_jprime(tmp_path, monkeypatch):
    """A fake sovereign jarvis-prime repo carrying the GPU spec."""
    spec = tmp_path / "infra" / "packer" / "jprime_gpu_golden_image.pkr.hcl"
    spec.parent.mkdir(parents=True)
    spec.write_text('source "googlecompute" "x" {}\nbuild { sources=["x"] }\n')
    monkeypatch.setenv("JARVIS_PRIME_PATH", str(tmp_path))
    return tmp_path


def test_dry_run_resolves_sovereign_spec(fake_jprime, capsys):
    mod = _load()
    rc = mod.main(["--project", "myproj", "--image-family", "jarvis-prime-coder-32b", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "sovereign jarvis-prime" in out          # reads from jarvis-prime
    assert str(fake_jprime) in out                  # the resolved sovereign path
    assert "base64 -d" in out                       # spec inlined (no GCS)
    assert "multi-zonal fallback" in out.lower()


def test_dry_run_is_default(fake_jprime, capsys):
    mod = _load()
    rc = mod.main(["--project", "p"])               # no --execute -> dry-run
    assert rc == 0
    assert "DRY RUN" in capsys.readouterr().out


def test_missing_sovereign_spec_fails_fast(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_PRIME_PATH", str(tmp_path))   # empty repo -> no spec
    mod = _load()
    rc = mod.main(["--project", "p", "--dry-run"])
    assert rc == 2
    assert "CrossRepoDependencyError" in capsys.readouterr().err

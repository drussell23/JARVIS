"""Slice 65 — containerized test-execution backend for SWE-bench-Pro scoring.

Verified (bt-2026-06-02): local scoring can't run per-repo tests (qutebrowser
needs PyQt etc.), but the problem's Docker image (jefzda/sweap-images:<tag>) has
the full env — `cd /app && git apply test_patch && git apply model_patch &&
pytest <fail_to_pass+pass_to_pass>` made the gold patch's fail_to_pass test PASS.

container_engine is an ALTERNATIVE execution backend for the scorer's apply+run
step (gated, default-OFF → existing local path byte-identical). Adaptive: detects
host arch for --platform emulation; reads the image tag + test ids from the
problem; no hardcoded image/registry/test ids. Docker is driven via an injectable
async runner (real = `docker` CLI subprocess, mirroring scorer._git_apply_patch),
so the orchestration is unit-testable without a daemon.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance.swe_bench_pro import container_engine as ce
from backend.core.ouroboros.governance.swe_bench_pro.dataset_loader import ProblemSpec


def _problem(**meta):
    base = dict(instance_id="instance_x", repo="x/y", base_commit="abc",
                problem_statement="p", test_patch="--- a/t\n+++ b/t\n", gold_patch="g")
    return ProblemSpec(metadata=meta, **base)


# ── pure: image resolution + platform ──────────────────────────────────────

def test_resolve_image_default_namespace(monkeypatch):
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_IMAGE_NAMESPACE", raising=False)
    assert ce.resolve_image("qutebrowser.foo-bar") == "jefzda/sweap-images:qutebrowser.foo-bar"


def test_resolve_image_namespace_overridable(monkeypatch):
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_IMAGE_NAMESPACE", "myorg/imgs")
    assert ce.resolve_image("t") == "myorg/imgs:t"


def test_host_platform_flag_arm_needs_amd64(monkeypatch):
    monkeypatch.setattr(ce.platform, "machine", lambda: "arm64")
    assert ce.host_platform_flag() == "linux/amd64"
    monkeypatch.setattr(ce.platform, "machine", lambda: "aarch64")
    assert ce.host_platform_flag() == "linux/amd64"


def test_host_platform_flag_x86_none(monkeypatch):
    monkeypatch.setattr(ce.platform, "machine", lambda: "x86_64")
    assert ce.host_platform_flag() is None


def test_problem_image_tag_from_metadata():
    p = _problem(dockerhub_tag="qutebrowser.foo")
    assert ce.problem_image_tag(p) == "qutebrowser.foo"
    assert ce.problem_image_tag(_problem()) is None


# ── pure: gating ────────────────────────────────────────────────────────────

def test_should_use_container_gated(monkeypatch):
    p = _problem(dockerhub_tag="t")
    monkeypatch.delenv("JARVIS_SWE_BENCH_PRO_CONTAINER_EVAL_ENABLED", raising=False)
    assert ce.should_use_container(p) is False              # default off
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_CONTAINER_EVAL_ENABLED", "true")
    assert ce.should_use_container(p) is True               # on + has tag
    assert ce.should_use_container(_problem()) is False     # on but no tag


# ── pure: test id extraction (JSON-string OR list in metadata) ──────────────

def test_target_test_ids_union_dedup():
    p = _problem(fail_to_pass='["t::a"]', pass_to_pass='["t::b", "t::a"]')
    ids = ce.target_test_ids(p)
    assert "t::a" in ids and "t::b" in ids and len(ids) == 2  # union, deduped


def test_target_test_ids_mixed_json_and_python_repr():
    # The real dataset shape: fail_to_pass is a Python repr (single quotes),
    # pass_to_pass is JSON (double quotes). Both must parse to clean ids.
    p = _problem(fail_to_pass="['t::error']", pass_to_pass='["t::ok1", "t::ok2"]')
    ids = ce.target_test_ids(p)
    assert ids == ["t::error", "t::ok1", "t::ok2"], ids
    assert "['t::error']" not in ids  # NOT the unparsed literal


# ── pure: pytest -rA result parsing ─────────────────────────────────────────

def test_parse_pytest_text_pass_and_fail():
    out = (
        "==== test session ====\n"
        "PASSED tests/t.py::test_a\n"
        "FAILED tests/t.py::test_b\n"
        "PASSED tests/t.py::test_c\n"
        "==== 2 passed, 1 failed ====\n"
    )
    r = ce.parse_pytest_text(out, ["tests/t.py::test_a", "tests/t.py::test_b", "tests/t.py::test_c"])
    assert r.passed == 2 and r.failed == 1 and r.total == 3
    assert "tests/t.py::test_b" in r.failed_tests


def test_parse_pytest_text_apply_fail_sentinel():
    r = ce.parse_pytest_text("JARVIS_APPLY_FAIL\n", ["t::a"])
    assert r.error and "apply" in r.error.lower()


# ── pure: eval-script builder grounded in the verified flow ─────────────────

def test_build_eval_script_has_verified_steps():
    s = ce.build_eval_script(repo_root="/app",
                             fail_to_pass=["tests/t.py::test_error"],
                             pass_to_pass=["tests/t.py::test_ok"])
    assert "cd /app" in s or "/app" in s
    assert "git apply" in s
    assert "pytest" in s
    assert "tests/t.py::test_error" in s and "tests/t.py::test_ok" in s


# ── orchestration: injectable docker runner (no daemon needed) ──────────────

def test_run_container_scoring_pass(monkeypatch, tmp_path):
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_CONTAINER_EVAL_ENABLED", "true")
    monkeypatch.setattr(ce.platform, "machine", lambda: "arm64")
    p = _problem(dockerhub_tag="qb.tag", fail_to_pass='["tests/t.py::test_error"]',
                 pass_to_pass='["tests/t.py::test_ok"]')

    captured = {}

    async def _fake_docker_run(argv, timeout_s):
        captured["argv"] = argv
        return 0, ("PASSED tests/t.py::test_error\nPASSED tests/t.py::test_ok\n"
                   "==== 2 passed ====\n"), ""

    res = asyncio.run(ce.run_container_scoring(
        p, "--- a/x\n+++ b/x\n", timeout_s=30, _docker_run=_fake_docker_run))
    assert res.error is None
    assert res.total == 2 and res.failed == 0 and res.passed == 2
    # adaptive platform flag + resolved image + entrypoint bash in the argv
    flat = " ".join(captured["argv"])
    assert "--platform" in flat and "linux/amd64" in flat
    assert "jefzda/sweap-images:qb.tag" in flat
    assert "--entrypoint" in flat and "bash" in flat


def test_run_container_scoring_fail(monkeypatch):
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_CONTAINER_EVAL_ENABLED", "true")
    p = _problem(dockerhub_tag="qb.tag", fail_to_pass='["tests/t.py::test_error"]',
                 pass_to_pass='[]')

    async def _fake(argv, timeout_s):
        return 1, "FAILED tests/t.py::test_error\n==== 1 failed ====\n", ""

    res = asyncio.run(ce.run_container_scoring(
        p, "patch", timeout_s=30, _docker_run=_fake))
    assert res.error is None and res.failed == 1 and res.passed == 0


def test_run_container_scoring_docker_error_is_scoring_error(monkeypatch):
    monkeypatch.setenv("JARVIS_SWE_BENCH_PRO_CONTAINER_EVAL_ENABLED", "true")
    p = _problem(dockerhub_tag="qb.tag", fail_to_pass='["t::a"]', pass_to_pass='[]')

    async def _boom(argv, timeout_s):
        raise RuntimeError("docker daemon not running")

    res = asyncio.run(ce.run_container_scoring(p, "patch", timeout_s=30, _docker_run=_boom))
    assert res.error is not None  # surfaced as a scoring error, never raises

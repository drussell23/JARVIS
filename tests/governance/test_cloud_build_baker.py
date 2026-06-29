"""Serverless IaC Execution -- the Automated REST Baker (Cloud Build).

Submits the GPU Packer .hcl as a remote Cloud Build job via REST (reusing the ADC
auth bridge), so the 32B golden image is manufactured hands-off -- zero local
Packer, zero gcloud, zero GCS upload (the spec is inlined as base64 into the build
steps). These tests cover the PURE pieces: the Build-resource construction and the
build-status state machine.
"""
from __future__ import annotations

import base64

import pytest

from backend.core.ouroboros.governance.cloud_build_baker import (
    build_packer_cloud_build,
    build_status_is_terminal,
    build_status_is_success,
)


_SPEC = 'source "googlecompute" "x" {}\nbuild { sources = ["x"] }\n'


def test_build_has_write_init_build_steps():
    cfg = build_packer_cloud_build(
        spec_text=_SPEC, project="proj", substitutions={}, image_family="fam",
    )
    steps = cfg["steps"]
    assert len(steps) >= 3
    joined = " ".join(" ".join(s.get("args", [])) for s in steps)
    assert "base64 -d" in joined           # step 1 writes the inlined spec
    assert "init" in joined                # packer init
    assert "build" in joined               # packer build


def test_spec_is_inlined_as_gzip_base64_roundtrip():
    import gzip
    cfg = build_packer_cloud_build(
        spec_text=_SPEC, project="proj", substitutions={}, image_family="fam",
    )
    writer_arg = cfg["steps"][0]["args"][1]
    assert "gunzip" in writer_arg          # gzip-compressed inline (stays under 10000)
    # Extract the base64 blob and prove it round-trips back to the exact spec.
    b64 = writer_arg.split("printf %s '")[1].split("'")[0]
    assert gzip.decompress(base64.b64decode(b64)).decode() == _SPEC
    assert len(writer_arg) < 10000         # Cloud Build per-arg limit respected


def test_build_passes_project_and_image_family_vars():
    cfg = build_packer_cloud_build(
        spec_text=_SPEC, project="myproj", substitutions={}, image_family="jarvis-prime-coder-32b",
    )
    joined = " ".join(" ".join(s.get("args", [])) for s in cfg["steps"])
    assert "project_id=myproj" in joined
    assert "image_family=jarvis-prime-coder-32b" in joined


def test_build_has_long_timeout_and_machine():
    cfg = build_packer_cloud_build(
        spec_text=_SPEC, project="p", substitutions={}, image_family="f",
        timeout_s=3600, machine_type="E2_HIGHCPU_8",
    )
    assert cfg["timeout"] == "3600s"       # GPU bake + 32B pull needs >>10min default
    assert cfg["options"]["machineType"] == "E2_HIGHCPU_8"


def test_no_gcs_source():
    cfg = build_packer_cloud_build(
        spec_text=_SPEC, project="p", substitutions={}, image_family="f",
    )
    assert "source" not in cfg             # inline -> no storageSource, no upload


def test_extra_substitutions_merge():
    cfg = build_packer_cloud_build(
        spec_text=_SPEC, project="p", substitutions={"_MODEL": "qwen2.5-coder:32b"},
        image_family="f",
    )
    assert cfg["substitutions"]["_MODEL"] == "qwen2.5-coder:32b"


@pytest.mark.parametrize("status,terminal", [
    ("SUCCESS", True), ("FAILURE", True), ("TIMEOUT", True),
    ("CANCELLED", True), ("EXPIRED", True), ("INTERNAL_ERROR", True),
    ("QUEUED", False), ("WORKING", False), ("PENDING", False), ("", False),
])
def test_status_terminal(status, terminal):
    assert build_status_is_terminal(status) is terminal


@pytest.mark.parametrize("status,ok", [
    ("SUCCESS", True), ("FAILURE", False), ("TIMEOUT", False), ("WORKING", False),
])
def test_status_success(status, ok):
    assert build_status_is_success(status) is ok


def test_iso_duration_parsing():
    from backend.core.ouroboros.governance.cloud_build_baker import _iso_duration_s
    assert _iso_duration_s("2026-06-29T19:50:00Z", "2026-06-29T19:51:14Z") == 74.0
    # nanosecond precision + Z tolerated
    d = _iso_duration_s("2026-06-29T19:50:00.123456789Z", "2026-06-29T19:50:05.123456789Z")
    assert abs(d - 5.0) < 0.001
    assert _iso_duration_s(None, "x") is None


async def test_stockout_probe_uses_step_duration(tmp_path, monkeypatch):
    from backend.core.ouroboros.governance.cloud_build_baker import CloudBuildBaker
    spec = tmp_path / "x.pkr.hcl"; spec.write_text('source "googlecompute" "x" {}\n')
    b = CloudBuildBaker(spec_path=str(spec), project="p", image_family="f")

    async def auth():
        return "tok", "p"
    monkeypatch.setattr(b, "_auth", auth)

    def build_with_step(dur_s):
        async def _g(project, token, bid):
            return {"steps": [
                {"status": "SUCCESS"},
                {"status": "FAILURE", "timing": {
                    "startTime": "2026-06-29T19:50:00Z",
                    "endTime": f"2026-06-29T19:50:{dur_s:02d}Z"}},
            ]}
        return _g

    # 74s failed step -> reached instance creation -> capacity/stockout -> True
    monkeypatch.setattr(b, "_get_build", build_with_step(0))  # placeholder
    async def slow(project, token, bid):
        return {"steps": [{"status": "FAILURE", "timing": {
            "startTime": "2026-06-29T19:50:00Z", "endTime": "2026-06-29T19:51:14Z"}}]}
    monkeypatch.setattr(b, "_get_build", slow)
    assert await b._build_failed_with_stockout("bid") is True

    # 6s fast fail -> config/prepare -> NOT capacity -> False
    monkeypatch.setattr(b, "_get_build", build_with_step(6))
    assert await b._build_failed_with_stockout("bid") is False

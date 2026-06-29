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


def test_spec_is_inlined_as_base64():
    cfg = build_packer_cloud_build(
        spec_text=_SPEC, project="proj", substitutions={}, image_family="fam",
    )
    b64 = base64.b64encode(_SPEC.encode()).decode()
    blob = repr(cfg["steps"])
    assert b64 in blob                     # the exact spec rides inline (no GCS)


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

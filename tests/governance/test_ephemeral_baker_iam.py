"""Ephemeral Zero-Trust IAM for the bake -- pure pieces.

The baker creates a DEDICATED temporary service account, binds least-privilege
roles, runs the Cloud Build AS that SA, and GUARANTEES teardown (delete the SA the
millisecond the build concludes). The default Cloud Build SA is never touched.
"""
from __future__ import annotations

import re

import pytest

from backend.core.ouroboros.governance.cloud_build_baker import (
    build_packer_cloud_build,
    baker_sa_roles,
    temp_sa_account_id,
    create_sa_payload,
    iam_policy_add_binding,
    iam_policy_remove_member,
)

_SPEC = 'source "googlecompute" "x" {}\n'


def test_build_runs_as_custom_sa():
    cfg = build_packer_cloud_build(
        spec_text=_SPEC, project="proj", image_family="f",
        service_account="projects/proj/serviceAccounts/baker@proj.iam.gserviceaccount.com",
    )
    assert cfg["serviceAccount"].endswith("baker@proj.iam.gserviceaccount.com")
    # Custom SA -> GCS logging (survives SA deletion; stockout detector reads it).
    assert cfg["options"]["logging"] == "GCS_ONLY"
    assert cfg["logsBucket"].startswith("gs://")


def test_no_service_account_no_logging_override():
    cfg = build_packer_cloud_build(spec_text=_SPEC, project="p", image_family="f")
    assert "serviceAccount" not in cfg
    assert "logging" not in cfg["options"]


def test_temp_sa_account_id_is_valid_gcp_id():
    aid = temp_sa_account_id()
    # GCP SA accountId: 6-30 chars, ^[a-z][a-z0-9-]{4,28}[a-z0-9]$
    assert re.fullmatch(r"[a-z][a-z0-9-]{4,28}[a-z0-9]", aid), aid
    assert "baker" in aid


def test_temp_sa_account_ids_are_unique_per_call():
    assert temp_sa_account_id() != temp_sa_account_id()  # collision-free temp SAs


def test_create_sa_payload_shape():
    p = create_sa_payload("jarvis-gpu-baker-ab12cd", "GPU baker (ephemeral)")
    assert p["accountId"] == "jarvis-gpu-baker-ab12cd"
    assert p["serviceAccount"]["displayName"] == "GPU baker (ephemeral)"


def test_default_roles_are_least_privilege():
    roles = baker_sa_roles()
    # Exactly what Packer needs to bake a GPU image + write build logs.
    assert "roles/compute.instanceAdmin.v1" in roles
    assert "roles/iam.serviceAccountUser" in roles
    assert "roles/logging.logWriter" in roles
    # The blast-radius role we REFUSED to grant the default SA is NOT here.
    assert "roles/compute.admin" not in roles
    assert "roles/owner" not in roles
    assert "roles/editor" not in roles


def test_roles_env_overridable(monkeypatch):
    monkeypatch.setenv("JARVIS_BAKE_SA_ROLES", "roles/compute.instanceAdmin.v1,roles/logging.logWriter")
    assert baker_sa_roles() == ["roles/compute.instanceAdmin.v1", "roles/logging.logWriter"]


def test_iam_policy_add_binding_creates_and_appends():
    policy = {"bindings": [{"role": "roles/x", "members": ["user:a@b.com"]}]}
    member = "serviceAccount:baker@proj.iam.gserviceaccount.com"
    # New role -> new binding.
    p2 = iam_policy_add_binding(policy, "roles/compute.instanceAdmin.v1", member)
    b = [x for x in p2["bindings"] if x["role"] == "roles/compute.instanceAdmin.v1"]
    assert b and member in b[0]["members"]
    # Existing role -> append member, no duplicate binding.
    p3 = iam_policy_add_binding(p2, "roles/x", member)
    bx = [x for x in p3["bindings"] if x["role"] == "roles/x"]
    assert len(bx) == 1 and member in bx[0]["members"]


def test_iam_policy_remove_member_strips_everywhere():
    member = "serviceAccount:baker@proj.iam.gserviceaccount.com"
    policy = {"bindings": [
        {"role": "roles/a", "members": [member, "user:keep@b.com"]},
        {"role": "roles/b", "members": [member]},
    ]}
    p2 = iam_policy_remove_member(policy, member)
    # member gone everywhere; the now-empty binding is dropped; others kept.
    flat = [m for b in p2["bindings"] for m in b["members"]]
    assert member not in flat
    assert "user:keep@b.com" in flat
    assert all(b["members"] for b in p2["bindings"])  # no empty bindings linger

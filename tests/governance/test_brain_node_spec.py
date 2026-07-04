"""Stage-1 Brain-VM spec tests (Task 2).

The CPU Brain VM is a SPEC VARIANT of the existing J-Prime insert -- NOT a
parallel provisioning path. These tests pin the spec-dict contract:

  * machine type / image family / Spot / idle-shutdown all env-resolved at call
    time (Mandate 2 -- Architectural Purity; zero baked assumptions),
  * CPU-only: the payload carries NO ``guestAccelerators`` (Mandate 4 --
    Bulletproof; no accidental GPU spend),
  * ``labels[jarvis-role] == brain`` for the Stage-1 discovery filter (Task 3),
  * the ``brain-env`` metadata item carries the idle-shutdown seconds (the
    Task-1 bake reads instance-metadata key ``brain-env`` -> /etc/jarvis/brain.env),
  * the existing ``_build_insert_payload`` stays BYTE-IDENTICAL when the new
    ``labels`` / ``extra_metadata`` params are None (backward-compat pin --
    every existing L4/J-Prime caller is unchanged).

No live GCP: these operate purely on the constructed spec dict.
"""
from __future__ import annotations

import pytest

from backend.core.ouroboros.governance.gcp_compute_rest import (
    GCPComputeRest,
    _brain_idle_shutdown_s,
    _brain_persistent,
    _brain_spot,
)


# Fixed identity args -- the Brain node still resolves zone/project dynamically
# in create_instance; the spec builder just needs them threaded for the URL.
_NAME = "jarvis-brain"
_ZONE = "us-central1-a"
_PROJECT = "test-project"


def _client() -> GCPComputeRest:
    return GCPComputeRest(project=_PROJECT, zone=_ZONE)


def _brain_payload(client: GCPComputeRest):
    return client.build_brain_insert_payload(
        name=_NAME, zone=_ZONE, project=_PROJECT, startup_script="echo brain",
    )


# -- (a) machine type env-driven (varied across 2 settings, not a literal) ----

@pytest.mark.parametrize("machine_type", ["e2-highmem-4", "n2-highmem-8"])
def test_brain_machine_type_from_env(monkeypatch, machine_type):
    monkeypatch.setenv("JARVIS_BRAIN_VM_MACHINE_TYPE", machine_type)
    payload = _brain_payload(_client())
    assert payload["machineType"] == "zones/{}/machineTypes/{}".format(_ZONE, machine_type)


# -- (b) NO guestAccelerators -- no accidental GPU spend ----------------------

def test_brain_payload_has_no_accelerator(monkeypatch):
    monkeypatch.delenv("JARVIS_BRAIN_VM_MACHINE_TYPE", raising=False)
    payload = _brain_payload(_client())
    assert "guestAccelerators" not in payload


# -- (c) labels[jarvis-role] == brain -----------------------------------------

def test_brain_payload_role_label(monkeypatch):
    payload = _brain_payload(_client())
    assert payload["labels"]["jarvis-role"] == "brain"


# -- (d) image family from env ------------------------------------------------

@pytest.mark.parametrize("image_family", ["jarvis-brain-golden", "jarvis-brain-golden-20260704"])
def test_brain_image_family_from_env(monkeypatch, image_family):
    monkeypatch.setenv("JARVIS_BRAIN_VM_IMAGE_FAMILY", image_family)
    payload = _brain_payload(_client())
    src = payload["disks"][0]["initializeParams"]["sourceImage"]
    assert src == "projects/{}/global/images/family/{}".format(_PROJECT, image_family)


# -- (e) idle-shutdown metadata item present with env value -------------------

@pytest.mark.parametrize("idle_s", ["1800", "600"])
def test_brain_idle_shutdown_metadata_item(monkeypatch, idle_s):
    monkeypatch.setenv("JARVIS_BRAIN_VM_IDLE_SHUTDOWN_S", idle_s)
    payload = _brain_payload(_client())
    items = payload["metadata"]["items"]
    brain_env = [it for it in items if it["key"] == "brain-env"]
    assert len(brain_env) == 1, "brain-env metadata item must be present"
    assert "JARVIS_BRAIN_VM_IDLE_SHUTDOWN_S={}".format(idle_s) in brain_env[0]["value"]
    # startup-script item is still first / preserved (backward-compat within items).
    assert items[0]["key"] == "startup-script"


def test_brain_env_folds_ignition_values(monkeypatch):
    """Task-4's ignition tokens/endpoints fold into the brain.env body via the
    same metadata mechanism (the plumbing this task installs)."""
    monkeypatch.setenv("JARVIS_BRAIN_VM_IDLE_SHUTDOWN_S", "1800")
    payload = _client().build_brain_insert_payload(
        name=_NAME, zone=_ZONE, project=_PROJECT, startup_script="echo brain",
        brain_env_values={"JARVIS_WS_ENDPOINT": "wss://brain:8443", "JARVIS_TOKEN": "abc"},
    )
    body = [it for it in payload["metadata"]["items"] if it["key"] == "brain-env"][0]["value"]
    assert "JARVIS_BRAIN_VM_IDLE_SHUTDOWN_S=1800" in body
    assert "JARVIS_WS_ENDPOINT=wss://brain:8443" in body
    assert "JARVIS_TOKEN=abc" in body


# -- (f) backward-compat pin: labels/extra_metadata None == pre-change payload -

def test_build_insert_payload_none_path_byte_identical(monkeypatch):
    """The pre-existing callers (L4 / J-Prime) pass neither labels nor
    extra_metadata. Adding those params (default None) MUST leave the payload
    byte-identical to what it produced before this change. Expected dict is
    constructed explicitly (not by calling the same function twice)."""
    # Omit diskType so the expected dict has no boot-disk field (deterministic).
    monkeypatch.setenv("JARVIS_FAILOVER_BOOT_DISK_TYPE", "")
    client = _client()
    got = client._build_insert_payload(
        name="n", zone="z", project="p", machine_type="mt",
        image_family="fam", startup_script="echo hi", spot=False,
        labels=None, extra_metadata=None,
    )
    expected = {
        "name": "n",
        "machineType": "zones/z/machineTypes/mt",
        "disks": [
            {
                "boot": True,
                "autoDelete": True,
                "initializeParams": {
                    "sourceImage": "projects/p/global/images/family/fam",
                },
            }
        ],
        "networkInterfaces": [
            {
                "network": "global/networks/default",
                "accessConfigs": [
                    {"type": "ONE_TO_ONE_NAT", "name": "External NAT"}
                ],
            }
        ],
        "serviceAccounts": [
            {
                "email": "default",
                "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
            }
        ],
        "metadata": {
            "items": [
                {"key": "startup-script", "value": "echo hi"},
            ]
        },
    }
    assert got == expected
    assert "labels" not in got


def test_build_insert_payload_labels_only_when_provided(monkeypatch):
    monkeypatch.setenv("JARVIS_FAILOVER_BOOT_DISK_TYPE", "")
    client = _client()
    got = client._build_insert_payload(
        name="n", zone="z", project="p", machine_type="mt",
        image_family="fam", startup_script="echo hi", spot=False,
        labels={"jarvis-role": "brain"},
    )
    assert got["labels"] == {"jarvis-role": "brain"}
    # extra_metadata None -> only the startup-script item.
    assert got["metadata"]["items"] == [{"key": "startup-script", "value": "echo hi"}]


# -- (g) persistent / spot accessors read env at CALL time --------------------

def test_brain_persistent_accessor_reads_env_at_call_time(monkeypatch):
    monkeypatch.delenv("JARVIS_BRAIN_VM_PERSISTENT", raising=False)
    assert _brain_persistent() is False  # default false
    monkeypatch.setenv("JARVIS_BRAIN_VM_PERSISTENT", "true")
    assert _brain_persistent() is True
    monkeypatch.setenv("JARVIS_BRAIN_VM_PERSISTENT", "0")
    assert _brain_persistent() is False


def test_brain_spot_accessor_reads_env_at_call_time(monkeypatch):
    monkeypatch.delenv("JARVIS_BRAIN_VM_SPOT", raising=False)
    assert _brain_spot() is True  # default true (Spot is cheap)
    monkeypatch.setenv("JARVIS_BRAIN_VM_SPOT", "false")
    assert _brain_spot() is False


def test_brain_idle_shutdown_accessor_reads_env_at_call_time(monkeypatch):
    monkeypatch.delenv("JARVIS_BRAIN_VM_IDLE_SHUTDOWN_S", raising=False)
    assert _brain_idle_shutdown_s() == 1800  # default
    monkeypatch.setenv("JARVIS_BRAIN_VM_IDLE_SHUTDOWN_S", "600")
    assert _brain_idle_shutdown_s() == 600


def test_brain_spot_reflected_in_payload_scheduling(monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_VM_SPOT", "true")
    payload = _brain_payload(_client())
    assert payload["scheduling"]["provisioningModel"] == "SPOT"
    monkeypatch.setenv("JARVIS_BRAIN_VM_SPOT", "false")
    payload = _brain_payload(_client())
    assert "scheduling" not in payload  # on-demand -> no Spot scheduling block

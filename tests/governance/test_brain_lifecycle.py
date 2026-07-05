"""Tests for backend/core/ouroboros/governance/brain_lifecycle.py (Stage-4 Task 1).

The hierarchical-ownership substrate: lineage labels + append-only Resource
Manifest + manifest-walk-only family teardown + the extracted Brain
provisioning core. NO live GCP -- every delete/create/query collaborator is an
injected fake. The existing driver suites (tests/infra/test_ignite_brain_vm.py,
test_brain_tls_delivery.py) pin the extraction's byte-equivalence UNMODIFIED.

Proves:
  (a) manifest append/replay round-trip incl. delete tombstones + corrupt-line
      tolerance;
  (b) ``live_family`` children-first ordering across a 3-level tree;
  (c) ``teardown_family`` deletes children before the parent, records delete
      tombstones, and is fail-soft on a single failing delete (continues +
      reports);
  (d) the drift detector: a label-query hit the manifest never recorded is
      LOUD-warned + returned as ``drift`` -- and NEVER deleted (the manifest is
      the only deletion source);
  (e) ``max_gen`` across records (delete tombstones never regress it);
  (f) ``sanitize_label_value`` edge cases (GCP label charset);
  (g) ``provision_brain`` composes labels + brain-env via an injected fake
      create fn;
  (h) failover_lifecycle lineage-label fold from brain-env (additive,
      env-absent -> None).
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

import pytest

from backend.core.ouroboros.governance import brain_lifecycle as bl


@pytest.fixture()
def manifest(tmp_path, monkeypatch):
    path = tmp_path / "manifests" / "resource_manifest.jsonl"
    monkeypatch.setenv("JARVIS_RESOURCE_MANIFEST_PATH", str(path))
    monkeypatch.setenv("JARVIS_KEEPER_ID", "keeper-test")
    return bl.ResourceManifest()


def _create(m: bl.ResourceManifest, name: str, *, parent: Optional[str] = None,
            kind: str = "instance", zone: str = "us-central1-a",
            gen: Optional[int] = None) -> Dict[str, Any]:
    return m.record_create(
        kind=kind, name=name, zone=zone,
        labels={bl.LABEL_OWNER: "root-brain"}, parent=parent, gen=gen,
    )


# ---------------------------------------------------------------------------
# (a) Append/replay round-trip + tombstones + corrupt-line tolerance.
# ---------------------------------------------------------------------------


def test_manifest_append_replay_round_trip(manifest) -> None:
    rec = _create(manifest, "brain-root", gen=1)
    _create(manifest, "child-a", parent="brain-root")

    assert manifest.path.exists(), "append must materialize the JSONL"
    fam = manifest.live_family("brain-root")
    names = [r["name"] for r in fam]
    assert names == ["child-a", "brain-root"]

    # Every record field survives the round trip.
    replayed_root = fam[-1]
    for key in ("op", "kind", "name", "zone", "labels", "parent", "gen",
                "keeper_id", "ts_utc"):
        assert key in replayed_root, "record must carry %s" % key
    assert replayed_root["kind"] == rec["kind"]
    assert replayed_root["keeper_id"] == "keeper-test"
    assert replayed_root["labels"] == {bl.LABEL_OWNER: "root-brain"}


def test_manifest_delete_tombstone_removes_from_live_set(manifest) -> None:
    _create(manifest, "brain-root")
    _create(manifest, "child-a", parent="brain-root")
    manifest.record_delete("child-a")

    names = [r["name"] for r in manifest.live_family("brain-root")]
    assert "child-a" not in names, "a tombstoned resource must not replay live"
    assert names == ["brain-root"]


def test_manifest_corrupt_line_is_skipped_not_fatal(manifest) -> None:
    _create(manifest, "brain-root")
    with manifest.path.open("a", encoding="utf-8") as fh:
        fh.write("{this is not json!!\n")
        fh.write("\n")  # blank line
    _create(manifest, "child-a", parent="brain-root")

    names = [r["name"] for r in manifest.live_family("brain-root")]
    assert names == ["child-a", "brain-root"], (
        "corrupt/blank lines must be skipped, records around them preserved")


def test_manifest_default_path_is_repo_local(monkeypatch) -> None:
    monkeypatch.delenv("JARVIS_RESOURCE_MANIFEST_PATH", raising=False)
    p = bl._default_manifest_path()
    assert p.parts[-3:] == (".jarvis", "manifests", "resource_manifest.jsonl")


# ---------------------------------------------------------------------------
# (b) live_family: children-first ordering across a 3-level tree.
# ---------------------------------------------------------------------------


def test_live_family_children_first_three_level_tree(manifest) -> None:
    #   root
    #   +-- child-1 --- grand-1
    #   +-- child-2
    _create(manifest, "root", gen=1)
    _create(manifest, "child-1", parent="root")
    _create(manifest, "child-2", parent="root")
    _create(manifest, "grand-1", parent="child-1")
    _create(manifest, "unrelated")  # different family -- must not appear

    names = [r["name"] for r in manifest.live_family("root")]
    assert "unrelated" not in names
    assert set(names) == {"root", "child-1", "child-2", "grand-1"}
    # CHILDREN FIRST: deepest level first, root strictly last.
    assert names[0] == "grand-1", "the deepest descendant must come first"
    assert names[-1] == "root", "the root must come last (deleted after children)"
    assert names.index("grand-1") < names.index("child-1")


def test_live_family_empty_for_unknown_root(manifest) -> None:
    _create(manifest, "some-node")
    assert manifest.live_family("never-recorded") == []


def test_live_family_terminates_on_parent_cycle(manifest) -> None:
    """Cycle-safety pin (Task-3 fold-in from the Task-1 review): a 2-node
    parent CYCLE (a->b->a) must terminate, return both nodes, and never hang.
    Bounded by a hard deadline -- a regression that reintroduces an unbounded
    walk fails the test instead of wedging the suite."""
    from concurrent.futures import ThreadPoolExecutor  # noqa: PLC0415

    _create(manifest, "node-a", parent="node-b")
    _create(manifest, "node-b", parent="node-a")

    with ThreadPoolExecutor(max_workers=1) as pool:
        # .result(timeout=...) is the bounded deadline: raises TimeoutError
        # instead of hanging if the BFS cycle guard ever regresses.
        fam = pool.submit(manifest.live_family, "node-a").result(timeout=10)

    names = [r["name"] for r in fam]
    assert set(names) == {"node-a", "node-b"}, "both cycle members returned"
    assert names[-1] == "node-a", "the root still comes last (children first)"


# ---------------------------------------------------------------------------
# (c) teardown_family: order + tombstones + fail-soft per item.
# ---------------------------------------------------------------------------


class _FakeDeleter:
    def __init__(self, fail_names: Optional[set] = None,
                 raise_names: Optional[set] = None) -> None:
        self.calls: List[Tuple[str, Optional[str]]] = []
        self.fw_calls: List[str] = []
        self._fail = fail_names or set()
        self._raise = raise_names or set()

    async def delete_instance(self, name: str, *, zone: Optional[str] = None
                              ) -> Tuple[bool, str]:
        self.calls.append((name, zone))
        if name in self._raise:
            raise RuntimeError("boom deleting %s" % name)
        if name in self._fail:
            return (False, "INSERT_STILL_RUNNING")
        return (True, "deleted:200")

    async def delete_firewall(self, *, rule_name: str) -> Tuple[bool, str]:
        self.fw_calls.append(rule_name)
        return (True, "deleted:404")


def test_teardown_family_deletes_children_before_parent(manifest) -> None:
    _create(manifest, "root")
    _create(manifest, "child-1", parent="root")
    _create(manifest, "grand-1", parent="child-1")
    fake = _FakeDeleter()

    result = asyncio.run(bl.teardown_family(
        "root", manifest=manifest, delete_instance_fn=fake.delete_instance,
    ))

    order = [n for n, _z in fake.calls]
    assert order == ["grand-1", "child-1", "root"]
    assert result["deleted"] == ["grand-1", "child-1", "root"]
    assert result["failed"] == []
    # Deletes are tombstoned -> the family has converged to empty.
    assert manifest.live_family("root") == []


def test_teardown_family_routes_firewall_kind_to_firewall_fn(manifest) -> None:
    _create(manifest, "root")
    _create(manifest, "root-mtls-fw", parent="root", kind="firewall")
    fake = _FakeDeleter()

    result = asyncio.run(bl.teardown_family(
        "root", manifest=manifest,
        delete_instance_fn=fake.delete_instance,
        delete_firewall_fn=fake.delete_firewall,
    ))
    assert fake.fw_calls == ["root-mtls-fw"]
    assert [n for n, _z in fake.calls] == ["root"], (
        "the firewall record must never hit the instance deleter")
    assert set(result["deleted"]) == {"root-mtls-fw", "root"}


def test_teardown_family_fail_soft_continues_and_reports(manifest) -> None:
    _create(manifest, "root")
    _create(manifest, "child-ok", parent="root")
    _create(manifest, "child-bad", parent="root")
    fake = _FakeDeleter(raise_names={"child-bad"})

    result = asyncio.run(bl.teardown_family(
        "root", manifest=manifest, delete_instance_fn=fake.delete_instance,
    ))

    attempted = {n for n, _z in fake.calls}
    assert attempted == {"child-ok", "child-bad", "root"}, (
        "one failing delete must not stop the walk")
    assert "child-ok" in result["deleted"] and "root" in result["deleted"]
    assert [f["name"] for f in result["failed"]] == ["child-bad"]
    # The failed child stays LIVE on the manifest (no tombstone) so the next
    # walk retries it.
    live = [r["name"] for r in manifest.live_family("root")]
    assert live == ["child-bad"]


def test_teardown_family_nonok_delete_reported_not_tombstoned(manifest) -> None:
    _create(manifest, "root")
    fake = _FakeDeleter(fail_names={"root"})
    result = asyncio.run(bl.teardown_family(
        "root", manifest=manifest, delete_instance_fn=fake.delete_instance,
    ))
    assert result["deleted"] == []
    assert [f["name"] for f in result["failed"]] == ["root"]
    assert [r["name"] for r in manifest.live_family("root")] == ["root"]


# ---------------------------------------------------------------------------
# (d) Drift detector: unknown label-family member -> loud warn + returned,
#     NEVER deleted (the manifest is the only deletion source).
# ---------------------------------------------------------------------------


def test_drift_detector_flags_unknown_resource_loudly(manifest, caplog) -> None:
    _create(manifest, "root")
    _create(manifest, "child-1", parent="root")
    fake = _FakeDeleter()
    query_calls: List[Dict[str, Any]] = []

    async def label_query(**kwargs: Any) -> List[Dict[str, Any]]:
        query_calls.append(kwargs)
        return [
            {"name": "child-1", "zone": "us-central1-a"},   # known -- no drift
            {"name": "jarvis-prime-failover-orphan", "zone": "us-west1-b"},
        ]

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(bl.teardown_family(
            "root", manifest=manifest,
            delete_instance_fn=fake.delete_instance,
            label_query_fn=label_query,
        ))

    assert [d["name"] for d in result["drift"]] == ["jarvis-prime-failover-orphan"]
    assert any("DRIFT" in r.message for r in caplog.records), (
        "an unrecorded family member must be LOUD-warned")
    # The query fed the detector, not the deleter: the orphan is NOT deleted.
    assert "jarvis-prime-failover-orphan" not in [n for n, _z in fake.calls]
    # The detector queries by the OWNER label, sanitized.
    assert query_calls == [
        {"label_key": bl.LABEL_OWNER, "label_value": "root"},
    ]


def test_drift_detector_absent_by_default(manifest) -> None:
    _create(manifest, "root")
    fake = _FakeDeleter()
    result = asyncio.run(bl.teardown_family(
        "root", manifest=manifest, delete_instance_fn=fake.delete_instance,
    ))
    assert result["drift"] == []


def test_manifest_walk_only_invariant_marker_present() -> None:
    """Grep-enforceable structural invariant: the teardown must be marked as
    manifest-walk-only (no list-style discovery as a deletion source)."""
    import inspect

    src = inspect.getsource(bl)
    assert "# INVARIANT: manifest-walk-only teardown" in src


# ---------------------------------------------------------------------------
# (e) max_gen across records.
# ---------------------------------------------------------------------------


def test_max_gen_across_records_ignoring_tombstones(manifest) -> None:
    assert manifest.max_gen() == 0, "empty manifest -> gen 0"
    _create(manifest, "brain-g1", gen=1)
    _create(manifest, "brain-g3", gen=3)
    _create(manifest, "brain-g2", gen=2)
    _create(manifest, "no-gen")            # gen=None tolerated
    manifest.record_delete("brain-g3")     # tombstone must NOT regress max_gen
    assert manifest.max_gen() == 3


# ---------------------------------------------------------------------------
# (f) sanitize_label_value edge cases.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    ("already-safe_1", "already-safe_1"),
    ("MyNode.01", "mynode-01"),
    ("Brain Node (v2)", "brain-node--v2-"),
    ("", ""),
    (None, ""),
    (42, "42"),
])
def test_sanitize_label_value_edge_cases(raw, expected) -> None:
    assert bl.sanitize_label_value(raw) == expected


def test_sanitize_label_value_truncates_to_63() -> None:
    out = bl.sanitize_label_value("A" * 100)
    assert len(out) == 63
    assert out == "a" * 63


def test_label_constants_are_gcp_safe() -> None:
    for const in (bl.LABEL_OWNER, bl.LABEL_PARENT, bl.LABEL_GEN):
        assert const == bl.sanitize_label_value(const), (
            "label KEYS must already satisfy the GCP charset")


# ---------------------------------------------------------------------------
# (g) provision_brain composes labels + brain-env via injected fake create fn.
# ---------------------------------------------------------------------------


def test_provision_brain_composes_labels_and_env(monkeypatch) -> None:
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "false")
    monkeypatch.delenv("JARVIS_BRAIN_MTLS_DIR", raising=False)
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", "8443")
    captured: Dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> Tuple[bool, str]:
        captured.update(kwargs)
        return (True, "created:done")

    ok, detail = asyncio.run(bl.provision_brain(
        node_name="jarvis-brain-gen2",
        labels={bl.LABEL_OWNER: "Root.Brain", bl.LABEL_GEN: 2},
        extra_env={"JARVIS_BRAIN_PARENT_NODE": "jarvis-brain-gen1",
                   "JARVIS_BRAIN_GENERATION": "2"},
        create_instance_fn=fake_create,
    ))
    assert ok is True and detail == "created:done"
    assert captured["name"] == "jarvis-brain-gen2"

    # Lineage labels arrive SANITIZED, merged over the base discovery label.
    labels = captured["labels"]
    assert labels["jarvis-role"] == "brain"
    assert labels[bl.LABEL_OWNER] == "root-brain"
    assert labels[bl.LABEL_GEN] == "2"

    # brain-env metadata carries the env-resolved values AND the extra_env fold.
    brain_env = captured["extra_metadata"]["brain-env"]
    assert "JARVIS_BRAIN_WS_PORT=8443" in brain_env
    assert "JARVIS_BRAIN_PARENT_NODE=jarvis-brain-gen1" in brain_env
    assert "JARVIS_BRAIN_GENERATION=2" in brain_env

    # The extracted composition still ships the CPU-only + startup contract.
    assert captured["accelerator_type"] == ""
    assert captured["accelerator_count"] == 0
    assert captured["startup_script"].startswith("#!")
    assert captured["machine_type"] and captured["image_family"]


def test_provision_brain_defaults_are_byte_equivalent_shape(monkeypatch) -> None:
    """labels=None / extra_env=None -> the exact legacy driver composition:
    only the discovery label, no lineage keys in brain-env."""
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "false")
    monkeypatch.delenv("JARVIS_BRAIN_MTLS_DIR", raising=False)
    captured: Dict[str, Any] = {}

    async def fake_create(**kwargs: Any) -> Tuple[bool, str]:
        captured.update(kwargs)
        return (True, "created:done")

    asyncio.run(bl.provision_brain(
        node_name="jarvis-brain-legacy", create_instance_fn=fake_create,
    ))
    assert captured["labels"] == {"jarvis-role": "brain"}
    assert "JARVIS_BRAIN_PARENT_NODE" not in captured["extra_metadata"]["brain-env"]


def test_provision_brain_tls_failclosed_before_create(monkeypatch, tmp_path) -> None:
    """TLS enabled + material missing -> TLSMaterialError BEFORE the create fn
    is ever called ($0 fail-closed, preserved through the extraction)."""
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "true")
    monkeypatch.setenv("JARVIS_BRAIN_MTLS_DIR", str(empty))
    calls: List[Dict[str, Any]] = []

    async def fake_create(**kwargs: Any) -> Tuple[bool, str]:
        calls.append(kwargs)
        return (True, "created:done")

    with pytest.raises(bl.TLSMaterialError):
        asyncio.run(bl.provision_brain(
            node_name="jarvis-brain-x", create_instance_fn=fake_create,
        ))
    assert calls == [], "no create may be attempted on a failed TLS preflight"


# ---------------------------------------------------------------------------
# (h) failover_lifecycle lineage fold from brain-env (additive).
# ---------------------------------------------------------------------------


def test_failover_lineage_labels_from_env(monkeypatch) -> None:
    from backend.core.ouroboros.governance import failover_lifecycle as fl

    monkeypatch.setenv("JARVIS_BRAIN_PARENT_NODE", "Jarvis-Brain-Gen1")
    monkeypatch.setenv("JARVIS_BRAIN_GENERATION", "2")
    assert fl._lineage_labels() == {
        bl.LABEL_PARENT: "jarvis-brain-gen1",
        bl.LABEL_GEN: "2",
    }


def test_failover_lineage_labels_absent_env_is_none(monkeypatch) -> None:
    from backend.core.ouroboros.governance import failover_lifecycle as fl

    monkeypatch.delenv("JARVIS_BRAIN_PARENT_NODE", raising=False)
    monkeypatch.delenv("JARVIS_BRAIN_GENERATION", raising=False)
    assert fl._lineage_labels() is None, (
        "env-absent must stay byte-identical (labels=None)")

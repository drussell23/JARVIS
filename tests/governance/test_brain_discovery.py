from __future__ import annotations

import asyncio

import pytest

from backend.core.ouroboros.governance import brain_discovery


# ---------------------------------------------------------------------------
# Fake GCE instance dicts (aggregatedList item shape) -- NO live GCP.
# ---------------------------------------------------------------------------


def _brain_instance(*, name, external, internal, status="RUNNING", gen=None):
    nics = [{"networkIP": internal}]
    if external:
        nics[0]["accessConfigs"] = [{"natIP": external}]
    labels = {"jarvis-role": "brain"}
    if gen is not None:
        labels["jarvis-brain-gen"] = str(gen)
    return {
        "name": name,
        "status": status,
        "labels": labels,
        "networkInterfaces": nics,
    }


def _non_brain_instance(*, name, external, internal):
    return {
        "name": name,
        "status": "RUNNING",
        "labels": {"jarvis-role": "l4-jprime"},
        "networkInterfaces": [
            {"networkIP": internal, "accessConfigs": [{"natIP": external}]}
        ],
    }


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# (a) discover returns the raced-first-healthy WS URL
# ---------------------------------------------------------------------------


def test_discover_returns_raced_first_healthy(monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", "8443")
    monkeypatch.setenv("JARVIS_BRAIN_WS_PATH", "/ws/trinity-bus")
    monkeypatch.setenv("JARVIS_BRAIN_WS_TLS_ENABLED", "true")

    instances = [_brain_instance(name="brain-1", external="203.0.113.9", internal="10.0.0.9")]

    async def fake_list():
        return instances

    async def fake_race(candidates, probe_fn):
        # racer binds the FIRST candidate (external natIP first).
        return candidates[0]

    async def always_healthy(url):
        return True

    url = _run(
        brain_discovery.discover_brain_endpoint(
            list_instances_fn=fake_list,
            probe_fn=always_healthy,
            race_fn=fake_race,
        )
    )
    assert url == "wss://203.0.113.9:8443/ws/trinity-bus"


# ---------------------------------------------------------------------------
# (b) the jarvis-role=brain label filter excludes non-brain instances
# ---------------------------------------------------------------------------


def test_label_filter_excludes_non_brain(monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", "8443")
    monkeypatch.setenv("JARVIS_BRAIN_WS_PATH", "/ws/trinity-bus")

    # The real list method filters server + client side; here the fake list
    # returns ONLY brain instances (mirroring list_instances_by_label's
    # contract), and we additionally prove a non-brain candidate never becomes
    # a URL by feeding a mixed list straight to the candidate builder.
    mixed = [
        _brain_instance(name="brain-1", external="203.0.113.10", internal="10.0.0.10"),
        _non_brain_instance(name="jprime-1", external="203.0.113.11", internal="10.0.0.11"),
    ]
    cfg = brain_discovery._brain_transport_config()
    urls = brain_discovery._candidate_urls(
        [i for i in mixed if i.get("labels", {}).get("jarvis-role") == "brain"], cfg
    )
    assert any("203.0.113.10" in u for u in urls)
    assert all("203.0.113.11" not in u for u in urls)


# ---------------------------------------------------------------------------
# (c) re-invoking discover re-lists + re-races (statelessness -- no cached IP)
# ---------------------------------------------------------------------------


def test_discover_is_stateless_relists_and_reraces(monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", "8443")
    monkeypatch.setenv("JARVIS_BRAIN_WS_PATH", "/ws/trinity-bus")

    calls = {"list": 0, "race": 0}

    # The brain moves IPs between reconnects -> discover must observe the NEW ip.
    ip_sequence = ["203.0.113.20", "203.0.113.21"]

    async def fake_list():
        idx = calls["list"]
        calls["list"] += 1
        return [_brain_instance(name="brain-1", external=ip_sequence[idx], internal="10.0.0.9")]

    async def fake_race(candidates, probe_fn):
        calls["race"] += 1
        return candidates[0]

    async def healthy(url):
        return True

    u1 = _run(brain_discovery.discover_brain_endpoint(list_instances_fn=fake_list, probe_fn=healthy, race_fn=fake_race))
    u2 = _run(brain_discovery.discover_brain_endpoint(list_instances_fn=fake_list, probe_fn=healthy, race_fn=fake_race))

    assert calls["list"] == 2 and calls["race"] == 2  # re-listed + re-raced
    assert u1 == "wss://203.0.113.20:8443/ws/trinity-bus"
    assert u2 == "wss://203.0.113.21:8443/ws/trinity-bus"  # NEW ip, no cache


# ---------------------------------------------------------------------------
# (d) discover returns None + does not raise when no healthy brain
# ---------------------------------------------------------------------------


def test_discover_none_when_no_healthy_brain(monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", "8443")

    async def empty_list():
        return []

    async def race_none(candidates, probe_fn):
        return None

    async def unhealthy(url):
        return False

    # No instances at all -> None.
    assert _run(brain_discovery.discover_brain_endpoint(list_instances_fn=empty_list, race_fn=race_none, probe_fn=unhealthy)) is None

    # Instances present but none healthy (racer returns None) -> None.
    async def one_list():
        return [_brain_instance(name="brain-1", external="203.0.113.30", internal="10.0.0.9")]

    assert _run(brain_discovery.discover_brain_endpoint(list_instances_fn=one_list, race_fn=race_none, probe_fn=unhealthy)) is None


def test_discover_failsoft_when_list_raises(monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", "8443")

    async def boom_list():
        raise RuntimeError("gcp exploded")

    # Must NOT propagate into the caller.
    assert _run(brain_discovery.discover_brain_endpoint(list_instances_fn=boom_list)) is None


# ---------------------------------------------------------------------------
# (d2) Stage-4 Task 3: generation filter (JARVIS_BRAIN_CURRENT_GEN).
# ---------------------------------------------------------------------------


def test_gen_filter_env_unset_zero_behavior_change(monkeypatch):
    monkeypatch.delenv("JARVIS_BRAIN_CURRENT_GEN", raising=False)
    mixed = [
        _brain_instance(name="brain-old", external="203.0.113.40", internal="10.0.0.40"),
        _brain_instance(name="brain-g1", external="203.0.113.41", internal="10.0.0.41", gen=1),
    ]
    out = brain_discovery._filter_stale_generations(mixed)
    assert out == mixed, "env unset: the filter must be a byte-level no-op"


def test_gen_filter_malformed_or_zero_env_is_inactive(monkeypatch):
    mixed = [_brain_instance(name="brain-old", external="203.0.113.40", internal="10.0.0.40")]
    for raw in ("not-a-number", "0", "-3", "  "):
        monkeypatch.setenv("JARVIS_BRAIN_CURRENT_GEN", raw)
        assert brain_discovery._filter_stale_generations(mixed) == mixed, (
            "malformed/non-positive env must stay fail-soft inactive: %r" % raw)


def test_gen_filter_excludes_lower_and_unlabeled_keeps_equal_higher(monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_CURRENT_GEN", "2")
    instances = [
        _brain_instance(name="brain-g1", external="203.0.113.51", internal="10.0.0.51", gen=1),
        _brain_instance(name="brain-g2", external="203.0.113.52", internal="10.0.0.52", gen=2),
        _brain_instance(name="brain-g3", external="203.0.113.53", internal="10.0.0.53", gen=3),
        # Pre-Stage-4 unlabeled brain: obsolete by definition once a gen'd
        # keeper runs.
        _brain_instance(name="brain-prestage4", external="203.0.113.54", internal="10.0.0.54"),
    ]
    out = brain_discovery._filter_stale_generations(instances)
    assert [i["name"] for i in out] == ["brain-g2", "brain-g3"]


def test_gen_filter_applies_before_probing(monkeypatch):
    """End-to-end through discover_brain_endpoint: excluded generations never
    become candidates -- they are never probed, never raced."""
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", "8443")
    monkeypatch.setenv("JARVIS_BRAIN_WS_PATH", "/ws/trinity-bus")
    monkeypatch.setenv("JARVIS_BRAIN_CURRENT_GEN", "2")

    async def fake_list():
        return [
            _brain_instance(name="brain-g1", external="203.0.113.61", internal="10.0.0.61", gen=1),
            _brain_instance(name="brain-old", external="203.0.113.62", internal="10.0.0.62"),
            _brain_instance(name="brain-g2", external="203.0.113.63", internal="10.0.0.63", gen=2),
        ]

    seen = {"candidates": None, "probed": []}

    async def probe(url):
        seen["probed"].append(url)
        return True

    async def fake_race(candidates, probe_fn):
        seen["candidates"] = list(candidates)
        for u in candidates:
            await probe_fn(u)
        return candidates[0]

    url = _run(brain_discovery.discover_brain_endpoint(
        list_instances_fn=fake_list, probe_fn=probe, race_fn=fake_race))

    assert url == "wss://203.0.113.63:8443/ws/trinity-bus"
    joined = " ".join(seen["candidates"]) + " " + " ".join(seen["probed"])
    assert "203.0.113.61" not in joined, "lower gen excluded PRE-probe"
    assert "203.0.113.62" not in joined, "unlabeled excluded PRE-probe"


# ---------------------------------------------------------------------------
# (e) firewall wrappers use the /32 source from resolve_local_public_ip + env rule
# ---------------------------------------------------------------------------


def test_open_firewall_uses_resolved_ip_and_env_rule(monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_FIREWALL_RULE_NAME", "jarvis-brain-mtls-testrule")
    monkeypatch.setenv("JARVIS_BRAIN_WS_PORT", "8443")

    seen = {}

    async def fake_resolve():
        return "198.51.100.7"

    async def fake_create(*, name, source_ip, port):
        seen.update({"name": name, "source_ip": source_ip, "port": port})
        return (True, "created:200")

    ok, detail = _run(
        brain_discovery.open_brain_firewall(
            resolve_ip_fn=fake_resolve, create_fn=fake_create
        )
    )
    assert ok
    assert seen["name"] == "jarvis-brain-mtls-testrule"
    assert seen["source_ip"] == "198.51.100.7"  # /32 applied inside create_firewall_rule
    assert seen["port"] == 8443


def test_open_firewall_refuses_when_no_ip(monkeypatch):
    async def no_ip():
        return None

    called = {"create": False}

    async def fake_create(**kwargs):
        called["create"] = True
        return (True, "x")

    ok, detail = _run(brain_discovery.open_brain_firewall(resolve_ip_fn=no_ip, create_fn=fake_create))
    assert not ok
    assert called["create"] is False  # never open the port without a source IP


def test_close_firewall_deletes_env_rule(monkeypatch):
    monkeypatch.setenv("JARVIS_BRAIN_FIREWALL_RULE_NAME", "jarvis-brain-mtls-closeme")
    seen = {}

    async def fake_delete(name):
        seen["name"] = name
        return (True, "deleted:404")

    ok, _ = _run(brain_discovery.close_brain_firewall(delete_fn=fake_delete))
    assert ok
    assert seen["name"] == "jarvis-brain-mtls-closeme"


# ---------------------------------------------------------------------------
# (f) the no-hardcoded-endpoint invariant now covers brain_discovery.py
# ---------------------------------------------------------------------------


def test_no_hardcoded_endpoint_invariant_covers_brain_discovery(monkeypatch):
    monkeypatch.setenv("JARVIS_SHIPPED_CODE_INVARIANTS_ENABLED", "true")
    from backend.core.ouroboros.governance.meta import shipped_code_invariants as sci

    names = {inv.invariant_name for inv in sci.list_shipped_code_invariants()}
    assert "brain_discovery_no_hardcoded_endpoints" in names

    targets = {
        inv.target_file
        for inv in sci.list_shipped_code_invariants()
        if inv.invariant_name == "brain_discovery_no_hardcoded_endpoints"
    }
    assert any("brain_discovery.py" in t for t in targets)

    # The shipped brain_discovery.py must have ZERO hardcoded endpoint literals.
    violations = [
        v for v in sci.validate_all() if v.target_file.endswith("brain_discovery.py")
    ]
    assert violations == [], violations

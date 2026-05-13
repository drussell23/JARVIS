"""
Stage 1.6 Slice 1 — ParkedOpStore + ParkSignal substrate spine.

Closes the operator binding 2026-05-13:

    "At GENERATE entry (or the narrowest await boundary that already
     exists), the op must release BG pool occupancy while waiting on
     provider I/O, without losing single-flight / op identity /
     cancellation semantics."

Slice 1 is substrate-only — no orchestrator integration, no BG-pool
wiring, no behavioral change at runtime (master flag default-FALSE).
This spine pins:

  * Closed taxonomies (status, enum value, descriptor kind)
  * Single-flight admission semantics
  * Terminal-flip-once idempotency
  * TTL prune + LRU evict invariants
  * Authority-free composition (no orchestrator/pool/ledger calls)
  * §33.5 lossless roundtrip on ParkedOpResult
  * FlagRegistry seed presence + §33.1 default-FALSE master
  * Module-level singleton shape

Slice 2 (orchestrator wiring) will land additional integration spine
under ``test_bg_park_integration.py``.

This spine is async-only (no pytest-trio) and uses ``asyncio.run`` per
test to keep the loop fresh.  Singleton state is reset between tests
via ``reset_default_store()``.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from backend.core.ouroboros.governance.ledger import OperationState
from backend.core.ouroboros.governance.op_park_store import (
    ParkedOpResult,
    ParkedOpStore,
    get_default_store,
    park_enabled,
    reset_default_store,
)
from backend.core.ouroboros.governance.park_signal import (
    ParkDescriptor,
    ParkSignal,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PARK_STORE_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "op_park_store.py"
)
_PARK_SIGNAL_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "park_signal.py"
)
_SEED_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance"
    / "flag_registry_seed.py"
)
_LEDGER_SRC = (
    Path(__file__).parents[2]
    / "backend" / "core" / "ouroboros" / "governance" / "ledger.py"
)


def _enable_master(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the master flag on for the duration of one test."""
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", "true")


def _make_descriptor(kind: str = "generate", **payload) -> ParkDescriptor:
    return ParkDescriptor(kind=kind, payload=payload)


# ---------------------------------------------------------------------------
# Enum + closed-taxonomy pins
# ---------------------------------------------------------------------------


def test_operation_state_has_parked_generate_value():
    """The ledger enum is single source of truth for the park state name."""
    assert OperationState.PARKED_GENERATE.value == "parked_generate", (
        "OperationState.PARKED_GENERATE must serialize as 'parked_generate' "
        "— ledger replay + dedup key + cross-process audit all depend on "
        "this exact string."
    )


def test_parked_op_result_status_taxonomy_is_closed():
    """Result status enum must be the documented 5 values, no others."""
    valid = {"pending", "completed", "cancelled", "ttl_expired", "evicted"}
    # Happy path: each accepted
    for status in valid:
        r = ParkedOpResult(status=status)
        assert r.status == status
    # Drift: anything outside the table is rejected
    with pytest.raises(ValueError, match="ParkedOpResult.status"):
        ParkedOpResult(status="bogus")


def test_parked_op_result_roundtrip_lossless():
    """§33.5 to_dict/from_dict roundtrip preserves the full payload."""
    src = ParkedOpResult(
        status="completed",
        payload={"a": 1, "nested": {"b": [2, 3]}},
        reason="all-good",
    )
    rt = ParkedOpResult.from_dict(src.to_dict())
    assert rt.status == src.status
    assert dict(rt.payload) == dict(src.payload)
    assert rt.reason == src.reason


# ---------------------------------------------------------------------------
# ParkSignal frozen contract
# ---------------------------------------------------------------------------


def test_park_signal_is_frozen_dataclass():
    """ParkSignal MUST be frozen — produced by GENERATE, consumed by BG pool."""
    sig = ParkSignal(
        op_id="op-test",
        token="op-test::attempt-1",
        attempt_seq=1,
        descriptor=_make_descriptor(),
        park_started_at=0.0,
    )
    with pytest.raises(Exception):  # FrozenInstanceError or AttributeError
        sig.op_id = "mutated"  # type: ignore[misc]


def test_park_descriptor_is_frozen_dataclass():
    """ParkDescriptor MUST be frozen — read by resume continuation only."""
    d = _make_descriptor(kind="generate", prompt="hello")
    with pytest.raises(Exception):
        d.kind = "mutated"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Token shape
# ---------------------------------------------------------------------------


def test_token_shape_is_deterministic_in_op_id_and_attempt_seq():
    """Tokens must be derived purely from (op_id, attempt_seq)."""
    assert ParkedOpStore.make_token("op-abc", 1) == "op-abc::attempt-1"
    assert ParkedOpStore.make_token("op-abc", 7) == "op-abc::attempt-7"
    # Stable across calls
    assert ParkedOpStore.make_token("x", 2) == ParkedOpStore.make_token("x", 2)


def test_token_rejects_empty_op_id():
    with pytest.raises(ValueError, match="op_id"):
        ParkedOpStore.make_token("", 1)


def test_token_rejects_non_positive_attempt_seq():
    with pytest.raises(ValueError, match="attempt_seq"):
        ParkedOpStore.make_token("op-x", 0)
    with pytest.raises(ValueError, match="attempt_seq"):
        ParkedOpStore.make_token("op-x", -3)


# ---------------------------------------------------------------------------
# Master flag gating — operator §33.1 default-FALSE discipline
# ---------------------------------------------------------------------------


def test_park_enabled_default_false():
    """Master flag MUST default false per §33.1 for new substrate."""
    # Don't monkeypatch — verify the bare default at module level.
    import os
    assert "JARVIS_BG_PARK_ENABLED" not in os.environ or \
        os.environ["JARVIS_BG_PARK_ENABLED"].strip().lower() not in {
            "true", "1", "yes", "on",
        }, (
            "Test pollution: another test left JARVIS_BG_PARK_ENABLED=true "
            "in the environment."
        )
    assert park_enabled() is False


@pytest.mark.parametrize("val,expected", [
    ("true", True), ("TRUE", True), ("True", True),
    ("1", True), ("yes", True), ("on", True),
    ("false", False), ("0", False), ("no", False), ("", False),
    ("garbage", False),
])
def test_park_enabled_parses_known_truthy_set(
    monkeypatch: pytest.MonkeyPatch, val: str, expected: bool,
):
    monkeypatch.setenv("JARVIS_BG_PARK_ENABLED", val)
    assert park_enabled() is expected


def test_park_raises_when_master_flag_off():
    """park() MUST refuse to admit when master is off — no silent succeed."""
    store = ParkedOpStore()

    async def _try():
        with pytest.raises(RuntimeError, match="master flag off"):
            await store.park("op-x", 1, _make_descriptor())

    asyncio.run(_try())


# ---------------------------------------------------------------------------
# park() admission semantics
# ---------------------------------------------------------------------------


def test_park_admits_fresh_record(monkeypatch: pytest.MonkeyPatch):
    _enable_master(monkeypatch)
    store = ParkedOpStore()

    async def _go():
        token, fresh = await store.park(
            "op-abc", 1, _make_descriptor(kind="generate"),
        )
        assert token == "op-abc::attempt-1"
        assert fresh is True
        assert await store.size() == 1
        assert await store.is_parked(token) is True

    asyncio.run(_go())


def test_park_is_idempotent_on_same_key(monkeypatch: pytest.MonkeyPatch):
    """Single-flight invariant: re-park returns existing token, fresh=False."""
    _enable_master(monkeypatch)
    store = ParkedOpStore()

    async def _go():
        t1, f1 = await store.park("op-x", 1, _make_descriptor())
        t2, f2 = await store.park("op-x", 1, _make_descriptor(kind="other"))
        assert t1 == t2
        assert f1 is True
        assert f2 is False
        # Store size MUST stay 1 — no duplicate admission
        assert await store.size() == 1

    asyncio.run(_go())


def test_park_different_attempts_admit_separately(
    monkeypatch: pytest.MonkeyPatch,
):
    """GENERATE_RETRY produces a new attempt_seq → new park record."""
    _enable_master(monkeypatch)
    store = ParkedOpStore()

    async def _go():
        t1, f1 = await store.park("op-x", 1, _make_descriptor())
        t2, f2 = await store.park("op-x", 2, _make_descriptor())
        assert t1 != t2
        assert f1 is True
        assert f2 is True
        assert await store.size() == 2

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# complete() / cancel() terminal-flip semantics
# ---------------------------------------------------------------------------


def test_complete_flips_event_and_returns_result(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable_master(monkeypatch)
    store = ParkedOpStore()

    async def _go():
        token, _ = await store.park("op-x", 1, _make_descriptor())
        flipped = await store.complete(
            token, payload={"response": "hello world"},
        )
        assert flipped is True
        result = await store.result_for(token)
        assert result is not None
        assert result.status == "completed"
        assert dict(result.payload) == {"response": "hello world"}

    asyncio.run(_go())


def test_complete_is_idempotent(monkeypatch: pytest.MonkeyPatch):
    """Second complete() call MUST be a no-op (no double-dispatch)."""
    _enable_master(monkeypatch)
    store = ParkedOpStore()

    async def _go():
        token, _ = await store.park("op-x", 1, _make_descriptor())
        assert await store.complete(token, payload={"v": 1}) is True
        assert await store.complete(token, payload={"v": 2}) is False
        result = await store.result_for(token)
        # First write wins (single-flight terminal)
        assert dict(result.payload) == {"v": 1}

    asyncio.run(_go())


def test_complete_on_unknown_token_is_noop(monkeypatch: pytest.MonkeyPatch):
    _enable_master(monkeypatch)
    store = ParkedOpStore()

    async def _go():
        flipped = await store.complete("op-nonexistent::attempt-1")
        assert flipped is False

    asyncio.run(_go())


def test_cancel_flips_event_with_cancelled_status(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable_master(monkeypatch)
    store = ParkedOpStore()

    async def _go():
        token, _ = await store.park("op-x", 1, _make_descriptor())
        flipped = await store.cancel(token, reason="user_requested")
        assert flipped is True
        result = await store.result_for(token)
        assert result.status == "cancelled"
        assert result.reason == "user_requested"

    asyncio.run(_go())


def test_cancel_after_complete_is_noop(monkeypatch: pytest.MonkeyPatch):
    """Terminal-flip-once — cancel after complete preserves the completion."""
    _enable_master(monkeypatch)
    store = ParkedOpStore()

    async def _go():
        token, _ = await store.park("op-x", 1, _make_descriptor())
        await store.complete(token, payload={"v": 1})
        flipped = await store.cancel(token, reason="too_late")
        assert flipped is False
        result = await store.result_for(token)
        assert result.status == "completed"

    asyncio.run(_go())


def test_result_for_unknown_token_returns_none(
    monkeypatch: pytest.MonkeyPatch,
):
    _enable_master(monkeypatch)
    store = ParkedOpStore()

    async def _go():
        result = await store.result_for("never-parked::attempt-1")
        assert result is None

    asyncio.run(_go())


def test_result_for_blocks_until_complete(monkeypatch: pytest.MonkeyPatch):
    """result_for awaits the Event — the unlock happens via complete()."""
    _enable_master(monkeypatch)
    store = ParkedOpStore()

    async def _waiter(token: str):
        return await store.result_for(token)

    async def _go():
        token, _ = await store.park("op-x", 1, _make_descriptor())
        # Kick the waiter; it should NOT complete before we call complete()
        wait_task = asyncio.create_task(_waiter(token))
        await asyncio.sleep(0.05)
        assert not wait_task.done(), (
            "result_for must block until terminal flip"
        )
        await store.complete(token, payload={"x": 1})
        result = await asyncio.wait_for(wait_task, timeout=1.0)
        assert result is not None
        assert result.status == "completed"

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# TTL prune + LRU eviction
# ---------------------------------------------------------------------------


def test_ttl_prune_reaps_stale_records(monkeypatch: pytest.MonkeyPatch):
    """Records older than TTL flip to status=ttl_expired."""
    _enable_master(monkeypatch)
    # 0.05s TTL — fast test, real value defaults to 1800s
    monkeypatch.setenv("JARVIS_BG_PARK_TTL_S", "0.05")
    store = ParkedOpStore()

    async def _go():
        token, _ = await store.park("op-x", 1, _make_descriptor())
        await asyncio.sleep(0.15)
        # Triggering park() OR explicit prune both reap
        reaped = await store.prune_stale()
        assert reaped == 1
        # The reaped record's awaiter must unblock with status=ttl_expired
        # NOTE: after prune, the record is dropped from the dict, so
        # result_for returns None (caller mis-ordered).  This is the
        # intended contract — TTL is for ABANDONED parks where the
        # resume continuation died.  Live awaiters get their Event
        # flipped to ttl_expired by _prune_stale_locked() BEFORE the
        # record is dropped.
        # Re-park the same key to verify the slot is reclaimed:
        token2, fresh = await store.park("op-x", 1, _make_descriptor())
        assert fresh is True
        assert token2 == token  # same shape, brand-new record

    asyncio.run(_go())


def test_ttl_prune_unblocks_live_awaiter(monkeypatch: pytest.MonkeyPatch):
    """An awaiter on a parked op MUST unblock when TTL fires."""
    _enable_master(monkeypatch)
    monkeypatch.setenv("JARVIS_BG_PARK_TTL_S", "0.05")
    store = ParkedOpStore()

    async def _go():
        token, _ = await store.park("op-x", 1, _make_descriptor())
        # Start awaiter, then let TTL expire, then trigger prune
        wait_task = asyncio.create_task(store.result_for(token))
        await asyncio.sleep(0.15)
        await store.prune_stale()
        # Awaiter unblocks; the record was dropped after the Event flip,
        # so result_for's second-look returns None — operator-visible
        # signal that the parked op died.
        result = await asyncio.wait_for(wait_task, timeout=1.0)
        assert result is None

    asyncio.run(_go())


def test_lru_evict_when_at_capacity(monkeypatch: pytest.MonkeyPatch):
    """At capacity, the oldest non-terminal record is evicted."""
    _enable_master(monkeypatch)
    monkeypatch.setenv("JARVIS_BG_PARK_STORE_MAX_SIZE", "2")
    store = ParkedOpStore()

    async def _go():
        t1, _ = await store.park("op-1", 1, _make_descriptor())
        # Force monotonic ordering for the LRU pick
        await asyncio.sleep(0.01)
        t2, _ = await store.park("op-2", 1, _make_descriptor())
        await asyncio.sleep(0.01)
        # Awaiter on the oldest before eviction
        wait_task = asyncio.create_task(store.result_for(t1))
        # Force eviction
        t3, _ = await store.park("op-3", 1, _make_descriptor())
        # Oldest (t1) MUST have been evicted
        assert await store.size() == 2
        # The awaiter MUST unblock with status=evicted
        result = await asyncio.wait_for(wait_task, timeout=1.0)
        assert result is None  # record dropped, second-look None
        # t2 + t3 still parked
        assert await store.is_parked(t2)
        assert await store.is_parked(t3)

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# reset() unblocks awaiters
# ---------------------------------------------------------------------------


def test_reset_unblocks_all_awaiters(monkeypatch: pytest.MonkeyPatch):
    _enable_master(monkeypatch)
    store = ParkedOpStore()

    async def _go():
        t1, _ = await store.park("op-1", 1, _make_descriptor())
        t2, _ = await store.park("op-2", 1, _make_descriptor())
        w1 = asyncio.create_task(store.result_for(t1))
        w2 = asyncio.create_task(store.result_for(t2))
        await asyncio.sleep(0.05)
        await store.reset()
        # Both awaiters MUST unblock with status=cancelled reason=store_reset
        r1 = await asyncio.wait_for(w1, timeout=1.0)
        r2 = await asyncio.wait_for(w2, timeout=1.0)
        # After reset the records are dropped; result_for second-look None
        assert r1 is None
        assert r2 is None
        assert await store.size() == 0

    asyncio.run(_go())


# ---------------------------------------------------------------------------
# Module-level singleton — get_default_store / reset_default_store
# ---------------------------------------------------------------------------


def test_get_default_store_returns_singleton():
    reset_default_store()
    a = get_default_store()
    b = get_default_store()
    assert a is b


def test_reset_default_store_yields_fresh_instance():
    a = get_default_store()
    reset_default_store()
    b = get_default_store()
    assert a is not b


# ---------------------------------------------------------------------------
# FlagRegistry seed presence (operator §33.1 + §33 discipline)
# ---------------------------------------------------------------------------


def test_seed_has_park_master_flag_default_false():
    """JARVIS_BG_PARK_ENABLED MUST exist in seed AND default to False."""
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_BG_PARK_ENABLED" in src, (
        "FlagRegistry must seed JARVIS_BG_PARK_ENABLED so operators "
        "can /help flag and toggle without grepping the codebase."
    )
    idx = src.find("JARVIS_BG_PARK_ENABLED")
    # Default must be False — §33.1 — within a tight window around the name
    window = src[idx:idx + 1200]
    assert "default=False" in window, (
        "JARVIS_BG_PARK_ENABLED FlagSpec MUST default to False per §33.1 "
        "until Slice 2 wiring + Slice 3 soak graduate the flag."
    )
    assert "Category.SAFETY" in window, (
        "Master kill switches must be Category.SAFETY for the /help "
        "posture filter to surface them correctly."
    )


def test_seed_has_park_ttl_and_size_knobs():
    src = _SEED_SRC.read_text(encoding="utf-8")
    assert "JARVIS_BG_PARK_TTL_S" in src
    assert "JARVIS_BG_PARK_STORE_MAX_SIZE" in src


# ---------------------------------------------------------------------------
# Authority-invariant AST pins — substrate composes, does not call
# ---------------------------------------------------------------------------


def test_ast_pin_op_park_store_has_no_authority_imports():
    """The store MUST NOT import orchestrator / pool / change_engine.

    Authority discipline: the store is passive data with single-flight
    admission.  It cannot reach back into the orchestrator or pool.
    """
    src = _PARK_STORE_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    forbidden_module_substrings = (
        "orchestrator",
        "background_agent_pool",
        "change_engine",
        "candidate_generator",
        "phase_runners",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for bad in forbidden_module_substrings:
                    assert bad not in alias.name, (
                        f"op_park_store.py must not import {alias.name!r} "
                        f"— substrate authority-invariant breach"
                    )
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            for bad in forbidden_module_substrings:
                assert bad not in mod, (
                    f"op_park_store.py must not import-from {mod!r} "
                    f"— substrate authority-invariant breach"
                )


def test_ast_pin_park_signal_imports_only_stdlib():
    """park_signal.py MUST import only dataclasses + typing.

    The signal is the contract between GENERATE and the BG pool; any
    backend import here would couple producer and consumer to the
    substrate module set.
    """
    src = _PARK_SIGNAL_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    allowed_modules = {"dataclasses", "typing", "__future__"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert (node.module or "") in allowed_modules, (
                f"park_signal.py must not import-from {node.module!r} — "
                f"contract module must stay stdlib-only"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name in allowed_modules, (
                    f"park_signal.py must not import {alias.name!r} — "
                    f"contract module must stay stdlib-only"
                )


def test_ast_pin_ledger_imports_only_enum_value():
    """op_park_store imports the canonical OperationState enum, not its own.

    Single source of truth — PARKED_GENERATE lives in ledger.py.
    """
    src = _PARK_STORE_SRC.read_text(encoding="utf-8")
    assert "from backend.core.ouroboros.governance.ledger import OperationState" in src, (
        "op_park_store.py MUST import OperationState from ledger.py — "
        "the enum is single source of truth for the park state name"
    )


def test_ast_pin_master_flag_required_before_park():
    """park() MUST gate on park_enabled() before admitting.

    The runtime check sits in the function body; this AST pin makes the
    invariant grep-resistant against future refactors.
    """
    src = _PARK_STORE_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    park_fns = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "park"
    ]
    assert park_fns, "op_park_store.py must expose async def park()"
    park_fn = park_fns[0]
    # Skip the docstring (an ast.Expr whose .value is a string Constant)
    # so the gate stays the first EXECUTABLE statement regardless of
    # whether the docstring grows.
    body = list(park_fn.body)
    if body and isinstance(body[0], ast.Expr) \
            and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    assert body, "park() must have at least one executable statement"
    first_stmt = body[0]
    assert isinstance(first_stmt, ast.If), (
        "park()'s first executable statement MUST be the master-flag "
        "guard `if not park_enabled(): raise ...` — nothing may execute "
        "before the gate (no logging, no time reads, no list builds)"
    )
    # Drill in: the test condition must reference park_enabled
    cond_src = ast.unparse(first_stmt.test)
    assert "park_enabled" in cond_src, (
        f"park()'s gate condition must call park_enabled(); got: {cond_src!r}"
    )


def test_ast_pin_seed_default_false_for_master():
    """AST-level pin (not just substring) — master flag FlagSpec is BOOL=False."""
    src = _SEED_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    found_master = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "FlagSpec":
            name_kw = None
            default_kw = None
            for kw in node.keywords:
                if kw.arg == "name" and isinstance(kw.value, ast.Constant):
                    name_kw = kw.value.value
                elif kw.arg == "default":
                    default_kw = kw.value
            if name_kw == "JARVIS_BG_PARK_ENABLED":
                found_master = True
                assert isinstance(default_kw, ast.Constant) \
                    and default_kw.value is False, (
                        "JARVIS_BG_PARK_ENABLED FlagSpec default MUST be "
                        "the literal Python `False` per §33.1"
                    )
    assert found_master, "JARVIS_BG_PARK_ENABLED FlagSpec must be present in seed list"


# ---------------------------------------------------------------------------
# Ledger enum AST pin — PARKED_GENERATE entry stays additive
# ---------------------------------------------------------------------------


def test_ast_pin_ledger_has_parked_generate():
    """The enum value must live in OperationState — single source of truth."""
    src = _LEDGER_SRC.read_text(encoding="utf-8")
    tree = ast.parse(src)
    op_state_class = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "OperationState":
            op_state_class = node
            break
    assert op_state_class is not None, "OperationState class must exist"
    names = {
        target.id
        for stmt in op_state_class.body
        if isinstance(stmt, ast.Assign)
        for target in stmt.targets
        if isinstance(target, ast.Name)
    }
    assert "PARKED_GENERATE" in names, (
        "OperationState.PARKED_GENERATE MUST be defined as a class-level "
        "assignment in ledger.py — Stage 1.6 substrate contract"
    )

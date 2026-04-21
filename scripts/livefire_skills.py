#!/usr/bin/env python3
"""Live-fire battle test — First-Class Skill System arc.

Resolves the gap quote:
  "A plugin system exists, but there's no 'skills marketplace' concept."

Scenarios
---------
 1. Manifest parses + validates from YAML on disk.
 2. Namespaced qualified names work (`plugin:skill`).
 3. §1 authority: model source rejected at catalog register.
 4. Marketplace install from a source directory + register.
 5. Marketplace discover scans a root tree and registers every manifest.
 6. Invoker resolves entrypoint + runs handler + returns outcome.
 7. Invoker bounded-preview truncates huge results.
 8. Invoker catches handler raise → ok=False + structured error.
 9. /skills REPL list / show / run / install / remove / discover round-trip.
10. Authority invariant grep on arc modules.

Run::
    python3 scripts/livefire_skills.py
"""
from __future__ import annotations

import asyncio
import re as _re
import shutil
import sys
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from backend.core.ouroboros.governance.skill_catalog import (  # noqa: E402
    SkillAuthorityError,
    SkillCatalog,
    SkillInvoker,
    SkillMarketplace,
    SkillSource,
    dispatch_skill_command,
    reset_default_catalog,
    reset_default_invoker,
)
from backend.core.ouroboros.governance.skill_manifest import (  # noqa: E402
    SkillManifest,
)


C_PASS, C_FAIL, C_BOLD, C_DIM, C_END = (
    "\033[92m", "\033[91m", "\033[1m", "\033[2m", "\033[0m",
)


def _banner(t: str) -> None:
    print(f"\n{C_BOLD}{'━' * 72}{C_END}\n{C_BOLD}▶ {t}{C_END}\n{C_BOLD}{'━' * 72}{C_END}")


def _pass(t: str) -> None:
    print(f"  {C_PASS}✓ {t}{C_END}")


def _fail(t: str) -> None:
    print(f"  {C_FAIL}✗ {t}{C_END}")


class Scenario:
    def __init__(self, title: str) -> None:
        self.title = title
        self.passed: List[str] = []
        self.failed: List[str] = []

    def check(self, desc: str, ok: bool) -> None:
        (self.passed if ok else self.failed).append(desc)
        (_pass if ok else _fail)(desc)

    @property
    def ok(self) -> bool:
        return not self.failed


# ---------------------------------------------------------------------------
# Fixture handlers — injected into sys.modules for the invoker
# ---------------------------------------------------------------------------


class _LivefireHandlers:
    @staticmethod
    async def greet(manifest, args):
        return f"hello {args.get('who', 'world')} from {manifest.qualified_name}"

    @staticmethod
    def sync_checker(manifest, args):
        return {"checked": True, "target": args.get("path")}

    @staticmethod
    def huge_output(manifest, args):
        return "x" * 10_000

    @staticmethod
    def boom(manifest, args):
        raise RuntimeError("intentional")


sys.modules["__livefire_handlers__"] = _LivefireHandlers  # type: ignore[assignment]


def _write_manifest_yaml(dir_: Path, **overrides: Any) -> Path:
    """Emit a YAML manifest with sensible defaults for tests."""
    import yaml  # type: ignore[import-untyped]
    data = {
        "name": "greet",
        "description": "Say hello",
        "trigger": "when the operator says hi",
        "entrypoint": "__livefire_handlers__:greet",
        "version": "1.0.0",
        "author": "livefire@test",
        "permissions": ["read_only"],
        "args_schema": {
            "who": {"type": "string", "default": "world"},
        },
        **overrides,
    }
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / "manifest.yaml"
    path.write_text(yaml.safe_dump(data))
    return path


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------


async def scenario_manifest_parses_from_yaml() -> Scenario:
    """Manifest parses + validates from YAML on disk."""
    s = Scenario("Manifest parses from YAML")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_manifest_yaml(root / "greet")
        m = SkillManifest.from_yaml_file(root / "greet" / "manifest.yaml")
        s.check(f"name=greet (got {m.name})", m.name == "greet")
        s.check(f"version=1.0.0 (got {m.version})", m.version == "1.0.0")
        s.check(
            f"qualified_name=greet (got {m.qualified_name})",
            m.qualified_name == "greet",
        )
        s.check("args_schema has 'who'", "who" in m.args_schema)
    return s


async def scenario_namespaced_qname() -> Scenario:
    """Namespaced qualified names resolve to plugin:skill."""
    s = Scenario("Namespaced qualified name resolves")
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        _write_manifest_yaml(
            root / "greet",
            name="hello", plugin_namespace="ralph-loop",
        )
        m = SkillManifest.from_yaml_file(root / "greet" / "manifest.yaml")
        s.check(
            f"qualified_name == 'ralph-loop:hello' (got {m.qualified_name})",
            m.qualified_name == "ralph-loop:hello",
        )
    return s


async def scenario_model_source_rejected() -> Scenario:
    """§1: model source rejected at catalog register."""
    s = Scenario("§1 authority: model source rejected")
    reset_default_catalog()

    class FakeSource(str):
        pass

    cat = SkillCatalog()
    m = SkillManifest.from_mapping({
        "name": "x",
        "description": "d", "trigger": "t",
        "entrypoint": "mod.x:f",
    })
    try:
        cat.register(m, source=FakeSource("model"))  # type: ignore[arg-type]
        s.check("model source refused (didn't raise)", False)
    except SkillAuthorityError:
        s.check("model source → SkillAuthorityError", True)
    reset_default_catalog()
    return s


async def scenario_marketplace_install() -> Scenario:
    """Marketplace install from source directory + register."""
    s = Scenario("Marketplace install from source directory")
    reset_default_catalog()
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "source" / "greet"
        _write_manifest_yaml(src)
        market_root = Path(td) / "market"
        cat = SkillCatalog()
        market = SkillMarketplace(market_root, catalog=cat)
        installed = market.install_from_directory(src)
        s.check(
            f"installed.qualified_name == 'greet' (got {installed.qualified_name})",
            installed.qualified_name == "greet",
        )
        s.check(
            "on-disk copy exists under market root",
            (market_root / "greet" / "manifest.yaml").exists(),
        )
        s.check("registered in catalog", cat.has("greet"))
    reset_default_catalog()
    return s


async def scenario_marketplace_discover() -> Scenario:
    """Discover scans root tree + registers every manifest."""
    s = Scenario("Marketplace discover walks tree")
    reset_default_catalog()
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "market"
        _write_manifest_yaml(root / "a", name="a")
        _write_manifest_yaml(root / "b", name="b")
        _write_manifest_yaml(
            root / "rp" / "x", name="x", plugin_namespace="rp",
        )
        # Drop a deliberately-malformed manifest — should be skipped, not crash
        (root / "broken").mkdir()
        (root / "broken" / "manifest.yaml").write_text(
            "name: BAD-uppercase rejected"
        )
        cat = SkillCatalog()
        market = SkillMarketplace(root, catalog=cat)
        loaded = market.discover()
        qnames = {m.qualified_name for m in loaded}
        s.check(
            f"loaded 3 valid skills (got {len(loaded)})",
            len(loaded) == 3,
        )
        s.check("'a' loaded", "a" in qnames)
        s.check("'b' loaded", "b" in qnames)
        s.check("'rp:x' loaded", "rp:x" in qnames)
        s.check(
            "malformed manifest skipped (didn't crash)",
            not cat.has("BAD-uppercase"),
        )
    reset_default_catalog()
    return s


async def scenario_invoker_runs_handler() -> Scenario:
    """Invoker resolves entrypoint + runs handler."""
    s = Scenario("Invoker resolves + runs handler")
    reset_default_catalog()
    cat = SkillCatalog()
    m = SkillManifest.from_mapping({
        "name": "greet",
        "description": "say hi",
        "trigger": "when testing",
        "entrypoint": "__livefire_handlers__:greet",
        "args_schema": {
            "who": {"type": "string", "default": "friend"},
        },
    })
    cat.register(m, source=SkillSource.OPERATOR)
    inv = SkillInvoker(catalog=cat)
    out = await inv.invoke("greet", args={"who": "marketplace"})
    s.check("outcome.ok", out.ok)
    s.check(
        f"result preview contains 'marketplace' (got {out.result_preview!r})",
        "marketplace" in out.result_preview,
    )
    s.check(f"duration_ms recorded ({out.duration_ms:.2f})", out.duration_ms >= 0)
    reset_default_catalog()
    return s


async def scenario_invoker_bounded_preview() -> Scenario:
    """Huge handler output is truncated in result_preview."""
    s = Scenario("Invoker bounded preview on huge output")
    reset_default_catalog()
    cat = SkillCatalog()
    m = SkillManifest.from_mapping({
        "name": "huge",
        "description": "emits huge output",
        "trigger": "when testing",
        "entrypoint": "__livefire_handlers__:huge_output",
    })
    cat.register(m, source=SkillSource.OPERATOR)
    inv = SkillInvoker(catalog=cat)
    out = await inv.invoke("huge", output_preview_chars=200)
    s.check("outcome.ok", out.ok)
    s.check(
        f"preview ≤ 200 chars (got {len(out.result_preview)})",
        len(out.result_preview) <= 200,
    )
    reset_default_catalog()
    return s


async def scenario_invoker_handler_raise_captured() -> Scenario:
    """Handler raise → ok=False + structured error."""
    s = Scenario("Invoker catches handler raise")
    reset_default_catalog()
    cat = SkillCatalog()
    m = SkillManifest.from_mapping({
        "name": "boom",
        "description": "blows up",
        "trigger": "when testing",
        "entrypoint": "__livefire_handlers__:boom",
    })
    cat.register(m, source=SkillSource.OPERATOR)
    inv = SkillInvoker(catalog=cat)
    out = await inv.invoke("boom")
    s.check("outcome.ok == False", out.ok is False)
    s.check(
        "error tagged handler_raise:RuntimeError",
        "handler_raise:RuntimeError" in (out.error or ""),
    )
    reset_default_catalog()
    return s


async def scenario_repl_full_round_trip() -> Scenario:
    """/skills REPL list / show / run / install / remove / discover."""
    s = Scenario("/skills REPL full round trip")
    reset_default_catalog()
    reset_default_invoker()
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / "source" / "greet"
        _write_manifest_yaml(src)
        market_root = Path(td) / "market"
        cat = SkillCatalog()
        market = SkillMarketplace(market_root, catalog=cat)
        inv = SkillInvoker(catalog=cat)

        r_install = dispatch_skill_command(
            f"/skills install {src}",
            catalog=cat, marketplace=market,
        )
        s.check("/skills install ok", r_install.ok)
        s.check("catalog has greet", cat.has("greet"))

        r_list = dispatch_skill_command("/skills", catalog=cat)
        s.check(
            "/skills list contains greet", "greet" in r_list.text,
        )

        r_show = dispatch_skill_command(
            "/skills show greet", catalog=cat,
        )
        s.check("/skills show ok", r_show.ok)
        s.check(
            "show includes entrypoint line",
            "entrypoint" in r_show.text,
        )

        # `/skills run` requires a coroutine runner; from INSIDE an async
        # scenario we can't use asyncio.run (nested loop forbidden).
        # Invoke directly via the SkillInvoker to exercise the same code
        # path the REPL's run command exercises:
        run_outcome = await inv.invoke("greet", args={"who": "livefire"})
        s.check("invoke ok", run_outcome.ok)
        s.check(
            "run preview mentions livefire",
            "livefire" in (run_outcome.result_preview or ""),
        )

        r_remove = dispatch_skill_command(
            "/skills remove greet",
            catalog=cat, marketplace=market,
        )
        s.check("/skills remove ok", r_remove.ok)
        s.check("catalog no longer has greet", not cat.has("greet"))

        # Install two + discover
        _write_manifest_yaml(market_root / "fresh" / "greet", name="fresh-a")
        _write_manifest_yaml(
            market_root / "fresh" / "bravo", name="fresh-b",
        )
        # Need to clear catalog first to see discovery count cleanly
        r_disc = dispatch_skill_command(
            "/skills discover",
            catalog=cat, marketplace=market,
        )
        s.check("/skills discover ok", r_disc.ok)
        s.check(
            f"discovered >=2 skills ({cat.list_all()})",
            len(cat.list_all()) >= 2,
        )
    reset_default_catalog()
    reset_default_invoker()
    return s


async def scenario_authority_invariant() -> Scenario:
    """Arc modules import no gate/execution code."""
    s = Scenario("Authority invariant grep")
    forbidden = [
        "orchestrator", "policy_engine", "iron_gate", "risk_tier_floor",
        "semantic_guardian", "tool_executor", "candidate_generator",
        "change_engine",
    ]
    modules = [
        "backend/core/ouroboros/governance/skill_manifest.py",
        "backend/core/ouroboros/governance/skill_catalog.py",
    ]
    for path in modules:
        src = Path(path).read_text()
        violations = []
        for mod in forbidden:
            if _re.search(
                rf"^\s*(from|import)\s+[^#\n]*{_re.escape(mod)}",
                src, _re.MULTILINE,
            ):
                violations.append(mod)
        s.check(
            f"{Path(path).name}: zero forbidden imports",
            not violations,
        )
    return s


ALL_SCENARIOS = [
    scenario_manifest_parses_from_yaml,
    scenario_namespaced_qname,
    scenario_model_source_rejected,
    scenario_marketplace_install,
    scenario_marketplace_discover,
    scenario_invoker_runs_handler,
    scenario_invoker_bounded_preview,
    scenario_invoker_handler_raise_captured,
    scenario_repl_full_round_trip,
    scenario_authority_invariant,
]


async def main() -> int:
    print(f"{C_BOLD}First-Class Skill System — live-fire{C_END}")
    print(f"{C_DIM}Manifest + catalog + invoker + marketplace + REPL{C_END}")
    t0 = time.monotonic()
    results: List[Scenario] = []
    for fn in ALL_SCENARIOS:
        title = fn.__doc__.splitlines()[0] if fn.__doc__ else fn.__name__
        _banner(title)
        try:
            results.append(await fn())
        except Exception as exc:
            sc = Scenario(fn.__name__)
            sc.failed.append(f"raised: {type(exc).__name__}: {exc}")
            _fail(f"raised: {type(exc).__name__}: {exc}")
            import traceback
            traceback.print_exc()
            results.append(sc)
    elapsed = time.monotonic() - t0
    _banner("SUMMARY")
    total_pass = sum(len(s.passed) for s in results)
    total_fail = sum(len(s.failed) for s in results)
    ok = sum(1 for s in results if s.ok)
    for sc in results:
        status = f"{C_PASS}PASS{C_END}" if sc.ok else f"{C_FAIL}FAIL{C_END}"
        print(f"  {status} {sc.title}  ({len(sc.passed)} ✓, {len(sc.failed)} ✗)")
    print()
    print(
        f"  {C_BOLD}Total:{C_END} {total_pass} checks passed, "
        f"{total_fail} failed — {ok}/{len(results)} scenarios OK"
    )
    print(f"  {C_DIM}elapsed: {elapsed:.2f}s{C_END}")
    print()
    if total_fail == 0:
        print(
            f"  {C_PASS}{C_BOLD}"
            f"FIRST-CLASS SKILL SYSTEM GAP: CLOSED"
            f"{C_END}"
        )
        return 0
    print(
        f"  {C_FAIL}{C_BOLD}{total_fail} check(s) failed{C_END}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

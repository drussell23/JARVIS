"""Polymorphic Source Router — the Swarm across all file architectures.

Mandated bulletproof: a 50,000-line JSON config. Assert the Factory routes it to
the JSONTreeChunker, isolates a DEEPLY NESTED target key, hands it to the mock
Swarm, and stitches it back WITHOUT corrupting the global JSON structure.

Plus: factory dispatch by extension; a non-Python file never crashes the Python
AST parser; nested-key isolation is exact; YAML key-path + universal text
fallback; and a bad graft is rejected so the whole stays well-formed.
"""

from __future__ import annotations

import json

import pytest

from backend.core.ouroboros.governance.polyglot_chunker import (
    Chunk,
    ChunkerFactory,
    JSONTreeChunker,
    PythonASTChunker,
    RegexIndentationChunker,
    YAMLTreeChunker,
    polyglot_repair,
    polymorphic_extract_target,
)


def _make_50k_json() -> str:
    """A big pretty-printed config with a deeply nested target key."""
    top = {"metadata": {"version": "1.0"}, "services": {}}
    for i in range(6300):   # ~ each service block is several lines → >50k lines
        top["services"][f"svc_{i}"] = {
            "image": f"repo/svc_{i}:latest",
            "replicas": i % 5,
            "env": {"TIER": "batch", "REGION": "us"},
        }
    # The deeply nested target: services.svc_4242.env.TIER
    top["services"]["svc_4242"]["env"]["TIER"] = "batch"
    return json.dumps(top, indent=2)


_BIG_JSON = _make_50k_json()


# ---------------------------------------------------------------------------
# Factory dispatch
# ---------------------------------------------------------------------------


def test_factory_routes_by_extension() -> None:
    assert isinstance(ChunkerFactory.for_file("a/b/config.json"), JSONTreeChunker)
    assert isinstance(ChunkerFactory.for_file("k8s/manifest.yaml"), YAMLTreeChunker)
    assert isinstance(ChunkerFactory.for_file("deploy.yml"), YAMLTreeChunker)
    assert isinstance(ChunkerFactory.for_file("mod.py"), PythonASTChunker)
    # Unknown → universal fallback, NOT the Python parser.
    assert isinstance(ChunkerFactory.for_file("README.md"), RegexIndentationChunker)
    assert isinstance(ChunkerFactory.for_file("notes.txt"), RegexIndentationChunker)
    assert isinstance(ChunkerFactory.for_file("app.tsx"), RegexIndentationChunker)


def test_json_does_not_crash_python_parser() -> None:
    # The whole point: a .json never reaches ast.parse.
    chunk = polymorphic_extract_target(_BIG_JSON, "config.json", "metadata.version")
    assert chunk is not None
    assert chunk.language == "json"


# ---------------------------------------------------------------------------
# The mandated 50k-line JSON deep-key isolate → swarm → stitch
# ---------------------------------------------------------------------------


async def test_50k_json_deep_key_isolated_swarmed_and_stitched() -> None:
    assert _BIG_JSON.count("\n") > 50000, "fixture must be 50k+ lines"

    # (1) Factory routes to the JSONTreeChunker.
    strat = ChunkerFactory.for_file("config.json")
    assert isinstance(strat, JSONTreeChunker)

    # (2) It isolates the deeply nested key exactly.
    target = "services.svc_4242.env.TIER"
    chunk = strat.extract(_BIG_JSON, "config.json", target)
    assert chunk is not None
    assert '"TIER"' in chunk.source_code
    assert '"batch"' in chunk.source_code
    # A surgical, tiny slice of a 50k-line file — not the whole thing.
    assert (chunk.end_line - chunk.start_line) <= 2

    # (3) The mock Swarm repairs ONLY that node (batch → realtime).
    async def agent_fn(c: Chunk) -> str:
        assert c.name == target
        # Return the same entry with the value flipped, indentation preserved.
        return c.source_code.replace('"batch"', '"realtime"')

    result = await polyglot_repair(_BIG_JSON, "config.json", [target], agent_fn)

    assert result.language == "json"
    assert result.succeeded == [target]
    assert result.failed == []

    # (4) The stitched file is STILL valid JSON — no bracket/indent corruption.
    parsed = json.loads(result.stitched)
    assert parsed["services"]["svc_4242"]["env"]["TIER"] == "realtime"   # the fix landed
    # Everything else is byte-intact.
    assert parsed["services"]["svc_4242"]["env"]["REGION"] == "us"
    assert parsed["services"]["svc_0"]["image"] == "repo/svc_0:latest"
    assert parsed["services"]["svc_6299"]["replicas"] == 6299 % 5
    assert parsed["metadata"]["version"] == "1.0"
    assert len(parsed["services"]) == 6300


async def test_json_bad_graft_is_rejected_structure_preserved() -> None:
    target = "metadata.version"

    async def broken_agent(c: Chunk) -> str:
        return '"version": "1.0" }}} OOPS'   # corrupt — would break the JSON

    result = await polyglot_repair(_BIG_JSON, "config.json", [target], broken_agent)
    assert target in result.failed            # graft rejected
    json.loads(result.stitched)               # file STILL parses (atomic invariant)


# ---------------------------------------------------------------------------
# YAML + universal fallback
# ---------------------------------------------------------------------------


def test_yaml_nested_key_isolated() -> None:
    yaml_src = (
        "metadata:\n"
        "  version: '1.0'\n"
        "services:\n"
        "  web:\n"
        "    image: nginx\n"
        "    port: 8080\n"
        "  db:\n"
        "    image: postgres\n"
    )
    chunk = polymorphic_extract_target(yaml_src, "compose.yaml", "services.web.port")
    assert chunk is not None
    assert "port: 8080" in chunk.source_code
    assert chunk.language == "yaml"


def test_text_fallback_line_range_and_anchor() -> None:
    txt = "alpha\nbeta\ngamma\ndelta\n"
    # Line-range target.
    c1 = polymorphic_extract_target(txt, "notes.txt", "2-3")
    assert c1 is not None and c1.source_code == "beta\ngamma\n"
    # Anchor target.
    c2 = polymorphic_extract_target(txt, "notes.md", "delta")
    assert c2 is not None and "delta" in c2.source_code
    # Text always validates (no grammar to corrupt).
    assert RegexIndentationChunker().validate("anything at all {[}") is True

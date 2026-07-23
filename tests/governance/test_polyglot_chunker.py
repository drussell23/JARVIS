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
    from backend.core.ouroboros.governance.polyglot_chunker import TSXChunker
    assert isinstance(ChunkerFactory.for_file("app.tsx"), TSXChunker)
    assert isinstance(ChunkerFactory.for_file("hook.ts"), TSXChunker)
    # Truly-unknown → universal fallback, NOT the Python parser.
    assert isinstance(ChunkerFactory.for_file("README.md"), RegexIndentationChunker)
    assert isinstance(ChunkerFactory.for_file("notes.txt"), RegexIndentationChunker)


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


# ---------------------------------------------------------------------------
# (Mandated 1) TSX Semantic Context Bundling
# ---------------------------------------------------------------------------

_TSX = '''import React, { useState } from "react";
import { fetchUser } from "./api";

interface UserCardProps {
  userId: string;
  compact: boolean;
}

interface Unrelated {
  foo: number;
}

type Theme = "light" | "dark";

export function UserCard(props: UserCardProps) {
  const [open, setOpen] = useState(false);
  const theme: Theme = "light";
  return <div className={theme}>{props.userId}</div>;
}

export function OtherComponent() {
  return <span>hi</span>;
}
'''


async def test_tsx_bundles_target_node_and_required_interface() -> None:
    from backend.core.ouroboros.governance.polyglot_chunker import (
        TSXChunker,
        build_context_bundled_target,
    )

    strat = ChunkerFactory.for_file("components/UserCard.tsx")
    assert isinstance(strat, TSXChunker)

    chunk = strat.extract(_TSX, "UserCard.tsx", "UserCard")
    assert chunk is not None
    # The target node itself.
    assert "function UserCard" in chunk.source_code
    assert 'className={theme}' in chunk.source_code

    # (1) The context header bundles the imports AND the REQUIRED interface/type
    # defs the node references (UserCardProps, Theme) — zero context starvation.
    hdr = chunk.context_header
    assert "interface UserCardProps" in hdr
    assert "type Theme" in hdr
    assert 'import React' in hdr and 'from "./api"' in hdr
    # ...but NOT the unrelated interface the node never references.
    assert "Unrelated" not in hdr

    # The ChunkTarget the swarm consumes carries BOTH node + context (read-only).
    target = build_context_bundled_target(chunk, instruction="fix UserCard")
    assert "UserCardProps" in target.prompt          # required interface present
    assert "READ-ONLY CONTEXT" in target.prompt
    assert target.chunk.source_code == chunk.source_code


# ---------------------------------------------------------------------------
# (Mandated 2) YAML Indentation Lock at Fan-In
# ---------------------------------------------------------------------------


async def test_yaml_indentation_lock_normalizes_llm_whitespace() -> None:
    from backend.core.ouroboros.governance.polyglot_chunker import YAMLTreeChunker

    yaml_src = (
        "root:\n"
        "  services:\n"
        "    web:\n"
        "      image: nginx\n"
        "      port: 8080\n"
        "  other:\n"
        "    keep: true\n"
    )
    strat = YAMLTreeChunker()
    # Deeply nested target at absolute indent 6.
    chunk = strat.extract(yaml_src, "compose.yaml", "root.services.web.port")
    assert chunk is not None
    assert chunk.indent == 6                       # locked baseline captured

    # The "LLM" returns the fix with WRONG leading whitespace (0-indented).
    llm_body = "port: 9090"
    stitched = strat.stitch(yaml_src, chunk, llm_body)
    assert stitched is not None

    # Indentation Lock normalized it to EXACTLY 6 spaces before grafting.
    assert "      port: 9090\n" in stitched
    assert "    port: 9090" not in stitched.replace("      port", "")  # no wrong depth

    # The global YAML tree is intact + valid.
    try:
        import yaml
        parsed = yaml.safe_load(stitched)
        assert parsed["root"]["services"]["web"]["port"] == 9090
        assert parsed["root"]["services"]["web"]["image"] == "nginx"
        assert parsed["root"]["other"]["keep"] is True
    except ImportError:
        assert strat.validate(stitched) is True


async def test_yaml_lock_handles_over_indented_multiline_block() -> None:
    from backend.core.ouroboros.governance.polyglot_chunker import _normalize_to_indent

    # A multi-line block the LLM over-indented by 10 spaces — relative structure
    # must survive, absolute baseline re-locked to 4.
    over = "          web:\n            image: nginx\n            port: 80"
    normalized = _normalize_to_indent(over, 4)
    lines = normalized.split("\n")
    assert lines[0] == "    web:"                  # baseline locked to 4
    assert lines[1] == "      image: nginx"        # relative +2 preserved
    assert lines[2] == "      port: 80"

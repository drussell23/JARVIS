"""Slices 2+3+4 tests — Validator + Extractor + Repair + Renderer + REPL."""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping

import pytest

from backend.core.ouroboros.governance.output_contract import (
    OutputContract,
    OutputFormat,
)
from backend.core.ouroboros.governance.output_validator import (
    OUTPUT_VALIDATOR_SCHEMA_VERSION,
    FormatDispatchResult,
    OutputContractRegistry,
    OutputExtractor,
    OutputRendererRegistry,
    OutputRepairLoop,
    OutputRepairPrompt,
    OutputValidator,
    RenderSurface,
    RepairLoopOutcome,
    ValidationIssue,
    ValidationResult,
    build_repair_prompt,
    dispatch_format_command,
    get_default_contract_registry,
    get_default_renderer_registry,
    reset_default_contract_registry,
    reset_default_renderer_registry,
)


@pytest.fixture(autouse=True)
def _clean():
    reset_default_contract_registry()
    reset_default_renderer_registry()
    yield
    reset_default_contract_registry()
    reset_default_renderer_registry()


# ===========================================================================
# Schema version
# ===========================================================================


def test_schema_version_stable():
    assert OUTPUT_VALIDATOR_SCHEMA_VERSION == "output_validator.v1"


# ===========================================================================
# OutputExtractor — JSON
# ===========================================================================


def test_extract_json_plain():
    assert OutputExtractor.parse_json('{"a": 1}') == {"a": 1}


def test_extract_json_unfences():
    raw = '```json\n{"a": 1}\n```'
    assert OutputExtractor.parse_json(raw) == {"a": 1}


def test_extract_json_raises_on_garbage():
    with pytest.raises(ValueError):
        OutputExtractor.parse_json("this is not json")


# ===========================================================================
# OutputExtractor — YAML
# ===========================================================================


def test_extract_yaml_plain():
    assert OutputExtractor.parse_yaml("a: 1\nb: 2\n") == {"a": 1, "b": 2}


def test_extract_yaml_unfences():
    raw = "```yaml\nkey: value\n```"
    assert OutputExtractor.parse_yaml(raw) == {"key": "value"}


# ===========================================================================
# OutputExtractor — CSV
# ===========================================================================


def test_extract_csv_with_header():
    raw = "name,count\nalice,1\nbob,2\n"
    out = OutputExtractor.parse_csv(raw)
    assert out["header"] == ["name", "count"]
    assert out["rows"] == [["alice", "1"], ["bob", "2"]]
    assert out["row_dicts"][0]["name"] == "alice"


def test_extract_csv_rejects_empty():
    with pytest.raises(ValueError):
        OutputExtractor.parse_csv("   \n")


def test_extract_csv_unfences():
    raw = "```csv\nx,y\n1,2\n```"
    out = OutputExtractor.parse_csv(raw)
    assert out["header"] == ["x", "y"]


# ===========================================================================
# OutputExtractor — code block
# ===========================================================================


def test_extract_code_block():
    raw = "Intro prose.\n\n```python\nprint('hi')\n```\nTail."
    out = OutputExtractor.parse_code_block(raw, expected_language="python")
    assert out["language"] == "python"
    assert "print('hi')" in out["body"]


def test_extract_code_block_no_fence():
    with pytest.raises(ValueError):
        OutputExtractor.parse_code_block("no fences here", expected_language="python")


def test_extract_code_block_language_mismatch():
    raw = "```python\nx=1\n```"
    with pytest.raises(ValueError):
        OutputExtractor.parse_code_block(raw, expected_language="javascript")


def test_extract_code_block_no_language_check():
    raw = "```\nbareblock\n```"
    out = OutputExtractor.parse_code_block(raw)
    assert out["language"] == ""


# ===========================================================================
# OutputExtractor — markdown sections
# ===========================================================================


def _md_contract_with_sections(sections_data):
    return OutputContract.from_mapping({
        "name": "test-md",
        "format": "markdown_sections",
        "sections": sections_data,
    })


def test_extract_sections_basic():
    c = _md_contract_with_sections([
        {"name": "Summary"}, {"name": "Test plan"},
    ])
    raw = (
        "## Summary\nThis is the summary body.\n\n"
        "## Test plan\n- [ ] step 1\n- [ ] step 2\n"
    )
    parsed = OutputExtractor.parse_markdown_sections(raw, c.sections)
    assert "Summary" in parsed
    assert "step 1" in parsed["Test plan"]


def test_extract_sections_missing_one():
    c = _md_contract_with_sections([
        {"name": "Summary"}, {"name": "Checklist"},
    ])
    raw = "## Summary\nonly summary here\n"
    parsed = OutputExtractor.parse_markdown_sections(raw, c.sections)
    assert "Summary" in parsed
    assert "Checklist" not in parsed


def test_extract_sections_respects_heading_level():
    c = _md_contract_with_sections([
        {"name": "Alpha", "heading_level": 3},
    ])
    raw = "# Top\n## Mid\n### Alpha\nalpha body\n## Mid2\n"
    parsed = OutputExtractor.parse_markdown_sections(raw, c.sections)
    assert "alpha body" in parsed["Alpha"]


# ===========================================================================
# OutputExtractor — extractor hints
# ===========================================================================


def test_extractor_hints_captures_groups():
    raw = 'file: backend/auth.py\nfile: tests/test_auth.py\n'
    out = OutputExtractor.apply_extractor_hints(
        raw, [r"file:\s*(\S+)"],
    )
    assert out[r"file:\s*(\S+)"] == ["backend/auth.py", "tests/test_auth.py"]


def test_extractor_hints_malformed_silently_skipped():
    raw = "anything"
    out = OutputExtractor.apply_extractor_hints(raw, ["[invalid"])
    assert out == {}


# ===========================================================================
# OutputValidator — JSON
# ===========================================================================


def test_validate_json_happy_path():
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {
            "fields": {
                "ok": {"type": "boolean", "required": True},
                "count": {"type": "integer", "minimum": 0},
            },
        },
    })
    v = OutputValidator()
    r = v.validate(c, '{"ok": true, "count": 5}')
    assert r.ok is True
    assert r.issues == ()


def test_validate_json_missing_required():
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {"fields": {"ok": {"type": "boolean", "required": True}}},
    })
    v = OutputValidator()
    r = v.validate(c, '{"other": 1}')
    assert r.ok is False
    assert any(i.code == "missing_required_field" for i in r.issues)


def test_validate_json_wrong_type():
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {"fields": {"count": {"type": "integer"}}},
    })
    r = OutputValidator().validate(c, '{"count": "not-a-number"}')
    assert r.ok is False
    assert any(i.code == "wrong_type" for i in r.issues)


def test_validate_json_bool_not_integer():
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {"fields": {"count": {"type": "integer"}}},
    })
    r = OutputValidator().validate(c, '{"count": true}')
    assert r.ok is False
    assert any(i.code == "wrong_type" for i in r.issues)


def test_validate_json_enum_violation():
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {
            "fields": {"color": {"type": "string", "enum": ["red", "blue"]}},
        },
    })
    r = OutputValidator().validate(c, '{"color": "green"}')
    assert any(i.code == "enum_violation" for i in r.issues)


def test_validate_json_regex():
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {
            "fields": {"id": {"type": "string", "regex": r"^op-\d+$"}},
        },
    })
    r = OutputValidator().validate(c, '{"id": "not-matching"}')
    assert any(i.code == "regex_mismatch" for i in r.issues)
    r2 = OutputValidator().validate(c, '{"id": "op-123"}')
    assert r2.ok is True


def test_validate_json_strict_rejects_unknown():
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {
            "fields": {"a": {"type": "string"}},
            "strict": True,
        },
    })
    r = OutputValidator().validate(c, '{"a": "x", "b": "surprise"}')
    assert any(i.code == "unknown_field" for i in r.issues)


def test_validate_json_non_strict_tolerates_unknown():
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {
            "fields": {"a": {"type": "string"}},
            "strict": False,
        },
    })
    r = OutputValidator().validate(c, '{"a": "x", "b": "surprise"}')
    assert r.ok is True


def test_validate_json_parse_error():
    c = OutputContract.from_mapping({"name": "c", "format": "json"})
    r = OutputValidator().validate(c, "this is not json")
    assert any(i.code == "json_parse_error" for i in r.issues)


def test_validate_json_weak_contract_accepts_valid_json():
    """JSON contract without schema still parses + accepts valid JSON."""
    c = OutputContract.from_mapping({"name": "c", "format": "json"})
    r = OutputValidator().validate(c, '[1,2,3]')
    assert r.ok is True
    assert r.extracted["value"] == [1, 2, 3]


# ===========================================================================
# OutputValidator — Markdown sections
# ===========================================================================


def test_validate_md_sections_happy_path():
    c = OutputContract.from_mapping({
        "name": "pr", "format": "markdown_sections",
        "sections": [
            {"name": "Summary", "min_body_chars": 10},
            {"name": "Test plan"},
        ],
    })
    raw = (
        "## Summary\nThis is a reasonably long summary.\n\n"
        "## Test plan\n- [ ] run tests\n"
    )
    r = OutputValidator().validate(c, raw)
    assert r.ok is True


def test_validate_md_sections_missing_required():
    c = OutputContract.from_mapping({
        "name": "pr", "format": "markdown_sections",
        "sections": [
            {"name": "Summary"}, {"name": "Test plan"},
        ],
    })
    raw = "## Summary\nhere\n"
    r = OutputValidator().validate(c, raw)
    assert any(i.code == "missing_section" for i in r.issues)


def test_validate_md_sections_body_too_short():
    c = OutputContract.from_mapping({
        "name": "pr", "format": "markdown_sections",
        "sections": [{"name": "Summary", "min_body_chars": 100}],
    })
    raw = "## Summary\nshort\n"
    r = OutputValidator().validate(c, raw)
    assert any(i.code == "section_body_too_short" for i in r.issues)


# ===========================================================================
# OutputValidator — CSV
# ===========================================================================


def test_validate_csv_happy_path():
    c = OutputContract.from_mapping({
        "name": "r", "format": "csv",
        "schema": {
            "fields": {
                "name": {"type": "string", "required": True},
                "count": {"type": "integer"},
            },
            "strict": False,
        },
    })
    r = OutputValidator().validate(c, "name,count\nalice,1\nbob,2\n")
    assert r.ok is True
    assert r.extracted["header"] == ["name", "count"]


def test_validate_csv_missing_required_column():
    c = OutputContract.from_mapping({
        "name": "r", "format": "csv",
        "schema": {
            "fields": {
                "id": {"type": "string", "required": True},
            },
        },
    })
    r = OutputValidator().validate(c, "name\nalice\n")
    assert any(i.code == "missing_csv_column" for i in r.issues)


def test_validate_csv_strict_rejects_unknown_column():
    c = OutputContract.from_mapping({
        "name": "r", "format": "csv",
        "schema": {
            "fields": {"name": {"type": "string"}},
            "strict": True,
        },
    })
    r = OutputValidator().validate(c, "name,extra\nalice,x\n")
    assert any(i.code == "unknown_csv_column" for i in r.issues)


def test_validate_csv_parse_error():
    c = OutputContract.from_mapping({"name": "r", "format": "csv"})
    r = OutputValidator().validate(c, "   \n   \n")
    assert any(i.code == "csv_parse_error" for i in r.issues)


# ===========================================================================
# OutputValidator — YAML
# ===========================================================================


def test_validate_yaml_happy_path():
    c = OutputContract.from_mapping({
        "name": "cfg", "format": "yaml",
        "schema": {
            "fields": {"port": {"type": "integer", "required": True}},
            "strict": False,
        },
    })
    r = OutputValidator().validate(c, "port: 8080\nname: api\n")
    assert r.ok is True


def test_validate_yaml_parse_error():
    c = OutputContract.from_mapping({"name": "cfg", "format": "yaml"})
    r = OutputValidator().validate(c, "not: valid: yaml: [[[")
    assert any(i.code == "yaml_parse_error" for i in r.issues)


def test_validate_yaml_non_mapping_with_schema():
    c = OutputContract.from_mapping({
        "name": "cfg", "format": "yaml",
        "schema": {"fields": {"x": {"type": "string"}}},
    })
    r = OutputValidator().validate(c, "- a\n- b\n")
    assert any(i.code == "yaml_not_mapping" for i in r.issues)


# ===========================================================================
# OutputValidator — code block
# ===========================================================================


def test_validate_code_block_happy_path():
    c = OutputContract.from_mapping({
        "name": "snippet", "format": "code_block",
        "fence_language": "python",
    })
    raw = "some prose\n```python\nprint('hi')\n```\ntail"
    r = OutputValidator().validate(c, raw)
    assert r.ok is True
    assert "print" in r.extracted["block"]["body"]


def test_validate_code_block_missing_fence():
    c = OutputContract.from_mapping({
        "name": "snippet", "format": "code_block",
        "fence_language": "python",
    })
    r = OutputValidator().validate(c, "no fences")
    assert any(i.code == "code_block_error" for i in r.issues)


def test_validate_code_block_wrong_language():
    c = OutputContract.from_mapping({
        "name": "snippet", "format": "code_block",
        "fence_language": "python",
    })
    r = OutputValidator().validate(c, "```js\nconsole.log(1)\n```")
    assert any(i.code == "code_block_error" for i in r.issues)


# ===========================================================================
# OutputValidator — length bounds
# ===========================================================================


def test_validate_length_over_max():
    c = OutputContract.from_mapping({
        "name": "c", "format": "plain",
        "max_length_chars": 10,
    })
    r = OutputValidator().validate(c, "x" * 100)
    assert any(i.code == "over_max_length" for i in r.issues)


def test_validate_length_under_min():
    c = OutputContract.from_mapping({
        "name": "c", "format": "plain",
        "min_length_chars": 50,
    })
    r = OutputValidator().validate(c, "short")
    assert any(i.code == "under_min_length" for i in r.issues)


# ===========================================================================
# OutputValidator — extractor hints always populated
# ===========================================================================


def test_extractor_hints_populated_on_every_validation():
    c = OutputContract.from_mapping({
        "name": "c", "format": "plain",
        "extractor_hints": [r"file:\s*(\S+)"],
    })
    r = OutputValidator().validate(c, "file: backend/x.py\nother line")
    assert "hints" in r.extracted
    assert r.extracted["hints"][r"file:\s*(\S+)"] == ["backend/x.py"]


def test_validate_rejects_non_string():
    c = OutputContract.from_mapping({"name": "c", "format": "plain"})
    r = OutputValidator().validate(c, None)  # type: ignore[arg-type]
    assert r.ok is False
    assert any(i.code == "non_string_output" for i in r.issues)


# ===========================================================================
# build_repair_prompt
# ===========================================================================


def test_repair_prompt_names_contract_and_issues():
    c = OutputContract.from_mapping({
        "name": "pr", "format": "markdown_sections",
        "sections": [{"name": "Summary"}, {"name": "Test plan"}],
    })
    r = OutputValidator().validate(c, "just prose, no headings")
    rp = build_repair_prompt(
        contract=c, previous_raw="irrelevant", result=r, attempt=1,
    )
    assert rp.contract_name == "pr"
    assert rp.attempt == 1
    assert "pr" in rp.text
    assert "Summary" in rp.text  # section reminder included
    assert "markdown_sections" in rp.text


def test_repair_prompt_json_reminder_lists_required_fields():
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {
            "fields": {
                "ok": {"type": "boolean", "required": True},
                "data": {"type": "object", "required": True},
            },
        },
    })
    r = OutputValidator().validate(c, "{}")
    rp = build_repair_prompt(
        contract=c, previous_raw="{}", result=r, attempt=1,
    )
    assert "ok" in rp.text
    assert "data" in rp.text


# ===========================================================================
# OutputRepairLoop — bounded convergence
# ===========================================================================


def test_repair_loop_converges_after_one_attempt():
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {"fields": {"ok": {"type": "boolean", "required": True}}},
    })

    def _model(_orig: str, _prompt: OutputRepairPrompt) -> str:
        return '{"ok": true}'

    loop = OutputRepairLoop(max_attempts=2)
    outcome = loop.run(
        contract=c, original_prompt="do thing",
        initial_raw='{"wrong": 1}', model_fn=_model,
    )
    assert outcome.converged is True
    assert outcome.attempts == 1
    assert outcome.final_validation.ok is True


def test_repair_loop_exhausts_attempts():
    c = OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {"fields": {"ok": {"type": "boolean", "required": True}}},
    })

    def _bad_model(_o: str, _p: OutputRepairPrompt) -> str:
        return "still not json"

    loop = OutputRepairLoop(max_attempts=2)
    outcome = loop.run(
        contract=c, original_prompt="do thing",
        initial_raw="nope", model_fn=_bad_model,
    )
    assert outcome.converged is False
    assert outcome.attempts == 2
    assert outcome.final_validation.ok is False


def test_repair_loop_no_initial_repair_needed():
    c = OutputContract.from_mapping({"name": "c", "format": "json"})

    def _should_not_run(_o: str, _p: OutputRepairPrompt) -> str:
        raise AssertionError("model must not be called")

    loop = OutputRepairLoop()
    outcome = loop.run(
        contract=c, original_prompt="x",
        initial_raw='{"ok": true}', model_fn=_should_not_run,
    )
    assert outcome.converged is True
    assert outcome.attempts == 0
    assert outcome.repair_prompts == ()


def test_repair_loop_model_raises_captured():
    c = OutputContract.from_mapping({"name": "c", "format": "json"})

    def _boom(_o: str, _p: OutputRepairPrompt) -> str:
        raise RuntimeError("provider down")

    loop = OutputRepairLoop(max_attempts=2)
    outcome = loop.run(
        contract=c, original_prompt="x",
        initial_raw="broken", model_fn=_boom,
    )
    # Loop breaks out of the repair cycle — final validation is the
    # last one that ran (initial invalid).
    assert outcome.converged is False
    # Either 1 attempt (before raise) or 0 attempts counted depending
    # on exact ordering; the important thing is no crash
    assert outcome.final_validation.ok is False


# ===========================================================================
# OutputRenderer
# ===========================================================================


def test_default_renderer_markdown_sections_repl():
    reg = get_default_renderer_registry()
    c = OutputContract.from_mapping({
        "name": "pr", "format": "markdown_sections",
        "sections": [{"name": "Summary"}],
    })
    r = OutputValidator().validate(c, "## Summary\nhello\n")
    text = reg.render(
        result=r, format=OutputFormat.MARKDOWN_SECTIONS,
        surface=RenderSurface.REPL,
    )
    assert "Summary" in text
    assert "hello" in text


def test_default_renderer_json_repl():
    reg = get_default_renderer_registry()
    c = OutputContract.from_mapping({"name": "c", "format": "json"})
    r = OutputValidator().validate(c, '{"a": 1}')
    text = reg.render(
        result=r, format=OutputFormat.JSON, surface=RenderSurface.REPL,
    )
    assert '"a"' in text
    assert "1" in text


def test_renderer_falls_back_to_default_compact_json():
    reg = OutputRendererRegistry()  # fresh, no defaults
    c = OutputContract.from_mapping({"name": "c", "format": "plain"})
    r = OutputValidator().validate(c, "plain text")
    text = reg.render(
        result=r, format=OutputFormat.PLAIN, surface=RenderSurface.SSE,
    )
    assert "contract" in text
    assert "plain" in text


def test_renderer_register_custom():
    reg = OutputRendererRegistry()

    def _custom(result: ValidationResult, opts: Mapping[str, Any]) -> str:
        return f"CUSTOM({result.ok})"

    reg.register(
        format=OutputFormat.JSON,
        surface=RenderSurface.IDE,
        renderer=_custom,
    )
    c = OutputContract.from_mapping({"name": "c", "format": "json"})
    r = OutputValidator().validate(c, '{"ok": true}')
    text = reg.render(
        result=r, format=OutputFormat.JSON, surface=RenderSurface.IDE,
    )
    assert text == "CUSTOM(True)"


def test_renderer_register_rejects_non_callable():
    reg = OutputRendererRegistry()
    with pytest.raises(TypeError):
        reg.register(
            format=OutputFormat.JSON,
            surface=RenderSurface.REPL,
            renderer="not-callable",  # type: ignore[arg-type]
        )


# ===========================================================================
# OutputContractRegistry
# ===========================================================================


def test_contract_registry_register_and_get():
    reg = OutputContractRegistry()
    c = OutputContract.from_mapping({"name": "c", "format": "json"})
    reg.register(c)
    assert reg.get("c") is c


def test_contract_registry_duplicate_rejected():
    reg = OutputContractRegistry()
    c = OutputContract.from_mapping({"name": "c", "format": "json"})
    reg.register(c)
    with pytest.raises(KeyError):
        reg.register(c)


def test_contract_registry_list_sorted():
    reg = OutputContractRegistry()
    reg.register(OutputContract.from_mapping({"name": "b", "format": "json"}))
    reg.register(OutputContract.from_mapping({"name": "a", "format": "json"}))
    names = [c.name for c in reg.list_all()]
    assert names == ["a", "b"]


# ===========================================================================
# /format REPL dispatcher
# ===========================================================================


def test_repl_unmatched_falls_through():
    r = dispatch_format_command("/plan mode on")
    assert r.matched is False


def test_repl_format_list_empty():
    r = dispatch_format_command("/format")
    assert r.ok is True
    assert "no contracts" in r.text.lower()


def test_repl_format_list_populated():
    reg = OutputContractRegistry()
    reg.register(OutputContract.from_mapping({"name": "a", "format": "json"}))
    reg.register(OutputContract.from_mapping({
        "name": "pr", "format": "markdown_sections",
        "sections": [{"name": "Summary"}],
    }))
    r = dispatch_format_command("/format list", contract_registry=reg)
    assert r.ok is True
    assert "a" in r.text
    assert "pr" in r.text


def test_repl_format_show():
    reg = OutputContractRegistry()
    reg.register(OutputContract.from_mapping({
        "name": "pr", "format": "markdown_sections",
        "sections": [{"name": "Summary"}],
        "description": "Pull request template",
    }))
    r = dispatch_format_command("/format show pr", contract_registry=reg)
    assert r.ok is True
    assert "Pull request template" in r.text
    assert "markdown_sections" in r.text


def test_repl_format_show_unknown():
    r = dispatch_format_command(
        "/format show nope",
        contract_registry=OutputContractRegistry(),
    )
    assert r.ok is False


def test_repl_format_validate_happy():
    reg = OutputContractRegistry()
    reg.register(OutputContract.from_mapping({"name": "c", "format": "json"}))
    r = dispatch_format_command(
        '/format validate c {"ok":true}',
        contract_registry=reg,
    )
    assert r.ok is True
    assert "ok=True" in r.text


def test_repl_format_validate_shows_issues():
    reg = OutputContractRegistry()
    reg.register(OutputContract.from_mapping({
        "name": "c", "format": "json",
        "schema": {"fields": {"x": {"type": "string", "required": True}}},
    }))
    r = dispatch_format_command(
        '/format validate c {"y":1}',
        contract_registry=reg,
    )
    # Validation runs; issues listed
    assert r.ok is True
    assert "ok=False" in r.text
    assert "missing_required_field" in r.text


def test_repl_format_validate_needs_raw():
    reg = OutputContractRegistry()
    reg.register(OutputContract.from_mapping({"name": "c", "format": "json"}))
    r = dispatch_format_command("/format validate c", contract_registry=reg)
    assert r.ok is False


def test_repl_format_help():
    r = dispatch_format_command("/format help")
    assert r.ok is True
    assert "/format" in r.text


def test_repl_format_show_short_form():
    reg = OutputContractRegistry()
    reg.register(OutputContract.from_mapping({"name": "pr", "format": "json"}))
    r = dispatch_format_command("/format pr", contract_registry=reg)
    assert r.ok is True
    assert "pr" in r.text


# ===========================================================================
# Singletons
# ===========================================================================


def test_default_contract_registry_singleton():
    a = get_default_contract_registry()
    b = get_default_contract_registry()
    assert a is b


def test_default_renderer_registry_includes_defaults():
    reg = get_default_renderer_registry()
    assert reg.get(
        OutputFormat.MARKDOWN_SECTIONS, RenderSurface.REPL,
    ) is not None
    assert reg.get(
        OutputFormat.JSON, RenderSurface.REPL,
    ) is not None

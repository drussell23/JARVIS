"""Regression spine for §41.4 Phase 1 sixth arc — Coverage gate."""
from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, List

import pytest


from backend.core.ouroboros.governance import coverage_gate as cg
from backend.core.ouroboros.governance.coverage_gate import (
    COVERAGE_GATE_SCHEMA_VERSION,
    CoverageReport,
    CoverageSource,
    CoverageVerdict,
    FileCoverage,
    _ENV_FLOOR_THRESHOLD,
    _ENV_JSON_REPORT_PATH,
    _ENV_LEDGER_PATH,
    _ENV_MASTER,
    _ENV_MISSING_FILE_PENALTY,
    _ENV_PERSIST,
    _ENV_SQLITE_PATH,
    _ENV_STRONG_THRESHOLD,
    _ENV_SUBPROCESS_TIMEOUT_S,
    _ENV_XML_REPORT_PATH,
    _find_coverage_for_file,
    _normalize_path,
    _verdict_for_overall_pct,
    evaluate_coverage,
    floor_threshold,
    format_coverage_panel,
    json_report_path,
    ledger_path,
    load_coverage_data,
    master_enabled,
    missing_file_penalty,
    parse_coverage_json,
    parse_coverage_report_stdout,
    parse_coverage_xml,
    persistence_enabled,
    register_flags,
    register_shipped_invariants,
    source_glyph,
    sqlite_data_path,
    strong_threshold,
    subprocess_timeout_s,
    verdict_glyph,
    xml_report_path,
)


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    for env in (
        _ENV_MASTER, _ENV_PERSIST,
        _ENV_FLOOR_THRESHOLD, _ENV_STRONG_THRESHOLD,
        _ENV_MISSING_FILE_PENALTY,
        _ENV_JSON_REPORT_PATH, _ENV_XML_REPORT_PATH,
        _ENV_SQLITE_PATH, _ENV_SUBPROCESS_TIMEOUT_S,
        _ENV_LEDGER_PATH,
    ):
        monkeypatch.delenv(env, raising=False)
    # Point report paths into tmp so we never accidentally
    # pick up a real repo coverage file.
    monkeypatch.setenv(
        _ENV_JSON_REPORT_PATH, str(tmp_path / "cov.json"),
    )
    monkeypatch.setenv(
        _ENV_XML_REPORT_PATH, str(tmp_path / "cov.xml"),
    )
    monkeypatch.setenv(
        _ENV_SQLITE_PATH, str(tmp_path / ".cov_sqlite"),
    )
    monkeypatch.setenv(
        _ENV_LEDGER_PATH, str(tmp_path / "ledger.jsonl"),
    )
    yield


# Defaults


def test_schema():
    assert COVERAGE_GATE_SCHEMA_VERSION == "coverage_gate.1"


def test_master_default_false():
    assert master_enabled() is False


def test_persistence_default_true():
    assert persistence_enabled() is True


def test_floor_threshold_default():
    assert floor_threshold() == 0.60


def test_strong_threshold_default():
    assert strong_threshold() == 0.85


def test_strong_threshold_auto_clamp(monkeypatch):
    monkeypatch.setenv(_ENV_FLOOR_THRESHOLD, "0.90")
    monkeypatch.setenv(_ENV_STRONG_THRESHOLD, "0.50")
    # strong < floor → clamped to floor
    assert strong_threshold() == 0.90


def test_missing_file_penalty_default():
    assert missing_file_penalty() == 0.0


def test_subprocess_timeout_default():
    assert subprocess_timeout_s() == 30


def test_json_path_default(monkeypatch, tmp_path):
    monkeypatch.delenv(_ENV_JSON_REPORT_PATH, raising=False)
    p = json_report_path()
    assert str(p) == "coverage.json"


def test_xml_path_default(monkeypatch):
    monkeypatch.delenv(_ENV_XML_REPORT_PATH, raising=False)
    p = xml_report_path()
    assert str(p) == "coverage.xml"


def test_sqlite_path_default(monkeypatch):
    monkeypatch.delenv(_ENV_SQLITE_PATH, raising=False)
    p = sqlite_data_path()
    assert str(p) == ".coverage"


# Taxonomies


def test_verdict_taxonomy_closed():
    assert {v.value for v in CoverageVerdict} == {
        "below_floor", "acceptable", "strong", "disabled",
    }


def test_source_taxonomy_closed():
    assert {s.value for s in CoverageSource} == {
        "json", "xml", "sqlite", "subprocess",
    }


@pytest.mark.parametrize("v", list(CoverageVerdict))
def test_verdict_glyph(v):
    assert verdict_glyph(v) != "?"


@pytest.mark.parametrize("s", list(CoverageSource))
def test_source_glyph(s):
    assert source_glyph(s) != "?"


# JSON parser


def test_parse_json_empty():
    assert parse_coverage_json("") == ()


def test_parse_json_malformed():
    assert parse_coverage_json("not json") == ()


def test_parse_json_missing_files_key():
    assert parse_coverage_json('{"other": "data"}') == ()


def test_parse_json_basic():
    raw = json.dumps({
        "files": {
            "foo.py": {
                "summary": {
                    "covered_lines": 8,
                    "num_statements": 10,
                    "percent_covered": 80.0,
                },
                "missing_lines": [3, 7],
            }
        }
    })
    records = parse_coverage_json(raw)
    assert len(records) == 1
    assert records[0].file_path == "foo.py"
    assert records[0].line_coverage_pct == 0.80
    assert records[0].lines_total == 10
    assert records[0].lines_covered == 8
    assert records[0].missing_lines == (3, 7)


def test_parse_json_normalizes_percent_below_one():
    """Some report tools emit 0.80; some 80.0. Both → 0.80."""
    raw = json.dumps({
        "files": {
            "foo.py": {
                "summary": {
                    "covered_lines": 8,
                    "num_statements": 10,
                    "percent_covered": 0.8,  # already ratio
                }
            }
        }
    })
    records = parse_coverage_json(raw)
    assert records[0].line_coverage_pct == 0.80


def test_parse_json_skips_malformed_file():
    raw = json.dumps({
        "files": {
            "good.py": {
                "summary": {
                    "covered_lines": 5,
                    "num_statements": 5,
                    "percent_covered": 100.0,
                }
            },
            "bad.py": "not a dict",
        }
    })
    records = parse_coverage_json(raw)
    assert len(records) == 1
    assert records[0].file_path == "good.py"


# XML parser


def test_parse_xml_empty():
    assert parse_coverage_xml("") == ()


def test_parse_xml_malformed():
    assert parse_coverage_xml("<broken") == ()


def test_parse_xml_basic():
    raw = (
        '<?xml version="1.0"?>'
        '<coverage>'
        '  <packages><package><classes>'
        '    <class filename="bar.py" line-rate="0.75" '
        '           branch-rate="0.5">'
        '      <lines>'
        '        <line number="1" hits="1"/>'
        '        <line number="2" hits="1"/>'
        '        <line number="3" hits="0"/>'
        '        <line number="4" hits="1"/>'
        '      </lines>'
        '    </class>'
        '  </classes></package></packages>'
        '</coverage>'
    )
    records = parse_coverage_xml(raw)
    assert len(records) == 1
    assert records[0].file_path == "bar.py"
    assert records[0].line_coverage_pct == 0.75
    assert records[0].branch_coverage_pct == 0.5
    assert records[0].lines_total == 4
    assert records[0].lines_covered == 3
    assert records[0].missing_lines == (3,)


def test_parse_xml_multiple_classes():
    raw = (
        '<coverage><packages><package><classes>'
        '<class filename="a.py" line-rate="1.0">'
        '<lines><line number="1" hits="1"/></lines>'
        '</class>'
        '<class filename="b.py" line-rate="0.0">'
        '<lines><line number="1" hits="0"/></lines>'
        '</class>'
        '</classes></package></packages></coverage>'
    )
    records = parse_coverage_xml(raw)
    assert len(records) == 2
    files = {r.file_path for r in records}
    assert files == {"a.py", "b.py"}


# Subprocess stdout parser


def test_parse_stdout_empty():
    assert parse_coverage_report_stdout("") == ()


def test_parse_stdout_basic():
    stdout = (
        "Name              Stmts   Miss  Cover\n"
        "-------------------------------------\n"
        "backend/foo.py       10      2    80%\n"
        "backend/bar.py       20      0   100%\n"
        "-------------------------------------\n"
        "TOTAL                30      2    93%\n"
    )
    records = parse_coverage_report_stdout(stdout)
    files = {r.file_path: r for r in records}
    assert "backend/foo.py" in files
    assert "backend/bar.py" in files
    assert files["backend/foo.py"].line_coverage_pct == 0.80
    assert files["backend/bar.py"].line_coverage_pct == 1.0


def test_parse_stdout_skips_total_line():
    stdout = "TOTAL    100    20    80%\n"
    records = parse_coverage_report_stdout(stdout)
    assert records == ()


# Path normalization + matching


def test_normalize_path():
    assert _normalize_path("foo/bar.py") == "foo/bar.py"
    assert _normalize_path("foo\\bar.py") == "foo/bar.py"
    assert _normalize_path("  foo.py  ") == "foo.py"
    assert _normalize_path("") == ""


def test_find_coverage_exact_match():
    records = (
        FileCoverage(
            file_path="foo.py", line_coverage_pct=0.8,
            branch_coverage_pct=0.0, lines_total=10,
            lines_covered=8, missing_lines=(),
        ),
    )
    match = _find_coverage_for_file("foo.py", records)
    assert match is not None
    assert match.file_path == "foo.py"


def test_find_coverage_basename_match():
    records = (
        FileCoverage(
            file_path="src/backend/foo.py",
            line_coverage_pct=0.8, branch_coverage_pct=0.0,
            lines_total=10, lines_covered=8, missing_lines=(),
        ),
    )
    match = _find_coverage_for_file("backend/foo.py", records)
    assert match is not None


def test_find_coverage_no_match():
    records = (
        FileCoverage(
            file_path="other.py", line_coverage_pct=1.0,
            branch_coverage_pct=0.0, lines_total=1,
            lines_covered=1, missing_lines=(),
        ),
    )
    assert _find_coverage_for_file("missing.py", records) is None


def test_find_coverage_empty():
    assert _find_coverage_for_file("foo.py", ()) is None
    assert _find_coverage_for_file("", ()) is None


# Verdict classifier


def test_verdict_disabled_no_data():
    assert (
        _verdict_for_overall_pct(0.0, has_data=False)
        is CoverageVerdict.DISABLED
    )


def test_verdict_below_floor():
    assert (
        _verdict_for_overall_pct(0.5, has_data=True)
        is CoverageVerdict.BELOW_FLOOR
    )


def test_verdict_acceptable():
    assert (
        _verdict_for_overall_pct(0.75, has_data=True)
        is CoverageVerdict.ACCEPTABLE
    )


def test_verdict_strong():
    assert (
        _verdict_for_overall_pct(0.95, has_data=True)
        is CoverageVerdict.STRONG
    )


def test_verdict_threshold_boundary():
    """floor = 0.60 default. 0.60 is ACCEPTABLE (≥ floor)."""
    assert (
        _verdict_for_overall_pct(0.60, has_data=True)
        is CoverageVerdict.ACCEPTABLE
    )


# Loading + source priority


def test_load_no_sources_returns_empty(monkeypatch):
    """All paths point to nonexistent files."""
    source, records = load_coverage_data()
    assert records == ()


def test_load_json_source_priority(tmp_path, monkeypatch):
    """When JSON exists, it's preferred over XML."""
    json_path = tmp_path / "cov.json"
    xml_path = tmp_path / "cov.xml"
    json_path.write_text(
        json.dumps({
            "files": {"foo.py": {"summary": {
                "covered_lines": 10, "num_statements": 10,
                "percent_covered": 100.0,
            }}}
        }),
        encoding="utf-8",
    )
    xml_path.write_text(
        '<coverage><packages><package><classes>'
        '<class filename="bar.py" line-rate="0.5">'
        '<lines><line number="1" hits="0"/></lines>'
        '</class></classes></package></packages></coverage>',
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_JSON_REPORT_PATH, str(json_path))
    monkeypatch.setenv(_ENV_XML_REPORT_PATH, str(xml_path))
    source, records = load_coverage_data()
    # JSON wins by priority
    assert source is CoverageSource.JSON
    assert any(r.file_path == "foo.py" for r in records)


def test_load_falls_back_to_xml(tmp_path, monkeypatch):
    xml_path = tmp_path / "cov.xml"
    xml_path.write_text(
        '<coverage><packages><package><classes>'
        '<class filename="bar.py" line-rate="0.5">'
        '<lines><line number="1" hits="0"/></lines>'
        '</class></classes></package></packages></coverage>',
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_XML_REPORT_PATH, str(xml_path))
    monkeypatch.setenv(
        _ENV_JSON_REPORT_PATH, str(tmp_path / "absent.json"),
    )
    source, records = load_coverage_data()
    assert source is CoverageSource.XML
    assert any(r.file_path == "bar.py" for r in records)


def test_load_source_override_skips_fallback(tmp_path, monkeypatch):
    """Override forces XML even when JSON exists."""
    json_path = tmp_path / "cov.json"
    json_path.write_text(
        json.dumps({"files": {"a.py": {"summary": {
            "covered_lines": 1, "num_statements": 1,
            "percent_covered": 100.0,
        }}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_JSON_REPORT_PATH, str(json_path))
    # XML is unset/absent → override returns empty without
    # falling back to JSON
    source, records = load_coverage_data(
        source_override=CoverageSource.XML,
    )
    assert source is CoverageSource.XML
    assert records == ()


# evaluate_coverage


def test_evaluate_master_off():
    report = evaluate_coverage(["foo.py"])
    assert report.master_enabled is False
    assert report.verdict is CoverageVerdict.DISABLED


def test_evaluate_no_target_files(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_coverage([])
    assert report.verdict is CoverageVerdict.DISABLED


def test_evaluate_no_coverage_source(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    report = evaluate_coverage(["foo.py"])
    assert report.verdict is CoverageVerdict.DISABLED
    assert "no coverage data" in report.diagnostic.lower()


def test_evaluate_strong_with_records_override(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    records = (
        FileCoverage(
            file_path="foo.py", line_coverage_pct=0.95,
            branch_coverage_pct=0.0, lines_total=20,
            lines_covered=19, missing_lines=(7,),
        ),
    )
    report = evaluate_coverage(
        ["foo.py"], records_override=records,
    )
    assert report.verdict is CoverageVerdict.STRONG
    assert report.overall_pct == 0.95


def test_evaluate_below_floor(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    records = (
        FileCoverage(
            file_path="foo.py", line_coverage_pct=0.30,
            branch_coverage_pct=0.0, lines_total=10,
            lines_covered=3, missing_lines=(),
        ),
    )
    report = evaluate_coverage(
        ["foo.py"], records_override=records,
    )
    assert report.verdict is CoverageVerdict.BELOW_FLOOR


def test_evaluate_acceptable_band(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    records = (
        FileCoverage(
            file_path="foo.py", line_coverage_pct=0.70,
            branch_coverage_pct=0.0, lines_total=10,
            lines_covered=7, missing_lines=(),
        ),
    )
    report = evaluate_coverage(
        ["foo.py"], records_override=records,
    )
    assert report.verdict is CoverageVerdict.ACCEPTABLE


def test_evaluate_mixed_files(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    records = (
        FileCoverage(
            file_path="high.py", line_coverage_pct=0.95,
            branch_coverage_pct=0.0, lines_total=20,
            lines_covered=19, missing_lines=(),
        ),
        FileCoverage(
            file_path="low.py", line_coverage_pct=0.25,
            branch_coverage_pct=0.0, lines_total=20,
            lines_covered=5, missing_lines=(),
        ),
    )
    report = evaluate_coverage(
        ["high.py", "low.py"], records_override=records,
    )
    # Mean = 0.60 → ACCEPTABLE (≥ floor)
    assert report.overall_pct == 0.60
    assert report.verdict is CoverageVerdict.ACCEPTABLE


def test_evaluate_missing_file_default_penalty(monkeypatch):
    """Default missing_file_penalty=0.0 → missing files
    count as 0%."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    records = (
        FileCoverage(
            file_path="found.py", line_coverage_pct=1.0,
            branch_coverage_pct=0.0, lines_total=10,
            lines_covered=10, missing_lines=(),
        ),
    )
    report = evaluate_coverage(
        ["found.py", "missing.py"],
        records_override=records,
    )
    # 1.0 + 0.0 = 0.50 average → BELOW_FLOOR
    assert report.overall_pct == 0.50
    assert "missing.py" in report.missing_files


def test_evaluate_missing_file_with_high_penalty(monkeypatch):
    """High missing_file_penalty gives benefit of the doubt."""
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_MISSING_FILE_PENALTY, "0.9")
    records = (
        FileCoverage(
            file_path="found.py", line_coverage_pct=0.9,
            branch_coverage_pct=0.0, lines_total=10,
            lines_covered=9, missing_lines=(),
        ),
    )
    report = evaluate_coverage(
        ["found.py", "missing.py"],
        records_override=records,
    )
    # 0.9 + 0.9 = 0.90 → STRONG
    assert report.overall_pct == 0.90
    assert report.verdict is CoverageVerdict.STRONG


def test_evaluate_with_real_json_file(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    json_path = tmp_path / "cov.json"
    json_path.write_text(
        json.dumps({"files": {"foo.py": {"summary": {
            "covered_lines": 10, "num_statements": 10,
            "percent_covered": 100.0,
        }}}}),
        encoding="utf-8",
    )
    monkeypatch.setenv(_ENV_JSON_REPORT_PATH, str(json_path))
    report = evaluate_coverage(["foo.py"])
    assert report.verdict is CoverageVerdict.STRONG
    assert report.source is CoverageSource.JSON


# Persistence


def test_persist_disabled_verdict_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    evaluate_coverage(["foo.py"])
    # No coverage data → DISABLED → no persist
    assert not ledger_path().exists()


def test_persist_writes(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    monkeypatch.setenv(_ENV_PERSIST, "true")
    records = (
        FileCoverage(
            file_path="foo.py", line_coverage_pct=0.95,
            branch_coverage_pct=0.0, lines_total=10,
            lines_covered=9, missing_lines=(),
        ),
    )
    evaluate_coverage(["foo.py"], records_override=records)
    assert ledger_path().exists()


def test_persist_master_off_no_write(monkeypatch):
    monkeypatch.setenv(_ENV_PERSIST, "true")
    records = (
        FileCoverage(
            file_path="foo.py", line_coverage_pct=0.95,
            branch_coverage_pct=0.0, lines_total=10,
            lines_covered=9, missing_lines=(),
        ),
    )
    evaluate_coverage(["foo.py"], records_override=records)
    assert not ledger_path().exists()


# Renderer


def test_format_master_off():
    out = format_coverage_panel()
    assert "disabled" in out


def test_format_with_report(monkeypatch):
    monkeypatch.setenv(_ENV_MASTER, "true")
    records = (
        FileCoverage(
            file_path="foo.py", line_coverage_pct=0.85,
            branch_coverage_pct=0.0, lines_total=10,
            lines_covered=8, missing_lines=(),
        ),
    )
    report = evaluate_coverage(
        ["foo.py"], records_override=records,
    )
    out = format_coverage_panel(report)
    assert "Coverage Gate" in out
    assert "foo.py" in out


# to_dict


def test_file_coverage_to_dict():
    f = FileCoverage(
        file_path="x.py", line_coverage_pct=0.8,
        branch_coverage_pct=0.0, lines_total=10,
        lines_covered=8, missing_lines=(3, 7),
    )
    d = f.to_dict()
    assert d["schema_version"] == COVERAGE_GATE_SCHEMA_VERSION
    assert d["missing_lines"] == [3, 7]


def test_report_to_dict():
    r = CoverageReport(
        evaluated_at_unix=1.0, master_enabled=True,
        verdict=CoverageVerdict.STRONG,
        source=CoverageSource.JSON,
        overall_pct=0.9, floor_threshold=0.6,
        strong_threshold=0.85, per_file=(),
        missing_files=(), boundary_crossed=False,
        diagnostic="x", elapsed_s=0.0,
    )
    d = r.to_dict()
    assert d["verdict"] == "strong"
    assert d["source"] == "json"


# AST pins


@pytest.fixture(scope="module")
def _canonical():
    src = Path(
        "backend/core/ouroboros/governance/coverage_gate.py",
    ).read_text(encoding="utf-8")
    return ast.parse(src), src


def test_pins_count():
    assert len(register_shipped_invariants()) == 5


@pytest.mark.parametrize(
    "name_part",
    [
        "verdict_taxonomy_closed",
        "source_taxonomy_closed",
        "authority_asymmetry",
        "master_default_false",
        "composes_canonical",
    ],
)
def test_pin_canonical(_canonical, name_part):
    tree, src = _canonical
    pins = register_shipped_invariants()
    pin = next(p for p in pins if name_part in p.invariant_name)
    assert pin.validate(tree, src) == ()


def test_pin_authority_forbids_iron_gate():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "authority_asymmetry" in p.invariant_name
    )
    bad = (
        "from backend.core.ouroboros.governance.iron_gate "
        "import x\n"
    )
    assert pin.validate(ast.parse(bad), bad)


def test_pin_composes_synthetic():
    pins = register_shipped_invariants()
    pin = next(
        p for p in pins
        if "composes_canonical" in p.invariant_name
    )
    bad = "# no canonical surfaces\n"
    assert pin.validate(ast.parse(bad), bad)


# Flag registry


class _CapturingRegistry:
    def __init__(self):
        self.registered: List[Any] = []

    def register(self, spec):
        self.registered.append(spec)


def test_flag_seed_count():
    reg = _CapturingRegistry()
    count = register_flags(reg)
    assert count == 9


def test_flag_master_default_false():
    reg = _CapturingRegistry()
    register_flags(reg)
    master = next(
        s for s in reg.registered if s.name == _ENV_MASTER
    )
    assert master.default is False


# SSE


def test_sse_event_exists():
    from backend.core.ouroboros.governance import (
        ide_observability_stream as ios,
    )
    assert (
        ios.EVENT_TYPE_COVERAGE_GATE_EVALUATED
        == "coverage_gate_evaluated"
    )
    assert (
        "coverage_gate_evaluated" in ios._VALID_EVENT_TYPES
    )

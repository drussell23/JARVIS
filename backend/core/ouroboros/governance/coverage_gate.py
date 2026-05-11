"""
Coverage Gate — Test Coverage Threshold Advisor
================================================

Closes §41.4 Phase 1 sixth arc (PRD v3.0+). Per the binding:

  "Coverage gate | Doesn't exist | ~1 week | Composes
   coverage.py + Iron Gate; refuses commits below threshold"

The substrate is the **advisor** half. It parses coverage data
(XML, JSON, or coverage.py SQLite) for the files touched by a
proposed change and emits a 4-value :class:`CoverageVerdict`.
Operator-side wiring (Iron Gate composing the verdict into a
commit-refuse predicate) stays out of scope — AST pin forbids
``iron_gate`` import.

Why "advisor + wiring" split:

1. Different operator workflows want different gating policies
   (some block POOR + WEAK, others only block FAILED).
   Substrate exposes the verdict; operator picks the policy.
2. The substrate must NEVER raise into the orchestrator path,
   even when coverage data is unparseable. Iron Gate would.
   Keeping them separate honors §6 (no implicit cage bypass).
3. Coverage data lives in many places (.coverage / coverage.xml
   / coverage.json / subprocess `coverage report`). Substrate
   exposes a closed 4-value :class:`CoverageSource` taxonomy
   so consumers know which path produced the verdict.

Approach (deterministic + composable):

1. **Source resolution** — try in priority order:
   - JSON report (``coverage.json``) — fastest, structured
   - XML report (``coverage.xml``) — Cobertura format
   - SQLite (``.coverage``) via coverage.py CoverageData API
     (lazy import; degrades to SUBPROCESS when unavailable)
   - SUBPROCESS — invoke ``python -m coverage report``
     and parse stdout
2. **Per-file projection** — for each target_file in the
   proposed change, find its FileCoverage record; missing
   files are reported separately (operator may opt to treat
   missing as 0% or skip).
3. **Verdict synthesis** — overall coverage = mean of touched-
   file coverages; map to verdict band.

The substrate is **deterministic** — same coverage report +
same target_files → same verdict. Operators can re-run reports
to compare proposed-change coverage against current baseline.

Composition contract:

* :mod:`xml.etree.ElementTree` (stdlib) — XML parser.
* :mod:`json` (stdlib) — JSON parser.
* :mod:`subprocess` (stdlib) — fallback coverage CLI invoker.
* :func:`governance_boundary_gate.is_boundary_crossed` (Wave
  2 #5) — cage-touch flag.
* :func:`cross_process_jsonl.flock_append_line` — §33.4
  audit at ``.jarvis/coverage_gate_ledger.jsonl``.

NEVER raises. Missing report / malformed XML / missing files
all degrade to ``DISABLED`` verdict, not exception.

Closed 4-value :class:`CoverageVerdict`:

  BELOW_FLOOR    overall_pct < floor_threshold (default 0.60)
                 — operator-side should refuse APPLY
  ACCEPTABLE     floor ≤ overall_pct < strong_threshold
  STRONG         overall_pct ≥ strong_threshold (default 0.85)
  DISABLED       master off OR no coverage source found

Closed 4-value :class:`CoverageSource`:

  JSON           parsed coverage.json (preferred)
  XML            parsed coverage.xml (Cobertura)
  SQLITE         coverage.py CoverageData API (lazy import)
  SUBPROCESS     parsed stdout of `python -m coverage report`

§33.1 cognitive substrate ``JARVIS_COVERAGE_GATE_ENABLED``
default-**FALSE**.

Authority asymmetry (AST-pinned): stdlib only at module load.
``governance_boundary_gate`` + ``cross_process_jsonl`` are
lazy-imported. coverage.py is lazy-imported behind the SQLITE
source path. Does NOT import orchestrator / iron_gate /
policy / providers / candidate_generator / urgency_router /
change_engine / semantic_guardian / auto_committer /
risk_tier_floor / tool_executor / plan_generator (substrate
is advisory; operator-side wiring composes Iron Gate, not
the substrate).
"""
from __future__ import annotations

import ast
import asyncio
import enum
import json
import logging
import os
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import (
    Any,
    Awaitable,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

logger = logging.getLogger(__name__)


COVERAGE_GATE_SCHEMA_VERSION: str = "coverage_gate.1"


_ENV_MASTER = "JARVIS_COVERAGE_GATE_ENABLED"
_ENV_PERSIST = "JARVIS_COVERAGE_GATE_PERSIST_ENABLED"
_ENV_FLOOR_THRESHOLD = "JARVIS_COVERAGE_GATE_FLOOR_THRESHOLD"
_ENV_STRONG_THRESHOLD = "JARVIS_COVERAGE_GATE_STRONG_THRESHOLD"
_ENV_MISSING_FILE_PENALTY = (
    "JARVIS_COVERAGE_GATE_MISSING_FILE_PENALTY"
)
_ENV_JSON_REPORT_PATH = "JARVIS_COVERAGE_GATE_JSON_PATH"
_ENV_XML_REPORT_PATH = "JARVIS_COVERAGE_GATE_XML_PATH"
_ENV_SQLITE_PATH = "JARVIS_COVERAGE_GATE_SQLITE_PATH"
_ENV_SUBPROCESS_TIMEOUT_S = (
    "JARVIS_COVERAGE_GATE_SUBPROCESS_TIMEOUT_S"
)
_ENV_LEDGER_PATH = "JARVIS_COVERAGE_GATE_LEDGER_PATH"

_DEFAULT_FLOOR_THRESHOLD = 0.60
_DEFAULT_STRONG_THRESHOLD = 0.85
_DEFAULT_MISSING_FILE_PENALTY = 0.0  # missing → 0%
_DEFAULT_JSON_REPORT_REL = "coverage.json"
_DEFAULT_XML_REPORT_REL = "coverage.xml"
_DEFAULT_SQLITE_REL = ".coverage"
_DEFAULT_SUBPROCESS_TIMEOUT_S = 30
_DEFAULT_LEDGER_REL = ".jarvis/coverage_gate_ledger.jsonl"

_TRUTHY: FrozenSet[str] = frozenset({"1", "true", "yes", "on"})


def _flag(name: str, *, default: bool = False) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in _TRUTHY


def master_enabled() -> bool:
    """§33.1 — default-FALSE."""
    return _flag(_ENV_MASTER, default=False)


def persistence_enabled() -> bool:
    return _flag(_ENV_PERSIST, default=True)


def _read_clamped_float(
    name: str, default: float, lo: float, hi: float,
) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _read_clamped_int(
    name: str, default: int, lo: int, hi: int,
) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, n))


def floor_threshold() -> float:
    """overall_pct below this → BELOW_FLOOR verdict.
    Default 0.60. Clamped [0.0, 1.0]."""
    return _read_clamped_float(
        _ENV_FLOOR_THRESHOLD,
        _DEFAULT_FLOOR_THRESHOLD,
        0.0, 1.0,
    )


def strong_threshold() -> float:
    """overall_pct above this → STRONG verdict. Default 0.85.
    Auto-clamped ≥ floor_threshold so band is non-empty."""
    raw = _read_clamped_float(
        _ENV_STRONG_THRESHOLD,
        _DEFAULT_STRONG_THRESHOLD,
        0.0, 1.0,
    )
    return max(raw, floor_threshold())


def missing_file_penalty() -> float:
    """Coverage attributed to files missing from the report.
    Default 0.0 — missing files count as 0%. Operator may
    raise this to e.g. 0.5 to give files-without-data benefit
    of the doubt."""
    return _read_clamped_float(
        _ENV_MISSING_FILE_PENALTY,
        _DEFAULT_MISSING_FILE_PENALTY,
        0.0, 1.0,
    )


def subprocess_timeout_s() -> int:
    return _read_clamped_int(
        _ENV_SUBPROCESS_TIMEOUT_S,
        _DEFAULT_SUBPROCESS_TIMEOUT_S, 1, 600,
    )


def json_report_path() -> Path:
    raw = os.environ.get(_ENV_JSON_REPORT_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_JSON_REPORT_REL)


def xml_report_path() -> Path:
    raw = os.environ.get(_ENV_XML_REPORT_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_XML_REPORT_REL)


def sqlite_data_path() -> Path:
    raw = os.environ.get(_ENV_SQLITE_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_SQLITE_REL)


def ledger_path() -> Path:
    raw = os.environ.get(_ENV_LEDGER_PATH, "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(_DEFAULT_LEDGER_REL)


# Closed taxonomies


class CoverageVerdict(str, enum.Enum):
    """Closed 4-value verdict — bytes-pinned via AST."""

    BELOW_FLOOR = "below_floor"
    ACCEPTABLE = "acceptable"
    STRONG = "strong"
    DISABLED = "disabled"


class CoverageSource(str, enum.Enum):
    """Closed 4-value source taxonomy — bytes-pinned via AST."""

    JSON = "json"
    XML = "xml"
    SQLITE = "sqlite"
    SUBPROCESS = "subprocess"


_VERDICT_GLYPH: Dict[str, str] = {
    CoverageVerdict.BELOW_FLOOR.value: "✗",
    CoverageVerdict.ACCEPTABLE.value: "◐",
    CoverageVerdict.STRONG.value: "✓",
    CoverageVerdict.DISABLED.value: "◌",
}


_SOURCE_GLYPH: Dict[str, str] = {
    CoverageSource.JSON.value: "📋",
    CoverageSource.XML.value: "📄",
    CoverageSource.SQLITE.value: "🗃",
    CoverageSource.SUBPROCESS.value: "⚙",
}


def verdict_glyph(verdict: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(verdict, "value"):
            return _VERDICT_GLYPH.get(str(verdict.value), "?")
        return _VERDICT_GLYPH.get(
            str(verdict or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


def source_glyph(source: object) -> str:
    """NEVER raises."""
    try:
        if hasattr(source, "value"):
            return _SOURCE_GLYPH.get(str(source.value), "?")
        return _SOURCE_GLYPH.get(
            str(source or "").strip().lower(), "?",
        )
    except Exception:  # noqa: BLE001
        return "?"


# §33.5 frozen artifacts


@dataclass(frozen=True)
class FileCoverage:
    """Per-file coverage projection."""

    file_path: str
    line_coverage_pct: float       # 0.0–1.0
    branch_coverage_pct: float     # 0.0–1.0 (0.0 if not measured)
    lines_total: int
    lines_covered: int
    missing_lines: Tuple[int, ...]
    schema_version: str = COVERAGE_GATE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "file_path": self.file_path[:256],
            "line_coverage_pct": float(self.line_coverage_pct),
            "branch_coverage_pct": float(self.branch_coverage_pct),
            "lines_total": int(self.lines_total),
            "lines_covered": int(self.lines_covered),
            "missing_lines": list(self.missing_lines),
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class CoverageReport:
    """Top-level coverage assessment."""

    evaluated_at_unix: float
    master_enabled: bool
    verdict: CoverageVerdict
    source: CoverageSource
    overall_pct: float
    floor_threshold: float
    strong_threshold: float
    per_file: Tuple[FileCoverage, ...]
    missing_files: Tuple[str, ...]
    boundary_crossed: bool
    diagnostic: str
    elapsed_s: float
    schema_version: str = COVERAGE_GATE_SCHEMA_VERSION

    def to_dict(self) -> Dict[str, Any]:
        return {
            "evaluated_at_unix": self.evaluated_at_unix,
            "master_enabled": self.master_enabled,
            "verdict": self.verdict.value,
            "source": self.source.value,
            "overall_pct": float(self.overall_pct),
            "floor_threshold": float(self.floor_threshold),
            "strong_threshold": float(self.strong_threshold),
            "per_file": [f.to_dict() for f in self.per_file],
            "missing_files": list(self.missing_files),
            "boundary_crossed": bool(self.boundary_crossed),
            "diagnostic": self.diagnostic[:512],
            "elapsed_s": float(self.elapsed_s),
            "schema_version": self.schema_version,
        }


# Composers — lazy-imported governance surfaces


def _is_boundary_crossed(file_paths: Sequence[str]) -> bool:
    """Compose Wave 2 #5 boundary gate. NEVER raises."""
    if not file_paths:
        return False
    try:
        from backend.core.ouroboros.governance.governance_boundary_gate import (  # noqa: E501
            is_boundary_crossed,
        )
        return bool(is_boundary_crossed(file_paths))
    except Exception:  # noqa: BLE001
        return False


def _flock_append(payload: Mapping[str, Any]) -> bool:
    """Best-effort §33.4 write. NEVER raises."""
    if not master_enabled() or not persistence_enabled():
        return False
    try:
        from backend.core.ouroboros.governance.cross_process_jsonl import (  # noqa: E501
            flock_append_line,
        )
    except ImportError:
        return False
    try:
        target = ledger_path()
        target.parent.mkdir(parents=True, exist_ok=True)
        flock_append_line(target, json.dumps(dict(payload)))
        return True
    except Exception:  # noqa: BLE001
        return False


# Coverage parsers (pure, stdlib only)


def parse_coverage_json(
    raw_text: str,
) -> Tuple[FileCoverage, ...]:
    """Parse coverage.py JSON report. NEVER raises.

    Expected shape (coverage.py JSON v2):
      {
        "files": {
          "path/to/file.py": {
            "summary": {
              "covered_lines": int,
              "num_statements": int,
              "percent_covered": float (0..100),
              "missing_lines": int,
            },
            "missing_lines": [int, ...]
          }
        }
      }
    """
    if not raw_text:
        return ()
    try:
        data = json.loads(raw_text)
    except (ValueError, TypeError):
        return ()
    if not isinstance(data, dict):
        return ()
    files_dict = data.get("files")
    if not isinstance(files_dict, dict):
        return ()
    out: List[FileCoverage] = []
    for path, file_data in files_dict.items():
        if not isinstance(file_data, dict):
            continue
        try:
            summary = file_data.get("summary", {})
            if not isinstance(summary, dict):
                continue
            covered = int(summary.get("covered_lines", 0) or 0)
            total = int(summary.get("num_statements", 0) or 0)
            pct = float(summary.get("percent_covered", 0.0) or 0.0)
            # coverage.py reports percent in 0–100; normalize.
            line_pct = (
                pct / 100.0 if pct > 1.5
                else float(pct)
            )
            missing_lines_raw = file_data.get("missing_lines") or ()
            missing = tuple(
                int(x) for x in missing_lines_raw
                if isinstance(x, (int, float))
            )
            out.append(FileCoverage(
                file_path=str(path),
                line_coverage_pct=max(0.0, min(1.0, line_pct)),
                branch_coverage_pct=0.0,
                lines_total=total,
                lines_covered=covered,
                missing_lines=missing,
            ))
        except Exception:  # noqa: BLE001
            continue
    return tuple(out)


def parse_coverage_xml(
    raw_text: str,
) -> Tuple[FileCoverage, ...]:
    """Parse Cobertura-format XML. NEVER raises.

    Expected shape:
      <coverage>
        <packages>
          <package>
            <classes>
              <class filename="path.py" line-rate="0.85"
                     branch-rate="0.70">
                <lines>
                  <line number="3" hits="1"/>
                  <line number="5" hits="0"/>
                </lines>
              </class>
            </classes>
          </package>
        </packages>
      </coverage>
    """
    if not raw_text:
        return ()
    try:
        root = ET.fromstring(raw_text)
    except ET.ParseError:
        return ()
    out: List[FileCoverage] = []
    # XPath search for all <class> elements regardless of nesting depth
    for cls in root.iter("class"):
        try:
            filename = cls.attrib.get("filename") or ""
            if not filename:
                continue
            line_rate = float(cls.attrib.get("line-rate", "0") or 0)
            branch_rate = float(
                cls.attrib.get("branch-rate", "0") or 0,
            )
            covered = 0
            total = 0
            missing: List[int] = []
            for line_elem in cls.iter("line"):
                try:
                    num = int(line_elem.attrib.get("number", "0"))
                    hits = int(line_elem.attrib.get("hits", "0"))
                    total += 1
                    if hits > 0:
                        covered += 1
                    else:
                        missing.append(num)
                except (ValueError, TypeError):
                    continue
            out.append(FileCoverage(
                file_path=filename,
                line_coverage_pct=max(0.0, min(1.0, line_rate)),
                branch_coverage_pct=max(0.0, min(1.0, branch_rate)),
                lines_total=total,
                lines_covered=covered,
                missing_lines=tuple(missing),
            ))
        except Exception:  # noqa: BLE001
            continue
    return tuple(out)


# Coverage data loaders


def _load_json_report(
    *, path_override: Optional[Path] = None,
) -> Tuple[FileCoverage, ...]:
    """Try to load JSON report. NEVER raises."""
    target = path_override or json_report_path()
    try:
        if not target.exists() or not target.is_file():
            return ()
        return parse_coverage_json(
            target.read_text(encoding="utf-8"),
        )
    except Exception:  # noqa: BLE001
        return ()


def _load_xml_report(
    *, path_override: Optional[Path] = None,
) -> Tuple[FileCoverage, ...]:
    """Try to load XML report. NEVER raises."""
    target = path_override or xml_report_path()
    try:
        if not target.exists() or not target.is_file():
            return ()
        return parse_coverage_xml(
            target.read_text(encoding="utf-8"),
        )
    except Exception:  # noqa: BLE001
        return ()


def _load_sqlite_via_coverage_py(
    *, path_override: Optional[Path] = None,
) -> Tuple[FileCoverage, ...]:
    """Compose coverage.py CoverageData API. NEVER raises.
    Returns empty when coverage.py unavailable."""
    target = path_override or sqlite_data_path()
    try:
        if not target.exists():
            return ()
    except Exception:  # noqa: BLE001
        return ()
    try:
        from coverage import CoverageData  # type: ignore
    except ImportError:
        return ()
    try:
        cd = CoverageData(basename=str(target))
        cd.read()
        out: List[FileCoverage] = []
        for filename in cd.measured_files():
            try:
                # CoverageData API differs across versions; use
                # the most stable accessors.
                lines = cd.lines(filename) or []
                lines_set = set(int(x) for x in lines)
                # Count executable lines from the file itself
                # (coverage.py knows them); fall back to lines_set.
                try:
                    arcs = cd.arcs(filename)
                    # Get line count from arc set if available
                    total = max(lines_set) if lines_set else 0
                except Exception:  # noqa: BLE001
                    total = max(lines_set) if lines_set else 0
                # Missing lines = lines in source not in lines_set
                # (coverage.py reports HIT lines; missing = absent).
                # Without source-line introspection, approximate.
                covered = len(lines_set)
                pct = (
                    covered / total if total > 0 else 0.0
                )
                out.append(FileCoverage(
                    file_path=str(filename),
                    line_coverage_pct=max(0.0, min(1.0, pct)),
                    branch_coverage_pct=0.0,
                    lines_total=total,
                    lines_covered=covered,
                    missing_lines=(),
                ))
            except Exception:  # noqa: BLE001
                continue
        return tuple(out)
    except Exception:  # noqa: BLE001
        return ()


_SUBPROCESS_LINE_RE = re.compile(
    r"^(?P<path>[\w./\\-]+\.py)\s+"
    r"(?P<stmts>\d+)\s+"
    r"(?P<miss>\d+)(?:\s+\d+\s+\d+)?\s+"
    r"(?P<pct>\d+(?:\.\d+)?)%"
)


def parse_coverage_report_stdout(
    stdout: str,
) -> Tuple[FileCoverage, ...]:
    """Parse `coverage report` plaintext stdout. NEVER raises.

    Expected format:
      Name              Stmts   Miss  Cover
      -------------------------------------
      backend/foo.py       10      2    80%
      backend/bar.py       20      0   100%
      -------------------------------------
      TOTAL                30      2    93%
    """
    if not stdout:
        return ()
    out: List[FileCoverage] = []
    try:
        for line in stdout.splitlines():
            m = _SUBPROCESS_LINE_RE.match(line.strip())
            if not m:
                continue
            path = m.group("path")
            if path.upper() == "TOTAL":
                continue
            try:
                stmts = int(m.group("stmts"))
                miss = int(m.group("miss"))
                pct = float(m.group("pct"))
                covered = max(0, stmts - miss)
                out.append(FileCoverage(
                    file_path=path,
                    line_coverage_pct=max(
                        0.0, min(1.0, pct / 100.0),
                    ),
                    branch_coverage_pct=0.0,
                    lines_total=stmts,
                    lines_covered=covered,
                    missing_lines=(),
                ))
            except (ValueError, TypeError):
                continue
    except Exception:  # noqa: BLE001
        return ()
    return tuple(out)


def _load_subprocess_report() -> Tuple[FileCoverage, ...]:
    """Invoke `python -m coverage report` and parse stdout.
    NEVER raises."""
    try:
        result = subprocess.run(
            ["python3", "-m", "coverage", "report"],
            capture_output=True,
            text=True,
            timeout=float(subprocess_timeout_s()),
            check=False,
        )
        if result.returncode != 0:
            return ()
        return parse_coverage_report_stdout(result.stdout or "")
    except Exception:  # noqa: BLE001
        return ()


def load_coverage_data(
    *,
    source_override: Optional[CoverageSource] = None,
    json_path_override: Optional[Path] = None,
    xml_path_override: Optional[Path] = None,
    sqlite_path_override: Optional[Path] = None,
) -> Tuple[CoverageSource, Tuple[FileCoverage, ...]]:
    """Compose all 4 sources in priority order. Returns
    ``(source, coverage_records)``. Empty tuple means no
    source found. NEVER raises.

    When ``source_override`` is provided, only that source
    is attempted (no fallback)."""
    sources_to_try: Sequence[CoverageSource]
    if source_override is not None:
        sources_to_try = (source_override,)
    else:
        sources_to_try = (
            CoverageSource.JSON,
            CoverageSource.XML,
            CoverageSource.SQLITE,
            CoverageSource.SUBPROCESS,
        )
    for src in sources_to_try:
        if src is CoverageSource.JSON:
            records = _load_json_report(
                path_override=json_path_override,
            )
        elif src is CoverageSource.XML:
            records = _load_xml_report(
                path_override=xml_path_override,
            )
        elif src is CoverageSource.SQLITE:
            records = _load_sqlite_via_coverage_py(
                path_override=sqlite_path_override,
            )
        else:  # SUBPROCESS
            records = _load_subprocess_report()
        if records:
            return src, records
    # When source_override was supplied, preserve it in the
    # empty result so the caller's diagnostic reflects WHICH
    # source they asked for (not the priority-default JSON).
    if source_override is not None:
        return source_override, ()
    return CoverageSource.JSON, ()


# Verdict + assessment


def _verdict_for_overall_pct(
    overall_pct: float,
    *,
    has_data: bool,
) -> CoverageVerdict:
    """Pure classifier. NEVER raises."""
    if not has_data:
        return CoverageVerdict.DISABLED
    floor = floor_threshold()
    strong = strong_threshold()
    if overall_pct < floor:
        return CoverageVerdict.BELOW_FLOOR
    if overall_pct < strong:
        return CoverageVerdict.ACCEPTABLE
    return CoverageVerdict.STRONG


def _normalize_path(p: str) -> str:
    """Canonical path normalization for matching. NEVER raises."""
    try:
        return str(p).replace("\\", "/").strip()
    except Exception:  # noqa: BLE001
        return ""


def _find_coverage_for_file(
    target_file: str,
    records: Sequence[FileCoverage],
) -> Optional[FileCoverage]:
    """Match target_file against records' file_path values.
    Tries exact match first, then suffix/basename match for
    cases where one side has a fuller path prefix. NEVER raises."""
    if not target_file or not records:
        return None
    norm_target = _normalize_path(target_file)
    if not norm_target:
        return None
    # Pass 1 — exact match
    for rec in records:
        if _normalize_path(rec.file_path) == norm_target:
            return rec
    # Pass 2 — basename match (handles repo-relative vs absolute)
    try:
        target_base = Path(norm_target).name
    except Exception:  # noqa: BLE001
        return None
    for rec in records:
        try:
            if Path(_normalize_path(rec.file_path)).name == target_base:
                return rec
        except Exception:  # noqa: BLE001
            continue
    # Pass 3 — suffix match (target ends with record path or vice versa)
    for rec in records:
        rec_path = _normalize_path(rec.file_path)
        if not rec_path:
            continue
        if (
            norm_target.endswith(rec_path)
            or rec_path.endswith(norm_target)
        ):
            return rec
    return None


def evaluate_coverage(
    target_files: Sequence[str],
    *,
    source_override: Optional[CoverageSource] = None,
    records_override: Optional[Sequence[FileCoverage]] = None,
    json_path_override: Optional[Path] = None,
    xml_path_override: Optional[Path] = None,
    sqlite_path_override: Optional[Path] = None,
    now_unix: Optional[float] = None,
) -> CoverageReport:
    """Top-level evaluation. NEVER raises.

    Parameters
    ----------
    target_files:
        Proposed-change file paths to evaluate against.
    source_override:
        Force a specific source; default tries all 4 in priority.
    records_override:
        Testing seam — pass FileCoverage records directly.
    json/xml/sqlite_path_override:
        Operator/testing path overrides for each source."""
    started = time.time() if now_unix is None else float(now_unix)
    if not master_enabled():
        return CoverageReport(
            evaluated_at_unix=started,
            master_enabled=False,
            verdict=CoverageVerdict.DISABLED,
            source=CoverageSource.JSON,
            overall_pct=0.0,
            floor_threshold=floor_threshold(),
            strong_threshold=strong_threshold(),
            per_file=(),
            missing_files=(),
            boundary_crossed=False,
            diagnostic=f"gate disabled via {_ENV_MASTER}=false",
            elapsed_s=0.0,
        )

    files = tuple(
        _normalize_path(f) for f in target_files
        if _normalize_path(f)
    )
    if not files:
        return CoverageReport(
            evaluated_at_unix=started,
            master_enabled=True,
            verdict=CoverageVerdict.DISABLED,
            source=CoverageSource.JSON,
            overall_pct=0.0,
            floor_threshold=floor_threshold(),
            strong_threshold=strong_threshold(),
            per_file=(),
            missing_files=(),
            boundary_crossed=False,
            diagnostic="no target_files supplied",
            elapsed_s=max(0.0, time.time() - started),
        )

    # Load coverage data.
    if records_override is not None:
        records = tuple(records_override)
        # When records are injected, pick the operator-supplied
        # source label (or default to JSON).
        source = source_override or CoverageSource.JSON
    else:
        source, records = load_coverage_data(
            source_override=source_override,
            json_path_override=json_path_override,
            xml_path_override=xml_path_override,
            sqlite_path_override=sqlite_path_override,
        )

    if not records:
        return CoverageReport(
            evaluated_at_unix=started,
            master_enabled=True,
            verdict=CoverageVerdict.DISABLED,
            source=source,
            overall_pct=0.0,
            floor_threshold=floor_threshold(),
            strong_threshold=strong_threshold(),
            per_file=(),
            missing_files=files,
            boundary_crossed=_is_boundary_crossed(files),
            diagnostic="no coverage data found across all 4 sources",
            elapsed_s=max(0.0, time.time() - started),
        )

    # Match per-file.
    per_file: List[FileCoverage] = []
    missing: List[str] = []
    penalty = missing_file_penalty()
    pcts: List[float] = []
    for f in files:
        match = _find_coverage_for_file(f, records)
        if match is not None:
            per_file.append(match)
            pcts.append(match.line_coverage_pct)
        else:
            missing.append(f)
            pcts.append(penalty)

    overall = sum(pcts) / len(pcts) if pcts else 0.0
    verdict = _verdict_for_overall_pct(overall, has_data=True)
    boundary = _is_boundary_crossed(files)
    diagnostic = (
        f"overall={overall:.2%} (floor={floor_threshold():.0%} "
        f"strong={strong_threshold():.0%}); "
        f"matched={len(per_file)} missing={len(missing)} "
        f"via {source.value}"
    )

    report = CoverageReport(
        evaluated_at_unix=started,
        master_enabled=True,
        verdict=verdict,
        source=source,
        overall_pct=overall,
        floor_threshold=floor_threshold(),
        strong_threshold=strong_threshold(),
        per_file=tuple(per_file),
        missing_files=tuple(missing),
        boundary_crossed=boundary,
        diagnostic=diagnostic,
        elapsed_s=max(0.0, time.time() - started),
    )
    _persist_report(report)
    _publish_event(report)
    return report


def _persist_report(report: CoverageReport) -> None:
    if report.verdict is CoverageVerdict.DISABLED:
        return
    _flock_append({
        "kind": "coverage_report", "payload": report.to_dict(),
    })


def _publish_event(report: CoverageReport) -> None:
    if not master_enabled():
        return
    if report.verdict is CoverageVerdict.DISABLED:
        return
    try:
        from backend.core.ouroboros.governance.ide_observability_stream import (  # noqa: E501
            EVENT_TYPE_COVERAGE_GATE_EVALUATED,
            publish_task_event,
        )
        publish_task_event(
            EVENT_TYPE_COVERAGE_GATE_EVALUATED,
            (
                f"system::coverage_gate::"
                f"{report.schema_version}"
            ),
            {
                "verdict": report.verdict.value,
                "source": report.source.value,
                "overall_pct": report.overall_pct,
                "floor_threshold": report.floor_threshold,
                "strong_threshold": report.strong_threshold,
                "matched_count": len(report.per_file),
                "missing_count": len(report.missing_files),
                "boundary_crossed": report.boundary_crossed,
                "elapsed_s": report.elapsed_s,
                "schema_version": report.schema_version,
            },
        )
    except Exception:  # noqa: BLE001
        return


def format_coverage_panel(
    report: Optional[CoverageReport] = None,
) -> str:
    """NEVER raises."""
    if report is None:
        if not master_enabled():
            return (
                f"coverage gate: disabled "
                f"({_ENV_MASTER}=false)"
            )
        return "coverage gate: no report"
    if not report.master_enabled:
        return (
            f"coverage gate: disabled "
            f"({_ENV_MASTER}=false)"
        )
    vg = verdict_glyph(report.verdict)
    sg = source_glyph(report.source)
    lines = [
        f"📊 Coverage Gate  {vg} {report.verdict.value}",
        f"  source           : {sg} {report.source.value}",
        f"  overall          : {report.overall_pct:.1%}",
        f"  floor            : {report.floor_threshold:.0%}",
        f"  strong           : {report.strong_threshold:.0%}",
        f"  matched          : {len(report.per_file)}",
        f"  missing          : {len(report.missing_files)}",
    ]
    if report.per_file:
        lines.append("  per-file:")
        for f in report.per_file[:5]:
            lines.append(
                f"    {f.file_path[:48]:<48} "
                f"{f.line_coverage_pct:.1%} "
                f"({f.lines_covered}/{f.lines_total})"
            )
        if len(report.per_file) > 5:
            lines.append(
                f"    ... (+{len(report.per_file) - 5} more)"
            )
    if report.missing_files:
        lines.append(
            f"  missing files (top 3): "
            f"{list(report.missing_files[:3])}"
        )
    lines.append(f"  diagnostic       : {report.diagnostic}")
    return "\n".join(lines)


# AST pins


def register_shipped_invariants() -> list:
    try:
        from backend.core.ouroboros.governance.meta.shipped_code_invariants import (  # noqa: E501
            ShippedCodeInvariant,
        )
    except ImportError:
        return []

    target = (
        "backend/core/ouroboros/governance/coverage_gate.py"
    )

    _EXPECTED_VERDICTS = {
        "below_floor", "acceptable", "strong", "disabled",
    }
    _EXPECTED_SOURCES = {
        "json", "xml", "sqlite", "subprocess",
    }

    def _validate_taxonomy(class_name: str, expected: set):
        def _validate(tree: ast.AST, source: str) -> tuple:  # noqa: ARG001
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ClassDef)
                    and node.name == class_name
                ):
                    found = set()
                    for sub in node.body:
                        if (
                            isinstance(sub, ast.Assign)
                            and len(sub.targets) == 1
                            and isinstance(sub.targets[0], ast.Name)
                            and isinstance(sub.value, ast.Constant)
                            and isinstance(sub.value.value, str)
                        ):
                            found.add(sub.value.value)
                    missing = expected - found
                    extra = found - expected
                    if missing:
                        return (
                            f"{class_name} missing: "
                            f"{sorted(missing)}",
                        )
                    if extra:
                        return (
                            f"{class_name} drift: "
                            f"{sorted(extra)}",
                        )
                    return ()
            return (f"{class_name} class not found",)
        return _validate

    def _validate_authority_asymmetry(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        forbidden = (
            "backend.core.ouroboros.governance.orchestrator",
            "backend.core.ouroboros.governance.iron_gate",
            "backend.core.ouroboros.governance.policy",
            "backend.core.ouroboros.governance.providers",
            "backend.core.ouroboros.governance.candidate_generator",
            "backend.core.ouroboros.governance.urgency_router",
            "backend.core.ouroboros.governance.change_engine",
            "backend.core.ouroboros.governance.semantic_guardian",
            "backend.core.ouroboros.governance.auto_committer",
            "backend.core.ouroboros.governance.risk_tier_floor",
            "backend.core.ouroboros.governance.tool_executor",
            "backend.core.ouroboros.governance.plan_generator",
        )
        violations: List[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if any(mod == f for f in forbidden):
                    violations.append(
                        f"forbidden authority import: {mod}",
                    )
        return tuple(violations)

    def _validate_master_default_false(
        tree: ast.AST, source: str,  # noqa: ARG001
    ) -> tuple:
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "master_enabled"
            ):
                for sub in ast.walk(node):
                    if (
                        isinstance(sub, ast.Call)
                        and isinstance(sub.func, ast.Name)
                        and sub.func.id == "_flag"
                    ):
                        for kw in sub.keywords:
                            if (
                                kw.arg == "default"
                                and isinstance(kw.value, ast.Constant)
                                and kw.value.value is False
                            ):
                                return ()
                return (
                    "master_enabled() must call _flag(...) "
                    "with default=False per §33.1",
                )
        return ("master_enabled() not found",)

    def _validate_composes_canonical(
        tree: ast.AST, source: str,
    ) -> tuple:
        violations: List[str] = []
        if "governance_boundary_gate" not in source:
            violations.append(
                "must compose Wave 2 #5 "
                "governance_boundary_gate (cage detection)",
            )
        if "cross_process_jsonl" not in source:
            violations.append(
                "must compose cross_process_jsonl",
            )
        if (
            "xml.etree.ElementTree" not in source
            and "xml.etree" not in source
        ):
            violations.append(
                "must compose stdlib xml.etree (XML parser)",
            )
        if "import json" not in source:
            violations.append(
                "must compose stdlib json (JSON parser)",
            )
        if "subprocess" not in source:
            violations.append(
                "must compose stdlib subprocess "
                "(coverage CLI fallback)",
            )
        return tuple(violations)

    return [
        ShippedCodeInvariant(
            invariant_name=(
                "coverage_gate_verdict_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "CoverageVerdict 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "CoverageVerdict", _EXPECTED_VERDICTS,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "coverage_gate_source_taxonomy_closed"
            ),
            target_file=target,
            description=(
                "CoverageSource 4-value taxonomy bytes-pinned."
            ),
            validate=_validate_taxonomy(
                "CoverageSource", _EXPECTED_SOURCES,
            ),
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "coverage_gate_authority_asymmetry"
            ),
            target_file=target,
            description=(
                "Substrate purity — advisory only. MUST NOT "
                "import orchestrator / iron_gate / policy / "
                "etc / plan_generator. Operator-side wiring "
                "composes Iron Gate via the verdict; substrate "
                "exposes verdict only."
            ),
            validate=_validate_authority_asymmetry,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "coverage_gate_master_default_false"
            ),
            target_file=target,
            description="§33.1 default-FALSE.",
            validate=_validate_master_default_false,
        ),
        ShippedCodeInvariant(
            invariant_name=(
                "coverage_gate_composes_canonical"
            ),
            target_file=target,
            description=(
                "Substrate composes Wave 2 #5 "
                "governance_boundary_gate + cross_process_jsonl "
                "+ stdlib xml.etree + stdlib json + stdlib "
                "subprocess. coverage.py is OPTIONAL via lazy "
                "import (degrades to subprocess source)."
            ),
            validate=_validate_composes_canonical,
        ),
    ]


def register_flags(registry: Any) -> int:
    from backend.core.ouroboros.governance.flag_registry import (
        Category,
        FlagSpec,
        FlagType,
    )

    src = (
        "backend/core/ouroboros/governance/coverage_gate.py"
    )

    seeds = [
        FlagSpec(
            name=_ENV_MASTER,
            type=FlagType.BOOL,
            default=False,
            description=(
                "Coverage Gate master. §33.1 default-FALSE. "
                "Closes §41.4 Phase 1 sixth arc (PRD v3.0+). "
                "Advisory — substrate emits verdict; operator-"
                "side wiring composes Iron Gate."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=f"{_ENV_MASTER}=true",
        ),
        FlagSpec(
            name=_ENV_PERSIST,
            type=FlagType.BOOL,
            default=True,
            description="Sub-flag — §33.4 ledger writes.",
            category=Category.SAFETY,
            source_file=src,
            example=f"{_ENV_PERSIST}=false",
        ),
        FlagSpec(
            name=_ENV_FLOOR_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_FLOOR_THRESHOLD,
            description=(
                "overall_pct below this → BELOW_FLOOR verdict. "
                "Default 0.60 (60%)."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_FLOOR_THRESHOLD}=0.70",
        ),
        FlagSpec(
            name=_ENV_STRONG_THRESHOLD,
            type=FlagType.FLOAT,
            default=_DEFAULT_STRONG_THRESHOLD,
            description=(
                "overall_pct ≥ this → STRONG verdict. Default "
                "0.85. Auto-clamped ≥ floor."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_STRONG_THRESHOLD}=0.90",
        ),
        FlagSpec(
            name=_ENV_MISSING_FILE_PENALTY,
            type=FlagType.FLOAT,
            default=_DEFAULT_MISSING_FILE_PENALTY,
            description=(
                "Coverage attributed to files missing from "
                "report. Default 0.0 (missing = 0%)."
            ),
            category=Category.TUNING,
            source_file=src,
            example=f"{_ENV_MISSING_FILE_PENALTY}=0.5",
        ),
        FlagSpec(
            name=_ENV_JSON_REPORT_PATH,
            type=FlagType.STR,
            default="",
            description=(
                "Path to coverage.json. Default 'coverage.json' "
                "in cwd."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=(
                f"{_ENV_JSON_REPORT_PATH}=/repo/build/cov.json"
            ),
        ),
        FlagSpec(
            name=_ENV_XML_REPORT_PATH,
            type=FlagType.STR,
            default="",
            description=(
                "Path to coverage.xml (Cobertura). Default "
                "'coverage.xml' in cwd."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=(
                f"{_ENV_XML_REPORT_PATH}=/repo/build/cov.xml"
            ),
        ),
        FlagSpec(
            name=_ENV_SQLITE_PATH,
            type=FlagType.STR,
            default="",
            description=(
                "Path to .coverage SQLite. Default '.coverage' "
                "in cwd."
            ),
            category=Category.INTEGRATION,
            source_file=src,
            example=(
                f"{_ENV_SQLITE_PATH}=/repo/build/.coverage"
            ),
        ),
        FlagSpec(
            name=_ENV_SUBPROCESS_TIMEOUT_S,
            type=FlagType.INT,
            default=_DEFAULT_SUBPROCESS_TIMEOUT_S,
            description=(
                "Timeout for `coverage report` subprocess. "
                "Default 30s."
            ),
            category=Category.TIMING,
            source_file=src,
            example=f"{_ENV_SUBPROCESS_TIMEOUT_S}=60",
        ),
    ]

    count = 0
    for spec in seeds:
        try:
            registry.register(spec)
            count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


__all__ = [
    "COVERAGE_GATE_SCHEMA_VERSION",
    "CoverageVerdict",
    "CoverageSource",
    "FileCoverage",
    "CoverageReport",
    "master_enabled",
    "persistence_enabled",
    "floor_threshold",
    "strong_threshold",
    "missing_file_penalty",
    "subprocess_timeout_s",
    "json_report_path",
    "xml_report_path",
    "sqlite_data_path",
    "ledger_path",
    "verdict_glyph",
    "source_glyph",
    "parse_coverage_json",
    "parse_coverage_xml",
    "parse_coverage_report_stdout",
    "load_coverage_data",
    "evaluate_coverage",
    "format_coverage_panel",
    "register_shipped_invariants",
    "register_flags",
]

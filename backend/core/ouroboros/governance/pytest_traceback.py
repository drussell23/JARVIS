"""Deterministic traceback resolution for the micro-fix repair lane.

Why this exists
---------------

The micro-fix loop refuses to patch unless it knows *which line* failed, and
it was refusing on every op with ``error_type=UnknownError line=0``. The
existing regex cascade already understood five pytest shapes, so the missing
piece was not pytest literacy. Reproducing the real subprocess found four
separate faults, three of which are worse than the refusal they caused:

1. ``pytest.ini`` carries ``--color=yes``, which forces ANSI escapes even
   into a pipe. Every pattern in the cascade is line-anchored (``^E\\s+``,
   ``^(\\S+):(\\d+):``) and an escape sequence sits in front of the anchor,
   so nothing matched and everything fell through to ``UnknownError``. A
   bare-temp-dir reproduction hides this exactly, because it has no
   ``pytest.ini``.

2. The short-summary branch reported ``line_number=1`` when it could not
   find a real one. The hard guard only rejects ``line <= 0``, so a
   fabricated 1 sailed through it -- the loop would patch the top of the
   file, which is the blind-patching corruption the guard exists to prevent.

3. Frames were taken from the end of the traceback without asking who owns
   them, so a failure resolved into ``pathlib.py`` or ``_pytest/python.py``
   -- the loop would have edited the standard library.

4. The run was unscoped, so pytest collected the whole repository and ``-x``
   stopped at the first ambient failure, which is some other file entirely.
   That is fixed at the call site; this module makes the consequence
   impossible to act on by refusing frames the candidate does not own.

What it guarantees
------------------

A location is returned only when it is a line this repository controls, that
exists, and that carries a statement. Otherwise the answer is ``None`` and
the caller drops the repair. Nothing here asks a model where the failure is:
the traceback already says, and a guess is what produced the corruption this
lane is guarded against.
"""
from __future__ import annotations

import ast
import asyncio
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, Optional, Sequence, Tuple

logger = logging.getLogger("Ouroboros.PytestTraceback")

# CSI sequences, including the private-parameter forms pytest's terminal
# writer emits. Applied before any matching: see fault (1).
_ANSI_RE = re.compile(r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])")

# A path segment that means "not our source". This is the authority for the
# traceback-ownership question only. Several modules carry a private
# ``_SKIP_DIRS`` for directory walks; those answer "should I descend into
# this while scanning the tree", which is a different question with a
# different correct answer (a walk may skip ``.git``; a traceback never
# names it), so they are deliberately not shared.
_VENDORED_SEGMENTS = frozenset({
    "site-packages", "dist-packages", "node_modules",
    ".venv", "venv", ".tox", ".nox", ".eggs", "__pypackages__",
})

# pytest frame:  tests/foo.py:42: in test_bar
#                tests/foo.py:42: AssertionError
# The function clause is optional because pytest omits it on the final
# frame, which is usually the one that matters.
_PYTEST_FRAME = re.compile(
    r"^(?P<path>[^\s:][^:\n]*\.py):(?P<line>\d+):"
    r"(?:\s+in\s+(?P<func>\S+))?\s*(?P<tail>.*)$",
    re.MULTILINE,
)

# stdlib frame:  File "/x/y.py", line 42, in fn
# The optional ``E`` prefix is not cosmetic: pytest reports a collection
# SyntaxError as ``E     File "...", line 1``, and requiring a bare line
# start dropped the single most repairable failure there is -- the engine
# refused it as unattributable and the candidate never got fixed. ``File
# "..."`` is unambiguous enough to read through the marker, which is why the
# pytest frame pattern below still refuses ``E`` lines and this one does not.
_STDLIB_FRAME = re.compile(
    r'^(?:E\s+)?\s*File "(?P<path>[^"]+)", line (?P<line>\d+)'
    r'(?:, in (?P<func>\S+))?',
    re.MULTILINE,
)

# pytest's error line:  E   AssertionError: assert 1 == 2
_ERROR_LINE = re.compile(
    r"^E\s+(?P<etype>[A-Za-z_][\w.]*"
    r"(?:Error|Exception|Warning|Failure|Exit|Interrupted|Skipped))"
    r"\s*(?::\s*(?P<msg>.*))?$",
    re.MULTILINE,
)

# Bare terminal exception line:  AssertionError: assert 1 == 2
_BARE_ERROR = re.compile(
    r"^(?P<etype>[A-Za-z_][\w.]*(?:Error|Exception))\s*(?::\s*(?P<msg>.*))?$",
    re.MULTILINE,
)

# A plain failed assertion:  E       assert 1 == 2
# pytest names no exception class for these -- it prints the rewritten
# expression and nothing else -- so without this they arrive as an untyped
# "PytestFailure" carrying no message, and the repair prompt is told a test
# failed without being told what it asserted.
_ASSERT_LINE = re.compile(r"^E\s+(?P<msg>assert\s.*)$", re.MULTILINE)

# pytest's section rule:  ======= FAILURES =======   /   ==== 2 failed in 0.1s ====
# The report is a sequence of these rules, each followed by its body. The
# title is kept verbatim (case-folded as a key) rather than matched against a
# list, so a section this module has never heard of is still a section.
_SECTION_RULE = re.compile(r"^=+ (?P<title>.+?) =+$", re.MULTILINE)

#: The sections that say WHAT failed, in the order pytest prints them.
FAILURE_SECTIONS: Tuple[str, ...] = ("errors", "failures")
#: The section pytest writes one ``FAILED id - message`` line per failure into.
SUMMARY_SECTION = "short test summary info"


@dataclass(frozen=True)
class Frame:
    """One traceback frame, exactly as the traceback stated it."""

    file_path: str
    line_number: int
    function: str = ""


@dataclass(frozen=True)
class ParsedFailure:
    """A failure located on a line this repository owns."""

    error_type: str
    message: str
    file_path: str
    line_number: int
    frames: Tuple[Frame, ...]
    resolution: str
    """How the frame was chosen -- ``candidate``, ``repo``. Carried so an
    operator can tell a frame in the file under repair from one merely
    somewhere in the repo, without re-deriving it from the log."""


def strip_ansi(text: str) -> str:
    """Remove terminal control sequences. NEVER raises."""
    try:
        return _ANSI_RE.sub("", text or "")
    except Exception:  # noqa: BLE001
        return text or ""


def report_sections(output: str) -> Dict[str, str]:
    """pytest's report split on its own ``=== title ===`` rules. NEVER raises.

    Keyed by the case-folded title; the value is the text between that rule
    and the next, ANSI already stripped. A title repeated by pytest (it does
    not, today) keeps both bodies, joined in printed order.
    """
    out: Dict[str, str] = {}
    try:
        clean = strip_ansi(output)
        rules = list(_SECTION_RULE.finditer(clean))
        for i, rule in enumerate(rules):
            end = rules[i + 1].start() if i + 1 < len(rules) else len(clean)
            key = rule.group("title").strip().casefold()
            body = clean[rule.end():end].strip("\n")
            out[key] = f"{out[key]}\n{body}" if key in out else body
    except Exception:  # noqa: BLE001
        logger.debug("[PytestTraceback] section parse degraded", exc_info=True)
    return out


@dataclass(frozen=True)
class FailureEvidence:
    """What a test run said about its failure, in the words a repair needs.

    ``summary`` is one line per failure (pytest's own ``FAILED id - message``
    lines); ``trace`` is the tracebacks behind them. Both ANSI-free and bounded
    by :func:`epistemic_feedback.trace_max_chars`.
    """

    summary: str
    trace: str


def failure_evidence(stdout: str, stderr: str = "") -> FailureEvidence:
    """The failure a test run reported, read from BOTH of its streams. NEVER raises.

    ## Why this exists

    The L2 repair loop told the model what failed with
    ``(stdout + stderr)[:300]`` and ``stderr``. pytest writes its failures to
    STDOUT, after a session header and a progress bar, and ``pytest.ini``
    forces ``--color=yes`` into the pipe: measured through the real
    ``RepairSandbox``, those 300 characters were the header and a red ``FF``,
    and stderr was empty. Every repair iteration in bt-2026-09-21-235603 was
    asked to fix a test without being told what it asserted -- 40 iterations,
    zero converged, seven ops ended ``class_retries_exhausted:test``.

    ## What counts as evidence

    pytest's ``errors``/``failures`` sections when it printed any; otherwise
    (a timeout, an interpreter crash, a collection abort, a non-pytest runner)
    the whole cleaned output, because then the output IS the evidence and
    nothing here can know which part matters. stderr is appended when it says
    anything, never dropped. The summary falls back to the last error the
    output names, then to the trace's last line, so it is never empty while
    the run said something.
    """
    from backend.core.ouroboros.governance import epistemic_feedback as _ef  # noqa: PLC0415

    try:
        out = strip_ansi(stdout or "").strip("\n")
        err = strip_ansi(stderr or "").strip("\n")
        sections = report_sections(out)
        failed = [sections[name] for name in FAILURE_SECTIONS if sections.get(name)]
        if failed:
            trace = "\n\n".join(failed)
            if err.strip():
                trace = f"{trace}\n\n--- stderr ---\n{err}"
        else:
            trace = "\n".join(part for part in (out, err) if part.strip())
        summary = sections.get(SUMMARY_SECTION, "").strip()
        if not summary:
            etype, message = parse_error(f"{out}\n{err}")
            summary = ": ".join(part for part in (etype, message) if part)
        if not summary and trace.strip():
            summary = trace.strip().splitlines()[-1]
        budget = _ef.trace_max_chars()
        return FailureEvidence(
            summary=_ef.truncate_middle(summary, budget),
            trace=_ef.truncate_middle(trace, budget),
        )
    except Exception:  # noqa: BLE001 — evidence is best-effort, never fatal
        logger.debug("[PytestTraceback] failure evidence degraded", exc_info=True)
        raw = "\n".join(part for part in (stdout or "", stderr or "") if part)
        return FailureEvidence(summary="", trace=strip_ansi(raw))


def is_vendored(path: str) -> bool:
    """Whether *path* is third-party or an interpreter's own library.

    Segment-wise rather than substring: a repository legitimately named
    ``.../venv-tooling/...`` is not vendored, and ``in`` on the raw string
    would say it was.
    """
    try:
        parts = set(Path(path).parts)
    except Exception:  # noqa: BLE001
        return True
    if parts & _VENDORED_SEGMENTS:
        return True
    # CPython's own library, which no repo root contains.
    return "/lib/python3." in path.replace("\\", "/")


def _owned(path: str, repo_root: Path) -> bool:
    """Whether *path* is a source file this repository controls."""
    if not path or is_vendored(path):
        return False
    try:
        candidate = Path(path)
        if not candidate.is_absolute():
            # pytest prints repo-relative paths; they are ours by definition
            # only if they resolve inside the root.
            candidate = repo_root / candidate
        candidate.resolve().relative_to(repo_root.resolve())
        return True
    except Exception:  # noqa: BLE001
        return False


def parse_frames(output: str) -> Tuple[Frame, ...]:
    """Every frame the output names, outermost first. NEVER raises.

    Both traceback dialects are collected and kept in the order they were
    printed, so "innermost" is simply the last one -- which is what a stack
    walk needs and what neither dialect states explicitly.
    """
    frames: list = []
    try:
        clean = strip_ansi(output)
        seen: set = set()
        for match in _PYTEST_FRAME.finditer(clean):
            # `E   AssertionError: x.py:1: whatever` is error text, not a
            # frame; pytest never indents a real frame line.
            if match.group(0).startswith(("E ", "E\t")):
                continue
            key = (match.start(), match.group("path"))
            if key in seen:
                continue
            seen.add(key)
            frames.append((match.start(), Frame(
                file_path=match.group("path"),
                line_number=int(match.group("line")),
                function=(match.group("func") or "").strip(),
            )))
        for match in _STDLIB_FRAME.finditer(clean):
            frames.append((match.start(), Frame(
                file_path=match.group("path"),
                line_number=int(match.group("line")),
                function=(match.group("func") or "").strip(),
            )))
    except Exception:  # noqa: BLE001
        logger.debug("[PytestTraceback] frame parse degraded", exc_info=True)
    frames.sort(key=lambda pair: pair[0])
    return tuple(frame for _pos, frame in frames)


def parse_error(output: str) -> Tuple[str, str]:
    """``(error_type, message)`` for the LAST error the output names.

    Last rather than first: with ``-x`` the final error is the one that
    stopped the run, and a summary block may repeat earlier ones.
    """
    try:
        clean = strip_ansi(output)
        matches = list(_ERROR_LINE.finditer(clean)) or list(
            _BARE_ERROR.finditer(clean)
        )
        if matches:
            last = matches[-1]
            return (
                (last.group("etype") or "").strip(),
                (last.group("msg") or "").strip(),
            )
        asserts = list(_ASSERT_LINE.finditer(clean))
        if asserts:
            return "AssertionError", asserts[-1].group("msg").strip()
        return "", ""
    except Exception:  # noqa: BLE001
        return "", ""


def resolve_owned_frame(
    frames: Sequence[Frame],
    *,
    repo_root: Path,
    preferred_paths: Iterable[str] = (),
) -> Tuple[Optional[Frame], str]:
    """The innermost frame this repository owns, walking the stack backwards.

    A failure that surfaces in a fixture, a helper, or the standard library
    is still OUR failure, but it is not OUR line: patching ``pathlib.py``
    because ``read_text`` raised there is worse than not patching at all. So
    the walk starts at the innermost frame and moves outwards until it
    reaches code the repo controls.

    Frames in *preferred_paths* -- the files the candidate actually proposes
    -- outrank other repo frames regardless of depth. Without that, a failure
    raised inside a repo-owned conftest or helper would send the repair at a
    file the op never proposed and has no sanction to edit.

    Returns ``(None, reason)`` when nothing qualifies. That is a real answer:
    the caller drops the attempt.
    """
    if not frames:
        return None, "no_frames"
    preferred = {str(p) for p in preferred_paths if p}

    owned: list = []
    for frame in frames:
        if _owned(frame.file_path, repo_root):
            owned.append(frame)
    if not owned:
        return None, "no_repo_owned_frame"

    if preferred:
        for frame in reversed(owned):
            name = frame.file_path
            if name in preferred or any(
                name.endswith(p) or p.endswith(name) for p in preferred
            ):
                return frame, "candidate"
    return owned[-1], "repo"


def _verify_line(path: Path, line_number: int) -> bool:
    """Whether *line_number* is a real statement-bearing line in *path*.

    The traceback can outlive the text it describes -- the repair loop
    rewrites the file between iterations. A line past the end, or one
    carrying only a comment or blank space, means the location is stale and
    a patch aimed at it would land on unrelated code.

    A file that does not parse is NOT rejected: a SyntaxError candidate is
    precisely what the micro-fix should be repairing, and pytest reports its
    line accurately.
    """
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return False
    lines = text.splitlines()
    if line_number < 1 or line_number > len(lines):
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return True
    except Exception:  # noqa: BLE001
        return False
    for node in ast.walk(tree):
        start = getattr(node, "lineno", None)
        if start is None:
            continue
        end = getattr(node, "end_lineno", None) or start
        if start <= line_number <= end:
            return True
    return False


async def parse_failure(
    output: str,
    *,
    repo_root: Path,
    preferred_paths: Iterable[str] = (),
) -> Optional[ParsedFailure]:
    """Locate a failure on a line this repository owns, or return ``None``.

    Async because the verification step reads and parses source files: on a
    deep traceback that is real blocking I/O, and this runs inside the
    orchestrator's event loop alongside heartbeats and the control plane,
    which a synchronous read would stall. The parsing itself is pure.
    """
    frames = parse_frames(output)
    frame, resolution = resolve_owned_frame(
        frames, repo_root=repo_root, preferred_paths=preferred_paths,
    )
    if frame is None:
        logger.info(
            "[PytestTraceback] unattributable failure (%s): %d frame(s), "
            "none owned by %s — dropping rather than guessing",
            resolution, len(frames), repo_root,
        )
        return None

    absolute = Path(frame.file_path)
    if not absolute.is_absolute():
        absolute = repo_root / frame.file_path
    try:
        verified = await asyncio.to_thread(
            _verify_line, absolute, frame.line_number,
        )
    except Exception:  # noqa: BLE001
        verified = False
    if not verified:
        logger.info(
            "[PytestTraceback] %s:%d does not carry a statement — stale or "
            "out-of-range location, dropping rather than patching it",
            frame.file_path, frame.line_number,
        )
        return None

    etype, message = parse_error(output)
    return ParsedFailure(
        error_type=etype or "PytestFailure",
        message=message,
        file_path=frame.file_path,
        line_number=frame.line_number,
        frames=frames,
        resolution=resolution,
    )


__all__ = [
    "Frame",
    "ParsedFailure",
    "is_vendored",
    "parse_error",
    "parse_failure",
    "parse_frames",
    "resolve_owned_frame",
    "strip_ansi",
]

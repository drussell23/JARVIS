"""Unified-diff placement -- the ONE applier, for O+V and for the reward that trains it.

O+V edits an existing file by unified diff, and a hunk lands by a ladder:
verbatim at the stated position, then verbatim or whitespace-stripped within
``OUROBOROS_DIFF_FUZZY_WINDOW`` lines, then ANCHORED (its non-blank context
and removal lines located in order anywhere in the file, nearest the stated
position first). That ladder lived inside ``providers.py``.

Reactor's GRPO reward needs the same answer -- does this completion's diff
land in the file it was written against, and how nearly -- and it runs in a
different venv that cannot import the governance package. Measured
2026-10-10, before this module: the reward graded a ``2b.1-diff`` answer by
``ast.parse`` of the DIFF TEXT, so a correct diff and a hallucinated one
both scored 0.25 (``syntax_error:line1``) and any whole-file answer scored
0.79. Training could not see placement at all, and pushed away from the
contract O+V enforces.

So the ladder is here, standard library only, loadable BY PATH: the
training handoff names this file to the trainer
(``REACTOR_GRPO_DIFF_APPLIER``), which grades every diff with exactly the
code that will apply it in production -- no second copy to drift.

Also here, because both are about what the model was SHOWN:

* :func:`source_from_prompt` -- the target file as the prompt carried it
  (whole, truncated, or a numbered region), current and legacy renderings.
* :func:`alignment` -- per hunk: did it place, how much of its context
  exists in the file and in order, how far its header was from where it
  landed, and whether it copied line numbers.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

try:
    from backend.core.ouroboros.governance import source_lines
except ImportError:  # loaded by path (Reactor's venv): the sibling file is the module
    import importlib.util as _ilu
    _name = "_ov_source_lines"
    source_lines = sys.modules.get(_name)  # type: ignore[assignment]
    if source_lines is None:
        _spec = _ilu.spec_from_file_location(
            _name, os.path.join(os.path.dirname(os.path.abspath(__file__)), "source_lines.py"))
        source_lines = _ilu.module_from_spec(_spec)  # type: ignore[assignment]
        sys.modules[_name] = source_lines  # type: ignore[assignment]
        _spec.loader.exec_module(source_lines)  # type: ignore[union-attr]

Ops = List[Tuple[str, str]]


def fuzzy_window() -> int:
    """How far (±lines) a hunk may sit from the position its header states —
    ``OUROBOROS_DIFF_FUZZY_WINDOW`` (default 15). ONE accessor for the
    pre-check and the apply, so the two can never disagree about a hunk."""
    try:
        return max(0, int(os.environ.get("OUROBOROS_DIFF_FUZZY_WINDOW", "15") or 15))
    except (TypeError, ValueError):
        return 15


HUNK_RE = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class Hunk:
    #: 0-indexed start the header stated.
    header_start: int
    #: 0-indexed start used for placement: the header's, or the first copied
    #: line number's (the file's own position) when the model copied them.
    start: int
    ops: Ops = field(default_factory=list)
    #: Lines whose copied ``source_lines`` prefix was removed.
    echoed: int = 0


def parse_hunks(diff_text: str) -> List[Hunk]:
    """Every hunk of *diff_text*. ``ops`` = ``(kind, text)`` with kind in
    ``' '``/``'-'``/``'+'`` and text keeping its newline. ``---``/``+++``
    headers are skipped; a bare empty line inside a hunk is a blank context
    line whose leading space the model dropped; any other stray line
    (``\\ No newline at end of file``) is ignored. The ONE parser.

    Line numbers the model copied from a numbered snapshot
    (``source_lines``) are removed here, once, for every consumer -- and
    the first one replaces the header's start, because it is the file's
    own position while the header is the model's count."""
    diff_lines = (diff_text or "").splitlines(keepends=True)
    hunks: List[Hunk] = []
    i = 0
    while i < len(diff_lines) and not diff_lines[i].startswith("@@"):
        i += 1
    while i < len(diff_lines):
        m = HUNK_RE.match(diff_lines[i])
        if m is None:
            i += 1
            continue
        header = int(m.group(1)) - 1
        i += 1
        ops: Ops = []
        while i < len(diff_lines) and not HUNK_RE.match(diff_lines[i]):
            line = diff_lines[i]
            if line[:1] in ("-", "+", " "):
                ops.append((line[0], line[1:]))
            elif line.strip() == "":
                ops.append((" ", line))
            elif source_lines.echoed_number(line) is not None:
                ops.append((" ", line))     # a copied context line whose marker was dropped
            i += 1
        hunks.append(_strip_echoed_numbers(header, ops))
    return hunks


def parse_unified_hunks(diff_text: str) -> List[Tuple[int, Ops]]:
    """``[(orig_start_0idx, ops)]`` per hunk -- :func:`parse_hunks`, shaped
    for the applier."""
    return [(h.start, h.ops) for h in parse_hunks(diff_text)]


def _strip_echoed_numbers(header: int, ops: Ops) -> Hunk:
    texts, _first, n = source_lines.strip_echoed([t for _k, t in ops],
                                                 min_share=source_lines.hunk_share())
    if not n:
        return Hunk(header, header, ops, 0)
    start, side = header, 0
    for kind, text in ops:
        if kind in (" ", "-"):
            pos = source_lines.echoed_number(text)
            if pos is not None:
                start = max(0, pos - 1 - side)
                break
            side += 1
    return Hunk(header, start, [(k, t) for (k, _t), t in zip(ops, texts)], n)


def norm_lines(lines: List[str]) -> List[str]:
    return [ln.rstrip("\n\r") for ln in lines]


def find_hunk_start(result: List[str], orig_start: int, hunk_orig: List[str], window: int) -> int:
    """The first index within ±window of *orig_start* where *hunk_orig* sits
    verbatim (line endings ignored), else whitespace-stripped; -1 if neither.
    Tolerates the off-by-N line numbers models commonly state."""
    norm_hunk = norm_lines(hunk_orig)
    hunk_len = len(hunk_orig)
    lo = max(0, orig_start - window)
    hi = min(len(result) - hunk_len + 1, orig_start + window + 1)
    for candidate in range(lo, hi):
        if norm_lines(result[candidate:candidate + hunk_len]) == norm_hunk:
            return candidate
    ws_hunk = [ln.strip() for ln in norm_hunk]
    for candidate in range(lo, hi):
        if [ln.strip() for ln in norm_lines(result[candidate:candidate + hunk_len])] == ws_hunk:
            return candidate
    return -1


def hunk_anchors(ops: Ops) -> List[Tuple[int, str]]:
    """The hunk's original-side NON-BLANK lines — its locators — as
    ``(op_index, stripped_text)``."""
    return [(i, t.strip()) for i, (k, t) in enumerate(ops) if k in (" ", "-") and t.strip()]


def align_hunk(
    file_lines: List[str], orig_start: int, ops: Ops, window: int,
) -> Optional[Tuple[int, int, List[str]]]:
    """ANCHORED placement — the hunk's context is a locator, not a verbatim
    requirement. Its non-blank original-side lines are matched, in order and
    whitespace-normalised, against the file, skipping only BLANK file lines
    between them; a blank line the model hallucinated (or omitted) is
    tolerated, a non-blank line the file does not have is a fracture.
    Returns ``(start, end, new_span)`` — replace ``file_lines[start:end]``
    with ``new_span``, whose context lines are the FILE's own text (the
    model's rendering of them is never written) — or None when the anchors
    cannot be placed.

    Why: the served 30B emits diffs whose context carries one phantom blank
    line (soak 2026-09-08 00:11Z, hunk 2 of the tracer goal) and the
    equal-length matchers refused an otherwise exact edit twice, tripping
    forward-progress. Nearest placement to the stated position wins."""
    anchors = hunk_anchors(ops)
    if not anchors:
        return None
    n = len(file_lines)
    # ANCHORED placement searches the WHOLE FILE, nearest-to-stated first.
    #
    # The ±window bound was the root cause of a 43% malformed-diff rate
    # measured on the local 30B (soak bt-2026-09-17-205140). Its rejections
    # read "Diff hunk at line 1 does not match source ... file line 26 is
    # ...": the model stated 1, the anchor sat at 25, and the default window
    # of 15 refused to look that far. The hunk was CORRECT and placeable; the
    # search gave up before reaching it.
    #
    # A window is the wrong shape for this tier anyway. The earlier tiers use
    # it legitimately — they compare a hunk's full text slice against a
    # position, so a bound keeps that O(window). This tier asks a different
    # question: WHERE does this anchor sequence occur? That question has an
    # answer in the file or it does not, and a line number the model guessed
    # is a hint for disambiguation, never a constraint on the search.
    #
    # Precision is preserved by the sequence, not the bound: every anchor must
    # match in order, and candidates are still tried nearest-to-stated first,
    # so a file with several matching regions still resolves to the one the
    # model meant. Cost is one scan of a file already in memory.
    _ = window        # retained for signature parity with the bounded tiers
    first_key = anchors[0][1]
    for cand in sorted(range(0, n), key=lambda c: (abs(c - orig_start), c)):
        if file_lines[cand].strip() != first_key:
            continue
        mapping: Dict[int, int] = {}
        fi = cand
        ok = True
        for op_i, key in anchors:
            while fi < n and not file_lines[fi].strip():
                fi += 1
            if fi >= n or file_lines[fi].strip() != key:
                ok = False
                break
            mapping[op_i] = fi
            fi += 1
        if not ok:
            continue
        start = mapping[anchors[0][0]]
        end = mapping[anchors[-1][0]] + 1
        new: List[str] = []
        fpos = start
        for i, (kind, text) in enumerate(ops):
            if kind == "+":
                new.append(text)
                continue
            target = mapping.get(i)
            if target is not None:
                new.extend(file_lines[fpos:target])  # the file's own blank lines in between
                if kind == " ":
                    new.append(file_lines[target])
                fpos = target + 1
            elif fpos < end and not file_lines[fpos].strip():
                # a blank context / removal line: honoured only where the file has one
                if kind == " ":
                    new.append(file_lines[fpos])
                fpos += 1
        new.extend(file_lines[fpos:end])
        return start, end, new
    return None


def diagnose_hunk(file_lines: List[str], orig_start: int, ops: Ops, window: int) -> str:
    """WHY a hunk could not be placed — the first locator the file does not
    have near the stated position, so the model (and the operator) see the
    fracture instead of two lines that matched."""
    anchors = hunk_anchors(ops)
    if not anchors:
        return "the hunk carries no non-blank context or removal lines to anchor on"
    n = len(file_lines)
    lo = max(0, orig_start - window)
    hi = min(n, orig_start + window + 1)
    best: Tuple[int, int, str, str] = (-1, 0, "", "")  # matched, file_line_no, expected, got
    for cand in range(lo, hi):
        if file_lines[cand].strip() != anchors[0][1]:
            continue
        fi = cand
        matched = 0
        for _op_i, key in anchors:
            while fi < n and not file_lines[fi].strip():
                fi += 1
            if fi >= n or file_lines[fi].strip() != key:
                got = file_lines[fi].rstrip("\n\r") if fi < n else "<end of file>"
                if matched > best[0]:
                    best = (matched, fi + 1, key, got)
                break
            matched += 1
            fi += 1
        else:
            return "anchors align but the hunk could not be placed"
    if best[0] < 0:
        # Diagnosed over the span the placement searched (the anchored tier
        # searches the whole file); a ±15 diagnosis named the wrong fracture.
        where = ("anywhere in the file" if window >= n
                 else f"within ±{window} lines of line {orig_start + 1}")
        return f"context line {anchors[0][1]!r} not found {where}"
    return (
        f"context diverges after {best[0]} matching line(s): expected {best[2]!r}, "
        f"file line {best[1]} is {best[3]!r}"
    )


def place(lines: List[str], orig_start: int, ops: Ops, window: int) -> Optional[Tuple[int, int, List[str]]]:
    """The ladder for one hunk: ``(start, end, new_span)`` or None."""
    hunk_orig = [t for k, t in ops if k in (" ", "-")]
    hunk_new = [t for k, t in ops if k in (" ", "+")]
    end = orig_start + len(hunk_orig)
    if norm_lines(lines[orig_start:end]) == norm_lines(hunk_orig):
        return orig_start, end, hunk_new
    found = find_hunk_start(lines, orig_start, hunk_orig, window)
    if found != -1:
        return found, found + len(hunk_orig), hunk_new
    return align_hunk(lines, orig_start, ops, window)


def apply_unified_diff(original: str, diff_text: str) -> str:
    """Apply a unified diff to *original*, returning patched content. Hunks
    are applied in reverse order so earlier-hunk indices remain valid.

    Raises ValueError when a hunk cannot be placed — the message names the
    first locator the file does not have, not two lines that happened to
    match."""
    result: List[str] = list(original.splitlines(keepends=True))
    window = fuzzy_window()
    for orig_start, ops in reversed(parse_unified_hunks(diff_text)):
        placed = place(result, orig_start, ops, window)
        if placed is None:
            raise ValueError(
                f"Diff hunk at line {orig_start + 1} does not match source — "
                f"{diagnose_hunk(result, orig_start, ops, len(result))}"
            )
        start, stop, new_span = placed
        result[start:stop] = new_span
    return "".join(result)


# ---------------------------------------------------------------------------
# What the model was shown, and how its diff measures against it
# ---------------------------------------------------------------------------

@dataclass
class KnownSource:
    """The target file as a prompt carried it: 1-indexed line -> text."""

    lines: Dict[int, str]
    #: The prompt carried the WHOLE file, in order: it can be patched.
    complete: bool
    #: The file itself, when complete.
    text: Optional[str] = None


_SECTION_RE = re.compile(r"^## (?:File|Target): (\S+)(.*)$")
_COUNT_RE = re.compile(r"\[(\d+) bytes, (\d+) lines\]")
_LEGACY_REGION_RE = re.compile(r"^\s*(\d+) \| ?(.*)$")
_TRUNC_RE = re.compile(r"^\[TRUNCATED: \d+ bytes, \d+ lines omitted\]$")
_ELIDED_RE = re.compile(r"^\[\.\.\. \d+ lines (?:above|below) \.\.\.\]$")


def _fenced_after(lines: List[str], i: int, min_lines: int = 0) -> Optional[Tuple[int, int]]:
    """``(first, end)`` of the fenced block whose opening fence is the first
    ``` line after line *i* and before the next target section (the lean
    prompt puts a ``## Structural Index`` between its header and region). With
    *min_lines*, a bare ``` line earlier than that (one INSIDE an unnumbered
    file) does not close it, unless a truncation marker shortened the block."""
    j = i + 1
    while j < len(lines) and not lines[j].startswith("```"):
        if _SECTION_RE.match(lines[j]):
            return None
        j += 1
    if j >= len(lines):
        return None
    k, cut = j + 1, False
    while k < len(lines):
        cut = cut or bool(_TRUNC_RE.match(lines[k]))
        if lines[k].rstrip() == "```" and (cut or k - (j + 1) >= min_lines):
            break
        k += 1
    return j + 1, k


def source_from_prompt(prompt: str, file_path: str = "") -> Optional[KnownSource]:
    """The target file's lines as *prompt* showed them, for *file_path* (or
    the first target section when it names none). Reads the full builder's
    ``## File:`` snapshot (whole, numbered, or truncated) and the lean
    builder's ``### Target Region`` (``source_lines`` or the legacy
    ``NNNN | `` numbering). A truncation boundary's cut lines are dropped:
    they are fragments, not lines. None when the prompt shows no source.
    NEVER raises."""
    try:
        lines = (prompt or "").split("\n")
        want = str(file_path or "").strip()
        for i, ln in enumerate(lines):
            m = _SECTION_RE.match(ln)
            if not m or (want and m.group(1) != want):
                continue
            meta = m.group(2)
            if "[AST-SLICED]" in meta:
                return None              # an outline: lines are not the file's
            declared = _COUNT_RE.search(meta)
            newlines = int(declared.group(2)) if declared else None
            # A whole-file snapshot (## File:) spans the file's own line
            # count; a lean region (## Target:) is a numbered excerpt.
            whole = ln.startswith("## File:")
            block = _fenced_after(lines, i, (newlines or 0) if whole else 0)
            if block is None:
                continue
            body = lines[block[0]:block[1]]
            if whole and body and (source_lines.echoed_number(body[0]) is not None):
                block = _fenced_after(lines, i)      # numbered: no ``` can hide inside
                body = lines[block[0]:block[1]] if block else body
            return _read_block(body, newlines)
        return None
    except Exception:  # noqa: BLE001
        return None


def _read_block(body: List[str], declared_newlines: Optional[int]) -> Optional[KnownSource]:
    known: Dict[int, str] = {}
    numbered = [ln for ln in body if ln.strip()]
    echoed = numbered and all(source_lines.echoed_number(ln) is not None or _TRUNC_RE.match(ln)
                              or _ELIDED_RE.match(ln) for ln in numbered)
    legacy = (not echoed and numbered
              and all(_LEGACY_REGION_RE.match(ln) or _ELIDED_RE.match(ln) for ln in numbered))
    truncated = any(_TRUNC_RE.match(ln) for ln in body)
    if echoed or legacy:
        cut: List[int] = []
        last: Optional[int] = None
        for ln in body:
            if _TRUNC_RE.match(ln):
                if last is not None:
                    cut.append(last)     # the head's last line was cut mid-way
                last = -1                # ...and so is the tail's first
                continue
            if _ELIDED_RE.match(ln):
                continue
            if echoed:
                n = source_lines.echoed_number(ln)
                if n is None:
                    continue
                text = ln[ln.index(source_lines.SEPARATOR) + 1:]
            else:
                lm = _LEGACY_REGION_RE.match(ln)
                if lm is None:
                    continue
                n, text = int(lm.group(1)), lm.group(2)
            if last == -1:
                cut.append(n)
            known[n] = text
            last = n
        for n in cut:
            known.pop(n, None)
        if not known:
            return None
        complete = (not truncated and min(known) == 1
                    and len(known) == max(known)
                    and (declared_newlines is None or max(known) >= declared_newlines))
    else:
        # An unnumbered snapshot (prompts rendered before numbering): lines
        # count from 1 up to a truncation marker, past which positions are
        # unknown. The declared line count separates the file from a ```
        # inside it.
        head: List[str] = []
        for ln in body:
            if _TRUNC_RE.match(ln):
                head = head[:-1]         # cut mid-line
                break
            head.append(ln)
        known = {i + 1: t for i, t in enumerate(head)}
        complete = not truncated
    if not known:
        return None
    text = None
    if complete:
        n = max(known)
        text = "\n".join(known[i] for i in range(1, n + 1))
        if declared_newlines is not None and declared_newlines >= n:
            text += "\n"
    return KnownSource(known, complete, text)


def _segments(known: Dict[int, str]) -> List[Tuple[int, List[str]]]:
    """Contiguous runs of known lines: ``(first_line_1idx, [text...])``."""
    out: List[Tuple[int, List[str]]] = []
    for n in sorted(known):
        if out and out[-1][0] + len(out[-1][1]) == n:
            out[-1][1].append(known[n])
        else:
            out.append((n, [known[n]]))
    return out


def alignment(known: KnownSource, diff_text: str) -> Dict[str, object]:
    """How *diff_text* measures against the source the model was shown.

    ``hunks``: per hunk ``{anchors, present, in_order, placed, checkable,
    header_error, echoed, classes}`` (``classes``: :func:`anchor_classes`)
    -- ``present`` is the share of its context and
    removal lines that exist in the shown source at all, ``in_order`` the
    longest run of them that matches consecutively from where its first
    line occurs; ``header_error`` is |stated start - placed start| in lines.
    A hunk aimed outside a partial snapshot is not ``checkable``.
    ``applies``: every hunk placed (None when any is uncheckable).
    ``patched``: the file after the diff, when the snapshot was complete
    and every hunk placed. NEVER raises."""
    try:
        hunks = parse_hunks(diff_text)
        window = fuzzy_window()
        segs = ([(1, (known.text or "").splitlines(keepends=True))] if known.complete
                else [(s, [t + "\n" for t in ts]) for s, ts in _segments(known.lines)])
        present_set = {t.strip() for t in known.lines.values() if t.strip()}
        rows: List[Dict[str, object]] = []
        for h in hunks:
            anchors = hunk_anchors(h.ops)
            row: Dict[str, object] = {"anchors": len(anchors), "echoed": h.echoed,
                                      "placed": False, "checkable": known.complete,
                                      "header_error": None, "present": 0.0, "in_order": 0.0}
            if anchors:
                row["present"] = sum(1 for _i, a in anchors if a in present_set) / len(anchors)
            for first, seg in segs:
                rel = h.start - (first - 1)
                got = place(seg, max(0, rel), h.ops, window) if anchors else None
                if got is not None:
                    row.update(placed=True, checkable=True,
                               header_error=abs((first - 1 + got[0]) - h.header_start))
                    break
                if 0 <= rel < len(seg) + len(anchors):
                    row["checkable"] = True
            run = (0, 0)
            if anchors and not row["placed"]:
                run = _best_run(segs, anchors)
                row["in_order"] = run[1] / len(anchors)
            elif row["placed"]:
                row["in_order"] = 1.0
            row["classes"] = anchor_classes(h.ops, present_set, run, bool(row["placed"]))
            rows.append(row)
        checkable = [r for r in rows if r["checkable"]]
        applies: Optional[bool] = (None if not rows or len(checkable) < len(rows)
                                   else all(r["placed"] for r in rows))
        patched = None
        if applies and known.complete and known.text is not None:
            try:
                patched = apply_unified_diff(known.text, diff_text)
            except ValueError:
                applies = False
        return {"hunks": rows, "applies": applies, "patched": patched}
    except Exception:  # noqa: BLE001
        return {"hunks": [], "applies": None, "patched": None}


def _longest_in_order(segs: List[Tuple[int, List[str]]], anchors: List[Tuple[int, str]]) -> int:
    """The most anchors matched consecutively (blank file lines skipped) from
    any line where one of them occurs -- how far the hunk got before it
    diverged from the file, wherever it starts."""
    return _best_run(segs, anchors)[1]


def _best_run(segs: List[Tuple[int, List[str]]],
              anchors: List[Tuple[int, str]]) -> Tuple[int, int]:
    """``(first_anchor, length)`` of the longest in-order run (see
    :func:`_longest_in_order`)."""
    best = (0, 0)
    keys = [a for _i, a in anchors]
    for _first, seg in segs:
        stripped = [ln.strip() for ln in seg]
        n = len(stripped)
        for start_key in range(len(keys)):
            for cand in (c for c in range(n) if stripped[c] == keys[start_key]):
                fi, run = cand, 0
                for key in keys[start_key:]:
                    while fi < n and not stripped[fi]:
                        fi += 1
                    if fi >= n or stripped[fi] != key:
                        break
                    run += 1
                    fi += 1
                if run > best[1]:
                    best = (start_key, run)
                if best[1] == len(keys):
                    return best
    return best


def anchor_classes(ops: Ops, present_set: set, run: Tuple[int, int],
                   placed: bool) -> Dict[str, Dict[str, int]]:
    """Every locator of one hunk, by line kind and by what the file says:

    * ``exact`` -- inside the hunk's placement (or its longest in-order run
      when it did not place): the file has this line HERE;
    * ``displaced`` -- the file has the line, but not in this sequence;
    * ``fabricated`` -- the line exists nowhere in the shown source.

    ``context`` and ``removal`` are kept apart because deleting a line the
    file does not have is a stronger claim than citing it as context. This
    is what the GRPO diff reward's penalty matrix weighs."""
    out = {k: {"exact": 0, "displaced": 0, "fabricated": 0} for k in ("context", "removal")}
    lo, length = run
    for pos, (op_i, key) in enumerate(hunk_anchors(ops)):
        kind = "removal" if ops[op_i][0] == "-" else "context"
        if placed or lo <= pos < lo + length:
            cls = "exact"
        elif key in present_set:
            cls = "displaced"
        else:
            cls = "fabricated"
        out[kind][cls] += 1
    return out


__all__ = ["Hunk", "KnownSource", "HUNK_RE", "align_hunk", "alignment", "anchor_classes",
           "apply_unified_diff",
           "diagnose_hunk", "find_hunk_start", "fuzzy_window", "hunk_anchors", "norm_lines",
           "parse_hunks", "parse_unified_hunks", "place", "source_from_prompt"]

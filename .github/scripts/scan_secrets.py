#!/usr/bin/env python3
"""AST + entropy secret scanner — context-aware, not keyword-blind.

Why the previous scanner had to go
----------------------------------
It regexed the raw text of every file for keywords like ``password =`` or
``token =``. Because raw text carries no structure, it produced 8 permanent
false positives on ``main`` and zero true positives:

  * a DOCSTRING in which the MCP scanner documents its own detector patterns;
  * a TEST FIXTURE proving SemanticGuardian catches credential shapes;
  * a LOG LINE containing the literal placeholder ``'YOUR_PASSWORD'``;
  * ENUM members (``FALLBACK_PASSWORD = "fallback_password"``);
  * ENV-VAR NAME constants (``_ENV_HMAC_SECRET = "JARVIS_..._HMAC_SECRET"``);
  * and — worst — code that correctly READS a key from ``.env``.

A permanently-red check is worse than no check: it trains everyone to ignore
it, so a genuine leak lands camouflaged among the noise.

How this one decides
--------------------
Two independent filters, both of which a finding must pass:

1. **Structure (AST).** The file is parsed, not grepped. Only the VALUE side of
   an assignment or a keyword argument is considered. Docstrings, comments and
   bare expression strings are unreachable by construction — no exclusion list
   required, which is what makes this robust rather than brittle.

2. **Content (Shannon entropy + shape).** A real credential is high-entropy;
   English identifiers are not. ``"fallback_password"`` scores ~3.4 bits/char,
   a genuine key ~4.5-5.5. Entropy alone would still miss structured secrets
   whose alphabet is small, so known high-confidence SHAPES (AWS ``AKIA…``, PEM
   blocks, ``gh[pousr]_…``, Google ``AIza…``, Slack ``xox…``, JWTs, OpenAI
   ``sk-…``) bypass the entropy gate and are always reported.

Deliberately NOT suppressed
---------------------------
Test files are still scanned. The previous scanner skipped anything with
"test" in the path, which is precisely where a leaked fixture credential tends
to live. Instead, a file may opt out per-line with ``# pragma: allowlist
secret`` — an explicit, greppable, reviewable marker rather than a silent
whole-directory hole.
"""

from __future__ import annotations

import argparse
import ast
import io
import json
import math
import os
import re
import shlex
import subprocess
import sys
import tokenize
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# --------------------------------------------------------------------------
# Tunables (env-overridable — no hardcoded policy)
# --------------------------------------------------------------------------

DEFAULT_ENTROPY = 3.6   # NOT 4.0: log2(16)=4.0 is hex's mathematical
                        # ceiling, so a 4.0 gate can never flag a hex key.
DEFAULT_MIN_LEN = 20
ALLOWLIST_PRAGMA = "pragma: allowlist secret"

#: Identifier substrings that make a value credential-SHAPED if it is also
#: high-entropy. Used to rank, never to decide alone.
_SUSPECT_NAME = re.compile(
    # Boundaries matter: a bare `auth` matched __author__, Authorization and
    # COMMIT_AUTHORITY_SCHEMA_VERSION; a bare `token` matched _PY3_TOKEN. The
    # keyword must be a WORD in the identifier, not any substring of one.
    r"(?:^|[^a-z])("
    r"api[_-]?keys?|secrets?|passwd|passwords?|tokens?|credentials?|"
    r"private[_-]?keys?|auth[_-]?(?:key|token|secret)|bearer|"
    r"session[_-]?keys?|access[_-]?keys?"
    r")(?:$|[^a-z])",
    re.IGNORECASE,
)

#: Names that describe WHERE a secret lives rather than BEING one. An env-var
#: identifier constant is a pointer, not a credential.
_POINTER_NAME = re.compile(r"^_?(ENV|VAR|KEY_NAME|FIELD|HEADER|PARAM)_", re.IGNORECASE)

#: High-confidence structural signatures. These bypass the entropy gate.
_SHAPES: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("AWS Access Key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("AWS Session Key", re.compile(r"\bASIA[0-9A-Z]{16}\b")),
    ("Google API Key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}")),
    ("GitHub Token", re.compile(r"\bgh[pousr]_[0-9A-Za-z]{36,}\b")),
    ("Slack Token", re.compile(r"\bxox[abprs]-[0-9A-Za-z\-]{10,}\b")),
    ("OpenAI Key", re.compile(r"\bsk-[A-Za-z0-9]{32,}\b")),
    ("Anthropic Key", re.compile(r"\bsk-ant-[A-Za-z0-9\-_]{32,}\b")),
    ("PEM Private Key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT", re.compile(r"\bey[A-Za-z0-9_\-]{10,}\.ey[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\b")),
    ("Stripe Key", re.compile(r"\b[sr]k_live_[0-9A-Za-z]{20,}\b")),
)

#: In PROSE (docstrings, comments) a shape must be a complete credential, not
#: its marker: documentation legitimately names ``-----BEGIN PRIVATE KEY-----``
#: when describing what a detector looks for. A leaked key has a body.
_PROSE_SHAPE_OVERRIDES = {
    "PEM Private Key": re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----\s*[A-Za-z0-9+/=]{40,}",
    ),
}

#: The regex each label is held to in prose.
_SHAPES_BY_LABEL = {
    label: _PROSE_SHAPE_OVERRIDES.get(label, rx) for label, rx in _SHAPES
}

# --------------------------------------------------------------------------
# Prose (docstring + comment) randomness -- calibrated, see _prose_suspect
# --------------------------------------------------------------------------

DEFAULT_PROSE_COVERAGE = 0.5
DEFAULT_PROSE_RANDOMNESS = 0.8
#: A candidate run: the base64/base64url alphabet plus '=' padding and the
#: separators that assignments and ids use.
_PROSE_TOKEN = re.compile(r"[A-Za-z0-9+/=_\-.:]+")
_PROSE_SEP = re.compile(r"[-_=.:]+")
_HEX_RUN = re.compile(r"[0-9a-fA-F]+")
#: Word-like runs: Capitalised or lowercase words, SCREAMING words, years/ids.
#: In a uniform base62 string these are rare (runs of >=3 same-case letters);
#: in an identifier, path or model name they are most of the text.
_WORD_RUN = re.compile(r"[A-Z]?[a-z]{3,}|[A-Z]{3,}(?![a-z])|[0-9]{4,}")
#: '-' and '_' are 2 of base64url's 64 symbols: the separator density a
#: genuinely random url-safe token carries.
_URL_SEPARATOR_DENSITY = 2 / 64

#: Values that are self-evidently not secrets regardless of entropy.
_PLACEHOLDER = re.compile(
    r"^(your[_\-]?|my[_\-]?|example|sample|dummy|fake|test|placeholder|"
    r"changeme|xxx+|\.\.\.|<.*>|\{\{.*\}\}|\$\{.*\}|%s|%\(.*\)s)",
    re.IGNORECASE,
)

_SKIP_DIR_PARTS = (
    "node_modules", "site-packages", ".venv", "venv", "__pycache__",
    ".git", "build", "dist", ".mypy_cache", ".pytest_cache", "vendor",
    # Runtime state and vendored copies — not this repository's source.
    # `.worktrees` is the daemon's own checkout: scanning it double-reports
    # every finding under a second path. `.jarvis` holds caches including a
    # full clone of third-party repos (django) whose fixtures are not ours.
    ".worktrees", ".jarvis", "repo_cache", ".ouroboros",
)


def shannon_entropy(value: str) -> float:
    """Bits per character. English identifiers land ~3.0-3.5; random
    credentials ~4.5-5.5. Empty string is 0.0."""
    if not value:
        return 0.0
    length = len(value)
    counts: dict = {}
    for ch in value:
        counts[ch] = counts.get(ch, 0) + 1
    entropy = 0.0
    for n in counts.values():
        p = n / length
        entropy -= p * math.log2(p)
    return entropy


def matched_shape(value: str) -> Optional[str]:
    """Name of the structural signature this value matches, if any."""
    for label, rx in _SHAPES:
        if rx.search(value):
            return label
    return None


def matched_prose_shape(value: str) -> Optional[str]:
    """:func:`matched_shape` for prose: every shape, with markers that are
    only documentation (a PEM header without a body) held to a complete key."""
    for label, rx in _SHAPES:
        if _PROSE_SHAPE_OVERRIDES.get(label, rx).search(value):
            return label
    return None


def _word_coverage(token: str) -> float:
    return sum(len(m.group(0)) for m in _WORD_RUN.finditer(token)) / max(len(token), 1)


def _randomness(token: str) -> float:
    """Entropy as a fraction of the most this token COULD carry: log2 of the
    smaller of its length and its alphabet (16 for hex, 64 otherwise). A fixed
    bits-per-char gate cannot work on prose -- a 20-char token can never exceed
    log2(20) -- so the gate adapts to the token's own ceiling."""
    alphabet = 16 if _HEX_RUN.fullmatch(token) else 64
    return shannon_entropy(token) / math.log2(max(2, min(len(token), alphabet)))


def _random_run(
    token: str, *, min_length: int, coverage: float, randomness: float,
) -> bool:
    return (
        len(token) >= min_length
        and not _HEX_RUN.fullmatch(token)
        and any(c.isdigit() for c in token)
        and any(c.isalpha() for c in token)
        and _word_coverage(token) < coverage
        and _randomness(token) >= randomness
    )


def _prose_suspect(
    token: str, *, min_length: int, coverage: float, randomness: float,
) -> bool:
    """Is this prose token a random credential rather than language?

    Calibrated on this repository: 2,762 candidate tokens across every
    docstring and comment, against uniform random keys. Character entropy
    alone could not separate them -- ``M10AdaptiveThreshold`` scores as random
    as a real key -- but WORD COVERAGE does: random base62 has a median of 0.25,
    identifiers, paths and model names sit far above 0.5.

    Two readings, either of which flags:

    * the longest separator-free run: splits op ids and UUIDs
      (``op-019fa4d2-2468-…``) into short hex pieces that never qualify;
    * the whole token, only if MIXED-case (hex ids are single-case) and its
      separator density is what a random base64url token would carry --
      model names like ``Qwen3-VL-30B-A3B`` are cut into short pieces far more
      densely, a url-safe key is not.

    Pure hex is never flagged here: in prose it is overwhelmingly a digest or a
    commit id, indistinguishable by content from a hex key. Shapes still apply.
    """
    knobs = dict(min_length=min_length, coverage=coverage, randomness=randomness)
    if _random_run(max(_PROSE_SEP.split(token), key=len), **knobs):
        return True
    whole = token.strip("-_=.:")
    if not (any(c.isupper() for c in whole) and any(c.islower() for c in whole)):
        return False
    density = len(_PROSE_SEP.findall(whole)) / max(len(whole), 1)
    return density <= 3 * _URL_SEPARATOR_DENSITY and _random_run(whole, **knobs)


def _is_pointer_name(name: str) -> bool:
    """True for identifiers that name WHERE a secret lives (env-var names)."""
    return bool(_POINTER_NAME.match(name or ""))


def _looks_like_env_var_name(value: str) -> bool:
    """``"JARVIS_ROADMAP_READER_HMAC_SECRET"`` is an identifier, not a secret:
    SCREAMING_SNAKE with no lowercase and no punctuation beyond underscores."""
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]{2,}", value or ""))


class _Finding:
    __slots__ = ("path", "line", "name", "kind", "entropy", "preview")

    def __init__(self, path, line, name, kind, entropy, preview):
        self.path, self.line, self.name = path, line, name
        self.kind, self.entropy, self.preview = kind, entropy, preview

    def to_dict(self) -> dict:
        return {
            "file": self.path, "line": self.line, "name": self.name,
            "kind": self.kind, "entropy": round(self.entropy, 2),
            "preview": self.preview,
        }


class _Visitor(ast.NodeVisitor):
    """Collects ``(identifier, string_value, lineno)`` for VALUE positions only.

    Docstrings and comments are structurally unreachable here: a docstring is a
    bare ``Expr`` statement (never visited) and comments never enter the AST at
    all. That is the whole point of parsing instead of grepping — the exclusion
    is a property of the representation, not a list someone must maintain."""

    def __init__(self) -> None:
        self.pairs: List[Tuple[str, str, int]] = []
        #: EVERY string literal outside a docstring, as ``(value, first, last)``
        #: line span. Checked against the structural SHAPES only: an
        #: ``AKIA…`` key in a parametrize list or a positional argument is
        #: as exposed as one in an assignment -- text-level scanners
        #: (GitGuardian, push protection) read the file, not the AST.
        self.literals: List[Tuple[str, int, int]] = []
        #: Docstrings and bare string statements, as ``(text, first, last)``.
        self.docstrings: List[Tuple[str, int, int]] = []

    def visit_Expr(self, node: ast.Expr) -> None:
        # A bare string statement is a docstring or inert prose. It is not a
        # VALUE, so the value heuristics never judge it -- but a key pasted
        # into a docstring "as an example" is published all the same, so it is
        # collected for the prose pass. Any other expression is walked normally.
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            first = getattr(node, "lineno", 0)
            self.docstrings.append(
                (node.value.value, first, getattr(node, "end_lineno", None) or first),
            )
            return
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str):
            first = getattr(node, "lineno", 0)
            self.literals.append(
                (node.value, first, getattr(node, "end_lineno", None) or first),
            )

    def _record(self, name: str, node: ast.AST) -> None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            self.pairs.append((name, node.value, getattr(node, "lineno", 0)))
        elif isinstance(node, ast.JoinedStr):
            # f-strings: only the literal segments can carry a baked-in secret.
            for part in node.values:
                if isinstance(part, ast.Constant) and isinstance(part.value, str):
                    self.pairs.append((name, part.value, getattr(node, "lineno", 0)))

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            if isinstance(target, ast.Name):
                self._record(target.id, node.value)
            elif isinstance(target, ast.Attribute):
                self._record(target.attr, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name) and node.value is not None:
            self._record(node.target.id, node.value)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for kw in node.keywords or ():
            if kw.arg:
                self._record(kw.arg, kw.value)
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                self._record(k.value, v)
        self.generic_visit(node)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def scan_source(
    source: str, path: str = "<memory>", *,
    entropy_threshold: float = DEFAULT_ENTROPY,
    min_length: int = DEFAULT_MIN_LEN,
    prose_coverage: Optional[float] = None,
    prose_randomness: Optional[float] = None,
) -> List[_Finding]:
    """Findings for one Python source string. Unparseable input yields [] —
    a syntax error is the linter's problem, not the scanner's.

    The prose thresholds default to ``SECRET_SCAN_PROSE_COVERAGE`` /
    ``SECRET_SCAN_PROSE_RANDOMNESS``, so every entry point (tree, revisions,
    the pre-push gate) honours the same tuning."""
    if prose_coverage is None:
        prose_coverage = _env_float("SECRET_SCAN_PROSE_COVERAGE", DEFAULT_PROSE_COVERAGE)
    if prose_randomness is None:
        prose_randomness = _env_float("SECRET_SCAN_PROSE_RANDOMNESS", DEFAULT_PROSE_RANDOMNESS)
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []

    lines = source.splitlines()
    visitor = _Visitor()
    visitor.visit(tree)

    findings: List[_Finding] = []
    for name, value, lineno in visitor.pairs:
        if not value:
            continue
        # Explicit, greppable, reviewable opt-out.
        raw_line = lines[lineno - 1] if 0 < lineno <= len(lines) else ""
        if ALLOWLIST_PRAGMA in raw_line:
            continue

        shape = matched_shape(value)
        if shape is not None:
            # Structural signatures are decisive: report regardless of entropy,
            # name, or placeholder heuristics. A real AKIA key in a variable
            # called `example` is still a real AKIA key.
            findings.append(_Finding(
                path, lineno, name, shape, shannon_entropy(value),
                value[:12] + "…",
            ))
            continue

        if len(value) < min_length:
            continue
        if _PLACEHOLDER.match(value):
            continue
        # A credential never contains whitespace. Everything that survived the
        # name gate but was actually prose, a shell invocation, an AppleScript
        # block or a dotted module path had spaces or newlines in it. One
        # principled property, not five path exclusions.
        if any(c.isspace() for c in value):
            continue
        if _looks_like_env_var_name(value) or _is_pointer_name(name):
            continue
        if not _SUSPECT_NAME.search(name or ""):
            continue

        ent = shannon_entropy(value)
        # Lowering the gate to catch hex would re-admit prose, so entropy alone
        # is not enough. Real credentials mix DIGITS WITH LETTERS; English
        # identifiers ("change_me_before_deploying") essentially never do. That
        # single structural property separates them without a keyword list.
        mixed = any(c.isdigit() for c in value) and any(c.isalpha() for c in value)
        if ent >= entropy_threshold and mixed:
            findings.append(_Finding(
                path, lineno, name, "High-entropy literal", ent,
                value[:8] + "…",
            ))

    # Shapes in every other literal. Only the decisive structural signatures:
    # the entropy/name heuristics need an identifier to rank against, and a
    # positional literal has none.
    # Keyed on the VALUE: the pass above already reported or deliberately
    # allowlisted it, and an f-string part's line number is the enclosing
    # expression's on 3.11, so a (value, line) key would double-report.
    judged = {value for _name, value, _lineno in visitor.pairs}
    for value, first, last in visitor.literals:
        if value in judged:
            continue
        span = lines[max(first - 1, 0):last]
        if any(ALLOWLIST_PRAGMA in line for line in span):
            continue
        shape = matched_shape(value)
        if shape is not None:
            findings.append(_Finding(
                path, first, "<literal>", shape, shannon_entropy(value),
                value[:12] + "…",
            ))

    # Prose: docstrings and comments. Nothing here is a VALUE, but all of it
    # is published -- and a model asked for an example will paste a key.
    prose = [("<docstring>", text, first, last) for text, first, last in visitor.docstrings]
    prose += [("<comment>", text, line, line) for text, line in _comments(source)]
    for channel, text, first, _last in prose:
        # The pragma covers the LINE it is on, not the whole block: one
        # reviewed id in a long module docstring must not blind the scanner to
        # the rest of it.
        findings.extend(
            f for f in _prose_findings(
                path, channel, text, first, min_length=min_length,
                coverage=prose_coverage, randomness=prose_randomness,
            )
            if not (0 < f.line <= len(lines) and ALLOWLIST_PRAGMA in lines[f.line - 1])
        )
    return findings


def _comments(source: str) -> List[Tuple[str, int]]:
    """``(text, line)`` of every comment. A file the tokenizer rejects yields
    what it produced before failing -- never raises."""
    out: List[Tuple[str, int]] = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type == tokenize.COMMENT:
                out.append((tok.string, tok.start[0]))
    except (tokenize.TokenError, SyntaxError, IndentationError):
        pass
    return out


def _prose_findings(
    path: str, channel: str, text: str, first: int, *, min_length: int,
    coverage: float, randomness: float,
) -> List[_Finding]:
    """Findings in one docstring or comment: complete credential shapes, then
    random runs no language produces (:func:`_prose_suspect`)."""
    found: List[_Finding] = []
    claimed: List[Tuple[int, int]] = []
    for label, rx in _SHAPES_BY_LABEL.items():
        for m in rx.finditer(text):
            claimed.append(m.span())
            found.append(_Finding(
                path, first + text[: m.start()].count("\n"), channel, label,
                shannon_entropy(m.group(0)), m.group(0)[:12] + "…",
            ))
    for m in _PROSE_TOKEN.finditer(text):
        token = m.group(0)
        if any(s < m.end() and m.start() < e for s, e in claimed):
            continue  # already reported, by the decisive shape
        if _prose_suspect(
            token, min_length=min_length, coverage=coverage, randomness=randomness,
        ):
            found.append(_Finding(
                path, first + text[: m.start()].count("\n"), channel,
                "High-entropy token in prose", shannon_entropy(token),
                token[:8] + "…",
            ))
    return found


def iter_python_files(root: Path) -> Iterable[Path]:
    for p in root.rglob("*.py"):
        if any(part in _SKIP_DIR_PARTS for part in p.parts):
            continue
        yield p


def scan_tree(
    root: Path, *, entropy_threshold: float = DEFAULT_ENTROPY,
    min_length: int = DEFAULT_MIN_LEN,
) -> List[_Finding]:
    out: List[_Finding] = []
    for path in iter_python_files(root):
        try:
            src = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.extend(scan_source(
            src, str(path), entropy_threshold=entropy_threshold,
            min_length=min_length,
        ))
    return out


def _git(repo: Path, *args: str, stdin: Optional[bytes] = None) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], input=stdin,
        capture_output=True, check=True,
    ).stdout


def _read_blobs(repo: Path, blobs: Sequence[str]) -> Dict[str, bytes]:
    """Contents of *blobs*, in ONE ``git cat-file --batch`` process."""
    if not blobs:
        return {}
    out = _git(repo, "cat-file", "--batch", stdin="".join(b + "\n" for b in blobs).encode())
    contents: Dict[str, bytes] = {}
    pos = 0
    while pos < len(out):
        header_end = out.index(b"\n", pos)
        fields = out[pos:header_end].split()
        pos = header_end + 1
        if len(fields) < 3 or fields[1] == b"missing":
            continue
        size = int(fields[2])
        contents[fields[0].decode()] = out[pos:pos + size]
        pos += size + 1  # trailing newline after every object
    return contents


def scan_revisions(
    repo: Path, revs: Sequence[str], *, entropy_threshold: float = DEFAULT_ENTROPY,
    min_length: int = DEFAULT_MIN_LEN,
) -> List[_Finding]:
    """Findings in every Python file version introduced by the commits *revs*
    selects (``git rev-list`` syntax: ``A..B``, ``^X``, ``--not --remotes``).

    Per COMMIT, not per tip: a text scanner on the receiving end (GitGuardian,
    push protection) reads every pushed commit, so a credential added in one
    commit and deleted in the next has still been published. Each distinct
    blob is scanned once -- content-addressed, so a file carried unchanged
    through a long range costs one scan.
    """
    commits = _git(repo, "rev-list", *revs).decode().split()
    introduced: Dict[str, Tuple[str, str]] = {}  # blob -> (path, commit)
    for commit in commits:
        # `-m`: a merge's conflict resolution can introduce content no parent
        # had, and diff-tree shows merges as empty without it.
        tokens = iter(_git(
            repo, "diff-tree", "--no-commit-id", "-r", "--root", "-m", "-z",
            "--diff-filter=ACMR", commit,
        ).decode("utf-8", "replace").split("\0"))
        for header in tokens:
            if not header.startswith(":"):
                continue
            fields = header.split()        # :<mode> <mode> <sha> <sha> <status>
            path = next(tokens, "")
            if fields[-1][:1] in ("R", "C"):
                path = next(tokens, "")    # renames/copies carry src THEN dst
            if len(fields) < 5 or not path.endswith(".py"):
                continue
            if any(part in _SKIP_DIR_PARTS for part in Path(path).parts):
                continue
            introduced.setdefault(fields[3], (path, commit))
    contents = _read_blobs(repo, list(introduced))
    out: List[_Finding] = []
    for blob, (path, commit) in introduced.items():
        src = contents.get(blob)
        if src is None:
            continue
        out.extend(scan_source(
            src.decode("utf-8", "replace"), f"{path}@{commit[:10]}",
            entropy_threshold=entropy_threshold, min_length=min_length,
        ))
    return out


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".")
    ap.add_argument(
        "--revs", default=None,
        help="scan the file versions introduced by these commits instead of "
             "the working tree (git rev-list syntax, e.g. 'origin/main..HEAD')",
    )
    ap.add_argument("--json", action="store_true")
    ap.add_argument(
        "--entropy",
        type=float,
        default=float(os.environ.get("SECRET_SCAN_ENTROPY", DEFAULT_ENTROPY)),
    )
    ap.add_argument(
        "--min-length",
        type=int,
        default=int(os.environ.get("SECRET_SCAN_MIN_LENGTH", DEFAULT_MIN_LEN)),
    )
    args = ap.parse_args(argv)

    if args.revs:
        findings = scan_revisions(
            Path(args.root), shlex.split(args.revs),
            entropy_threshold=args.entropy, min_length=args.min_length,
        )
    else:
        findings = scan_tree(
            Path(args.root), entropy_threshold=args.entropy,
            min_length=args.min_length,
        )

    if args.json:
        print(json.dumps([f.to_dict() for f in findings], indent=2))
    elif findings:
        print("\n⚠️  POTENTIAL SENSITIVE DATA EXPOSURE DETECTED!\n")
        for f in findings:
            print(
                f"   ❌ {f.kind} · {f.path}:{f.line} "
                f"({f.name}, entropy={f.entropy:.2f})"
            )
        print(f"\n❌ Found {len(findings)} potential issue(s)")
        print("   Move these to environment variables, or mark a reviewed")
        print(f"   false positive with '# {ALLOWLIST_PRAGMA}'.")
    else:
        print("✅ No hardcoded secrets detected (AST + entropy scan).")
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())

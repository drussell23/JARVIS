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
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

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


def scan_source(
    source: str, path: str = "<memory>", *,
    entropy_threshold: float = DEFAULT_ENTROPY,
    min_length: int = DEFAULT_MIN_LEN,
) -> List[_Finding]:
    """Findings for one Python source string. Unparseable input yields [] —
    a syntax error is the linter's problem, not the scanner's."""
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
    return findings


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


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default=".")
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

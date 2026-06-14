#!/usr/bin/env python3
"""Distillation Surgeon — AST/tokenize structural edits for unified_supervisor.py.

One-off tool for the Sovereign Distillation (Phase A+B). No hardcoded line
numbers: every span is derived from the AST at run time, so the tool is immune
to line drift between edits. Removed at the end of the campaign (Task 7).

Subcommands:
  list-dupes          --file F
  delete-class        --file F --name N [--occurrence first|last|only] [--expect-total K]
  rename-class        --file F --old O --new W      (requires exactly 2 top-level defs of O)
  remove-registration --file F --name N             (removes _r(ServiceDescriptor(name="N", ...)))
"""
from __future__ import annotations

import argparse
import ast
import io
import sys
import tokenize


def _read(path: str) -> str:
    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


def _write(path: str, text: str) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def _top_classes(tree: ast.Module, name: str):
    return [n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == name]


def _node_start(node: ast.ClassDef) -> int:
    if node.decorator_list:
        return min(d.lineno for d in node.decorator_list)
    return node.lineno


def _delete_span(src: str, start: int, end: int | None) -> str:
    """Delete 1-based inclusive [start, end] plus trailing blank lines."""
    if end is None:  # ast nodes always set end_lineno on 3.8+; guard for the type-checker
        raise ValueError("node has no end_lineno")
    lines = src.splitlines(keepends=True)
    while end < len(lines) and lines[end].strip() == "":
        end += 1
    del lines[start - 1:end]
    out = "".join(lines)
    ast.parse(out)  # validity guard — raises if we corrupted the file
    return out


def cmd_list_dupes(args) -> int:
    tree = ast.parse(_read(args.file))
    names = [n.name for n in tree.body if isinstance(n, ast.ClassDef)]
    seen, dupes = set(), []
    for n in names:
        if n in seen and n not in dupes:
            dupes.append(n)
        seen.add(n)
    for d in dupes:
        print(d)
    print(f"# {len(dupes)} duplicate top-level class name(s)", file=sys.stderr)
    return 0


def cmd_delete_class(args) -> int:
    src = _read(args.file)
    tree = ast.parse(src)
    matches = sorted(_top_classes(tree, args.name), key=lambda n: n.lineno)
    if args.expect_total is not None and len(matches) != args.expect_total:
        print(f"ERROR: expected {args.expect_total} top-level class(es) named "
              f"{args.name}, found {len(matches)}", file=sys.stderr)
        return 2
    if not matches:
        print(f"ERROR: no top-level class named {args.name}", file=sys.stderr)
        return 2
    if args.occurrence == "first":
        node = matches[0]
    elif args.occurrence == "last":
        node = matches[-1]
    else:  # only
        if len(matches) != 1:
            print(f"ERROR: --occurrence only but {len(matches)} found for "
                  f"{args.name}", file=sys.stderr)
            return 2
        node = matches[0]
    start, end = _node_start(node), node.end_lineno
    _write(args.file, _delete_span(src, start, end))
    print(f"deleted class {args.name} (from line {start})", file=sys.stderr)
    return 0


def cmd_rename_class(args) -> int:
    src = _read(args.file)
    tree = ast.parse(src)
    defs = sorted(_top_classes(tree, args.old), key=lambda n: n.lineno)
    if len(defs) != 2:
        print(f"ERROR: rename-class expects exactly 2 top-level defs of "
              f"{args.old}, found {len(defs)}", file=sys.stderr)
        return 2
    d1_row, d2_row = _node_start(defs[0]), _node_start(defs[1])
    repls: dict[int, list[int]] = {}
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.NAME and tok.string == args.old:
            row, col = tok.start
            if d1_row <= row < d2_row:
                repls.setdefault(row, []).append(col)
    if not repls:
        print(f"ERROR: no NAME tokens '{args.old}' in [{d1_row},{d2_row})",
              file=sys.stderr)
        return 2
    lines = src.splitlines(keepends=True)
    for row, cols in repls.items():
        line = lines[row - 1]
        for col in sorted(cols, reverse=True):
            line = line[:col] + args.new + line[col + len(args.old):]
        lines[row - 1] = line
    out = "".join(lines)
    ast.parse(out)
    _write(args.file, out)
    total = sum(len(v) for v in repls.values())
    print(f"renamed {args.old} -> {args.new}: {total} token(s) in "
          f"[{d1_row},{d2_row})", file=sys.stderr)
    return 0


def cmd_remove_registration(args) -> int:
    src = _read(args.file)
    tree = ast.parse(src)
    target = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Expr):
            continue
        call = node.value
        if not (isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name) and call.func.id == "_r"
                and call.args and isinstance(call.args[0], ast.Call)
                and isinstance(call.args[0].func, ast.Name)
                and call.args[0].func.id == "ServiceDescriptor"):
            continue
        for kw in call.args[0].keywords:
            if (kw.arg == "name" and isinstance(kw.value, ast.Constant)
                    and kw.value.value == args.name):
                target = node
                break
        if target is not None:
            break
    if target is None:
        print(f"ERROR: no _r(ServiceDescriptor(name={args.name!r})) found",
              file=sys.stderr)
        return 2
    _write(args.file, _delete_span(src, target.lineno, target.end_lineno))
    print(f"removed registration name={args.name} (from line {target.lineno})",
          file=sys.stderr)
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Distillation Surgeon")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("list-dupes"); s.add_argument("--file", required=True)
    s.set_defaults(fn=cmd_list_dupes)

    s = sub.add_parser("delete-class")
    s.add_argument("--file", required=True)
    s.add_argument("--name", required=True)
    s.add_argument("--occurrence", choices=["first", "last", "only"], default="only")
    s.add_argument("--expect-total", type=int, default=None)
    s.set_defaults(fn=cmd_delete_class)

    s = sub.add_parser("rename-class")
    s.add_argument("--file", required=True)
    s.add_argument("--old", required=True)
    s.add_argument("--new", required=True)
    s.set_defaults(fn=cmd_rename_class)

    s = sub.add_parser("remove-registration")
    s.add_argument("--file", required=True)
    s.add_argument("--name", required=True)
    s.set_defaults(fn=cmd_remove_registration)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""
Add a PDF outline / bookmark tree to the Chrome-headless-generated benchmark PDF.

Why
---
Chrome's print-to-PDF generates named anchor destinations (pandoc's --toc
produces <a href="#anchor"> links, Chrome preserves them as clickable), but
it does NOT write a PDF /Outlines dictionary. The /Outlines dict is what
Preview's sidebar "Table of Contents" view reads. Without it, the sidebar
shows only page thumbnails.

This script:
  1. Parses the HTML TOC (nav#TOC element) that pandoc --toc generated,
     walking the nested <ul><li> structure.
  2. Maps each TOC entry's href (e.g. "#part-i--executive-summary") to a
     named destination already present in the Chrome-generated PDF.
  3. Looks up the page number for each destination.
  4. Writes a new PDF with an /Outlines tree mirroring the TOC hierarchy.

Result: Preview's sidebar shows a navigable outline. Same in Adobe Acrobat,
any other PDF reader that supports PDF Outlines (which is all of them).

Usage
-----
    python3 scripts/benchmarks/add_pdf_outline.py \\
        docs/benchmarks/DW_BENCHMARKS_2026-04-16.html \\
        docs/benchmarks/DW_BENCHMARKS_2026-04-16.pdf \\
        docs/benchmarks/DW_BENCHMARKS_2026-04-16.pdf

The output path may equal the input path (writes to a temp file, renames).

Dependencies
------------
    pypdf >= 5.0 (for PdfReader/PdfWriter.add_outline_item)
    stdlib html.parser for TOC extraction
"""
from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from pypdf import PdfReader, PdfWriter
from pypdf.generic import Fit


# ---------------------------------------------------------------------------
# HTML TOC parser
# ---------------------------------------------------------------------------

class TocNode:
    __slots__ = ("title", "href", "children")

    def __init__(self, title: str = "", href: Optional[str] = None) -> None:
        self.title = title
        self.href = href
        self.children: List["TocNode"] = []


class TocParser(HTMLParser):
    """
    Walks <nav id="TOC"> and builds a tree of (title, href, children).
    Assumes pandoc's canonical structure:
        <nav id="TOC">
          <ul>
            <li><a href="#x">Title</a>
              <ul>... nested ...</ul>
            </li>
          </ul>
        </nav>
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_toc = False
        self.toc_depth = 0  # <nav id="TOC"> → 0, inside <ul> ... nesting
        self.ul_stack: List[TocNode] = []
        self.root = TocNode(title="__root__")
        self.current_li: Optional[TocNode] = None
        self.capture_text = False
        self.text_buffer: List[str] = []
        self.ul_stack.append(self.root)

    def handle_starttag(self, tag: str, attrs: List[Tuple[str, Optional[str]]]) -> None:
        ad = dict(attrs)
        if tag == "nav" and ad.get("id") == "TOC":
            self.in_toc = True
            return
        if not self.in_toc:
            return
        if tag == "ul":
            # A nested <ul> inside an <li> means this li's descendants go
            # underneath it. Push current_li (if any) as the parent scope
            # for the contained <li> elements.
            if self.current_li is not None:
                self.ul_stack.append(self.current_li)
                self.current_li = None
        elif tag == "li":
            parent = self.ul_stack[-1]
            self.current_li = TocNode()
            parent.children.append(self.current_li)
        elif tag == "a" and self.current_li is not None:
            self.current_li.href = ad.get("href")
            self.capture_text = True
            self.text_buffer = []

    def handle_endtag(self, tag: str) -> None:
        if tag == "nav" and self.in_toc:
            self.in_toc = False
            return
        if not self.in_toc:
            return
        if tag == "a" and self.capture_text:
            raw = "".join(self.text_buffer).strip()
            # Collapse any internal whitespace (newlines, tabs, multiple spaces)
            # into single spaces so bookmark titles are single-line.
            text = re.sub(r"\s+", " ", raw)
            if self.current_li is not None:
                self.current_li.title = text
            self.capture_text = False
        elif tag == "li":
            # Done with this li; outer <ul> may have more sibling <li>s
            self.current_li = None
        elif tag == "ul":
            # End of nested list — pop back to outer scope
            if len(self.ul_stack) > 1:
                self.ul_stack.pop()

    def handle_data(self, data: str) -> None:
        if self.capture_text:
            self.text_buffer.append(data)


def parse_toc(html_path: Path) -> TocNode:
    parser = TocParser()
    parser.feed(html_path.read_text(encoding="utf-8"))
    return parser.root


# ---------------------------------------------------------------------------
# PDF destination extraction
# ---------------------------------------------------------------------------

def build_dest_map(reader: PdfReader) -> Dict[str, int]:
    """
    Map named destination → 0-indexed page number.

    PDFs generated by Chrome put named destinations in /Catalog/Names/Dests
    or /Catalog/Dests. pypdf exposes them via reader.named_destinations.

    Each destination resolves to a page object; we turn that into a page index.
    """
    dest_to_page: Dict[str, int] = {}
    named = reader.named_destinations or {}
    # Build a reverse lookup of page object IDs → index
    page_id_to_idx = {id(p): idx for idx, p in enumerate(reader.pages)}
    for dest_name, dest_obj in named.items():
        page_ref = getattr(dest_obj, "page", None)
        if page_ref is None:
            # Older pypdf versions: dest_obj is a list-like [page_ref, /XYZ, ...]
            try:
                page_ref = dest_obj[0]
            except Exception:
                continue
        # page_ref may be an IndirectObject; resolve
        try:
            page_obj = page_ref.get_object() if hasattr(page_ref, "get_object") else page_ref
        except Exception:
            continue
        idx = page_id_to_idx.get(id(page_obj))
        if idx is None:
            # Fallback: linear scan comparing /Type and /Contents
            for i, p in enumerate(reader.pages):
                if p == page_obj:
                    idx = i
                    break
        if idx is not None:
            # Chrome-generated PDFs prefix named destinations with '/'.
            # Normalize to the raw name so href lookups match.
            key = dest_name.lstrip("/") if isinstance(dest_name, str) else str(dest_name).lstrip("/")
            dest_to_page[key] = idx
    return dest_to_page


# ---------------------------------------------------------------------------
# Outline tree builder
# ---------------------------------------------------------------------------

def href_to_dest_name(href: str) -> str:
    """Strip the leading '#' from href values like '#part-i--executive-summary'."""
    if href.startswith("#"):
        return href[1:]
    return href


def add_outline_recursive(
    writer: PdfWriter,
    node: TocNode,
    dest_map: Dict[str, int],
    parent: Any = None,
    stats: Optional[Dict[str, int]] = None,
) -> None:
    """
    Walk TocNode tree, adding outline items to writer. Skip nodes whose href
    doesn't resolve to a destination (warn via stats counter).
    """
    if stats is None:
        stats = {"added": 0, "skipped_no_href": 0, "skipped_no_dest": 0}

    for child in node.children:
        title = child.title or "(untitled)"
        if child.href is None:
            stats["skipped_no_href"] += 1
            # Still recurse into grandchildren (shouldn't happen for our TOC)
            add_outline_recursive(writer, child, dest_map, parent, stats)
            continue
        dest_name = href_to_dest_name(child.href)
        page_idx = dest_map.get(dest_name)
        if page_idx is None:
            stats["skipped_no_dest"] += 1
            continue
        # Add the outline item; writer.add_outline_item returns the new node
        # for use as parent of further nested items.
        added_item = writer.add_outline_item(
            title=title,
            page_number=page_idx,
            parent=parent,
            fit=Fit.fit(),
        )
        stats["added"] += 1
        if child.children:
            add_outline_recursive(writer, child, dest_map, parent=added_item, stats=stats)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description="Add PDF outline tree from HTML TOC")
    parser.add_argument("html_in", type=Path, help="Source HTML (with pandoc --toc)")
    parser.add_argument("pdf_in", type=Path, help="Chrome-generated PDF")
    parser.add_argument("pdf_out", type=Path, help="Output PDF (may equal pdf_in)")
    args = parser.parse_args()

    if not args.html_in.is_file():
        print(f"ERROR: HTML not found: {args.html_in}", file=sys.stderr)
        return 1
    if not args.pdf_in.is_file():
        print(f"ERROR: PDF not found: {args.pdf_in}", file=sys.stderr)
        return 1

    print(f"[outline] parsing TOC from {args.html_in.name}...")
    root = parse_toc(args.html_in)
    if not root.children:
        print("ERROR: no TOC entries found — was pandoc run with --toc?", file=sys.stderr)
        return 2
    # Count nodes recursively for visibility
    def count(node: TocNode) -> int:
        return 1 + sum(count(c) for c in node.children)
    total_toc_entries = count(root) - 1  # exclude __root__
    print(f"[outline] TOC has {total_toc_entries} entries (including nested)")

    print(f"[outline] reading PDF {args.pdf_in.name}...")
    reader = PdfReader(str(args.pdf_in))
    n_pages = len(reader.pages)
    print(f"[outline]   {n_pages} pages")

    dest_map = build_dest_map(reader)
    print(f"[outline]   {len(dest_map)} named destinations in PDF")

    # clone_document_from_reader preserves the full catalog: /Dests (named
    # destinations for our TOC links), metadata, page structure. Versus
    # append_pages_from_reader which only copies pages and drops /Dests.
    writer = PdfWriter(clone_from=reader)

    stats = {"added": 0, "skipped_no_href": 0, "skipped_no_dest": 0}
    add_outline_recursive(writer, root, dest_map, parent=None, stats=stats)

    print(f"[outline] added {stats['added']} bookmark nodes to outline")
    if stats["skipped_no_dest"]:
        print(f"[outline]   (skipped {stats['skipped_no_dest']} TOC entries with no matching PDF destination)")

    # Write to temp file, then swap — allows pdf_out == pdf_in
    tmp_path = args.pdf_out.with_suffix(".pdf.tmp")
    with open(tmp_path, "wb") as fh:
        writer.write(fh)
    tmp_path.replace(args.pdf_out)

    # Size report
    out_size = args.pdf_out.stat().st_size
    print(f"[outline] wrote {args.pdf_out.name} ({out_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

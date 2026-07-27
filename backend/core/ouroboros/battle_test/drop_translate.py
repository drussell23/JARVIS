"""A dropped file becomes something the organism understands.

Drag a file onto a terminal and it injects an absolute path — often shell
quoted, sometimes a `file://` URI, sometimes with escaped spaces. The operator
sees that string appear in their prompt and has to know, unprompted, that O+V
wants `@relative/path` for repo files and `/attach /abs/path` for everything
else.

So the drop is translated at the moment it lands:

    /Users/dj/repos/JARVIS/backend/auth.py   →  @backend/auth.py
    /Users/dj/Desktop/screenshot.png         →  /attach /Users/dj/Desktop/screenshot.png

Why translate rather than transport
------------------------------------
The alternative is carrying the bytes: read the file client-side, base64 it,
push it over the UDS bridge. That bridge is a single ordered stream shared with
op chrome, the token mirror and the heartbeat — a megabyte of base64 would
block every one of them behind it. The daemon already reads files by path, and
it is the process that has the repo, so a path is both smaller and more
truthful than a copy.

Repo-inside vs outside
----------------------
`@mentions` are repo-relative by construction — the completer that offers them
is rooted at the repo and cannot climb out. A dropped file from the Desktop
has no repo-relative spelling, so forcing one would produce `@../../Desktop/x`,
which the daemon would refuse and the operator would not understand. Those get
`/attach` with the absolute path, which is exactly the verb that already
handles them.

Nothing is guessed
------------------
A path that does not exist is left alone. A pasted URL, a code snippet, a
sentence — left alone. Rewriting text the operator did not mean as a file is
worse than leaving them to type the verb themselves, because it happens
silently and they have to notice to undo it.
"""
from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional, Tuple
from urllib.parse import unquote, urlparse

logger = logging.getLogger("Ouroboros.DropTranslate")

__all__ = ["drop_translation_enabled", "normalise_dropped_path",
           "translate_drop"]

#: Extensions worth treating as an attachment when dropped. Deliberately NOT
#: "any file": a dropped `.py` inside the repo is a mention, and outside it is
#: usually a mistake, whereas an image or a document is unambiguous about
#: intent. Env-extendable because the set is a property of what the organism
#: can ingest, which is allowed to grow.
_MEDIA_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".heic",
    ".pdf", ".md", ".txt", ".csv", ".json", ".yaml", ".yml", ".log",
}

#: A terminal may shell-quote a dropped path, wrap it in quotes, or escape
#: spaces. All three are the same path.
_ESCAPED_SPACE = re.compile(r"\\(\s)")


def drop_translation_enabled() -> bool:
    """Default ON. A dropped file that does nothing is the worse default."""
    return os.environ.get(
        "JARVIS_DROP_TRANSLATE_ENABLED", "1",
    ).strip().lower() not in ("0", "false", "no", "off")


def _media_extensions() -> set:
    try:
        extra = os.environ.get("JARVIS_DROP_EXTRA_EXTENSIONS", "") or ""
        return _MEDIA_EXT | {
            e.strip().lower() if e.strip().startswith(".") else f".{e.strip().lower()}"
            for e in extra.split(",") if e.strip()
        }
    except Exception:  # noqa: BLE001
        return _MEDIA_EXT


def normalise_dropped_path(text: Any) -> str:
    """The filesystem path a terminal meant by this pasted text, or "".

    Handles the three spellings a drop actually produces — a bare absolute
    path, one wrapped in quotes, and a `file://` URI — plus backslash-escaped
    spaces, which macOS Terminal emits and which turn a real path into one
    that does not resolve.
    """
    try:
        raw = str(text or "").strip()
        if not raw or "\n" in raw:
            # Multi-line paste is prose or code, not a drop. A drop is one
            # path; treating a pasted diff as a filename would be absurd and
            # is exactly the kind of silent rewrite this must not do.
            return ""
        if raw.startswith("file://"):
            parsed = urlparse(raw)
            raw = unquote(parsed.path or "")
        # Strip one layer of matching quotes.
        for quote in ("'", '"'):
            if len(raw) >= 2 and raw.startswith(quote) and raw.endswith(quote):
                raw = raw[1:-1]
                break
        raw = _ESCAPED_SPACE.sub(r"\1", raw)
        return raw.strip()
    except Exception:  # noqa: BLE001
        return ""


def _repo_root() -> Path:
    return Path(os.environ.get("JARVIS_REPO_PATH", ".")).resolve()


def _inside_repo(path: Path, root: Path) -> Optional[str]:
    """Repo-relative spelling, or None when the file is outside.

    Uses the SAME rooting the `@` completer uses, so a path the completer
    would offer and a path a drop translates to can never disagree about
    what counts as "in the repo".
    """
    try:
        return str(path.resolve().relative_to(root))
    except Exception:  # noqa: BLE001
        return None


def translate_drop(text: Any, root: Optional[Path] = None) -> Tuple[str, str]:
    """Translate a dropped path. Returns ``(translated, kind)``.

    ``kind`` is ``"mention"``, ``"attach"``, or ``""`` when the text was left
    alone — which is the common case and must stay cheap.

    Returns the ORIGINAL text unchanged whenever it is not clearly a dropped
    file. Rewriting something the operator did not mean is worse than making
    them type the verb, because it happens silently.
    """
    original = str(text or "")
    try:
        if not drop_translation_enabled():
            return original, ""
        candidate = normalise_dropped_path(original)
        if not candidate or not os.path.isabs(candidate):
            # Relative text is prose or an already-typed mention. Only a
            # terminal drop produces an absolute path.
            return original, ""
        path = Path(candidate)
        if path.suffix.lower() not in _media_extensions():
            return original, ""
        if not path.is_file():
            # A path that does not exist is a string that looks like one.
            return original, ""

        repo = (root or _repo_root()).resolve()
        relative = _inside_repo(path, repo)
        if relative:
            return f"@{relative}", "mention"
        return f"/attach {path}", "attach"
    except Exception:  # noqa: BLE001 — a paste must never fail to paste
        logger.debug("[DropTranslate] degraded", exc_info=True)
        return original, ""


def install_drop_translation(buffer: Any, root: Optional[Path] = None) -> bool:
    """Translate drops as they land in *buffer*. NEVER raises.

    Wraps `Buffer.insert_text` rather than binding a paste key: a drop arrives
    as bracketed-paste text through the same insertion path as typing, and
    there is no keystroke to bind. Wrapping the one method every insertion
    goes through means a drop is caught however the terminal delivers it.

    Single-shot inserts (ordinary typing) are passed straight through — the
    check is skipped entirely unless the text is long enough to be a path, so
    per-keystroke cost stays nil.
    """
    try:
        if not drop_translation_enabled() or buffer is None:
            return False
        original_insert = buffer.insert_text

        def _insert(data: Any, *args: Any, **kwargs: Any) -> Any:
            try:
                text = str(data or "")
                # A path is never one character; skip the work for typing.
                if len(text) > 3 and os.path.isabs(
                    normalise_dropped_path(text) or "",
                ):
                    translated, kind = translate_drop(text, root)
                    if kind:
                        data = translated
            except Exception:  # noqa: BLE001
                pass
            return original_insert(data, *args, **kwargs)

        buffer.insert_text = _insert  # type: ignore[assignment]
        return True
    except Exception:  # noqa: BLE001
        logger.debug("[DropTranslate] install degraded", exc_info=True)
        return False

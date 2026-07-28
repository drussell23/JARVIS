"""``:emoji:`` shortcodes — type ``:fi`` and the palette offers 🔥.

CC parity, riding the SAME self-gating completer composition every other
vocabulary uses: ``/`` opens verbs, ``@`` opens files, ``:`` (two or more
characters into a name) opens emoji. Accepting replaces the ``:name``
fragment with the character itself.

The table is DATA, not behavior — the same standing a theme palette has:
a curated set of the shortcodes people actually type (git/gitmoji/slack
core), not a registry that pretends to be exhaustive. Extend via
``JARVIS_EMOJI_EXTRA`` (``name=emoji,name=emoji``) — an operator's pet
shortcode should not need a code change.

Env: ``JARVIS_EMOJI_SHORTCODES_ENABLED`` (default true).

NEVER raises into the completion path.
"""
from __future__ import annotations

import logging
import os
import re
from typing import Any, Dict

logger = logging.getLogger(__name__)

EMOJI_SHORTCODES_SCHEMA_VERSION: str = "emoji_shortcodes.1"

MASTER_FLAG_ENV_VAR: str = "JARVIS_EMOJI_SHORTCODES_ENABLED"
EXTRA_ENV_VAR: str = "JARVIS_EMOJI_EXTRA"

#: The working set. Data, one entry per line-of-thought an operator has.
SHORTCODES: Dict[str, str] = {
    "fire": "🔥", "rocket": "🚀", "bug": "🐛", "sparkles": "✨",
    "tada": "🎉", "zap": "⚡", "boom": "💥", "art": "🎨",
    "wrench": "🔧", "hammer": "🔨", "gear": "⚙️", "lock": "🔒",
    "unlock": "🔓", "key": "🔑", "shield": "🛡️", "warning": "⚠️",
    "check": "✅", "white_check_mark": "✅", "x": "❌", "cross": "❌",
    "question": "❓", "exclamation": "❗", "bulb": "💡", "brain": "🧠",
    "eyes": "👀", "thinking": "🤔", "thumbsup": "👍", "+1": "👍",
    "thumbsdown": "👎", "-1": "👎", "clap": "👏", "wave": "👋",
    "pray": "🙏", "muscle": "💪", "point_right": "👉", "point_up": "☝️",
    "heart": "❤️", "green_heart": "💚", "purple_heart": "💜",
    "star": "⭐", "star2": "🌟", "snake": "🐍", "dragon": "🐉",
    "bee": "🐝", "spider": "🕷️", "ghost": "👻", "skull": "💀",
    "robot": "🤖", "alien": "👽", "brain_": "🧠", "dna": "🧬",
    "microscope": "🔬", "telescope": "🔭", "satellite": "📡",
    "computer": "💻", "keyboard": "⌨️", "desktop": "🖥️", "phone": "📱",
    "floppy": "💾", "cd": "💿", "package": "📦", "inbox": "📥",
    "outbox": "📤", "mailbox": "📬", "memo": "📝", "pencil": "✏️",
    "book": "📖", "books": "📚", "bookmark": "🔖", "label": "🏷️",
    "folder": "📁", "open_folder": "📂", "clipboard": "📋",
    "chart": "📊", "chart_up": "📈", "chart_down": "📉",
    "calendar": "📅", "clock": "🕐", "hourglass": "⏳", "stopwatch": "⏱️",
    "alarm": "⏰", "bell": "🔔", "no_bell": "🔕", "mega": "📣",
    "loud": "🔊", "mute": "🔇", "mic": "🎤", "headphones": "🎧",
    "link": "🔗", "paperclip": "📎", "pushpin": "📌", "scissors": "✂️",
    "mag": "🔍", "mag_right": "🔎", "flashlight": "🔦", "candle": "🕯️",
    "seedling": "🌱", "herb": "🌿", "tree": "🌳", "leaves": "🍃",
    "sun": "☀️", "moon": "🌙", "cloud": "☁️", "rain": "🌧️",
    "lightning": "🌩️", "rainbow": "🌈", "ocean": "🌊", "earth": "🌍",
    "construction": "🚧", "stop": "🛑", "traffic_light": "🚦",
    "recycle": "♻️", "trash": "🗑️", "broom": "🧹", "soap": "🧼",
    "pill": "💊", "syringe": "💉", "bandage": "🩹", "ambulance": "🚑",
    "police": "🚓", "fire_engine": "🚒", "airplane": "✈️", "ship": "🚢",
    "anchor": "⚓", "compass": "🧭", "map": "🗺️", "mountain": "⛰️",
    "volcano": "🌋", "camp": "🏕️", "house": "🏠", "office": "🏢",
    "factory": "🏭", "bank": "🏦", "hospital": "🏥", "school": "🏫",
    "trophy": "🏆", "medal": "🏅", "crown": "👑", "gem": "💎",
    "money": "💰", "dollar": "💵", "credit_card": "💳", "coin": "🪙",
    "gift": "🎁", "balloon": "🎈", "confetti": "🎊", "dice": "🎲",
    "dart": "🎯", "puzzle": "🧩", "magic": "🪄", "crystal_ball": "🔮",
    "coffee": "☕", "tea": "🍵", "beer": "🍺", "pizza": "🍕",
    "cake": "🍰", "cookie": "🍪", "apple": "🍎", "banana": "🍌",
    "100": "💯", "ok": "🆗", "new": "🆕", "free": "🆓", "sos": "🆘",
    "infinity": "♾️", "hourglass_done": "⌛", "fast_forward": "⏩",
    "play": "▶️", "pause": "⏸️", "record": "⏺️", "eject": "⏏️",
}

#: The fragment behind the cursor that counts as a shortcode-in-progress:
#: a ``:`` at a word boundary followed by ≥2 name characters. One char is
#: far more likely to be prose punctuation than the start of a name.
_FRAGMENT_RE = re.compile(r"(?:^|\s):([a-z0-9_+-]{2,})$")


def is_emoji_shortcodes_enabled() -> bool:
    """Master flag — default true. NEVER raises."""
    raw = os.environ.get(MASTER_FLAG_ENV_VAR, "true")
    return raw.strip().lower() not in ("0", "false", "no", "off")


def shortcode_table() -> Dict[str, str]:
    """Builtin table + operator extras. NEVER raises."""
    table = dict(SHORTCODES)
    try:
        raw = os.environ.get(EXTRA_ENV_VAR, "").strip()
        for pair in raw.split(","):
            if "=" in pair:
                name, emoji = pair.split("=", 1)
                name, emoji = name.strip().lower(), emoji.strip()
                if name and emoji:
                    table[name] = emoji
    except Exception:  # noqa: BLE001
        pass
    return table


def _completer_base() -> Any:
    """INHERITING ``Completer`` is load-bearing — prompt_toolkit consumes
    outer-merged completers via ``get_completions_async``, which only the
    base supplies (the duck-typing lesson history_search learned live)."""
    try:
        from prompt_toolkit.completion import Completer
        return Completer
    except Exception:  # noqa: BLE001
        return object


class EmojiShortcodeCompleter(_completer_base()):  # type: ignore[misc]
    """Self-gating on the ``:name`` fragment — inert everywhere else."""

    def get_completions(self, document: Any, _event: Any = None):
        if not is_emoji_shortcodes_enabled():
            return
        try:
            from prompt_toolkit.completion import Completion
        except ImportError:
            return
        try:
            text = document.text_before_cursor or ""
            match = _FRAGMENT_RE.search(text)
            if match is None:
                return
            frag = match.group(1).lower()
            replace = -(len(frag) + 1)  # the fragment AND its colon
            table = shortcode_table()
            names = sorted(
                (n for n in table if frag in n),
                key=lambda n: (0 if n.startswith(frag) else 1, n),
            )
            for name in names[:24]:
                yield Completion(
                    text=table[name],
                    start_position=replace,
                    display=f"{table[name]}  :{name}:",
                    display_meta="emoji",
                )
        except Exception:  # noqa: BLE001 — never break typing
            logger.debug("[Emoji] completion degraded", exc_info=True)
            return


__all__ = [
    "EMOJI_SHORTCODES_SCHEMA_VERSION",
    "EXTRA_ENV_VAR",
    "EmojiShortcodeCompleter",
    "MASTER_FLAG_ENV_VAR",
    "SHORTCODES",
    "is_emoji_shortcodes_enabled",
    "shortcode_table",
]

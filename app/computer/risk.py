"""How risky is a keystroke or a piece of typed text on the desktop?

Every mouse/keyboard action already needs the user's "yes" (level 2). On top of that:

    level 3 (always refused)  Windows-key shortcuts, Ctrl+Alt+Del, Alt+F4, Shift+Delete
                              (permanent delete), the clipboard (Ctrl+V/C/X - it may hold a
                              copied password), typing passwords, card or account numbers
    not allowed               any key that isn't on the app's own list (see apps.py)
"""

import re

from app.memory.policy import contains_sensitive_data
from app.tools.base import PermissionLevel

_MODIFIERS = ("ctrl", "alt", "shift", "win")
_ALIASES = {
    "control": "ctrl",
    "return": "enter",
    "esc": "escape",
    "del": "delete",
    "windows": "win",
    "cmd": "win",
    "page up": "pageup",
    "page down": "pagedown",
    "pgup": "pageup",
    "pgdn": "pagedown",
    "arrowup": "up",
    "arrowdown": "down",
    "arrowleft": "left",
    "arrowright": "right",
}
_NEVER = {"ctrl+alt+delete", "alt+f4", "shift+delete", "ctrl+v", "ctrl+c", "ctrl+x", "alt+tab"}


def normalize_key(raw: str) -> str:
    """ "Ctrl + S" -> "ctrl+s"; modifiers always in the order ctrl, alt, shift, win."""
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", raw.strip()) if p.strip()]
    parts = [_ALIASES.get(p, p) for p in parts]
    mods = [m for m in _MODIFIERS if m in parts]
    keys = [p for p in parts if p not in _MODIFIERS]
    return "+".join([*mods, *keys])


def key_level(key: str) -> PermissionLevel:
    key = normalize_key(key)
    if key in _NEVER or key.startswith("win") or "+win" in key:
        return PermissionLevel.SENSITIVE
    return PermissionLevel.CONFIRM


_CARD_OR_ACCOUNT = re.compile(r"(?:\d[ -]?){12,19}")


def text_level(text: str) -> PermissionLevel:
    if contains_sensitive_data(text) or _CARD_OR_ACCOUNT.search(text):
        return PermissionLevel.SENSITIVE
    return PermissionLevel.CONFIRM


# pywinauto's send_keys gives these characters a special meaning - wrap them in braces.
_SPECIAL = set("+^%~(){}[]")
_KEY_NAMES = {
    "enter": "{ENTER}",
    "tab": "{TAB}",
    "backspace": "{BACKSPACE}",
    "delete": "{DELETE}",
    "escape": "{ESC}",
    "up": "{UP}",
    "down": "{DOWN}",
    "left": "{LEFT}",
    "right": "{RIGHT}",
    "home": "{HOME}",
    "end": "{END}",
    "pageup": "{PGUP}",
    "pagedown": "{PGDN}",
    "f5": "{F5}",
}
_MOD_CODES = {"ctrl": "^", "alt": "%", "shift": "+"}


def to_send_keys_text(text: str) -> str:
    """Literal text -> pywinauto send_keys syntax (newline = Enter, tab = Tab)."""
    out = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            out.append("{ENTER}")
        elif ch == "\t":
            out.append("{TAB}")
        elif ch in _SPECIAL:
            out.append("{" + ch + "}")
        else:
            out.append(ch)
    return "".join(out)


def to_send_keys_combo(key: str) -> str:
    """ "ctrl+a" -> "^a", "shift+tab" -> "+{TAB}", "alt+up" -> "%{UP}"."""
    *mods, last = normalize_key(key).split("+")
    code = _KEY_NAMES.get(last, last if len(last) == 1 else "{" + last.upper() + "}")
    return "".join(_MOD_CODES[m] for m in mods) + code

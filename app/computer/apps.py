"""Which desktop apps ARTHUR may use, and the rules for each one.

Everything here is plain data + small functions, so the safety rules can be tested
without opening real windows. `desktop.py` does the actual Windows work.

    notepad     only the new tab ARTHUR opened itself - never your other tabs/files
    calculator  all buttons and the display
    explorer    only while EVERY tab of the window shows one of your allowed folders;
                secret-looking files are hidden; items can be selected, never opened
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from app.files.workspace import is_blocked_name

# Keys every app may use (lower-case, "+" between modifiers and key).
NAVIGATION_KEYS = frozenset(
    {"up", "down", "left", "right", "home", "end", "pageup", "pagedown", "escape", "tab"}
)
EDITING_KEYS = frozenset(
    {"enter", "backspace", "delete", "shift+tab", "ctrl+a", "ctrl+z", "ctrl+y", "ctrl+home",
     "ctrl+end"}
)  # fmt: skip

# "Is this folder inside the user's allowed folders?" (the file sandbox answers it)
FolderCheck = Callable[[str], bool]


@dataclass(frozen=True)
class WindowInfo:
    """What Windows tells us about one top-level window."""

    handle: int
    title: str
    class_name: str
    process: str  # executable name, lower-case, e.g. "notepad.exe"
    locations: tuple[str, ...] = ()  # File Explorer: the folder shown in each tab
    arthur_tab: bool = False  # Notepad: the selected tab is one ARTHUR opened itself


@dataclass(frozen=True)
class AppSpec:
    name: str  # what the model and .env use, e.g. "notepad"
    label: str  # what the user sees, e.g. "Notepad"
    launch: tuple[str, ...]
    keys: frozenset[str]
    control_types: frozenset[str]  # UI Automation control types ARTHUR is shown
    can_type: bool = True
    hidden_names: frozenset[str] = field(default_factory=frozenset)  # controls never shown

    def matches(self, window: WindowInfo) -> bool:
        raise NotImplementedError

    def problem(self, window: WindowInfo, allowed: FolderCheck) -> str | None:
        """Why ARTHUR may NOT use this (matching) window right now, or None if it may."""
        if is_blocked_name(_document_name(window.title)):
            return f"{self.label} is showing a private file, so ARTHUR won't touch it."
        return None


class _Notepad(AppSpec):
    def matches(self, window: WindowInfo) -> bool:
        return window.class_name == "Notepad" and window.process == "notepad.exe"

    def problem(self, window: WindowInfo, allowed: FolderCheck) -> str | None:
        if not window.arthur_tab:
            # Your own files are open in other tabs - ARTHUR only works in the tab it opened.
            # (Not "any Untitled tab": Notepad renames new tabs after their first line.)
            return "Notepad is showing one of your tabs. ARTHUR only uses the new tab it opens."
        return super().problem(window, allowed)


class _Calculator(AppSpec):
    def matches(self, window: WindowInfo) -> bool:
        return window.class_name == "ApplicationFrameWindow" and window.title == "Calculator"


class _Explorer(AppSpec):
    def matches(self, window: WindowInfo) -> bool:
        # explorer.exe also runs the taskbar and desktop - only real folder windows count.
        return window.class_name == "CabinetWClass" and window.process == "explorer.exe"

    def problem(self, window: WindowInfo, allowed: FolderCheck) -> str | None:
        if not window.locations:
            return "File Explorer isn't showing a folder ARTHUR can check."
        for location in window.locations:
            if not allowed(location):
                return (
                    f"File Explorer is showing {location or 'a special place'}, which is "
                    "outside your allowed folders."
                )
        return None


APPS: dict[str, AppSpec] = {
    "notepad": _Notepad(
        name="notepad",
        label="Notepad",
        launch=("notepad.exe",),
        keys=NAVIGATION_KEYS | EDITING_KEYS,
        control_types=frozenset({"Document"}),  # no tabs (names of your files), no menus
    ),
    "calculator": _Calculator(
        name="calculator",
        label="Calculator",
        launch=("calc.exe",),
        keys=frozenset({"enter", "escape", "backspace", "delete"}),
        control_types=frozenset({"Button"}),
        hidden_names=frozenset(
            {"Minimize Calculator", "Maximize Calculator", "Keep on top", "Open Navigation"}
        ),
    ),
    "explorer": _Explorer(
        name="explorer",
        label="File Explorer",
        launch=("explorer.exe",),
        keys=NAVIGATION_KEYS | {"delete", "f5", "alt+up"},  # no Enter: that OPENS (runs) files
        control_types=frozenset({"ListItem"}),
        can_type=False,
    ),
}


def allowed_apps(names: str) -> dict[str, AppSpec]:
    """Parse COMPUTER_ALLOWED_APPS ("notepad;calculator"). Unknown names are ignored."""
    wanted = {n.strip().lower() for n in re.split(r"[;,]", names) if n.strip()}
    return {name: spec for name, spec in APPS.items() if name in wanted}


def calculator_text_ok(text: str) -> bool:
    return bool(re.fullmatch(r"[0-9+\-*/=.,%() \n]+", text))


def _document_name(title: str) -> str:
    """ "notes.txt - Notepad" -> "notes.txt" (also strips the unsaved-changes star)."""
    return title.rsplit(" - ", 1)[0].lstrip("*").strip()


def item_is_hidden(name: str) -> bool:
    """Explorer items ARTHUR must not see: the same secret-looking names the file tools hide."""
    return name.startswith(".") or is_blocked_name(name)

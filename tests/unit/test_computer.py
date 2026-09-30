"""Computer use (Phase 16): app rules, key/text risk, and the tools.

Everything here runs without touching the real desktop (a fake controller stands in).
The real-desktop check is opt-in:  $env:ARTHUR_DESKTOP_TESTS=1; pytest -k real_desktop
"""

import os
from pathlib import Path

import pytest

from app.computer.apps import APPS, WindowInfo, allowed_apps, calculator_text_ok, item_is_hidden
from app.computer.desktop import DesktopError, WindowSnapshot, workspace_rules
from app.computer.risk import (
    key_level,
    normalize_key,
    text_level,
    to_send_keys_combo,
    to_send_keys_text,
)
from app.files.workspace import Workspace
from app.security.permissions import PermissionPolicy
from app.tools.base import PermissionLevel, ToolContext
from app.tools.computer_tools import (
    ClickControlTool,
    OpenAppTool,
    PressKeyTool,
    ReadWindowTool,
    TypeTextTool,
)
from app.tools.registry import ToolRegistry

ALLOW_ALL = lambda folder: True  # noqa: E731
ALLOW_NONE = lambda folder: False  # noqa: E731


def notepad(title="Untitled - Notepad", arthur_tab=True):
    return WindowInfo(1, title, "Notepad", "notepad.exe", arthur_tab=arthur_tab)


# ---------- which windows ARTHUR may use ----------


def test_notepad_only_in_arthurs_own_tab():
    spec = APPS["notepad"]
    assert spec.matches(notepad())
    assert spec.problem(notepad(), ALLOW_ALL) is None
    # Notepad renames a new tab after its first line - still ARTHUR's tab:
    assert spec.problem(notepad("Shopping list: - Notepad"), ALLOW_ALL) is None
    # Your own tabs (even an Untitled one you made) are off limits:
    assert "your tabs" in spec.problem(notepad(arthur_tab=False), ALLOW_ALL)
    assert "private file" in spec.problem(notepad(".env - Notepad"), ALLOW_ALL)


def test_calculator_matches_only_the_calculator_frame():
    spec = APPS["calculator"]
    assert spec.matches(
        WindowInfo(2, "Calculator", "ApplicationFrameWindow", "applicationframehost.exe")
    )
    # Settings uses the same frame class and process:
    assert not spec.matches(
        WindowInfo(3, "Settings", "ApplicationFrameWindow", "applicationframehost.exe")
    )


def test_explorer_rules():
    spec = APPS["explorer"]
    documents = WindowInfo(4, "ARTHUR", "CabinetWClass", "explorer.exe", (r"C:\Users\x\Documents",))
    assert spec.matches(documents)
    assert spec.problem(documents, ALLOW_ALL) is None
    assert "outside your allowed folders" in spec.problem(documents, ALLOW_NONE)
    # The taskbar and desktop also belong to explorer.exe - never matched:
    assert not spec.matches(WindowInfo(5, "Taskbar", "Shell_TrayWnd", "explorer.exe"))
    # One tab outside the allowed folders makes the whole window off limits:
    tabs = WindowInfo(6, "x", "CabinetWClass", "explorer.exe", (r"C:\ok", r"C:\Windows"))
    assert spec.problem(tabs, lambda folder: folder == r"C:\ok") is not None
    assert "Enter" not in {k.title() for k in spec.keys}  # Enter would OPEN (run) a file
    assert not spec.can_type


def test_allowed_apps_parsing():
    assert list(allowed_apps("notepad; Calculator")) == ["notepad", "calculator"]
    assert allowed_apps("paint;cmd;powershell") == {}


def test_hidden_explorer_items():
    assert item_is_hidden(".env")
    assert item_is_hidden("id_rsa")
    assert item_is_hidden("bank passwords.xlsx")
    assert not item_is_hidden("report.docx")


def test_calculator_accepts_only_maths():
    assert calculator_text_ok("12*7=")
    assert calculator_text_ok("(3.5 + 2) / 4 =")
    assert not calculator_text_ok("hello")


def test_workspace_rules_for_explorer(tmp_path):
    root = tmp_path / "allowed"
    (root / "sub").mkdir(parents=True)
    workspace = Workspace([root], root, system_roots=[Path(os.environ["SYSTEMROOT"])])
    folder_allowed, resolve_folder = workspace_rules(workspace)
    assert folder_allowed(str(root / "sub"))
    assert not folder_allowed(str(tmp_path))
    assert not folder_allowed("::{20D04FE0-3AEA-1069-A2D8-08002B30309D}")  # "This PC"
    assert not folder_allowed("")
    assert resolve_folder(str(root / "sub")) == (root / "sub").resolve()
    with pytest.raises(DesktopError):
        resolve_folder(r"C:\Windows")


# ---------- keys and text ----------


@pytest.mark.parametrize(
    ("raw", "normal"),
    [
        ("Ctrl + S", "ctrl+s"),
        ("shift+ctrl+z", "ctrl+shift+z"),
        ("Return", "enter"),
        ("ESC", "escape"),
    ],
)
def test_normalize_key(raw, normal):
    assert normalize_key(raw) == normal


@pytest.mark.parametrize(
    ("key", "level"),
    [
        ("enter", PermissionLevel.CONFIRM),
        ("ctrl+z", PermissionLevel.CONFIRM),
        ("win+r", PermissionLevel.SENSITIVE),  # Run dialog -> any program
        ("Windows", PermissionLevel.SENSITIVE),
        ("ctrl+alt+del", PermissionLevel.SENSITIVE),
        ("alt+f4", PermissionLevel.SENSITIVE),
        ("shift+delete", PermissionLevel.SENSITIVE),  # permanent delete
        ("ctrl+v", PermissionLevel.SENSITIVE),  # the clipboard may hold a password
    ],
)
def test_key_levels(key, level):
    assert key_level(key) == level


def test_text_levels():
    assert text_level("Shopping list: milk") == PermissionLevel.CONFIRM
    assert text_level("my password is hunter2") == PermissionLevel.SENSITIVE
    assert text_level("card 4111 1111 1111 1111") == PermissionLevel.SENSITIVE


def test_send_keys_escaping():
    assert to_send_keys_text("a+b (c) {d}\nx") == "a{+}b {(}c{)} {{}d{}}{ENTER}x"
    assert to_send_keys_combo("ctrl+a") == "^a"
    assert to_send_keys_combo("shift+tab") == "+{TAB}"
    assert to_send_keys_combo("alt+up") == "%{UP}"


# ---------- tools with a fake desktop ----------


class FakeDesktop:
    def __init__(self):
        self.apps = allowed_apps("notepad;calculator;explorer")
        self.calls = []
        self.cancelled = False

    def snapshot(self, app="calculator"):
        return WindowSnapshot(
            app=app,
            title="Calculator",
            text="Display is 84",
            elements=[{"id": 1, "type": "Button", "name": "Seven"}],
        )

    async def open_app(self, app, folder=None):
        self.calls.append(("open", app, folder))
        return self.snapshot(app)

    async def read_window(self, app):
        return self.snapshot(app)

    async def click(self, app, element):
        self.calls.append(("click", app, element))
        return self.snapshot(app)

    async def type_text(self, app, text):
        self.calls.append(("type", app, text))
        return self.snapshot(app)

    async def press_key(self, app, key):
        self.calls.append(("key", app, key))
        return self.snapshot(app)

    def element(self, app, element):
        return {"type": "Button", "name": "Seven"} if element == 1 else None

    def cancel(self):
        self.cancelled = True


@pytest.fixture
def desktop():
    return FakeDesktop()


@pytest.fixture
def registry(desktop):
    registry = ToolRegistry(PermissionPolicy(), None)
    for tool in (OpenAppTool, ReadWindowTool, ClickControlTool, TypeTextTool, PressKeyTool):
        registry.register(tool(desktop))
    return registry


async def test_open_and_read_run_without_asking(registry, desktop):
    opened = await registry.execute("open_app", {"app": "calculator"})
    assert opened.status == "ok"
    assert opened.output["elements"] == ["[1] Button: Seven"]
    assert (await registry.execute("read_window", {"app": "calculator"})).status == "ok"


async def test_every_input_action_asks_first(registry, desktop):
    click = await registry.execute("click_control", {"app": "calculator", "element": 1})
    assert click.status == "needs_confirmation"
    assert click.preview == 'Press Button "Seven" in Calculator'

    typed = await registry.execute("type_text", {"app": "notepad", "text": "milk\neggs"})
    assert typed.status == "needs_confirmation"
    assert "milk" in typed.preview

    key = await registry.execute("press_key", {"app": "explorer", "shortcut": "Delete"})
    assert key.status == "needs_confirmation"
    assert "Recycle Bin" in key.preview
    assert desktop.calls == []  # nothing happened before the user's "yes"

    done = await registry.execute(
        "click_control", {"app": "calculator", "element": 1}, ToolContext(confirmed=True)
    )
    assert done.status == "ok"
    assert desktop.calls == [("click", "calculator", 1)]


async def test_dangerous_input_is_refused_even_with_yes(registry, desktop):
    yes = ToolContext(confirmed=True)
    password = await registry.execute(
        "type_text", {"app": "notepad", "text": "password: hunter2"}, yes
    )
    assert password.status == "denied"
    run_dialog = await registry.execute("press_key", {"app": "notepad", "shortcut": "win+r"}, yes)
    assert run_dialog.status == "denied"
    wrong_key = await registry.execute("press_key", {"app": "explorer", "shortcut": "enter"}, yes)
    assert wrong_key.status == "error"
    assert "isn't allowed in File Explorer" in wrong_key.error
    assert desktop.calls == []


async def test_stop_cancels_typing(desktop):
    import asyncio

    class SlowDesktop(FakeDesktop):
        async def type_text(self, app, text):
            await asyncio.sleep(10)

    slow = SlowDesktop()
    tool = TypeTextTool(slow)
    task = asyncio.create_task(
        tool.run(tool.input_model(app="notepad", text="long text"), ToolContext(confirmed=True))
    )
    await asyncio.sleep(0.05)
    task.cancel()  # what the Stop button does to the running turn
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slow.cancelled


# ---------- real desktop (opt-in) ----------


@pytest.mark.skipif(
    os.environ.get("ARTHUR_DESKTOP_TESTS") != "1", reason="set ARTHUR_DESKTOP_TESTS=1 to run"
)
async def test_real_desktop_calculator_and_rules():
    from app.computer.desktop import DesktopController

    controller = DesktopController(
        allowed_apps("calculator"), ALLOW_NONE, lambda raw: Path(raw), Path.home()
    )
    try:
        await controller.open_app("calculator")
        snapshot = await controller.type_text("calculator", "12*7=")
        assert "84" in snapshot.text
        with pytest.raises(DesktopError, match="not an allowed app"):
            await controller.open_app("notepad")
    finally:
        controller.close()

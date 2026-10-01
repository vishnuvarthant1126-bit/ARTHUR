"""Controlled desktop use through Windows UI Automation (pywinauto).

UI Automation is the accessibility system screen readers use: every window is a tree
of named controls ("Button 'Seven'", "Document 'Text editor'"). ARTHUR lists them with
numbers - like the browser's numbered links - instead of guessing mouse coordinates.

Safety, enforced here and not by the model:
- only apps in COMPUTER_ALLOWED_APPS, and only windows their rules allow (apps.py);
- the rules are checked again right before EVERY action (the window may have changed);
- buttons are pressed with UI Automation "invoke" - the real mouse never moves;
- ARTHUR NEVER SENDS KEYSTROKES. Simulated key presses go to whichever window has the
  keyboard focus - in a live test, text meant for Notepad (with its Enter key) landed in
  another app when that app took the focus. Instead:
    Notepad     text and editing keys are Windows messages sent to the handle of Notepad's
                own text control (EM_REPLACESEL, WM_KEYDOWN ...): they can't arrive anywhere else
    Calculator  its buttons are pressed through UI Automation ("invoke")
    Explorer    refresh / go up / delete go through the Windows shell, for the checked items
- typed text is read back and compared - ARTHUR never reports success it didn't see;
- apps are started from their fixed Windows paths, never through a shell.

All UI Automation calls run on ONE worker thread (COM objects belong to the thread
that created them); the async methods hand work to it.
"""

import asyncio
import contextlib
import ctypes
import io
import os
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from app.computer.apps import (
    AppSpec,
    FolderCheck,
    WindowInfo,
    calculator_text_ok,
    item_is_hidden,
)
from app.computer.risk import normalize_key
from app.observability.logging import get_logger

log = get_logger(__name__)

MAX_ELEMENTS = 80
MAX_TEXT_CHARS = 20_000

# Windows messages for edit controls (sent to ONE control's handle, never "to the keyboard").
EM_SETSEL, EM_REPLACESEL, EM_UNDO, EM_REDO = 0x00B1, 0x00C2, 0x00C7, 0x0454
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
SW_SHOWNOACTIVATE, GA_ROOT = 4, 2

# key -> (how, value): insert text, set the selection, an edit message, or a virtual-key code
NOTEPAD_KEYS: dict[str, tuple[str, Any]] = {
    "enter": ("text", "\r\n"),
    "tab": ("text", "\t"),
    "ctrl+a": ("select", (0, -1)),
    "ctrl+home": ("select", (0, 0)),
    "ctrl+end": ("select", (-1, -1)),
    "ctrl+z": ("message", EM_UNDO),
    "ctrl+y": ("message", EM_REDO),
    "backspace": ("key", 0x08),
    "delete": ("key", 0x2E),
    "pageup": ("key", 0x21),
    "pagedown": ("key", 0x22),
    "end": ("key", 0x23),
    "home": ("key", 0x24),
    "left": ("key", 0x25),
    "up": ("key", 0x26),
    "right": ("key", 0x27),
    "down": ("key", 0x28),
}
# character / name -> the automation id of Calculator's button
CALCULATOR_BUTTONS = {
    **{str(n): f"num{n}Button" for n in range(10)},
    "+": "plusButton",
    "-": "minusButton",
    "*": "multiplyButton",
    "x": "multiplyButton",
    "×": "multiplyButton",
    "/": "divideButton",
    "÷": "divideButton",
    "=": "equalButton",
    "\n": "equalButton",
    ".": "decimalSeparatorButton",
    "%": "percentButton",
    "clear": "clearButton",
    "clear_entry": "clearEntryButton",
    "backspace": "backSpaceButton",
}
CALCULATOR_KEYS = {
    "enter": "=",
    "escape": "clear",
    "backspace": "backspace",
    "delete": "clear_entry",
}


class DesktopError(Exception):
    """A desktop problem that is safe to show the user."""


class WindowSnapshot(BaseModel):
    app: str
    title: str
    text: str
    elements: list[dict]


class DesktopController:
    def __init__(
        self,
        apps: dict[str, AppSpec],
        folder_allowed: FolderCheck,
        resolve_folder: Callable[[str], Path],
        default_folder: Path,
    ) -> None:
        self.apps = apps
        self.folder_allowed = folder_allowed  # is this Explorer location allowed?
        self.resolve_folder = resolve_folder  # user text -> allowed folder (or raises)
        self.default_folder = default_folder
        self._executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="arthur-desktop", initializer=_init_thread
        )
        self._stop = threading.Event()
        self._last: dict[str, tuple[int, dict[int, Any]]] = {}  # app -> (window, id -> control)
        self._own_tabs: set[tuple | None] = set()  # Notepad tabs ARTHUR opened (UIA ids)
        self._preferred: dict[str, int] = {}  # app -> the window ARTHUR opened last

    # ---------- public async API ----------

    async def open_app(self, app: str, folder: str | None = None) -> WindowSnapshot:
        return await self._run(self._open_app, app, folder)

    async def read_window(self, app: str) -> WindowSnapshot:
        return await self._run(self._snapshot, app)

    async def click(self, app: str, element: int) -> WindowSnapshot:
        return await self._run(self._click, app, element)

    async def type_text(self, app: str, text: str) -> WindowSnapshot:
        return await self._run(self._type_text, app, text)

    async def press_key(self, app: str, key: str) -> WindowSnapshot:
        return await self._run(self._press_key, app, key)

    async def selection(self, app: str) -> list[str]:
        """Names of the items selected in File Explorer (for the delete confirmation)."""
        return await self._run(self._selection, app)

    async def screenshot(self, app: str | None, max_side: int = 1280) -> bytes:
        return await self._run(self._screenshot, app, max_side)

    def element(self, app: str, element: int) -> dict | None:
        """What the last read_window said about element [n] (for confirmation previews)."""
        _, controls = self._last.get(app, (0, {}))
        control = controls.get(element)
        if control is None:
            return None
        info = control.element_info
        return {"type": info.control_type, "name": info.name or ""}

    def cancel(self) -> None:
        """Emergency stop: a running sequence of button presses stops at the next one."""
        self._stop.set()

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)

    async def _run(self, fn, *args):
        self._stop.clear()
        return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    # ---------- everything below runs on the worker thread ----------

    def _spec(self, app: str) -> AppSpec:
        spec = self.apps.get(app.strip().lower())
        if spec is None:
            allowed = ", ".join(self.apps) or "none"
            raise DesktopError(f"'{app}' is not an allowed app. Allowed: {allowed}.")
        return spec

    def _windows(self) -> list[tuple[WindowInfo, Any]]:
        from pywinauto import Desktop

        locations = _explorer_locations()
        result = []
        for window in Desktop(backend="uia").windows():
            info = window.element_info
            result.append(
                (
                    WindowInfo(
                        handle=info.handle or 0,
                        title=info.name or "",
                        class_name=info.class_name or "",
                        process=_process_name(info.process_id),
                        locations=tuple(locations.get(info.handle, ())),
                        arthur_tab=info.class_name == "Notepad"
                        and _selected_tab(window) in self._own_tabs,
                    ),
                    window,
                )
            )
        return result

    def _find(self, app: str) -> tuple[AppSpec, WindowInfo, Any]:
        """The window ARTHUR may use for this app right now - the rules are checked every time."""
        spec = self._spec(app)
        matching = [(info, w) for info, w in self._windows() if spec.matches(info)]
        # The window ARTHUR opened last comes first (you may have other ones open).
        matching.sort(key=lambda pair: pair[0].handle != self._preferred.get(spec.name))
        if not matching:
            raise DesktopError(f"{spec.label} isn't open. Use open_app first.")
        problems = []
        for info, window in matching:
            problem = spec.problem(info, self.folder_allowed)
            if problem is None:
                return spec, info, window
            problems.append(problem)
        raise DesktopError(problems[0])

    def _open_app(self, app: str, folder: str | None) -> WindowSnapshot:
        spec = self._spec(app)
        args: list[str] = []
        if spec.name == "explorer":
            path = self.resolve_folder(folder) if folder else self.default_folder
            args = [str(path)]
        elif folder:
            raise DesktopError("Only File Explorer opens a folder.")

        if spec.name == "notepad":
            # Notepad may already be open with your files (or reopen them on start):
            # ARTHUR always adds its OWN new tab and remembers it.
            if not any(spec.matches(info) for info, _ in self._windows()):
                subprocess.Popen([_system_exe(spec.launch[0])])  # no shell, fixed path
            window = self._wait_for_window(spec)
            window.set_focus()  # a minimized Notepad has no text area to work with
            _invoke_named(window, "Button", "Add New Tab")
            self._own_tabs.add(_selected_tab(window))
        elif spec.name == "explorer" or not self._try_find(app):
            subprocess.Popen([_system_exe(spec.launch[0]), *args])
        log.info("desktop_open_app", app=spec.name, folder=args[0] if args else None)

        wanted = args[0].lower() if args else None  # Explorer: the folder just opened
        deadline = time.monotonic() + 10
        while True:
            try:
                if wanted:  # prefer the Explorer window that shows the new folder
                    for info, _ in self._windows():
                        if spec.matches(info) and wanted in (x.lower() for x in info.locations):
                            self._preferred[spec.name] = info.handle
                _spec, info, window = self._find(app)
                if wanted and wanted not in (x.lower() for x in info.locations):
                    raise DesktopError(f"{spec.label} didn't show {args[0]}.")
                self._preferred[spec.name] = info.handle
                window.set_focus()  # bring it to the front, like opening it yourself
                return self._snapshot(app)
            except DesktopError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.4)

    def _wait_for_window(self, spec: AppSpec) -> Any:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for info, window in self._windows():
                if spec.matches(info):
                    return window
            time.sleep(0.4)
        raise DesktopError(f"{spec.label} didn't start.")

    def _try_find(self, app: str) -> bool:
        try:
            self._find(app)
            return True
        except DesktopError:
            return False

    def _snapshot(self, app: str) -> WindowSnapshot:
        spec, info, window = self._find(app)
        controls: dict[int, Any] = {}
        elements = []
        for control_type in sorted(spec.control_types):
            for control in window.descendants(control_type=control_type):
                name = control.element_info.name or ""
                if name in spec.hidden_names or (spec.name == "explorer" and item_is_hidden(name)):
                    continue
                if len(elements) >= MAX_ELEMENTS:
                    break
                element_id = len(elements) + 1
                controls[element_id] = control
                element = {"id": element_id, "type": control_type, "name": name[:80]}
                if control_type == "ListItem":
                    element["selected"] = _safe(control.is_selected, False)
                elements.append(element)
        self._last[spec.name] = (info.handle, controls)
        return WindowSnapshot(
            app=spec.name, title=info.title, text=self._text(spec, info, window), elements=elements
        )

    def _text(self, spec: AppSpec, info: WindowInfo, window: Any) -> str:
        if spec.name == "notepad":
            documents = window.descendants(control_type="Document")
            if not documents:
                return ""
            document = documents[0]
            text = _safe(lambda: document.iface_text.DocumentRange.GetText(MAX_TEXT_CHARS), "")
            return text.replace("\r\n", "\n").replace("\r", "\n")
        if spec.name == "calculator":
            wanted = {"CalculatorExpression", "CalculatorResults"}
            texts = [
                c.element_info.name
                for c in window.descendants(control_type="Text")
                if c.element_info.automation_id in wanted and c.element_info.name
            ]
            return "\n".join(texts)
        if spec.name == "explorer":
            return f"Folder: {info.locations[0]}"
        return ""

    def _control(self, app: str, element: int) -> tuple[AppSpec, WindowInfo, Any]:
        spec, info, _window = self._find(app)  # rules checked again NOW
        handle, controls = self._last.get(spec.name, (0, {}))
        if handle != info.handle or element not in controls:
            raise DesktopError(
                f"[{element}] isn't on the current {spec.label} window. Use read_window first."
            )
        return spec, info, controls[element]

    def _click(self, app: str, element: int) -> WindowSnapshot:
        spec, _info, control = self._control(app, element)
        control_type = control.element_info.control_type
        if control_type == "ListItem":
            control.select()  # select only - opening a file could run a program
        elif control_type == "Button":
            control.invoke()  # UI Automation "press" - the mouse doesn't move
        else:
            control.set_focus()
        log.info("desktop_click", app=spec.name, element=element, type=control_type)
        time.sleep(0.3)
        return self._snapshot(app)

    def _type_text(self, app: str, text: str) -> WindowSnapshot:
        spec, info, window = self._find(app)
        if not spec.can_type:
            raise DesktopError(f"ARTHUR doesn't type into {spec.label}.")
        if spec.name == "calculator":
            if not calculator_text_ok(text):
                raise DesktopError("Calculator only takes numbers and + - * / = . %")
            self._calculator_press(window, ["clear", *text])  # each sum starts fresh
        else:
            handle = self._notepad_edit(info, window)
            wanted = text.replace("\r\n", "\n").replace("\r", "\n")
            _send(handle, EM_SETSEL, -1, -1)  # caret to the end of the document
            _send(handle, EM_REPLACESEL, 1, wanted.replace("\n", "\r\n"))
            time.sleep(0.1)
            if wanted not in self._text(spec, info, window):
                raise DesktopError(
                    "Notepad didn't take the text as sent. Use read_window to see what it shows."
                )
        log.info("desktop_type", app=spec.name, chars=len(text))
        return self._snapshot(app)

    def _notepad_edit(self, info: WindowInfo, window: Any) -> int:
        """The window handle of Notepad's text control in ARTHUR's own tab."""
        import win32gui

        if win32gui.IsIconic(info.handle):  # minimized: show it WITHOUT taking the focus
            win32gui.ShowWindow(info.handle, SW_SHOWNOACTIVATE)
            time.sleep(0.4)
        documents = window.descendants(control_type="Document")
        handle = documents[0].element_info.handle if documents else 0
        if (
            not handle
            or win32gui.GetClassName(handle) != "RichEditD2DPT"
            or win32gui.GetAncestor(handle, GA_ROOT) != info.handle
        ):
            raise DesktopError("Notepad's text area wasn't found.")
        return handle

    def _calculator_press(self, window: Any, symbols: list[str]) -> None:
        """Press Calculator's own buttons through UI Automation - no keyboard involved."""
        deadline = time.monotonic() + 5  # a just-started Calculator needs a moment
        while True:
            buttons = {
                b.element_info.automation_id: b for b in window.descendants(control_type="Button")
            }
            if "clearButton" in buttons or time.monotonic() > deadline:
                break
            time.sleep(0.3)
        for symbol in symbols:
            if self._stop.is_set():
                raise DesktopError("Stopped.")
            if symbol in (" ", ","):
                continue
            button = buttons.get(CALCULATOR_BUTTONS.get(symbol, ""))
            if button is None:
                raise DesktopError(f"Calculator has no '{symbol}' button in this mode.")
            button.invoke()
            time.sleep(0.03)

    def _press_key(self, app: str, key: str) -> WindowSnapshot:
        spec, info, window = self._find(app)
        key = normalize_key(key)
        if key not in spec.keys:
            raise DesktopError(f"'{key}' isn't allowed in {spec.label}.")
        if spec.name == "notepad":
            handle = self._notepad_edit(info, window)
            kind, value = NOTEPAD_KEYS[key]
            if kind == "text":
                _send(handle, EM_REPLACESEL, 1, value)
            elif kind == "select":
                _send(handle, EM_SETSEL, *value)
            elif kind == "message":
                _send(handle, value, 0, 0)
            else:  # a navigation/editing key, delivered to this control only
                _send(handle, WM_KEYDOWN, value, 0)
                _send(handle, WM_KEYUP, value, 0xC0000001)
        elif spec.name == "calculator":
            self._calculator_press(window, [CALCULATOR_KEYS[key]])
        else:
            self._explorer_key(info, key)
        log.info("desktop_key", app=spec.name, key=key)
        time.sleep(0.3)
        return self._snapshot(app)

    def _explorer_key(self, info: WindowInfo, key: str) -> None:
        shell_window = self._explorer_tab(info)
        if key == "f5":
            shell_window.Refresh()
        elif key == "alt+up":
            parent = str(Path(info.locations[0]).parent)
            if not self.folder_allowed(parent):
                raise DesktopError("The folder above is outside your allowed folders.")
            shell_window.Navigate2(parent)
        elif key == "delete":
            from win32com.shell import shell, shellcon

            flags = (
                shellcon.FOF_ALLOWUNDO  # = to the Recycle Bin, not gone for good
                | shellcon.FOF_NOCONFIRMATION  # the user already confirmed in ARTHUR
                | shellcon.FOF_SILENT
                | shellcon.FOF_NOERRORUI
            )
            for path in self._selected_paths(info):
                shell.SHFileOperation((0, shellcon.FO_DELETE, path, None, flags, None, None))
                log.info("desktop_recycled", name=Path(path).name)

    def _explorer_tab(self, info: WindowInfo) -> Any:
        import win32com.client

        if len(info.locations) != 1:
            raise DesktopError(
                "That File Explorer window has several tabs and ARTHUR can't tell which one "
                "is in front. Close the other tabs, or use open_app for a new window."
            )
        for shell_window in win32com.client.Dispatch("Shell.Application").Windows():
            if _safe(lambda w=shell_window: int(w.HWND), 0) == info.handle:
                return shell_window
        raise DesktopError("File Explorer window not found.")

    def _selected_paths(self, info: WindowInfo) -> list[str]:
        """Selected items, checked: only things ARTHUR may see, inside allowed folders."""
        items = self._explorer_tab(info).Document.SelectedItems()
        paths = [items.Item(i).Path for i in range(items.Count)]
        if not paths:
            raise DesktopError("Nothing is selected in File Explorer. Select an item first.")
        for path in paths:
            if item_is_hidden(Path(path).name) or not self.folder_allowed(str(Path(path).parent)):
                raise DesktopError("The selection includes something ARTHUR may not touch.")
        return paths

    def _selection(self, app: str) -> list[str]:
        _spec, info, _window = self._find(app)
        return [Path(path).name for path in self._selected_paths(info)]

    def _screenshot(self, app: str | None, max_side: int) -> bytes:
        from PIL import ImageGrab

        if app is None:
            image = ImageGrab.grab(all_screens=True)
        else:
            _spec, info, _window = self._find(app)
            image = _capture_window(info.handle)
        image = image.convert("RGB")
        image.thumbnail((max_side, max_side))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=85)
        return buffer.getvalue()


# ---------- small Windows helpers ----------


def _init_thread() -> None:
    import pythoncom

    pythoncom.CoInitialize()
    with contextlib.suppress(AttributeError, OSError):  # real pixel sizes on 125 % displays
        ctypes.windll.shcore.SetProcessDpiAwareness(2)


def _safe(fn, default):
    try:
        return fn()
    except Exception:
        return default


def _system_exe(name: str) -> str:
    """Full path in the Windows folder, so a look-alike program elsewhere can't be started."""
    root = os.environ.get("SYSTEMROOT", r"C:\Windows")
    folder = root if name == "explorer.exe" else os.path.join(root, "System32")
    return os.path.join(folder, name)


def _process_name(pid: int | None) -> str:
    if not pid:
        return ""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(1024)
        size = ctypes.c_ulong(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return Path(buffer.value).name.lower()
        return ""
    finally:
        kernel32.CloseHandle(handle)


def _explorer_locations() -> dict[int, list[str]]:
    """File Explorer window -> the folder shown in each of its tabs."""
    import win32com.client

    locations: dict[int, list[str]] = {}
    for window in win32com.client.Dispatch("Shell.Application").Windows():
        handle = _safe(lambda w=window: int(w.HWND), 0)
        path = _safe(lambda w=window: w.Document.Folder.Self.Path, "")
        locations.setdefault(handle, []).append(path)
    return locations


def _invoke_named(window: Any, control_type: str, name: str) -> None:
    for control in window.descendants(control_type=control_type):
        if control.element_info.name == name:
            control.invoke()
            time.sleep(0.5)
            return
    raise DesktopError(f"Couldn't find '{name}'.")


def _send(handle: int, message: int, wparam: Any, lparam: Any) -> int:
    """Send one Windows message to ONE control. It is delivered to that handle - it does
    not matter which window has the focus."""
    import win32gui

    return win32gui.SendMessage(handle, message, wparam, lparam)


def _capture_window(handle: int):
    """Picture of ONE window, even if other windows cover it (PrintWindow)."""
    import win32gui
    import win32ui
    from PIL import Image

    left, top, right, bottom = win32gui.GetWindowRect(handle)
    width, height = right - left, bottom - top
    window_dc = win32gui.GetWindowDC(handle)
    source = win32ui.CreateDCFromHandle(window_dc)
    memory = source.CreateCompatibleDC()
    bitmap = win32ui.CreateBitmap()
    try:
        bitmap.CreateCompatibleBitmap(source, width, height)
        memory.SelectObject(bitmap)
        ctypes.windll.user32.PrintWindow(handle, memory.GetSafeHdc(), 2)  # 2 = full content
        bits = bitmap.GetBitmapBits(True)
        return Image.frombuffer("RGB", (width, height), bits, "raw", "BGRX", 0, 1)
    finally:
        win32gui.DeleteObject(bitmap.GetHandle())
        memory.DeleteDC()
        source.DeleteDC()
        win32gui.ReleaseDC(handle, window_dc)


def workspace_rules(workspace) -> tuple[FolderCheck, Callable[[str], Path]]:
    """The file sandbox (Phase 14) decides which folders File Explorer may show."""
    from app.files.workspace import FileAccessError

    def folder_allowed(location: str) -> bool:
        if not location or location.startswith("::"):  # "This PC", "Home", ... special places
            return False
        try:
            return workspace.is_allowed(Path(location).resolve(strict=True))
        except (OSError, ValueError):
            return False

    def resolve_folder(raw: str) -> Path:
        try:
            path = workspace.resolve(raw)
        except FileAccessError as exc:
            hint = " Use list_folder or find_files to get the exact folder path."
            raise DesktopError(str(exc) + hint) from exc
        if not path.is_dir():
            raise DesktopError(f"Not a folder: {path}")
        return path

    return folder_allowed, resolve_folder


def _selected_tab(window: Any) -> tuple | None:
    """UI Automation id of the selected tab (it stays the same while the tab exists)."""
    for tab in window.descendants(control_type="TabItem"):
        if _safe(tab.is_selected, False):
            return tuple(tab.element_info.runtime_id or ())
    return None

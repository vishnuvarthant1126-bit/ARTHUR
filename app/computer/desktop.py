"""Controlled desktop use through Windows UI Automation (pywinauto).

UI Automation is the accessibility system screen readers use: every window is a tree
of named controls ("Button 'Seven'", "Document 'Text editor'"). ARTHUR lists them with
numbers - like the browser's numbered links - instead of guessing mouse coordinates.

Safety, enforced here and not by the model:
- only apps in COMPUTER_ALLOWED_APPS, and only windows their rules allow (apps.py);
- the rules are checked again right before EVERY action (the window may have changed);
- buttons are pressed with UI Automation "invoke" - the real mouse never moves;
- typing goes in small chunks; before each one ARTHUR checks the right window still
  has the keyboard focus and that Stop wasn't pressed;
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
from app.computer.risk import normalize_key, to_send_keys_combo, to_send_keys_text
from app.observability.logging import get_logger

log = get_logger(__name__)

MAX_ELEMENTS = 80
MAX_TEXT_CHARS = 20_000
TYPE_CHUNK = 20  # characters typed between focus/stop checks


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
        """Emergency stop: typing stops at the next chunk."""
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
        from pywinauto.keyboard import send_keys

        spec, info, window = self._find(app)
        if not spec.can_type:
            raise DesktopError(f"ARTHUR doesn't type into {spec.label}.")
        if spec.name == "calculator" and not calculator_text_ok(text):
            raise DesktopError("Calculator only takes numbers and + - * / = . %")
        window.set_focus()  # also restores a minimized window (which has no text area)
        target = window
        if spec.name == "notepad":
            documents = window.descendants(control_type="Document")
            if not documents:
                raise DesktopError("Notepad's text area wasn't found.")
            target = documents[0]
            target.set_focus()
        elif spec.name == "calculator":
            _require_foreground(info.handle)
            send_keys("{ESC}")  # every typed calculation starts fresh
        for start in range(0, len(text), TYPE_CHUNK):
            if self._stop.is_set():
                raise DesktopError("Stopped - typing was cancelled.")
            _require_foreground(info.handle)
            if spec.name == "notepad" and _selected_tab(window) not in self._own_tabs:
                raise DesktopError("You switched Notepad tabs, so ARTHUR stopped typing.")
            chunk = text[start : start + TYPE_CHUNK]
            send_keys(to_send_keys_text(chunk), with_spaces=True, with_tabs=True, pause=0.005)
        log.info("desktop_type", app=spec.name, chars=len(text))
        time.sleep(0.2)
        return self._snapshot(app)

    def _press_key(self, app: str, key: str) -> WindowSnapshot:
        from pywinauto.keyboard import send_keys

        spec, info, window = self._find(app)
        key = normalize_key(key)
        if key not in spec.keys:
            raise DesktopError(f"'{key}' isn't allowed in {spec.label}.")
        window.set_focus()
        _require_foreground(info.handle)
        send_keys(to_send_keys_combo(key))
        log.info("desktop_key", app=spec.name, key=key)
        time.sleep(0.3)
        return self._snapshot(app)

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


def _require_foreground(handle: int) -> None:
    import win32gui

    deadline = time.monotonic() + 1.0  # Windows needs a moment to switch windows
    while win32gui.GetForegroundWindow() != handle:
        if time.monotonic() > deadline:
            raise DesktopError("Another window took the keyboard focus, so ARTHUR stopped typing.")
        time.sleep(0.05)


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

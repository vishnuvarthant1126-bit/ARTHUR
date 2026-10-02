"""The file workspace: the ONLY way ARTHUR's tools touch files on your computer.

Every path goes through `Workspace.resolve()`, which turns it into a real,
absolute path (following ".." and links) and then checks it:

    inside an allowed folder?          no  -> refused
    under a system folder?             yes -> refused (C:\\Windows, Program Files, AppData...)
    hidden, a key, a secret, a vault?  yes -> refused (.ssh, id_rsa, *.pem, *.kdbx, .env ...)
    a network or device path?          yes -> refused (\\\\server\\share, \\\\?\\...)

Checking the *resolved* path matters: "Documents\\..\\AppData" looks like it is in
Documents until you resolve it. Searches never follow links or junctions, so a
link inside Documents can't lead the search somewhere else.
"""

import contextlib
import fnmatch
import os
import stat
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit
from urllib.request import url2pathname

from pydantic import BaseModel

from app.observability.logging import get_logger
from app.rag.ingestion import SUPPORTED_TYPES, DocumentError, extract

log = get_logger(__name__)

# Folder names that are never entered (case-insensitive).
BLOCKED_DIR_NAMES = {
    ".git", ".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker", ".config",
    "node_modules", "__pycache__", ".venv", "venv", "appdata", "$recycle.bin",
    "system volume information",
}  # fmt: skip
# File names that are never read or written, even inside allowed folders.
BLOCKED_FILE_PATTERNS = [
    ".env", ".env.*", "*.key", "*.pem", "*.pfx", "*.p12", "*.crt", "*.cer", "id_rsa*",
    "id_ed25519*", "id_ecdsa*", "*.kdbx", "*.kdb", "*.ovpn", "wallet.dat", "*credential*",
    "*password*", "*passwd*", "*secret*", "*.lnk", "desktop.ini", "ntuser.dat*",
]  # fmt: skip
# Plain-text formats read as-is (documents like PDF/DOCX go through the RAG extractors).
TEXT_EXTENSIONS = {
    ".txt", ".md", ".log", ".json", ".yaml", ".yml", ".xml", ".html", ".htm", ".py", ".js",
    ".ts", ".java", ".c", ".cpp", ".h", ".cs", ".ino", ".sql", ".ini", ".toml", ".drawio",
}  # fmt: skip
READABLE_EXTENSIONS = TEXT_EXTENSIONS | set(SUPPORTED_TYPES)
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}  # -> describe_image
WRITABLE_EXTENSIONS = {".md", ".txt", ".csv"}


# The same idea in a Linux container (Phase 23). /app is ARTHUR itself: code and private data.
LINUX_SYSTEM_ROOTS = (
    "/app", "/bin", "/boot", "/dev", "/etc", "/lib", "/proc", "/root", "/run", "/sbin",
    "/sys", "/usr", "/var",
)  # fmt: skip


def _system_roots(windows: bool | None = None) -> list[Path]:
    if not (os.name == "nt" if windows is None else windows):
        return [Path(c).resolve() for c in LINUX_SYSTEM_ROOTS]
    candidates = [
        os.environ.get("SYSTEMROOT", r"C:\Windows"),
        os.environ.get("PROGRAMFILES", r"C:\Program Files"),
        os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        os.environ.get("PROGRAMDATA", r"C:\ProgramData"),
        os.environ.get("APPDATA", ""),
        os.environ.get("LOCALAPPDATA", ""),
    ]
    return [Path(c).resolve() for c in candidates if c]


class FileAccessError(Exception):
    """A refused or failed file operation, safe to show the user."""


class FileInfo(BaseModel):
    path: str
    name: str
    size_kb: float
    modified: str  # ISO date-time, local time


class FolderEntry(BaseModel):
    name: str
    is_folder: bool
    size_kb: float | None = None


class Workspace:
    def __init__(
        self,
        roots: list[Path],
        save_dir: Path,
        *,
        max_read_bytes: int = 20 * 1024 * 1024,
        max_write_bytes: int = 1024 * 1024,
        max_scan: int = 100_000,
        scan_seconds: float = 8.0,
        system_roots: list[Path] | None = None,  # tests only; the app always uses the default
    ) -> None:
        # ARTHUR's own reports folder is created if missing (on a fresh computer nothing
        # exists yet, and "save this" would fail with "folder doesn't exist").
        with contextlib.suppress(OSError):
            save_dir.mkdir(parents=True, exist_ok=True)
        self.roots = [r.resolve() for r in roots if r.exists()]
        missing = [str(r) for r in roots if not r.exists()]
        if missing:
            log.warning("workspace_roots_missing", roots=missing)
        self.save_dir = save_dir.resolve()
        self.max_read_bytes = max_read_bytes
        self.max_write_bytes = max_write_bytes
        self.max_scan = max_scan
        self.scan_seconds = scan_seconds
        self._system_roots = system_roots if system_roots is not None else _system_roots()

    # ---------- the gatekeeper ----------

    def resolve(self, raw: str, *, must_exist: bool = True) -> Path:
        """Turn user/LLM input into a real path inside the workspace, or raise."""
        text = raw.strip().strip('"').strip("'")
        if not text or "\x00" in text:
            raise FileAccessError("Empty or invalid path.")
        if text.lower().startswith("file:"):
            text = _path_from_file_url(text)  # models sometimes pass file:///C:/... links
        if text.startswith(("\\\\", "//")):
            raise FileAccessError("Network and device paths are not allowed.")
        if ":" in text[2:]:
            raise FileAccessError("Invalid path (':' is only allowed after the drive letter).")

        path = Path(text).expanduser()
        if not path.is_absolute():
            # A bare name or relative path: look for it inside each allowed folder.
            candidates = [root / path for root in self.roots]
            existing = [c for c in candidates if c.exists()]
            path = existing[0] if existing else (candidates[0] if candidates else path)
        real = path.resolve()  # follows ".." and links: we check where it REALLY points

        if not self.is_allowed(real):
            raise FileAccessError(
                "That location is outside the folders ARTHUR may use: "
                + "; ".join(str(r) for r in self.roots)
            )
        if must_exist and not real.exists():
            raise FileAccessError(f"Not found: {real}")
        return real

    def is_allowed(self, real: Path) -> bool:
        if not any(_inside(real, root) for root in self.roots):
            return False
        if any(_inside(real, system) for system in self._system_roots):
            return False
        # Every folder on the way from the root, and the final name, must be allowed.
        root = next(r for r in self.roots if _inside(real, r))
        for part in real.relative_to(root).parts:
            if part.startswith(".") or part.lower() in BLOCKED_DIR_NAMES or is_blocked_name(part):
                return False
        try:
            if real.exists() and _hidden(real.stat()):  # Windows "hidden"/"system" attribute
                return False
        except OSError:
            return False
        return True

    # ---------- operations ----------

    def find(
        self, query: str, extensions: list[str] | None = None, limit: int = 10
    ) -> tuple[list[FileInfo], bool]:
        """Files whose NAME contains every word of `query`, newest first.
        Returns (results, stopped_early)."""
        words = [w.lower() for w in query.replace("_", " ").replace("-", " ").split() if w]
        wanted = {("." + e.lower().lstrip(".")) for e in extensions or []}
        found: list[tuple[float, Path, os.stat_result]] = []
        scanned, start, stopped = 0, time.monotonic(), False

        for root in self.roots:
            for folder, dirs, files in os.walk(root, followlinks=False):
                # Prune in place: never descend into blocked, hidden or linked folders.
                dirs[:] = [
                    d for d in dirs
                    if d.lower() not in BLOCKED_DIR_NAMES and not d.startswith(".")
                    and not _is_link_or_hidden(Path(folder) / d)
                ]  # fmt: skip
                for name in files:
                    scanned += 1
                    if scanned > self.max_scan or time.monotonic() - start > self.scan_seconds:
                        stopped = True
                        break
                    path = Path(folder) / name
                    lower = name.lower()
                    if wanted and path.suffix.lower() not in wanted:
                        continue
                    if not all(w in lower.replace("_", " ").replace("-", " ") for w in words):
                        continue
                    if is_blocked_name(name) or name.startswith("."):
                        continue
                    try:
                        info = path.stat()
                    except OSError:
                        continue
                    if _hidden(info):
                        continue
                    found.append((info.st_mtime, path, info))
                if stopped:
                    break
            if stopped:
                break

        found.sort(key=lambda item: item[0], reverse=True)  # newest first: "my LATEST resume"
        unique = list({str(p).lower(): (p, i) for _, p, i in found}.values())[:limit]
        log.info("files_found", scanned=scanned, matches=len(found), stopped_early=stopped)
        return [_file_info(p, i) for p, i in unique], stopped

    def list_folder(self, raw: str, limit: int = 100) -> tuple[Path, list[FolderEntry]]:
        folder = self.resolve(raw)
        if not folder.is_dir():
            raise FileAccessError(f"Not a folder: {folder}")
        entries = []
        for child in sorted(folder.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            if len(entries) >= limit:
                break
            if not self.is_allowed(child.resolve()) or _is_link_or_hidden(child):
                continue
            size = None if child.is_dir() else round(child.stat().st_size / 1024, 1)
            entries.append(FolderEntry(name=child.name, is_folder=child.is_dir(), size_kb=size))
        return folder, entries

    def read(self, raw: str, max_chars: int = 6000) -> dict:
        path = self.resolve(raw)
        if not path.is_file():
            raise FileAccessError(f"Not a file: {path}")
        extension = path.suffix.lower()
        if extension in IMAGE_EXTENSIONS:
            raise FileAccessError("That's a picture - use describe_image to look at it.")
        if extension not in READABLE_EXTENSIONS:
            raise FileAccessError(
                f"ARTHUR can't read '{extension}' files. Readable: "
                + ", ".join(sorted(READABLE_EXTENSIONS))
            )
        size = path.stat().st_size
        if size > self.max_read_bytes:
            raise FileAccessError(f"File too large to read ({size / 1024 / 1024:.1f} MB).")
        data = path.read_bytes()
        if extension in SUPPORTED_TYPES:  # PDF, DOCX, TXT, MD, CSV: the RAG extractors
            try:
                pages = extract(data, extension)
            except DocumentError as exc:
                raise FileAccessError(str(exc)) from exc
            text = "\n\n".join(f"[{p.label}] {p.text}" if p.label else p.text for p in pages)
            page_count = len(pages)
        else:
            text = data.decode("utf-8", errors="replace")
            page_count = None
        return {
            "path": str(path),
            "pages": page_count,
            "characters": len(text),
            "truncated": len(text) > max_chars,
            "text": text[:max_chars],
        }

    def plan_save(self, raw: str, content: str, *, overwrite: bool = False) -> Path:
        """Check a save without writing (used for the confirmation preview too)."""
        name = raw.strip()
        target = Path(name)
        if not target.is_absolute() and len(target.parts) == 1:
            target = self.save_dir / target  # plain file name -> the reports folder
        target = self.resolve(str(target), must_exist=False)
        if target.suffix.lower() not in WRITABLE_EXTENSIONS:
            raise FileAccessError("ARTHUR only saves .md, .txt or .csv files.")
        if len(content.encode("utf-8")) > self.max_write_bytes:
            raise FileAccessError("Content too large to save (max 1 MB).")
        if target.exists() and not overwrite:
            raise FileAccessError(f"{target.name} already exists. Choose another name.")
        if not target.parent.exists():
            raise FileAccessError(f"Folder doesn't exist: {target.parent}")
        return target

    def save(self, raw: str, content: str, *, overwrite: bool = False) -> Path:
        target = self.plan_save(raw, content, overwrite=overwrite)
        target.write_text(content, encoding="utf-8")
        log.info("file_saved", path=str(target), bytes=len(content.encode("utf-8")))
        return target


# ---------- helpers ----------


def _path_from_file_url(url: str) -> str:
    """file:///C:/Users/x/a.pdf (possibly %-encoded) -> C:\\Users\\x\\a.pdf.
    The result still goes through every normal check afterwards."""
    parts = urlsplit(url)
    if parts.netloc and parts.netloc.lower() != "localhost":
        raise FileAccessError("Network file links (file://server/...) are not allowed.")
    return url2pathname(unquote(parts.path))


def _inside(path: Path, root: Path) -> bool:
    # Windows paths are case-insensitive: compare normalised strings.
    p, r = os.path.normcase(str(path)), os.path.normcase(str(root))
    return p == r or p.startswith(r.rstrip("\\/") + os.sep)


def is_blocked_name(name: str) -> bool:
    lower = name.lower()
    return any(fnmatch.fnmatch(lower, pattern) for pattern in BLOCKED_FILE_PATTERNS)


def _hidden(info: os.stat_result) -> bool:
    attributes = getattr(info, "st_file_attributes", 0)  # Windows only
    return bool(attributes & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))


def _is_link_or_hidden(path: Path) -> bool:
    try:
        if path.is_symlink() or path.is_junction():
            return True
        return _hidden(path.lstat())
    except OSError:
        return True


def _file_info(path: Path, info: os.stat_result) -> FileInfo:
    modified = datetime.fromtimestamp(info.st_mtime, tz=UTC).astimezone()
    return FileInfo(
        path=str(path),
        name=path.name,
        size_kb=round(info.st_size / 1024, 1),
        modified=modified.isoformat(timespec="minutes"),
    )

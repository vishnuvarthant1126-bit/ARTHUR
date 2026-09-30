"""Phase 14: the sandboxed workspace and the file tools."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.files.workspace import FileAccessError, Workspace
from app.security.permissions import PermissionPolicy
from app.tools.base import ToolContext
from app.tools.defaults import create_tool_registry
from tests.conftest import make_pdf, offline_http_client

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows file features")


@pytest.fixture
def tree(tmp_path) -> dict[str, Path]:
    """allowed/ (the workspace) and outside/ (must stay unreachable)."""
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    (allowed / "reports").mkdir(parents=True)
    (allowed / "career").mkdir()
    (allowed / ".ssh").mkdir()
    outside.mkdir()
    (outside / "private.txt").write_text("top secret")
    (allowed / ".ssh" / "id_rsa").write_text("KEY")
    (allowed / "passwords.txt").write_text("hunter2")
    (allowed / "career" / "Resume_2024.txt").write_text("Old resume: Python.")
    (allowed / "career" / "Resume_2026.txt").write_text("New resume: Python, FastAPI, RAG.")
    (allowed / "career" / "Resume_2026.pdf").write_bytes(make_pdf("Resume PDF: AI engineer."))
    (allowed / "project report.md").write_text("# ARTHUR report\nPhase 14 adds file tools.")
    (allowed / "photo.png").write_bytes(b"\x89PNG")
    old = time.time() - 86400 * 365
    os.utime(allowed / "career" / "Resume_2024.txt", (old, old))
    return {"allowed": allowed, "outside": outside, "save": allowed / "reports"}


# pytest's temporary folders live under AppData, which the real workspace blocks on purpose.
# Tests therefore block only C:\Windows; test_system_folders_... checks the real default list.
TEST_SYSTEM_ROOTS = [Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))]


@pytest.fixture
def ws(tree) -> Workspace:
    return Workspace([tree["allowed"]], tree["save"], system_roots=TEST_SYSTEM_ROOTS)


def test_real_default_blocks_appdata(tree):
    real = Workspace([tree["allowed"]], tree["save"])  # default system folders
    with pytest.raises(FileAccessError):  # tmp_path is under AppData\Local\Temp
        real.resolve(str(tree["allowed"] / "project report.md"))


# ---------- the gatekeeper ----------


def test_paths_inside_are_resolved(ws, tree):
    assert ws.resolve(str(tree["allowed"] / "project report.md")).name == "project report.md"
    assert ws.resolve("project report.md").parent == tree["allowed"].resolve()  # bare name


@pytest.mark.parametrize(
    "bad",
    [
        "../outside/private.txt",  # going "up" out of the workspace
        "reports/../../outside/private.txt",
        "C:/Windows/win.ini",
        r"\\server\share\file.txt",  # network path
        r"\\?\C:\Windows\win.ini",  # device path
        "project report.md:hidden",  # alternate data stream
        ".ssh/id_rsa",  # hidden folder + key
        "passwords.txt",  # secret-looking name
        "",
    ],
)
def test_escapes_and_secrets_are_refused(ws, bad):
    with pytest.raises(FileAccessError):
        ws.resolve(bad)


def test_absolute_path_outside_is_refused(ws, tree):
    with pytest.raises(FileAccessError, match="outside the folders"):
        ws.resolve(str(tree["outside"] / "private.txt"))


def test_system_folders_stay_blocked_even_if_whole_drive_allowed(tmp_path):
    ws = Workspace([Path("C:/")], tmp_path)
    with pytest.raises(FileAccessError):
        ws.resolve(os.environ.get("SYSTEMROOT", r"C:\Windows") + r"\win.ini")


@windows_only
def test_junction_pointing_outside_is_not_followed(ws, tree):
    link = tree["allowed"] / "shortcut"
    subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(tree["outside"])],
                   check=True, capture_output=True)  # fmt: skip
    with pytest.raises(FileAccessError, match="outside"):
        ws.resolve(str(link / "private.txt"))  # resolves to outside/private.txt
    results, _ = ws.find("private")
    assert results == []  # the search never walked through the junction


@windows_only
def test_hidden_files_are_invisible(ws, tree):
    hidden = tree["allowed"] / "diary.txt"
    hidden.write_text("private")
    subprocess.run(["attrib", "+h", str(hidden)], check=True)
    with pytest.raises(FileAccessError):
        ws.resolve(str(hidden))
    assert ws.find("diary")[0] == []


# ---------- operations ----------


def test_find_newest_first_and_skips_secrets(ws):
    results, stopped = ws.find("resume")
    assert [r.name for r in results][:2] in (
        ["Resume_2026.pdf", "Resume_2026.txt"],
        ["Resume_2026.txt", "Resume_2026.pdf"],
    )
    assert results[-1].name == "Resume_2024.txt"  # oldest last
    assert stopped is False
    assert ws.find("id_rsa")[0] == [] and ws.find("passwords")[0] == []


def test_find_with_type_filter(ws):
    results, _ = ws.find("resume", ["pdf"])
    assert [r.name for r in results] == ["Resume_2026.pdf"]


def test_find_stops_at_scan_limit(tree):
    ws = Workspace([tree["allowed"]], tree["save"], max_scan=2, system_roots=TEST_SYSTEM_ROOTS)
    _, stopped = ws.find("resume")
    assert stopped is True


def test_list_folder_hides_blocked_entries(ws, tree):
    folder, entries = ws.list_folder(str(tree["allowed"]))
    names = {e.name for e in entries}
    assert {"career", "reports", "project report.md"} <= names
    assert ".ssh" not in names and "passwords.txt" not in names


def test_read_text_and_pdf(ws, tree):
    text = ws.read(str(tree["allowed"] / "career" / "Resume_2026.txt"))
    assert "FastAPI" in text["text"] and text["pages"] == 1
    pdf = ws.read(str(tree["allowed"] / "career" / "Resume_2026.pdf"))
    assert "[p. 1]" in pdf["text"] and "AI engineer" in pdf["text"]


def test_read_refuses_binary_and_huge_files(ws, tree):
    with pytest.raises(FileAccessError, match="can't read '.png'"):
        ws.read(str(tree["allowed"] / "photo.png"))
    ws.max_read_bytes = 10
    with pytest.raises(FileAccessError, match="too large"):
        ws.read(str(tree["allowed"] / "project report.md"))


def test_save_rules(ws, tree):
    saved = ws.save("notes.md", "# Notes")
    assert saved == (tree["save"] / "notes.md").resolve() and saved.read_text() == "# Notes"
    with pytest.raises(FileAccessError, match="already exists"):
        ws.save("notes.md", "again")
    assert ws.save("notes.md", "v2", overwrite=True).read_text() == "v2"
    with pytest.raises(FileAccessError, match=r"\.md, \.txt or \.csv"):
        ws.save("script.bat", "del *.*")
    with pytest.raises(FileAccessError, match="outside"):
        ws.save(str(tree["outside"] / "x.md"), "nope")


# ---------- tools and permissions ----------


@pytest.fixture
def registry(ws):
    return create_tool_registry(
        policy=PermissionPolicy(), audit=None, http_client=offline_http_client(),
        memory=None, workspace=ws,
    )  # fmt: skip


async def test_find_and_read_tools(registry):
    found = await registry.execute("find_files", {"query": "resume", "file_types": ["txt"]})
    assert found.ok and found.output["results"][0]["name"] == "Resume_2026.txt"
    path = found.output["results"][0]["path"]

    read = await registry.execute("read_file", {"path": path})
    assert read.ok and "FastAPI" in read.output["text"]
    assert "never instructions" in read.output["note"]


async def test_tools_forgive_small_model_habits(registry, tree):
    """Measured with qwen3: file:/// links as paths, max_chars=100000, a wrong type guess."""
    resume = (tree["allowed"] / "career" / "Resume_2026.txt").resolve()
    url = "file:///" + str(resume).replace("\\", "%5C").replace(":", "%3A")

    read = await registry.execute("read_file", {"path": url, "max_chars": 100000})
    assert read.ok and "FastAPI" in read.output["text"]

    found = await registry.execute("find_files", {"query": "project report", "file_types": ["pdf"]})
    assert found.output["results"][0]["name"] == "project report.md"
    assert "No pdf files matched" in found.output["note"]


async def test_network_file_links_are_refused(registry):
    result = await registry.execute("read_file", {"path": "file://evil-server/share/x.txt"})
    assert result.status == "error" and "Network file links" in result.error


async def test_read_tool_refuses_outside_path(registry, tree):
    result = await registry.execute("read_file", {"path": str(tree["outside"] / "private.txt")})
    assert result.status == "error" and "outside" in result.error


async def test_save_needs_confirmation_with_a_clear_preview(registry, tree):
    args = {"filename": "ai_roles.md", "content": "# AI roles\n- ML engineer"}

    first = await registry.execute("save_file", args)
    assert first.status == "needs_confirmation"
    assert first.preview.startswith("Save ai_roles.md (")
    assert str(tree["save"].resolve()) in first.preview
    assert not (tree["save"] / "ai_roles.md").exists()  # nothing written yet

    second = await registry.execute("save_file", args, ToolContext(confirmed=True))
    assert second.ok and (tree["save"] / "ai_roles.md").read_text().startswith("# AI roles")


def test_file_tools_are_available_to_the_agent():
    from app.agent.orchestrator import AGENT_TOOLS

    assert {"find_files", "list_folder", "read_file", "save_file"} <= AGENT_TOOLS

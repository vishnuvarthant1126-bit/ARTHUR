"""Tool paths the coverage report showed as never executed: the one-line summaries shown
in the UI, error paths, and the memory tools when called through the registry."""

import os
from pathlib import Path

import pytest

from app.files.workspace import Workspace
from app.security.permissions import PermissionPolicy
from app.tools.base import ToolContext
from app.tools.file_tools import FindFilesTool, ListFolderTool, ReadFileTool, SaveFileTool
from app.tools.memory_tools import DeleteMemoryTool, SaveMemoryTool, SearchMemoryTool
from app.tools.registry import ToolRegistry
from tests.conftest import make_memory

SYSTEM_ROOTS = [Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))]


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    root = tmp_path / "allowed"
    (root / "notes").mkdir(parents=True)
    (root / "notes" / "plan.md").write_text("# Plan\nShip ARTHUR.", encoding="utf-8")
    (root / "report.txt").write_text("Quarterly report", encoding="utf-8")
    return Workspace([root], root / "reports", system_roots=SYSTEM_ROOTS)


@pytest.fixture
def files(workspace) -> ToolRegistry:
    registry = ToolRegistry(PermissionPolicy(), None)
    for tool in (FindFilesTool, ListFolderTool, ReadFileTool, SaveFileTool):
        registry.register(tool(workspace))
    return registry


# ---------- file tools ----------


async def test_list_folder_tool(files, workspace):
    listed = await files.execute("list_folder", {"path": str(workspace.roots[0])})
    assert listed.status == "ok"
    assert {e["name"] for e in listed.output["entries"]} == {"notes", "report.txt", "reports"}
    assert files.get("list_folder").summarize(listed.output).startswith("3 items in ")

    outside = await files.execute("list_folder", {"path": r"C:\Windows"})
    assert outside.status == "error"
    assert "outside the folders" in outside.error


async def test_summaries_shown_in_the_chat(files):
    found = await files.execute("find_files", {"query": "report"})
    assert files.get("find_files").summarize(found.output) == "report.txt"
    nothing = await files.execute("find_files", {"query": "zzz-no-such-file"})
    assert files.get("find_files").summarize(nothing.output) == "no matching files"

    read = await files.execute("read_file", {"path": "report.txt"})
    assert files.get("read_file").summarize(read.output) == "read 16 characters, 1 pages"

    missing = await files.execute("read_file", {"path": "nope.txt"})
    assert missing.status == "error"


async def test_save_that_cannot_work_never_asks_for_confirmation(files, workspace):
    """Found by reading uncovered code: the preview said "Save x.exe - but it will fail"
    and the user was still asked to confirm."""
    bad_type = await files.execute("save_file", {"filename": "tool.exe", "content": "x"})
    assert bad_type.status == "error"
    assert bad_type.preview is None

    outside = await files.execute("save_file", {"filename": r"C:\Windows\x.txt", "content": "x"})
    assert outside.status == "error"

    good = await files.execute("save_file", {"filename": "summary.md", "content": "# Hi"})
    assert good.status == "needs_confirmation"
    assert good.preview.startswith("Save summary.md (0.0 KB) in ")

    done = await files.execute(
        "save_file", {"filename": "summary.md", "content": "# Hi"}, ToolContext(confirmed=True)
    )
    assert done.status == "ok"
    assert files.get("save_file").summarize(done.output).startswith("saved ")
    assert (workspace.save_dir / "summary.md").read_text(encoding="utf-8") == "# Hi"

    again = await files.execute("save_file", {"filename": "summary.md", "content": "new"})
    assert again.status == "error"  # no silent overwrite
    overwrite = await files.execute(
        "save_file", {"filename": "summary.md", "content": "new", "overwrite": True}
    )
    assert overwrite.preview.startswith("Overwrite summary.md")


# ---------- memory tools ----------


@pytest.fixture
async def memory_tools():
    memory, _ = make_memory()
    registry = ToolRegistry(PermissionPolicy(), None)
    registry.register(SearchMemoryTool(memory, min_score=0.0))
    registry.register(SaveMemoryTool(memory))
    registry.register(DeleteMemoryTool(memory))
    return registry, memory


async def test_save_and_search_memory_tools(memory_tools):
    registry, memory = memory_tools

    saved = await registry.execute("save_memory", {"content": "The user's cat is called Miso."})
    assert saved.status == "ok" and saved.output["created"] is True

    secret = await registry.execute("save_memory", {"content": "My password is hunter2"})
    assert secret.status == "error"
    assert "secret" in secret.error
    assert await memory.count() == 1

    found = await registry.execute("search_memory", {"query": "cat"})
    assert found.output[0]["content"] == "The user's cat is called Miso."
    assert registry.get("search_memory").summarize(found.output) == "1 memory found"
    assert registry.get("search_memory").summarize([]) == "0 memories found"


async def test_delete_memory_tool_after_confirmation(memory_tools):
    registry, memory = memory_tools
    saved, _ = await memory.save_memory("The user likes tea.")

    asked = await registry.execute("delete_memory", {"memory_id": saved.id})
    assert asked.preview == 'Forget the memory "The user likes tea."'

    done = await registry.execute(
        "delete_memory", {"memory_id": saved.id}, ToolContext(confirmed=True)
    )
    assert done.output == {"deleted": saved.id}
    assert await memory.count() == 0

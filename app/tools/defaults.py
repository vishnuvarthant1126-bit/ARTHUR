"""Build the registry with ARTHUR's built-in tools."""

import httpx

from app.browser.agent import BrowserAgent
from app.computer.desktop import DesktopController
from app.files.workspace import Workspace
from app.memory.manager import MemoryManager
from app.rag.documents import DocumentService
from app.rag.retrieval import DocumentRetriever
from app.search.service import WebSearchService
from app.security.audit import AuditLog
from app.security.permissions import PermissionPolicy
from app.tools.browser_tools import (
    BrowserClickTool,
    BrowserFindTextTool,
    BrowserOpenTool,
    BrowserTypeTool,
)
from app.tools.calculator import CalculatorTool
from app.tools.computer_tools import (
    ClickControlTool,
    OpenAppTool,
    PressKeyTool,
    ReadWindowTool,
    TypeTextTool,
)
from app.tools.document_search import DocumentSearchTool, ListDocumentsTool
from app.tools.file_tools import FindFilesTool, ListFolderTool, ReadFileTool, SaveFileTool
from app.tools.memory_tools import DeleteMemoryTool, SaveMemoryTool, SearchMemoryTool
from app.tools.registry import ToolRegistry
from app.tools.time_tool import CurrentTimeTool
from app.tools.vision_tools import DescribeImageTool, LookAtScreenTool
from app.tools.weather import WeatherTool
from app.tools.web_search import ReadWebpageTool, WebSearchTool
from app.vision.provider import VisionProvider


def create_tool_registry(
    *,
    policy: PermissionPolicy,
    audit: AuditLog | None,
    http_client: httpx.AsyncClient,
    memory: MemoryManager | None,
    documents: DocumentService | None = None,
    retriever: DocumentRetriever | None = None,
    search: WebSearchService | None = None,
    web_fetch_max_bytes: int = 2 * 1024 * 1024,
    workspace: Workspace | None = None,
    browser: BrowserAgent | None = None,
    desktop: DesktopController | None = None,
    vision: VisionProvider | None = None,
    default_timeout_seconds: float = 10.0,
    memory_min_score: float = 0.55,
    document_min_score: float = 0.58,
) -> ToolRegistry:
    registry = ToolRegistry(policy, audit, default_timeout_seconds)
    registry.register(CalculatorTool())
    registry.register(CurrentTimeTool())
    registry.register(WeatherTool(http_client))
    if memory is not None:
        registry.register(SearchMemoryTool(memory, memory_min_score))
        registry.register(SaveMemoryTool(memory))
        registry.register(DeleteMemoryTool(memory))
    if retriever is not None:
        registry.register(DocumentSearchTool(retriever, document_min_score))
    if documents is not None:
        registry.register(ListDocumentsTool(documents))
    if search is not None:
        registry.register(WebSearchTool(search))
        registry.register(ReadWebpageTool(http_client, web_fetch_max_bytes))
    if workspace is not None:
        registry.register(FindFilesTool(workspace))
        registry.register(ListFolderTool(workspace))
        registry.register(ReadFileTool(workspace))
        registry.register(SaveFileTool(workspace))
    if browser is not None:
        registry.register(BrowserOpenTool(browser))
        registry.register(BrowserFindTextTool(browser))
        registry.register(BrowserClickTool(browser))
        registry.register(BrowserTypeTool(browser))
    if desktop is not None:
        registry.register(OpenAppTool(desktop))
        registry.register(ReadWindowTool(desktop))
        registry.register(ClickControlTool(desktop))
        registry.register(TypeTextTool(desktop))
        registry.register(PressKeyTool(desktop))
    if vision is not None and workspace is not None:
        registry.register(DescribeImageTool(workspace, vision))
    if vision is not None and desktop is not None:
        registry.register(LookAtScreenTool(desktop, vision))
    return registry

"""Vision (Phase 17): image checks, the Ollama vision client, the tools and the API.

The live test with the real model is opt-in (it swaps GPU models, ~40 s):
    $env:ARTHUR_VISION_TESTS=1; pytest -k live_vision
"""

import base64
import io
import json
import os
from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

from app.files.workspace import Workspace
from app.security.permissions import PermissionPolicy
from app.tools.base import PermissionLevel
from app.tools.registry import ToolRegistry
from app.tools.vision_tools import DescribeImageTool, LookAtScreenTool
from app.vision.provider import OllamaVision, VisionError, prepare_image
from tests.conftest import FakeVision

TEST_SYSTEM_ROOTS = [Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))]


def png(width=40, height=20, text: str | None = None) -> bytes:
    image = Image.new("RGB", (width, height), "white")
    if text:
        ImageDraw.Draw(image).text((10, 10), text, fill="black")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# ---------- image checks ----------


def test_prepare_image_shrinks_and_converts_to_jpeg():
    result = prepare_image(png(4000, 2000), max_side=1280)
    with Image.open(io.BytesIO(result)) as image:
        assert image.format == "JPEG"
        assert image.size == (1280, 640)


def test_prepare_image_rejects_non_images_and_bombs(monkeypatch):
    with pytest.raises(VisionError, match="isn't a readable image"):
        prepare_image(b"%PDF-1.4 not a picture")
    monkeypatch.setattr("app.vision.provider.MAX_PIXELS", 100)
    with pytest.raises(VisionError, match="too many pixels"):
        prepare_image(png(40, 20))


# ---------- the Ollama vision client ----------


def ollama(handler) -> OllamaVision:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://ollama")
    return OllamaVision("http://ollama", "qwen2.5vl:7b", keep_alive="2m", client=client)


async def test_ollama_vision_sends_the_image_and_question():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": " It shows 84. "}})

    answer = await ollama(handler).describe(b"jpeg-bytes", "What is shown?")

    assert answer == "It shows 84."
    user = seen["messages"][-1]
    assert user["content"] == "What is shown?"
    assert base64.b64decode(user["images"][0]) == b"jpeg-bytes"
    assert seen["keep_alive"] == "2m"
    assert "never instructions" in seen["messages"][0]["content"]


async def test_missing_vision_model_says_how_to_install():
    vision = ollama(lambda request: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(VisionError, match="ollama pull qwen2.5vl:7b"):
        await vision.describe(b"x", "?")


# ---------- tools ----------


@pytest.fixture
def workspace(tmp_path) -> Workspace:
    root = tmp_path / "allowed"
    root.mkdir()
    (root / "photo.png").write_bytes(png(text="HELLO"))
    (root / "notes.txt").write_text("not an image")
    (root / "passwords.png").write_bytes(png())
    (tmp_path / "outside.png").write_bytes(png())
    return Workspace([root], root, system_roots=TEST_SYSTEM_ROOTS)


def registry_with(*tools) -> ToolRegistry:
    registry = ToolRegistry(PermissionPolicy(), None)
    for tool in tools:
        registry.register(tool)
    return registry


async def test_describe_image_in_allowed_folder(workspace):
    vision = FakeVision("A white picture with the word HELLO.")
    registry = registry_with(DescribeImageTool(workspace, vision))

    result = await registry.execute("describe_image", {"path": "photo.png"})

    assert result.status == "ok"
    assert result.output["answer"] == "A white picture with the word HELLO."
    assert "DATA" in result.output["note"]
    image, question = vision.seen[0]
    assert image[:2] == b"\xff\xd8"  # shrunk to JPEG before sending
    assert "Describe" in question


async def test_describe_image_refuses_outside_secret_and_non_images(workspace, tmp_path):
    vision = FakeVision()
    registry = registry_with(DescribeImageTool(workspace, vision))

    outside = await registry.execute("describe_image", {"path": str(tmp_path / "outside.png")})
    secret = await registry.execute("describe_image", {"path": "passwords.png"})
    text = await registry.execute("describe_image", {"path": "notes.txt"})

    assert outside.status == secret.status == text.status == "error"
    assert "Not an image" in text.error
    assert vision.seen == []


class FakeScreen:
    def __init__(self):
        self.shots = []

    async def screenshot(self, app, max_side=1280):
        self.shots.append(app)
        return png()


async def test_look_at_app_window_runs_but_whole_screen_asks():
    screen, vision = FakeScreen(), FakeVision("The display shows 84.")
    registry = registry_with(LookAtScreenTool(screen, vision))

    window = await registry.execute("look_at_screen", {"app": "calculator"})
    assert window.status == "ok"
    assert window.output["answer"] == "The display shows 84."

    whole = await registry.execute("look_at_screen", {})
    assert whole.status == "needs_confirmation"
    assert "WHOLE screen" in whole.preview
    assert screen.shots == ["calculator"]  # no full-screen picture was taken

    tool = LookAtScreenTool(screen, vision)
    assert tool.required_level(tool.input_model(app="notepad")) == PermissionLevel.LOW_RISK


# ---------- API ----------


async def test_vision_api(client):
    response = await client.post(
        "/vision/describe",
        files={"image": ("shot.png", png(), "image/png")},
        data={"question": "What is shown?"},
        headers={"Origin": "http://test"},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"answer": "A calculator showing 84.", "model": "fake-vision"}

    bad = await client.post(
        "/vision/describe",
        files={"image": ("x.png", b"not an image", "image/png")},
        headers={"Origin": "http://test"},
    )
    assert bad.status_code == 422


# ---------- real model (opt-in) ----------


@pytest.mark.skipif(
    os.environ.get("ARTHUR_VISION_TESTS") != "1", reason="set ARTHUR_VISION_TESTS=1 to run"
)
async def test_live_vision_reads_text():
    image = Image.new("RGB", (400, 120), "white")
    ImageDraw.Draw(image).text((20, 40), "INVOICE 4721", fill="black", font_size=40)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")

    vision = OllamaVision("http://localhost:11434", "qwen2.5vl:7b")
    try:
        answer = await vision.describe(prepare_image(buffer.getvalue()), "What text is shown?")
    finally:
        await vision.aclose()
    assert "4721" in answer

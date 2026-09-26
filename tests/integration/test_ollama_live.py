"""Live test against the real local Ollama. Skipped automatically if Ollama isn't running."""

import pytest

from app.config.settings import get_settings
from app.llm.base import Message, Role
from app.llm.factory import create_llm_provider

pytestmark = pytest.mark.integration


@pytest.fixture
async def llm():
    provider = create_llm_provider(get_settings())
    if not await provider.health():
        await provider.aclose()
        pytest.skip("Ollama is not running")
    yield provider
    await provider.aclose()


async def test_real_ollama_answers(llm):
    result = await llm.generate(
        [Message(role=Role.USER, content="Reply with exactly the word: pong")], temperature=0
    )
    assert "pong" in result.content.lower()
    assert "<think>" not in result.content

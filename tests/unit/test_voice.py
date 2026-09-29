"""Phase 11+: voice - input checks, the transcribe endpoint (with a fake recogniser)."""

import pytest

from app.voice.speech_to_text import TranscriptionError, WhisperSTT
from tests.conftest import FakeSTT, wav_bytes

# ---------- WhisperSTT checks that run before the model is ever loaded ----------


@pytest.fixture
def stt() -> WhisperSTT:
    stt = WhisperSTT("tiny", max_seconds=5)
    stt._get_model = lambda: pytest.fail("the model must not be loaded for invalid input")
    return stt


async def test_empty_audio_is_rejected(stt):
    with pytest.raises(TranscriptionError, match="empty"):
        await stt.transcribe(b"")


async def test_unreadable_audio_is_rejected(stt):
    with pytest.raises(TranscriptionError, match="Couldn't read this audio"):
        await stt.transcribe(b"definitely not audio" * 100)


async def test_too_short_recording_is_rejected(stt):
    with pytest.raises(TranscriptionError, match="too short"):
        await stt.transcribe(wav_bytes(0.1))


async def test_too_long_recording_is_rejected(stt):
    with pytest.raises(TranscriptionError, match="too long"):
        await stt.transcribe(wav_bytes(6))


async def test_gpu_failure_while_running_falls_back_to_cpu(monkeypatch):
    """The real bug: CUDA loaded fine, then failed at run time (cublas64_12.dll missing)."""
    stt = WhisperSTT("tiny", device="cuda")
    loads = []
    calls = []

    def fake_load(device, compute):
        loads.append(device)
        stt._model = object()
        stt._active_device = device

    def fake_run(samples, language):
        calls.append(stt._active_device)
        if stt._active_device == "cuda":
            raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        return [], None

    monkeypatch.setattr(stt, "_load", fake_load)
    monkeypatch.setattr(stt, "_run_model", fake_run)
    stt._get_model()

    stt._run_model_with_fallback([0.0], None)

    assert loads == ["cuda", "cpu"]
    assert calls == ["cuda", "cpu"]


async def test_other_runtime_errors_are_not_hidden(monkeypatch):
    stt = WhisperSTT("tiny", device="cpu")
    stt._active_device = "cpu"

    def broken(samples, language):
        raise RuntimeError("something else broke")

    monkeypatch.setattr(stt, "_run_model", broken)
    with pytest.raises(RuntimeError, match="something else"):
        stt._run_model_with_fallback([0.0], None)


def test_logging_survives_characters_the_console_cannot_show(capsys):
    from app.observability.logging import configure_logging, get_logger

    configure_logging("INFO")
    get_logger("test").error("unicode_check", text="box ─│ emoji 🎤 arrow →")  # no exception


# ---------- POST /voice/transcribe ----------


async def test_transcribe_endpoint_returns_text(client):
    response = await client.post(
        "/voice/transcribe", files={"audio": ("recording.webm", b"fake-audio", "audio/webm")}
    )
    assert response.status_code == 200
    assert response.json()["text"] == "What is 25 times 50?"


async def test_transcription_problem_is_a_friendly_422(client):
    client._transport.app.state.stt = FakeSTT(error=TranscriptionError("I didn't hear any speech."))
    response = await client.post("/voice/transcribe", files={"audio": ("r.webm", b"x")})
    assert response.status_code == 422
    assert response.json()["detail"] == "I didn't hear any speech."


async def test_oversized_recording_is_rejected(client):
    big = b"0" * (10 * 1024 * 1024 + 1)
    response = await client.post("/voice/transcribe", files={"audio": ("r.webm", big)})
    assert response.status_code == 413


async def test_transcribe_from_another_website_is_blocked(client):
    response = await client.post(
        "/voice/transcribe",
        files={"audio": ("r.webm", b"x")},
        headers={"Origin": "https://evil.example"},
    )
    assert response.status_code == 403

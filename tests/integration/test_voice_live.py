"""Live voice test: Piper speaks a sentence, Whisper must transcribe it back.

Skipped when the Piper voice or faster-whisper isn't available. Uses the small
`base.en` Whisper model so it stays reasonably fast.
"""

import io
import wave

import pytest

from app.config.settings import get_settings
from app.voice.speech_to_text import TranscriptionError, WhisperSTT
from app.voice.text_to_speech import PiperTTS, prepare_for_speech
from tests.conftest import wav_bytes

pytestmark = pytest.mark.integration

VOICE = get_settings().resolve(get_settings().voices_path) / "en_US-lessac-medium.onnx"


@pytest.fixture(scope="module")
def stt() -> WhisperSTT:
    pytest.importorskip("faster_whisper")
    models = get_settings().resolve(get_settings().models_path) / "whisper"
    return WhisperSTT("base.en", download_root=str(models))


def speak(text: str) -> bytes:
    piper = pytest.importorskip("piper")
    if not VOICE.exists():
        pytest.skip("Piper voice not downloaded")
    voice = piper.PiperVoice.load(str(VOICE))
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        voice.synthesize_wav(text, wav)
    return buffer.getvalue()


async def test_spoken_sentence_is_transcribed(stt):
    transcript = await stt.transcribe(speak("What is twenty five times fifty?"))
    assert transcript.text.lower().replace(",", "").startswith("what is 25 times 50")
    assert transcript.confidence > 0.5


async def test_arthur_can_understand_his_own_voice(stt):
    """Round trip through the real TTS service: prepare_for_speech -> Piper -> Whisper."""
    if not VOICE.exists():
        pytest.skip("Piper voice not downloaded")
    tts = PiperTTS(VOICE.parent, default_voice="en_US-lessac-medium")
    audio = await tts.synthesize(prepare_for_speech("**Take an umbrella** [weather, p. 1]."))
    transcript = await stt.transcribe(audio)
    assert "umbrella" in transcript.text.lower()
    assert "weather" not in transcript.text.lower()  # the citation was not read aloud


async def test_silence_is_not_turned_into_words(stt):
    with pytest.raises(TranscriptionError, match="didn't hear any speech"):
        await stt.transcribe(wav_bytes(3))


async def test_a_beep_is_not_speech(stt):
    with pytest.raises(TranscriptionError):
        await stt.transcribe(wav_bytes(2, tone=True))

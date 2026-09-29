"""Phase 12: text-to-speech - cleaning text for speech, the endpoints (with a fake voice)."""

import pytest

from app.voice.text_to_speech import (
    PiperTTS,
    SpeechError,
    TextToSpeech,
    VoiceInfo,
    prepare_for_speech,
)


@pytest.mark.parametrize(
    ("text", "spoken"),
    [
        ("**Bold** and *italic*", "Bold and italic."),
        ("You need 120 credits [handbook.pdf, p. 2].", "You need 120 credits."),
        ("See [python.org](https://www.python.org/) for details", "See python.org for details."),
        ("Visit https://example.com/page now", "Visit the link on screen now."),
        ("482 × 29 = 13,978", "482 times 29 = 13,978."),
        ("It's 28 °C today ☔", "It's 28 degrees Celsius today."),
        ("## Summary\n- first point\n- second point", "Summary. first point. second point."),
        ("Here:\n```python\nprint('hi')\n```\nDone.", "Here:. (code shown on screen). Done."),
        ("[student_handbook.pdf, p. 5]", ""),
        ("Take an umbrella [weather, p. 1].", "Take an umbrella."),
        ("Grades are fine [grades.csv, rows 2-26].", "Grades are fine."),
        ("Keep [brackets] that aren't citations", "Keep [brackets] that aren't citations."),
    ],
)
def test_prepare_for_speech(text, spoken):
    assert prepare_for_speech(text) == spoken


# ---------- PiperTTS input checks (no voice model needed) ----------


@pytest.fixture
def tts(tmp_path) -> PiperTTS:
    (tmp_path / "en_GB-alan-medium.onnx").write_bytes(b"model")
    (tmp_path / "en_GB-alan-medium.onnx.json").write_text("{}")
    (tmp_path / "broken-voice.onnx").write_bytes(b"no config file next to me")
    return PiperTTS(tmp_path, default_voice="en_GB-alan-medium")


def test_voices_are_discovered_with_their_config(tts):
    assert [v.id for v in tts.voices()] == ["en_GB-alan-medium"]
    voice = tts.voices()[0]
    assert (voice.language, voice.name, voice.quality) == ("en_GB", "alan", "medium")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"text": "   "}, "Nothing to say"),
        ({"text": "x" * 1001}, "too long"),
        ({"text": "hi", "speed": 3.0}, "Speed must be"),
        ({"text": "hi", "voice": "de_DE-nobody-low"}, "not installed"),
    ],
)
async def test_invalid_speech_requests(tts, kwargs, message):
    text = kwargs.pop("text")
    with pytest.raises(SpeechError, match=message):
        await tts.synthesize(text, **kwargs)


# ---------- endpoints ----------


class FakeTTS(TextToSpeech):
    default_voice = "en_GB-alan-medium"

    def __init__(self) -> None:
        self.spoken: list[tuple[str, str | None, float]] = []

    def voices(self) -> list[VoiceInfo]:
        return [VoiceInfo(id="en_GB-alan-medium", language="en_GB", name="alan", quality="medium")]

    async def synthesize(self, text, *, voice=None, speed=1.0) -> bytes:
        self.spoken.append((text, voice, speed))
        return b"RIFF....WAVEfake"


@pytest.fixture
def fake_tts(client) -> FakeTTS:
    tts = FakeTTS()
    client._transport.app.state.tts = tts
    return tts


async def test_speak_returns_wav_of_cleaned_text(client, fake_tts):
    response = await client.post(
        "/voice/speak", json={"text": "**Yes** [handbook.pdf, p. 2]", "speed": 1.2}
    )
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert fake_tts.spoken == [("Yes.", None, 1.2)]


async def test_nothing_speakable_returns_204(client, fake_tts):
    response = await client.post("/voice/speak", json={"text": "[handbook.pdf, p. 2]"})
    assert response.status_code == 204
    assert fake_tts.spoken == []


@pytest.mark.parametrize(
    "body",
    [{"text": ""}, {"text": "hi", "speed": 5}, {"text": "hi", "voice": "../../etc/passwd"}],
)
async def test_speak_validates_input(client, fake_tts, body):
    assert (await client.post("/voice/speak", json=body)).status_code == 422


async def test_voices_endpoint(client, fake_tts):
    body = (await client.get("/voice/voices")).json()
    assert body["default"] == "en_GB-alan-medium"
    assert body["voices"][0]["name"] == "alan"

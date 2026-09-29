"""Phase 13: wake-word detection and the /voice/wake endpoint."""

import pytest

from app.voice.speech_to_text import TranscriptionError
from app.voice.wake_word import detect_wake_phrase
from tests.conftest import FakeSTT


@pytest.mark.parametrize(
    ("heard", "command"),
    [
        ("Hey Arthur, what's the weather in Singapore?", "what's the weather in Singapore"),
        ("Hey, Arthur. What is 25 times 50?", "What is 25 times 50"),
        ("OK Arthur, remind me to call mum", "remind me to call mum"),
        ("Arthur, set a timer", "set a timer"),
        ("Hey Arther, open my documents", "open my documents"),  # small mishearing
        ("hi artur what time is it", "what time is it"),
        ("Hey Arthur.", None),
        ("Arthur?", None),
        ("Hey offer!", None),  # measured: noisy "Hey Arthur" heard as "Hey offer"
    ],
)
def test_wake_phrases_are_detected(heard, command):
    result = detect_wake_phrase(heard)
    assert result.wake is True
    assert result.command == command


@pytest.mark.parametrize(
    "heard",
    [
        "I really like that author.",  # sounds similar, but isn't the name
        "Hey author, nice book",
        "Did you talk to Arthur yesterday?",  # the name, but not at the start
        "Tell me about King Arthur",
        "Hey there, how are you?",
        "Archer is a good show",
        "Father, can you help?",
        "Offer them a discount",  # "offer" only counts right after a greeting
        "",
        "...",
    ],
)
def test_other_speech_is_ignored(heard):
    assert detect_wake_phrase(heard).wake is False


# ---------- endpoint ----------


def use_stt(client, *, quick: FakeSTT, accurate: FakeSTT | None = None):
    client._transport.app.state.wake_stt = quick
    if accurate:
        client._transport.app.state.stt = accurate


async def post_clip(client):
    return await client.post(
        "/voice/wake", files={"audio": ("clip.wav", b"RIFF-fake", "audio/wav")}
    )


async def test_wake_with_command_uses_the_accurate_transcript(client):
    quick = FakeSTT("Hey Arthur, what's the wether in Singapur")  # fast model, a bit sloppy
    accurate = FakeSTT("Hey Arthur, what's the weather in Singapore?")
    use_stt(client, quick=quick, accurate=accurate)

    body = (await post_clip(client)).json()

    assert body == {
        "wake": True,
        "command": "what's the weather in Singapore",
        "heard": "Hey Arthur, what's the weather in Singapore?",
    }
    assert len(accurate.received) == 1


async def test_wake_without_command_skips_the_slow_model(client):
    accurate = FakeSTT("unused")
    use_stt(client, quick=FakeSTT("Hey Arthur."), accurate=accurate)

    body = (await post_clip(client)).json()

    assert body["wake"] is True and body["command"] is None
    assert accurate.received == []  # no need for a second, slower pass


async def test_ordinary_speech_is_not_a_wake_word(client):
    accurate = FakeSTT("unused")
    use_stt(client, quick=FakeSTT("I was just reading about an author"), accurate=accurate)

    body = (await post_clip(client)).json()

    assert body["wake"] is False
    assert accurate.received == []


async def test_noise_or_silence_is_simply_no_wake(client):
    use_stt(client, quick=FakeSTT(error=TranscriptionError("I didn't hear any speech.")))
    response = await post_clip(client)
    assert response.status_code == 200
    assert response.json()["wake"] is False


async def test_wake_clip_size_limit(client):
    use_stt(client, quick=FakeSTT())
    big = b"0" * (2 * 1024 * 1024 + 1)
    response = await client.post("/voice/wake", files={"audio": ("clip.wav", big)})
    assert response.status_code == 413

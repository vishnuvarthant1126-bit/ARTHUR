"""Voice endpoints: speech-to-text (Phase 11) and text-to-speech (Phase 12)."""

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, Field

from app.observability.logging import get_logger
from app.voice.speech_to_text import Transcript, TranscriptionError
from app.voice.text_to_speech import MAX_SPEECH_CHARS, SpeechError, VoiceInfo, prepare_for_speech
from app.voice.wake_word import WakeResult, detect_wake_phrase

router = APIRouter(prefix="/voice", tags=["voice"])
log = get_logger(__name__)

MAX_AUDIO_BYTES = 10 * 1024 * 1024  # 10 MB is several minutes of compressed speech


@router.post("/transcribe", response_model=Transcript)
async def transcribe(audio: UploadFile, request: Request) -> Transcript:
    stt = request.app.state.stt
    data = await audio.read(MAX_AUDIO_BYTES + 1)
    if len(data) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Recording too large (max 10 MB)."
        )
    try:
        return await stt.transcribe(data)
    except TranscriptionError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc


MAX_WAKE_CLIP_BYTES = 2 * 1024 * 1024  # a 12 s, 16 kHz mono WAV is ~0.4 MB


@router.post("/wake", response_model=WakeResult)
async def check_wake_word(audio: UploadFile, request: Request) -> WakeResult:
    """Did this short clip start with "Hey Arthur"? Clips are never stored or logged."""
    data = await audio.read(MAX_WAKE_CLIP_BYTES + 1)
    if len(data) > MAX_WAKE_CLIP_BYTES:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Clip too large.")
    try:
        quick = await request.app.state.wake_stt.transcribe(data)  # fast model: ~0.6 s
    except TranscriptionError:
        return WakeResult(wake=False)  # noise, a cough, silence: simply not a wake word
    result = detect_wake_phrase(quick.text)

    if result.wake and result.command:
        # "Hey Arthur, <command>": re-read the same clip with the accurate model, so the
        # command itself is transcribed as well as a normal voice message would be.
        try:
            accurate = detect_wake_phrase((await request.app.state.stt.transcribe(data)).text)
            if accurate.wake and accurate.command:
                result = accurate
        except TranscriptionError:
            pass
    log.info("wake_check", wake=result.wake, has_command=bool(result.command))
    return result


class VoiceList(BaseModel):
    default: str
    voices: list[VoiceInfo]


class SpeakRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_SPEECH_CHARS * 2)
    voice: str | None = Field(default=None, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")
    speed: float = Field(default=1.0, ge=0.5, le=2.0)


@router.get("/voices", response_model=VoiceList)
async def list_voices(request: Request) -> VoiceList:
    tts = request.app.state.tts
    return VoiceList(default=tts.default_voice, voices=tts.voices())


@router.post(
    "/speak",
    response_class=Response,
    responses={200: {"content": {"audio/wav": {}}, "description": "Spoken audio (WAV)"}},
)
async def speak(body: SpeakRequest, request: Request) -> Response:
    text = prepare_for_speech(body.text)
    if not text:
        return Response(
            status_code=status.HTTP_204_NO_CONTENT
        )  # nothing speakable (e.g. only a link)
    try:
        audio = await request.app.state.tts.synthesize(
            text[:MAX_SPEECH_CHARS], voice=body.voice, speed=body.speed
        )
    except SpeechError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    except Exception as exc:  # a broken voice model or audio library (Phase 28)
        log.exception("tts_failed")
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ARTHUR's voice isn't working right now - the answer is still shown as text.",
        ) from exc
    return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})

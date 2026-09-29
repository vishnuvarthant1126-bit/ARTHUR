"""Voice endpoints: speech-to-text (Phase 11) and text-to-speech (Phase 12)."""

from fastapi import APIRouter, HTTPException, Request, Response, UploadFile, status
from pydantic import BaseModel, Field

from app.voice.speech_to_text import Transcript, TranscriptionError
from app.voice.text_to_speech import MAX_SPEECH_CHARS, SpeechError, VoiceInfo, prepare_for_speech

router = APIRouter(prefix="/voice", tags=["voice"])

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
    return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})

"""Voice endpoints: speech-to-text now; text-to-speech in Phase 12."""

from fastapi import APIRouter, HTTPException, Request, UploadFile, status

from app.voice.speech_to_text import Transcript, TranscriptionError

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

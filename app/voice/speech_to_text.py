"""Speech-to-text (STT): turn a recording into text, locally with faster-whisper.

    audio bytes (webm/ogg/wav/mp3/m4a from the browser or a file)
      ─► limits: size and duration
      ─► decode to 16 kHz mono (PyAV, the ffmpeg libraries bundled with faster-whisper)
      ─► VAD: keep only the parts with speech (silence and noise are skipped)
      ─► Whisper model ─► text + language + confidence
      ─► nothing heard / very unsure ─► TranscriptionError with a friendly message

The model is loaded lazily on first use (loading takes a few seconds and ~0.5 GB RAM)
and shared. Transcription is CPU-heavy, so it runs in a worker thread, one at a time.
"""

import asyncio
import io
import math
import threading
import time
from abc import ABC, abstractmethod

from pydantic import BaseModel

from app.observability.logging import get_logger

log = get_logger(__name__)

SAMPLE_RATE = 16_000  # Whisper works on 16 kHz mono audio


class TranscriptionError(Exception):
    """A problem with the recording, safe to show the user."""


class Transcript(BaseModel):
    text: str
    language: str
    duration_seconds: float  # length of the recording
    speech_seconds: float  # how much of it was speech (after VAD)
    confidence: float  # 0..1, from Whisper's average log-probability
    processing_ms: float


class SpeechToText(ABC):
    @abstractmethod
    async def transcribe(self, audio: bytes, *, language: str | None = None) -> Transcript: ...


class WhisperSTT(SpeechToText):
    def __init__(
        self,
        model_size: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        *,
        language: str | None = "en",
        max_seconds: float = 60.0,
        min_confidence: float = 0.35,
        download_root: str | None = None,
    ) -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        # A fixed language skips Whisper's language detection (~1 s faster). None = auto.
        self.language = language
        self.max_seconds = max_seconds
        self.min_confidence = min_confidence
        self.download_root = download_root
        self._model = None
        self._load_lock = threading.Lock()
        self._run_lock = asyncio.Lock()  # one transcription at a time: they compete for CPU

    async def transcribe(self, audio: bytes, *, language: str | None = None) -> Transcript:
        if not audio:
            raise TranscriptionError("The recording is empty.")
        async with self._run_lock:
            return await asyncio.to_thread(self._transcribe, audio, language)

    def warm_up(self) -> None:
        """Load the model now instead of on the first request."""
        self._get_model()

    # ---------- runs in a worker thread ----------

    def _get_model(self):
        with self._load_lock:
            if self._model is None:
                device, compute = self.device, self.compute_type
                if device == "auto":
                    device, compute = _best_device()
                try:
                    self._load(device, compute)
                except Exception as exc:  # e.g. CUDA libraries missing -> fall back to CPU
                    if device == "cpu":
                        raise
                    log.warning("whisper_gpu_failed_using_cpu", error=str(exc))
                    self._load("cpu", "int8")
            return self._model

    def _load(self, device: str, compute: str) -> None:
        from faster_whisper import WhisperModel

        start = time.perf_counter()
        self._model = WhisperModel(
            self.model_size, device=device, compute_type=compute, download_root=self.download_root
        )
        self._active_device = device
        log.info(
            "whisper_loaded",
            model=self.model_size,
            device=device,
            compute_type=compute,
            load_ms=round((time.perf_counter() - start) * 1000),
        )

    def _run_model(self, samples, language: str | None):
        segments, info = self._get_model().transcribe(
            samples,
            language=language or self.language,
            beam_size=5,
            vad_filter=True,  # skip silence/noise: faster, and fewer invented words
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,  # stops repetition loops on noisy audio
        )
        return list(segments), info  # the model only really runs while we iterate

    def _run_model_with_fallback(self, samples, language: str | None):
        try:
            return self._run_model(samples, language)
        except RuntimeError as exc:
            # A GPU can load the model but fail when it runs (e.g. cublas64_12.dll missing).
            gpu_problem = any(k in str(exc).lower() for k in ("cuda", "cublas", "cudnn"))
            if not gpu_problem or getattr(self, "_active_device", "cpu") == "cpu":
                raise
            log.warning("whisper_gpu_failed_using_cpu", error=str(exc))
            with self._load_lock:
                self._load("cpu", "int8")
            return self._run_model(samples, language)

    def _transcribe(self, audio: bytes, language: str | None) -> Transcript:
        from faster_whisper.audio import decode_audio

        start = time.perf_counter()
        try:
            samples = decode_audio(io.BytesIO(audio), sampling_rate=SAMPLE_RATE)
        except Exception as exc:  # PyAV raises many different errors for bad input
            raise TranscriptionError(
                "Couldn't read this audio. Supported: WebM, Ogg, WAV, MP3, M4A."
            ) from exc

        duration = len(samples) / SAMPLE_RATE
        if duration < 0.3:
            raise TranscriptionError("The recording is too short. Hold the mic a bit longer.")
        if duration > self.max_seconds:
            raise TranscriptionError(
                f"The recording is too long ({duration:.0f} s). The limit is "
                f"{self.max_seconds:.0f} s."
            )

        segments, info = self._run_model_with_fallback(samples, language)
        segments = [s for s in segments if s.no_speech_prob < 0.6]
        text = " ".join(s.text.strip() for s in segments).strip()
        speech = sum(s.end - s.start for s in segments)
        confidence = (
            math.exp(sum(s.avg_logprob * (s.end - s.start) for s in segments) / speech)
            if speech > 0
            else 0.0
        )

        result = Transcript(
            text=text,
            language=info.language,
            duration_seconds=round(duration, 2),
            speech_seconds=round(speech, 2),
            confidence=round(confidence, 3),
            processing_ms=round((time.perf_counter() - start) * 1000, 1),
        )
        log.info(
            "transcribed",
            duration_s=result.duration_seconds,
            speech_s=result.speech_seconds,
            confidence=result.confidence,
            language=result.language,
            processing_ms=result.processing_ms,
            chars=len(text),
        )
        if not text:
            raise TranscriptionError("I didn't hear any speech. Try again a bit closer to the mic.")
        if confidence < self.min_confidence:
            raise TranscriptionError(
                "Sorry, I couldn't understand that clearly. Could you say it again?"
            )
        return result


def _best_device() -> tuple[str, str]:
    """GPU only if one exists AND NVIDIA's cuBLAS library can actually be loaded."""
    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() > 0 and _cublas_available():
            return "cuda", "float16"
    except Exception:
        pass
    return "cpu", "int8"


def _cublas_available() -> bool:
    import ctypes
    import sys

    name = "cublas64_12.dll" if sys.platform == "win32" else "libcublas.so.12"
    try:
        ctypes.CDLL(name)
        return True
    except OSError:
        return False

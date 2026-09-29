"""Text-to-speech (TTS): ARTHUR's voice, generated locally with Piper.

    reply text ─► prepare_for_speech(): drop Markdown, links, citations, code
               ─► Piper voice model (.onnx) ─► WAV audio ─► the browser plays it

The browser asks for one sentence at a time while the reply is still streaming,
so speaking starts after the first sentence instead of after the whole answer.

Voices are `.onnx` + `.onnx.json` pairs in data/voices/. Get more with:
    python -m piper.download_voices --download-dir data/voices en_GB-alan-medium
"""

import asyncio
import io
import re
import threading
import time
import wave
from abc import ABC, abstractmethod
from pathlib import Path

from pydantic import BaseModel

from app.observability.logging import get_logger

log = get_logger(__name__)

MAX_SPEECH_CHARS = 1000  # per request: the browser sends sentence-sized pieces


class SpeechError(Exception):
    """A TTS problem that is safe to show the user."""


class VoiceInfo(BaseModel):
    id: str  # e.g. "en_GB-alan-medium"
    language: str  # e.g. "en_GB"
    name: str  # e.g. "alan"
    quality: str  # x_low | low | medium | high


class TextToSpeech(ABC):
    default_voice: str

    @abstractmethod
    def voices(self) -> list[VoiceInfo]: ...

    @abstractmethod
    async def synthesize(self, text: str, *, voice: str | None = None, speed: float = 1.0) -> bytes:
        """Return WAV audio for `text` (already cleaned for speech)."""


class PiperTTS(TextToSpeech):
    def __init__(self, voices_dir: Path, default_voice: str = "en_GB-alan-medium") -> None:
        self.voices_dir = voices_dir
        self.default_voice = default_voice
        self._loaded: dict[str, object] = {}
        self._lock = threading.Lock()

    def voices(self) -> list[VoiceInfo]:
        found = []
        for model in sorted(self.voices_dir.glob("*.onnx")):
            if not model.with_suffix(".onnx.json").exists():
                continue  # a voice needs its config file too
            parts = model.stem.split("-")
            found.append(
                VoiceInfo(
                    id=model.stem,
                    language=parts[0],
                    name=parts[1] if len(parts) > 1 else model.stem,
                    quality=parts[2] if len(parts) > 2 else "",
                )
            )
        return found

    async def synthesize(self, text: str, *, voice: str | None = None, speed: float = 1.0) -> bytes:
        text = text.strip()
        if not text:
            raise SpeechError("Nothing to say.")
        if len(text) > MAX_SPEECH_CHARS:
            raise SpeechError(
                f"Text too long to speak at once (max {MAX_SPEECH_CHARS} characters)."
            )
        if not 0.5 <= speed <= 2.0:
            raise SpeechError("Speed must be between 0.5 and 2.0.")
        voice_id = voice or self.default_voice
        if voice_id not in {v.id for v in self.voices()}:
            raise SpeechError(f"Voice '{voice_id}' is not installed.")
        # Piper is CPU work: run it in a thread so the server stays responsive.
        return await asyncio.to_thread(self._synthesize, text, voice_id, speed)

    def warm_up(self) -> None:
        """Load the default voice now (~1.5 s), so the first spoken reply starts quickly."""
        if self.default_voice in {v.id for v in self.voices()}:
            self._voice(self.default_voice)

    def _voice(self, voice_id: str):
        with self._lock:
            if voice_id not in self._loaded:
                from piper import PiperVoice

                start = time.perf_counter()
                self._loaded[voice_id] = PiperVoice.load(str(self.voices_dir / f"{voice_id}.onnx"))
                log.info(
                    "tts_voice_loaded",
                    voice=voice_id,
                    load_ms=round((time.perf_counter() - start) * 1000),
                )
            return self._loaded[voice_id]

    def _synthesize(self, text: str, voice_id: str, speed: float) -> bytes:
        from piper import SynthesisConfig

        start = time.perf_counter()
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            # length_scale is "how long each sound lasts": 0.8 = faster, 1.25 = slower.
            config = SynthesisConfig(length_scale=1.0 / speed)
            self._voice(voice_id).synthesize_wav(text, wav, syn_config=config)
        audio = buffer.getvalue()
        log.info(
            "tts_synthesized",
            voice=voice_id,
            chars=len(text),
            kb=round(len(audio) / 1024),
            duration_ms=round((time.perf_counter() - start) * 1000),
        )
        return audio


# ---------- making text sound right when spoken ----------

_CODE_BLOCK = re.compile(r"```.*?```", re.DOTALL)
_MD_LINK = re.compile(r"\[([^\]]+)\]\((?:https?://)[^)]+\)")
# [handbook.pdf, p. 2], [notes.docx], [grades.csv, rows 2-26], [source, p. 4] ...
_CITATION = re.compile(
    r"\[[^\[\]]*(?:\.(?:pdf|docx|txt|md|csv)\b|,\s*(?:p\.|pp\.|page|rows?)\s*[\d\s-]+)[^\[\]]*\]",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://\S+")
_LATEX = re.compile(r"\$\$?([^$]+)\$\$?")
_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿️]")


def prepare_for_speech(text: str) -> str:
    """Turn a Markdown reply into something pleasant to listen to."""
    text = _CODE_BLOCK.sub(" (code shown on screen) ", text)
    text = _MD_LINK.sub(r"\1", text)  # [python.org](https://...) -> "python.org"
    text = _CITATION.sub("", text)  # [handbook.pdf, p. 2] -> ""
    text = _URL.sub("the link on screen", text)
    text = _LATEX.sub(r"\1", text)
    text = (
        text.replace("\\times", " times ").replace("×", " times ").replace("÷", " divided by ")
        .replace("°C", " degrees Celsius").replace("°F", " degrees Fahrenheit")
        .replace("&", " and ").replace("—", ", ").replace("–", " to ")
    )  # fmt: skip
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)  # headings
    text = re.sub(r"^\s*(?:[-*+]|\d+[.)])\s+", "", text, flags=re.MULTILINE)  # list markers
    text = re.sub(r"[*_`>|]+", "", text)  # bold, italics, code, quotes, table bars
    text = _EMOJI.sub("", text)
    text = re.sub(r"\s*\n\s*", ". ", text)  # line breaks become pauses
    text = re.sub(r"\.(\s*\.)+", ".", text)
    text = re.sub(r"\s{2,}", " ", text)
    return text.strip(" .") + ("." if text.strip(" .") else "")

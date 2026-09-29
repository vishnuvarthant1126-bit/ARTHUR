"""Measure wake-word accuracy: false negatives (missed "Hey Arthur") and false positives.

Piper speaks each phrase in every installed voice, optionally with background noise
added, and the same fast Whisper model + detector used by /voice/wake checks it.

    python scripts/evaluate_wake_word.py

Real voices and rooms differ - this is a baseline, not a guarantee.
"""

import asyncio
import io
import random
import struct
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from piper import PiperVoice  # noqa: E402

from app.config.settings import get_settings  # noqa: E402
from app.voice.speech_to_text import TranscriptionError, WhisperSTT  # noqa: E402
from app.voice.wake_word import detect_wake_phrase  # noqa: E402

SHOULD_WAKE = [
    "Hey Arthur, what's the weather in Singapore?",
    "Hey Arthur.",
    "OK Arthur, what is twenty five times fifty?",
    "Arthur, remind me about my meeting.",
    "Hello Arthur, search my documents for the handbook.",
]
SHOULD_NOT_WAKE = [
    "I really enjoyed that author's new book.",
    "Did you call Arthur yesterday?",
    "Tell me about King Arthur and the round table.",
    "Hey there, how are you doing today?",
    "The archer hit the target.",
    "Offer them a discount on the tickets.",
    "Other people might disagree.",
    "My father is coming home early.",
]


def speak(voice: PiperVoice, text: str, noise: float) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        voice.synthesize_wav(text, wav)
    if not noise:
        return buffer.getvalue()
    buffer.seek(0)
    with wave.open(buffer, "rb") as wav:
        params, frames = wav.getparams(), wav.readframes(wav.getnframes())
    samples = struct.unpack(f"<{len(frames) // 2}h", frames)
    rng = random.Random(42)
    noisy = [max(-32768, min(32767, int(s + rng.gauss(0, noise * 32767)))) for s in samples]
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setparams(params)
        wav.writeframes(struct.pack(f"<{len(noisy)}h", *noisy))
    return out.getvalue()


async def main() -> None:
    s = get_settings()
    voices_dir = s.resolve(s.voices_path)
    # Optional Whisper hint, e.g. "Hey Arthur". Measured: it made Whisper DROP the wake phrase
    # from transcripts (12/20 detected instead of 18/20), so ARTHUR doesn't use one.
    hotwords = sys.argv[1] if len(sys.argv) > 1 else ""
    stt = WhisperSTT(
        s.whisper_wake_model, language="en", min_confidence=0.3, hotwords=hotwords or None,
        download_root=str(s.resolve(s.models_path) / "whisper"),
    )  # fmt: skip
    print(f"hotwords: {hotwords!r}")
    stt.warm_up()
    missed, false_alarms, total_pos, total_neg, times = [], [], 0, 0, []
    for model in sorted(voices_dir.glob("*.onnx")):
        voice = PiperVoice.load(str(model))
        for noise in (0.0, 0.02):  # clean, and with light background hiss
            label = f"{model.stem} {'noisy' if noise else 'clean'}"
            for text, expected in [(t, True) for t in SHOULD_WAKE] + [
                (t, False) for t in SHOULD_NOT_WAKE
            ]:
                start = time.perf_counter()
                try:
                    heard = (await stt.transcribe(speak(voice, text, noise))).text
                except TranscriptionError:
                    heard = ""
                times.append(time.perf_counter() - start)
                woke = detect_wake_phrase(heard).wake
                total_pos += expected
                total_neg += not expected
                if expected and not woke:
                    missed.append(f"{label}: {text!r} -> heard {heard!r}")
                if woke and not expected:
                    false_alarms.append(f"{label}: {text!r} -> heard {heard!r}")
    print(f"wake phrases detected: {total_pos - len(missed)}/{total_pos}")
    print(f"false alarms:          {len(false_alarms)}/{total_neg}")
    print(f"average check time:    {sum(times) / len(times):.2f} s (speech synthesis included)")
    for line in missed:
        print("  MISSED:     ", line)
    for line in false_alarms:
        print("  FALSE ALARM:", line)


if __name__ == "__main__":
    asyncio.run(main())

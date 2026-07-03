"""
tts.py — Instant, natural speech via Deepgram Aura.

The offline voice (pyttsx3) is robust but robotic. For a natural, low-latency female
voice we call Deepgram Aura over plain HTTPS and play the returned WAV. To keep
time-to-first-word low, long replies are split into sentence chunks and spoken as
they arrive.

Pure parts (chunking, request building, error parsing) are unit-tested; the actual
HTTP + playback need internet + a speaker, so they're verified on the desk machine.
Everything falls back to the offline voice if Aura is unavailable.
"""

from __future__ import annotations

import io
import json
import re
import urllib.error
import urllib.request
import wave

DEEPGRAM_TTS_URL = "https://api.deepgram.com/v1/speak"


def aura_available(api_key: str | None) -> bool:
    return bool(api_key)


def chunk_for_speech(text: str, max_chars: int = 180) -> list[str]:
    """Split text into sentence-ish chunks so the first words play sooner."""
    text = (text or "").strip()
    if not text:
        return []
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks: list[str] = []
    cur = ""
    for s in sentences:
        if not s:
            continue
        if not cur:
            cur = s
        elif len(cur) + 1 + len(s) <= max_chars:
            cur += " " + s
        else:
            chunks.append(cur)
            cur = s
    if cur:
        chunks.append(cur)
    return chunks


def build_request(text: str, api_key: str, *, model: str = "aura-asteria-en",
                  sample_rate: int = 24000) -> tuple[str, dict, bytes]:
    """Return (url, headers, body) for an Aura synth call. Pure — easy to test."""
    url = (f"{DEEPGRAM_TTS_URL}?model={model}&encoding=linear16"
           f"&container=wav&sample_rate={sample_rate}")
    headers = {"Authorization": f"Token {api_key}", "Content-Type": "application/json"}
    body = json.dumps({"text": text}).encode("utf-8")
    return url, headers, body


def parse_error(raw: bytes) -> str:
    try:
        d = json.loads(raw.decode("utf-8", "ignore"))
        return str(d.get("err_msg") or d.get("reason") or d.get("message") or raw[:160])
    except (ValueError, AttributeError):
        return raw.decode("utf-8", "ignore")[:160]


def synthesize(text: str, api_key: str, *, model: str = "aura-asteria-en",
               sample_rate: int = 24000, timeout: float = 30.0) -> bytes:
    """Call Aura and return WAV bytes. Raises RuntimeError on failure."""
    if not api_key:
        raise RuntimeError("No Deepgram key for Aura TTS.")
    url, headers, body = build_request(text, api_key, model=model, sample_rate=sample_rate)
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Aura error {e.code}: {parse_error(e.read())}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach Aura: {e.reason}") from e


def play_wav_bytes(wav_bytes: bytes) -> None:
    """Play WAV audio through the default speaker. Needs sounddevice + a speaker."""
    import numpy as np
    import sounddevice as sd
    with wave.open(io.BytesIO(wav_bytes), "rb") as w:
        rate = w.getframerate()
        frames = w.readframes(w.getnframes())
    audio = np.frombuffer(frames, dtype=np.int16)
    sd.play(audio, rate)
    sd.wait()


def speak(text: str, api_key: str, *, model: str = "aura-asteria-en",
          sample_rate: int = 24000) -> None:
    """Synthesize and play one chunk (blocking). Used inside Abbey's voice thread."""
    play_wav_bytes(synthesize(text, api_key, model=model, sample_rate=sample_rate))

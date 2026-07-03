"""
stt.py — Speech-to-text for talking to Abbey.

Pipeline (press-to-talk):
    mic (sounddevice) -> PCM -> WAV bytes -> Deepgram -> transcript

Design notes:
  * Provider is Deepgram (lowest-latency mainstream STT). The HTTP call uses the
    standard library (urllib) so there is no extra dependency and no stub — it
    really posts your audio and reads the transcript back.
  * Recording ends automatically on trailing silence (natural, no fixed wait) or a
    max duration, via `Endpointer` — which is a pure state machine and unit-tested.
  * `sounddevice` (mic) and the network call can't run in the build sandbox, so
    those two functions are thin; everything they rely on (RMS, endpointing, WAV
    encoding, Deepgram response parsing) is pure and tested.

Key: set DEEPGRAM_API_KEY in the environment (see SETUP_GUIDE.md).
"""

from __future__ import annotations

import io
import json
import math
import urllib.error
import urllib.request
import wave

import numpy as np

DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"


# ---------------------------------------------------------------------------
# Pure audio helpers (unit-tested)
# ---------------------------------------------------------------------------
def rms(block: np.ndarray) -> float:
    """Root-mean-square loudness of an int16 PCM block."""
    if block.size == 0:
        return 0.0
    x = block.astype(np.float64)
    return float(math.sqrt(np.mean(x * x)))


def pcm_to_wav_bytes(pcm_int16: np.ndarray, sample_rate: int) -> bytes:
    """Encode mono int16 PCM as a WAV byte string (16-bit, 1 channel)."""
    pcm_int16 = np.asarray(pcm_int16, dtype=np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)             # 16-bit
        w.setframerate(int(sample_rate))
        w.writeframes(pcm_int16.tobytes())
    return buf.getvalue()


class Endpointer:
    """Decides when the speaker has finished: wait for speech to start, then stop
    once there's been `silence_ms` of quiet, or after `max_ms` overall.

    Pure state machine (feed it one block's RMS at a time), so it's fully tested.
    """

    def __init__(self, threshold_rms: float, silence_ms: int, block_ms: int,
                 max_ms: int, min_speech_ms: int = 200):
        self.threshold = threshold_rms
        self.silence_ms = silence_ms
        self.block_ms = block_ms
        self.max_ms = max_ms
        self.min_speech_ms = min_speech_ms
        self.elapsed = 0
        self.speech_ms = 0
        self.trailing_silence = 0
        self.started = False

    def update(self, block_rms: float) -> bool:
        """Feed the newest block's RMS. Returns True when recording should stop."""
        self.elapsed += self.block_ms
        if block_rms >= self.threshold:
            self.started = True
            self.speech_ms += self.block_ms
            self.trailing_silence = 0
        elif self.started:
            self.trailing_silence += self.block_ms
        # Stop when we've heard enough speech and it's gone quiet…
        if (self.started and self.speech_ms >= self.min_speech_ms
                and self.trailing_silence >= self.silence_ms):
            return True
        # …or we've simply hit the ceiling.
        return self.elapsed >= self.max_ms


# ---------------------------------------------------------------------------
# Mic capture (real; needs a microphone — verify on the desk machine)
# ---------------------------------------------------------------------------
def record_until_silence(*, sample_rate: int = 16000, block_ms: int = 30,
                         threshold_rms: float = 500.0, silence_ms: int = 800,
                         max_seconds: int = 12) -> np.ndarray:
    """Record from the default microphone until the speaker stops, and return
    mono int16 PCM. Raises RuntimeError with a clear message if audio isn't
    available so the app can tell the operator what to fix."""
    try:
        import sounddevice as sd
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "Microphone support isn't available (sounddevice/PortAudio). "
            "Install with: pip install sounddevice  (Linux also needs libportaudio2)."
        ) from e

    block = int(sample_rate * block_ms / 1000)
    ep = Endpointer(threshold_rms, silence_ms, block_ms, max_seconds * 1000)
    chunks: list[np.ndarray] = []
    try:
        with sd.InputStream(samplerate=sample_rate, channels=1, dtype="int16",
                            blocksize=block) as stream:
            while True:
                data, _ = stream.read(block)
                mono = data.reshape(-1)
                chunks.append(mono.copy())
                if ep.update(rms(mono)):
                    break
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Recording failed: {e}") from e

    if not chunks:
        return np.zeros(0, dtype=np.int16)
    return np.concatenate(chunks).astype(np.int16)


# ---------------------------------------------------------------------------
# Deepgram transcription (real HTTP; needs internet — verify on the desk machine)
# ---------------------------------------------------------------------------
def parse_deepgram_response(data: dict) -> str:
    """Pull the transcript out of Deepgram's JSON. Safe on unexpected shapes."""
    try:
        alts = data["results"]["channels"][0]["alternatives"]
        return str(alts[0].get("transcript", "")).strip()
    except (KeyError, IndexError, TypeError):
        return ""


def transcribe_deepgram(wav_bytes: bytes, api_key: str, model: str = "nova-2",
                        timeout: float = 15.0) -> str:
    """POST WAV audio to Deepgram and return the transcript text."""
    if not api_key:
        raise RuntimeError("DEEPGRAM_API_KEY is not set.")
    url = f"{DEEPGRAM_URL}?model={model}&smart_format=true&punctuate=true"
    req = urllib.request.Request(url, data=wav_bytes, method="POST")
    req.add_header("Authorization", f"Token {api_key}")
    req.add_header("Content-Type", "audio/wav")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Deepgram error {e.code}: {e.read().decode('utf-8', 'ignore')[:200]}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach Deepgram: {e.reason}") from e
    return parse_deepgram_response(data)


def transcribe(wav_bytes: bytes, *, provider: str, api_key: str, model: str) -> str:
    """Dispatch to the configured STT provider."""
    if provider == "deepgram":
        return transcribe_deepgram(wav_bytes, api_key, model)
    raise RuntimeError(f"Unknown STT provider: {provider}")


# ---------------------------------------------------------------------------
# Streaming / hands-free
# ---------------------------------------------------------------------------
def parse_live_message(data: dict) -> dict:
    """Parse one Deepgram live-streaming message → {transcript, is_final, speech_final}.
    Safe on unexpected shapes. (Used if a websocket transport is wired in; the pure
    parse is what we can prove here.)"""
    try:
        alt = data["channel"]["alternatives"][0]
        return {"transcript": str(alt.get("transcript", "")).strip(),
                "is_final": bool(data.get("is_final", False)),
                "speech_final": bool(data.get("speech_final", False))}
    except (KeyError, IndexError, TypeError):
        return {"transcript": "", "is_final": False, "speech_final": False}


class BackgroundListener:
    """Continuously capture endpointed utterances on a daemon thread and hand each
    finished transcript to `on_utterance`. This gives a reliable hands-free loop
    using the same fast endpointed capture as press-to-talk. (A Deepgram websocket
    transport can replace the record→transcribe step later for lower latency; the
    control logic lives in handsfree.HandsFree and is unit-tested.)

    Audio + network make this desk-machine-only; the class is structured so its
    behaviour is driven by the tested pieces around it.
    """

    def __init__(self, on_utterance, *, provider: str, api_key: str, stt_key: str,
                 model: str, sample_rate: int = 16000, block_ms: int = 30,
                 threshold_rms: float = 500.0, silence_ms: int = 800,
                 max_seconds: int = 12):
        import threading
        self.on_utterance = on_utterance
        self.provider = provider
        self.api_key = api_key
        self.stt_key = stt_key
        self.model = model
        self.sample_rate = sample_rate
        self.block_ms = block_ms
        self.threshold_rms = threshold_rms
        self.silence_ms = silence_ms
        self.max_seconds = max_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                pcm = record_until_silence(
                    sample_rate=self.sample_rate, block_ms=self.block_ms,
                    threshold_rms=self.threshold_rms, silence_ms=self.silence_ms,
                    max_seconds=self.max_seconds)
                if pcm.size == 0:
                    continue
                wav = pcm_to_wav_bytes(pcm, self.sample_rate)
                text = transcribe(wav, provider=self.provider, api_key=self.stt_key,
                                  model=self.model)
                if text:
                    self.on_utterance(text)
            except Exception:
                # a transient audio/network hiccup shouldn't kill the loop
                if self._stop.wait(0.5):
                    break

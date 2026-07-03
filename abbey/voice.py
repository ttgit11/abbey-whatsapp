"""
voice.py — Abbey's spoken voice.

Uses pyttsx3, which speaks OFFLINE (no cloud, no per-word cost) on Windows, macOS
and Linux. We pick a female system voice by name heuristic and speak on a
background thread so the UI never blocks.

If pyttsx3 isn't installed the whole thing degrades to silent no-ops, so the app
still runs. Install with:  pip install pyttsx3
(Windows/macOS have built-in voices; on Linux install espeak-ng.)
"""

from __future__ import annotations

import queue
import threading

try:
    import pyttsx3
    _HAVE_TTS = True
except Exception:            # ImportError or driver init issues
    _HAVE_TTS = False


# Common female voice identifiers across platforms.
_FEMALE_HINTS = ("female", "zira", "hazel", "susan", "samantha", "victoria",
                 "karen", "moira", "tessa", "fiona", "serena", "amelie", "anna")


class Abbey:
    """Speak short lines as 'Abbey'. Thread-safe, non-blocking, safe if TTS
    is unavailable."""

    def __init__(self, enabled: bool = True, rate: int = 172, volume: float = 0.9,
                 prefer: str = "female", cloud_key: str | None = None,
                 cloud_model: str = "aura-asteria-en", sample_rate: int = 24000,
                 prefer_cloud: bool = False):
        self.cloud_key = cloud_key
        self.cloud_model = cloud_model
        self.sample_rate = sample_rate
        self.prefer_cloud = bool(prefer_cloud and cloud_key)
        self._q: "queue.Queue[str | None]" = queue.Queue()
        self._engine = None
        self._thread = None
        self._have_offline = False
        self._speaking = False
        # We can speak if the user wants voice AND we have either engine.
        self.enabled = enabled and (_HAVE_TTS or self.prefer_cloud)
        if self.enabled:
            self._start(rate, volume, prefer)

    def _start(self, rate, volume, prefer):
        if _HAVE_TTS:
            try:
                self._engine = pyttsx3.init()
                self._engine.setProperty("rate", rate)
                self._engine.setProperty("volume", volume)
                self._select_voice(prefer)
                self._have_offline = True
            except Exception:
                self._engine = None
                self._have_offline = False
        if self._have_offline or self.prefer_cloud:
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
        else:
            self.enabled = False

    def _select_voice(self, prefer: str):
        try:
            voices = self._engine.getProperty("voices")
        except Exception:
            return
        prefer = (prefer or "female").lower()
        # 1) explicit gender attribute; 2) name heuristic; else leave default.
        for v in voices:
            gender = (getattr(v, "gender", "") or "").lower()
            name = (getattr(v, "name", "") or "").lower()
            if prefer in gender or any(h in name for h in _FEMALE_HINTS):
                self._engine.setProperty("voice", v.id)
                return

    def _speak_cloud(self, text: str) -> bool:
        """Speak via Deepgram Aura in sentence chunks. Returns False on any failure
        so the caller can fall back to the offline voice."""
        try:
            from . import tts
            for chunk in tts.chunk_for_speech(text):
                tts.speak(chunk, self.cloud_key, model=self.cloud_model,
                          sample_rate=self.sample_rate)
            return True
        except Exception:
            return False

    def _speak_offline(self, text: str) -> None:
        if not self._have_offline or self._engine is None:
            return
        try:
            self._engine.say(text)
            self._engine.runAndWait()
        except Exception:
            pass

    def _run(self):
        while True:
            text = self._q.get()
            if text is None:
                break
            self._speaking = True
            try:
                if self.prefer_cloud and self.cloud_key and self._speak_cloud(text):
                    pass
                else:
                    self._speak_offline(text)
            finally:
                self._speaking = False

    def is_speaking(self) -> bool:
        """True while Abbey is actually talking or has speech queued."""
        return self._speaking or not self._q.empty()

    def shush(self) -> None:
        """Stop talking now (barge-in): drop anything queued and cut playback."""
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass
        try:                       # cut cloud audio mid-word if it's playing
            import sounddevice as sd
            sd.stop()
        except Exception:
            pass

    def say(self, text: str) -> None:
        if self.enabled and text:
            self._q.put(text)

    def stop(self) -> None:
        if self.enabled:
            self._q.put(None)


# Canned lines, kept in one place so the personality is consistent.
LINES = {
    "greeting": "Hello, I'm Abbey. Place an item in front of the camera when you're ready.",
    "captured": "Got it. Let me take a look.",
    "ready": "Here's my draft. Have a look and correct me if I'm wrong.",
    "saved": "Saved. Next item whenever you are.",
    "unsure": "I'm not certain on this one — it may need a close-up.",
    "learning": "I've spotted a pattern in your corrections and I'd like to adjust my pricing. I'll need the passcode.",
    "locked": "Too many wrong passcodes. The settings are locked for now.",
}

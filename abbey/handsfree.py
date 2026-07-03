"""
handsfree.py — The controller for hands-free, always-listening conversation.

The desk is busy and staff have their hands full, so Abbey can run in a continuous
loop: listen → understand → speak → listen again, with no button. This module is the
BRAIN of that loop (a pure state machine), kept separate from the audio plumbing so
it can be fully unit-tested:

  * wake words   — optional "Abbey, …" to start a command (configurable/off);
  * stop words   — "stop listening", "that's enough" to stand down;
  * barge-in     — if the operator starts talking while Abbey is speaking, she yields;
  * turn-taking  — after speaking she returns to listening automatically.

The audio thread feeds transcripts in via `on_transcript`; the return value tells the
app what to do. No I/O happens here.
"""

from __future__ import annotations

IDLE = "idle"
LISTENING = "listening"
THINKING = "thinking"
SPEAKING = "speaking"


class HandsFree:
    def __init__(self, wake_words=("abbey",),
                 stop_words=("stop listening", "that's enough", "pause listening", "stop abbey"),
                 require_wake: bool = False, barge_in: bool = True):
        self.state = IDLE
        self.wake_words = [w.lower() for w in wake_words]
        self.stop_words = [w.lower() for w in stop_words]
        self.require_wake = require_wake
        self.barge_in = barge_in
        self.interrupted = False

    # --- control ---
    def start(self) -> str:
        self.state = LISTENING
        self.interrupted = False
        return self.state

    def stop(self) -> str:
        self.state = IDLE
        return self.state

    def begin_speaking(self) -> None:
        self.state = SPEAKING
        self.interrupted = False

    def done_speaking(self) -> None:
        # only resume listening if we didn't get told to stop mid-turn
        if self.state != IDLE:
            self.state = LISTENING

    # --- classification ---
    def is_wake(self, text: str) -> bool:
        t = text.lower()
        return any(w in t for w in self.wake_words)

    def is_stop(self, text: str) -> bool:
        t = text.lower()
        return any(w in t for w in self.stop_words)

    def _strip_wake(self, text: str) -> str:
        t = text.lower()
        for w in self.wake_words:
            i = t.find(w)
            if i != -1:
                return (text[:i] + text[i + len(w):]).strip(" ,.-").strip()
        return text.strip()

    def on_transcript(self, text: str) -> dict:
        """Feed a finished utterance. Returns {"type": ..., "text": ...} where type
        is one of: 'process' (run it), 'stop', 'wake' (acknowledged, no command),
        'ignore'."""
        text = (text or "").strip()
        if not text:
            return {"type": "ignore", "text": ""}

        if self.is_stop(text):
            self.stop()
            return {"type": "stop", "text": text}

        # barge-in: operator spoke while Abbey was talking
        if self.state == SPEAKING:
            if not self.barge_in:
                return {"type": "ignore", "text": text}
            self.interrupted = True

        # wake gating
        if self.require_wake and self.state in (IDLE,):
            if self.is_wake(text):
                self.state = LISTENING
                cmd = self._strip_wake(text)
                if cmd:
                    self.state = THINKING
                    return {"type": "process", "text": cmd}
                return {"type": "wake", "text": ""}
            return {"type": "ignore", "text": text}

        # otherwise treat it as a command (strip a leading "Abbey" if present)
        self.state = THINKING
        cmd = self._strip_wake(text) if self.is_wake(text) else text
        return {"type": "process", "text": cmd}

"""
health.py — A startup system check.

With two API keys (Claude + Deepgram), a camera, a microphone and a voice engine
all in play, a bad setup is easy to miss until mid-sale. `diagnostics()` returns a
simple list of (name, ok, detail) so the sidebar can show green/red ticks before
the first item.

Pure and unit-tested (imports are probed defensively).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".write_test"
        probe.write_text("ok")
        probe.unlink()
        return True
    except OSError:
        return False


def diagnostics(*, anthropic_key: str | None, deepgram_key: str | None,
                data_dir: Path, sales_dir: Path) -> list[tuple[str, bool, str]]:
    """Return [(check_name, ok, detail), …]."""
    checks: list[tuple[str, bool, str]] = []
    checks.append(("Claude API key", bool(anthropic_key),
                   "set" if anthropic_key else "missing — set ANTHROPIC_API_KEY"))
    checks.append(("Speech key (Deepgram)", bool(deepgram_key),
                   "set" if deepgram_key else "missing — set DEEPGRAM_API_KEY (voice)"))
    checks.append(("Data folder writable", _writable(Path(data_dir)), str(data_dir)))
    checks.append(("Sales folder", _writable(Path(sales_dir)), str(sales_dir)))
    checks.append(("Camera (OpenCV)", _importable("cv2"),
                   "installed" if _importable("cv2") else "pip install opencv-python"))
    checks.append(("Microphone (sounddevice)", _importable("sounddevice"),
                   "installed" if _importable("sounddevice") else "pip install sounddevice"))
    checks.append(("Voice out (pyttsx3)", _importable("pyttsx3"),
                   "installed" if _importable("pyttsx3") else "pip install pyttsx3"))
    checks.append(("Anthropic SDK", _importable("anthropic"),
                   "installed" if _importable("anthropic") else "pip install anthropic"))
    return checks


def all_ok(checks: list[tuple[str, bool, str]]) -> bool:
    return all(ok for _, ok, _ in checks)

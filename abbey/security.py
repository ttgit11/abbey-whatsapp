"""
security.py — Passcode gate for anything that changes how Abbey behaves.

Design rules:
  * The passcode is NEVER stored in plaintext. We keep a PBKDF2-HMAC-SHA256 hash
    with a random per-install salt.
  * Ordinary work (fixing a title, nudging one estimate) never needs the passcode.
  * "Learning shifts" and settings changes DO: applying a category-wide price
    change, trusting/untrusting a source, editing config, clearing data, or
    changing the passcode itself.
  * After too many wrong attempts the panel locks for a cool-down period.

Pure and fully unit-tested in tests/test_security.py.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

_PBKDF2_ROUNDS = 200_000
_DEFAULT_PASSCODE = "1969"   # Abbeys established 1969 — CHANGE THIS on first run.


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------
def hash_passcode(passcode: str, salt: bytes | None = None) -> tuple[str, str]:
    """Return (salt_hex, hash_hex)."""
    if salt is None:
        salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return salt.hex(), dk.hex()


def verify_passcode(passcode: str, salt_hex: str, hash_hex: str) -> bool:
    """Constant-time verification."""
    salt = bytes.fromhex(salt_hex)
    dk = hashlib.pbkdf2_hmac("sha256", passcode.encode("utf-8"), salt, _PBKDF2_ROUNDS)
    return hmac.compare_digest(dk.hex(), hash_hex)


# ---------------------------------------------------------------------------
# Which actions require the passcode
# ---------------------------------------------------------------------------
# Actions that are always free (no passcode):
FREE_ACTIONS = {
    "edit_title", "edit_description", "edit_single_estimate",
    "edit_category", "edit_condition", "capture", "save_lot", "export_csv",
}
# Actions that always require the passcode:
GATED_ACTIONS = {
    "apply_price_shift", "demote_source", "promote_source",
    "change_config", "clear_data", "change_passcode", "bulk_reprice",
}


def requires_passcode(action: str, magnitude_pct: float = 0.0,
                      auto_band_pct: float = 8.0) -> bool:
    """Central policy. `magnitude_pct` lets a *small* automatic price nudge pass
    while a large one is gated."""
    if action in FREE_ACTIONS:
        return False
    if action in GATED_ACTIONS:
        # A price shift within the auto-learn band is allowed through unattended.
        if action == "apply_price_shift" and abs(magnitude_pct) <= auto_band_pct:
            return False
        return True
    # Unknown action: fail safe -> require the passcode.
    return True


# ---------------------------------------------------------------------------
# Passcode store with lockout
# ---------------------------------------------------------------------------
@dataclass
class _LockState:
    fails: int = 0
    locked_until: float = 0.0


class PasscodeStore:
    """Persists the salt+hash to a small json file next to the app."""

    def __init__(self, path: Path, max_attempts: int = 5, lockout_seconds: int = 300):
        self.path = Path(path)
        self.max_attempts = max_attempts
        self.lockout_seconds = lockout_seconds
        self._lock = _LockState()
        if not self.path.exists():
            self._write(*hash_passcode(_DEFAULT_PASSCODE), is_default=True)

    # --- persistence ---
    def _write(self, salt_hex: str, hash_hex: str, is_default: bool = False) -> None:
        self.path.write_text(json.dumps({
            "salt": salt_hex, "hash": hash_hex, "is_default": is_default,
        }))

    def _read(self) -> dict:
        return json.loads(self.path.read_text())

    def is_default(self) -> bool:
        return bool(self._read().get("is_default", False))

    # --- lockout ---
    def is_locked(self) -> bool:
        return time.time() < self._lock.locked_until

    def seconds_remaining(self) -> int:
        return max(0, int(self._lock.locked_until - time.time()))

    # --- operations ---
    def verify(self, passcode: str) -> bool:
        if self.is_locked():
            return False
        rec = self._read()
        ok = verify_passcode(passcode, rec["salt"], rec["hash"])
        if ok:
            self._lock = _LockState()  # reset on success
        else:
            self._lock.fails += 1
            if self._lock.fails >= self.max_attempts:
                self._lock.locked_until = time.time() + self.lockout_seconds
                self._lock.fails = 0
        return ok

    def change(self, old_passcode: str, new_passcode: str) -> bool:
        """Change the passcode. Requires the current one. New must be >= 4 chars."""
        if not self.verify(old_passcode):
            return False
        if len(new_passcode) < 4:
            return False
        self._write(*hash_passcode(new_passcode), is_default=False)
        return True

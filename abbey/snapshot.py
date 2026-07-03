"""
snapshot.py — Visual proof of where a price came from (Sweep 3).

For each comparable Abbey finds, we can capture a screenshot of the source page
and keep it with the lot, so a price isn't just a claim — you can see the listing
it came from.

Capture uses `wkhtmltoimage` (a tiny, script-friendly headless renderer). The path
logic is pure and tested; the capture itself needs the binary + internet, so it's
verified on the desk machine.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path


def _tool() -> str | None:
    return shutil.which("wkhtmltoimage")


def snapshot_available() -> tuple[bool, str]:
    t = _tool()
    if t:
        return True, t
    return False, "wkhtmltoimage not found — install the wkhtmltopdf package to capture snapshots."


def snapshot_filename(out_dir: Path, lot_ref: str, idx: int, url: str) -> Path:
    """Deterministic, collision-free name for one source snapshot."""
    h = hashlib.sha1((url or "").encode("utf-8")).hexdigest()[:8]
    safe = (lot_ref or "lot").replace("/", "-").replace(" ", "")
    return Path(out_dir) / f"{safe}_src{idx}_{h}.png"


def capture_url(url: str, out_path: Path, *, width: int = 1024, timeout: int = 30) -> str:
    """Screenshot a web page to PNG. Raises RuntimeError with a clear message on
    failure so the app can show it."""
    tool = _tool()
    if not tool:
        raise RuntimeError(snapshot_available()[1])
    if not (url or "").startswith("http"):
        raise RuntimeError("Not a valid web address.")
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    cmd = [tool, "--width", str(width), "--quality", "80",
           "--load-error-handling", "ignore", "--javascript-delay", "1200",
           url, str(out_path)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"Snapshot failed: {e.stderr.decode('utf-8', 'ignore')[:160]}") from e
    except subprocess.TimeoutExpired as e:
        raise RuntimeError("Snapshot timed out — the page took too long.") from e
    return str(out_path)


def capture_comps(comps, out_dir: Path, lot_ref: str, *, width: int = 1024,
                  timeout: int = 30, limit: int = 6) -> list[dict]:
    """Capture snapshots for a list of research comps (each needs a .url).

    Returns [{url, source, price_text, path|None, error|None}], never raising —
    one bad URL shouldn't stop the rest.
    """
    results = []
    for i, c in enumerate(comps[:limit]):
        url = getattr(c, "url", "") or ""
        if not url.startswith("http"):
            continue
        path = snapshot_filename(out_dir, lot_ref, i, url)
        entry = {"url": url, "source": getattr(c, "source", ""),
                 "price_text": getattr(c, "price_text", ""), "path": None, "error": None}
        try:
            entry["path"] = capture_url(url, path, width=width, timeout=timeout)
        except RuntimeError as e:
            entry["error"] = str(e)
        results.append(entry)
    return results

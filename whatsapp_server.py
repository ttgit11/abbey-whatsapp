"""
whatsapp_server.py — The offsite valuer webhook (deploy to Railway/Render).

Twilio POSTs each incoming WhatsApp message here. We ACK instantly (Twilio's
15-second rule), download any photo, feed the message to the offsite Session state
machine, and — when the valuer says "done" (or after 2 minutes idle) — run Abbey's
brain over each item in the background and send back the list + a Go Auction Excel,
all over WhatsApp via Twilio's REST API.

This is the thin network layer. All the item-grouping logic lives in
abbey/offsite.py (pure, tested); the cataloguing/pricing lives in abbey/agent.py
and abbey/knowledge.py (tested). This file wires them to Twilio.

ENV VARS (set on the host — never in code):
  TWILIO_SID, TWILIO_AUTH_TOKEN, TWILIO_WHATSAPP_FROM, YOUR_WHATSAPP_TO
  ANTHROPIC_API_KEY            (Abbey's brain)
Run:  python whatsapp_server.py         (or gunicorn whatsapp_server:app)
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import requests
from flask import Flask, request, Response

import logging

from abbey import offsite, agent, knowledge, storage, increments, memory
import config
import seed_house_knowledge

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("abbey.offsite")

app = Flask(__name__)

TWILIO_SID = os.environ.get("TWILIO_SID", "")
TWILIO_AUTH = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_FROM = os.environ.get("TWILIO_WHATSAPP_FROM", "")
DEFAULT_TO = os.environ.get("YOUR_WHATSAPP_TO", "")

MEDIA_DIR = config.DATA_DIR / "offsite_media"
MEDIA_DIR.mkdir(parents=True, exist_ok=True)

# One session per sender number. (Simple in-memory store; fine for a few valuers.)
_sessions: dict[str, offsite.Session] = {}
_lock = threading.Lock()
# Guard so a job isn't processed twice at once (webhook + watchdog race).
_processing: set[str] = set()


def ensure_seeded() -> None:
    """On the cloud host the DB starts empty — seed the 2706 bands so offsite
    pricing is as sharp as the desk. Idempotent; safe to call every boot."""
    try:
        conn = storage.connect(config.DB_PATH)
        if not knowledge.all_comps(conn):
            n_comps, n_src = seed_house_knowledge.seed(conn)
            log.info("Seeded offsite DB: %d comps, %d sources", n_comps, n_src)
        else:
            log.info("Offsite DB already has house bands — no seed needed.")
    except Exception as e:  # noqa: BLE001
        log.warning("Could not seed offsite DB: %s", e)


def _session(sender: str) -> offsite.Session:
    with _lock:
        if sender not in _sessions:
            _sessions[sender] = offsite.Session()
        return _sessions[sender]


# ---------------------------------------------------------------------------
# Twilio helpers
# ---------------------------------------------------------------------------
def send_whatsapp(to: str, body: str) -> None:
    for chunk in offsite.split_for_whatsapp(body):
        try:
            requests.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
                auth=(TWILIO_SID, TWILIO_AUTH),
                data={"From": TWILIO_FROM, "To": to, "Body": chunk},
                timeout=20)
        except Exception as e:  # noqa: BLE001
            log.warning("send_whatsapp failed: %s", e)


def send_media(to: str, media_url: str, caption: str = "") -> None:
    try:
        requests.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            auth=(TWILIO_SID, TWILIO_AUTH),
            data={"From": TWILIO_FROM, "To": to, "Body": caption, "MediaUrl": media_url},
            timeout=30)
    except Exception as e:  # noqa: BLE001
        log.warning("send_media failed: %s", e)


def download_media(url: str, dest: Path) -> str | None:
    try:
        r = requests.get(url, auth=(TWILIO_SID, TWILIO_AUTH), timeout=30)
        if r.status_code == 200:
            dest.write_bytes(r.content)
            return str(dest)
    except Exception as e:  # noqa: BLE001
        log.warning("download_media failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Abbey processing (background)
# ---------------------------------------------------------------------------
def process_and_reply(sender: str, to: str) -> None:
    """Run Abbey over every dirty item, then send the list + Excel.
    Guarded so the webhook and the idle watchdog can't process the same job twice,
    and so a failure leaves the job re-runnable rather than stuck."""
    with _lock:
        if sender in _processing:
            log.info("Skipping duplicate process for %s (already running)", sender)
            return
        _processing.add(sender)
    sess = _session(sender)
    job = sess.job
    if job is None:
        with _lock:
            _processing.discard(sender)
        return
    try:
        from anthropic import Anthropic
        conn = storage.connect(config.DB_PATH)
        client = Anthropic(api_key=config.get_api_key())
        sys_prompt = agent.build_system_prompt(
            config.SETTINGS.house_name, knowledge.all_comps(conn),
            knowledge.trusted_sources(conn), config.SETTINGS.buyers_premium_pct,
            insights_block=memory.context_for(conn))

        failed = 0
        for it in job.items:
            if not it.photos or not it.dirty:
                continue
            try:
                _process_one_item(client, conn, sys_prompt, job, it)
            except Exception as e:  # noqa: BLE001 — isolate one bad item, keep going
                failed += 1
                log.warning("Item %s failed: %s", it.number, e)
                if not it.title:
                    it.title = f"(item {it.number} — needs manual review)"

        # 1) the text list
        send_whatsapp(to, offsite.format_list(job))
        # 2) the Excel (built + hosted so Twilio can fetch it)
        xlsx_path = build_excel(job)
        public = os.environ.get("PUBLIC_BASE_URL", "")
        if public:
            send_media(to, f"{public}/files/{xlsx_path.name}",
                       caption=f"Go Auction upload — receipt {job.receipt}")
        else:
            send_whatsapp(to, "(Excel ready on the server; set PUBLIC_BASE_URL to receive it in chat.)")
        if failed:
            send_whatsapp(to, f"Note: {failed} item(s) didn't process cleanly and are marked "
                              f"for manual review. Re-send a photo or a note to retry them.")
        log.info("Receipt %s done: %d items, %d failed", job.receipt,
                 len([i for i in job.items if i.photos]), failed)
    except Exception as e:  # noqa: BLE001 — whole-job failure: leave it re-runnable
        log.exception("process_and_reply failed for %s", sender)
        if job is not None:
            job.finalised = False        # so a nudge can retry rather than being stuck
        send_whatsapp(to, f"Abbey hit a problem finishing that receipt: {e}. "
                          f"Send 'done' again to retry.")
    finally:
        with _lock:
            _processing.discard(sender)


def _process_one_item(client, conn, sys_prompt, job, it) -> None:
    """Catalogue a single item with Abbey's brain. Raises on failure."""
    with open(it.photos[0], "rb") as fh:
        img = fh.read()
    note = (f"\nValuer's note (use it — keywords, damage, measurement, or a "
            f"price to use as the estimate): {it.note}" if it.note else "")
    draft = agent.analyze_item(
        client, img, model=config.SETTINGS.model_primary,
        max_tokens=config.SETTINGS.max_tokens,
        system_prompt=sys_prompt + note,
        receipt=job.receipt, item_no=str(it.number),
        enable_web=config.SETTINGS.enable_web_research)
    it.title, it.description, it.category = draft.title, draft.description, draft.category
    # price: valuer's stated price wins, else Abbey's snapped estimate
    want = offsite.wants_price(it.note)
    if want:
        lo, hi = increments.snap_estimate(want * 0.85, want * 1.15)
    elif draft.category and draft.ai_low_estimate and draft.ai_high_estimate:
        lo, hi = knowledge.effective_estimate(
            conn, draft.category, draft.ai_low_estimate, draft.ai_high_estimate)
    else:
        lo, hi = increments.snap_estimate(draft.low_estimate or 0, draft.high_estimate or 0)
    it.low, it.high = float(lo), float(hi)
    it.processed, it.dirty = True, False


def build_excel(job) -> Path:
    """Write the Go Auction Appraisal Import rows for this job to an .xlsx."""
    from openpyxl import Workbook
    wb = Workbook(); ws = wb.active
    ws.append(config.SETTINGS.csv_columns)
    n = 0
    for it in job.items:
        if not it.photos:
            continue
        n += 1
        desc = it.description or ""
        ws.append([n, it.title, desc,
                   int(it.low) if it.low else "", int(it.high) if it.high else "",
                   it.category, "", config.SETTINGS.default_consign_to, "", "",
                   "", ""])
    out = MEDIA_DIR / f"receipt_{job.receipt or 'draft'}_{int(time.time())}.xlsx"
    wb.save(out)
    return out


# ---------------------------------------------------------------------------
# Idle-finalise watchdog (2 minutes of silence = done)
# ---------------------------------------------------------------------------
def _watchdog() -> None:
    while True:
        time.sleep(15)
        for sender, sess in list(_sessions.items()):
            if sender in _processing:
                continue                       # already being handled
            if sess.idle_finalise_due():
                log.info("Idle finalise for %s after 2 min silence", sender)
                sess.job.finalised = True
                threading.Thread(target=process_and_reply,
                                 args=(sender, DEFAULT_TO or sender), daemon=True).start()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    sender = request.form.get("From", "")
    body = request.form.get("Body", "")
    num_media = int(request.form.get("NumMedia", 0) or 0)
    sess = _session(sender)
    to = DEFAULT_TO or sender

    # photos first
    for i in range(num_media):
        url = request.form.get(f"MediaUrl{i}")
        if not url:
            continue
        dest = MEDIA_DIR / f"{sender.replace(':','_').replace('+','')}_{int(time.time()*1000)}_{i}.jpg"
        path = download_media(url, dest)
        if path:
            sess.on_photo(path)

    # then the text (may be empty if only media)
    if body.strip():
        action = sess.on_text(body)
        if action["action"] in ("done", "edit"):
            threading.Thread(target=process_and_reply, args=(sender, to), daemon=True).start()
        elif action["action"] == "edit_miss":
            send_whatsapp(to, f"There's no item {action['requested']} on this receipt "
                              f"(items are {action['existing']}). Try again with a valid number.")
        elif action["action"] == "receipt":
            send_whatsapp(to, f"Got receipt {action['receipt']}. Send photos, then a note per "
                              f"item. Say 'done' when finished.")

    return Response("<Response></Response>", mimetype="application/xml")


@app.route("/files/<name>", methods=["GET"])
def files(name):
    p = MEDIA_DIR / name
    if not p.exists():
        return Response("not found", status=404)
    return Response(p.read_bytes(),
                    mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@app.route("/health", methods=["GET"])
def health():
    return {"ok": True, "sessions": len(_sessions)}


# Start-up work that must run under BOTH `python whatsapp_server.py` AND gunicorn.
# We defer it to the first real request (and the __main__ path) rather than doing it
# at import time, so merely importing the module (e.g. in tests) has no side effects
# — no DB writes, no watchdog thread.
_started = False
_start_lock = threading.Lock()


def _startup() -> None:
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
    ensure_seeded()
    threading.Thread(target=_watchdog, daemon=True).start()
    log.info("Abbey offsite service started.")


@app.before_request
def _ensure_started():
    if not _started:
        _startup()


if __name__ == "__main__":
    _startup()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))

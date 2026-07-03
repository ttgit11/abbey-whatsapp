# Abbey WhatsApp — Railway Deploy (this folder)

This folder is the **complete, self-contained** WhatsApp valuer service, ready for Railway.
Everything it needs is here; nothing from the desk app is required. It's been tested to boot
exactly the way Railway runs it.

## The 6 stages (you're up to Stage 4)

### Stage 4 — Get this folder to Railway (via GitHub, easiest)
1. Go to **github.com**, sign in (or make a free account).
2. Click **New repository** → name it `abbey-whatsapp` → keep it **Private** → Create.
3. On the new repo page, click **"uploading an existing file"**.
4. Drag in **everything in this folder** — the loose files (whatsapp_server.py, config.py,
   seed_house_knowledge.py, Procfile, requirements.txt) AND the whole `abbey` folder.
5. Click **Commit changes**.
6. Go to **railway.app** → **New Project** → **Deploy from GitHub repo** → pick `abbey-whatsapp`.
   (If Railway isn't linked to GitHub yet, it'll prompt you — click Authorize.)
7. Railway starts building automatically. It reads the **Procfile** and **requirements.txt**
   and knows what to do. Wait for it to finish (a minute or two).

### Stage 5 — Set the environment variables
In your Railway project → click the service → **Variables** tab → add these six
(paste your real values — the ones from your notepad):
```
TWILIO_SID            = AC...            (your Account SID)
TWILIO_AUTH_TOKEN     = ...              (your Auth Token)
TWILIO_WHATSAPP_FROM  = whatsapp:+14155238886
YOUR_WHATSAPP_TO      = whatsapp:+61488020101
ANTHROPIC_API_KEY     = sk-ant-...       (the new abbey-whatsapp key)
```
Then, after the first deploy gives you a public URL (next step), add one more and redeploy:
```
PUBLIC_BASE_URL       = https://your-app.up.railway.app
```

### Stage 5b — Get your public URL
Railway → your service → **Settings** → **Networking** → **Generate Domain**.
It gives you something like `https://abbey-whatsapp-production.up.railway.app`.
- Put that into the `PUBLIC_BASE_URL` variable above (then it redeploys).
- Test it: open `https://your-app.up.railway.app/health` in a browser — you should see
  `{"ok": true, "sessions": 0}`.

### Stage 6 — Point Twilio at it
Twilio Console → **Messaging → Try it out → WhatsApp Sandbox → Sandbox settings**.
In **"When a message comes in"**, paste:
```
https://your-app.up.railway.app/whatsapp        Method: POST
```
Save.

### Then: send a test
From your WhatsApp (+61488020101) to the sandbox number **+14155238886**:
1. Send a receipt number, e.g. `2612` → Abbey replies she's ready.
2. Send a photo of an item, then a short text (e.g. "oil painting, gilt frame").
3. Send another item's photos, then a text. Repeat.
4. Send `done`.
5. Abbey sends back the list, then the Go Auction Excel.

## If something goes wrong
- **Build fails:** Railway → Deployments → View logs. Usually a missing file — make sure the
  whole `abbey` folder uploaded (26 files inside it).
- **App crashes on boot:** check the logs for the error. Most common is a typo in a variable
  name — they must match exactly (all caps, underscores).
- **No reply on WhatsApp:** re-check the webhook URL in Twilio ends in `/whatsapp` and Method
  is POST; confirm the sandbox is still joined (72-hour window — re-send `join quiet-choose`).
- **"Excel ready on the server" but no file:** `PUBLIC_BASE_URL` isn't set or is wrong — set
  it to your exact Railway domain and redeploy.

## Notes
- The service auto-seeds your 2706 price bands on first boot — offsite pricing matches the desk.
- Sandbox membership lasts 72 hours; re-send `join quiet-choose` to reconnect.
- Each item = real Claude vision work; a big receipt takes a couple of minutes before "done".

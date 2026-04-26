# Extract Skool Course Section → Google Doc Takeaways

## Purpose
One-shot workflow that logs into Skool, lets you pick any section interactively,
scrapes video transcripts, extracts key takeaways with an LLM via OpenRouter,
and writes a formatted Google Doc organized by Phase → Video → bullet points.

Run it whenever you want. Each run creates a new Google Doc and leaves old ones
untouched in your Drive.

---

## One-Time Setup (do this once)

### 1. Install Python dependencies
```bash
pip install -r requirements.txt
playwright install chromium
```

Whisper fallback also requires `ffmpeg` on PATH:
```bash
winget install Gyan.FFmpeg
```
(Only needed if videos have no captions. Restart your terminal after install.)

### 2. Populate .env
Open `.env` in the project root and fill in:

```
SKOOL_EMAIL=your@email.com
SKOOL_PASSWORD=yourpassword
SKOOL_COMMUNITY_URL=https://www.skool.com/ai-automation-system-plus-society
OPENROUTER_API_KEY=sk-or-...
OPENROUTER_MODEL=anthropic/claude-opus-4-5
```

Get your OpenRouter API key at https://openrouter.ai/keys
Browse available models at https://openrouter.ai/models

### 3. Set up Google OAuth (one-time, ~10 minutes)

**a) Create a Google Cloud project**
- Go to https://console.cloud.google.com
- Create a new project (e.g. "Skool Extractor") or select an existing one

**b) Enable APIs**
- APIs & Services → Library → search "Google Docs API" → Enable
- APIs & Services → Library → search "Google Drive API" → Enable

**c) Configure OAuth consent screen**
- APIs & Services → OAuth consent screen
- User type: **External** → Create
- App name: Skool Extractor | Support email: cmiklesavage@gmail.com | Developer email: cmiklesavage@gmail.com
- Scopes page: Add `https://www.googleapis.com/auth/documents` and `https://www.googleapis.com/auth/drive.file`
- Test users: add `cmiklesavage@gmail.com`
- Save

**d) Create OAuth credentials**
- APIs & Services → Credentials → Create Credentials → OAuth client ID
- Application type: **Desktop app** | Name: Skool Extractor Desktop
- Click Create → Download JSON
- Rename the file to `credentials.json` and place it in the project root

**e) First-run OAuth consent**
- Run `python tools/write_google_doc.py`
- A browser tab opens — sign in with your Google account and click Allow
- `token.json` is auto-saved. You won't be prompted again unless you revoke the app.

---

## Running the Workflow

### Step 1 — Scrape course transcripts (~15–45 min)
```bash
python tools/scrape_skool_course.py
```

The script logs in, then shows a numbered menu:
```
Available sections:
  [1] Build Your Portfolio
  [2] Traffic & Leads
  ...
Which section? Enter a number: 1
```

Type a number and press Enter. The script scrapes all videos in that section
and saves `.tmp/course_data.json`. Progress is saved incrementally — if it
gets interrupted, re-running picks up where it left off.

To skip the menu (e.g., for a specific section every time):
```bash
python tools/scrape_skool_course.py --section "Build Your Portfolio"
```

To show the browser window (useful if Skool asks for MFA):
```bash
python tools/scrape_skool_course.py --no-headless
```

### Step 2 — Extract AI takeaways (~2–5 min)
```bash
python tools/extract_takeaways.py
```

Sends each transcript to OpenRouter and writes `.tmp/course_takeaways.json`.
To use a different model, change `OPENROUTER_MODEL` in `.env`.

### Step 3 — Write Google Doc (<30 sec)
```bash
python tools/write_google_doc.py
```

Creates the Google Doc and prints the URL. Open it in your browser.

---

## Intermediate Files

| File | Purpose | Safe to delete? |
|------|---------|----------------|
| `.tmp/course_data.json` | Raw transcripts from Skool | Yes — re-run Step 1 to regenerate |
| `.tmp/course_takeaways.json` | Transcripts + AI takeaways | Yes — re-run Step 2 to regenerate |
| `.tmp/skool_session.json` | Saved Skool login session (expires ~24h) | Yes — regenerated on next login |

---

## Troubleshooting

**"Login timed out"** — Check SKOOL_EMAIL and SKOOL_PASSWORD in `.env`. Try `--no-headless` to see what's happening in the browser.

**"No sections found"** — Skool may have updated their page structure. Run with `--no-headless` and check the classroom URL manually.

**"No transcript available" for some videos** — Those videos have no captions and Whisper couldn't download audio. This can happen with very short clips or if ffmpeg is missing.

**Google Doc has formatting issues** — This can happen if a phase has many videos. The Doc is still usable; formatting is cosmetic.

**"credentials.json not found"** — Complete Step 3d above.

**"Token expired" error** — Delete `token.json` from the project root and re-run `write_google_doc.py` to re-authenticate.

---

## Notes
- Each run creates a NEW Google Doc. Old ones remain in your Drive — you can delete them manually.
- To re-run just the AI extraction (e.g., to try a different model), delete `.tmp/course_takeaways.json` and re-run Step 2. You don't need to re-scrape.
- To re-run just the Google Doc step (e.g., to reformat), just re-run Step 3.
- The community is fixed to `SKOOL_COMMUNITY_URL` in `.env`. To use a different community, update that value.

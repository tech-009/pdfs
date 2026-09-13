# PDF Text-Editing Core (local build, pass 1)

This is the **real text-editing core** of the full PDF editor spec — the
hardest and most important requirement — built to run locally on your
machine. It is deliberately scoped: no UI shell, no OCR, no database, no
Railway deployment yet. Those are the next passes.

## What this actually does

- Parses a real PDF with PyMuPDF and extracts every run of text as a
  structured element with its **real** bounding box, font name, size,
  weight, style, and color — straight from the PDF content stream, not
  guessed from rendering.
- Lets you edit any text element's content, and run search/replace
  across the whole document.
- On export, it **redacts** the original glyphs (a real content-stream
  operation, not a visual cover-up) and re-inserts your edited text at
  the same position using the closest matching font, so the layout
  stays close to the original. Every element you didn't touch — other
  text, images, vector graphics — is left completely untouched.
- Ships with a small HTML test harness so you can see the whole loop
  (upload → edit → export → download a real PDF) without waiting for
  the full desktop UI.

## I could not test-run this myself

The sandbox I built this in has no network access, so I couldn't `pip
install` PyMuPDF/FastAPI or actually execute the server here. I've
written and reviewed the code carefully against the PyMuPDF API, but
you should treat this as "ready to try," not "verified passing" —
please run it and tell me what breaks so I can fix it fast.

## Local setup

**Backend:**
```bash
cd backend
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8000
```

**Frontend** (in a second terminal):
```bash
cd frontend
npm install
npm start                       # serves on http://localhost:3000, talks to localhost:8000 by default
```

Open `http://localhost:3000`, upload a text-based PDF, edit a line,
then search/replace, then export.

## Deploying to Railway (two services, one repo)

This repo is a monorepo with two independently-deployable folders:
`backend/` (FastAPI) and `frontend/` (Node/Express static server). Each
has its own `railway.json` with an explicit start command, so Railway
doesn't have to guess how to run it.

**1. Create the backend service**
- New Service → Deploy from GitHub repo → pick this repo.
- Settings → **Root Directory** → set to `backend`.
- Deploy. Once it's live, Settings → Networking → **Generate Domain**.
  Copy that URL (e.g. `https://pdf-editor-backend-production.up.railway.app`).
- Visit `<that-url>/health` — you should see `{"status": "ok"}`.

**2. Create the frontend service**
- In the same Railway project: New Service → Deploy from GitHub repo → same repo again.
- Settings → **Root Directory** → set to `frontend`.
- Settings → Variables → add `API_BASE_URL` = the backend URL from step 1.
- Deploy, then Generate Domain for this service too.
- Visit the frontend's URL — it should load the editor UI and successfully talk to the backend.

**3. Lock down CORS (optional but recommended once both URLs are known)**
- Go back to the **backend** service → Variables → add
  `ALLOWED_ORIGINS` = the frontend URL from step 2 (comma-separate
  multiple origins if needed).
- Redeploy the backend. Without this variable it defaults to `*`
  (any origin), which is fine for testing but not for a real deployment.

**Why two services instead of one:** the backend needs a Python
runtime and PyMuPDF; the frontend is a static Node server. Railpack
builds each from its own `railway.json`/`requirements.txt`/
`package.json`, so keeping them as separate services with separate
Root Directories is the standard Railway pattern for a repo like this
— it also means you can redeploy or scale either independently later.

**If the build fails again:** paste me the new build log. The most
common causes on Railway are (a) Root Directory not set, so it tries
to build the whole repo as one app, or (b) a missing/incorrect start
command — both are addressed by the `railway.json` files in this repo,
but Railway dashboard settings can still override them if configured
differently.

## Try it with

Any normal text-based PDF (not a scanned image) — an invoice, a
letter, a report exported from Word/Google Docs, etc. Scanned/image
PDFs will parse with zero text elements, which is expected — OCR is
next.

## Honest current limitations

- **No paragraph reflow.** Editing a span replaces it in place; if the
  new text is much longer than the original box, PyMuPDF auto-shrinks
  it to fit rather than wrapping into a new line. Real reflow across a
  whole paragraph is a real feature to build next — the document model
  already groups spans by block/line, which is what that would key off.
- **Font matching, not font embedding.** We map every font to the
  closest PyMuPDF base-14 font rather than re-embedding the original
  font file. Visually close for common fonts (Arial/Helvetica,
  Times, Courier); more distinctive fonts will look approximated. The
  natural next step is caching embedded font files per document and
  passing `fontfile=` to `insert_textbox`.
- **Rotation and letter-spacing are not yet extracted** — `rotation`
  and `letter_spacing` exist in the model but are placeholders (see
  comments in `pdf_parser.py` for exactly where to extend this using
  `get_text("rawdict")`).
- **Redaction background fill is a heuristic** (samples the strip just
  above the text box), not a guaranteed pixel-perfect background match
  on complex/gradient backgrounds.
- **Storage is in-memory + local disk**, single process, no auth, no
  per-user isolation, resets on restart. That's the whole `storage.py`
  module, on purpose, so it's a clean single place to swap in
  PostgreSQL + object storage.
- **No OCR yet** — scanned PDFs currently produce a document with
  images but no editable text.

## How this maps onto the full spec

| Full spec piece | Status |
|---|---|
| Real text extraction & editable model | ✅ this pass |
| Real PDF reconstruction (redact + reinsert) | ✅ this pass |
| Search & replace | ✅ this pass |
| Font embedding (not just matching) | 🔜 next |
| Paragraph reflow | 🔜 next |
| OCR for scanned PDFs (Tesseract/PaddleOCR) | 🔜 next |
| Desktop editor UI (toolbar/pages/canvas/properties) | 🔜 next |
| Images/shapes/annotations/signature/redaction UI | 🔜 next |
| Auth, projects, autosave | 🔜 next |
| PostgreSQL + Redis + workers + Railway deploy | 🔜 later |

## Project layout

```
backend/
  app/
    main.py              FastAPI routes
    models.py             DocumentModel / TextElement / ImageElement schemas
    pdf_parser.py          PDF -> DocumentModel (the extraction logic)
    pdf_reconstructor.py   DocumentModel + edits -> real PDF (the reconstruction logic)
    search.py              search/replace over the document model
    storage.py              local dev storage (swap point for DB + object storage)
  requirements.txt
  railway.json             Railway build/start config for this service
  Procfile                 fallback start command (railway.json takes priority)
frontend/
  server.js                Express static server; injects API_BASE_URL at runtime via /config.js
  package.json
  railway.json             Railway build/start config for this service
  public/
    index.html             functional test UI (not the final desktop editor)
```

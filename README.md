# PDF Editor (backend hardened + full live editor UI)

This build has two layers:

1. **Backend (`backend/`)** — real PDF editing with PyMuPDF: text edit,
   drawings/shapes/highlights, new text boxes, image add/move/resize,
   and page rotate/delete/reorder. Every one of these is baked into a
   real, downloadable PDF — none of it is preview-only.
2. **Frontend (`frontend/public/`)** — a professional live editor:
   `pdf.js` renders the real original page as the visual background,
   `Fabric.js` renders an interactive overlay on top (drag/resize/
   rotate/select), with a top bar, page-thumbnail sidebar (drag to
   reorder, rotate, delete), a tool row (select/text/draw/line/arrow/
   shape/highlight/image/search), a contextual properties panel, and a
   status bar. Undo/redo, autosave, and Save/Download all operate on
   one unambiguous state object that's kept in sync with what actually
   gets exported.

The previous version of this project was a **text-only backend** with a
bare-bones test harness frontend (kept for reference at
`frontend/public/legacy-test-harness.html`) — none of the drawing/
image/page-management/undo-redo functionality existed before this pass.

## What this actually does

- Parses a real PDF with PyMuPDF and extracts every run of text as a
  structured element with its **real** bounding box, font name, size,
  weight, style, and color — straight from the PDF content stream, not
  guessed from rendering.
- Lets you edit any text element's content in place (click to edit,
  right on the real rendered page), run search/replace across the
  whole document, draw freehand, add lines/arrows/rectangles/
  highlights, add new text boxes and images, move/resize/rotate
  existing images, and rotate/delete/reorder whole pages.
- On export, everything above is baked into a real PDF: text edits are
  **redacted** (a real content-stream operation, not a visual cover-up)
  and reinserted; drawings/shapes/new text/images are inserted as real
  vector/text/image PDF content; page operations set the real
  `/Rotate` entry and reorder/delete pages via PyMuPDF's `Document.select()`.
  Every element you didn't touch — other text, images, vector graphics,
  untouched pages — is left completely alone.
- Download always reflects the current live state, even if you forgot
  to click Save first — see `_ensure_export_is_current` in `main.py`.

## Known, documented limitations (not hidden)

- **Live preview font rendering is an approximation.** While you're
  editing a text span, the on-screen preview uses a generic font
  (matched by weight/style/serif-vs-sans) rather than the PDF's exact
  embedded font — the **final exported PDF** uses PyMuPDF's real font
  matching and is more accurate than the live preview. This is a
  common tradeoff in browser-based PDF editors; a pixel-exact live
  preview would need embedding/rendering the original font in-browser.
- **Arrow tool**: the live preview shows a plain line while dragging;
  the arrowhead only appears in the exported PDF. Cosmetic only.
- **Rotation is locked** on drawings, lines/arrows, rectangles, and
  highlights in this pass (only images and text boxes support rotate)
  — this was a deliberate scope cut to avoid shipping unverified
  transform-matrix math (see "I could not test-run this" below).
- **Moved images**: if you move an existing image, then navigate away
  and back without ever exporting, the live preview is cached
  client-side so it still looks right; a moved image's real pixels are
  always re-extracted from the original PDF at export time regardless
  (`source_xref`), so the exported file is correct even if a preview
  cache were ever missing.
- Text edits still don't reflow paragraphs (documented in the original
  pass 1 notes below) — long replacement text auto-shrinks to fit the
  box rather than wrapping to a new line.

## I could not test-run any of this myself

The sandbox I built this in has no network access, so I couldn't `pip
install` PyMuPDF/FastAPI, run `npm`, or load the frontend in an actual
browser. I compiled every Python file and syntax-checked the JS with
Node, and reasoned through the PyMuPDF/Fabric.js/pdf.js APIs carefully
against my knowledge of them, catching and fixing several real bugs
that way (a page-rotation double-counting bug, and a Fabric.js
Line/Polyline coordinate bug, among others) — but "carefully reasoned
through" is not the same as "verified passing in a browser." Please
run it, and treat the first real test pass as expected to surface a
few issues — that's normal for a build this size done without a
runtime to check it against, not a sign the approach is wrong.

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
| Real text extraction & editable model | ✅ |
| Real PDF reconstruction (redact + reinsert) | ✅ |
| Search & replace | ✅ |
| Live editor UI (top bar/sidebar/canvas/properties/status bar) | ✅ this pass |
| Drawing, lines/arrows/rectangles, highlights | ✅ this pass (baked into real PDF content) |
| New text boxes, image add/move/resize | ✅ this pass (baked into real PDF content) |
| Page rotate/delete/reorder, thumbnails, drag-reorder | ✅ this pass |
| Undo/redo | ✅ this pass (full-state snapshots) |
| Save / Export / Download never stale | ✅ this pass |
| Font embedding (not just matching) | 🔜 next |
| Paragraph reflow | 🔜 next |
| OCR for scanned PDFs (Tesseract/PaddleOCR) | 🔜 next |
| Signature tool, redaction-as-a-feature (vs. as an internal mechanism) | 🔜 next |
| Auth, projects, per-user storage | 🔜 next |
| PostgreSQL + Redis + workers + Railway deploy | 🔜 later |

## Project layout

```
backend/
  app/
    main.py              FastAPI routes (documents, elements, search/replace,
                          state, export, download)
    models.py             DocumentModel / TextElement / ImageElement schemas,
                          plus EditorState / PageOps / CanvasObject (drawings,
                          shapes, highlights, new text, images)
    pdf_parser.py          PDF -> DocumentModel (the extraction logic)
    pdf_reconstructor.py   DocumentModel + EditorState -> real PDF (redact/
                          reinsert text, draw shapes, insert images, rotate/
                          delete/reorder pages)
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
    index.html              the live editor shell (top bar, sidebar, canvas, context panel, toolbar, status bar)
    editor.css               design tokens + layout + responsive/mobile styles
    editor.js                 all editor logic: pdf.js rendering, Fabric.js overlay,
                              tools, undo/redo, save/export/download
    legacy-test-harness.html   the earlier plain-text-only test UI, kept for reference
```

The editor loads `pdf.js` and `Fabric.js` from cdnjs at runtime (see the
`<script>` tags at the bottom of `index.html`) — the deployed frontend
needs outbound access to `cdnjs.cloudflare.com` in the user's browser
(this is normal for any site using a CDN; it's not a server-side
dependency, so it doesn't affect your Railway build).

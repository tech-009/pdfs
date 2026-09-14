"""
Local-dev API for the PDF text-editing core.

Run with:
    uvicorn app.main:app --reload --port 8000

Endpoints:
    POST   /documents                          upload a PDF, get back the parsed document model
    GET    /documents/{document_id}             get the current document model (edits applied to `text`)
    PATCH  /documents/{document_id}/elements/{element_id}   edit one text element
    POST   /documents/{document_id}/search       search all text elements
    POST   /documents/{document_id}/replace      replace across all text elements
    PUT    /documents/{document_id}/state        save the full editor state (drawings, shapes,
                                                   images, new text, page rotate/delete/reorder)
    POST   /documents/{document_id}/export       reconstruct a real PDF with all edits applied
    GET    /documents/{document_id}/download     download the edited PDF (always regenerates
                                                   if the state changed since the last export)

See ../README.md for setup, limitations, and how this maps onto the
full production architecture (auth, DB, queue, OCR, Railway).
"""
from __future__ import annotations

import copy
import logging
import os

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from . import search as search_module
from .models import (
    DocumentModel,
    TextEditRequest,
    SearchRequest,
    SearchMatch,
    ReplaceRequest,
    ReplaceResponse,
    ExportResponse,
    EditorState,
    SaveStateResponse,
)
from .pdf_parser import parse_pdf
from .pdf_reconstructor import export_pdf, PdfExportError
from .storage import store, DocumentRecord

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pdf_editor")

app = FastAPI(title="PDF Editor Core API", version="0.1.0")

# CORS origins come from an env var so the deployed frontend's real
# domain can be locked in without a code change. Defaults to "*" so
# local dev and first-deploy testing work with zero configuration —
# set ALLOWED_ORIGINS (comma-separated) once you know your frontend's
# Railway domain, e.g. "https://pdf-editor-frontend.up.railway.app".
_allowed_origins_env = os.getenv("ALLOWED_ORIGINS", "*").strip()
_allowed_origins = (
    ["*"] if _allowed_origins_env == "*"
    else [o.strip() for o in _allowed_origins_env.split(",") if o.strip()]
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
    # Without this, the browser silently hides Content-Disposition on
    # cross-origin responses (frontend and backend are different Railway
    # domains) — the frontend has a same-convention fallback filename if
    # this header isn't readable, but exposing it avoids relying on that.
    expose_headers=["Content-Disposition"],
)

MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50MB — adjust for real usage


def _document_with_edits(record: DocumentRecord) -> DocumentModel:
    """Returns a copy of the parsed document model with `text` and
    `edited` reflecting any edits made so far, without mutating the
    stored original parse (which we keep pristine for diffing/export)."""
    doc = copy.deepcopy(record.document)
    for page in doc.pages:
        for el in page.text_elements:
            if el.id in record.edits:
                el.text = record.edits[el.id]
                el.edited = True
    return doc


@app.post("/documents", response_model=DocumentModel)
async def upload_document(file: UploadFile = File(...)):
    if file.content_type not in ("application/pdf", "application/octet-stream"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    contents = await file.read()
    if len(contents) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File exceeds the maximum allowed size.")
    if not contents.startswith(b"%PDF"):
        raise HTTPException(status_code=400, detail="This does not look like a valid PDF file.")

    document_id = store.new_document_id()
    original_path = store.save_upload(document_id, contents)

    try:
        document = parse_pdf(original_path, document_id, file.filename or "document.pdf")
    except Exception as exc:
        logger.exception("Failed to parse uploaded PDF")
        # Deliberately generic message to the client — never leak internals.
        # parse_pdf/fitz.open always close their own Document handle even
        # on failure (see pdf_parser.py), so no fd/memory leak here.
        raise HTTPException(status_code=422, detail="Could not process this PDF. It may be corrupted, encrypted, or password-protected.") from exc

    store.put(document_id, DocumentRecord(document=document, original_path=original_path))
    return document


@app.get("/documents/{document_id}", response_model=DocumentModel)
async def get_document(document_id: str):
    record = store.get(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return _document_with_edits(record)


@app.patch("/documents/{document_id}/elements/{element_id}")
async def edit_element(document_id: str, element_id: str, body: TextEditRequest):
    record = store.get(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")

    exists = any(
        el.id == element_id
        for page in record.document.pages
        for el in page.text_elements
    )
    if not exists:
        raise HTTPException(status_code=404, detail="Text element not found on this document.")

    record.edits[element_id] = body.text
    record.edits_version += 1  # marks any existing export as stale
    return {"element_id": element_id, "text": body.text}


@app.post("/documents/{document_id}/search", response_model=list[SearchMatch])
async def search_document(document_id: str, body: SearchRequest):
    record = store.get(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")
    return search_module.search(record.document, record.edits, body.query, body.case_sensitive, body.whole_word)


@app.post("/documents/{document_id}/replace", response_model=ReplaceResponse)
async def replace_in_document(document_id: str, body: ReplaceRequest):
    record = store.get(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")

    count, updated_ids = search_module.replace(
        record.document,
        record.edits,
        body.query,
        body.replacement,
        body.case_sensitive,
        body.whole_word,
        body.replace_all,
    )
    if updated_ids:
        record.edits_version += 1  # marks any existing export as stale
    return ReplaceResponse(replaced_count=count, updated_element_ids=updated_ids)


@app.put("/documents/{document_id}/state", response_model=SaveStateResponse)
async def save_state(document_id: str, body: EditorState):
    """Saves the FULL live-editor state in one call: text edits, every
    drawing/shape/highlight/new-text/image object per page, and any page
    rotate/delete/reorder. The frontend sends this as a whole blob (not
    incremental ops), debounced during editing and always flushed
    immediately before Export/Download, so there is always exactly one
    unambiguous state to bake into the final PDF."""
    record = store.get(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")

    record.edits = dict(body.text_edits)
    record.objects_by_page = {k: list(v) for k, v in body.objects_by_page.items()}
    record.page_ops = body.page_ops
    record.edits_version += 1  # marks any existing export as stale
    return SaveStateResponse(document_id=document_id, edits_version=record.edits_version)


def _download_filename(original_filename: str) -> str:
    stem, ext = os.path.splitext(original_filename or "document.pdf")
    if ext.lower() != ".pdf":
        ext = ".pdf"
    return f"{stem}-edited{ext}"


async def _ensure_export_is_current(document_id: str, record: DocumentRecord) -> str:
    """Regenerates the exported PDF from the live edits dict if (and only
    if) it's out of date, then returns the path to a file that is
    guaranteed to reflect the current edit state. This is the single
    choke point that prevents a stale/old-version download: nothing
    downstream of this function ever sees a path written from an older
    `edits` snapshot than the one in memory right now."""
    async with record.export_lock:
        # Re-check inside the lock: another concurrent request may have
        # just finished the exact export we were about to do.
        if record.exported_path and record.exported_edits_version == record.edits_version and os.path.exists(record.exported_path):
            return record.exported_path

        output_path = store.export_path_for(document_id)
        # Freeze the full state we're exporting so nothing mutates mid-write.
        state_snapshot = EditorState(
            text_edits=dict(record.edits),
            page_ops=record.page_ops,
            objects_by_page={k: list(v) for k, v in record.objects_by_page.items()},
        )
        target_version = record.edits_version
        try:
            export_pdf(record.original_path, output_path, record.document, state_snapshot)
        except PdfExportError as exc:
            logger.exception("Failed to export PDF: %s", exc)
            raise HTTPException(status_code=500, detail="Failed to generate the edited PDF. Please try again.") from exc
        except Exception as exc:
            logger.exception("Unexpected error exporting PDF")
            raise HTTPException(status_code=500, detail="Failed to generate the edited PDF. Please try again.") from exc

        record.exported_path = output_path
        record.exported_edits_version = target_version
        return output_path


@app.post("/documents/{document_id}/export", response_model=ExportResponse)
async def export_document(document_id: str):
    record = store.get(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")

    await _ensure_export_is_current(document_id, record)
    return ExportResponse(document_id=document_id, download_url=f"/documents/{document_id}/download")


@app.get("/documents/{document_id}/download")
async def download_document(document_id: str):
    record = store.get(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")

    # Always regenerate if edits have changed since the last export —
    # download never hands back a version older than the current edit
    # state, even if the client forgot to call /export again first.
    output_path = await _ensure_export_is_current(document_id, record)
    return FileResponse(
        output_path,
        media_type="application/pdf",
        filename=_download_filename(record.document.filename),
        # The URL is identical across repeated downloads of the same
        # document even though the file's bytes change after every new
        # edit — without this, a browser (or any intermediary cache) can
        # legally serve a stale previous download instead of re-fetching,
        # which is exactly the "second download still has the old edit"
        # bug. no-store forces every download click to hit the server.
        headers={"Cache-Control": "no-store, no-cache, must-revalidate"},
    )


@app.get("/health")
async def health():
    return {"status": "ok"}

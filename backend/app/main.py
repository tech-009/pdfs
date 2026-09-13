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
    POST   /documents/{document_id}/export       reconstruct a real PDF with edits applied
    GET    /documents/{document_id}/download     download the exported PDF

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
)
from .pdf_parser import parse_pdf
from .pdf_reconstructor import export_pdf
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
    return ReplaceResponse(replaced_count=count, updated_element_ids=updated_ids)


@app.post("/documents/{document_id}/export", response_model=ExportResponse)
async def export_document(document_id: str):
    record = store.get(document_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Document not found.")

    output_path = store.export_path_for(document_id)
    try:
        export_pdf(record.original_path, output_path, record.document, record.edits)
    except Exception as exc:
        logger.exception("Failed to export PDF")
        raise HTTPException(status_code=500, detail="Failed to generate the edited PDF.") from exc

    record.exported_path = output_path
    return ExportResponse(document_id=document_id, download_url=f"/documents/{document_id}/download")


@app.get("/documents/{document_id}/download")
async def download_document(document_id: str):
    record = store.get(document_id)
    if record is None or not record.exported_path or not os.path.exists(record.exported_path):
        raise HTTPException(status_code=404, detail="No exported file available. Export the document first.")
    return FileResponse(
        record.exported_path,
        media_type="application/pdf",
        filename=f"edited_{record.document.filename}",
    )


@app.get("/health")
async def health():
    return {"status": "ok"}

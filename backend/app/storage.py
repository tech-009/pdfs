"""
Minimal local storage for this local/dev build.

Explicitly NOT the production storage layer described in the original
spec (PostgreSQL for metadata + object storage for files, per-user
isolation, signed downloads). This module exists so the API has
somewhere to keep the uploaded file, the parsed DocumentModel, and any
edits between requests while you run it on your own machine.

Swapping this out later:
  - `documents` dict -> a `documents` table (id, filename, created_at, owner_id)
  - `edits` dict -> an `edits` table or a JSONB column, keyed by document_id
  - local `storage/` folder -> S3-compatible bucket (Railway volumes / R2 / S3),
    with the DocumentStore interface below unchanged so callers don't need to change.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional

from .models import DocumentModel

STORAGE_DIR = os.path.join(os.path.dirname(__file__), "..", "storage")
os.makedirs(STORAGE_DIR, exist_ok=True)


@dataclass
class DocumentRecord:
    document: DocumentModel
    original_path: str
    edits: Dict[str, str] = field(default_factory=dict)  # element_id -> new text
    exported_path: Optional[str] = None


class DocumentStore:
    """Process-memory store. Restarting the server loses all documents —
    acceptable for local dev/testing of the editing core, not for production."""

    def __init__(self) -> None:
        self._records: Dict[str, DocumentRecord] = {}

    def new_document_id(self) -> str:
        return uuid.uuid4().hex

    def save_upload(self, document_id: str, file_bytes: bytes) -> str:
        path = os.path.join(STORAGE_DIR, f"{document_id}_original.pdf")
        # Path is derived entirely from a server-generated uuid, never
        # from user-supplied input, so this is not path-traversable.
        with open(path, "wb") as f:
            f.write(file_bytes)
        return path

    def put(self, document_id: str, record: DocumentRecord) -> None:
        self._records[document_id] = record

    def get(self, document_id: str) -> Optional[DocumentRecord]:
        return self._records.get(document_id)

    def export_path_for(self, document_id: str) -> str:
        return os.path.join(STORAGE_DIR, f"{document_id}_export.pdf")


store = DocumentStore()

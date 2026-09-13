"""
Document model schemas.

This is the internal editable representation described in the spec:
Document -> Page -> {text elements, image elements}.

Kept intentionally flat and typed so it can be extended later with
vector elements, form fields, and annotations without breaking the
existing shape.
"""
from __future__ import annotations

from typing import List, Optional, Literal
from pydantic import BaseModel, Field


class TextElement(BaseModel):
    id: str  # stable id: "p{page}_b{block}_l{line}_s{span}"
    text: str
    x: float
    y: float
    width: float
    height: float
    font_family: str          # raw font name as embedded in the PDF
    mapped_font: str          # closest available reconstruction font (PyMuPDF base font code)
    font_size: float
    font_weight: Literal["normal", "bold"]
    font_style: Literal["normal", "italic"]
    color: str                 # hex, e.g. "#111111"
    opacity: float = 1.0
    alignment: Literal["left", "center", "right", "justify"] = "left"
    letter_spacing: float = 0.0     # not reliably extractable from PDF spans; extension point
    line_height: float             # approximated from span bbox height
    rotation: float = 0.0          # 0 for the common horizontal-text case; see parser notes
    original_pdf_object_id: str    # block/line/span index path, for traceability
    edited: bool = False           # true once the user has changed `text` from original


class ImageElement(BaseModel):
    id: str
    x: float
    y: float
    width: float
    height: float
    xref: int                  # PDF internal image object reference
    rotation: float = 0.0
    opacity: float = 1.0
    # Replace/resize/reposition are stubbed for this pass (see README) —
    # the id/xref/bbox are already enough to support that as a follow-up.


class Page(BaseModel):
    number: int                 # 0-indexed
    width: float
    height: float
    text_elements: List[TextElement]
    image_elements: List[ImageElement]


class DocumentModel(BaseModel):
    id: str
    filename: str
    page_count: int
    pages: List[Page]


class TextEditRequest(BaseModel):
    text: str


class SearchRequest(BaseModel):
    query: str
    case_sensitive: bool = False
    whole_word: bool = False


class SearchMatch(BaseModel):
    element_id: str
    page: int
    text: str


class ReplaceRequest(BaseModel):
    query: str
    replacement: str
    case_sensitive: bool = False
    whole_word: bool = False
    replace_all: bool = True   # if False, only the first match is replaced


class ReplaceResponse(BaseModel):
    replaced_count: int
    updated_element_ids: List[str]


class ExportResponse(BaseModel):
    document_id: str
    download_url: str

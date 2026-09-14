"""
Document model schemas.

This is the internal editable representation described in the spec:
Document -> Page -> {text elements, image elements}.

Kept intentionally flat and typed so it can be extended later with
vector elements, form fields, and annotations without breaking the
existing shape.
"""
from __future__ import annotations

from typing import Annotated, Dict, List, Optional, Literal
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


# ---------------------------------------------------------------------------
# Canvas state — everything added in the live editor beyond plain text edits:
# drawings, shapes, new text boxes, image add/move/resize, highlights, and
# page-level operations (rotate/delete/reorder). This is the payload the
# frontend PUTs to /documents/{id}/state, and it's what export_pdf() bakes
# into the real, final PDF — nothing here is preview-only.
# ---------------------------------------------------------------------------

class DrawingObject(BaseModel):
    """A freehand pen stroke, in original-page point space."""
    type: Literal["drawing"] = "drawing"
    id: str
    points: List[List[float]]   # [[x, y], ...] polyline
    color: str = "#111111"
    stroke_width: float = 2.0
    opacity: float = 1.0


class ShapeObject(BaseModel):
    """A line, rectangle, or arrow, in original-page point space."""
    type: Literal["shape"] = "shape"
    id: str
    shape: Literal["line", "rect", "arrow"]
    x1: float
    y1: float
    x2: float
    y2: float
    color: str = "#111111"
    stroke_width: float = 2.0
    fill: Optional[str] = None       # hex fill color for "rect", or None for outline-only
    opacity: float = 1.0


class HighlightObject(BaseModel):
    type: Literal["highlight"] = "highlight"
    id: str
    x: float
    y: float
    width: float
    height: float
    color: str = "#ffeb3b"
    opacity: float = 0.4


class NewTextObject(BaseModel):
    """A brand-new text box added by the user (distinct from editing an
    existing extracted TextElement, which goes through `text_edits`)."""
    type: Literal["text"] = "text"
    id: str
    text: str
    x: float
    y: float
    width: float
    height: float
    font_size: float = 14.0
    color: str = "#111111"
    bold: bool = False
    italic: bool = False
    underline: bool = False
    align: Literal["left", "center", "right"] = "left"
    opacity: float = 1.0


class ImageObject(BaseModel):
    """Either a brand-new image the user added (`image_data` is a base64
    data URL), or an existing extracted image being moved/resized
    (`source_xref` refers to the original PDF image object, extracted and
    reinserted at the new position/size on export)."""
    type: Literal["image"] = "image"
    id: str
    x: float
    y: float
    width: float
    height: float
    rotation: float = 0.0
    opacity: float = 1.0
    image_data: Optional[str] = None    # data URL, e.g. "data:image/png;base64,..."
    source_xref: Optional[int] = None   # set instead of image_data when moving an existing image
    # When moving/resizing an existing image, also carry its original rect so
    # export can redact the old position before drawing it at the new one.
    original_x: Optional[float] = None
    original_y: Optional[float] = None
    original_width: Optional[float] = None
    original_height: Optional[float] = None


CanvasObjectUnion = Annotated[
    DrawingObject | ShapeObject | HighlightObject | NewTextObject | ImageObject,
    Field(discriminator="type"),
]


class PageOps(BaseModel):
    """Page-level operations, keyed by ORIGINAL page index (before any
    deletion/reordering — export applies content edits first, then
    resolves final order/deletion last via a single reindex pass)."""
    # Final page order, as a list of original page indices; an original
    # index simply absent from this list means "deleted". E.g. for a
    # 3-page doc, [2, 0] means: final page 1 = original page 3 (rotated/
    # edited as specified), final page 2 = original page 1, original
    # page 2 is deleted.
    order: List[int]
    # original_page_index -> rotation DELTA in degrees (0/90/180/270) the
    # user applied in the editor, added on top of whatever rotation the
    # page already had (see pdf_reconstructor._apply_page_ops).
    rotations: Dict[int, int] = Field(default_factory=dict)


class EditorState(BaseModel):
    """The full live-editor state for one document. Sent as a whole blob
    (not incremental ops) so Save/Export/Download always have one
    unambiguous source of truth to flush and bake, keyed by version for
    staleness checks."""
    text_edits: Dict[str, str] = Field(default_factory=dict)
    page_ops: Optional[PageOps] = None
    # original_page_index (as string, for JSON key compatibility) -> objects
    objects_by_page: Dict[str, List[CanvasObjectUnion]] = Field(default_factory=dict)


class SaveStateResponse(BaseModel):
    document_id: str
    edits_version: int
    status: Literal["saved"] = "saved"

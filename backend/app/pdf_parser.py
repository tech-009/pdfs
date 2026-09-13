"""
Parses a real PDF into the internal DocumentModel using PyMuPDF (fitz).

This is the piece that satisfies "do not fake text editing with an HTML
overlay": we read PyMuPDF's structured text dict, which gives us, per
span of text that shares the same formatting:
  - the exact bounding box (bbox) in PDF page-space
  - the font name as embedded/referenced in the PDF
  - font size
  - a flags bitfield encoding italic / serif / monospace / bold
  - a packed sRGB integer color

That's enough to reconstruct a very close visual match on export.
Known, documented limitations (kept honest rather than papered over):
  - Rotated text: PyMuPDF's plain-text dict reports span bboxes in
    unrotated page space for the common horizontal-text case. Pages
    with rotated text blocks are still parsed, but `rotation` on the
    TextElement is a placeholder (0.0) until a follow-up pass adds
    matrix-based rotation extraction (page.get_text("rawdict") plus
    the text matrix would be the extension point).
  - Letter-spacing: not exposed at the span level by PyMuPDF; defaults
    to 0.0. Extension point: derive from per-character positions in
    "rawdict" mode if exact reproduction is required.
  - Alignment: inferred as "left" by default since PDF has no native
    paragraph-alignment concept once text is laid out; a heuristic
    (comparing line start/end x against block bbox) could be added
    later but would be a guess, not extracted fact.
"""
from __future__ import annotations

from typing import Tuple
import fitz  # PyMuPDF

from .models import DocumentModel, Page, TextElement, ImageElement

# PyMuPDF span flag bits (see PyMuPDF docs: Page.get_text("dict")):
FLAG_SUPERSCRIPT = 1 << 0
FLAG_ITALIC = 1 << 1
FLAG_SERIFED = 1 << 2
FLAG_MONOSPACED = 1 << 3
FLAG_BOLD = 1 << 4

# PyMuPDF's built-in "base 14" font codes we can always render without
# needing to embed/subset a font ourselves. We map every extracted font
# to the closest one of these for reconstruction. If the original font
# happens to be one of the standard 14 already, we still route through
# this map for consistency; embedding the *original* font file back in
# is the natural follow-up (PyMuPDF supports inserting embedded fonts
# via `fontfile=`) once a font-file cache is added.
def map_font(font_family: str, bold: bool, italic: bool, monospaced: bool, serifed: bool) -> str:
    name = (font_family or "").lower()

    if monospaced or "courier" in name or "mono" in name or "consol" in name:
        base = "co"
        if bold and italic:
            return "cobi"
        if bold:
            return "cobo"
        if italic:
            return "coit"
        return "cour"

    if serifed or "times" in name or "georgia" in name or "garamond" in name or "serif" in name:
        if bold and italic:
            return "tibi"
        if bold:
            return "tibo"
        if italic:
            return "tiit"
        return "tiro"  # Times-Roman

    # default: sans-serif (Helvetica family) — matches Arial/Calibri/etc.
    if bold and italic:
        return "hebi"
    if bold:
        return "hebo"
    if italic:
        return "heit"
    return "helv"


def _color_int_to_hex(color_int: int) -> str:
    r = (color_int >> 16) & 255
    g = (color_int >> 8) & 255
    b = color_int & 255
    return f"#{r:02x}{g:02x}{b:02x}"


def _decode_flags(flags: int) -> Tuple[bool, bool, bool, bool]:
    bold = bool(flags & FLAG_BOLD)
    italic = bool(flags & FLAG_ITALIC)
    serifed = bool(flags & FLAG_SERIFED)
    monospaced = bool(flags & FLAG_MONOSPACED)
    return bold, italic, serifed, monospaced


def parse_pdf(path: str, document_id: str, filename: str) -> DocumentModel:
    doc = fitz.open(path)
    try:
        return _parse_opened_pdf(doc, document_id, filename)
    finally:
        # Always released, even if parsing raises partway through a page —
        # otherwise a bad upload leaks a file handle every time.
        doc.close()


def _parse_opened_pdf(doc: "fitz.Document", document_id: str, filename: str) -> DocumentModel:
    if doc.is_encrypted:
        # fitz.open() succeeds even on a password-protected file; it just
        # can't read content until authenticated. Fail clearly here instead
        # of returning a document with zero text elements and no explanation.
        raise ValueError("PDF is password-protected/encrypted.")

    pages: list[Page] = []

    for page_index in range(doc.page_count):
        page = doc[page_index]
        text_dict = page.get_text("dict")
        text_elements: list[TextElement] = []

        for block_no, block in enumerate(text_dict.get("blocks", [])):
            if block.get("type") != 0:
                continue  # non-text (image) block, handled separately below
            for line_no, line in enumerate(block.get("lines", [])):
                for span_no, span in enumerate(line.get("spans", [])):
                    text = span.get("text", "")
                    if text.strip() == "":
                        # keep purely-whitespace spans out of the editable
                        # model; they carry no meaningful edit target
                        continue

                    bold, italic, serifed, monospaced = _decode_flags(span.get("flags", 0))
                    font_family = span.get("font", "unknown")
                    bbox = span.get("bbox", [0, 0, 0, 0])
                    x0, y0, x1, y1 = bbox
                    font_size = float(span.get("size", 12.0))

                    element_id = f"p{page_index}_b{block_no}_l{line_no}_s{span_no}"

                    text_elements.append(
                        TextElement(
                            id=element_id,
                            text=text,
                            x=x0,
                            y=y0,
                            width=max(x1 - x0, 1.0),
                            height=max(y1 - y0, font_size),
                            font_family=font_family,
                            mapped_font=map_font(font_family, bold, italic, monospaced, serifed),
                            font_size=font_size,
                            font_weight="bold" if bold else "normal",
                            font_style="italic" if italic else "normal",
                            color=_color_int_to_hex(span.get("color", 0)),
                            opacity=1.0,
                            alignment="left",
                            letter_spacing=0.0,
                            line_height=max(y1 - y0, font_size * 1.2),
                            rotation=0.0,
                            original_pdf_object_id=f"block{block_no}/line{line_no}/span{span_no}",
                            edited=False,
                        )
                    )

        image_elements: list[ImageElement] = []
        for img_index, img in enumerate(page.get_images(full=True)):
            xref = img[0]
            try:
                rects = page.get_image_rects(xref)
            except Exception:
                rects = []
            for r_index, rect in enumerate(rects):
                image_elements.append(
                    ImageElement(
                        id=f"p{page_index}_img{img_index}_{r_index}",
                        x=rect.x0,
                        y=rect.y0,
                        width=rect.width,
                        height=rect.height,
                        xref=xref,
                        rotation=0.0,
                        opacity=1.0,
                    )
                )

        pages.append(
            Page(
                number=page_index,
                width=page.rect.width,
                height=page.rect.height,
                text_elements=text_elements,
                image_elements=image_elements,
            )
        )

    return DocumentModel(
        id=document_id,
        filename=filename,
        page_count=len(pages),
        pages=pages,
    )

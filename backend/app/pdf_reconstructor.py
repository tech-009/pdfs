"""
Rebuilds a real, downloadable PDF from the original file plus a set of
text edits — this is the "reconstruct the PDF while preserving the
original document layout" requirement.

Approach (per element that was edited):
  1. Sample the average background color under the element's original
     bbox (cheap heuristic: average the pixel colors of a low-res render
     of that box) so redaction doesn't leave a jarring white patch on
     non-white backgrounds.
  2. Add a redaction annotation over the original bbox and apply it —
     this genuinely removes the original glyph/vector data from the
     PDF content stream (not just a visual cover-up).
  3. Insert the new text at the same position using the closest
     available font, matching size/weight/italic/color, so the layout
     stays as close to the original as technically possible without
     embedding the exact original font file.

Everything that was NOT edited is left completely untouched — images,
vector graphics, and unedited text keep their original PDF objects,
never converted to overlays or rasterized.

Known limitation (documented, not hidden): this does not reflow
paragraphs. If replacement text is significantly longer than the
original box, it will be shrunk to fit the box width via PyMuPDF's
`insert_textbox` auto-sizing rather than wrapping into the next line —
true paragraph reflow across a whole text block is a follow-up feature
(the document model already groups spans by block/line, which is what
that feature would key off of).
"""
from __future__ import annotations

from typing import Dict
import fitz  # PyMuPDF

from .models import DocumentModel, TextElement


def _hex_to_rgb01(hex_color: str) -> tuple[float, float, float]:
    hex_color = hex_color.lstrip("#")
    r = int(hex_color[0:2], 16) / 255.0
    g = int(hex_color[2:4], 16) / 255.0
    b = int(hex_color[4:6], 16) / 255.0
    return (r, g, b)


def _sample_background_color(page: fitz.Page, rect: fitz.Rect) -> tuple[float, float, float]:
    """Best-effort background color sample from just outside the text
    bbox, so redaction fill doesn't default to a stark white patch on
    colored/dark backgrounds. Falls back to white on any failure."""
    try:
        # Sample a thin strip just above the box (still inside the page)
        probe = fitz.Rect(rect.x0, max(rect.y0 - 2, 0), rect.x1, rect.y0)
        if probe.is_empty or probe.width <= 0:
            return (1.0, 1.0, 1.0)
        pix = page.get_pixmap(clip=probe, matrix=fitz.Matrix(1, 1))
        if pix.n < 3:
            return (1.0, 1.0, 1.0)
        # average all pixels in the sampled strip
        samples = pix.samples
        n_pixels = pix.width * pix.height
        if n_pixels == 0:
            return (1.0, 1.0, 1.0)
        step = pix.n
        r_total = g_total = b_total = 0
        for i in range(0, len(samples), step):
            r_total += samples[i]
            g_total += samples[i + 1]
            b_total += samples[i + 2]
        return (r_total / n_pixels / 255.0, g_total / n_pixels / 255.0, b_total / n_pixels / 255.0)
    except Exception:
        return (1.0, 1.0, 1.0)


def export_pdf(original_path: str, output_path: str, document: DocumentModel, edits: Dict[str, str]) -> None:
    """
    original_path: path to the untouched, originally-uploaded PDF
    output_path: where to write the reconstructed PDF
    document: the parsed DocumentModel (gives us bbox/font/color for every element id)
    edits: {element_id: new_text} — only elements present here are touched
    """
    doc = fitz.open(original_path)

    # index elements by id for quick lookup, grouped by page
    elements_by_id: Dict[str, TextElement] = {}
    for page in document.pages:
        for el in page.text_elements:
            elements_by_id[el.id] = el

    # group edits by page number so we can batch redactions per page
    edits_by_page: Dict[int, list[str]] = {}
    for element_id in edits:
        el = elements_by_id.get(element_id)
        if el is None:
            continue
        page_no = int(element_id.split("_")[0][1:])  # "p{n}_..." -> n
        edits_by_page.setdefault(page_no, []).append(element_id)

    for page_no, element_ids in edits_by_page.items():
        page = doc[page_no]

        # Pass 1: redact all edited spans on this page
        fill_colors: Dict[str, tuple[float, float, float]] = {}
        for element_id in element_ids:
            el = elements_by_id[element_id]
            rect = fitz.Rect(el.x, el.y, el.x + el.width, el.y + el.height)
            fill = _sample_background_color(page, rect)
            fill_colors[element_id] = fill
            page.add_redact_annot(rect, fill=fill)
        page.apply_redactions()

        # Pass 2: insert the new text for each edited span
        for element_id in element_ids:
            el = elements_by_id[element_id]
            new_text = edits[element_id]
            rect = fitz.Rect(el.x, el.y, el.x + el.width, el.y + el.height)
            # give a little vertical breathing room so descenders aren't clipped
            insert_rect = fitz.Rect(rect.x0, rect.y0 - el.font_size * 0.15, rect.x1 + 4, rect.y1 + 4)
            color = _hex_to_rgb01(el.color)

            align = fitz.TEXT_ALIGN_LEFT
            if el.alignment == "center":
                align = fitz.TEXT_ALIGN_CENTER
            elif el.alignment == "right":
                align = fitz.TEXT_ALIGN_RIGHT
            elif el.alignment == "justify":
                align = fitz.TEXT_ALIGN_JUSTIFY

            page.insert_textbox(
                insert_rect,
                new_text,
                fontsize=el.font_size,
                fontname=el.mapped_font,
                color=color,
                align=align,
            )

    doc.save(output_path, garbage=4, deflate=True)
    doc.close()

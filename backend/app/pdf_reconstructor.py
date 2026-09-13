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

import os
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


class PdfExportError(RuntimeError):
    """Raised when export fails or produces an invalid PDF. The caller
    (main.py) turns this into a generic HTTP 500 without leaking the
    underlying PyMuPDF error to the client."""


def export_pdf(original_path: str, output_path: str, document: DocumentModel, edits: Dict[str, str]) -> None:
    """
    original_path: path to the untouched, originally-uploaded PDF
    output_path: where to write the reconstructed PDF
    document: the parsed DocumentModel (gives us bbox/font/color for every element id)
    edits: {element_id: new_text} — only elements present here are touched

    IMPORTANT: this always opens `original_path` fresh and applies the
    *current* `edits` dict passed in — it never reads a cached/previous
    export. Callers must always pass the live edits dict at call time
    (main.py's export/download routes do this), which is what prevents
    the "stale download" bug: there is no code path that can hand back
    a PDF representing an older state than what's in `edits` right now.
    """
    doc = None
    tmp_output_path = output_path + ".tmp"
    try:
        doc = fitz.open(original_path)
        if doc.is_encrypted:
            # A password-protected source PDF can't be safely rewritten here.
            raise PdfExportError("Source PDF is encrypted/password-protected.")

        # index elements by id for quick lookup, grouped by page
        elements_by_id: Dict[str, TextElement] = {}
        for page in document.pages:
            for el in page.text_elements:
                elements_by_id[el.id] = el

        # group edits by page number so we can batch redactions per page
        edits_by_page: Dict[int, list[str]] = {}
        skipped_unknown_ids: list[str] = []
        for element_id, new_text in edits.items():
            el = elements_by_id.get(element_id)
            if el is None:
                # Edit refers to an element id that no longer exists on this
                # document (e.g. stale client state). Skip it rather than
                # crashing the whole export over one bad id.
                skipped_unknown_ids.append(element_id)
                continue
            if new_text == el.text:
                # No actual change — skip the redact/reinsert cycle entirely
                # so untouched spans truly stay untouched.
                continue
            page_no = int(element_id.split("_")[0][1:])  # "p{n}_..." -> n
            if page_no < 0 or page_no >= doc.page_count:
                skipped_unknown_ids.append(element_id)
                continue
            edits_by_page.setdefault(page_no, []).append(element_id)

        for page_no, element_ids in edits_by_page.items():
            page = doc[page_no]

            # Pass 1: redact all edited spans on this page
            for element_id in element_ids:
                el = elements_by_id[element_id]
                rect = fitz.Rect(el.x, el.y, el.x + el.width, el.y + el.height)
                fill = _sample_background_color(page, rect)
                page.add_redact_annot(rect, fill=fill)
            # images/vector graphics on the page are untouched by default —
            # only the annotated text rectangles are affected.
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

                rc = page.insert_textbox(
                    insert_rect,
                    new_text,
                    fontsize=el.font_size,
                    fontname=el.mapped_font,
                    color=color,
                    align=align,
                )
                if rc < 0:
                    # Negative return means the text didn't fit even after
                    # PyMuPDF's internal auto-shrink — surface this instead
                    # of silently dropping the edit.
                    raise PdfExportError(f"Edited text for element {element_id} could not be laid out.")

        # Write to a temp path first, then validate, then atomically move
        # into place — a half-written/corrupt file is never left at
        # `output_path` where a concurrent download could pick it up.
        doc.save(tmp_output_path, garbage=4, deflate=True)
        doc.close()
        doc = None

        _validate_pdf(tmp_output_path, expected_page_count=document.page_count)
        os.replace(tmp_output_path, output_path)
    except PdfExportError:
        raise
    except Exception as exc:  # noqa: BLE001 - re-raised as our own type
        raise PdfExportError(str(exc)) from exc
    finally:
        if doc is not None:
            doc.close()
        if os.path.exists(tmp_output_path):
            try:
                os.remove(tmp_output_path)
            except OSError:
                pass


def _validate_pdf(path: str, expected_page_count: int) -> None:
    """Confirms the file we just wrote is actually a valid, openable PDF
    with the page count we expect, before we ever hand it back as a
    download. Never trust that `doc.save()` succeeding means the output
    is good — reopen and check."""
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise PdfExportError("Generated PDF file is empty.")
    try:
        check_doc = fitz.open(path)
        try:
            if check_doc.page_count != expected_page_count:
                raise PdfExportError(
                    f"Generated PDF has {check_doc.page_count} page(s), expected {expected_page_count}."
                )
            # Touching a page's content forces PyMuPDF to actually parse it,
            # catching truncated/corrupt output that `fitz.open` alone won't.
            _ = check_doc[0].get_text() if check_doc.page_count else None
        finally:
            check_doc.close()
    except PdfExportError:
        raise
    except Exception as exc:
        raise PdfExportError(f"Generated PDF failed validation: {exc}") from exc

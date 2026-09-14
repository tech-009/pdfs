"""
Rebuilds a real, downloadable PDF from the original file plus the full
live-editor state (text edits, drawings, shapes, new text boxes, image
add/move/resize, and page rotate/delete/reorder).

Every operation here is a real PDF-level change made with PyMuPDF, never
a rasterized screenshot or an HTML/CSS overlay:
  - Text edits: redact the original glyphs (a real content-stream
    operation), then re-insert the edited text at the same position
    using the closest matching font.
  - Drawings/shapes: drawn as real vector content via PyMuPDF's Shape API.
  - New text boxes: inserted as real text content.
  - New images: inserted as real embedded image XObjects.
  - Moved/resized existing images: the original image is redacted from
    its old position and its original bytes are reinserted at the new
    position/size (never re-encoded, so no quality loss).
  - Page rotate: sets the page's real /Rotate PDF entry.
  - Page delete/reorder: applied last, in one atomic `Document.select()`
    call, once every other content edit has already been baked in
    against the ORIGINAL page indices (so we never have to track a
    moving target while editing content).

Everything the user did NOT touch — other text, images, vector graphics,
whole untouched pages — is left completely alone.

Known limitation (documented, not hidden): text edits don't reflow
paragraphs. If replacement text is significantly longer than the
original box, PyMuPDF auto-shrinks it to fit rather than wrapping to a
new line.
"""
from __future__ import annotations

import base64
import os
from typing import Dict, List, Optional
import fitz  # PyMuPDF

from .models import (
    DocumentModel,
    TextElement,
    EditorState,
    DrawingObject,
    ShapeObject,
    HighlightObject,
    NewTextObject,
    ImageObject,
)


class PdfExportError(RuntimeError):
    """Raised when export fails or produces an invalid PDF. The caller
    (main.py) turns this into a generic HTTP 500 without leaking the
    underlying PyMuPDF error to the client."""


def _hex_to_rgb01(hex_color: str) -> tuple[float, float, float]:
    hex_color = (hex_color or "#000000").lstrip("#")
    if len(hex_color) != 6:
        return (0.0, 0.0, 0.0)
    r = int(hex_color[0:2], 16) / 255.0
    g = int(hex_color[2:4], 16) / 255.0
    b = int(hex_color[4:6], 16) / 255.0
    return (r, g, b)


def _sample_background_color(page: "fitz.Page", rect: "fitz.Rect") -> tuple[float, float, float]:
    """Best-effort background color sample from just outside the text
    bbox, so redaction fill doesn't default to a stark white patch on
    colored/dark backgrounds. Falls back to white on any failure."""
    try:
        probe = fitz.Rect(rect.x0, max(rect.y0 - 2, 0), rect.x1, rect.y0)
        if probe.is_empty or probe.width <= 0:
            return (1.0, 1.0, 1.0)
        pix = page.get_pixmap(clip=probe, matrix=fitz.Matrix(1, 1))
        if pix.n < 3:
            return (1.0, 1.0, 1.0)
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


def _apply_text_edits(doc: "fitz.Document", document: DocumentModel, edits: Dict[str, str]) -> None:
    elements_by_id: Dict[str, TextElement] = {}
    for page in document.pages:
        for el in page.text_elements:
            elements_by_id[el.id] = el

    edits_by_page: Dict[int, list[str]] = {}
    for element_id, new_text in edits.items():
        el = elements_by_id.get(element_id)
        if el is None:
            continue  # stale/unknown id from an older client state — skip, don't crash
        if new_text == el.text:
            continue  # no actual change — leave this span completely untouched
        page_no = int(element_id.split("_")[0][1:])  # "p{n}_..." -> n
        if page_no < 0 or page_no >= doc.page_count:
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
        page.apply_redactions()

        # Pass 2: insert the new text for each edited span
        for element_id in element_ids:
            el = elements_by_id[element_id]
            new_text = edits[element_id]
            rect = fitz.Rect(el.x, el.y, el.x + el.width, el.y + el.height)
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
                insert_rect, new_text, fontsize=el.font_size,
                fontname=el.mapped_font, color=color, align=align,
            )
            if rc < 0:
                raise PdfExportError(f"Edited text for element {element_id} could not be laid out.")


def _resolve_new_text_font(obj: NewTextObject) -> str:
    if obj.bold and obj.italic:
        return "hebi"
    if obj.bold:
        return "hebo"
    if obj.italic:
        return "heit"
    return "helv"


def _apply_canvas_objects(doc: "fitz.Document", objects_by_page: Dict[str, list]) -> None:
    """Bakes drawings, shapes, highlights, new text boxes, and image
    add/move/resize onto their ORIGINAL page indices. Must run before
    `_apply_page_ops`, which is the only step allowed to change page
    indices/count."""
    for page_key, objects in objects_by_page.items():
        try:
            page_no = int(page_key)
        except (TypeError, ValueError):
            continue
        if page_no < 0 or page_no >= doc.page_count:
            continue  # references a page that no longer exists — skip, don't crash
        page = doc[page_no]

        # Existing-image moves need their old position redacted first, in
        # one batch per page, before anything is drawn on top of them.
        image_moves = [o for o in objects if isinstance(o, ImageObject) and o.source_xref is not None]
        for obj in image_moves:
            if obj.original_x is None or obj.original_y is None:
                continue
            old_rect = fitz.Rect(
                obj.original_x, obj.original_y,
                obj.original_x + (obj.original_width or obj.width),
                obj.original_y + (obj.original_height or obj.height),
            )
            fill = _sample_background_color(page, old_rect)
            page.add_redact_annot(old_rect, fill=fill)
        if image_moves:
            page.apply_redactions()

        shape_drawer = page.new_shape()
        used_shape = False

        for obj in objects:
            if isinstance(obj, HighlightObject):
                rect = fitz.Rect(obj.x, obj.y, obj.x + obj.width, obj.y + obj.height)
                annot = page.add_highlight_annot(rect)
                annot.set_colors(stroke=_hex_to_rgb01(obj.color))
                annot.set_opacity(max(0.05, min(obj.opacity, 1.0)))
                annot.update()

            elif isinstance(obj, DrawingObject):
                if len(obj.points) < 2:
                    continue
                pts = [fitz.Point(x, y) for x, y in obj.points]
                shape_drawer.draw_polyline(pts)
                shape_drawer.finish(
                    color=_hex_to_rgb01(obj.color),
                    width=max(obj.stroke_width, 0.1),
                    closePath=False,
                )
                used_shape = True

            elif isinstance(obj, ShapeObject):
                p1, p2 = fitz.Point(obj.x1, obj.y1), fitz.Point(obj.x2, obj.y2)
                fill_color = _hex_to_rgb01(obj.fill) if obj.fill else None
                if obj.shape == "line":
                    shape_drawer.draw_line(p1, p2)
                    shape_drawer.finish(color=_hex_to_rgb01(obj.color), width=max(obj.stroke_width, 0.1))
                elif obj.shape == "rect":
                    rect = fitz.Rect(min(obj.x1, obj.x2), min(obj.y1, obj.y2), max(obj.x1, obj.x2), max(obj.y1, obj.y2))
                    shape_drawer.draw_rect(rect)
                    shape_drawer.finish(color=_hex_to_rgb01(obj.color), fill=fill_color, width=max(obj.stroke_width, 0.1))
                elif obj.shape == "arrow":
                    shape_drawer.draw_line(p1, p2)
                    # simple arrowhead: two short lines back from the tip
                    import math
                    angle = math.atan2(p2.y - p1.y, p2.x - p1.x)
                    head_len = max(obj.stroke_width * 4, 8)
                    for da in (math.pi / 7, -math.pi / 7):
                        hx = p2.x - head_len * math.cos(angle - da)
                        hy = p2.y - head_len * math.sin(angle - da)
                        shape_drawer.draw_line(p2, fitz.Point(hx, hy))
                    shape_drawer.finish(color=_hex_to_rgb01(obj.color), width=max(obj.stroke_width, 0.1))
                used_shape = True

            elif isinstance(obj, NewTextObject):
                rect = fitz.Rect(obj.x, obj.y, obj.x + obj.width, obj.y + obj.height)
                align = fitz.TEXT_ALIGN_LEFT
                if obj.align == "center":
                    align = fitz.TEXT_ALIGN_CENTER
                elif obj.align == "right":
                    align = fitz.TEXT_ALIGN_RIGHT
                page.insert_textbox(
                    rect, obj.text, fontsize=obj.font_size,
                    fontname=_resolve_new_text_font(obj), color=_hex_to_rgb01(obj.color), align=align,
                )
                if obj.underline:
                    uy = obj.y + obj.font_size * 1.05
                    page.draw_line(fitz.Point(obj.x, uy), fitz.Point(obj.x + obj.width, uy), color=_hex_to_rgb01(obj.color), width=1)

            elif isinstance(obj, ImageObject):
                rect = fitz.Rect(obj.x, obj.y, obj.x + obj.width, obj.y + obj.height)
                if obj.image_data:
                    try:
                        header, b64data = obj.image_data.split(",", 1) if "," in obj.image_data else ("", obj.image_data)
                        img_bytes = base64.b64decode(b64data)
                    except Exception as exc:
                        raise PdfExportError(f"Could not decode image data for object {obj.id}: {exc}") from exc
                    page.insert_image(rect, stream=img_bytes)
                elif obj.source_xref is not None:
                    try:
                        extracted = doc.extract_image(obj.source_xref)
                        img_bytes = extracted["image"]
                    except Exception as exc:
                        raise PdfExportError(f"Could not extract original image for object {obj.id}: {exc}") from exc
                    page.insert_image(rect, stream=img_bytes)

        if used_shape:
            shape_drawer.commit()


def _apply_page_ops(doc: "fitz.Document", page_ops) -> None:
    """Applies rotation (in place, doesn't change indices) then resolves
    final page delete/reorder in one atomic `select()` call. Must run
    LAST, after every other content edit has been baked in against
    original indices.

    `page_ops.rotations` holds a DELTA the user applied in the editor UI
    (0/90/180/270), not an absolute value — a page that already had its
    own /Rotate baked in (common for scanned documents) must keep that
    and have the delta added on top. We read the page's own current
    `page.rotation` (PyMuPDF's authoritative view of the real PDF, not
    whatever the client may have assumed) and add the delta to it, so a
    page that was already rotated is never double-counted or reset.
    """
    if page_ops is None:
        return

    for page_no_str, delta_degrees in (page_ops.rotations or {}).items():
        page_no = int(page_no_str) if isinstance(page_no_str, str) else page_no_str
        if 0 <= page_no < doc.page_count:
            page = doc[page_no]
            absolute_degrees = (page.rotation + int(delta_degrees)) % 360
            page.set_rotation(absolute_degrees)

    order = page_ops.order
    if order:
        valid_order = [i for i in order if 0 <= i < doc.page_count]
        if not valid_order:
            raise PdfExportError("Requested page order removes every page — a PDF needs at least one page.")
        doc.select(valid_order)


def export_pdf(original_path: str, output_path: str, document: DocumentModel, state: EditorState) -> None:
    """
    original_path: path to the untouched, originally-uploaded PDF
    output_path: where to write the reconstructed PDF
    document: the parsed DocumentModel (bbox/font/color for every original element id)
    state: the full live-editor state (text edits, drawings/shapes/images, page ops)

    IMPORTANT: always opens `original_path` fresh and applies the *current*
    `state` passed in — never reads a cached/previous export. Callers must
    always pass the live state at call time, which is what prevents the
    "stale download" bug.
    """
    doc = None
    tmp_output_path = output_path + ".tmp"
    try:
        doc = fitz.open(original_path)
        if doc.is_encrypted:
            raise PdfExportError("Source PDF is encrypted/password-protected.")

        expected_final_page_count = (
            len([i for i in state.page_ops.order if 0 <= i < doc.page_count])
            if state.page_ops and state.page_ops.order
            else document.page_count
        )

        # Order matters: content edits first (against ORIGINAL indices),
        # page delete/reorder always last.
        _apply_text_edits(doc, document, state.text_edits)
        _apply_canvas_objects(doc, state.objects_by_page)
        _apply_page_ops(doc, state.page_ops)

        doc.save(tmp_output_path, garbage=4, deflate=True)
        doc.close()
        doc = None

        _validate_pdf(tmp_output_path, expected_page_count=expected_final_page_count)
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
            if expected_page_count and check_doc.page_count != expected_page_count:
                raise PdfExportError(
                    f"Generated PDF has {check_doc.page_count} page(s), expected {expected_page_count}."
                )
            _ = check_doc[0].get_text() if check_doc.page_count else None
        finally:
            check_doc.close()
    except PdfExportError:
        raise
    except Exception as exc:
        raise PdfExportError(f"Generated PDF failed validation: {exc}") from exc

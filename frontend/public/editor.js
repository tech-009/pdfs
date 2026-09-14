/**
 * PDF Editor — frontend application logic.
 *
 * Architecture:
 *  - pdf.js renders the REAL original PDF pages as the visual background
 *    (so unedited content — images, vector art, fonts — always looks
 *    exactly like the source document, not a re-implementation of it).
 *  - Fabric.js renders an interactive overlay on top: drawings, shapes,
 *    highlights, new text boxes, added/moved images, and invisible hit
 *    regions over existing text so it can be clicked to edit in place.
 *  - The single source of truth for what will be baked into the final
 *    PDF is NOT the fabric canvas — it's a handful of plain JS objects
 *    (`textEdits`, `objectsByPage`, `pageOrder`, `pageRotations`) that
 *    mirror the backend's EditorState exactly. The canvas is rebuilt
 *    FROM that state whenever a page loads or an undo/redo happens;
 *    interacting with the canvas writes BACK into that state. This is
 *    what makes Save/Export/Download unambiguous — there is always
 *    exactly one state, and it's always what gets sent to the server.
 */
(() => {
  "use strict";

  const API = window.__API_BASE__ || "http://localhost:8000";
  pdfjsLib.GlobalWorkerOptions.workerSrc =
    "https://cdnjs.cloudflare.com/ajax/libs/pdf.js/3.11.174/pdf.worker.min.js";

  const BASE_SCALE = 1.3333333; // ~96/72 dpi so a 100%-zoom page reads at a normal on-screen size

  // ---------------------------------------------------------------------
  // Global editor state
  // ---------------------------------------------------------------------
  const state = {
    documentId: null,
    documentModel: null,      // backend DocumentModel: { id, filename, page_count, pages: [...] }
    pdfDoc: null,             // pdf.js PDFDocumentProxy (built from the raw uploaded bytes)
    pageOrder: [],            // current display order, as a list of ORIGINAL page indices
    pageRotations: {},        // originalIndex(number) -> cumulative rotation delta in degrees
    textEdits: {},            // elementId -> new text
    objectsByPage: {},        // originalIndex(string) -> [ CanvasObject, ... ] (backend shape)
    currentPageOriginalIndex: 0,
    zoom: 1,
    currentTool: "select",
    pendingImageDataUrl: null, // set after picking a file for the "image" tool
  };

  let fabricCanvas = null;
  let pageRasterCanvas = null; // the raw pdf.js render of the current page, for pixel sampling
  let originalPageCount = 0;

  // history: array of JSON snapshots + a pointer to the current one
  const history = { stack: [], index: -1 };

  let saveTimer = null;
  let saveInFlight = null;
  let dirty = false;

  // ---------------------------------------------------------------------
  // Small utilities
  // ---------------------------------------------------------------------
  const $ = (id) => document.getElementById(id);
  const uid = () => Math.random().toString(36).slice(2) + Date.now().toString(36);
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  const currentScale = () => state.zoom * BASE_SCALE;

  function toast(message, isError) {
    const el = $("toast");
    el.textContent = message;
    el.className = "toast" + (isError ? " error" : "");
    el.hidden = false;
    clearTimeout(toast._t);
    toast._t = setTimeout(() => { el.hidden = true; }, 4000);
  }

  function setStatusMessage(msg) { $("statusMessage").textContent = msg; }

  function friendlyError(err, fallback) {
    // Never surface raw stack traces / internal details to the user.
    if (err && err.userMessage) return err.userMessage;
    return fallback || "Something went wrong. Please try again.";
  }

  async function apiFetch(path, options) {
    let res;
    try {
      res = await fetch(`${API}${path}`, options);
    } catch (networkErr) {
      const e = new Error("network");
      e.userMessage = "Couldn't reach the server. Check your connection and try again.";
      throw e;
    }
    if (!res.ok) {
      let detail = null;
      try { detail = (await res.json()).detail; } catch (_) { /* ignore */ }
      const e = new Error(`HTTP ${res.status}`);
      e.userMessage = detail || "The server couldn't complete that request.";
      throw e;
    }
    return res;
  }

  // ---------------------------------------------------------------------
  // Upload
  // ---------------------------------------------------------------------
  $("fileInput").addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    await handleUpload(file);
  });

  async function handleUpload(file) {
    const statusEl = $("uploadStatus");
    statusEl.className = "upload-status";
    statusEl.textContent = "Uploading & parsing…";

    try {
      const arrayBuffer = await file.arrayBuffer();

      const form = new FormData();
      form.append("file", file);
      const res = await apiFetch("/documents", { method: "POST", body: form });
      const documentModel = await res.json();

      // Load the raw bytes into pdf.js for accurate visual rendering.
      // pdf.js detaches/transfers the buffer, so give it a fresh copy.
      const pdfDoc = await pdfjsLib.getDocument({ data: arrayBuffer.slice(0) }).promise;

      state.documentId = documentModel.id;
      state.documentModel = documentModel;
      state.pdfDoc = pdfDoc;
      originalPageCount = documentModel.page_count;
      state.pageOrder = Array.from({ length: originalPageCount }, (_, i) => i);
      state.pageRotations = {};
      state.textEdits = {};
      state.objectsByPage = {};
      state.currentPageOriginalIndex = 0;
      state.zoom = 1;

      history.stack = [snapshotState()];
      history.index = 0;

      $("docName").textContent = documentModel.filename;
      $("uploadScreen").hidden = true;
      $("editorShell").hidden = false;

      initFabricCanvas();
      await renderAllThumbnails();
      await loadPage(0);
      updateUndoRedoButtons();
      setSaveStatus("saved");
    } catch (err) {
      statusEl.classList.add("error");
      statusEl.textContent = friendlyError(err, "Couldn't open this PDF. It may be corrupted, encrypted, or password-protected.");
    }
  }

  $("btnHome").addEventListener("click", () => {
    if (!confirm("Go back to upload? Any unsaved changes to this session will be lost from view (your last save is kept on the server).")) return;
    location.reload();
  });

  // ---------------------------------------------------------------------
  // Fabric canvas setup
  // ---------------------------------------------------------------------
  function initFabricCanvas() {
    fabricCanvas = new fabric.Canvas("fabricCanvas", {
      selection: true,
      preserveObjectStacking: true,
    });

    fabricCanvas.on("selection:created", onSelectionChanged);
    fabricCanvas.on("selection:updated", onSelectionChanged);
    fabricCanvas.on("selection:cleared", () => hideContextPanel());
    fabricCanvas.on("object:modified", onObjectModified);
    fabricCanvas.on("mouse:down", onCanvasMouseDown);
    fabricCanvas.on("mouse:move", onCanvasMouseMove);
    fabricCanvas.on("mouse:up", onCanvasMouseUp);

    document.addEventListener("keydown", (e) => {
      // Ctrl/Cmd+S must work as "Save" even while actively typing inside a
      // text box — so it's checked before the isEditingText early-return,
      // and it browser-preventDefaults first so the OS "Save Page" dialog
      // never appears. flushSave() itself calls commitActiveTextEditing(),
      // so the in-progress edit is captured correctly either way.
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
        e.preventDefault();
        $("btnSave").click();
        return;
      }

      const tag = (document.activeElement && document.activeElement.tagName) || "";
      const isEditingText = tag === "INPUT" || tag === "TEXTAREA" ||
        (fabricCanvas.getActiveObject() && fabricCanvas.getActiveObject().isEditing);
      if (isEditingText) return;

      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !e.shiftKey) { e.preventDefault(); undo(); }
      else if ((e.ctrlKey || e.metaKey) && (e.key.toLowerCase() === "y" || (e.key.toLowerCase() === "z" && e.shiftKey))) { e.preventDefault(); redo(); }
      else if ((e.key === "Delete" || e.key === "Backspace")) {
        const obj = fabricCanvas.getActiveObject();
        if (obj) { e.preventDefault(); deleteSelectedObject(); }
      }
    });
  }

  // ---------------------------------------------------------------------
  // Page rendering (pdf.js -> raster background)
  // ---------------------------------------------------------------------
  function totalRotationFor(originalIndex, pdfPage) {
    const delta = state.pageRotations[originalIndex] || 0;
    return (((pdfPage.rotate || 0) + delta) % 360 + 360) % 360;
  }

  async function renderPageRaster(originalIndex, scale) {
    const pdfPage = await state.pdfDoc.getPage(originalIndex + 1); // pdf.js is 1-indexed
    const rotation = totalRotationFor(originalIndex, pdfPage);
    const viewport = pdfPage.getViewport({ scale, rotation });

    const canvas = document.createElement("canvas");
    canvas.width = Math.ceil(viewport.width);
    canvas.height = Math.ceil(viewport.height);
    const ctx = canvas.getContext("2d");
    await pdfPage.render({ canvasContext: ctx, viewport }).promise;
    return canvas;
  }

  async function loadPage(originalIndex) {
    showOverlay("Loading page…");
    try {
      state.currentPageOriginalIndex = originalIndex;
      const scale = currentScale();
      pageRasterCanvas = await renderPageRaster(originalIndex, scale);

      fabricCanvas.clear();
      fabricCanvas.setWidth(pageRasterCanvas.width);
      fabricCanvas.setHeight(pageRasterCanvas.height);
      document.getElementById("canvasStage").style.width = pageRasterCanvas.width + "px";

      const bgImg = new fabric.Image(pageRasterCanvas);
      fabricCanvas.setBackgroundImage(bgImg, fabricCanvas.renderAll.bind(fabricCanvas));

      buildOverlayForPage(originalIndex);
      updatePageIndicator();
      highlightActiveThumbnail();
      hideContextPanel();
    } catch (err) {
      toast(friendlyError(err, "Couldn't render this page."), true);
    } finally {
      hideOverlay();
    }
  }

  function pageIndexFromElementId(elementId) {
    // ids look like "p{page}_b{block}_l{line}_s{span}"
    const match = /^p(\d+)_/.exec(elementId);
    return match ? parseInt(match[1], 10) : -1;
  }

  function buildOverlayForPage(originalIndex) {
    const scale = currentScale();
    const page = state.documentModel.pages[originalIndex];
    if (!page) return;

    // 1) Existing text: either a clickable invisible hit-region (unedited)
    //    or a committed cover+textbox (edited).
    for (const el of page.text_elements) {
      const isEdited = Object.prototype.hasOwnProperty.call(state.textEdits, el.id) && state.textEdits[el.id] !== el.text;
      if (isEdited) {
        addCommittedTextOverlay(el, state.textEdits[el.id], scale);
      } else {
        addTextHitRegion(el, scale);
      }
    }

    // 2) Existing images: clickable hit-region until the user starts
    //    dragging one, unless it's already been moved (in which case its
    //    ImageObject entry in objectsByPage renders it, not the hit-region).
    const movedXrefs = new Set(
      (state.objectsByPage[String(originalIndex)] || [])
        .filter((o) => o.type === "image" && o.source_xref != null)
        .map((o) => o.source_xref)
    );
    for (const img of page.image_elements) {
      if (!movedXrefs.has(img.xref)) addImageHitRegion(img, scale);
    }

    // 3) Canvas objects added by the user on this page.
    const objects = state.objectsByPage[String(originalIndex)] || [];
    for (const obj of objects) addModelObjectToCanvas(obj, scale);

    fabricCanvas.requestRenderAll();
  }

  // ---------------------------------------------------------------------
  // Existing text: hit region (click to edit) <-> committed overlay
  // ---------------------------------------------------------------------
  function addTextHitRegion(el, scale) {
    const rect = new fabric.Rect({
      left: el.x * scale, top: el.y * scale,
      width: el.width * scale, height: el.height * scale,
      fill: "rgba(42,92,219,0)", stroke: "rgba(42,92,219,0)", strokeWidth: 1,
      hasControls: false, hasBorders: false, lockRotation: true, selectable: false,
      hoverCursor: "text",
    });
    rect._modelType = "textHit";
    rect._elementId = el.id;
    rect.on("mouseover", () => { rect.set({ fill: "rgba(42,92,219,0.06)", stroke: "rgba(42,92,219,0.5)" }); fabricCanvas.requestRenderAll(); });
    rect.on("mouseout", () => { rect.set({ fill: "rgba(42,92,219,0)", stroke: "rgba(42,92,219,0)" }); fabricCanvas.requestRenderAll(); });
    rect.on("mousedown", () => {
      if (state.currentTool === "select") beginTextEdit(el);
    });
    fabricCanvas.add(rect);
  }

  function approximateFontFamily(el) {
    const name = (el.font_family || "").toLowerCase();
    if (name.includes("courier") || name.includes("mono") || name.includes("consol")) return "'Courier New', monospace";
    if (name.includes("times") || name.includes("georgia") || name.includes("garamond") || name.includes("serif")) return "Georgia, 'Times New Roman', serif";
    return "Helvetica, Arial, sans-serif";
  }

  function beginTextEdit(el) {
    // Remove the hit-region for this element, add a cover (so the raster
    // text underneath is visually replaced) plus an editable textbox.
    const target = fabricCanvas.getObjects().find((o) => o._modelType === "textHit" && o._elementId === el.id);
    if (target) fabricCanvas.remove(target);
    removeCommittedTextOverlay(el.id);

    const scale = currentScale();
    const cover = makeCoverRect(el.x * scale, el.y * scale, el.width * scale, el.height * scale);
    cover._modelType = "textCover";
    cover._elementId = el.id;
    fabricCanvas.add(cover);

    const box = new fabric.Textbox(state.textEdits[el.id] ?? el.text, {
      left: el.x * scale, top: el.y * scale - el.font_size * 0.1 * scale,
      width: Math.max(el.width * scale, 20),
      fontSize: Math.max(el.font_size * scale, 6),
      fill: el.color || "#111111",
      fontFamily: approximateFontFamily(el),
      fontWeight: el.font_weight === "bold" ? "bold" : "normal",
      fontStyle: el.font_style === "italic" ? "italic" : "normal",
      textAlign: el.alignment === "justify" ? "left" : (el.alignment || "left"),
      editable: true, hasControls: false, lockMovementX: true, lockMovementY: true,
    });
    box._modelType = "textEditOverlay";
    box._elementId = el.id;
    box._originalText = el.text;
    fabricCanvas.add(box);
    fabricCanvas.setActiveObject(box);
    box.enterEditing();
    box.selectAll();
    fabricCanvas.requestRenderAll();

    box.on("editing:exited", () => commitTextEdit(el, box, cover));
  }

  function removeCommittedTextOverlay(elementId) {
    fabricCanvas.getObjects()
      .filter((o) => o._elementId === elementId && (o._modelType === "textEditOverlay" || o._modelType === "textCover"))
      .forEach((o) => fabricCanvas.remove(o));
  }

  async function commitTextEdit(el, box, cover) {
    const newText = box.text;
    const changed = newText !== el.text;
    if (!newText.trim() && el.text.trim()) {
      // Emptying a span isn't a supported "delete text" gesture here — revert.
      toast("Text can't be left empty — reverted to the original.");
      fabricCanvas.remove(box); fabricCanvas.remove(cover);
      delete state.textEdits[el.id];
      addTextHitRegion(el, currentScale());
      fabricCanvas.requestRenderAll();
      return;
    }

    if (changed) {
      state.textEdits[el.id] = newText;
      box.set({ editable: false, selectable: true, hasControls: false });
      box.off("editing:exited");
      box.on("mousedblclick", () => beginTextEdit(el));
      fabricCanvas.requestRenderAll();
      try {
        await apiFetch(`/documents/${state.documentId}/elements/${el.id}`, {
          method: "PATCH", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ text: newText }),
        });
      } catch (err) {
        toast(friendlyError(err, "Couldn't save that text edit — it will retry on the next Save."));
      }
      pushHistory();
      markDirtyAndAutosave();
    } else {
      // Unchanged — remove the edit overlay, restore the plain hit-region.
      fabricCanvas.remove(box); fabricCanvas.remove(cover);
      delete state.textEdits[el.id];
      addTextHitRegion(el, currentScale());
      fabricCanvas.requestRenderAll();
    }
  }

  function addCommittedTextOverlay(el, editedText, scale) {
    const cover = makeCoverRect(el.x * scale, el.y * scale, el.width * scale, el.height * scale);
    cover._modelType = "textCover"; cover._elementId = el.id;
    fabricCanvas.add(cover);

    const box = new fabric.Textbox(editedText, {
      left: el.x * scale, top: el.y * scale - el.font_size * 0.1 * scale,
      width: Math.max(el.width * scale, 20),
      fontSize: Math.max(el.font_size * scale, 6),
      fill: el.color || "#111111",
      fontFamily: approximateFontFamily(el),
      fontWeight: el.font_weight === "bold" ? "bold" : "normal",
      fontStyle: el.font_style === "italic" ? "italic" : "normal",
      textAlign: el.alignment === "justify" ? "left" : (el.alignment || "left"),
      editable: false, hasControls: false, lockMovementX: true, lockMovementY: true,
    });
    box._modelType = "textEditOverlay"; box._elementId = el.id; box._originalText = el.text;
    box.on("mousedblclick", () => beginTextEdit(el));
    fabricCanvas.add(box);
  }

  function makeCoverRect(left, top, width, height) {
    // Best-effort background match: sample the pixel strip just above the
    // box from the actual rendered page raster (same heuristic as the
    // server uses at export time), so the live preview doesn't flash white.
    let fillColor = "#ffffff";
    try {
      const ctx = pageRasterCanvas.getContext("2d");
      const sampleY = Math.max(Math.round(top) - 2, 0);
      const data = ctx.getImageData(Math.max(Math.round(left), 0), sampleY, Math.max(Math.round(width), 1), 1).data;
      let r = 0, g = 0, b = 0, n = 0;
      for (let i = 0; i < data.length; i += 4) { r += data[i]; g += data[i + 1]; b += data[i + 2]; n++; }
      if (n > 0) fillColor = `rgb(${Math.round(r / n)},${Math.round(g / n)},${Math.round(b / n)})`;
    } catch (_) { /* canvas may be tainted in some browsers; fall back to white */ }
    return new fabric.Rect({
      left, top: top - 2, width, height: height + 4, fill: fillColor,
      selectable: false, evented: false,
    });
  }

  // ---------------------------------------------------------------------
  // Existing images: hit region -> "lift" into a draggable/resizable image
  // ---------------------------------------------------------------------
  function addImageHitRegion(imgEl, scale) {
    const rect = new fabric.Rect({
      left: imgEl.x * scale, top: imgEl.y * scale,
      width: imgEl.width * scale, height: imgEl.height * scale,
      fill: "rgba(0,0,0,0)", stroke: "rgba(0,0,0,0)",
      hasControls: false, hasBorders: false, hoverCursor: "move", selectable: false,
    });
    rect._modelType = "imageHit";
    rect._sourceXref = imgEl.xref;
    rect._originalRect = { x: imgEl.x, y: imgEl.y, width: imgEl.width, height: imgEl.height };
    rect.on("mouseover", () => { rect.set({ stroke: "rgba(42,92,219,0.5)", strokeDashArray: [4, 3] }); fabricCanvas.requestRenderAll(); });
    rect.on("mouseout", () => { rect.set({ stroke: "rgba(0,0,0,0)" }); fabricCanvas.requestRenderAll(); });
    rect.on("mousedown", () => { if (state.currentTool === "select") liftExistingImage(rect); });
    fabricCanvas.add(rect);
  }

  function liftExistingImage(hitRegion) {
    const scale = currentScale();
    const { x, y, width, height } = hitRegion._originalRect;
    const px = x * scale, py = y * scale, pw = width * scale, ph = height * scale;

    // Crop the pixels straight out of the current page raster so the
    // "lifted" copy looks identical to the original while it's dragged.
    const crop = document.createElement("canvas");
    crop.width = Math.max(1, Math.round(pw));
    crop.height = Math.max(1, Math.round(ph));
    try {
      crop.getContext("2d").drawImage(pageRasterCanvas, px, py, pw, ph, 0, 0, crop.width, crop.height);
    } catch (_) { /* ignore — worst case the lifted preview is blank until moved */ }

    fabricCanvas.remove(hitRegion);
    const cover = makeCoverRect(px, py, pw, ph);
    cover._modelType = "imageCover"; cover._sourceXref = hitRegion._sourceXref;
    fabricCanvas.add(cover);

    const modelObj = {
      type: "image", id: uid(),
      x, y, width, height, rotation: 0, opacity: 1,
      source_xref: hitRegion._sourceXref,
      original_x: x, original_y: y, original_width: width, original_height: height,
      // Client-only cache so revisiting this page still shows the real
      // pixels instead of a placeholder — stripped before it's ever sent
      // to the backend (which re-extracts the real bytes from the
      // original PDF via source_xref at export time regardless).
      _previewDataUrl: crop.toDataURL(),
    };
    addObjectToModel(modelObj);

    const fImg = new fabric.Image(crop, { left: px, top: py, width: crop.width, height: crop.height });
    fImg._modelType = "image"; fImg._modelId = modelObj.id;
    fabricCanvas.add(fImg);
    fabricCanvas.setActiveObject(fImg);
    fabricCanvas.requestRenderAll();
    pushHistory();
    markDirtyAndAutosave();
  }

  // ---------------------------------------------------------------------
  // Generic model <-> fabric object sync (drawings, shapes, highlights,
  // new text, images)
  // ---------------------------------------------------------------------
  function addObjectToModel(obj) {
    const key = String(state.currentPageOriginalIndex);
    if (!state.objectsByPage[key]) state.objectsByPage[key] = [];
    state.objectsByPage[key].push(obj);
  }

  function findModelObject(id) {
    const key = String(state.currentPageOriginalIndex);
    return (state.objectsByPage[key] || []).find((o) => o.id === id);
  }

  function removeModelObject(id) {
    const key = String(state.currentPageOriginalIndex);
    state.objectsByPage[key] = (state.objectsByPage[key] || []).filter((o) => o.id !== id);
  }

  function addModelObjectToCanvas(obj, scale) {
    let fObj = null;
    if (obj.type === "drawing") {
      const pts = obj.points.map(([x, y]) => ({ x: x * scale, y: y * scale }));
      fObj = new fabric.Polyline(pts, {
        stroke: obj.color, strokeWidth: obj.stroke_width * scale, fill: null,
        opacity: obj.opacity, objectCaching: false,
        hasControls: false, lockScalingX: true, lockScalingY: true, lockRotation: true,
      });
    } else if (obj.type === "shape") {
      const x1 = obj.x1 * scale, y1 = obj.y1 * scale, x2 = obj.x2 * scale, y2 = obj.y2 * scale;
      if (obj.shape === "rect") {
        fObj = new fabric.Rect({
          left: Math.min(x1, x2), top: Math.min(y1, y2), width: Math.abs(x2 - x1), height: Math.abs(y2 - y1),
          stroke: obj.color, strokeWidth: obj.stroke_width * scale,
          fill: obj.fill || "rgba(0,0,0,0)", opacity: obj.opacity,
          lockRotation: true,
        });
        fObj.setControlVisible && fObj.setControlVisible("mtr", false);
      } else {
        fObj = new fabric.Line([x1, y1, x2, y2], {
          stroke: obj.color, strokeWidth: obj.stroke_width * scale, opacity: obj.opacity,
          hasControls: false, lockScalingX: true, lockScalingY: true, lockRotation: true,
        });
        if (obj.shape === "arrow") fObj._isArrow = true;
      }
    } else if (obj.type === "highlight") {
      fObj = new fabric.Rect({
        left: obj.x * scale, top: obj.y * scale, width: obj.width * scale, height: obj.height * scale,
        fill: obj.color, opacity: obj.opacity, lockRotation: true,
      });
      fObj.setControlVisible && fObj.setControlVisible("mtr", false);
    } else if (obj.type === "text") {
      fObj = new fabric.Textbox(obj.text, {
        left: obj.x * scale, top: obj.y * scale, width: obj.width * scale,
        fontSize: obj.font_size * scale, fill: obj.color,
        fontWeight: obj.bold ? "bold" : "normal", fontStyle: obj.italic ? "italic" : "normal",
        underline: !!obj.underline, textAlign: obj.align || "left", opacity: obj.opacity, editable: true,
      });
      fObj.on("editing:exited", () => {
        const m = findModelObject(obj.id);
        if (!m) return;
        if (!fObj.text.trim()) { fabricCanvas.remove(fObj); removeModelObject(obj.id); pushHistory(); markDirtyAndAutosave(); return; }
        m.text = fObj.text;
        pushHistory(); markDirtyAndAutosave();
      });
    } else if (obj.type === "image" && obj.image_data) {
      fabric.Image.fromURL(obj.image_data, (img) => {
        img.set({ left: obj.x * scale, top: obj.y * scale });
        img.scaleToWidth(obj.width * scale);
        img.set({ opacity: obj.opacity, angle: obj.rotation || 0 });
        img._modelType = "image"; img._modelId = obj.id;
        fabricCanvas.add(img);
        fabricCanvas.requestRenderAll();
      });
      return; // async path — handled in callback
    } else if (obj.type === "image" && obj.source_xref != null && obj._previewDataUrl) {
      fabric.Image.fromURL(obj._previewDataUrl, (img) => {
        img.set({ left: obj.x * scale, top: obj.y * scale, opacity: obj.opacity, angle: obj.rotation || 0 });
        img.scaleToWidth(obj.width * scale);
        img._modelType = "image"; img._modelId = obj.id;
        fabricCanvas.add(img);
        fabricCanvas.requestRenderAll();
      });
      return;
    } else if (obj.type === "image" && obj.source_xref != null) {
      // A previously-moved existing image with no cached preview (e.g. the
      // cache didn't survive a hard refresh). We don't have raw bytes
      // client-side in that case, so show a neutral placeholder — the
      // real pixels are only needed again at export time, where the
      // server re-extracts them from the original PDF via source_xref.
      fObj = new fabric.Rect({
        left: obj.x * scale, top: obj.y * scale, width: obj.width * scale, height: obj.height * scale,
        fill: "#eef0f3", stroke: "#c7cbd4", strokeDashArray: [4, 3],
      });
      const label = new fabric.Textbox("Moved image", {
        left: obj.x * scale + 4, top: obj.y * scale + 4, width: obj.width * scale - 8,
        fontSize: 11, fill: "#8a8f99", selectable: false, evented: false,
      });
      const group = new fabric.Group([fObj, label], { left: obj.x * scale, top: obj.y * scale });
      group._modelType = "image"; group._modelId = obj.id;
      fabricCanvas.add(group);
      return;
    }
    if (fObj) {
      fObj._modelType = obj.type; fObj._modelId = obj.id;
      if (fObj.type === "line" || fObj.type === "polyline") {
        fObj._createdLeft = fObj.left; fObj._createdTop = fObj.top;
      }
      fabricCanvas.add(fObj);
    }
  }

  function pxRectFromFabricObject(o) {
    return {
      left: o.left, top: o.top,
      width: o.width * (o.scaleX || 1), height: o.height * (o.scaleY || 1),
      angle: o.angle || 0,
    };
  }

  // Line/Polyline objects in Fabric keep their original point coordinates
  // fixed and represent movement purely through left/top — so unlike a
  // Rect, you cannot read a Line's current endpoints directly off its
  // x1/y1/x2/y2 properties after it's been dragged. To stay correct
  // without depending on Fabric's internal transform-matrix math (which
  // we can't verify against a real Fabric.js runtime here), line/arrow
  // shapes and freehand drawings are movable but NOT resizable/rotatable
  // (see `hasControls:false` at creation) — so all we ever need is a
  // simple translation delta from the position last recorded here.
  function translationDelta(o) {
    const dx = (o.left - (o._createdLeft ?? o.left));
    const dy = (o.top - (o._createdTop ?? o.top));
    o._createdLeft = o.left; o._createdTop = o.top;
    return { dx, dy };
  }

  function onObjectModified(e) {
    const o = e.target;
    if (!o || !o._modelType) return;
    const scale = currentScale();

    if (o._modelType === "textHit" || o._modelType === "textCover" || o._modelType === "textEditOverlay") return; // position is fixed by design

    const m = o._modelId ? findModelObject(o._modelId) : null;
    if (!m) return;

    if (o._modelType === "image") {
      const r = pxRectFromFabricObject(o);
      m.x = r.left / scale; m.y = r.top / scale;
      m.width = r.width / scale; m.height = r.height / scale;
      m.rotation = r.angle;
      o.set({ scaleX: 1, scaleY: 1, width: r.width, height: r.height });
    } else if (o._modelType === "highlight") {
      const r = pxRectFromFabricObject(o);
      m.x = r.left / scale; m.y = r.top / scale; m.width = r.width / scale; m.height = r.height / scale;
      o.set({ scaleX: 1, scaleY: 1, width: r.width, height: r.height });
    } else if (o._modelType === "shape" && m.shape === "rect") {
      const r = pxRectFromFabricObject(o);
      m.x1 = r.left / scale; m.y1 = r.top / scale;
      m.x2 = (r.left + r.width) / scale; m.y2 = (r.top + r.height) / scale;
      o.set({ scaleX: 1, scaleY: 1, width: r.width, height: r.height });
    } else if (o._modelType === "shape") { // line / arrow — translate only
      const { dx, dy } = translationDelta(o);
      m.x1 += dx / scale; m.y1 += dy / scale; m.x2 += dx / scale; m.y2 += dy / scale;
    } else if (o._modelType === "drawing") { // freehand path — translate only
      const { dx, dy } = translationDelta(o);
      m.points = m.points.map(([x, y]) => [x + dx / scale, y + dy / scale]);
    } else if (o._modelType === "text") {
      const r = pxRectFromFabricObject(o);
      const newFontPx = o.fontSize * (o.scaleY || 1); // corner-resize scales a Textbox's rendered font
      m.x = r.left / scale; m.y = r.top / scale;
      m.width = r.width / scale; m.height = r.height / scale;
      m.font_size = newFontPx / scale;
      o.set({ fontSize: newFontPx, scaleX: 1, scaleY: 1, width: r.width });
    }
    pushHistory();
    markDirtyAndAutosave();
  }

  function deleteSelectedObject() {
    const o = fabricCanvas.getActiveObject();
    if (!o) return;
    if (o._modelType === "textEditOverlay") {
      revertTextEdit(o._elementId);
      return;
    }
    if (o._modelId) removeModelObject(o._modelId);
    fabricCanvas.remove(o);
    fabricCanvas.requestRenderAll();
    hideContextPanel();
    pushHistory();
    markDirtyAndAutosave();
  }

  function revertTextEdit(elementId) {
    const el = state.documentModel.pages[state.currentPageOriginalIndex].text_elements.find((e) => e.id === elementId);
    if (!el) return;
    removeCommittedTextOverlay(elementId);
    delete state.textEdits[elementId];
    addTextHitRegion(el, currentScale());
    fabricCanvas.requestRenderAll();
    hideContextPanel();
    pushHistory();
    markDirtyAndAutosave();
  }

  $("btnDeleteObject").addEventListener("click", deleteSelectedObject);

  // ---------------------------------------------------------------------
  // Tools: draw / line / arrow / rect / highlight / text / image
  // ---------------------------------------------------------------------
  document.querySelectorAll(".tool-btn").forEach((btn) => {
    btn.addEventListener("click", () => setTool(btn.dataset.tool));
  });

  function setTool(tool) {
    if (tool === "image") { $("imageInput").click(); return; }
    if (tool === "search") { toggleSearchPanel(true); return; }
    state.currentTool = tool;
    document.querySelectorAll(".tool-btn").forEach((b) => b.classList.toggle("active", b.dataset.tool === tool));
    fabricCanvas.discardActiveObject();
    fabricCanvas.selection = tool === "select";
    const isSelectMode = tool === "select";
    fabricCanvas.forEachObject((o) => {
      if (o._modelType === "textHit" || o._modelType === "imageHit") {
        // These are click-to-edit/click-to-lift hit regions, not drawable
        // content — while a drawing/shape/etc. tool is active they must
        // get out of the way so clicks land on the page underneath them.
        o.evented = isSelectMode;
      } else if (o._modelType) {
        o.selectable = isSelectMode;
      }
    });
    fabricCanvas.requestRenderAll();
  }

  $("imageInput").addEventListener("change", (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    const reader = new FileReader();
    reader.onload = () => {
      state.pendingImageDataUrl = reader.result;
      state.currentTool = "place-image";
      setStatusMessage("Click on the page to place the image.");
    };
    reader.readAsDataURL(file);
  });

  let drawState = null; // { tool, startPx, previewObj, points }

  function onCanvasMouseDown(opt) {
    const tool = state.currentTool;
    if (tool === "select") return;
    if (opt.target && opt.target._modelType && tool !== "place-image") return; // let object interactions win

    const p = fabricCanvas.getPointer(opt.e);

    if (tool === "place-image" && state.pendingImageDataUrl) {
      placeImageAt(p.x, p.y, state.pendingImageDataUrl);
      state.pendingImageDataUrl = null;
      setTool("select");
      return;
    }

    if (tool === "text") {
      placeTextAt(p.x, p.y);
      setTool("select");
      return;
    }

    if (["draw", "line", "arrow", "rect", "highlight"].includes(tool)) {
      drawState = { tool, startPx: p, points: [p] };
      if (tool === "draw") {
        drawState.previewObj = new fabric.Polyline([p], { stroke: "#111111", strokeWidth: 2, fill: null, selectable: false, evented: false });
      } else if (tool === "line" || tool === "arrow") {
        drawState.previewObj = new fabric.Line([p.x, p.y, p.x, p.y], { stroke: "#111111", strokeWidth: 2, selectable: false, evented: false });
      } else if (tool === "rect") {
        drawState.previewObj = new fabric.Rect({ left: p.x, top: p.y, width: 1, height: 1, stroke: "#111111", strokeWidth: 2, fill: "rgba(0,0,0,0)", selectable: false, evented: false });
      } else if (tool === "highlight") {
        drawState.previewObj = new fabric.Rect({ left: p.x, top: p.y, width: 1, height: 1, fill: "#ffeb3b", opacity: 0.4, selectable: false, evented: false });
      }
      fabricCanvas.add(drawState.previewObj);
    }
  }

  function onCanvasMouseMove(opt) {
    if (!drawState) return;
    const p = fabricCanvas.getPointer(opt.e);
    const { tool, startPx, previewObj } = drawState;

    if (tool === "draw") {
      drawState.points.push(p);
      fabricCanvas.remove(previewObj);
      drawState.previewObj = new fabric.Polyline(drawState.points.slice(), { stroke: "#111111", strokeWidth: 2, fill: null, selectable: false, evented: false });
      fabricCanvas.add(drawState.previewObj);
    } else if (tool === "line" || tool === "arrow") {
      previewObj.set({ x2: p.x, y2: p.y });
    } else if (tool === "rect" || tool === "highlight") {
      previewObj.set({
        left: Math.min(startPx.x, p.x), top: Math.min(startPx.y, p.y),
        width: Math.abs(p.x - startPx.x), height: Math.abs(p.y - startPx.y),
      });
    }
    fabricCanvas.requestRenderAll();
  }

  function onCanvasMouseUp(opt) {
    if (!drawState) return;
    const { tool, startPx, previewObj } = drawState;
    const p = fabricCanvas.getPointer(opt.e);
    const scale = currentScale();
    fabricCanvas.remove(previewObj);

    if (tool === "draw" && drawState.points.length > 1) {
      const obj = {
        type: "drawing", id: uid(),
        points: drawState.points.map((pt) => [pt.x / scale, pt.y / scale]),
        color: "#111111", stroke_width: 2 / scale, opacity: 1,
      };
      addObjectToModel(obj);
      addModelObjectToCanvas(obj, scale);
      finishDrawGesture();
    } else if ((tool === "line" || tool === "arrow") && (Math.abs(p.x - startPx.x) > 2 || Math.abs(p.y - startPx.y) > 2)) {
      const obj = {
        type: "shape", id: uid(), shape: tool,
        x1: startPx.x / scale, y1: startPx.y / scale, x2: p.x / scale, y2: p.y / scale,
        color: "#111111", stroke_width: 2 / scale, opacity: 1,
      };
      addObjectToModel(obj);
      addModelObjectToCanvas(obj, scale);
      finishDrawGesture();
    } else if (tool === "rect" && Math.abs(p.x - startPx.x) > 2 && Math.abs(p.y - startPx.y) > 2) {
      const x1 = Math.min(startPx.x, p.x) / scale, y1 = Math.min(startPx.y, p.y) / scale;
      const x2 = Math.max(startPx.x, p.x) / scale, y2 = Math.max(startPx.y, p.y) / scale;
      const obj = { type: "shape", id: uid(), shape: "rect", x1, y1, x2, y2, color: "#111111", stroke_width: 2 / scale, fill: null, opacity: 1 };
      addObjectToModel(obj);
      addModelObjectToCanvas(obj, scale);
      finishDrawGesture();
    } else if (tool === "highlight" && Math.abs(p.x - startPx.x) > 2 && Math.abs(p.y - startPx.y) > 2) {
      const x = Math.min(startPx.x, p.x) / scale, y = Math.min(startPx.y, p.y) / scale;
      const width = Math.abs(p.x - startPx.x) / scale, height = Math.abs(p.y - startPx.y) / scale;
      const obj = { type: "highlight", id: uid(), x, y, width, height, color: "#ffeb3b", opacity: 0.4 };
      addObjectToModel(obj);
      addModelObjectToCanvas(obj, scale);
      finishDrawGesture();
    }
    drawState = null;
    fabricCanvas.requestRenderAll();
  }

  function finishDrawGesture() {
    pushHistory();
    markDirtyAndAutosave();
    setTool("select");
  }

  function placeTextAt(px, py) {
    const scale = currentScale();
    const obj = {
      type: "text", id: uid(), text: "Text", x: px / scale, y: py / scale,
      width: 160 / scale, height: 24 / scale, font_size: 14, color: "#111111",
      bold: false, italic: false, underline: false, align: "left", opacity: 1,
    };
    addObjectToModel(obj);
    addModelObjectToCanvas(obj, scale);
    const fObj = fabricCanvas.getObjects().find((o) => o._modelId === obj.id);
    if (fObj) { fabricCanvas.setActiveObject(fObj); fObj.enterEditing(); fObj.selectAll(); }
    pushHistory();
    markDirtyAndAutosave();
  }

  function placeImageAt(px, py, dataUrl) {
    const scale = currentScale();
    const img = new Image();
    img.onload = () => {
      const maxW = 220; // pt, on the document scale
      const naturalWpt = img.naturalWidth || 100, naturalHpt = img.naturalHeight || 100;
      const w = Math.min(maxW, naturalWpt);
      const h = w * (naturalHpt / naturalWpt);
      const obj = {
        type: "image", id: uid(), x: px / scale - w / 2, y: py / scale - h / 2,
        width: w, height: h, rotation: 0, opacity: 1, image_data: dataUrl,
      };
      addObjectToModel(obj);
      addModelObjectToCanvas(obj, scale);
      pushHistory();
      markDirtyAndAutosave();
    };
    img.src = dataUrl;
  }

  // ---------------------------------------------------------------------
  // Context (properties) panel
  // ---------------------------------------------------------------------
  function onSelectionChanged(e) {
    const obj = fabricCanvas.getActiveObject();
    if (!obj || fabricCanvas.getActiveObjects().length > 1) { hideContextPanel(); return; }
    showContextPanelFor(obj);
  }

  function hideContextPanel() { $("contextPanel").hidden = true; }

  function showContextPanelFor(o) {
    const panel = $("contextPanel"), body = $("contextBody"), title = $("contextTitle");
    body.innerHTML = "";
    panel.hidden = false;

    const addField = (labelText, inputEl) => {
      const wrap = document.createElement("div"); wrap.className = "field";
      const label = document.createElement("label"); label.textContent = labelText;
      wrap.appendChild(label); wrap.appendChild(inputEl); body.appendChild(wrap);
      return inputEl;
    };
    const colorField = (labelText, value, onInput) => {
      const input = document.createElement("input"); input.type = "color"; input.value = value || "#111111";
      input.addEventListener("input", onInput);
      addField(labelText, input);
    };
    const rangeField = (labelText, value, min, max, step, onInput) => {
      const input = document.createElement("input"); input.type = "range";
      input.min = min; input.max = max; input.step = step; input.value = value;
      input.addEventListener("input", onInput);
      addField(labelText, input);
    };

    if (o._modelType === "textEditOverlay") {
      title.textContent = "Text";
      const p = document.createElement("p"); p.style.fontSize = "12.5px"; p.style.color = "var(--text-muted)";
      p.textContent = "Double-click the text on the page to edit it.";
      body.appendChild(p);
      const revertBtn = document.createElement("button"); revertBtn.className = "btn btn-ghost";
      revertBtn.textContent = "Revert to original text";
      revertBtn.style.width = "100%";
      revertBtn.addEventListener("click", () => revertTextEdit(o._elementId));
      body.appendChild(revertBtn);
      $("btnDeleteObject").style.display = "none";
      return;
    }
    $("btnDeleteObject").style.display = "block";

    if (o._modelType === "drawing" || o._modelType === "shape") {
      title.textContent = o._modelType === "drawing" ? "Drawing" : "Shape";
      const m = findModelObject(o._modelId);
      colorField("Color", m.color, (e) => { m.color = e.target.value; o.set({ stroke: e.target.value }); fabricCanvas.requestRenderAll(); scheduleCommit(); });
      rangeField("Stroke width", m.stroke_width, 0.5, 20, 0.5, (e) => { m.stroke_width = parseFloat(e.target.value); o.set({ strokeWidth: parseFloat(e.target.value) * currentScale() }); fabricCanvas.requestRenderAll(); scheduleCommit(); });
      if (o._modelType === "shape" && m.shape === "rect") {
        const fillInput = document.createElement("input"); fillInput.type = "checkbox"; fillInput.checked = !!m.fill;
        fillInput.addEventListener("change", (e) => {
          m.fill = e.target.checked ? (m.color || "#cccccc") : null;
          o.set({ fill: m.fill || "rgba(0,0,0,0)" }); fabricCanvas.requestRenderAll(); scheduleCommit();
        });
        addField("Filled", fillInput);
      }
      rangeField("Opacity", m.opacity, 0.1, 1, 0.05, (e) => { m.opacity = parseFloat(e.target.value); o.set({ opacity: parseFloat(e.target.value) }); fabricCanvas.requestRenderAll(); scheduleCommit(); });
    } else if (o._modelType === "highlight") {
      title.textContent = "Highlight";
      const m = findModelObject(o._modelId);
      colorField("Color", m.color, (e) => { m.color = e.target.value; o.set({ fill: e.target.value }); fabricCanvas.requestRenderAll(); scheduleCommit(); });
      rangeField("Opacity", m.opacity, 0.1, 0.9, 0.05, (e) => { m.opacity = parseFloat(e.target.value); o.set({ opacity: parseFloat(e.target.value) }); fabricCanvas.requestRenderAll(); scheduleCommit(); });
    } else if (o._modelType === "text") {
      title.textContent = "Text box";
      const m = findModelObject(o._modelId);
      const toggles = document.createElement("div"); toggles.className = "toggle-row";
      const mkToggle = (label, active, onClick) => {
        const b = document.createElement("button"); b.textContent = label; b.type = "button";
        b.classList.toggle("active", active); b.addEventListener("click", onClick);
        toggles.appendChild(b); return b;
      };
      mkToggle("B", m.bold, () => { m.bold = !m.bold; o.set({ fontWeight: m.bold ? "bold" : "normal" }); fabricCanvas.requestRenderAll(); refreshContext(o); scheduleCommit(); });
      mkToggle("I", m.italic, () => { m.italic = !m.italic; o.set({ fontStyle: m.italic ? "italic" : "normal" }); fabricCanvas.requestRenderAll(); refreshContext(o); scheduleCommit(); });
      mkToggle("U", m.underline, () => { m.underline = !m.underline; o.set({ underline: m.underline }); fabricCanvas.requestRenderAll(); refreshContext(o); scheduleCommit(); });
      addField("Style", toggles);

      const alignRow = document.createElement("div"); alignRow.className = "toggle-row";
      ["left", "center", "right"].forEach((al) => {
        mkToggleInto(alignRow, al[0].toUpperCase(), m.align === al, () => { m.align = al; o.set({ textAlign: al }); fabricCanvas.requestRenderAll(); refreshContext(o); scheduleCommit(); });
      });
      addField("Align", alignRow);

      rangeField("Font size", m.font_size, 6, 96, 1, (e) => { m.font_size = parseFloat(e.target.value); o.set({ fontSize: m.font_size * currentScale() }); fabricCanvas.requestRenderAll(); scheduleCommit(); });
      colorField("Color", m.color, (e) => { m.color = e.target.value; o.set({ fill: e.target.value }); fabricCanvas.requestRenderAll(); scheduleCommit(); });
      rangeField("Opacity", m.opacity, 0.1, 1, 0.05, (e) => { m.opacity = parseFloat(e.target.value); o.set({ opacity: parseFloat(e.target.value) }); fabricCanvas.requestRenderAll(); scheduleCommit(); });
    } else if (o._modelType === "image") {
      title.textContent = "Image";
      const m = findModelObject(o._modelId);
      if (m) rangeField("Opacity", m.opacity, 0.1, 1, 0.05, (e) => { m.opacity = parseFloat(e.target.value); o.set({ opacity: parseFloat(e.target.value) }); fabricCanvas.requestRenderAll(); scheduleCommit(); });
      const hint = document.createElement("p"); hint.style.fontSize = "12px"; hint.style.color = "var(--text-muted)";
      hint.textContent = "Drag the corner handles to resize, or drag the top handle to rotate.";
      body.appendChild(hint);
    } else {
      panel.hidden = true;
    }
  }

  function mkToggleInto(container, label, active, onClick) {
    const b = document.createElement("button"); b.textContent = label; b.type = "button";
    b.classList.toggle("active", active); b.addEventListener("click", onClick);
    container.appendChild(b);
  }

  function refreshContext(o) { showContextPanelFor(o); }

  let commitTimer = null;
  function scheduleCommit() {
    clearTimeout(commitTimer);
    commitTimer = setTimeout(() => { pushHistory(); markDirtyAndAutosave(); }, 400);
  }

  $("btnCloseContext").addEventListener("click", () => { fabricCanvas.discardActiveObject(); fabricCanvas.requestRenderAll(); hideContextPanel(); });

  // ---------------------------------------------------------------------
  // Page thumbnails: render, select, rotate, delete, drag-reorder
  // ---------------------------------------------------------------------
  async function renderAllThumbnails() {
    const list = $("pageList");
    list.innerHTML = "";
    for (const originalIndex of state.pageOrder) {
      list.appendChild(await buildThumbnailEl(originalIndex));
    }
  }

  async function buildThumbnailEl(originalIndex) {
    const wrap = document.createElement("div");
    wrap.className = "page-thumb" + (originalIndex === state.currentPageOriginalIndex ? " active" : "");
    wrap.draggable = true;
    wrap.dataset.originalIndex = String(originalIndex);

    const canvas = document.createElement("canvas");
    wrap.appendChild(canvas);
    const label = document.createElement("div");
    label.className = "page-thumb-label";
    label.textContent = `Page ${state.pageOrder.indexOf(originalIndex) + 1}`;
    wrap.appendChild(label);

    const actions = document.createElement("div");
    actions.className = "page-thumb-actions";
    const rotateBtn = iconButton("⟳", "Rotate page");
    const deleteBtn = iconButton("✕", "Delete page");
    actions.appendChild(rotateBtn); actions.appendChild(deleteBtn);
    wrap.appendChild(actions);

    rotateBtn.addEventListener("click", async (e) => { e.stopPropagation(); await rotatePage(originalIndex); });
    deleteBtn.addEventListener("click", async (e) => { e.stopPropagation(); await deletePage(originalIndex); });
    wrap.addEventListener("click", () => loadPage(originalIndex));

    wrap.addEventListener("dragstart", (e) => { wrap.classList.add("dragging"); e.dataTransfer.setData("text/plain", String(originalIndex)); });
    wrap.addEventListener("dragend", () => wrap.classList.remove("dragging"));
    wrap.addEventListener("dragover", (e) => e.preventDefault());
    wrap.addEventListener("drop", (e) => {
      e.preventDefault();
      const draggedIndex = parseInt(e.dataTransfer.getData("text/plain"), 10);
      reorderPages(draggedIndex, originalIndex);
    });

    try {
      const pdfPage = await state.pdfDoc.getPage(originalIndex + 1);
      const rotation = totalRotationFor(originalIndex, pdfPage);
      const targetWidth = 150;
      const unscaled = pdfPage.getViewport({ scale: 1, rotation });
      const thumbScale = targetWidth / unscaled.width;
      const viewport = pdfPage.getViewport({ scale: thumbScale, rotation });
      canvas.width = viewport.width; canvas.height = viewport.height;
      await pdfPage.render({ canvasContext: canvas.getContext("2d"), viewport }).promise;
    } catch (_) { /* thumbnail render is best-effort */ }

    return wrap;
  }

  function iconButton(text, title) {
    const b = document.createElement("button"); b.textContent = text; b.title = title; b.type = "button";
    return b;
  }

  async function rotatePage(originalIndex) {
    state.pageRotations[originalIndex] = ((state.pageRotations[originalIndex] || 0) + 90) % 360;
    await renderAllThumbnails();
    if (originalIndex === state.currentPageOriginalIndex) await loadPage(originalIndex);
    pushHistory();
    markDirtyAndAutosave();
  }

  async function deletePage(originalIndex) {
    if (state.pageOrder.length <= 1) { toast("A PDF needs at least one page."); return; }
    if (!confirm("Delete this page? You can undo this with Ctrl+Z.")) return;
    const wasCurrent = originalIndex === state.currentPageOriginalIndex;
    state.pageOrder = state.pageOrder.filter((i) => i !== originalIndex);
    await renderAllThumbnails();
    if (wasCurrent) await loadPage(state.pageOrder[0]);
    pushHistory();
    markDirtyAndAutosave();
  }

  async function reorderPages(draggedIndex, dropOnIndex) {
    if (draggedIndex === dropOnIndex) return;
    const from = state.pageOrder.indexOf(draggedIndex);
    const to = state.pageOrder.indexOf(dropOnIndex);
    if (from === -1 || to === -1) return;
    state.pageOrder.splice(from, 1);
    state.pageOrder.splice(to, 0, draggedIndex);
    await renderAllThumbnails();
    pushHistory();
    markDirtyAndAutosave();
  }

  function highlightActiveThumbnail() {
    document.querySelectorAll(".page-thumb").forEach((el) => {
      el.classList.toggle("active", parseInt(el.dataset.originalIndex, 10) === state.currentPageOriginalIndex);
    });
  }

  function updatePageIndicator() {
    const pos = state.pageOrder.indexOf(state.currentPageOriginalIndex) + 1;
    $("pageIndicator").textContent = `Page ${pos} of ${state.pageOrder.length}`;
  }

  // ---------------------------------------------------------------------
  // Zoom
  // ---------------------------------------------------------------------
  function setZoom(z) {
    state.zoom = clamp(z, 0.25, 4);
    const pct = Math.round(state.zoom * 100) + "%";
    $("zoomLabel").textContent = pct; $("zoomIndicator").textContent = pct;
    loadPage(state.currentPageOriginalIndex);
  }
  $("btnZoomIn").addEventListener("click", () => setZoom(state.zoom + 0.1));
  $("btnZoomOut").addEventListener("click", () => setZoom(state.zoom - 0.1));
  $("btnZoomFit").addEventListener("click", () => {
    const available = $("canvasArea").clientWidth - 72;
    if (pageRasterCanvas && available > 0) {
      const fitScale = (available / pageRasterCanvas.width) * state.zoom;
      setZoom(clamp(fitScale, 0.25, 4));
    } else {
      setZoom(1);
    }
  });

  // ---------------------------------------------------------------------
  // Undo / redo
  // ---------------------------------------------------------------------
  function snapshotState() {
    return JSON.parse(JSON.stringify({
      pageOrder: state.pageOrder, pageRotations: state.pageRotations,
      textEdits: state.textEdits, objectsByPage: state.objectsByPage,
    }));
  }

  function pushHistory() {
    const snap = snapshotState();
    history.stack = history.stack.slice(0, history.index + 1);
    history.stack.push(snap);
    if (history.stack.length > 60) history.stack.shift();
    history.index = history.stack.length - 1;
    updateUndoRedoButtons();
  }

  async function restoreSnapshot(snap) {
    state.pageOrder = snap.pageOrder;
    state.pageRotations = snap.pageRotations;
    state.textEdits = snap.textEdits;
    state.objectsByPage = snap.objectsByPage;
    if (!state.pageOrder.includes(state.currentPageOriginalIndex)) {
      state.currentPageOriginalIndex = state.pageOrder[0];
    }
    await renderAllThumbnails();
    await loadPage(state.currentPageOriginalIndex);
  }

  async function undo() {
    if (history.index <= 0) return;
    history.index--;
    await restoreSnapshot(history.stack[history.index]);
    updateUndoRedoButtons();
    markDirtyAndAutosave();
  }
  async function redo() {
    if (history.index >= history.stack.length - 1) return;
    history.index++;
    await restoreSnapshot(history.stack[history.index]);
    updateUndoRedoButtons();
    markDirtyAndAutosave();
  }
  function updateUndoRedoButtons() {
    $("btnUndo").disabled = history.index <= 0;
    $("btnRedo").disabled = history.index >= history.stack.length - 1;
  }
  $("btnUndo").addEventListener("click", undo);
  $("btnRedo").addEventListener("click", redo);

  // ---------------------------------------------------------------------
  // Save / Export / Download
  // ---------------------------------------------------------------------
  function setSaveStatus(mode) {
    const el = $("saveStatus");
    el.className = "save-status";
    if (mode === "saving") { el.classList.add("saving"); el.textContent = "Saving…"; }
    else if (mode === "error") { el.classList.add("error"); el.textContent = "Save failed — retrying"; }
    else if (mode === "unsaved") { el.textContent = "Unsaved changes"; }
    else { el.textContent = "All changes saved"; }
  }

  function buildEditorStatePayload() {
    const originalOrder = Array.from({ length: originalPageCount }, (_, i) => i);
    const orderChanged = JSON.stringify(state.pageOrder) !== JSON.stringify(originalOrder);
    const hasRotation = Object.values(state.pageRotations).some((v) => (v % 360) !== 0);
    const page_ops = (orderChanged || hasRotation) ? {
      order: state.pageOrder.slice(),
      rotations: Object.fromEntries(Object.entries(state.pageRotations).filter(([, v]) => (v % 360) !== 0)),
    } : null;
    return { text_edits: { ...state.textEdits }, page_ops, objects_by_page: sanitizeObjectsForBackend(state.objectsByPage) };
  }

  function sanitizeObjectsForBackend(objectsByPage) {
    // Strips client-only cache fields (leading underscore, e.g.
    // `_previewDataUrl`) so autosave payloads don't ship base64 image
    // bytes the backend doesn't need — it re-extracts real image bytes
    // from the original PDF via `source_xref` at export time.
    const out = {};
    for (const [page, objects] of Object.entries(objectsByPage)) {
      out[page] = objects.map((obj) => {
        const clean = {};
        for (const [k, v] of Object.entries(obj)) if (!k.startsWith("_")) clean[k] = v;
        return clean;
      });
    }
    return out;
  }

  function markDirtyAndAutosave() {
    dirty = true;
    setSaveStatus("unsaved");
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { queueSave(); }, 1200);
  }

  // If the user is still actively typing inside a text box (existing-text
  // edit OR a brand-new text box) when Save/Download is clicked, that last
  // keystroke never reaches state.textEdits / the object model — it only
  // commits on fabric's "editing:exited" event, which a button click does
  // NOT fire. That's the root cause of "I edited it but the download
  // doesn't have my last change": Save/Download must force any in-progress
  // text edit to commit BEFORE state is read, not just whenever the user
  // happens to click away first.
  function commitActiveTextEditing() {
    if (!fabricCanvas) return;
    const obj = fabricCanvas.getActiveObject();
    if (obj && obj.isEditing && typeof obj.exitEditing === "function") {
      obj.exitEditing(); // synchronously fires "editing:exited" -> commitTextEdit / the new-text handler
    }
  }

  // Saves are chained (never fired concurrently) so a slow save in flight
  // can't be overtaken and overwritten by a later one that started after
  // it but whose response arrives first — the classic async/race path to
  // "download used a stale state".
  let saveChain = Promise.resolve();

  function queueSave() {
    clearTimeout(saveTimer);
    saveChain = saveChain.then(performSave, performSave);
    saveInFlight = saveChain;
    return saveChain;
  }

  async function performSave() {
    if (!state.documentId || !dirty) return;
    setSaveStatus("saving");
    const payload = buildEditorStatePayload();
    dirty = false; // cleared BEFORE the request: any edit made during the request re-flags dirty for the next save
    try {
      await apiFetch(`/documents/${state.documentId}/state`, {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      setSaveStatus("saved");
    } catch (err) {
      dirty = true; // restore — nothing was actually persisted, so the retry must resend it
      setSaveStatus("error");
      toast(friendlyError(err, "Couldn't save your changes — will retry."), true);
      clearTimeout(saveTimer);
      saveTimer = setTimeout(() => queueSave(), 4000);
    }
  }

  async function flushSave() {
    commitActiveTextEditing();
    clearTimeout(saveTimer);
    if (dirty) { await queueSave(); return; }
    if (saveInFlight) await saveInFlight;
  }

  $("btnSave").addEventListener("click", async () => {
    $("btnSave").disabled = true;
    try { await flushSave(); toast("Saved."); }
    finally { $("btnSave").disabled = false; }
  });

  $("btnDownload").addEventListener("click", async () => {
    const btn = $("btnDownload");
    btn.disabled = true;
    showOverlay("Preparing download…");
    try {
      await flushSave();
      setStatusMessage("Generating PDF…");
      const exportRes = await apiFetch(`/documents/${state.documentId}/export`, { method: "POST" });
      const exportJson = await exportRes.json();

      setStatusMessage("Downloading…");
      const fileRes = await apiFetch(exportJson.download_url, { method: "GET", cache: "no-store" });
      const blob = await fileRes.blob();
      if (!blob || blob.size === 0) throw new Error("empty");

      const disposition = fileRes.headers.get("Content-Disposition") || "";
      const match = /filename="?([^"]+)"?/.exec(disposition);
      const filename = match ? match[1] : suggestedFilename();

      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 4000);

      setStatusMessage("Download ready");
      toast("Downloaded " + filename);
    } catch (err) {
      toast(friendlyError(err, "PDF export failed. Please try again."), true);
      setStatusMessage("Export failed");
    } finally {
      btn.disabled = false;
      hideOverlay();
    }
  });

  function suggestedFilename() {
    const name = state.documentModel ? state.documentModel.filename : "document.pdf";
    const dot = name.lastIndexOf(".");
    const stem = dot > 0 ? name.slice(0, dot) : name;
    return `${stem}-edited.pdf`;
  }

  // ---------------------------------------------------------------------
  // Search & replace
  // ---------------------------------------------------------------------
  function toggleSearchPanel(show) {
    $("searchPanel").hidden = !show;
    if (show) $("searchQuery").focus();
  }
  $("btnCloseSearch").addEventListener("click", () => toggleSearchPanel(false));

  $("btnSearch").addEventListener("click", async () => {
    const query = $("searchQuery").value;
    if (!query) { $("searchResultCount").textContent = ""; return; }
    try {
      const res = await apiFetch(`/documents/${state.documentId}/search`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ query, case_sensitive: $("caseSensitive").checked, whole_word: $("wholeWord").checked }),
      });
      const matches = await res.json();
      $("searchResultCount").textContent = matches.length ? `${matches.length} match(es)` : "No matches";
      if (matches.length) {
        const pageOfFirst = matches[0].page;
        if (pageOfFirst !== state.currentPageOriginalIndex) await loadPage(pageOfFirst);
      }
    } catch (err) {
      toast(friendlyError(err, "Search failed."), true);
    }
  });

  $("btnReplaceAll").addEventListener("click", async () => {
    const query = $("searchQuery").value;
    if (!query) return;
    try {
      const res = await apiFetch(`/documents/${state.documentId}/replace`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          query, replacement: $("replaceText").value,
          case_sensitive: $("caseSensitive").checked, whole_word: $("wholeWord").checked, replace_all: true,
        }),
      });
      const result = await res.json();
      $("searchResultCount").textContent = `Replaced ${result.replaced_count}`;

      // The backend applied the replacements to its own edit set directly;
      // pull the refreshed document so our local `textEdits` (the source
      // of truth for the next autosave) stays in sync and doesn't clobber
      // the replacement on the next Save.
      const docRes = await apiFetch(`/documents/${state.documentId}`, {});
      const refreshed = await docRes.json();
      for (const page of refreshed.pages) {
        for (const el of page.text_elements) {
          if (el.edited) state.textEdits[el.id] = el.text;
        }
      }
      await loadPage(state.currentPageOriginalIndex);
      pushHistory();
      markDirtyAndAutosave();
      toast(`Replaced ${result.replaced_count} occurrence(s).`);
    } catch (err) {
      toast(friendlyError(err, "Replace failed."), true);
    }
  });

  // ---------------------------------------------------------------------
  // Overlay / loading indicator
  // ---------------------------------------------------------------------
  function showOverlay(msg) { $("overlayMessage").textContent = msg; $("canvasOverlay").hidden = false; }
  function hideOverlay() { $("canvasOverlay").hidden = true; }

  // ---------------------------------------------------------------------
  // Mobile sidebar toggle
  // ---------------------------------------------------------------------
  function isMobile() { return window.matchMedia("(max-width: 860px)").matches; }
  function updateMobileChrome() {
    $("btnShowSidebar").classList.toggle("visible", isMobile());
    if (!isMobile()) $("sidebar").classList.remove("hidden-mobile");
  }
  window.addEventListener("resize", updateMobileChrome);
  updateMobileChrome();
  $("btnToggleSidebar").addEventListener("click", () => $("sidebar").classList.add("hidden-mobile"));
  $("btnShowSidebar").addEventListener("click", () => $("sidebar").classList.remove("hidden-mobile"));

  // Warn before leaving with unsaved changes.
  window.addEventListener("beforeunload", (e) => {
    if (dirty) { e.preventDefault(); e.returnValue = ""; }
  });
})();

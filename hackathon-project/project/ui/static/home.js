const homeState = {pdf: null, pptx: null, pdfBusy: false, pptxBusy: false, activePptx: null};
const pdfInspection = {document: null, mode: "zoning"};
const home$ = selector => document.querySelector(selector);
const homeEsc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

window.showAppScreen = function (screen) {
    const app = home$(".app");
    app.classList.remove("home-view", "history-view", "review-view", "batch-view");
    app.classList.add(screen === "home" ? "home-view" : screen === "history" ? "history-view" :
        screen === "batch" ? "batch-view" : "review-view");
    home$("#home").hidden = screen !== "home";
    home$("#history").hidden = screen !== "history";
    const reviewVisible = screen === "review" || screen === "batch";
    home$("#library").hidden = !reviewVisible;
    home$("#main").hidden = !reviewVisible;
    home$("#dash").hidden = true;
    setActive(home$("#nav-home"), screen === "home" || screen === "review");
    setActive(home$("#nav-history"), screen === "history");
};

home$("#nav-home").onclick = () => window.showAppScreen("home");
home$("#nav-history").onclick = () => {
    window.showAppScreen("history");
    loadHistory().catch(error => { home$("#history-list").textContent = error.message; });
};
home$("#history-refresh").onclick = () => loadHistory().catch(error => { home$("#history-list").textContent = error.message; });
home$("#open-batch").onclick = () => home$("#batch").click();

function setUploadStatus(kind, message, state = "") {
    const status = home$(kind === "pdf" ? "#home-pdf-status" : "#home-pptx-status");
    status.textContent = message;
    status.dataset.state = state;
}

function fileSize(size) {
    return new Intl.NumberFormat(undefined, {style: "unit", unit: "kilobyte", maximumFractionDigits: 1})
        .format(size / 1000);
}

function syncSelection(kind) {
    const file = homeState[kind];
    const prefix = kind === "pdf" ? "#pdf" : "#pptx";
    home$(prefix + "-selection").hidden = !file;
    home$(prefix + "-selected-name").textContent = file?.name || "";
    home$(prefix + "-selected-size").textContent = file ? fileSize(file.size) : "";
    home$(prefix + "-process").disabled = !file || homeState[kind + "Busy"];
    home$(prefix + "-remove").disabled = homeState[kind + "Busy"];
    home$(prefix + "-replace").disabled = homeState[kind + "Busy"];
}

async function validateSelectedFile(file, kind) {
    const extension = kind === "pdf" ? ".pdf" : ".pptx";
    const typeLabel = kind === "pdf" ? "PDF" : "PowerPoint";
    if (!file || !file.name.toLowerCase().endsWith(extension)) {
        throw new Error("That file is not a " + typeLabel + ". Choose a " + extension + " file.");
    }
    if (!file.size || file.size >= 64 * 1024 * 1024) {
        throw new Error("Choose a non-empty file smaller than 64 MB.");
    }
    const bytes = new Uint8Array(await file.slice(0, 8).arrayBuffer());
    const isPdf = kind === "pdf" && String.fromCharCode(...bytes.slice(0, 5)) === "%PDF-";
    const isZip = kind === "pptx" && bytes[0] === 0x50 && bytes[1] === 0x4b &&
        [0x03, 0x05, 0x07].includes(bytes[2]) && [0x04, 0x06, 0x08].includes(bytes[3]);
    if (!(isPdf || isZip)) {
        throw new Error("This file does not have a valid " + typeLabel + " file signature.");
    }
}

async function selectHomeFile(kind, files) {
    if (homeState[kind + "Busy"]) return;
    const selected = Array.from(files || []);
    if (selected.length !== 1) {
        setUploadStatus(kind, "Choose one file at a time.", "error");
        return;
    }
    setUploadStatus(kind, "Checking file...", "");
    try {
        await validateSelectedFile(selected[0], kind);
        if (kind === "pdf" && homeState.pdfBusy) return;
        homeState[kind] = selected[0];
        home$(kind === "pdf" ? "#pdf-result" : "#pptx-result").hidden = true;
        if (kind === "pdf") resetPdfInspection();
        setUploadStatus(kind, "File ready. Select Process to begin.");
        syncSelection(kind);
    } catch (error) {
        setUploadStatus(kind, error.message, "error");
    }
}

function setupDrop(kind) {
    const prefix = kind === "pdf" ? "pdf" : "pptx";
    const zone = home$("#" + prefix + "-drop");
    const input = home$("#home-" + prefix + "-file");
    zone.addEventListener("click", () => input.click());
    zone.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); input.click(); }
    });
    input.addEventListener("change", event => {
        selectHomeFile(kind, event.target.files);
        input.value = "";
    });
    for (const name of ["dragenter", "dragover"]) zone.addEventListener(name, event => {
        event.preventDefault(); event.stopPropagation(); zone.dataset.dragging = "true";
    });
    zone.addEventListener("dragleave", event => {
        event.preventDefault(); event.stopPropagation();
        if (!zone.contains(event.relatedTarget)) delete zone.dataset.dragging;
    });
    zone.addEventListener("drop", event => {
        event.preventDefault(); event.stopPropagation(); delete zone.dataset.dragging;
        selectHomeFile(kind, event.dataTransfer.files);
    });
    home$("#" + prefix + "-replace").onclick = () => input.click();
    home$("#" + prefix + "-remove").onclick = () => {
        if (homeState[kind + "Busy"]) return;
        homeState[kind] = null;
        home$(kind === "pdf" ? "#pdf-result" : "#pptx-result").hidden = true;
        if (kind === "pdf") resetPdfInspection();
        setUploadStatus(kind, "File removed.");
        syncSelection(kind);
    };
}

function uploadWithProgress(url, form, progress) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", url);
        xhr.responseType = "json";
        xhr.upload.onprogress = event => {
            if (event.lengthComputable) progress(event.loaded, event.total);
        };
        xhr.onload = () => xhr.status === 202 ? resolve(xhr.response) :
            reject(new Error(xhr.response?.error || "Upload failed (" + xhr.status + ")."));
        xhr.onerror = () => reject(new Error("The upload could not reach the local workspace."));
        xhr.send(form);
    });
}

function friendlyStage(job) {
    const stage = job?.processing?.stage || job?.processing?.last_ocr_attempt?.stage;
    return ({diagnostics: "Checking the PDF", rendering: "Preparing pages", detection: "Finding page structure",
        ocr: "Reading page text", ocr_attempt: "Reading page text", extracted: "Organizing document content",
        reconstruction: "Organizing document content", validation: "Checking the XML output"})[stage] || "Processing the PDF";
}

function resetPdfInspection(message = "Process the PDF first to see inspection results.") {
    pdfInspection.document = null;
    home$("#pdf-inspection").hidden = !homeState.pdf;
    if (!homeState.pdf) home$("#pdf-inspection").open = false;
    const status = home$("#pdf-inspection-status");
    status.hidden = false;
    status.textContent = message;
    home$("#pdf-inspection-page").hidden = true;
    const results = home$("#pdf-inspection-results");
    results.hidden = true;
    results.replaceChildren();
}

function showPdfInspection(data) {
    if (!data.pages?.length) {
        resetPdfInspection("No page inspection results are available for this PDF.");
        return;
    }
    pdfInspection.document = data;
    home$("#pdf-inspection-status").hidden = true;
    const select = home$("#pdf-inspection-page-select");
    select.replaceChildren(...data.pages.map((page, index) => new Option("Page " + page.n, index)));
    home$("#pdf-inspection-page").hidden = false;
    home$("#pdf-inspection-results").hidden = false;
    renderPdfInspection();
}

function renderPdfZoning(data, page) {
    const regions = page.layout_regions || [];
    const source = page.diag.layout?.engine || regions[0]?.source;
    const boxes = zoningOverlay(regions.filter(region => region.display_bbox));
    const image = "/api/documents/" + encodeURIComponent(data.id) + "/page/" + page.n + ".png?dpi=100";
    const summary = regions.length ? [source, regions.length + " detected regions"].filter(Boolean).join(" · ") :
        page.diag.layout?.error || "No layout detections are available for this page.";
    return '<div class="legend">' + homeEsc(summary) + '</div><div class="viewer pdf-inspection-preview">' +
        '<div class="page"><img loading="lazy" src="' + image + '" alt="Page ' + page.n + '">' +
        (boxes ? '<svg viewBox="0 0 ' + page.w + ' ' + page.h +
            '" preserveAspectRatio="none" aria-hidden="true">' + boxes + '</svg>' : '') + '</div></div>' +
        (regions.length ? '<ul class="pdf-inspection-regions" aria-label="Detected elements">' +
            regions.map(region => '<li>' + homeEsc(regionLabel(region)) +
                (region.display_bbox ? ' · ' + homeEsc(region.display_bbox.map(value => value.toFixed(1)).join(", ")) + ' pt' : '') +
                '</li>').join("") + '</ul>' : '');
}

function renderPdfMetadata(data, page) {
    const fields = [
        ["File", data.filename],
        ["Title", data.title?.value],
        ["Language", languageLabel(data.language) || data.lang],
        ["Pages", data.pages.length],
        ["Page size", Number.isFinite(page.w) && Number.isFinite(page.h) ? page.w.toFixed(1) + " × " + page.h.toFixed(1) + " pt" : ""],
        ["Title extraction confidence", Number.isFinite(data.title?.confidence) ? data.title.confidence.toFixed(1) + "%" : ""]
    ];
    return '<div class="card region-detail"><dl>' + fields.filter(([, value]) => value != null && value !== "")
        .map(([label, value]) => '<dt>' + label + '</dt><dd>' + homeEsc(value) + '</dd>').join("") +
        '</dl></div>' + pageDiagnostics(page);
}

function renderPdfInspection() {
    const data = pdfInspection.document;
    if (!data) return;
    const page = data.pages[home$("#pdf-inspection-page-select").value];
    const results = home$("#pdf-inspection-results");
    if (pdfInspection.mode === "zoning") results.innerHTML = renderPdfZoning(data, page);
    else if (pdfInspection.mode === "order") results.innerHTML = page.blocks.length ?
        renderTextBlocks(orderedBlocks(page), true) : '<p>No extracted content is available for this page.</p>';
    else results.innerHTML = renderPdfMetadata(data, page);
}

home$("#pdf-inspection-tabs").onclick = event => {
    const mode = event.target.dataset.inspection;
    if (!mode) return;
    pdfInspection.mode = mode;
    home$("#pdf-inspection-tabs").querySelectorAll("button").forEach(button =>
        setActive(button, button.dataset.inspection === mode));
    renderPdfInspection();
};
home$("#pdf-inspection-page-select").onchange = renderPdfInspection;

function renderPdfResult(id, report) {
    const card = home$("#pdf-result");
    const xml = report?.outputs?.xml;
    const draft = report.status === "manual_review_required";
    const exportUrl = "/api/documents/" + encodeURIComponent(id) + "/presentations/" + encodeURIComponent(report?.id || "");
    card.dataset.state = draft ? "review" : "success";
    card.innerHTML = (draft ? '<strong>Source review required</strong><span>' + homeEsc(report.error) + '</span>' :
        '<strong>XML ready</strong><span>' + homeEsc(xml?.filename || "Structured XML was generated.") + '</span>' +
        '<a class="btn pri" href="' + exportUrl + '/xml?format=auto">Download XML</a>') +
        '<button type="button" class="btn" id="open-pdf-review">Review extracted document</button>';
    card.hidden = false;
    home$("#open-pdf-review").onclick = () => window.showAppScreen("review");
}

async function pollPdf(id) {
    const progress = home$("#home-pdf-progress");
    const base = "/api/documents/" + encodeURIComponent(id);
    while (true) {
        const data = await workspaceFetch(base);
        const job = data.jobs[0];
        setUploadStatus("pdf", friendlyStage(job) + (job.total ? " · " + job.done + "/" + job.total + " pages" : ""));
        if (job.total > 0) { progress.max = job.total; progress.value = job.done; }
        if (job.status === "completed") {
            showPdfInspection(data);
            const reports = await workspaceFetch(base + "/presentations");
            const report = reports.find(item => item.mode === "pdf_processing") || reports[0];
            if (!["completed", "xml_only", "manual_review_required"].includes(report?.status) || !report.outputs?.xml) {
                throw new Error(report?.error || "The PDF finished, but a validated XML output was not published.");
            }
            await loadWorkspace(id);
            renderPdfResult(id, report);
            setUploadStatus("pdf", report.status === "manual_review_required" ?
                "Processing complete. Review the source before export." : "Processing complete.", "success");
            loadHistory().catch(() => {});
            return;
        }
        if (job.status === "failed") throw new Error(job.error || "The PDF could not be processed. Try again.");
        await pause(850);
    }
}

home$("#pdf-process").onclick = async () => {
    const file = homeState.pdf;
    if (!file || homeState.pdfBusy) return;
    homeState.pdfBusy = true;
    syncSelection("pdf");
    const progress = home$("#home-pdf-progress");
    progress.hidden = false; progress.removeAttribute("value");
    home$("#pdf-result").hidden = true;
    resetPdfInspection("PDF processing is in progress. Inspection results will appear here when it finishes.");
    setUploadStatus("pdf", "Uploading PDF...");
    const form = new FormData();
    form.append("pdf", file);
    form.append("phase", home$("#phase-select").value);
    form.append("ocr_engine", home$("#home-ocr-engine").value);
    form.append("ocr_language", "");
    try {
        const job = await uploadWithProgress("/api/documents", form, (loaded, total) => {
            progress.max = total; progress.value = loaded;
        });
        progress.removeAttribute("value");
        setUploadStatus("pdf", "PDF received. Processing has started...");
        await loadWorkspace(job.id);
        await pollPdf(job.id);
    } catch (error) {
        if (!pdfInspection.document) resetPdfInspection("Process the PDF again to see inspection results.");
        setUploadStatus("pdf", error.message, "error");
        home$("#pdf-result").hidden = false;
        home$("#pdf-result").dataset.state = "error";
        home$("#pdf-result").textContent = error.message;
    } finally {
        homeState.pdfBusy = false;
        progress.hidden = true;
        syncSelection("pdf");
    }
};

function renderPptxResult(job) {
    const result = job.result || {};
    const corrections = [...(result.applied || []), ...(result.review || [])];
    const issues = corrections.length ? '<details><summary>View proofreading results (' + corrections.length + ')</summary>' +
        corrections.map(item => '<article class="job-issue"><strong>Slide ' + homeEsc(item.slide) + ' · ' + homeEsc(item.issue) +
            ' · ' + homeEsc(item.action) + '</strong><small>Original: ' + homeEsc(item.old_text) + '</small>' +
            '<small>Suggestion: ' + homeEsc(item.corrected_text) + '</small><small>' + homeEsc(item.reason) +
            '</small></article>').join("") + '</details>' : '<span>No high-confidence writing corrections were needed.</span>';
    const card = home$("#pptx-result");
    card.dataset.state = "success";
    card.innerHTML = '<strong>Proofreading complete</strong><span>' + homeEsc(job.filename) + ' · ' +
        homeEsc(result.slides) + ' slides · ' + homeEsc(result.paragraphs_reviewed || 0) + ' text paragraphs reviewed · ' +
        homeEsc(result.applied?.length || 0) + ' corrections · ' +
        homeEsc(result.review?.length || 0) + ' suggestions to review</span><a class="btn pri" href="' +
        homeEsc(result.download_url || ("/api/presentations/" + encodeURIComponent(job.id) + "/download")) +
        '">Download corrected PowerPoint</a>' + issues;
    card.hidden = false;
}

async function pollPptx(jobId) {
    while (true) {
        const job = await workspaceFetch("/api/presentations/" + encodeURIComponent(jobId));
        if (job.progress?.message) setUploadStatus("pptx", job.progress.message);
        if (job.status === "completed") {
            renderPptxResult(job);
            setUploadStatus("pptx", "Proofreading complete.", "success");
            loadHistory().catch(() => {});
            return;
        }
        if (job.status === "failed") throw new Error(job.error || "The presentation could not be proofread. Try again.");
        await pause(700);
    }
}

home$("#pptx-process").onclick = async () => {
    const file = homeState.pptx;
    if (!file || homeState.pptxBusy) return;
    homeState.pptxBusy = true;
    syncSelection("pptx");
    const progress = home$("#home-pptx-progress");
    progress.hidden = false; progress.removeAttribute("value");
    home$("#pptx-result").hidden = true;
    setUploadStatus("pptx", "Uploading PowerPoint...");
    const form = new FormData(); form.append("pptx", file); form.append("phase", home$("#pptx-phase-select").value);
    try {
        const job = await uploadWithProgress("/api/presentations", form, (loaded, total) => {
            progress.max = total; progress.value = loaded;
        });
        homeState.activePptx = job.id;
        progress.removeAttribute("value");
        setUploadStatus("pptx", "Presentation received. Proofreading has started...");
        await pollPptx(job.id);
    } catch (error) {
        setUploadStatus("pptx", error.message, "error");
        home$("#pptx-result").hidden = false;
        home$("#pptx-result").dataset.state = "error";
        home$("#pptx-result").textContent = error.message;
    } finally {
        homeState.pptxBusy = false;
        progress.hidden = true;
        syncSelection("pptx");
    }
};

function statusLabel(status) {
    return ({queued: "Queued", running: "Processing", completed: "Completed", failed: "Failed"})[status] || status;
}

function renderHistory(data) {
    const items = [];
    for (const item of data.pdfs || []) {
        items.push('<article class="history-item"><div class="history-item-main"><strong>' + homeEsc(item.filename) +
            '</strong><span class="history-item-meta">PDF · Phase ' + homeEsc(item.phase) + ' · ' +
            homeEsc(new Date(item.created).toLocaleString()) + (item.total ? ' · ' + homeEsc(item.done) + '/' + homeEsc(item.total) + ' pages' : '') +
            '</span></div><span class="history-status ' + (item.status === "failed" ? "failed" : "") + '">' +
            homeEsc(statusLabel(item.status)) + '</span><div class="history-item-actions"><button class="btn" data-history-pdf="' +
            homeEsc(item.id) + '">Open PDF review</button>' + (item.status === "completed" ?
            '<button class="btn" data-history-xml="' + homeEsc(item.id) + '">Download XML</button>' : '') + '</div></article>');
    }
    for (const item of data.presentations || []) {
        items.push('<article class="history-item"><div class="history-item-main"><strong>' + homeEsc(item.filename) +
            '</strong><span class="history-item-meta">PowerPoint · Phase ' + homeEsc(item.phase) + ' · ' +
            homeEsc(new Date(item.created).toLocaleString()) + '</span></div><span class="history-status ' +
            (item.status === "failed" ? "failed" : "") + '">' + homeEsc(statusLabel(item.status)) +
            '</span><div class="history-item-actions"><button class="btn" data-history-pptx="' + homeEsc(item.id) + '">View results</button>' +
            (item.status === "completed" ? '<a class="btn" href="/api/presentations/' + encodeURIComponent(item.id) +
                '/download">Download</a>' : '') + '</div></article>');
    }
    home$("#history-list").innerHTML = items.join("") || '<p class="empty">No processing jobs yet. Your PDF and PowerPoint jobs will appear here.</p>';
    home$("#history-list").querySelectorAll("[data-history-pdf]").forEach(button => button.onclick = async () => {
        try { await loadWorkspace(button.dataset.historyPdf); window.showAppScreen("review"); }
        catch (error) { home$("#history-list").prepend(Object.assign(document.createElement("p"), {className: "notice", textContent: error.message})); }
    });
    home$("#history-list").querySelectorAll("[data-history-xml]").forEach(button => button.onclick = async () => {
        button.disabled = true;
        try {
            const base = "/api/documents/" + encodeURIComponent(button.dataset.historyXml);
            const reports = await workspaceFetch(base + "/presentations");
            const ready = item => ["completed", "xml_only"].includes(item.status) && item.outputs?.xml;
            const report = reports.find(item => item.mode === "pdf_processing" && ready(item)) || reports.find(ready);
            if (!report) throw new Error("The validated XML output is no longer available.");
            location.href = base + "/presentations/" + encodeURIComponent(report.id) + "/xml?format=auto";
        } catch (error) {
            button.disabled = false;
            button.textContent = error.message;
        }
    });
    home$("#history-list").querySelectorAll("[data-history-pptx]").forEach(button => button.onclick = async () => {
        try {
            const job = await workspaceFetch("/api/presentations/" + encodeURIComponent(button.dataset.historyPptx));
            window.showAppScreen("home");
            if (job.status === "completed") renderPptxResult(job);
            else {
                const card = home$("#pptx-result");
                card.dataset.state = job.status === "failed" ? "error" : "";
                card.textContent = job.error || job.progress?.message || statusLabel(job.status);
                card.hidden = false;
            }
            setUploadStatus("pptx", statusLabel(job.status), job.status === "failed" ? "error" : "");
        } catch (error) { button.textContent = error.message; }
    });
}

async function loadHistory() {
    const history = await workspaceFetch("/api/history");
    renderHistory(history);
}

async function loadHomeOcrPreference() {
    try {
        const preference = await workspaceFetch("/api/ocr-settings");
        home$("#home-ocr-engine").value = preference.engine === "paddleocr" ? "paddleocr" : "glm-ocr";
    } catch (_) { home$("#home-ocr-engine").value = "glm-ocr"; }
}

home$("#home-ocr-engine").onchange = async event => {
    try {
        await workspaceFetch("/api/ocr-settings", {method: "PATCH", headers: {"Content-Type": "application/json"},
            body: JSON.stringify({engine: event.target.value})});
    } catch (error) { setUploadStatus("pdf", "OCR setting could not be saved: " + error.message, "error"); }
};

setupDrop("pdf");
setupDrop("pptx");
syncSelection("pdf"); syncSelection("pptx");
loadHomeOcrPreference();

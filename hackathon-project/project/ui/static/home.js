const homeState = {pdf: null, pptx: null, uploading: false, activePptx: null, pair: null,
    uploads: {}, selecting: {pdf: 0, pptx: 0}};
const pptxPreview = {slide: 1, total: 0};
const home$ = selector => document.querySelector(selector);
const homeEsc = value => String(value ?? "").replace(/[&<>"']/g, char => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[char]));
const pause = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));

window.showAppScreen = function (screen) {
    const app = home$(".app");
    app.classList.remove("home-view", "history-view", "review-view", "batch-view", "pptx-view");
    app.classList.add(screen === "home" ? "home-view" : screen === "history" ? "history-view" :
        screen === "batch" ? "batch-view" : screen === "pptx" ? "pptx-view" : "review-view");
    home$("#home").hidden = screen !== "home";
    home$("#history").hidden = screen !== "history";
    home$("#pptx-workspace").hidden = screen !== "pptx";
    const reviewVisible = screen === "review" || screen === "batch";
    home$("#library").hidden = !reviewVisible || !W.batch;
    const pair = homeState.pair;
    const paired = !W.batch && pair && ((screen === "review" && S.id === pair.pdf.id) ||
        (screen === "pptx" && homeState.activePptx === pair.pptx.id));
    home$("#document-switch").hidden = !paired;
    app.classList.toggle("paired-workspace", Boolean(paired));
    app.classList.toggle("batch-tools-visible", reviewVisible && W.batch);
    setActive(home$("#switch-pdf"), screen === "review");
    setActive(home$("#switch-pptx"), screen === "pptx");
    home$("#main").hidden = !reviewVisible;
    home$("#dash").hidden = true;
    setActive(home$("#nav-home"), screen === "home" || screen === "review" || screen === "pptx");
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
    home$(prefix + "-remove").disabled = homeState.uploading;
    home$(prefix + "-replace").disabled = homeState.uploading;
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
    if (homeState.uploading) return;
    const selected = Array.from(files || []);
    if (selected.length !== 1) {
        setUploadStatus(kind, "Choose one file at a time.", "error");
        return;
    }
    const selection = ++homeState.selecting[kind];
    setUploadStatus(kind, "Checking file?");
    try {
        await validateSelectedFile(selected[0], kind);
        if (selection !== homeState.selecting[kind] || homeState.uploading) return;
        homeState[kind] = selected[0];
        delete homeState.uploads[kind];
        if (kind === "pdf") delete homeState.uploads.pptx;
        home$("#pair-retry").hidden = true;
        syncSelection(kind);
        const other = kind === "pdf" ? "pptx" : "pdf";
        if (homeState[other]) await uploadPair();
        else setUploadStatus(kind, kind === "pdf" ? "PDF selected. Add a PowerPoint to continue." :
            "PowerPoint selected. Add a PDF to continue.");
    } catch (error) {
        if (selection === homeState.selecting[kind]) setUploadStatus(kind, error.message, "error");
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
        if (homeState.uploading) return;
        homeState.selecting[kind]++;
        homeState[kind] = null;
        delete homeState.uploads[kind];
        if (kind === "pdf") delete homeState.uploads.pptx;
        home$("#pair-retry").hidden = true;
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

async function openPdfWorkspace(id) {
    W.batch = false;
    home$("#batch").textContent = "Batch review";
    for (const control of ["#upload-zone", "#layout-model", "#ocr-controls"]) home$(control).hidden = false;
    S.vtab = "pdf";
    S.stab = "meta";
    for (const group of ["#vtabs", "#stabs"]) {
        home$(group).querySelectorAll("button").forEach(button =>
            setActive(button, button.dataset.t === (group === "#vtabs" ? "pdf" : "meta")));
    }
    await loadWorkspace(id);
    window.showAppScreen("review");
}

async function uploadSelected(kind) {
    if (homeState.uploads[kind]) return;
    const pdf = kind === "pdf";
    const progress = home$("#home-" + kind + "-progress");
    progress.hidden = false;
    progress.removeAttribute("value");
    setUploadStatus(kind, pdf ? "Uploading PDF?" : "Uploading PowerPoint?");
    const form = new FormData();
    form.append(kind, homeState[kind]);
    form.append("phase", home$(pdf ? "#phase-select" : "#pptx-phase-select").value);
    if (pdf) {
        form.append("ocr_engine", home$("#home-ocr-engine").value);
        form.append("defer", "1");
    } else {
        form.append("document_id", homeState.uploads.pdf.id);
    }
    try {
        homeState.uploads[kind] = await uploadWithProgress(pdf ? "/api/documents" : "/api/presentations", form,
            (loaded, total) => { progress.max = total; progress.value = loaded; });
        setUploadStatus(kind, "Uploaded.", "success");
    } catch (error) {
        const message = (pdf ? "PDF" : "PowerPoint") + " upload failed. " + error.message;
        setUploadStatus(kind, message, "error");
        throw new Error(message);
    } finally {
        progress.hidden = true;
    }
}

async function uploadPair() {
    if (homeState.uploading || !homeState.pdf || !homeState.pptx) return;
    homeState.uploading = true;
    home$("#pair-retry").hidden = true;
    for (const kind of ["pdf", "pptx"]) syncSelection(kind);
    try {
        const backend = await workspaceFetch("/api/startup/identity");
        if (backend.workspace_api_version !== 2) throw new Error(workspaceUpdateMessage);
        await uploadSelected("pdf");
        await uploadSelected("pptx");
        homeState.pair = {...homeState.uploads};
        await openPdfWorkspace(homeState.pair.pdf.id);
        openPptxWorkspace(homeState.pair.pptx, false);
        homeState.uploads = {};
        for (const kind of ["pdf", "pptx"]) {
            homeState[kind] = null;
            setUploadStatus(kind, "");
        }
    } catch (error) {
        home$("#pair-error").textContent = error.message;
        home$("#pair-retry").hidden = false;
    } finally {
        homeState.uploading = false;
        for (const kind of ["pdf", "pptx"]) syncSelection(kind);
    }
}

home$("#retry-pair").onclick = uploadPair;
home$("#switch-pdf").onclick = () => {
    const id = homeState.pair.pdf.id;
    if (!W.batch && S.id === id) window.showAppScreen("review");
    else openPdfWorkspace(id).catch(showError);
};
home$("#switch-pptx").onclick = () => {
    const job = homeState.pair.pptx;
    if (homeState.activePptx === job.id) window.showAppScreen("pptx");
    else openPptxWorkspace(job);
};

function renderPptxResult(job) {
    const result = job.result || {};
    const corrections = [...(result.applied || []), ...(result.review || [])];
    const issues = corrections.length ? '<details><summary>View proofreading results (' + corrections.length + ')</summary>' +
        corrections.map(item => '<article class="job-issue"><strong>Slide ' + homeEsc(item.slide) + ' · ' + homeEsc(item.issue) +
            ' · ' + homeEsc(item.action) + '</strong><small>Original: ' + homeEsc(item.old_text) + '</small>' +
            '<small>Suggestion: ' + homeEsc(item.corrected_text) + '</small><small>' + homeEsc(item.reason) +
            '</small></article>').join("") + '</details>' : '<span>No high-confidence writing corrections were needed.</span>';
    const card = home$("#pptx-workspace-results");
    card.innerHTML = '<div><strong>Proofreading complete</strong><span>' + homeEsc(job.filename) + ' · ' +
        homeEsc(result.slides) + ' slides · ' + homeEsc(result.paragraphs_reviewed || 0) + ' text paragraphs reviewed · ' +
        homeEsc(result.applied?.length || 0) + ' corrections · ' +
        homeEsc(result.review?.length || 0) + ' suggestions to review</span><a class="btn pri" href="' +
        homeEsc(result.download_url || ("/api/presentations/" + encodeURIComponent(job.id) + "/download")) +
        '">Download corrected PowerPoint</a>' + issues + '</div>';
    card.hidden = false;
}

function pptxSlide() {
    const {slide, total} = pptxPreview;
    const image = document.createElement("img");
    image.alt = "Original slide " + slide;
    image.src = "/api/presentations/" + encodeURIComponent(homeState.activePptx) + "/preview/" + slide + ".png";
    const frame = document.createElement("div");
    frame.className = "page";
    frame.append(image);
    home$("#pptx-viewer").replaceChildren(frame);
    home$("#pptx-slide-label").textContent = slide + " / " + total;
    home$("#pptx-prev").disabled = slide === 1;
    home$("#pptx-next").disabled = slide === total;
}
home$("#pptx-prev").onclick = () => { pptxPreview.slide--; pptxSlide(); };
home$("#pptx-next").onclick = () => { pptxPreview.slide++; pptxSlide(); };

async function loadPptxPreview(id, base) {
    const viewer = home$("#pptx-viewer");
    try {
        const info = await workspaceFetch(base + "/preview");
        if (homeState.activePptx !== id) return;
        pptxPreview.total = info.slides;
        home$("#pptx-slide-tools").hidden = false;
        pptxSlide();
    } catch (error) {
        if (homeState.activePptx === id) viewer.textContent = error.message;
    }
}

function showPptxJob(job) {
    if (job.status === "completed") renderPptxResult(job);
    else home$("#pptx-workspace-results").textContent = job.error || job.progress?.message || statusLabel(job.status);
}

async function openPptxWorkspace(job, show = true) {
    homeState.activePptx = job.id;
    pptxPreview.slide = 1;
    const base = "/api/presentations/" + encodeURIComponent(job.id);
    home$("#pptx-workspace-name").textContent = job.filename;
    home$("#pptx-original").href = base + "/source";
    home$("#pptx-slide-tools").hidden = true;
    home$("#pptx-viewer").textContent = "Rendering the original slides?";
    showPptxJob(job);
    if (show) window.showAppScreen("pptx");
    loadPptxPreview(job.id, base);
    try {
        while (["queued", "running"].includes(job.status) && homeState.activePptx === job.id) {
            await pause(700);
            const current = await workspaceFetch(base);
            if (homeState.activePptx !== job.id) return;
            job = current;
            showPptxJob(job);
        }
        loadHistory().catch(() => {});
    } catch (error) {
        if (homeState.activePptx === job.id) home$("#pptx-workspace-results").textContent = error.message;
    }
}

function statusLabel(status) {
    return ({uploaded: "Ready to process", queued: "Queued", running: "Processing", completed: "Completed", failed: "Failed"})[status] || status;
}

function renderHistory(data) {
    const pptxLink = item => item.pptx_download_url ? '<a class="btn" href="' +
        homeEsc(item.pptx_download_url) + '">Download PPTX</a>' : "";
    const items = [];
    for (const item of data.pdfs || []) {
        items.push({created: item.created, html: '<article class="history-item"><div class="history-item-main"><strong>' + homeEsc(item.filename) +
            '</strong><span class="history-item-meta">PDF · Phase ' + homeEsc(item.phase) + ' · ' +
            homeEsc(new Date(item.created).toLocaleString()) + (item.total ? ' · ' + homeEsc(item.done) + '/' + homeEsc(item.total) + ' pages' : '') +
            '</span></div><span class="history-status ' + (item.status === "failed" ? "failed" : "") + '">' +
            homeEsc(statusLabel(item.status)) + '</span><div class="history-item-actions"><button class="btn" data-history-pdf="' +
            homeEsc(item.id) + '">Open PDF review</button>' + (item.status === "completed" ?
            '<button class="btn" data-history-xml="' + homeEsc(item.id) + '">Download XML</button>' : '') +
            pptxLink(item) + '</div></article>'});
    }
    for (const item of data.presentations || []) {
        items.push({created: item.created, html: '<article class="history-item"><div class="history-item-main"><strong>' + homeEsc(item.filename) +
            '</strong><span class="history-item-meta">PowerPoint · Phase ' + homeEsc(item.phase) + ' · ' +
            homeEsc(new Date(item.created).toLocaleString()) + '</span></div><span class="history-status ' +
            (item.status === "failed" ? "failed" : "") + '">' + homeEsc(statusLabel(item.status)) +
            '</span><div class="history-item-actions"><button class="btn" data-history-pptx="' + homeEsc(item.id) + '">View results</button>' +
            pptxLink(item) + '</div></article>'});
    }
    items.sort((first, second) => Date.parse(second.created) - Date.parse(first.created));
    home$("#history-list").innerHTML = items.map(item => item.html).join("") ||
        '<p class="empty">No processing jobs yet. Your PDF and PowerPoint jobs will appear here.</p>';
    home$("#history-list").querySelectorAll("[data-history-pdf]").forEach(button => button.onclick = async () => {
        try { await openPdfWorkspace(button.dataset.historyPdf); }
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
            await openPptxWorkspace(job);
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

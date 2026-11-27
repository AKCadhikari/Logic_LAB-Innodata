const W = {batch: false, documents: [], page: 1, zoom: 100, highlight: null,
           selection: 0, timer: null, previous: "", changes: null, region: null, block: null,
           showBoxes: true, exportPhases: new Map()};
const docUrl = () => "/api/documents/" + encodeURIComponent(S.id);
const workspaceUpdateMessage = "DocuSync is running an earlier backend. Restart the app and reload this page to open the document workspace.";
const statusText = message => $("#runchip").textContent = message;
const engineName = engine => ({"glm-ocr": "GLM-OCR", "paddleocr-vl": "PaddleOCR-VL (Ollama)",
    paddleocr: "PaddleOCR", tesseract: "Tesseract"}[engine] || "");
const ocrModeName = mode => engineName(mode) || (mode === "automatic" ? "Automatic" : "");
const ocrJobOptions = () => ({ocr_engine: $("#retry-engine")?.value || $("#ocr-engine").value,
    ocr_language: $("#ocr-language").value});
let ocrHealth;
let ocrPreferenceChanged = false;
let ocrPreferenceWrite = Promise.resolve();
let ocrProbeInProgress = false;
let layoutHealth;
let layoutTimer;

async function loadLayoutHealth(retry = false) {
    clearTimeout(layoutTimer);
    try {
        layoutHealth = await workspaceFetch("/api/layout-health", retry ? {method: "POST"} : undefined);
        showLayoutHealth();
        if (layoutHealth.status === "loading") layoutTimer = setTimeout(loadLayoutHealth, 850);
    } catch (error) {
        $("#layout-model-status").textContent = error.message;
        $("#layout-check").hidden = false;
        $("#layout-home-status").textContent = "Layout Detection: Unavailable";
        $("#pdf-advanced-status").textContent = "The layout model status could not be checked.";
    }
}

function showLayoutHealth() {
    if (!layoutHealth) return;
    const readiness = {ready: "Ready", loading: "Checking"};
    $("#layout-home-status").textContent = "Layout Detection: " + (readiness[layoutHealth.status] || "Unavailable");
    $("#pdf-advanced-status").textContent = [layoutHealth.model, layoutHealth.device?.toUpperCase(),
        layoutHealth.checkpoint_available ? "model available" : "model missing"].filter(Boolean).join(" · ");
    const step = S.data?.jobs?.[0]?.processing;
    const runtime = step?.stage === "detection" && step.runtime_status;
    const state = runtime || layoutHealth.status;
    const labels = {loading: "Loading", ready: "Ready", processing: "Processing", error: "Error", disabled: "Disabled"};
    $("#layout-model-status").textContent = [labels[state], layoutHealth.device?.toUpperCase(),
        layoutHealth.checkpoint_available ? "Checkpoint found" : "Checkpoint missing",
        layoutHealth.error].filter(Boolean).join(" · ");
    $("#layout-model-status").dataset.state = state;
    $("#layout-check").hidden = state !== "error";
}
$("#layout-check").onclick = () => loadLayoutHealth(true);

function showOcrChoice() {
    const mode = $("#ocr-engine").value, models = ocrHealth?.engines;
    $("#ocr-paddle-row").hidden = mode !== "automatic" && mode !== "paddleocr";
    const description = mode === "automatic" ? "Primary " + ocrModeName(ocrHealth?.automatic_primary || "glm-ocr") :
        (models?.[mode]?.model || ocrModeName(mode));
    const job = S.data?.jobs?.[0], attempt = job?.processing;
    const activeOcr = job?.status === "running" && ["ocr", "ocr_attempt"].includes(attempt?.stage) && attempt.engine;
    if (activeOcr && (mode === "automatic" || mode === attempt.engine)) {
        const state = attempt.stage === "ocr" ? "Checking / loading" : attempt.status === "requesting" ? "Processing" :
            ({loading: "Loading", unavailable: "Unavailable", timeout: "Timed out", rejected: "Error", failed: "Error", accepted: "Recognized"}
                [attempt.status] || attempt.status);
        $("#ocr-choice-status").textContent = engineName(attempt.engine) + " · " + state;
        return;
    }
    if (mode === "automatic" && models) {
        const primary = ocrHealth.automatic_primary;
        const secondary = ocrHealth.automatic_secondary;
        const alternatives = [...new Set([secondary, ocrHealth.automatic_tertiary])]
            .filter(engine => engine && engine !== primary);
        $("#ocr-choice-status").textContent = description + " · " + (models[primary]?.status === "unavailable" ?
            "Primary unavailable · " : "") + "Fallback " + alternatives.map(engine => engineName(engine) +
                (models[engine]?.status === "unavailable" ? " unavailable" : "")).join(", ");
        return;
    }
    const state = models?.[mode]?.status;
    $("#ocr-choice-status").textContent = description + (state === "unavailable" ? " · " + models[mode].reason :
        state === "verified_execution" ? " · Previous inference verified" :
        state === "available_unverified" ? " · Available, inference unverified" : !models ? " · Checking availability" : "");
}

async function loadOcrSettings() {
    try {
        const data = await workspaceFetch("/api/ocr-settings?detail=1");
        ocrHealth = data;
        if (!ocrPreferenceChanged) $("#ocr-engine").value = data.engine;
        for (const option of $("#ocr-engine").options) {
            const model = data.engines[option.value];
            option.textContent = ocrModeName(option.value) + (model?.model ? " · " + model.model : "") +
                (model?.status === "unavailable" ?
                " · Unavailable" : "");
        }
        const language = $("#ocr-language"), current = language.value;
        const labels = new Intl.DisplayNames(["en"], {type: "language"});
        language.replaceChildren(new Option("Detect / configured default", ""));
        for (const code of data.engines.paddleocr.languages) language.add(new Option(labels.of(code), code));
        if ([...language.options].some(option => option.value === current)) language.value = current;
        showOcrChoice();
        const statuses = {unavailable: "Unavailable", available_unverified: "Available · inference unverified",
                          verified_execution: "Previous inference verified"};
        const flags = value => value == null ? "Unobserved" : value ? "Yes" : "No";
        const cards = Object.entries(data.engines).map(([key, model]) => {
            const run = data.measurements[key];
            const when = date => date ? new Date(date).toLocaleString() : "None recorded";
            const languageMetrics = run ? Object.entries(run.languages).map(([code, values]) =>
                '<li>' + esc(labels.of(code) || code) + ' · ' + values.executions + ' runs · ' +
                (values.duration_ms / 1000).toFixed(2) + ' s · ' + values.failures + ' failed</li>').join("") : "";
            return '<div class="card"><strong>' + esc(engineName(key) + ' · ' + statuses[model.status]) + '</strong>' +
                '<div>' + esc(model.model || "") + (model.configured_language ? ' · Default ' + esc(model.configured_language) : '') + '</div>' +
                (Object.keys(model.runtime_versions || {}).length ?
                    '<div>' + esc(Object.entries(model.runtime_versions).map(([name, version]) => name + ' ' + version).join(" · ")) +
                    '</div>' : '') +
                '<div>Installed ' + flags(model.installed) + ' · Model available ' + flags(model.available) +
                ' · Loaded ' + flags(model.loaded) + '</div>' +
                (key !== "paddleocr" ? '<div>Ollama installed ' + flags(model.runtime_installed) +
                ' · Server online ' + flags(model.server_online) + '</div>' : '') +
                '<div>Last successful inference ' + esc(when(model.last_success?.completed_at)) + '</div>' +
                '<div>Last error ' + esc(model.last_error?.message || "None recorded") + '</div>' +
                (model.reason ? '<div class="notice">' + esc(model.reason) + '</div>' : '') +
                '<button type="button" class="btn" data-ocr-probe="' + esc(key) + '"' +
                (ocrProbeInProgress ? ' disabled' : '') + '>Test ' + esc(engineName(key)) + '</button>' +
                (model.probe ? '<div>Image test ' + (model.probe.verified ? "passed" : "failed") + ' · ' +
                    esc(model.probe.attempts.at(-1)?.seconds ?? "—") + ' s</div>' : '') +
                (run?.requests ? '<div>' + run.requests + ' requests · ' + (run.duration_ms / 1000).toFixed(2) +
                    ' s total · ' + (100 * run.failures / run.requests).toFixed(1) + '% failed</div>' : '') +
                (languageMetrics ? '<ul>' + languageMetrics + '</ul>' : '') +
                (run?.worker_rss_bytes ? '<div>Peak observed Paddle worker memory ' +
                Math.round(run.worker_rss_bytes / 1048576) + ' MiB</div>' : '') +
                (model.residency?.size_vram != null ? '<div>Ollama reported GPU memory ' +
                Math.round(model.residency.size_vram / 1048576) + ' MiB</div>' : '') + '</div>';
        }).join("");
        const score = value => value == null ? "—" : (value * 100).toFixed(1) + "%";
        const benchmark = data.benchmark?.benchmarks || [];
        $("#ocr-dashboard-results").innerHTML = cards + (benchmark.length ?
            '<h4>Labelled PDF benchmark</h4><table><thead><tr><th>Engine</th><th>Language</th><th>CER</th>' +
            '<th>WER</th><th>Table</th><th>Formula</th><th>Order</th><th>Time</th></tr></thead><tbody>' + benchmark.map(row =>
                '<tr><td>' + esc(engineName(row.engine)) + '</td><td>' + esc(row.language) + '</td><td>' + score(row.cer) +
                '</td><td>' + score(row.wer) + '</td><td>' + score(row.table_cell_accuracy) +
                '</td><td>' + (row.formula_exact == null ? "—" : row.formula_structured ? "Equation" : row.formula_exact ? "Text" : "Missed") +
                '</td><td>' + score(row.reading_order_accuracy) +
                '</td><td>' + esc(row.ocr_seconds ?? "—") + '</td></tr>').join("") + '</tbody></table>' :
            '<p>Accuracy requires a labelled reference. The workspace records execution measurements for uploaded PDFs.</p>');
        for (const button of document.querySelectorAll("[data-ocr-probe]")) {
            button.onclick = async () => {
                if (ocrProbeInProgress) return;
                ocrProbeInProgress = true;
                document.querySelectorAll("[data-ocr-probe]").forEach(item => { item.disabled = true; });
                button.textContent = "Testing with a real image…";
                try {
                    await workspaceFetch("/api/ocr-test", {method: "POST", headers: {"Content-Type": "application/json"},
                                                          body: JSON.stringify({engine: button.dataset.ocrProbe})});
                } catch (error) {
                    showError(error);
                } finally {
                    ocrProbeInProgress = false;
                    await loadOcrSettings();
                }
            };
        }
    } catch (error) {
        $("#ocr-dashboard-results").textContent = error.message;
        $("#ocr-choice-status").textContent = "Engine availability could not be checked.";
    }
}

$("#ocr-engine").onchange = () => {
    ocrPreferenceChanged = true;
    showOcrChoice();
    if ($("#ocr-remember").checked) {
        const engine = $("#ocr-engine").value;
        ocrPreferenceWrite = ocrPreferenceWrite.catch(() => {}).then(() => workspaceFetch("/api/ocr-settings", {
            method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify({engine})
        })).catch(showError);
    }
};
const processingStatus = step => {
    if (!step) return "";
    if (step.stage === "diagnostics") return "Inspecting PDF pages";
    if (step.stage === "rendering") return "Rendering PDF pages";
    if (step.stage === "detection") return step.page ? "Detecting layout · page " + step.page : "Initializing layout detection";
    if (step.stage === "ocr" || step.stage === "ocr_attempt") return step.engine ?
        engineName(step.engine) + " · page " + step.page : "Extracting document text";
    if (step.stage === "extracted") return "Extracted page " + step.page;
    if (step.stage === "association") return "Associating layout and OCR";
    if (step.stage === "layout") return "Reconstructing document" + (step.page ? " · page " + step.page : "");
    if (step.stage === "validation") return "Validating document and XML";
    if (step.stage === "xml_generation") return "Generating XML";
    if (step.stage === "assets") return "Extracting images and formula crops";
    if (step.stage === "pptx_analysis") return "Analyzing slide " + step.slide + " of " + step.total_slides;
    if (step.stage === "pptx_correction" || step.stage === "pptx") return "Correcting PowerPoint against the PDF";
    if (step.stage === "final_validation") return "Validating the repaired presentation";
    if (step.stage === "zip") return "Preparing the final ZIP";
    return "";
};

function showOcrTimeline(job, active) {
    const pane = $("#ocr-timeline"), step = job?.processing;
    pane.hidden = !job || job.status === "uploaded";
    const seconds = job?.started ? (new Date(job.finished || Date.now()) - new Date(job.started)) / 1000 : null;
    const message = job?.status === "queued" ? "Waiting in queue" : active ?
        processingStatus(step) || "Starting PDF processing" : "Processing " + job?.status;
    if (!pane.firstElementChild) {
        const status = document.createElement("span"), elapsed = document.createElement("span");
        elapsed.setAttribute("aria-hidden", "true");
        pane.replaceChildren(status, elapsed);
    }
    if (pane.firstElementChild.textContent !== message) pane.firstElementChild.textContent = message;
    pane.lastElementChild.textContent = seconds == null ? "" : Math.floor(Math.max(0, seconds)) + " s elapsed";
    const stages = $("#processing-stages");
    const phases = {...step?.phases, ...W.exportPhases.get(S.id)};
    stages.hidden = pane.hidden || !Object.keys(phases).length;
    const names = S.data?.source_pptx ? {pdf_extraction: "PDF extraction", xml_generation: "XML generation",
        assets: "Images and formulas", validation: "XML validation", pptx_analysis: "Slide analysis",
        pptx_correction: "Slide correction", final_validation: "Final validation", zip: "ZIP"} :
        {diagnostics: "PDF check", rendering: "Rendering", detection: "Layout detection",
            ocr: "Text extraction", association: "Region matching", layout: "Reading order", validation: "XML"};
    if (!S.data?.source_pptx && phases.pptx && phases.pptx.status !== "skipped") names.pptx = "PowerPoint";
    stages.innerHTML = Object.entries(names).map(([key, label]) => {
        const phase = phases[key], state = phase?.status || "pending";
        const duration = state === "running" && phase.at ? (Date.now() - new Date(phase.at)) / 1000 : phase?.seconds;
        return '<li data-state="' + esc(state) + '"' + (state === "running" ? ' aria-current="step"' : '') + '><strong>' + label + '</strong><span>' +
            state[0].toUpperCase() + state.slice(1) + (duration == null ? "" : " · " + Math.max(0, duration).toFixed(1) + " s") +
            '</span></li>';
    }).join("");
}
const processingCounts = counts => counts ? '<div class="card" aria-live="polite">' +
    '<div>OCR required on ' + counts.required + ' pages</div>' +
    '<div>GLM-OCR ' + counts.glm_ocr + ' · PaddleOCR-VL ' + (counts.paddle_vl || 0) + ' · PaddleOCR Python ' + counts.paddle_ocr +
    ' · OCR failures ' + counts.failed + '</div>' +
    '<div>Manual review ' + counts.review + '</div></div>' : "";
const analysisStatus = analysis => analysis ? '<p role="status">' + esc(analysis.model) + ' · ' +
    esc(analysis.status) + (analysis.reviewed ? ' · ' + analysis.reviewed + ' assessments' : '') +
    (analysis.reason ? ' · ' + esc(analysis.reason) : '') + '</p>' : "";
const showError = error => {
    statusText(error.message);
    const notice = document.createElement("p");
    notice.className = "notice";
    notice.setAttribute("role", "alert");
    notice.textContent = error.message;
    $("#sbody").prepend(notice);
};

async function workspaceFetch(url, options, asText = false) {
    const response = await fetch(url, options);
    if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        const missingPreviewRoute = response.status === 404 && !body.error &&
            /\/(?:source-info|preview)(?:\/|$)/.test(url);
        const error = new Error(body.error || (missingPreviewRoute ? workspaceUpdateMessage :
            "Request failed (" + response.status + ")."));
        if (body.outputs) error.exportReport = body;
        throw error;
    }
    return asText ? response.text() : response.json();
}

function setActive(button, active) {
    button.classList.toggle("on", active);
    button.setAttribute("aria-pressed", String(active));
}

function hasSavedPdfXml(exportInfo) {
    const xml = exportInfo?.outputs?.xml;
    return ["xml_only", "completed"].includes(exportInfo?.status) && xml?.valid && !xml.draft;
}

async function savedPdfXmlReport(data, base) {
    const reports = await workspaceFetch(base + "/presentations");
    const report = reports.find(item => item.mode === "pdf_processing" &&
        item.job_id === data.jobs[0].id && hasSavedPdfXml(item));
    if (!report) throw new Error("The saved XML is unavailable. Check the phase output folder or submit a review to export again.");
    return report;
}

$("#dlxml").onclick = async () => {
    if (!S.id) return;
    if (W.batch) {
        location.href = "/api/doc/" + encodeURIComponent(S.id) + "/xml?download=1";
        return;
    }
    const data = S.data, base = docUrl();
    if (data.approved) {
        location.href = base + "/xml?download=1";
        return;
    }
    try {
        const report = await savedPdfXmlReport(data, base);
        if (S.data !== data || W.batch) return;
        location.href = base + "/presentations/" + encodeURIComponent(report.id) + "/xml?format=auto";
    } catch (error) {
        showError(error);
    }
};

function setView(tab) {
    S.vtab = tab;
    document.querySelectorAll("#vtabs .tab").forEach(button =>
        setActive(button, button.dataset.t === tab));
    if (W.batch) viewer();
    else {
        workspaceViewer();
        if (tab !== "pdf") $("#viewer").scrollTop = 0;
        updateInspector();
    }
}

function setReviewTab(tab) {
    S.stab = tab;
    document.querySelectorAll("#stabs .tab").forEach(button => setActive(button, button.dataset.t === tab));
    if (W.batch) side();
    else reviewPanel().catch(showError);
}

$("#deck-tab").onclick = () => setReviewTab("deck");

async function loadWorkspace(preferred) {
    if (W.batch) return;
    W.documents = await workspaceFetch("/api/documents");
    if (W.batch) return;
    $("#docsel").disabled = !W.documents.length;
    $("#docsel").innerHTML = W.documents.map(doc =>
        '<option value="' + esc(doc.id) + '">' + esc(doc.filename) + ' · ' +
        doc.id.slice(0, 6) + '</option>').join("") || "<option>No documents yet</option>";
    const id = preferred || W.documents[0]?.id;
    if (id) await selectWorkspace(id);
    else {
        S.id = null;
        S.data = null;
        $("#viewer").innerHTML = '<div class="empty empty-state"><div class="empty-document" aria-hidden="true"></div>' +
            '<h3>Upload a PDF to begin</h3><p>The preview appears here after processing.</p></div>';
        $("#sbody").textContent = "Select a processed document to review its metadata and export XML.";
        $("#sbar").textContent = "";
        $("#page-tools").hidden = true;
        $("#rot").disabled = true;
        $("#dlxml").disabled = true;
        $("#dlpptx").disabled = true;
        showOcrTimeline(null, false);
        $("#reproc").disabled = true;
        statusText("Local workspace");
    }
}

async function selectWorkspace(id, reset = true) {
    const selection = ++W.selection;
    clearTimeout(W.timer);
    if (reset) {
        W.page = 1;
        W.zoom = 100;
        W.highlight = null;
        W.region = W.block = null;
        W.previous = "";
        W.changes = null;
        S.rot = 0;
    }
    const data = await workspaceFetch("/api/documents/" + encodeURIComponent(id));
    const source = W.source?.id === id ? W.source : {id, ...await workspaceFetch(
        "/api/documents/" + encodeURIComponent(id) + "/source-info").catch(error => ({error: error.message}))};
    if (W.batch || W.selection !== selection) return;
    W.source = source;
    S.id = id;
    S.data = data;
    window.updatePairedWorkspace?.(data);
    $("#docsel").value = id;
    const job = data.jobs[0];
    const listed = W.documents.find(document => document.id === id);
    if (listed) listed.status = job.status;
    const active = job.status === "queued" || job.status === "running";
    $("#preview-heading").textContent = data.filename;
    $("#start-pdf").hidden = job.status !== "uploaded" || !!source.error;
    $("#start-pdf").disabled = active;
    if (active) W.exportPhases.delete(id);
    showOcrTimeline(job, active);
    showLayoutHealth();
    showOcrChoice();
    const reviewPages = data.pages?.filter(page => page.requires_review).length || 0;
    const extractionStatus = data.content_validation?.status === "manual_review_required" ?
        "Extraction complete · manual review required" : data.content_validation?.status === "validation_partial" ?
            "Extraction complete · validation partial" : "Extraction complete";
    const waiting = {queued: "Queued", running: "Processing the PDF"};
    statusText((active && waiting[job.status] || (job.status === "completed" ? extractionStatus :
        job.status === "uploaded" ? source.error || "Ready to process" : job.error || job.status)) +
        (job.total ? " · " + job.done + "/" + job.total + " pages" : "") +
        (reviewPages && !active ? " · " + reviewPages + " for review" : ""));
    $("#runchip").dataset.documentId = id;
    $("#runchip").dataset.jobStatus = job.status;
    $("#reproc").disabled = active;
    $("#reproc").hidden = job.status === "uploaded";
    $("#reproc").textContent = job.status === "failed" ? "Retry" : "Reprocess";
    const canDownloadXml = !data.source_pptx && (data.approved || (job.status === "completed" && hasSavedPdfXml(data.submission_export)));
    $("#dlxml").disabled = !canDownloadXml;
    $("#dlpptx").disabled = active || !data.approved || !!data.content_validation?.blocking_pages?.length;
    $("#dlpptx").hidden = !!data.source_pptx || !data.approved;
    $("#deck-tab").hidden = !!data.source_pptx || !data.pages;
    $("#dlxml").hidden = !canDownloadXml;
    $("#dlxml").title = canDownloadXml ? "Downloads a ZIP when the XML has images" : "Approve metadata review before exporting";
    workspaceViewer();
    await reviewPanel();
    if (active && !W.batch && W.selection === selection) {
        W.timer = setTimeout(() => selectWorkspace(id, false).catch(showError), 800);
    }
}

async function startPdfProcessing(options = {}) {
    const id = S.id;
    await workspaceFetch(docUrl() + "/retry", {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify(options)});
    if (!W.batch && S.id === id) await selectWorkspace(id, false);
}

$("#start-pdf").onclick = async event => {
    const button = event.currentTarget;
    button.disabled = true;
    try { await startPdfProcessing(); }
    catch (error) { showError(error); button.disabled = false; }
};

const bidiDir = language => language?.base_direction === "rtl" ? "rtl" : language?.base_direction === "ltr" ? "ltr" : "auto";
const regionColors = {text: "#087568", title: "#6834ac", header: "#3659b2", footer: "#525e71",
    table: "#a54b14", image: "#af2e65", equation: "#8f371b", list: "#4a6d21", other: "#346983"};
const regionCategory = label => ["image_block", "equation_block"].includes(label) ? label.split("_")[0] :
    Object.hasOwn(regionColors, label) ? label : "other";
const regionName = label => label.replaceAll("_", " ");
const boxArea = ([left, top, right, bottom]) => (right - left) * (bottom - top);
const currentPage = () => S.data?.pages?.[W.page - 1];
const layoutRegions = page => (page.layout_regions || []).filter(region => region.source === "DocLayout-YOLO");
const regionLabel = region => regionName(region.class_name) + (Number.isFinite(region.confidence) ?
    " · " + (region.confidence * 100).toFixed(1) + "%" : "");
const blockOrder = block => block.reading_order || block.i + 1;
const orderedBlocks = page => [...page.blocks].sort((a, b) => blockOrder(a) - blockOrder(b));

function svgRectangle(box, attributes, label) {
    const [x, y, right, bottom] = box;
    return '<rect x="' + x + '" y="' + y + '" width="' + (right - x) + '" height="' + (bottom - y) +
        '" ' + attributes + '><title>' + esc(label) + '</title></rect>';
}

function zoningOverlay(regions, selectedId) {
    return [...regions].sort((a, b) => boxArea(b.display_bbox) - boxArea(a.display_bbox)).map(region => {
        const color = regionColors[regionCategory(region.class_name)];
        const selected = region.id === selectedId;
        const label = regionLabel(region);
        const interactive = selectedId === undefined ? "" : ' data-region="' + esc(region.id) +
            '" role="button" tabindex="0" aria-label="' + esc(label) + '" aria-pressed="' + selected + '"';
        return svgRectangle(region.display_bbox, 'class="layout-box' + (selected ? ' selected' : '') +
            '" fill="' + color + '" stroke="' + color + '"' + interactive, label);
    }).join("");
}

function inspectRegion(id) {
    W.region = id;
    W.block = null;
    W.highlight = currentPage()?.layout_regions.find(region => region.id === id)?.display_bbox || null;
    if (S.stab !== "meta") $("#stabs [data-t=meta]").click();
    workspaceViewer();
    updateInspector();
}

function inspectBlock(index) {
    W.block = index;
    W.region = null;
    W.highlight = currentPage()?.blocks.find(block => block.i === index)?.bbox || null;
    if (S.stab !== "meta") $("#stabs [data-t=meta]").click();
    workspaceViewer();
    updateInspector();
}

function updateInspector() {
    const panel = $("#region-inspector"), page = currentPage();
    if (!panel || !page) return;
    const regions = layoutRegions(page);
    const region = regions.find(item => item.id === W.region);
    const block = page.blocks.find(item => item.i === W.block);
    let detail = "";
    if (region || block) {
        const box = (region || block).bbox;
        const heading = region ? regionName(region.class_name) : block.kind || block.tag;
        const confidence = region && Number.isFinite(region.confidence) ? region.confidence * 100 : block?.conf;
        const engines = region ? region.ocr_engines : [block.ocr_engine].filter(Boolean);
        const text = region ? region.ocr_text : block.ocr_engine ? block.text : "";
        const source = region?.source || block?.source_reference?.layout_detection ||
            (block?.source_reference?.layout_fragments ? "DocLayout-YOLO" :
                page.diag.layout?.docling_status === "applied" ? "Docling" : "Reconstruction");
        detail = '<article class="card region-detail"><strong>' + esc(heading) + '</strong><dl>' +
            '<dt>Confidence</dt><dd>' + (confidence == null ? "Unavailable" : confidence.toFixed(1) + "%") + '</dd>' +
            '<dt>Page</dt><dd>' + page.n + '</dd><dt>PDF box (top-left)</dt><dd>' +
            (box ? box.map(value => value.toFixed(2)).join(", ") + ' pt' : "Unavailable") + '</dd>' +
            '<dt>Layout source</dt><dd>' + esc(source || "Reconstruction") + '</dd><dt>OCR engine</dt><dd>' +
            esc(engines.map(engine => engineName(engine) || engine).join(", ") || "No OCR linked") + '</dd></dl>' +
            (text ? '<div><b>OCR text</b><p dir="auto">' + esc(text) + '</p></div>' :
                region?.text || block?.text ? '<div><b>Extracted text</b><p dir="auto">' +
                    esc(region?.text || block?.text) + '</p></div>' : "") +
            (W.highlight ? '<button class="btn" id="locate-element">Locate in source PDF</button>' : "") + '</article>';
    }
    const items = S.vtab === "zoning" ? regions.map(item => ({
        label: regionLabel(item),
        value: item.id, current: item.id === W.region, attribute: "region"
    })) : S.vtab === "order" ? orderedBlocks(page)
        .map(item => ({label: blockOrder(item) + ". " + (item.kind || item.tag) + " · " +
            (item.text || item.caption || "").slice(0, 85), value: item.i, current: item.i === W.block, attribute: "block"})) : [];
    panel.innerHTML = detail + (items.length ? '<div class="sec">' + (S.vtab === "zoning" ? "Detected regions" : "Reading order") +
        '</div><div class="region-list">' + items.map(item => '<button class="btn" data-' + item.attribute +
            '="' + esc(item.value) + '" aria-pressed="' + item.current + '" dir="auto">' + esc(item.label) + '</button>').join("") +
        '</div>' : "");
    panel.querySelectorAll("[data-region]").forEach(button => button.onclick = () => inspectRegion(button.dataset.region));
    panel.querySelectorAll("[data-block]").forEach(button => button.onclick = () => inspectBlock(Number(button.dataset.block)));
    const locate = $("#locate-element");
    if (locate) locate.onclick = () => { S.rot = 0; setView("pdf"); };
}

function languageLabel(profile) {
    if (!profile?.language) return "";
    const language = profile.languages?.length > 1 ? profile.languages.join(", ") : profile.language;
    return [language === "und" ? "Undetermined language" : language, profile.script,
        profile.direction, profile.confidence == null ? "" :
            Math.round(profile.confidence * 100) + "% detection confidence"].filter(Boolean).join(" · ");
}

function renderTextBlocks(blocks, numbered = false) {
    return '<div class="textview">' + blocks.map(block => {
        let details = '<div dir="' + bidiDir(block.language) + '">' + esc(block.text || block.caption || block.alt) + '</div>';
        if (block.cells.length) {
            const rows = new Map();
            const rtl = bidiDir(block.language) === "rtl";
            const cells = rtl && block.source === "native" ? [...block.cells].sort((a, b) => a.row - b.row || b.col - a.col) : block.cells;
            for (const cell of cells) {
                if (!rows.has(cell.row)) rows.set(cell.row, []);
                const tag = cell.header ? "th" : "td";
                rows.get(cell.row).push('<' + tag + ' dir="' + bidiDir(cell.language) + '" rowspan="' + cell.rowspan + '" colspan="' + cell.colspan + '">' +
                    esc(cell.text) + '</' + tag + '>');
            }
            details += '<table dir="' + bidiDir(block.language) + '">' + [...rows.values()].map(row => "<tr>" + row.join("") + "</tr>").join("") + "</table>";
        }
        return '<article class="tb"><small>' + (numbered ? blockOrder(block) + ". " : "") +
            esc([block.kind || block.tag, languageLabel(block.language)].filter(Boolean).join(" · ")) +
            '</small>' + details + '</article>';
    }).join("") + '</div>';
}

function inspectionMessage(doc) {
    const job = doc.jobs[0];
    return job.error || (job.status === "uploaded" ? W.source?.error || "Start processing to see inspection results." :
        job.status === "completed" ? "No extracted content is available for this page." :
        "Processing the PDF. Inspection results will appear here when ready.");
}

function updatePageControls(total) {
    const select = $("#page-label");
    if (select.options.length !== total) {
        select.replaceChildren(...Array.from({length: total}, (_, index) =>
            new Option((index + 1) + " / " + total, index + 1)));
    }
    select.value = W.page;
    $("#page-prev").disabled = W.page <= 1;
    $("#page-next").disabled = W.page >= total;
    $("#zoom-out").disabled = W.zoom <= 50 || S.vtab === "text";
    $("#zoom-in").disabled = W.zoom >= 200 || S.vtab === "text";
    $("#fit-width").disabled = S.vtab === "text";
}

function updatePageInspector() {
    if (S.stab !== "meta") return;
    const diagnostics = $("#page-diagnostics"), page = currentPage();
    if (diagnostics && page) diagnostics.innerHTML = pageDiagnostics(page);
    const source = $("#source-fields");
    if (source) source.innerHTML = sourceFields();
    updateInspector();
}

function renderSourcePages(viewer, pages) {
    const key = JSON.stringify([S.id, S.rot, W.zoom, W.highlight]);
    if (viewer.dataset.sourceStack === key) return;
    viewer.dataset.sourceStack = key;
    const scale = W.zoom / 100, dpi = Math.min(220, Math.round(100 * scale));
    viewer.innerHTML = '<div class="pl"><span>' + W.zoom + '%</span>' +
        '<a class="workspace-link" target="_blank" rel="noopener" href="' + docUrl() + '/pdf">Open original PDF</a></div>' +
        pages.map(page => {
            const swapped = S.rot % 180 !== 0;
            const aspect = (swapped ? page.h : page.w) + '/' + (swapped ? page.w : page.h);
            const overlay = W.highlight && !S.rot && W.page === page.n ?
                '<svg viewBox="0 0 ' + page.w + ' ' + page.h + '">' + svgRectangle(W.highlight, 'class="highlight"', "Selected source region") + '</svg>' : '';
            return '<div class="page source-page" data-source-page="' + page.n + '" style="width:' + W.zoom + '%;aspect-ratio:' + aspect +
                '"><img ' + (page.n === W.page ? '' : 'loading="lazy" ') + 'src="' + docUrl() + '/page/' + page.n +
                '.png?dpi=' + dpi + '&amp;rot=' + S.rot + '" alt="Source PDF page ' + page.n + '">' + overlay + '</div>';
        }).join("");
    const frames = Array.from(viewer.querySelectorAll("[data-source-page]"));
    scrollSourcePage(viewer, W.page);
    viewer.onscroll = () => {
        const midpoint = viewer.getBoundingClientRect().top + Math.min(viewer.clientHeight * .28, 130);
        const frame = frames.find(item => item.getBoundingClientRect().bottom >= midpoint);
        const number = Number(frame?.dataset.sourcePage || pages.length);
        if (number === W.page) return;
        W.page = number;
        W.region = W.block = null;
        updatePageControls(pages.length);
        updatePageInspector();
    };
}

function scrollSourcePage(viewer, number) {
    const frame = viewer.querySelector('[data-source-page="' + number + '"]');
    if (frame) viewer.scrollTop = number === 1 ? 0 : viewer.scrollTop + frame.getBoundingClientRect().top - viewer.getBoundingClientRect().top - 8;
}

function workspaceViewer() {
    const doc = S.data;
    if (!doc) return;
    const page = doc.pages?.[W.page - 1];
    const sourcePages = W.source?.pages || doc.pages || [];
    const viewer = $("#viewer");
    $("#page-tools").hidden = !sourcePages.length;
    $("#rot").hidden = S.vtab !== "pdf";
    $("#rot").disabled = !sourcePages.length || S.vtab !== "pdf";
    $("#legend").hidden = true;
    updatePageControls(sourcePages.length);
    if (S.vtab === "pdf" && sourcePages.length) {
        renderSourcePages(viewer, sourcePages);
        return;
    }
    delete viewer.dataset.sourceStack;
    viewer.onscroll = null;
    if (!page) {
        viewer.innerHTML = '<div class="empty">' + esc(S.vtab === "pdf" && W.source?.error || inspectionMessage(doc)) + '</div>';
        return;
    }
    if (S.vtab === "text") {
        $("#viewer").innerHTML = renderTextBlocks(page.blocks);
        return;
    }
    let overlay = "";
    if (S.vtab === "zoning") {
        const regions = layoutRegions(page);
        const legend = $("#legend");
        legend.hidden = false;
        legend.innerHTML = '<label><input type="checkbox" id="show-boxes"' + (W.showBoxes ? " checked" : "") +
            '> Bounding boxes</label><span>' + esc(regions.length ? regions.length + " DocLayout-YOLO regions" :
                page.diag.layout?.error || "No DocLayout-YOLO regions on this page.") + '</span>' +
            [...new Set(regions.map(region => regionCategory(region.class_name)))].map(category =>
                '<span><b style="background:' + regionColors[category] + '"></b>' + category + '</span>').join("");
        $("#show-boxes").onchange = event => { W.showBoxes = event.target.checked; workspaceViewer(); $("#show-boxes").focus(); };
        if (W.showBoxes) overlay = zoningOverlay(regions, W.region);
    }
    if (S.vtab === "order") {
        const positioned = page.blocks.filter(block => block.bbox);
        const legend = $("#legend");
        legend.hidden = false;
        legend.textContent = positioned.length ?
            positioned.length + " of " + page.blocks.length + " blocks located on the page" :
            "No verified layout coordinates are available for this page.";
        if (page.source === "ocr" && (positioned.length < page.blocks.length || !page.diag.layout?.coordinate_alignment)
            && !["queued", "running"].includes(doc.jobs[0].status)) {
            const refresh = document.createElement("button");
            refresh.className = "btn";
            refresh.textContent = "Refresh layout";
            refresh.onclick = async () => {
                refresh.disabled = true;
                try {
                    await workspaceFetch(docUrl() + "/layout", {method: "POST"});
                    await selectWorkspace(doc.id, false);
                } catch (error) {
                    showError(error);
                    refresh.disabled = false;
                }
            };
            legend.append(" ", refresh);
        }
        overlay = positioned.map(block => {
            const [x, y] = block.bbox;
            const color = col(block.tag);
            return '<g class="order-marker' + (block.i === W.block ? ' selected' : '') + '" data-block="' + block.i +
                '" tabindex="0" role="button" aria-label="Block ' + (block.reading_order || block.i + 1) + '"><title>' +
                esc((block.reading_order || block.i + 1) +
                " · " + (block.text || block.kind || block.tag).slice(0, 120)) + '</title>' +
                '<circle cx="' + Math.max(13, x - 12) + '" cy="' + (y + 8) + '" r="10" fill="' + color +
                '"/><text x="' + Math.max(13, x - 12) + '" y="' + (y + 12) +
                '" text-anchor="middle" font-size="11" fill="white">' + (block.reading_order || block.i + 1) + '</text></g>';
        }).join("");
    }
    const dpi = Math.min(220, Math.round(100 * W.zoom / 100));
    $("#viewer").innerHTML = '<div class="pl"><span>' + esc(page.source === "ocr" ?
        engineName(page.ocr_engine) || "OCR" : "Native PDF") + ' · ' + W.zoom + '%</span>' +
        '<a class="workspace-link" target="_blank" rel="noopener" href="' + docUrl() + '/pdf#page=' + page.n +
        '">Open original</a></div><div class="page inspection-page" style="width:' + W.zoom + '%">' +
        '<img src="' + docUrl() + '/page/' + page.n + '.png?dpi=' + dpi + '" alt="Page ' + page.n + '">' +
        (overlay ? '<svg viewBox="0 0 ' + page.w + ' ' + page.h + '" preserveAspectRatio="none">' + overlay + '</svg>' : '') + '</div>' +
        (S.vtab === "order" ? '<section aria-label="Reconstructed reading sequence">' +
            '<div class="pl">Page ' + page.n + ' · Reconstructed reading order</div>' +
            (page.blocks.length ? renderTextBlocks(orderedBlocks(page), true) : '<p class="empty">No extracted blocks on this page.</p>') + '</section>' : '');
    $("#viewer").querySelectorAll("[data-region], [data-block]").forEach(control => {
        const activate = () => {
            const region = control.dataset.region, block = control.dataset.block;
            if (region) inspectRegion(region); else inspectBlock(Number(block));
            $("#viewer").querySelector(region ? '[data-region="' + CSS.escape(region) + '"]' : '[data-block="' + block + '"]')?.focus();
        };
        control.onclick = activate;
        control.onkeydown = event => {
            if (event.key === "Enter" || event.key === " ") { event.preventDefault(); activate(); }
        };
    });
}

function movePage(step) {
    const total = W.source?.pages?.length || S.data?.pages?.length || 0;
    W.page = Math.min(total, Math.max(1, W.page + step));
    W.highlight = null;
    W.region = W.block = null;
    workspaceViewer();
    if (S.vtab === "pdf") scrollSourcePage($("#viewer"), W.page);
    else $("#viewer").scrollTop = 0;
    updatePageInspector();
}

function pageDiagnostics(page) {
    const diag = page.diag, ocr = diag.ocr;
    const confidence = diag.confidence == null ? "Confidence unavailable" : badge(diag.confidence * 100);
    const selected = page.source === "ocr" ? engineName(page.ocr_engine || ocr?.engine) || "OCR" : "Native PDF";
    const attempts = (ocr?.attempts || []).map(attempt => '<div>' + esc(attempt.model || engineName(attempt.engine)) +
        ' · ' + esc(attempt.status) + ' · ' + esc(attempt.seconds) + ' s' +
        (attempt.fallback_reason ? ' · ' + esc(attempt.fallback_reason) : '') +
        (attempt.issues?.length ? ' · ' + esc(attempt.issues.join(" ")) : '') + '</div>').join("");
    return '<div class="sec">Page ' + page.n + ' diagnostics</div><div class="card">' +
        '<div>' + esc(selected) + ' · ' + confidence + '</div><div>' + esc(languageLabel(page.language)) + '</div>' +
        '<div>Native text: ' + esc(diag.health || "unknown") +
        (Number.isInteger(diag.native_chars) ? ' · ' + diag.native_chars + ' characters' : '') + '</div>' +
        '<div>' + (diag.ocr_required ? "OCR required" : "OCR not required") +
        (diag.appears_scanned ? ' · scanned page' : '') + '</div>' + attempts +
        (diag.layout ? '<div>Layout · ' + esc(diag.layout.engine) + ' · ' + esc(diag.layout.status) + '</div>' : '') +
        (diag.layout?.error ? '<div class="notice">' + esc(diag.layout.error) + '</div>' : '') +
        (diag.layout?.docling_status ? '<div>Docling · ' + esc(diag.layout.docling_status) + '</div>' : '') +
        (page.requires_review ? '<strong class="notice">Manual review required</strong>' : '') +
        '<div>' + esc([...(diag.reasons || []), ...(diag.review_reasons || [])].join(" ")) + '</div></div>';
}

function sourceFields() {
    const page = W.source?.pages?.[W.page - 1];
    const facts = [["Pages", W.source?.pages?.length], ["Current page", page?.n],
        ["Page size", page && page.w.toFixed(1) + " × " + page.h.toFixed(1) + " pt"]];
    const fields = Object.entries(W.source?.metadata || {}).map(([key, value]) => [key[0].toUpperCase() + key.slice(1), value]);
    const rows = values => '<dl>' + values.filter(([, value]) => value).map(([key, value]) =>
        '<dt>' + esc(key) + '</dt><dd>' + esc(value) + '</dd>').join("") + '</dl>';
    return (page ? '<div class="sec">Document and page</div><div class="card region-detail">' + rows(facts) + '</div>' : '') +
        (fields.length ? '<div class="sec">Embedded PDF metadata</div><div class="card region-detail">' + rows(fields) + '</div>' : '');
}

async function reviewPanel() {
    const data = S.data, panel = $("#sbody"), bar = $("#sbar");
    if (!data) return;
    const job = data.jobs[0];
    bar.textContent = data.pages ? (data.approved ? "Review approved" : "Review pending") : "";
    if (data.approved && data.review) bar.title = "Reviewed by " + data.review.reviewer;
    else bar.removeAttribute("title");
    $("#dlxml").style.display = S.stab === "deck" ? "none" : "";
    if (S.stab === "xml") {
        if (!data.pages) { panel.textContent = inspectionMessage(data); return; }
        const saved = data.submission_export, base = docUrl();
        panel.textContent = "Loading XML…";
        try {
            let url = base + "/xml";
            if (data.source_pptx && job.final_export?.xml_ready) {
                url = base + "/presentations/" + encodeURIComponent(job.final_export.id) + "/xml?preview=1";
            } else if (hasSavedPdfXml(saved) && !data.approved) {
                const report = await savedPdfXmlReport(data, base);
                url = base + "/presentations/" + encodeURIComponent(report.id) + "/xml?preview=1";
            }
            const xml = await workspaceFetch(url, undefined, true);
            if (S.data !== data || S.stab !== "xml" || W.batch) return;
            const preview = document.createElement("pre");
            preview.className = "xml";
            preview.innerHTML = xml.split(/(<!--[\s\S]*?-->|<!\[CDATA\[[\s\S]*?\]\]>|<[^>]+>)/g).map((token, index) =>
                index % 2 ? '<span class="xml-tag">' + esc(token) + '</span>' : esc(token)).join("");
            panel.replaceChildren(preview);
            if (!hasSavedPdfXml(saved) && !data.approved) panel.prepend(Object.assign(document.createElement("p"), {
                className: "notice", textContent: saved?.error || "XML preview. Submit the source review to publish an export."
            }));
        } catch (error) {
            if (S.data === data && S.stab === "xml" && !W.batch) panel.textContent = error.message;
        }
        return;
    }
    if (S.stab === "deck" && data.pages) {
        changesPanel(data);
        return;
    }
    const history = '<div class="sec">Processing history</div>' + data.jobs.map(attempt =>
        '<div class="card"><div>' + esc(attempt.status) + ' · ' + esc(new Date(attempt.created).toLocaleString()) +
        '</div>' + (attempt.error ? '<div class="notice">' + esc(attempt.error) + '</div>' : '') + '</div>').join("");
    const counts = processingCounts(job.processing?.counts || data.processing_counts);
    if (!data.pages) {
        panel.innerHTML = '<p role="status">' + esc(inspectionMessage(data)) + '</p>' +
            '<section id="source-fields">' + sourceFields() + '</section>' + counts;
        return;
    }
    const page = data.pages[W.page - 1], title = data.title;
    const detectedPages = data.pages.filter(item => item.diag.layout?.status === "completed").length;
    const failedLayoutPages = data.pages.filter(item => item.diag.layout?.status === "failed").length;
    const layoutStatus = detectedPages === data.pages.length ? "Completed" :
        detectedPages ? "Completed on " + detectedPages + " of " + data.pages.length + " pages" :
            failedLayoutPages ? "Failed on " + failedLayoutPages + " pages" : "Unavailable";
    const layoutCount = data.pages.reduce((total, item) => total + (item.layout_regions?.length || 0), 0);
    const layoutSeconds = data.jobs?.[0]?.processing?.phases?.detection?.seconds;
    const selectedOcr = [...new Set(data.pages.map(item => item.ocr_engine).filter(Boolean))];
    const ocrLabel = selectedOcr.length ? selectedOcr.map(engineName).join(", ") : "Native PDF text";
    const savedXml = data.submission_export?.outputs?.xml;
    const advanced = '<div class="sec">Processing details</div><div class="card"><div>Phase ' + esc(data.phase) +
        ' | ' + esc(data.pages.length) + ' pages | OCR ' + esc(ocrLabel) + '</div>' +
        '<div>DocLayout-YOLO | ' + esc(layoutStatus) + ' | ' + layoutCount + ' detections' +
        (Number.isFinite(layoutSeconds) ? ' | ' + esc(layoutSeconds) + ' s inference' : '') + '</div>' +
        '<div>Processing time | ' + esc(data.seconds) + ' s</div>' +
        (savedXml ? '<div>XML saved to ' + esc(data.submission_export.output_folder) + '</div>' :
            data.submission_export?.error ? '<div class="notice">' + esc(data.submission_export.error) + '</div>' : '') + '</div>' +
        counts + '<div id="page-diagnostics">' + pageDiagnostics(page) + '</div>' +
        '<div class="sec">Validation</div><div class="card">XML ' +
        (data.validation.issues.length ? esc(data.validation.issues.join(", ")) : "passed structural checks") +
        '<div>' + esc(languageLabel(data.language)) + '</div>' +
        (data.content_validation?.status === "manual_review_required" ?
            '<strong class="notice">Manual review required before PowerPoint export</strong>' :
            data.content_validation?.status === "validation_partial" ?
                '<strong class="notice">Validation partially passed · content review required</strong>' : '') + '</div>' +
        data.warnings.map(warning => '<div class="notice">' + esc(warning) + '</div>').join("") + history;
    const review = job.status === "review" ? job.final_export?.source_review : null;
    const blocking = review?.blocking_pages?.length || review?.issues?.length;
    const affected = review ? [...new Set([...review.blocking_pages, ...[...review.items, ...review.issues].map(row => row.page)].filter(Boolean))] : [];
    const reviewNotice = !review ? "" : '<div class="card"><strong>Source review required</strong><p>' +
        (blocking ? "Recover missing or inconsistent content by reprocessing the saved files." :
            "Check the affected pages against the original PDF. Submit review to resume the same job.") + '</p>' +
        (blocking ? '<label for="retry-engine">OCR for reprocessing</label> ' +
            '<select id="retry-engine"><option value="automatic">Automatic</option><option value="glm-ocr">GLM-OCR</option>' +
            '<option value="paddleocr">PaddleOCR</option></select>' : '') +
        affected.map(number => '<button type="button" class="btn" data-review-page="' + number + '">Page ' + number + '</button>').join(" ") +
        [...review.items, ...review.issues].slice(0, 12).map(row => '<div>' + esc(row.reason || row.kind || "Check source content") + '</div>').join("") + '</div>';
    const warnings = job.final_export?.source_warnings || [];
    const warningList = warnings.length ? '<details class="advanced-details"><summary>Source warnings (' + warnings.length +
        ')</summary>' + warnings.map(item => '<div>Page ' + esc(item.page) + ' · ' +
        esc(item.kind.replaceAll("_", " ")) + '</div>').join("") + '</details>' : '';
    panel.innerHTML = reviewNotice + warningList + '<section id="region-inspector" aria-label="Element inspector"></section>' +
        '<section id="source-fields">' + sourceFields() + '</section><div class="sec">Extracted metadata</div>' +
        '<form id="metadata-form" class="workspace-tools">' +
        '<div class="fld"><label for="review-title">Title</label>' +
        '<input id="review-title" maxlength="1000" value="' + esc(title?.value) + '">' +
        (Number.isFinite(title?.confidence) ? '<small>Extraction confidence ' + badge(title.confidence) + '</small>' : '') +
        (title?.bbox ? '<button type="button" class="btn" id="locate">Locate title source</button>' : '') + '</div>' +
        '<div class="fld"><label for="review-lang">Document language</label>' +
        '<input id="review-lang" required maxlength="35" value="' + esc(data.lang) + '"></div>' +
        '<div><button class="btn" value="save">' + (data.approved ? "Re-edit" : "Save draft") +
        '</button> <button class="btn pri" value="approve">Submit review</button></div></form>' +
        '<details class="advanced-details"><summary>Advanced metadata and diagnostics</summary>' +
        advanced + '</details>';
    updateInspector();
    panel.querySelectorAll("[data-review-page]").forEach(button => button.onclick = () => {
        setView("pdf");
        movePage(Number(button.dataset.reviewPage) - W.page);
    });
    const locate = $("#locate");
    if (locate) locate.onclick = () => {
        W.page = title.page;
        W.highlight = title.bbox;
        W.region = W.block = null;
        S.rot = 0;
        setView("pdf");
        $("#page-diagnostics").innerHTML = pageDiagnostics(data.pages[W.page - 1]);
    };
    const form = $("#metadata-form");
    const editable = data.source_pptx ? !!review && !blocking : job.status === "completed";
    form.querySelectorAll("input,button").forEach(control => control.disabled = !editable);
    if (data.source_pptx && !editable) form.querySelectorAll('button[value]').forEach(control => control.hidden = true);
    form.onsubmit = async event => {
        event.preventDefault();
        const payload = {title: $("#review-title").value, lang: $("#review-lang").value,
                         approved: event.submitter?.value === "approve"};
        try {
            await workspaceFetch(docUrl() + "/metadata", {
                method: "PATCH", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)
            });
            if (S.data === data) await selectWorkspace(data.id, false);
        } catch (error) { showError(error); }
    };
}

function exportCard(report, links) {
    const statuses = {
        completed: report.content_validation?.status === "validation_partial" ||
            report.content_validation?.presentation?.status === "manual_review_required" ?
            "XML and PowerPoint generated · content review required" : "XML and PowerPoint generated",
        manual_review_required: "Draft XML · manual review required",
        partial: report.outputs.pptx ? "XML and PowerPoint available · ZIP failed" : "XML available · PowerPoint failed",
        failed: "Export failed",
        xml_only: "XML-only export",
    };
    return '<article class="card export-card"><strong>' + esc(statuses[report.status] || report.status) + '</strong>' +
        '<div class="export-location">' + esc(report.output_folder || "") + '</div>' +
        [["xml", "XML"], ["pptx", "PowerPoint"]].map(([kind, label]) => {
            const file = report.outputs[kind];
            if (!file) return '<div>' + label + ' · unavailable</div>';
            return '<div class="export-location">' + esc(file.filename) + ' · ' + (file.valid ? "Structure validated" : "Invalid") +
                ' · ' + file.size_bytes.toLocaleString() + ' bytes</div>' +
                '<a class="workspace-link" href="' + links[kind] + '">Download ' + label + '</a>';
        }).join("") +
        (report.status === "completed" && links.zip ? '<div><a class="workspace-link" href="' +
            links.zip + '">Download both as ZIP</a></div>' : '') +
        (report.error ? '<div class="notice">' + esc(report.error) + '</div>' : '') + '</article>';
}

function presentationLinks(base, id) {
    const url = base + "/" + encodeURIComponent(id);
    return {xml: url + "/xml?format=auto", pptx: url, zip: url + "/zip"};
}

function changesPanel(data) {
    const alternatives = W.documents.filter(doc => doc.id !== data.id && doc.status === "completed");
    W.previous ||= data.previous_id || alternatives[0]?.id || "";
    $("#sbar").textContent = "Document changes";
    $("#sbody").innerHTML = '<div class="workspace-tools"><label for="previous">Previous version</label>' +
        '<select id="previous"><option value="">Select a processed PDF</option>' +
        alternatives.map(doc => '<option value="' + esc(doc.id) + '">' + esc(doc.filename) + ' · ' +
                                 doc.id.slice(0, 6) + '</option>').join("") + '</select>' +
        '<button id="compare" class="btn pri">Compare versions</button>' +
        '<form id="deck-upload" class="workspace-tools">' + (data.source_pptx ? '' : '<label for="deck-file">Related PowerPoint</label>' +
        '<input id="deck-file" type="file" accept=".pptx" required>' +
        '<button class="btn" value="source">Repair and export XML + PowerPoint</button>') +
        '<button class="btn" value="version">Apply version changes and export</button></form></div>' +
        '<div id="comparison-results"></div><div id="presentation-history"></div>';
    $("#previous").value = W.previous;
    const changeList = changes => changes.length ? changes.map(change =>
        '<article class="card"><div class="h">' + esc(change.category) + ' · ' + esc(change.change_type) +
        ' · ' + (change.original_location?.page || "—") + ' → ' + (change.new_location?.page || "—") +
        ' · ' + badge(change.confidence * 100) + '</div><div>' + esc(change.old_content) + '</div><div>' +
        esc(change.new_content) + '</div>' + (change.numerical_change ? '<div>Values · ' +
        esc(change.numerical_change.before.join(", ") || "—") + ' → ' +
        esc(change.numerical_change.after.join(", ") || "—") + '</div>' : '') +
        (change.assistance ? '<div>Qwen review · ' + esc(change.assistance.classification) +
        ' · ' + esc(change.assistance.relation) + (change.assistance.candidate ? ' · candidate on page ' +
        esc(change.assistance.candidate.page) : '') + '</div><div>' + esc(change.assistance.explanation) + '</div>' : '') +
        (change.reason ? '<div>' + esc(change.reason) + '</div>' : '') +
        '</article>').join("") : '<p>No changes detected.</p>';
    if (W.changes) $("#comparison-results").innerHTML =
        analysisStatus(W.changes.analysis) + changeList(W.changes.changes);
    const renderExports = async (displayedId = null) => {
        const url = docUrl() + "/presentations";
        const allReports = await workspaceFetch(url);
        const finalId = data.jobs[0].final_export?.id;
        const reports = allReports.filter(report => report.id !== displayedId && report.id !== finalId);
        const history = $("#presentation-history");
        if (!history || S.data !== data || S.stab !== "deck" || W.batch) return;
        const latest = reports.find(report => report.job_id === data.jobs[0].id && report.mode !== "unified");
        if (latest?.export_phases) {
            W.exportPhases.set(data.id, latest.export_phases);
            showOcrTimeline(data.jobs[0], false);
        }
        history.innerHTML = (reports.length ? '<div class="sec">Export history</div>' : '') + reports.map(report => report.mode === "unified" ?
            '<div class="card">' + esc(new Date(report.created).toLocaleString()) + ' · ' + esc(report.status) +
                (report.status === "completed" && report.archive?.valid ? '<div><a class="workspace-link" href="' +
                    presentationLinks(url, report.id).zip + '">Download archived final ZIP</a></div>' : '') + '</div>' : report.outputs ?
            exportCard(report, presentationLinks(url, report.id)) :
            '<div class="card"><a class="workspace-link" href="' + presentationLinks(url, report.id).pptx + '">' +
            esc(report.filename) + '</a> · ' + report.applied.length + ' edits · ' +
            esc(new Date(report.created).toLocaleString()) + '</div>').join("");
    };
    renderExports().catch(showError);
    const updateControls = () => {
        $("#compare").disabled = !$("#previous").value;
        $('#deck-upload button[value="version"]').disabled = !$("#previous").value;
    };
    updateControls();
    $("#previous").onchange = event => {
        W.previous = event.target.value;
        W.changes = null;
        $("#comparison-results").textContent = "";
        updateControls();
    };
    $("#compare").onclick = async event => {
        const previous = W.previous;
        const button = event.currentTarget;
        button.disabled = true;
        $("#comparison-results").innerHTML = '<p role="status">Comparing documents and requesting Qwen review…</p>';
        try {
            const result = await workspaceFetch(docUrl() + "/compare?include_analysis=1&previous_id=" +
                encodeURIComponent(previous));
            if (S.data === data && S.stab === "deck" && previous === W.previous) {
                W.changes = result;
                $("#comparison-results").innerHTML = analysisStatus(result.analysis) + changeList(result.changes);
            }
        } catch (error) { showError(error); }
        finally { if (button.isConnected) updateControls(); }
    };
    $("#deck-upload").onsubmit = async event => {
        event.preventDefault();
        const form = new FormData();
        if (!data.source_pptx) form.append("pptx", $("#deck-file").files[0]);
        const button = event.submitter;
        form.append("mode", button.value);
        if (button.value === "version") form.append("previous_id", W.previous);
        const buttons = event.currentTarget.querySelectorAll("button");
        buttons.forEach(item => { item.disabled = true; });
        $("#comparison-results").innerHTML = '<p role="status">Generating and validating export files…</p>';
        try {
            const url = docUrl() + "/presentations";
            const report = await submitExport(data, form);
            if (S.data !== data || S.stab !== "deck") return;
            $("#comparison-results").innerHTML = analysisStatus(report.analysis) +
                exportCard(report, presentationLinks(url, report.id)) +
                (report.status === "completed" ? '<p>' + report.applied.length + ' edits · ' +
                    report.review.length + ' changes for review</p>' : '') +
                report.applied.map(edit => '<div class="card"><b>Slide ' + edit.slide + '</b> · ' + esc(edit.shape) +
                    (edit.source ? '<div>PDF page ' + edit.source.page + ' · ' + esc(edit.source.xml_node) + '</div>' : '') +
                    '<div>' + esc(edit.old) + '</div><div>' + esc(edit.new) + '</div></div>').join("") +
                (report.status === "completed" ? changeList(report.review) : '');
            await renderExports(report.id);
        } catch (error) { showError(error); }
        finally {
            if (button.isConnected) {
                buttons.forEach(item => { item.disabled = false; });
                updateControls();
            }
        }
    };
}

$("#page-prev").onclick = () => movePage(-1);
$("#page-next").onclick = () => movePage(1);
$("#page-label").onchange = event => movePage(Number(event.target.value) - W.page);
$("#zoom-in").onclick = () => { W.zoom = Math.min(200, W.zoom + 25); workspaceViewer(); };
$("#zoom-out").onclick = () => { W.zoom = Math.max(50, W.zoom - 25); workspaceViewer(); };
$("#fit-width").onclick = () => { W.zoom = 100; workspaceViewer(); };
$("#fullscreen").hidden = !document.fullscreenEnabled;
$("#fullscreen").onclick = async () => {
    try {
        if (document.fullscreenElement) await document.exitFullscreen();
        else await $("#viewpane").requestFullscreen();
    } catch (error) { showError(error); }
};
document.addEventListener("fullscreenchange", () => {
    const active = !!document.fullscreenElement;
    $("#fullscreen").textContent = active ? "Exit fullscreen" : "Fullscreen";
    $("#fullscreen").setAttribute("aria-pressed", String(active));
});

async function submitExport(data, form) {
    const base = "/api/documents/" + encodeURIComponent(data.id);
    let finished = false;
    let timer;
    async function refresh() {
        try {
            const phases = await workspaceFetch(base + "/export-progress");
            if (!finished && Object.keys(phases).length) {
                W.exportPhases.set(data.id, phases);
                if (!W.batch && S.id === data.id) showOcrTimeline(data.jobs[0], false);
            }
        } finally {
            if (!finished) timer = setTimeout(() => refresh().catch(() => {}), 700);
        }
    }
    timer = setTimeout(() => refresh().catch(() => {}), 350);
    try {
        const report = await workspaceFetch(base + "/presentations", {method: "POST", body: form}).catch(error => {
            if (error.exportReport) return error.exportReport;
            throw error;
        });
        W.exportPhases.set(data.id, report.export_phases || {});
        if (S.id === data.id && !W.batch) showOcrTimeline(data.jobs[0], false);
        return report;
    } finally {
        finished = true;
        clearTimeout(timer);
    }
}

$("#dlpptx").onclick = async () => {
    const data = S.data, button = $("#dlpptx");
    if (W.batch || !data?.approved) return;
    button.disabled = true;
    try {
        const form = new FormData();
        form.set("mode", "generate");
        const report = await submitExport(data, form);
        if (S.data !== data) return;
        const links = presentationLinks(docUrl() + "/presentations", report.id);
        setReviewTab("deck");
        $("#comparison-results").innerHTML = exportCard(report, links);
        if (report.outputs.pptx) {
            const download = document.createElement("a");
            download.href = links.pptx;
            download.click();
        }
    } catch (error) { showError(error); }
    finally { if (S.data === data) button.disabled = false; }
};

let pendingFile = null;
let uploading = false;
const uploadZone = $("#upload-zone");
const uploadForm = $("#pdf-submit");
const uploadStatus = $("#upload-status");

function choosePdf(files) {
    if (uploading || W.batch) return;
    pendingFile = null;
    uploadForm.hidden = true;
    uploadStatus.textContent = "";
    if (files.length !== 1 || !/\.pdf$/i.test(files[0].name) || !files[0].size || files[0].size >= 64 * 1024 * 1024) {
        uploadStatus.textContent = "Choose one non-empty PDF smaller than 64 MB.";
        return;
    }
    pendingFile = files[0];
    $("#file-details").textContent = pendingFile.name + " · " + new Intl.NumberFormat(undefined, {
        style: "unit", unit: "kilobyte", maximumFractionDigits: 1
    }).format(pendingFile.size / 1000);
    uploadForm.hidden = false;
}

function uploadPdf(form) {
    return new Promise((resolve, reject) => {
        const xhr = new XMLHttpRequest();
        xhr.open("POST", "/api/documents");
        xhr.responseType = "json";
        xhr.upload.onprogress = event => {
            if (event.lengthComputable) {
                $("#upload-progress").max = event.total;
                $("#upload-progress").value = event.loaded;
            }
        };
        xhr.onload = () => xhr.status === 202 ? resolve(xhr.response) :
            reject(new Error(xhr.response?.error || "Upload failed (" + xhr.status + ")."));
        xhr.onerror = () => reject(new Error("The upload could not reach the workspace."));
        xhr.send(form);
    });
}

$("#upload").onclick = () => $("#pdf-file").click();
$("#pdf-file").onchange = event => {
    if (event.target.files.length) choosePdf(event.target.files);
    event.target.value = "";
};
uploadZone.ondragover = event => { event.preventDefault(); uploadZone.dataset.dragging = "true"; };
uploadZone.ondragleave = event => { if (!uploadZone.contains(event.relatedTarget)) delete uploadZone.dataset.dragging; };
uploadZone.ondrop = event => {
    event.preventDefault();
    delete uploadZone.dataset.dragging;
    choosePdf(event.dataTransfer.files);
};
uploadForm.onsubmit = async event => {
    event.preventDefault();
    if (uploading || !pendingFile) return;
    const form = new FormData();
    form.append("pdf", pendingFile);
    form.append("phase", $("#review-phase-select").value);
    for (const [key, value] of Object.entries(ocrJobOptions())) form.append(key, value);
    const previous = S.data?.pages && window.confirm("Link this upload as a new version of the selected PDF?");
    if (previous) form.append("previous_id", S.id);
    uploading = true;
    $("#upload").disabled = $("#batch").disabled = event.submitter.disabled = true;
    $("#upload-progress").hidden = false;
    $("#upload-progress").removeAttribute("value");
    uploadStatus.textContent = "Uploading PDF…";
    try {
        const doc = await uploadPdf(form);
        uploadForm.hidden = true;
        pendingFile = null;
        uploadStatus.textContent = "";
        statusText("PDF received");
        await loadWorkspace(doc.id);
    } catch (error) { uploadStatus.textContent = error.message; }
    finally {
        uploading = false;
        $("#upload-progress").hidden = true;
        $("#upload").disabled = $("#batch").disabled = event.submitter.disabled = false;
    }
};
$("#batch").onclick = async () => {
    clearTimeout(W.timer);
    W.selection++;
    W.batch = !W.batch;
    S.id = null;
    S.data = null;
    $("#batch").textContent = W.batch ? "Upload workspace" : "Batch review";
    $("#upload-zone").hidden = W.batch;
    $("#layout-model").hidden = W.batch;
    $("#dlpptx").hidden = W.batch;
    $("#start-pdf").hidden = true;
    $("#deck-tab").hidden = true;
    $("#ocr-controls").hidden = W.batch;
    $("#ocr-timeline").hidden = true;
    $("#processing-stages").hidden = true;
    $("#page-tools").hidden = true;
    $("#rot").disabled = true;
    $("#dlxml").disabled = !W.batch;
    $("#dlxml").hidden = !W.batch;
    $("#reproc").disabled = !W.batch;
    $("#reproc").hidden = false;
    delete $("#viewer").dataset.sourceStack;
    $("#viewer").onscroll = null;
    $("#viewer").textContent = "";
    $("#sbar").textContent = "";
    $("#sbody").textContent = "";
    nav("review");
    try { if (W.batch) await init(); else await loadWorkspace(); }
    catch (error) { showError(error); }
};
let startupRefresh;

async function loadDocumentAudit(startup) {
    const panel = $("#document-ai-audit"), id = S.id;
    if (W.batch || !id || !S.data?.pages) {
        panel.textContent = "Select a processed document to see its inference history.";
        return;
    }
    const endpoint = docUrl() + "/execution-audit";
    try {
        const data = await workspaceFetch(endpoint);
        if (id !== S.id) return;
        const usage = data["model_usage_report.json"], history = data["ai_execution_report.json"].executions;
        const counts = [
            usage.requests + " requests", usage.responses + " responses", usage.failures + " failures or rejections",
            usage.fallback_executions + " fallback executions", usage.used_responses + " responses used downstream",
            usage.ocr_pages + " OCR pages", usage.native_pages + " native pages"
        ];
        const inventory = (startup?.models || []).map(model => {
            const status = [];
            if (model.installed === true) status.push("INSTALLED");
            if (model.installed === true && startup.services?.some(service => service.url === model.url && service.healthy)) status.push("AVAILABLE");
            if (model.loaded === true) status.push("LOADED");
            if (status.includes("AVAILABLE") && !usage.executed_models[model.name]) status.push("AVAILABLE_NOT_EXECUTED");
            return '<div>' + esc(model.name + ' · ' + (status.join(" · ") || "Unverified")) + '</div>';
        }).join("");
        const downloads = Object.keys(data).map(name => '<a class="workspace-link" href="' + endpoint +
            '?report=' + encodeURIComponent(name) + '">' + esc(name) + '</a>').join(" · ");
        panel.innerHTML = '<h4>Selected document</h4><p>' + esc(counts.join(" · ")) + '</p>' + inventory + '<p>' + downloads + '</p>' +
            (history.length ? history.map(record => '<div class="card"><strong>' + esc(record.model) + '</strong>' +
                '<div>' + esc(record.task + (record.page ? " · Page " + record.page : "") +
                " · " + record.duration_ms + " ms") + '</div><div>' + esc(record.statuses.join(" · ")) +
                '</div><small>' + esc(record.execution_id) + '</small></div>').join("") :
                '<p>No verified model executions are recorded for this attempt. Check the page trace for its extraction methods.</p>');
    } catch (error) {
        if (id === S.id) panel.textContent = error.message;
    }
}

async function loadStartupStatus() {
    const details = $("#startup-details"), retry = $("#startup-retry"), indicator = $("#startup-button");
    clearTimeout(startupRefresh);
    try {
        const report = await workspaceFetch("/api/startup");
        const flag = value => value == null ? "Unverified" : value ? "Yes" : "No";
        const checking = report.retrying || report.status === "warming";
        const incomplete = report.models?.some(model => model.error || model.runtime_error ||
            model.warmup === "passed" && !model.loaded && !model.released_by_policy);
        const labels = {ready: "AI ready", degraded: "AI degraded", warming: "AI warming",
            failed: "AI startup failed", startup_disabled: "AI startup disabled", no_models_required: "AI optional",
            not_run: "AI startup", stopped: "AI stopped"};
        indicator.textContent = checking ? "AI warming" : report.status === "ready" && incomplete ?
            "AI review needed" : labels[report.status] || "AI startup";
        const services = (report.services || []).map(service => '<div>' + esc(service.url) + ' · Server running ' +
            flag(service.running) + ' · API healthy ' + flag(service.healthy) + ' · ' +
            esc(service.error || service.source || "") + '</div>').join("");
        details.innerHTML = '<div>Ollama installed ' + flag(report.ollama_installed) + '</div>' +
            '<div>Backend ready ' + flag(report.backend_ready) + ' · Frontend ready ' + flag(report.frontend_ready) + '</div>' +
            services + (report.error ? '<p class="notice">' + esc(report.error) + '</p>' : "") +
            (report.models || []).map(model => {
                const hardware = model.residency?.size_vram;
                const bytes = model.residency?.size;
                return '<div class="card"><strong>' + esc(model.label + " · " + model.name) + '</strong>' +
                    '<div>Installed ' + flag(model.installed) + ' · Loaded during startup ' + flag(model.loaded_during_startup) +
                    ' · Loaded now ' + flag(model.loaded) + ' · Warm-up ' +
                    esc(({passed: "Passed", failed: "Failed", on_demand: "On demand"})[model.warmup] || "Not run") + '</div>' +
                    (typeof hardware === "number" ? '<div>GPU memory reported by Ollama ' +
                        Math.round(hardware / 1048576) + ' MB</div>' : '') +
                    (typeof bytes === "number" ? '<div>Model memory ' + Math.round(bytes / 1048576) + ' MB</div>' : '') +
                    (model.error || model.runtime_error ? '<div class="notice">' + esc(model.error || model.runtime_error) + '</div>' : '') +
                    (model.installation ? '<code>' + esc(model.installation) + '</code>' : '') + '</div>';
            }).join("");
        retry.disabled = checking;
        if ($("#settings-dialog").open || checking) startupRefresh = setTimeout(loadStartupStatus, checking ? 1200 : 15000);
        return report;
    } catch (error) {
        details.textContent = error.message;
        retry.disabled = false;
    }
}

$("#startup-retry").onclick = async () => {
    $("#startup-retry").disabled = true;
    try {
        await workspaceFetch("/api/startup/retry", {
            method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"
        });
        await loadStartupStatus();
    } catch (error) {
        $("#startup-details").textContent = error.message;
        $("#startup-retry").disabled = false;
    }
};

$("#settings").onclick = () => {
    $("#settings-dialog").showModal();
    loadOcrSettings();
    loadStartupStatus().then(loadDocumentAudit);
    $("#settings-info").textContent = "Checking local processing settings…";
    $("#profile-status").textContent = "";
    const name = $("#profile-name"), save = $("#profile-form button");
    name.disabled = save.disabled = true;
    workspaceFetch("/api/profile").then(profile => {
        $("#profile-name").value = profile.name;
        $("#reviewer-label").textContent = profile.name;
        name.disabled = save.disabled = false;
    }).catch(error => { $("#profile-status").textContent = error.message; });
    workspaceFetch("/api/health").then(health => {
        const readiness = model => model.ready ? "ready" : model.reason || "unavailable";
        $("#settings-info").textContent = [
            health.runtime + " · " + health.xml_profile + " XML profile",
            "Ollama " + health.ollama,
            "Qwen " + health.qwen.model + " · " + readiness(health.qwen) + (health.qwen.enabled ? "" : " · review disabled"),
            !health.ocr_enabled ? "OCR disabled" : health.ocr_backend === "tesseract" ?
                "Tesseract languages " + health.ocr_languages.join(", ") : "Default OCR " + ocrModeName(health.ocr_backend),
            "Docling " + (health.docling?.ready ? "configured" : "unavailable"),
        ].join(" · ");
    }).catch(error => { $("#settings-info").textContent = error.message; });
};
$("#startup-button").onclick = () => {
    loadStartupStatus();
    $("#startup-status").scrollIntoView({block: "nearest"});
};
loadStartupStatus();
loadLayoutHealth();
$("#profile-form").onsubmit = async event => {
    event.preventDefault();
    try {
        const profile = await workspaceFetch("/api/profile", {method: "PATCH",
            headers: {"Content-Type": "application/json"}, body: JSON.stringify({name: $("#profile-name").value})});
        $("#profile-name").value = profile.name;
        $("#reviewer-label").textContent = profile.name;
        $("#profile-status").textContent = "Saved";
    } catch (error) { $("#profile-status").textContent = error.message; }
};
workspaceFetch("/api/profile").then(profile => { $("#reviewer-label").textContent = profile.name; }).catch(showError);
(new URLSearchParams(location.search).has("job") ? Promise.resolve() : loadWorkspace()).then(async () => {
    const prefs = await workspaceFetch("/api/ocr-settings");
    if (!ocrPreferenceChanged) $("#ocr-engine").value = prefs.engine;
    showOcrChoice();
    return loadOcrSettings();
}).catch(showError);

const W = {batch: false, documents: [], page: 1, zoom: 100, highlight: null,
           selection: 0, timer: null, previous: "", changes: null};
const docUrl = () => "/api/documents/" + encodeURIComponent(S.id);
const statusText = message => $("#runchip").textContent = message;
const engineName = engine => ({"glm-ocr": "GLM-OCR", paddleocr: "PaddleOCR", tesseract: "Tesseract"}[engine] || "");
const processingStatus = step => {
    if (!step) return "";
    if (step.stage === "diagnostics") return "Inspecting PDF pages";
    if (step.stage === "ocr") return engineName(step.engine) + " · page " + step.page;
    if (step.stage === "layout") return "Reconstructing layout · page " + step.page;
    if (step.stage === "validation") return "Validating document and XML";
    return "";
};
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
        throw new Error(body.error || "Request failed (" + response.status + ").");
    }
    return asText ? response.text() : response.json();
}

function setActive(button, active) {
    button.classList.toggle("on", active);
    button.setAttribute("aria-pressed", String(active));
}

function setView(tab) {
    S.vtab = tab;
    document.querySelectorAll("#vtabs .tab").forEach(button =>
        setActive(button, button.dataset.t === tab));
    if (W.batch) viewer(); else workspaceViewer();
}

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
        $("#reproc").disabled = true;
        statusText("Local workspace");
    }
}

async function selectWorkspace(id, reset = true) {
    const selection = ++W.selection;
    clearTimeout(W.timer);
    if (reset) {
        W.page = 1;
        W.highlight = null;
        W.previous = "";
        W.changes = null;
        S.rot = 0;
    }
    const data = await workspaceFetch("/api/documents/" + encodeURIComponent(id));
    if (W.batch || W.selection !== selection) return;
    S.id = id;
    S.data = data;
    $("#docsel").value = id;
    const job = data.jobs[0];
    const listed = W.documents.find(document => document.id === id);
    if (listed) listed.status = job.status;
    const active = job.status === "queued" || job.status === "running";
    const reviewPages = data.pages?.filter(page => page.requires_review).length || 0;
    statusText((active && processingStatus(job.processing) || job.status) +
        (job.total ? " · " + job.done + "/" + job.total + " pages" : "") +
        (reviewPages && !active ? " · " + reviewPages + " for review" : ""));
    $("#reproc").disabled = active;
    $("#reproc").textContent = job.status === "failed" ? "Retry" : "Reprocess";
    $("#dlxml").disabled = !data.approved;
    $("#dlxml").title = data.approved ? "Export XML" : "Approve metadata review before exporting";
    workspaceViewer();
    await reviewPanel();
    if (active && !W.batch && W.selection === selection) {
        W.timer = setTimeout(() => selectWorkspace(id, false).catch(showError), 800);
    }
}

function workspaceViewer() {
    const doc = S.data;
    if (!doc) return;
    const page = doc.pages?.[W.page - 1];
    $("#page-tools").hidden = !page;
    $("#rot").disabled = !page || S.vtab !== "pdf";
    $("#legend").hidden = true;
    if (!page) {
        $("#viewer").innerHTML = '<div class="empty">' + esc(doc.jobs[0].error ||
            "Processing the PDF. Page previews will appear when the attempt completes.") +
            ' <a class="workspace-link" target="_blank" rel="noopener" href="' + docUrl() + '/pdf">Open original PDF</a></div>';
        return;
    }
    $("#page-label").textContent = W.page + " / " + doc.pages.length;
    $("#page-prev").disabled = W.page === 1;
    $("#page-next").disabled = W.page === doc.pages.length;
    $("#zoom-out").disabled = W.zoom === 50;
    $("#zoom-in").disabled = W.zoom === 200;
    if (S.vtab === "text") {
        $("#viewer").innerHTML = '<div class="textview">' + page.blocks.map(block => {
            let details = esc(block.text || block.caption || block.alt);
            if (block.cells.length) {
                const rows = new Map();
                for (const cell of block.cells) {
                    if (!rows.has(cell.row)) rows.set(cell.row, []);
                    const tag = cell.header ? "th" : "td";
                    rows.get(cell.row).push('<' + tag + ' rowspan="' + cell.rowspan + '" colspan="' + cell.colspan + '">' +
                        esc(cell.text) + '</' + tag + '>');
                }
                details += "<table>" + [...rows.values()].map(row => "<tr>" + row.join("") + "</tr>").join("") + "</table>";
            }
            return '<article class="tb"><small>' + esc(block.kind || block.tag) + '</small>' + details + '</article>';
        }).join("") + '</div>';
        return;
    }
    let overlay = "";
    const rectangle = (box, color, label) => {
        const [x, y, right, bottom] = box;
        return '<rect x="' + x + '" y="' + y + '" width="' + (right - x) + '" height="' + (bottom - y) +
            '" ' + color + '><title>' + esc(label) + '</title></rect>';
    };
    if (S.vtab === "zoning" || S.vtab === "order") {
        overlay = page.blocks.filter(block => block.bbox).map(block => {
            const [x, y] = block.bbox;
            const color = col(block.tag);
            return S.vtab === "zoning" ? rectangle(block.bbox, 'fill="' + color + '" fill-opacity=".17" stroke="' +
                color + '"', block.kind || block.tag) :
                '<circle cx="' + x + '" cy="' + (y + 8) + '" r="10" fill="' + color + '"/><text x="' + x +
                '" y="' + (y + 12) + '" text-anchor="middle" fill="white">' + (block.i + 1) + '</text>';
        }).join("");
    }
    if (W.highlight && S.vtab === "pdf" && !S.rot) {
        overlay += rectangle(W.highlight, 'class="highlight"', "Title source");
    }
    const dpi = Math.min(220, Math.round(100 * W.zoom / 100));
    $("#viewer").innerHTML = '<div class="pl"><span>' + esc(page.source === "ocr" ?
        engineName(page.ocr_engine) || "OCR" : "Native PDF") + ' · ' + W.zoom + '%</span>' +
        '<a class="workspace-link" target="_blank" rel="noopener" href="' + docUrl() + '/pdf#page=' + page.n +
        '">Open original</a></div><div class="page" style="width:' + W.zoom + '%;max-width:' + (860 * W.zoom / 100) +
        'px"><img src="' + docUrl() + '/page/' + page.n + '.png?dpi=' + dpi + '&amp;rot=' +
        (S.vtab === "pdf" ? S.rot : 0) + '" alt="Page ' + page.n + '">' +
        (overlay ? '<svg viewBox="0 0 ' + page.w + ' ' + page.h + '" preserveAspectRatio="none">' + overlay + '</svg>' : '') + '</div>';
}

function movePage(step) {
    W.page += step;
    W.highlight = null;
    workspaceViewer();
    if (S.stab === "meta") {
        const diagnostics = $("#page-diagnostics");
        if (diagnostics) diagnostics.innerHTML = pageDiagnostics(S.data.pages[W.page - 1]);
    }
}

function pageDiagnostics(page) {
    const diag = page.diag, ocr = diag.ocr;
    const confidence = diag.confidence == null ? "Confidence unavailable" : badge(diag.confidence * 100);
    const selected = page.source === "ocr" ? engineName(page.ocr_engine || ocr?.engine) || "OCR" : "Native PDF";
    const attempts = (ocr?.attempts || []).map(attempt => '<div>' + esc(engineName(attempt.engine)) +
        ' · ' + esc(attempt.status) + (attempt.issues.length ? ' · ' + esc(attempt.issues.join(" ")) : '') + '</div>').join("");
    return '<div class="sec">Page ' + page.n + ' diagnostics</div><div class="card">' +
        '<div>' + esc(selected) + ' · ' + confidence + '</div>' +
        '<div>Native text: ' + esc(diag.health || "unknown") +
        (Number.isInteger(diag.native_chars) ? ' · ' + diag.native_chars + ' characters' : '') + '</div>' +
        '<div>' + (diag.ocr_required ? "OCR required" : "OCR not required") +
        (diag.appears_scanned ? ' · scanned page' : '') + '</div>' + attempts +
        (diag.layout ? '<div>Layout: ' + esc(diag.layout.engine) + ' · ' + esc(diag.layout.status) + '</div>' : '') +
        (page.requires_review ? '<strong class="notice">Manual review required</strong>' : '') +
        '<div>' + esc((diag.reasons || []).join(" ")) + '</div></div>';
}

async function reviewPanel() {
    const data = S.data, panel = $("#sbody"), bar = $("#sbar");
    if (!data) return;
    const job = data.jobs[0];
    bar.textContent = data.approved ? "Review approved" : "Review pending";
    if (data.approved && data.review) bar.title = "Reviewed by " + data.review.reviewer;
    else bar.removeAttribute("title");
    $("#dlxml").style.display = S.stab === "deck" ? "none" : "";
    if (S.stab === "xml") {
        panel.textContent = "";
        if (!data.pages) return;
        const xml = await workspaceFetch(docUrl() + "/xml", undefined, true);
        if (S.data !== data || S.stab !== "xml" || W.batch) return;
        const preview = document.createElement("pre");
        preview.className = "xml";
        preview.textContent = xml;
        panel.append(preview);
        return;
    }
    if (S.stab === "deck" && data.pages) {
        changesPanel(data);
        return;
    }
    const history = '<div class="sec">Processing history</div>' + data.jobs.map(attempt =>
        '<div class="card"><div>' + esc(attempt.status) + ' · ' + esc(new Date(attempt.created).toLocaleString()) +
        '</div>' + (attempt.error ? '<div class="notice">' + esc(attempt.error) + '</div>' : '') + '</div>').join("");
    if (!data.pages) {
        panel.innerHTML = history;
        return;
    }
    const page = data.pages[W.page - 1], title = data.title;
    panel.innerHTML = '<form id="metadata-form" class="workspace-tools">' +
        '<div class="fld"><label for="review-title">Title ' + (title?.confidence != null ? badge(title.confidence) : '') + '</label>' +
        '<input id="review-title" maxlength="1000" value="' + esc(title?.value) + '">' +
        (title?.bbox ? '<button type="button" class="btn" id="locate">Locate title source</button>' : '') + '</div>' +
        '<div class="fld"><label for="review-lang">Document language</label>' +
        '<input id="review-lang" required maxlength="35" value="' + esc(data.lang) + '"></div>' +
        '<div><button class="btn" value="save">Save draft</button> <button class="btn pri" value="approve">Approve</button></div></form>' +
        '<div id="page-diagnostics">' + pageDiagnostics(page) + '</div>' +
        '<div class="sec">Validation</div><div class="card">XML ' +
        (data.validation.issues.length ? esc(data.validation.issues.join(", ")) : "passed structural checks") + '</div>' +
        data.warnings.map(warning => '<div class="notice">' + esc(warning) + '</div>').join("") + history;
    const locate = $("#locate");
    if (locate) locate.onclick = () => {
        W.page = title.page;
        W.highlight = title.bbox;
        S.rot = 0;
        setView("pdf");
        $("#page-diagnostics").innerHTML = pageDiagnostics(data.pages[W.page - 1]);
    };
    const form = $("#metadata-form");
    form.querySelectorAll("input,button").forEach(control => control.disabled = job.status !== "completed");
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

function changesPanel(data) {
    const alternatives = W.documents.filter(doc => doc.id !== data.id && doc.status === "completed");
    W.previous ||= data.previous_id || alternatives[0]?.id || "";
    $("#sbar").textContent = "Document changes";
    $("#sbody").innerHTML = '<div class="workspace-tools"><label for="previous">Previous version</label>' +
        '<select id="previous"><option value="">Select a processed PDF</option>' +
        alternatives.map(doc => '<option value="' + esc(doc.id) + '">' + esc(doc.filename) + ' · ' +
                                 doc.id.slice(0, 6) + '</option>').join("") + '</select>' +
        '<button id="compare" class="btn pri">Compare versions</button>' +
        '<form id="deck-upload" class="workspace-tools"><label for="deck-file">Related PowerPoint</label>' +
        '<input id="deck-file" type="file" accept=".pptx" required>' +
        '<button class="btn" value="source">Repair against source PDF</button>' +
        '<button class="btn" value="version">Apply version changes</button></form></div>' +
        '<div id="comparison-results"></div><div id="presentation-history"></div>';
    $("#previous").value = W.previous;
    const changeList = changes => changes.length ? changes.map(change =>
        '<article class="card"><div class="h">' + esc(change.category) + ' · ' + esc(change.change_type) +
        ' · ' + (change.original_location?.page || "—") + ' → ' + (change.new_location?.page || "—") +
        ' · ' + badge(change.confidence * 100) + '</div><div>' + esc(change.old_content) + '</div><div>' +
        esc(change.new_content) + '</div>' + (change.assistance ? '<div>Local model: ' + esc(change.assistance.relation) +
        ' · candidate on page ' + esc(change.assistance.candidate.page) + '</div>' : '') +
        (change.reason ? '<div>' + esc(change.reason) + '</div>' : '') +
        '</article>').join("") : '<p>No changes detected.</p>';
    if (W.changes) $("#comparison-results").innerHTML = changeList(W.changes);
    const renderExports = async () => {
        const url = docUrl() + "/presentations";
        const reports = await workspaceFetch(url);
        const history = $("#presentation-history");
        if (!history || S.data !== data || S.stab !== "deck" || W.batch || !reports.length) return;
        history.innerHTML = '<div class="sec">Presentation exports</div>' + reports.map(report =>
            '<div class="card"><a class="workspace-link" href="' + url + '/' + esc(report.id) + '">' +
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
    $("#compare").onclick = async () => {
        const previous = W.previous;
        try {
            const changes = await workspaceFetch(docUrl() + "/compare?previous_id=" + encodeURIComponent(previous));
            if (S.data === data && S.stab === "deck" && previous === W.previous) {
                W.changes = changes;
                $("#comparison-results").innerHTML = changeList(changes);
            }
        } catch (error) { showError(error); }
    };
    $("#deck-upload").onsubmit = async event => {
        event.preventDefault();
        const form = new FormData();
        form.append("pptx", $("#deck-file").files[0]);
        const button = event.submitter;
        form.append("mode", button.value);
        if (button.value === "version") form.append("previous_id", W.previous);
        button.disabled = true;
        try {
            const url = docUrl() + "/presentations";
            const report = await workspaceFetch(url, {method: "POST", body: form});
            if (S.data !== data || S.stab !== "deck") return;
            $("#comparison-results").innerHTML = '<p><a class="workspace-link" href="' + url + '/' +
                esc(report.id) + '">Download validated presentation</a> · ' + report.applied.length +
                ' edits · ' + report.review.length + ' changes for review</p>' +
                '<p><a class="workspace-link" href="' + url + '/' + esc(report.id) +
                    '/evidence.xml">Download source evidence XML</a></p>' +
                report.applied.map(edit => '<div class="card"><b>Slide ' + edit.slide + '</b> · ' + esc(edit.shape) +
                    (edit.source ? '<div>PDF page ' + edit.source.page + ' · ' + esc(edit.source.xml_node) + '</div>' : '') +
                    '<div>' + esc(edit.old) + '</div><div>' + esc(edit.new) + '</div></div>').join("") + changeList(report.review);
            await renderExports();
        } catch (error) { showError(error); }
        finally { button.disabled = false; }
    };
}

$("#page-prev").onclick = () => movePage(-1);
$("#page-next").onclick = () => movePage(1);
$("#zoom-in").onclick = () => { W.zoom += 25; workspaceViewer(); };
$("#zoom-out").onclick = () => { W.zoom -= 25; workspaceViewer(); };
$("#upload").onclick = () => $("#pdf-file").click();
$("#pdf-file").onchange = async event => {
    const file = event.target.files[0];
    event.target.value = "";
    if (!file) return;
    const form = new FormData();
    form.append("pdf", file);
    const previous = S.data?.pages && window.confirm("Link this upload as a new version of the selected PDF?");
    if (previous) form.append("previous_id", S.id);
    $("#upload").disabled = true;
    try {
        const doc = await workspaceFetch("/api/documents", {method: "POST", body: form});
        await loadWorkspace(doc.id);
    } catch (error) { showError(error); }
    finally { $("#upload").disabled = false; }
};
$("#batch").onclick = async () => {
    clearTimeout(W.timer);
    W.selection++;
    W.batch = !W.batch;
    S.id = null;
    S.data = null;
    $("#batch").textContent = W.batch ? "Upload workspace" : "Batch review";
    $("#upload").hidden = W.batch;
    $("#page-tools").hidden = true;
    $("#rot").disabled = true;
    $("#nv-dash").hidden = !W.batch;
    $("#dlxml").disabled = !W.batch;
    $("#reproc").disabled = !W.batch;
    $("#viewer").textContent = "";
    $("#sbar").textContent = "";
    $("#sbody").textContent = "";
    nav("review");
    try { if (W.batch) await init(); else await loadWorkspace(); }
    catch (error) { showError(error); }
};
$("#settings").onclick = () => {
    $("#settings-dialog").showModal();
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
        const ocr = !health.ocr_enabled ? "OCR disabled" : health.ocr_backend === "tesseract" ?
            "Tesseract languages: " + health.ocr_languages.join(", ") :
            "GLM-OCR " + (health.glm_ocr?.ready ? "ready" : "unavailable") +
            " · PaddleOCR " + (health.paddle_ocr?.ready ? "ready" : "unavailable");
        $("#settings-info").textContent = health.runtime + " · " + health.xml_profile +
            " XML profile · " + ocr + " · Docling " + (health.docling?.ready ? "ready" : "unavailable");
    }).catch(error => { $("#settings-info").textContent = error.message; });
};
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
$("#nv-dash").hidden = true;
workspaceFetch("/api/profile").then(profile => { $("#reviewer-label").textContent = profile.name; }).catch(showError);
loadWorkspace().catch(showError);

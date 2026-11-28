"use strict";

const bulk = selector => document.querySelector(selector);
const endpoint = "/api/bulk/batches";
const picker = bulk("#bulk-directory");
const drop = bulk("#bulk-drop");
const chooser = bulk("#bulk-history");
const uploadProgress = bulk("#bulk-upload-progress");
const statusName = {
    checking: "Checking files", ready: "Ready", needs_review: "Needs file review",
    queued: "Queued", processing: "Processing", completed: "Completed", failed: "Failed",
    partial: "Partially completed", source_review: "Needs source review", canceled: "Canceled"
};
const stageName = {
    pdf_extraction: "Extracting PDF", diagnostics: "Inspecting PDF pages", rendering: "Rendering PDF pages",
    detection: "Detecting page layout", ocr: "Reading page text", ocr_attempt: "Reading page text",
    extracted: "Extracting PDF text", association: "Matching text to layout", layout: "Reconstructing reading order",
    xml_generation: "Generating XML", assets: "Saving XML assets", validation: "Validating XML",
    pptx: "Proofreading PowerPoint", pptx_analysis: "Comparing presentation to PDF",
    pptx_correction: "Correcting slides", final_validation: "Validating PowerPoint", zip: "Packaging results"
};
const summaryLabels = {
    folders: "Folders", pdf: "Valid PDFs", pptx: "Valid PPTX", complete_pairs: "Complete pairs",
    missing_pdf: "Missing PDF", missing_pptx: "Missing PPTX", invalid: "Invalid files", review: "Need file review"
};
const countLabels = {
    total: "Total jobs", queued: "Queued", processing: "Processing", completed: "Completed",
    failed: "Failed", partial: "Partial", source_review: "Source review", canceled: "Canceled"
};
const stamp = new Intl.DateTimeFormat(undefined, {dateStyle: "medium", timeStyle: "short"});
let current = new URLSearchParams(location.search).get("batch");
let selection = null;
let busy = false;
let update = 0;
let poll;
let previousMarkup = "";

function escapeText(value) {
    return String(value ?? "").replace(/[&<>"']/g, character =>
        ({"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"})[character]);
}

async function api(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "The local workspace could not complete the request.");
    return data;
}

function message(text = "", error = false) {
    const node = bulk("#bulk-message");
    node.textContent = text;
    node.toggleAttribute("data-error", error);
}

function setBusy(value) {
    busy = value;
    for (const selector of ["#bulk-drop", "#bulk-history", "#bulk-new", "#bulk-phase", "#bulk-engine", "#bulk-language"]) {
        bulk(selector).disabled = value;
    }
}

function chooseBatch(id) {
    current = id;
    const url = new URL(location.href);
    if (id) url.searchParams.set("batch", id);
    else url.searchParams.delete("batch");
    history.replaceState(null, "", url);
    chooser.value = id || "";
}

async function historyList() {
    const {batches} = await api(endpoint);
    chooser.replaceChildren(new Option("Select a batch", ""));
    for (const batch of batches) {
        chooser.add(new Option(`${stamp.format(new Date(batch.created))} · Phase ${batch.phase} · ${batch.folders} folders`, batch.id));
    }
    chooser.value = current || "";
}

function selectedFolders(fileList) {
    const groups = new Map();
    for (const file of fileList) {
        const path = file.webkitRelativePath.split("/");
        if (path.length < 2) throw new Error("This browser did not supply relative paths. Try dropping the folder.");
        const name = path.length === 2 ? path[0] : path[1];
        if (!groups.has(name)) groups.set(name, {name, files: []});
        groups.get(name).files.push({file, nested: path.length > 3});
    }
    return [...groups.values()];
}

function readDirectory(entry) {
    const reader = entry.createReader();
    return new Promise((resolve, reject) => {
        const items = [];
        function read() {
            reader.readEntries(entries => {
                if (entries.length === 0) resolve(items);
                else {
                    items.push(...entries);
                    read();
                }
            }, reject);
        }
        read();
    });
}

async function findFiles(entries, nested = false) {
    const files = [];
    for (const entry of entries) {
        if (entry.isDirectory) {
            const children = await readDirectory(entry);
            files.push(...await findFiles(children, true));
        } else {
            const file = await new Promise((resolve, reject) => entry.file(resolve, reject));
            files.push({file, nested});
        }
        if (files.length > 2000) throw new Error("Select at most 2,000 files in one batch.");
    }
    return files;
}

async function droppedFolders(entries) {
    const groups = [];
    if (!entries.length || entries.some(entry => !entry?.isDirectory)) {
        throw new Error("Drop folders containing PDF and PowerPoint files, or choose a parent folder.");
    }
    for (const entry of entries) {
        const children = await readDirectory(entry);
        const directories = children.filter(child => child.isDirectory);
        const loose = children.filter(child => child.isFile);
        if (loose.length || !directories.length) groups.push({name: entry.name, files: await findFiles(loose)});
        for (const directory of directories) {
            groups.push({name: directory.name, files: await findFiles(await readDirectory(directory))});
        }
    }
    return groups;
}

function statCards(labels, values) {
    return Object.entries(labels).map(([key, label]) =>
        `<div><dt>${label}</dt><dd>${values[key]}</dd></div>`).join("");
}

function fileList(files, kind) {
    return files.filter(file => file.kind === kind).map(file => `<div class="bulk-file">${escapeText(file.name)}
        ${file.error ? `<small class="bulk-error">${escapeText(file.error)}</small>` : ""}
        </div>`).join("") || '<span aria-label="Missing">—</span>';
}

function liveProgress(job) {
    if (job.status !== "processing") return "—";
    const label = stageName[job.stage] || "Starting the saved pair";
    const slides = job.slide_count > 0 && job.slide >= 0;
    const pages = ["ocr", "ocr_attempt", "extracted"].includes(job.stage) && job.page_count > 0;
    const total = slides ? job.slide_count : job.page_count;
    const done = slides ? job.slide : job.pages;
    const unit = slides ? "slides" : "PDF pages";
    return `${label}${slides || pages ? `<small>${done} / ${total} ${unit}</small>
        <progress aria-label="${label}" value="${done}" max="${total}"></progress>` :
        `<progress aria-label="${label}"></progress>`}`;
}

function jobRow(job) {
    const issues = [...job.issues, ...job.files.filter(file => file.kind === "unexpected")
        .map(file => file.name + " — " + file.error)];
    return `<tr><th scope="row">${escapeText(job.folder)}
        ${issues.length ? `<details data-folder="${job.id}"><summary>File issues</summary>
            <ul>${issues.map(issue => `<li>${escapeText(issue)}</li>`).join("")}</ul></details>` : ""}
        </th><td>${fileList(job.files, "pdf")}</td><td>${fileList(job.files, "pptx")}</td>
        <td><span class="history-status ${job.status}">${statusName[job.status]}</span>
            ${job.error ? `<small class="bulk-error">${escapeText(job.error)}</small>` : ""}
            ${job.warnings ? `<small>${job.warnings} review warning${job.warnings === 1 ? "" : "s"}</small>` : ""}</td>
        <td>${liveProgress(job)}</td>
        <td><div class="bulk-actions">
        ${job.view ? `<a id="view-${job.id}" href="${job.view}" target="_blank" rel="noopener">View results</a>` : ""}
        ${Object.entries(job.downloads).map(([kind, url]) =>
            `<a id="${kind}-${job.id}" href="${url}" aria-label="Download ${kind.toUpperCase()} for ${escapeText(job.folder)}">${kind.toUpperCase()}</a>`).join("")}
        ${job.can_retry ? `<button id="retry-${job.id}" class="bulk-text-button" data-action="retry" data-job="${job.id}">${job.status === "canceled" ? "Requeue" : "Retry"}</button>` : ""}
        ${job.status === "queued" ? `<button id="cancel-${job.id}" class="bulk-text-button" data-action="cancel" data-job="${job.id}">Cancel</button>` : ""}
        </div></td></tr>`;
}

function render(batch) {
    bulk("#bulk-intake").hidden = Boolean(batch);
    bulk("#bulk-review").hidden = !batch;
    bulk("#bulk-resume-upload").hidden = Boolean(!selection || !batch?.summary.pending || busy);
    if (!batch) return;
    const {started, summary, counts} = batch;
    bulk("#bulk-review-heading").textContent = started ? "Processing queue" : "Folder review";
    bulk("#bulk-batch-meta").textContent = `Phase ${batch.phase} · ${stamp.format(new Date(batch.created))}`;
    const start = bulk("#bulk-start");
    start.hidden = Boolean(started);
    start.disabled = busy || !summary.complete_pairs || Boolean(summary.pending);
    bulk("#bulk-cancel").hidden = !counts.queued;
    const download = bulk("#bulk-download");
    download.hidden = !batch.download;
    if (batch.download) download.href = batch.download;
    bulk("#bulk-summary").innerHTML = statCards(summaryLabels, summary);
    const countDisplay = bulk("#bulk-counts");
    countDisplay.hidden = !started;
    if (started) countDisplay.innerHTML = statCards(countLabels, counts);
    const list = bulk("#bulk-jobs");
    const markup = batch.jobs.map(jobRow).join("");
    if (markup === previousMarkup) return;
    const focused = list.contains(document.activeElement) && document.activeElement.id;
    const open = new Set([...list.querySelectorAll("details[open]")].map(node => node.dataset.folder));
    list.innerHTML = previousMarkup = markup;
    for (const node of list.querySelectorAll("details")) node.open = open.has(node.dataset.folder);
    if (focused) document.getElementById(focused)?.focus({preventScroll: true});
}

async function refresh({reportError = true} = {}) {
    clearTimeout(poll);
    const revision = ++update;
    if (!current) return render(null);
    try {
        const batch = await api(`${endpoint}/${current}`);
        if (revision !== update) return;
        render(batch);
        if (batch.counts.processing || batch.counts.queued || batch.counts.source_review) {
            poll = setTimeout(() => refresh(), 2000);
        }
        if (batch.summary.pending && !busy && !selection) {
            message("This upload was interrupted. Choose New batch to select the folders again.");
        }
    } catch (error) {
        if (revision === update && reportError) {
            message(error.message, true);
            poll = setTimeout(() => refresh({reportError: false}), 3000);
        }
    }
}

async function uploadSelection() {
    if (!selection) return;
    const {id, groups, options} = selection;
    const batch = await api(endpoint, {method: "POST", headers: {"Content-Type": "application/json"},
        body: JSON.stringify({id, ...options, folders: groups.map(group => ({name: group.name,
            files: group.files.map(({file, nested}) => ({name: file.name, size: file.size, nested}))}))})});
    chooseBatch(id);
    let remaining = batch.summary.pending;
    const total = remaining;
    uploadProgress.max = Math.max(1, total);
    uploadProgress.value = 0;
    uploadProgress.hidden = !remaining;
    render(batch);
    await historyList();
    for (const [index, job] of batch.jobs.entries()) {
        for (const [position, entry] of job.files.entries()) {
            if (entry.status !== "pending") continue;
            message(`Checking files · ${total - remaining} of ${total} uploaded`);
            const form = new FormData();
            form.append("file", groups[index].files[position].file);
            await api(`${endpoint}/${id}/files/${entry.id}`, {method: "PUT", body: form});
            uploadProgress.value = total - --remaining;
        }
    }
    selection = null;
    message("Files checked. Review the pairs before starting.");
}

async function intake(loadGroups) {
    if (busy) return;
    setBusy(true);
    message("Reading selected folders…");
    try {
        if (loadGroups) {
            const groups = await loadGroups();
            if (!groups.length) throw new Error("Choose at least one folder containing files.");
            selection = {groups, id: crypto.randomUUID().replaceAll("-", ""), options: {
                phase: bulk("#bulk-phase").value, ocr_engine: bulk("#bulk-engine").value,
                ocr_language: bulk("#bulk-language").value}};
        }
        await uploadSelection();
    } catch (error) {
        message(error.message, true);
    } finally {
        uploadProgress.hidden = true;
        setBusy(false);
        await refresh({reportError: false});
    }
}

async function action(path, button) {
    if (button.disabled) return;
    button.disabled = true;
    try {
        const result = await api(`${endpoint}/${current}/${path}`, {method: "POST"});
        message(path.endsWith("cancel") ? `${result.canceled} pending job${result.canceled === 1 ? "" : "s"} canceled.` : "");
    } catch (error) {
        message(error.message, true);
    } finally {
        button.disabled = false;
        await refresh();
    }
}

drop.onclick = () => picker.click();
picker.onchange = () => {
    const groups = [...picker.files];
    picker.value = "";
    if (groups.length) intake(() => selectedFolders(groups));
};
drop.ondragover = event => { event.preventDefault(); if (!busy) drop.dataset.dragging = "true"; };
drop.ondragleave = () => delete drop.dataset.dragging;
drop.ondrop = event => {
    event.preventDefault();
    delete drop.dataset.dragging;
    if (busy) return;
    const entries = [...event.dataTransfer.items].filter(item => item.kind === "file")
        .map(item => item.getAsEntry?.() || item.webkitGetAsEntry?.());
    intake(() => droppedFolders(entries));
};
chooser.onchange = () => { selection = null; message(); chooseBatch(chooser.value); refresh(); };
bulk("#bulk-new").onclick = () => { selection = null; message(); chooseBatch(null); refresh(); drop.focus(); };
bulk("#bulk-resume-upload").onclick = () => intake();
bulk("#bulk-start").onclick = event => action("start", event.currentTarget);
bulk("#bulk-cancel").onclick = event => action("cancel", event.currentTarget);
bulk("#bulk-jobs").onclick = event => {
    const button = event.target.closest("button[data-action]");
    if (button) action(`jobs/${button.dataset.job}/${button.dataset.action}`, button);
};
historyList().then(() => refresh()).catch(error => message(error.message, true));

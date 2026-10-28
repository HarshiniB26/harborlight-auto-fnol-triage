const queue = document.querySelector("#queue");
const panel = document.querySelector("#case");
const exportButton = document.querySelector("#export-results");
const exportMessage = document.querySelector("#export-message");
const search = document.querySelector("#search");
const queueCount = document.querySelector("#queue-count");
let selected = null;
let poll = null;
let filter = "work";
let cases = [];

const labels = {
  new: "Not processed",
  draft_ready: "Ready to Submit",
  pending: "Needs Review",
  approved: "Approved",
  rejected: "Rejected",
  record_created: "Record created",
};
const routes = {
  standard: "Standard Handling",
  urgent: "Urgent",
  specialist: "Specialist",
  manual_review: "Manual review",
};
const factLabels = {
  policy_number: "Policy",
  incident_timestamp: "Incident date",
  vehicle_id: "Vehicle",
  injury_reported: "Injury reported",
  selected_policy_version: "Policy version",
};

async function api(path, method = "GET") {
  const response = await fetch(path, {
    method,
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: method === "POST" ? "{}" : undefined,
  });
  const body = await response.json();
  if (!response.ok && !body.error) throw new Error("Request failed");
  return body;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

function shown(value) {
  if (value === null || value === undefined || value === "") return "Not in the documents";
  if (value === true) return "Yes";
  if (value === false) return "No";
  return String(value);
}

function age(value) {
  if (!value) return "";
  const then = new Date(value).getTime();
  if (Number.isNaN(then)) return "";
  const hours = Math.max(0, Math.round((Date.now() - then) / 3600000));
  if (hours < 1) return "just now";
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`;
  const days = Math.round(hours / 24);
  return `${days} day${days === 1 ? "" : "s"} ago`;
}

function visibleCases() {
  const query = search.value.trim().toLowerCase();
  return cases.filter((item) => {
    if (query && !item.case_id.toLowerCase().includes(query)) return false;
    if (filter === "pending") return item.status === "pending";
    if (filter === "records") return item.status === "record_created";
    return true;
  });
}

async function loadQueue() {
  const data = await api("/api/cases");
  cases = data.cases;
  const rows = visibleCases();
  const titles = { work: "All Cases", pending: "Pending Review", records: "Created Records" };
  queueCount.textContent = `${titles[filter]} (${rows.length})`;
  queue.innerHTML = rows.map((item) => `
    <li><button class="case${item.case_id === selected ? " active" : ""}" data-id="${esc(item.case_id)}">
      <span class="case-top">
        <strong>${esc(item.case_id)}</strong>
        <span class="chip ${esc(item.routing || item.status)}">${esc(item.routing ? routes[item.routing] || item.routing : labels[item.status] || item.status)}</span>
      </span>
      <span class="case-sub">
        <span>${esc(item.classification || "Loss type after draft")}</span>
        <span>${esc(age(item.processed_at))}</span>
      </span>
    </button></li>`).join("") || `<li class="empty">No cases in this queue.</li>`;
  queue.querySelectorAll(".case").forEach((button) => {
    button.addEventListener("click", () => openCase(button.dataset.id));
  });
}

async function openCase(caseId) {
  selected = caseId;
  const item = await api(`/api/cases/${encodeURIComponent(caseId)}`);
  render(item);
  loadQueue();
}

function sourceKind(sourceId) {
  if (sourceId.endsWith("INTAKE")) return "Intake";
  if (sourceId.endsWith("ESTIMATE")) return "Estimate";
  if (sourceId.endsWith("REPORT")) return "Report";
  return "Document";
}

function render(item) {
  const draft = item.draft;
  const review = item.review;
  const facts = draft ? draft.facts : {};
  const route = draft ? (routes[draft.routing] || draft.routing) : "Not processed";
  const command = review
    ? `uv run --locked python tools/runtime.py approve ${review.review_id} --reviewer reviewer-name`
    : "";
  const sources = item.sources || [];
  panel.innerHTML = `
    <div class="case-head">
      <div>
        <div class="title-row">
          <h2>${esc(item.case_id)}</h2>
          <span class="pill ${esc(draft ? draft.routing : item.status)}">${esc(route)}</span>
        </div>
        <p class="subhead">${esc(draft ? draft.classification : "Prepare a draft to read this loss")} · ${esc(labels[item.status] || item.status)}</p>
      </div>
    </div>
    ${draft ? `
      <section class="cards">
        <article class="card"><p class="label">Incident date</p><strong>${esc(shown(facts.incident_timestamp))}</strong></article>
        <article class="card"><p class="label">Vehicle</p><strong>${esc(shown(facts.vehicle_id))}</strong></article>
        <article class="card"><p class="label">Policy</p><strong>${esc(shown(facts.policy_number))}</strong><p class="label">Version ${esc(shown(facts.selected_policy_version))}</p></article>
        <article class="card"><p class="label">Loss type</p><strong>${esc(shown(draft.classification))}</strong></article>
      </section>
      <section class="split">
        <div>
          <article class="card">
            <h3>Accident summary</h3>
            <p>${esc(draft.narrative)}</p>
            ${Object.entries(factLabels).map(([key, label]) => `<p><span class="label">${esc(label)}</span><br>${esc(shown(facts[key]))}</p>`).join("")}
            ${draft.missing_information.length ? `<p class="banner">Missing: ${draft.missing_information.map(esc).join(", ")}</p>` : ""}
            ${(draft.follow_up_questions || []).map((q) => `<p><strong>${esc(q.field)}.</strong> ${esc(q.question)}</p>`).join("")}
          </article>
          <article class="card triage">
            <h3>Triage result</h3>
            <p><strong>${esc(route)}</strong></p>
            <p>${item.status === "record_created" ? "A record is on file for this loss." : "Draft only. Human approval automatically creates the FNOL record."}</p>
          </article>
        </div>
        <div>
          <article class="card">
            <div class="card-head"><h3>Supporting documents (${sources.length})</h3></div>
            ${sources.map((source, index) => `<div class="doc">
              <div><strong>${esc(source.source_id)}</strong><div class="label">${esc(sourceKind(source.source_id))}</div>
                <pre id="doc-${index}" class="hidden">${esc(source.text)}</pre>
              </div>
              <button class="view" type="button" data-doc="doc-${index}">View</button>
            </div>`).join("") || `<p class="empty">No documents are available.</p>`}
          </article>
          <article class="card">
            <h3>Generated FNOL draft</h3>
            ${draft.citations.map((c) => `<p><strong>${esc(c.claim)}</strong><br><span class="label">${esc(c.source_id)}</span></p><blockquote>${esc(c.quote)}</blockquote>`).join("")}
          </article>
        </div>
      </section>
    ` : `<p class="empty">Prepare a draft to see the facts, route, and documents.</p>`}
    ${item.warnings && item.warnings.length ? `<p class="banner">${item.warnings.map(esc).join(" ")}</p>` : ""}
    ${item.record ? `<p class="banner">Record ${esc(item.record.record_id)} is on file for this loss.</p>` : ""}
    ${item.status === "pending" ? `<p class="banner">This draft is awaiting human approval. Approval automatically creates the FNOL record.</p><pre class="source-view">${esc(command)}</pre>` : ""}
    ${(item.activity || []).length ? `<article class="card"><h3>Case activity</h3><ol class="activity">${item.activity.map((event) => `
      <li><strong>${esc(event.label)}</strong>
        ${event.reviewer ? `<span>Reviewer: ${esc(event.reviewer)}</span>` : ""}
        ${event.review_id ? `<span>Review: ${esc(event.review_id)}</span>` : ""}
        ${event.record_id ? `<span>Record: ${esc(event.record_id)}</span>` : ""}
        ${event.at ? `<span>${esc(event.at)}</span>` : ""}
        ${event.trace_id ? `<code>${esc(event.trace_id)}</code>` : ""}
      </li>`).join("")}</ol></article>` : ""}
    <div class="actions">
      <button class="action" id="process" type="button" ${item.status === "pending" || item.status === "approved" || item.status === "record_created" ? "disabled" : ""}>Prepare draft</button>
      <button class="action primary" id="submit" type="button" ${draft && item.status === "draft_ready" ? "" : "disabled"}>Submit for Review</button>
    </div>
    <p id="message"></p>`;
  panel.querySelector("#process").addEventListener("click", () => act(`/api/cases/${item.case_id}/process`));
  panel.querySelector("#submit").addEventListener("click", () => act(`/api/cases/${item.case_id}/review`));
  clearInterval(poll);
  if (item.status === "pending" || item.status === "approved") {
    poll = setInterval(() => {
      if (selected === item.case_id) openCase(item.case_id);
    }, 3000);
  }
  panel.querySelectorAll(".view").forEach((button) => {
    button.addEventListener("click", () => {
      const doc = panel.querySelector(`#${button.dataset.doc}`);
      doc.classList.toggle("hidden");
      button.textContent = doc.classList.contains("hidden") ? "View" : "Hide";
    });
  });
}

async function exportResults() {
  exportMessage.textContent = "Preparing export…";
  const response = await fetch("/api/results", { headers: { Accept: "application/json" } });
  const body = await response.json();
  if (!response.ok) {
    exportMessage.textContent = `${body.message || body.error} Missing: ${(body.missing_case_ids || []).join(", ")}`;
    return;
  }
  const blob = new Blob([JSON.stringify(body, null, 2)], { type: "application/json" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "results.json";
  link.click();
  URL.revokeObjectURL(link.href);
  exportMessage.textContent = `Exported ${body.length} drafts.`;
}

exportButton.addEventListener("click", () => {
  exportResults().catch((error) => { exportMessage.textContent = error.message; });
});
document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    filter = button.dataset.filter;
    document.querySelectorAll(".nav-item").forEach((item) => item.classList.toggle("active", item === button));
    loadQueue();
  });
});
search.addEventListener("input", () => loadQueue());

async function act(path) {
  const result = await api(path, "POST");
  const message = panel.querySelector("#message");
  if (result.error) message.textContent = result.message || result.error;
  await openCase(selected);
  if (result.error && message) {
    const slot = panel.querySelector("#message");
    if (slot) slot.textContent = result.message || result.error;
  }
}

loadQueue().catch((error) => {
  panel.innerHTML = `<p class="banner">${esc(error.message)}. Start the mock on port 8767, then refresh.</p>`;
});

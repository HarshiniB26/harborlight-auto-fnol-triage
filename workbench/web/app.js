const queue = document.querySelector("#queue");
const panel = document.querySelector("#case");
const exportButton = document.querySelector("#export-results");
const exportMessage = document.querySelector("#export-message");
let selected = null;

const labels = {
  new: "Not processed",
  draft_ready: "Draft ready",
  pending: "Awaiting approval",
  approved: "Approved",
  rejected: "Rejected",
  record_created: "Record created",
};

async function api(path, method = "GET", payload = null) {
  const response = await fetch(path, {
    method,
    headers: { Accept: "application/json", "Content-Type": "application/json" },
    body: method === "POST" ? JSON.stringify(payload || {}) : undefined,
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

async function loadQueue() {
  const data = await api("/api/cases");
  queue.innerHTML = data.cases.map((item) => `
    <li><button class="case${item.case_id === selected ? " active" : ""}" data-id="${esc(item.case_id)}">
      <strong>${esc(item.case_id)}</strong>
      <span>${esc(labels[item.status] || item.status)}</span>
    </button></li>`).join("");
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

function render(item) {
  const draft = item.draft;
  const review = item.review;
  const facts = draft ? draft.facts : null;
  panel.innerHTML = `
    <p class="meta">Auto FNOL</p>
    <h2>${esc(item.case_id)}</h2>
    <p class="status">${esc(labels[item.status] || item.status)}</p>
    <div class="row">
      <button class="action" id="process" ${item.status === "pending" || item.status === "approved" ? "disabled" : ""}>Prepare draft</button>
      <button class="action secondary" id="submit" ${draft && item.status === "draft_ready" ? "" : "disabled"}>Submit for review</button>
      <button class="action secondary" id="file" ${item.status === "approved" ? "" : "disabled"}>Create record</button>
      <button class="action secondary" id="refresh">Refresh</button>
    </div>
    ${item.warnings && item.warnings.length ? `<p class="banner">${item.warnings.map(esc).join(" ")}</p>` : ""}
    ${draft ? `
      <h3>${esc(draft.classification)} · ${esc(draft.routing)}</h3>
      <h3>Decision data</h3>
      <dl>
        ${Object.entries(facts).map(([key, value]) => `<dt>${esc(key)}</dt><dd>${esc(value === null ? "null" : value)}</dd>`).join("")}
        <dt>policy_ids</dt><dd>${draft.policy_ids.length ? draft.policy_ids.map(esc).join(", ") : "[]"}</dd>
        <dt>missing_information</dt><dd>${draft.missing_information.length ? draft.missing_information.map(esc).join(", ") : "[]"}</dd>
        <dt>deadline</dt><dd>${esc(draft.deadline === null ? "null" : draft.deadline)}</dd>
      </dl>
      <h3>Draft</h3>
      <p>${esc(draft.narrative)}</p>
      ${draft.missing_information.length ? `<p class="banner">Missing: ${draft.missing_information.map(esc).join(", ")}</p>` : ""}
      ${(draft.follow_up_questions || []).length ? `<h3>Follow-up questions</h3>${draft.follow_up_questions.map((q) => `<p><strong>${esc(q.field)}:</strong> ${esc(q.question)}</p>`).join("")}` : ""}
      <h3>Evidence citations</h3>
      <p class="help">Each claim below is supported by the exact quoted text from the named source.</p>
      ${draft.citations.map((c) => `<article class="citation">
        <p><strong>${esc(c.claim)}</strong></p>
        <p class="source">Source: ${esc(c.source_id)}</p>
        <blockquote class="quote">${esc(c.quote)}</blockquote>
      </article>`).join("")}
    ` : `<p class="empty">Prepare a draft to see the facts, route, and quotes.</p>`}
    ${review ? `<h3>Review ${esc(review.review_id)}</h3><p>Status: ${esc(review.status)}${review.reviewer ? ` · ${esc(review.reviewer)}` : ""}</p>` : ""}
    ${item.status === "pending" ? `<section class="review-box" aria-labelledby="review-heading">
      <h3 id="review-heading">Human decision</h3>
      <p class="help">Review the exact draft and citations above. Enter your name to create an auditable human decision.</p>
      <label for="reviewer-name">Reviewer name</label>
      <input id="reviewer-name" name="reviewer" maxlength="80" autocomplete="name" placeholder="Your full name">
      <div class="row">
        <button class="action" id="approve-review">Approve exact draft</button>
        <button class="action danger" id="reject-review">Reject draft</button>
      </div>
    </section>` : ""}
    ${item.record ? `<p class="banner">Live system status: record ${esc(item.record.record_id)} is on file for this loss.</p>` : ""}
    ${(item.activity || []).length ? `<h3>Case activity</h3><ol class="activity">${item.activity.map((event) => `
      <li><strong>${esc(event.label)}</strong>
        ${event.reviewer ? `<span>Reviewer: ${esc(event.reviewer)}</span>` : ""}
        ${event.review_id ? `<span>Review: ${esc(event.review_id)}</span>` : ""}
        ${event.record_id ? `<span>Record: ${esc(event.record_id)}</span>` : ""}
        ${event.at ? `<span>${esc(event.at)}</span>` : ""}
        ${event.trace_id ? `<code>${esc(event.trace_id)}</code>` : ""}
      </li>`).join("")}</ol>` : ""}
    <p id="message"></p>`;
  panel.querySelector("#process").addEventListener("click", () => act(`/api/cases/${item.case_id}/process`));
  panel.querySelector("#submit").addEventListener("click", () => act(`/api/cases/${item.case_id}/review`));
  panel.querySelector("#file").addEventListener("click", () => act(`/api/cases/${item.case_id}/record`));
  panel.querySelector("#refresh").addEventListener("click", () => openCase(item.case_id));
  if (item.status === "pending") {
    const decide = (decision) => {
      const reviewer = panel.querySelector("#reviewer-name").value.trim();
      if (!reviewer) {
        panel.querySelector("#message").textContent = "Enter your name before deciding.";
        return;
      }
      act(`/api/cases/${encodeURIComponent(item.case_id)}/decision`, { reviewer, decision });
    };
    panel.querySelector("#approve-review").addEventListener("click", () => decide("approved"));
    panel.querySelector("#reject-review").addEventListener("click", () => decide("rejected"));
  }
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
  exportMessage.textContent = `Exported ${body.length} contract-compliant drafts.`;
}

exportButton.addEventListener("click", () => {
  exportResults().catch((error) => { exportMessage.textContent = error.message; });
});

async function act(path, payload = null) {
  const result = await api(path, "POST", payload);
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

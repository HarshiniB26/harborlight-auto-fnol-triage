
const queue = document.querySelector("#queue");
const panel = document.querySelector("#case");
const caseCount = document.querySelector("#case-count");
const pendingCount = document.querySelector("#pending-count");
const queueState = document.querySelector("#queue-state");
const queueSearch = document.querySelector("#queue-search");
const globalSearch = document.querySelector("#global-search");
const exportButton = document.querySelector("#export-results");
const exportMessage = document.querySelector("#export-message");

let cases = [];
let selected = null;
let activeView = "all";
let searchTerm = "";

const labels = {
  new: "Preparing", draft_ready: "Draft ready", pending: "Needs review",
  approved: "Approved", rejected: "Rejected", record_created: "Record created",
};

const routeLabels = {
  standard: "Standard handling",
  urgent: "Urgent handling",
  specialist: "Specialist review",
  manual_review: "Manual review",
};

async function api(path, method = "GET", payload = null) {
  const response = await fetch(path, {
    method,
    headers: {
      Accept: "application/json",
      "Content-Type": "application/json",
    },
    body: method === "POST" ? JSON.stringify(payload || {}) : undefined,
  });
  const body = await response.json();
  if (!response.ok && !body.error) throw new Error("Request failed");
  return body;
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (character) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[character]));
}

function pretty(value) {
  return String(value || "—")
    .replaceAll("_", " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

function formatTime(value) {
  if (!value) return "Awaiting automation";
  const date = new Date(value);
  return Number.isNaN(date.valueOf())
    ? value
    : new Intl.DateTimeFormat("en-US", {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
      }).format(date);
}

function filteredCases() {
  return cases.filter((item) => {
    const matchesView =
      activeView === "all" ||
      (activeView === "reports" && Boolean(item.classification)) ||
      item.status === activeView;

    const haystack = [
      item.case_id,
      item.classification,
      item.routing,
      item.status,
    ].join(" ").toLowerCase();

    return matchesView && haystack.includes(searchTerm.toLowerCase());
  });
}

// Display the actual routing decision, not an inferred priority.
function routeFor(item) {
  const routes = {
    urgent: ["Urgent handling", "high"],
    specialist: ["Specialist review", "medium"],
    manual_review: ["Manual review", "medium"],
    standard: ["Standard handling", "standard"],
  };

  return routes[item.routing] || ["Awaiting routing", "standard"];
}

function renderQueue() {
  const visible = filteredCases();
  caseCount.textContent = visible.length;
  pendingCount.textContent = cases.filter(
    (item) => item.status === "pending"
  ).length;

  queueState.textContent = visible.length
    ? ""
    : "No drafts match this view.";

  queue.innerHTML = visible.map((item) => {
    const [routeLabel, routeClass] = routeFor(item);

    return `<li>
      <button class="case-card${item.case_id === selected ? " active" : ""}"
              data-id="${esc(item.case_id)}">
        <span class="case-card-top">
          <span class="priority ${routeClass}">${esc(routeLabel)}</span>
          <strong>${esc(item.case_id)}</strong>
          <time>${esc(formatTime(item.processed_at))}</time>
        </span>
        <span class="case-card-main">
          ${esc(pretty(item.classification || "Awaiting draft"))}
        </span>
        <span class="case-card-bottom">
          <span>FNOL triage</span>
          <span class="state ${esc(item.status)}">
            ${esc(labels[item.status] || item.status)}
          </span>
        </span>
      </button>
    </li>`;
  }).join("");

  queue.querySelectorAll(".case-card").forEach((button) => {
    button.addEventListener("click", () => openCase(button.dataset.id));
  });
}

async function loadQueue({ keepSelection = true } = {}) {
  queueState.textContent = "Refreshing drafts…";
  const data = await api("/api/cases");
  cases = data.cases;
  renderQueue();

  if (
    keepSelection &&
    selected &&
    cases.some((item) => item.case_id === selected)
  ) return;

  const first = filteredCases()[0];
  if (first) await openCase(first.case_id);
}

async function openCase(caseId) {
  selected = caseId;
  renderQueue();
  panel.setAttribute("aria-busy", "true");

  const item = await api(
    `/api/cases/${encodeURIComponent(caseId)}`
  );

  renderCase(item);
  panel.removeAttribute("aria-busy");
  panel.focus({ preventScroll: true });
}

function factRows(facts) {
  const preferred = [
    ["Incident timestamp", facts.incident_timestamp],
    ["Policy number", facts.policy_number],
    ["Vehicle ID", facts.vehicle_id],
    [
      "Injury reported",
      facts.injury_reported === true
        ? "Yes"
        : facts.injury_reported === false
          ? "No"
          : facts.injury_reported,
    ],
  ];

  return preferred.map(([label, value]) =>
    `<div><dt>${esc(label)}</dt><dd>${esc(value ?? "Not supplied")}</dd></div>`
  ).join("");
}

function documentType(sourceId) {
  if (sourceId.endsWith("-INTAKE")) return "First notice of loss";
  if (sourceId.endsWith("-ESTIMATE")) return "Repair estimate";
  if (sourceId.endsWith("-REPORT")) return "Incident report";
  return "Supporting evidence";
}

function renderCase(item) {
  const draft = item.draft;

  if (!draft) {
    panel.innerHTML = `
      <section class="welcome">
        <span class="welcome-icon">◷</span>
        <h2>${esc(item.case_id)} is being prepared</h2>
        <p>
          The background intake worker has not submitted this case
          for review yet. Refresh the queue after processing completes.
        </p>
        <button class="button secondary" id="refresh-case">
          Refresh status
        </button>
      </section>`;

    panel.querySelector("#refresh-case")
      .addEventListener("click", () => openCase(item.case_id));
    return;
  }

  const facts = draft.facts || {};
  const review = item.review;
  const [routeLabel, routeClass] = routeFor({
    routing: draft.routing,
  });
  const route = routeLabels[draft.routing] || pretty(draft.routing);

  panel.innerHTML = `
    <header class="case-header">
      <div>
        <div class="title-line">
          <h2>${esc(item.case_id)}</h2>
          <span class="priority ${routeClass}">${esc(routeLabel)}</span>
        </div>
        <p>
          ${esc(pretty(draft.classification))} ·
          Policy ${esc(facts.policy_number || "not supplied")}
        </p>
      </div>
      <div class="header-status">
        <span>Status</span>
        <strong class="state ${esc(item.status)}">
          ${esc(labels[item.status] || item.status)}
        </strong>
        <small>
          Prepared ${esc(formatTime((item.activity[0] || {}).at))}
        </small>
      </div>
    </header>

    <section class="summary-grid" aria-label="Case summary">
      <article>
        <span class="summary-icon">◷</span>
        <div>
          <small>Incident date</small>
          <strong>${esc(facts.incident_timestamp || "Not supplied")}</strong>
        </div>
      </article>
      <article>
        <span class="summary-icon">▰</span>
        <div>
          <small>Vehicle</small>
          <strong>${esc(facts.vehicle_id || "Not supplied")}</strong>
        </div>
      </article>
      <article>
        <span class="summary-icon">▤</span>
        <div>
          <small>Policy</small>
          <strong>
            ${esc(facts.policy_number || "Not supplied")}
            ${facts.selected_policy_version
              ? ` · Version ${esc(facts.selected_policy_version)}`
              : ""}
          </strong>
        </div>
      </article>
      <article>
        <span class="summary-icon">✦</span>
        <div>
          <small>Loss type</small>
          <strong>${esc(pretty(draft.classification))}</strong>
        </div>
      </article>
    </section>

    ${item.warnings.length ? `
      <div class="alert warning">
        <strong>Untrusted content ignored</strong>
        <span>${item.warnings.map(esc).join(" ")}</span>
      </div>` : ""}

    <div class="content-grid">
      <section class="card incident-card">
        <div class="card-heading">
          <div>
            <p class="eyebrow">Verified intake</p>
            <h3>Decision data</h3>
          </div>
        </div>
        <dl class="fact-list">${factRows(facts)}</dl>
        ${draft.missing_information.length ? `
          <div class="alert warning">
            <strong>Information required</strong>
            <span>
              ${draft.missing_information.map(pretty).map(esc).join(", ")}
            </span>
          </div>` : ""}
      </section>

      <section class="card documents-card">
        <div class="card-heading">
          <div>
            <p class="eyebrow">Evidence</p>
            <h3>
              Supporting documents
              <span>${item.documents.length}</span>
            </h3>
          </div>
        </div>
        <ul class="document-list">
          ${item.documents.map((sourceId) => `
            <li>
              <span class="doc-icon">DOC</span>
              <span>
                <strong>${esc(sourceId)}</strong>
                <small>${esc(documentType(sourceId))}</small>
              </span>
            </li>`).join("")}
        </ul>
      </section>

      <section class="card triage-card">
        <div class="card-heading">
          <div>
            <p class="eyebrow">Deterministic rules</p>
            <h3>Triage result</h3>
          </div>
        </div>
        <div class="route-result ${esc(draft.routing)}">
          <span class="route-check">✓</span>
          <div>
            <strong>${esc(route)}</strong>
            <p>
              ${draft.routing === "manual_review"
                ? "The case needs a handler because an exception or unavailable dependency was detected."
                : "The case is ready for the recommended human queue."}
            </p>
          </div>
        </div>
        <div class="reason-list">
          <strong>Decision basis</strong>
          <ul>
            <li>
              ${esc(draft.policy_ids.length
                ? `Policy sources: ${draft.policy_ids.join(", ")}`
                : "Policy selection requires human resolution")}
            </li>
            <li>
              ${esc(draft.missing_information.length
                ? "Required information is missing"
                : "Required intake information is present")}
            </li>
            <li>
              No coverage, liability, fraud, payment, or settlement
              decision was made
            </li>
          </ul>
        </div>
      </section>

      <section class="card draft-card">
        <div class="card-heading">
          <div>
            <p class="eyebrow">Generated output</p>
            <h3>FNOL draft preview</h3>
          </div>
          <button class="text-button" id="show-evidence">
            View evidence
          </button>
        </div>
        <div class="draft-preview">
          <div class="draft-meta">
            <span>
              Classification
              <strong>${esc(pretty(draft.classification))}</strong>
            </span>
            <span>
              Route
              <strong>${esc(route)}</strong>
            </span>
          </div>
          <p>${esc(draft.narrative)}</p>
        </div>
      </section>
    </div>

    <section class="card evidence-panel" id="evidence-panel" hidden>
      <div class="card-heading">
        <div>
          <p class="eyebrow">Source-grounded</p>
          <h3>Evidence citations</h3>
        </div>
      </div>
      <div class="citations">
        ${draft.citations.map((citation) => `
          <article>
            <strong>${esc(citation.claim)}</strong>
            <small>${esc(citation.source_id)}</small>
            <blockquote>${esc(citation.quote)}</blockquote>
          </article>`).join("")}
      </div>
    </section>

    <section class="review-bar" aria-label="Human review actions">
      <div class="review-context">
        <strong>
          ${review
            ? `Review ${esc(review.review_id)}`
            : "Review unavailable"}
        </strong>
        <span>
          ${review && review.reviewer
            ? `Decided by ${esc(review.reviewer)}`
            : "Human approval is required before record creation."}
        </span>
      </div>
      <div class="review-actions">
        ${item.status === "pending" ? `
          <label class="reviewer-field">
            <span>Reviewer name</span>
            <input id="reviewer-name" maxlength="80"
                   autocomplete="name" placeholder="Enter your name">
          </label>
          <button class="button danger secondary" id="reject-review">
            Reject
          </button>
          <button class="button" id="approve-review">
            Approve draft
          </button>` : ""}

        ${item.status === "approved" ? `
          <span class="decision-note">
            Approved — claim creation pending retry.
          </span>` : ""}

        ${item.status === "rejected" ? `
          <span class="decision-note rejected">
            This draft was rejected and remains on file
            for handler follow-up.
          </span>` : ""}

        ${item.status === "record_created" ? `
          <span class="decision-note created">
            Record ${esc((item.record || {}).record_id)} created
          </span>` : ""}

        <button class="button secondary compact"
                id="refresh-case" aria-label="Refresh case">
          ↻
        </button>
      </div>
      <p class="action-message" id="message" role="status"></p>
    </section>`;

  panel.querySelector("#refresh-case").addEventListener(
    "click", () => openCase(item.case_id)
  );

  panel.querySelector("#show-evidence").addEventListener(
    "click", (event) => {
      const evidence = panel.querySelector("#evidence-panel");
      evidence.hidden = !evidence.hidden;
      event.currentTarget.textContent =
        evidence.hidden ? "View evidence" : "Hide evidence";
    }
  );

  if (item.status === "pending") {
    const decide = (decision) => {
      const reviewer = panel.querySelector("#reviewer-name").value.trim();

      if (!reviewer) {
        panel.querySelector("#message").textContent =
          "Enter your name before deciding.";
        panel.querySelector("#reviewer-name").focus();
        return;
      }

      act(
        `/api/cases/${encodeURIComponent(item.case_id)}/decision`,
        { reviewer, decision }
      );
    };

    panel.querySelector("#approve-review").addEventListener(
      "click", () => decide("approved")
    );
    panel.querySelector("#reject-review").addEventListener(
      "click", () => decide("rejected")
    );
  }
}

async function act(path, payload = null) {
  panel.querySelectorAll("button").forEach((button) => {
    button.disabled = true;
  });

  const result = await api(path, "POST", payload);
  await loadQueue({ keepSelection: true });
  await openCase(selected);

  if (result.error) {
    panel.querySelector("#message").textContent =
      result.message || result.error;
  }
}

async function exportResults() {
  exportMessage.textContent = "Preparing export…";
  const response = await fetch("/api/results", {
    headers: { Accept: "application/json" },
  });
  const body = await response.json();

  if (!response.ok) {
    exportMessage.textContent = body.message || body.error;
    return;
  }

  const blob = new Blob(
    [JSON.stringify(body, null, 2)],
    { type: "application/json" }
  );

  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "results.json";
  link.click();
  URL.revokeObjectURL(link.href);

  exportMessage.textContent = `Exported ${body.length} drafts.`;
}

function updateSearch(value) {
  searchTerm = value.trim();
  queueSearch.value = value;
  globalSearch.value = value;
  renderQueue();
}

queueSearch.addEventListener(
  "input", (event) => updateSearch(event.target.value)
);

globalSearch.addEventListener(
  "input", (event) => updateSearch(event.target.value)
);

document.querySelector("#refresh-queue").addEventListener(
  "click", () => loadQueue()
);

document.querySelectorAll(".nav-item").forEach((button) => {
  button.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach(
      (item) => item.classList.remove("active")
    );
    button.classList.add("active");
    activeView = button.dataset.view;
    renderQueue();
  });
});

exportButton.addEventListener(
  "click",
  () => exportResults().catch((error) => {
    exportMessage.textContent = error.message;
  })
);

loadQueue({ keepSelection: false }).catch((error) => {
  queueState.textContent = "Case queue unavailable.";
  panel.innerHTML = `
    <section class="welcome error">
      <h2>Workbench unavailable</h2>
      <p>
        ${esc(error.message)}.
        Check the internal Runtime service and refresh.
      </p>
    </section>`;
});

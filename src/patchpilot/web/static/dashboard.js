const POLL_MS = 2000;
const TERMINAL = new Set(["completed", "rejected", "failed", "cancelled"]);

function detailText(detail) {
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail)) return "The request was rejected.";
  return detail
    .map((item) => {
      const loc = Array.isArray(item.loc)
        ? item.loc.filter((part) => part !== "body").join(".")
        : "";
      const message = typeof item.msg === "string" ? item.msg : "invalid";
      return loc ? `${loc}: ${message}` : message;
    })
    .join(" ");
}

async function postJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "application/json" },
    body: JSON.stringify(payload),
  });
  const body = response.status === 204 ? {} : await response.json();
  return { response, body };
}

function bindCreateForm() {
  const form = document.querySelector("#create-task");
  if (!form) return;
  const error = document.querySelector("#form-error");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const url = form.getAttribute("data-create-url");
    if (!url) return;
    const payload = {
      title: form.elements.title.value,
      description: form.elements.description.value,
    };
    const repo = form.elements.repo_id.value;
    if (repo) payload.repo_id = repo;
    const button = form.querySelector("button");
    if (button) button.disabled = true;
    if (error) error.textContent = "";
    try {
      const { response, body } = await postJson(url, payload);
      if (response.status === 201 && body.id) {
        window.location.assign(`/tasks/${body.id}`);
        return;
      }
      if (error) error.textContent = detailText(body.detail);
    } catch {
      if (error) error.textContent = "The task API could not be reached.";
    }
    if (button) button.disabled = false;
  });
}

function bindDecisionForm() {
  const form = document.querySelector("#decision-form");
  if (!form) return;
  const error = document.querySelector("#decision-error");
  form.addEventListener("click", async (event) => {
    const button = event.target.closest("button[data-decision]");
    if (!button) return;
    event.preventDefault();
    const url = form.getAttribute("data-decision-url");
    const approvalId = form.getAttribute("data-approval-id");
    const decision = button.getAttribute("data-decision");
    if (!url || !approvalId || !decision) return;
    const payload = { approval_id: approvalId, decision };
    const reason = form.elements.reason.value.trim();
    if (reason) payload.reason = reason;
    form.querySelectorAll("button").forEach((item) => {
      item.disabled = true;
    });
    if (error) error.textContent = "";
    try {
      const { response, body } = await postJson(url, payload);
      if (response.ok || response.status === 409) {
        window.location.reload();
        return;
      }
      if (error) error.textContent = detailText(body.detail);
    } catch {
      if (error) error.textContent = "The task API could not be reached.";
    }
    form.querySelectorAll("button").forEach((item) => {
      item.disabled = false;
    });
  });
}

function lastEventId() {
  const items = document.querySelectorAll("#timeline [data-event-id]");
  if (items.length === 0) return "";
  return items[items.length - 1].getAttribute("data-event-id") || "";
}

function appendEvents(snapshot) {
  const timeline = document.querySelector("#timeline");
  if (!timeline) return;
  snapshot.querySelectorAll("[data-event-id]").forEach((item) => {
    const id = item.getAttribute("data-event-id");
    if (!id || document.querySelector(`[data-event-id="${CSS.escape(id)}"]`)) {
      return;
    }
    const empty = document.querySelector("#timeline-empty");
    if (empty) empty.remove();
    timeline.append(item);
  });
}

async function poll() {
  const root = document.querySelector("[data-task-page]");
  if (!root) return;
  const progress = root.getAttribute("data-progress-url");
  if (!progress) return;
  const after = lastEventId();
  const url = after ? `${progress}?after=${encodeURIComponent(after)}` : progress;
  try {
    const response = await fetch(url, { headers: { accept: "text/html" }, cache: "no-store" });
    if (!response.ok) {
      window.setTimeout(poll, POLL_MS);
      return;
    }
    const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
    const snapshot = parsed.querySelector("[data-progress]");
    if (!snapshot) {
      window.setTimeout(poll, POLL_MS);
      return;
    }
    const status = snapshot.getAttribute("data-status") || "";
    const stage = snapshot.getAttribute("data-stage") || "";
    const sameStatus = status === (root.getAttribute("data-status") || "");
    const sameStage = stage === (root.getAttribute("data-stage") || "");
    if (!sameStatus || !sameStage) {
      window.location.reload();
      return;
    }
    const summary = document.querySelector("#result-summary");
    const nextSummary = snapshot.getAttribute("data-summary");
    if (
      summary &&
      nextSummary !== null &&
      nextSummary !== (root.getAttribute("data-summary") || "")
    ) {
      summary.textContent = nextSummary;
      root.setAttribute("data-summary", nextSummary);
    }
    appendEvents(snapshot);
    if (!TERMINAL.has(status)) window.setTimeout(poll, POLL_MS);
  } catch {
    window.setTimeout(poll, POLL_MS);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  bindCreateForm();
  bindDecisionForm();
  if (document.querySelector("[data-task-page]")) window.setTimeout(poll, POLL_MS);
});

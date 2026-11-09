"use strict";

const STATUSES = [
  { value: "todo", label: "To do" },
  { value: "in_progress", label: "In progress" },
  { value: "done", label: "Done" },
];

const form = document.getElementById("task-form");
const createButton = document.getElementById("create-button");
const notification = document.getElementById("notification");
let tasks = [];

async function apiRequest(url, options = {}) {
  const response = await fetch(url, {
    ...options,
    headers: { "Content-Type": "application/json", ...options.headers },
  });
  if (!response.ok) {
    let message = `Request failed (${response.status})`;
    try {
      const data = await response.json();
      if (typeof data.detail === "string") message = data.detail;
    } catch (_error) {
      // Response was not JSON; retain the HTTP status in the message.
    }
    throw new Error(message);
  }
  return response.status === 204 ? null : response.json();
}

function showError(error) {
  notification.textContent = error.message || "An unexpected error occurred.";
  notification.hidden = false;
}

function clearError() {
  notification.hidden = true;
  notification.textContent = "";
}

function dateLabel(timestamp) {
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return "Unknown date";
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function createTaskCard(task) {
  const card = document.createElement("article");
  card.className = "task-card";
  card.dataset.taskId = String(task.id);

  const top = document.createElement("div");
  top.className = "card-top";
  const title = document.createElement("h4");
  title.className = "card-title";
  // Always use textContent for user data; never insert task text as HTML.
  title.textContent = task.title;
  top.appendChild(title);

  const deleteButton = document.createElement("button");
  deleteButton.type = "button";
  deleteButton.className = "delete-button";
  deleteButton.title = "Delete task";
  deleteButton.setAttribute("aria-label", `Delete ${task.title}`);
  deleteButton.textContent = "×";
  deleteButton.addEventListener("click", async () => {
    if (!window.confirm(`Delete "${task.title}"?`)) return;
    deleteButton.disabled = true;
    try {
      await apiRequest(`/api/tasks/${task.id}`, { method: "DELETE" });
      tasks = tasks.filter((item) => item.id !== task.id);
      clearError();
      renderBoard();
    } catch (error) {
      deleteButton.disabled = false;
      showError(error);
    }
  });
  top.appendChild(deleteButton);
  card.appendChild(top);

  if (task.description) {
    const description = document.createElement("p");
    description.className = "card-description";
    description.textContent = task.description;
    card.appendChild(description);
  }

  const bottom = document.createElement("div");
  bottom.className = "card-bottom";
  const date = document.createElement("span");
  date.className = "card-date";
  date.textContent = `Created ${dateLabel(task.created_at)}`;
  bottom.appendChild(date);

  const statusSelect = document.createElement("select");
  statusSelect.className = "card-status";
  statusSelect.setAttribute("aria-label", `Status for ${task.title}`);
  for (const status of STATUSES) {
    const option = document.createElement("option");
    option.value = status.value;
    option.textContent = status.label;
    statusSelect.appendChild(option);
  }
  statusSelect.value = task.status;
  statusSelect.addEventListener("change", async () => {
    const nextStatus = statusSelect.value;
    statusSelect.disabled = true;
    try {
      const updated = await apiRequest(`/api/tasks/${task.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status: nextStatus }),
      });
      tasks = tasks.map((item) => (item.id === updated.id ? updated : item));
      clearError();
      renderBoard();
    } catch (error) {
      statusSelect.value = task.status;
      statusSelect.disabled = false;
      showError(error);
    }
  });
  bottom.appendChild(statusSelect);
  card.appendChild(bottom);
  return card;
}

function renderBoard() {
  const counts = Object.fromEntries(STATUSES.map(({ value }) => [value, 0]));
  for (const status of STATUSES) {
    document.getElementById(`column-${status.value}`).replaceChildren();
  }
  for (const task of tasks) {
    const column = document.getElementById(`column-${task.status}`);
    if (!column) continue;
    column.appendChild(createTaskCard(task));
    counts[task.status] += 1;
  }
  for (const status of STATUSES) {
    const column = document.getElementById(`column-${status.value}`);
    document.getElementById(`count-${status.value}`).textContent = String(counts[status.value]);
    if (counts[status.value] === 0) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      const symbol = document.createElement("span");
      symbol.className = "empty-symbol";
      symbol.setAttribute("aria-hidden", "true");
      symbol.textContent = "◇";
      const message = document.createElement("p");
      message.textContent = "Nothing here yet.\nYour next task awaits.";
      empty.append(symbol, message);
      column.appendChild(empty);
    }
  }
  document.getElementById("stat-total").textContent = String(tasks.length);
  document.getElementById("stat-active").textContent = String(counts.in_progress);
  document.getElementById("stat-done").textContent = String(counts.done);
  document.getElementById("task-count").textContent = `${tasks.length} ${tasks.length === 1 ? "task" : "tasks"}`;
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const title = document.getElementById("task-title").value.trim();
  const description = document.getElementById("task-description").value;
  const status = document.getElementById("task-status").value;
  if (!title) return;
  createButton.disabled = true;
  try {
    const created = await apiRequest("/api/tasks", {
      method: "POST",
      body: JSON.stringify({ title, description, status }),
    });
    tasks.unshift(created);
    form.reset();
    clearError();
    renderBoard();
    document.getElementById("task-title").focus();
  } catch (error) {
    showError(error);
  } finally {
    createButton.disabled = false;
  }
});

async function loadTasks() {
  try {
    tasks = await apiRequest("/api/tasks");
    clearError();
    renderBoard();
  } catch (error) {
    showError(error);
  }
}

loadTasks();

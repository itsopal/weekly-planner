/* Weekly Planner - frontend logic (no framework, plain JS) */

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const DEFAULT_COLUMNS = ["Work", "Study", "Health", "Personal"];
const LOCAL_STATE_KEY = "weekly-planner-state-v1";
const PENDING_CELLS_KEY = "weekly-planner-pending-cells-v1";

let columns = [];
let entries = {}; // { "2026-09-28": { "Work": {done, note} } }
let currentMonday = mondayOf(new Date());
let pendingCells = new Map();
let syncInProgress = false;
let retryTimer = null;
let retryDelayMs = 1000;
const saveTimers = {};

/* ---------------- date helpers ---------------- */
function pad(n) { return String(n).padStart(2, "0"); }
function iso(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
function mondayOf(d) {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  const dow = (x.getDay() + 6) % 7; // Monday = 0
  x.setDate(x.getDate() - dow);
  return x;
}
function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
function fmtLong(d) { return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`; }
function weekDates() { return Array.from({ length: 7 }, (_, i) => addDays(currentMonday, i)); }
function cellKey(d, col) { return JSON.stringify([d, col]); }

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* ---------------- browser-side recovery ---------------- */
function restoreLocalState() {
  try {
    const saved = JSON.parse(localStorage.getItem(LOCAL_STATE_KEY) || "null");
    if (saved && Array.isArray(saved.columns) && saved.entries
        && typeof saved.entries === "object" && !Array.isArray(saved.entries)) {
      columns = saved.columns.filter(name => typeof name === "string");
      entries = saved.entries;
      return true;
    }
  } catch (error) {
    console.warn("Could not read the cached planner state.", error);
  }
  return false;
}

function persistLocalState() {
  try {
    localStorage.setItem(LOCAL_STATE_KEY, JSON.stringify({ columns, entries }));
    return true;
  } catch (error) {
    console.warn("Could not cache the planner state locally.", error);
    return false;
  }
}

function restorePendingCells() {
  try {
    const saved = JSON.parse(localStorage.getItem(PENDING_CELLS_KEY) || "[]");
    if (!Array.isArray(saved)) return;
    for (const item of saved) {
      if (!item || typeof item.date !== "string" || typeof item.column !== "string") continue;
      pendingCells.set(cellKey(item.date, item.column), {
        ...item,
        revision: item.revision || `${Date.now()}-${Math.random()}`,
      });
    }
  } catch (error) {
    console.warn("Could not read pending planner edits.", error);
  }
}

function persistPendingCells() {
  try {
    localStorage.setItem(PENDING_CELLS_KEY, JSON.stringify([...pendingCells.values()]));
    return true;
  } catch (error) {
    console.warn("Could not save pending edits locally.", error);
    return false;
  }
}

function overlayPendingCells() {
  for (const item of pendingCells.values()) {
    // Keep an unsynced category visible if it was removed on the server meanwhile.
    if (!columns.includes(item.column)) columns.push(item.column);
    entries[item.date] = entries[item.date] || {};
    entries[item.date][item.column] = { done: !!item.done, note: String(item.note || "") };
  }
}

function updateRetryButton() {
  const button = document.getElementById("retryBtn");
  if (!button) return;
  button.hidden = pendingCells.size === 0;
  button.textContent = navigator.onLine ? "Retry saves" : "Waiting for connection";
}

/* ---------------- API helpers ---------------- */
function setStatus(msg, cls) {
  const el = document.getElementById("saveStatus");
  if (el) {
    el.textContent = msg;
    el.className = "save-status " + (cls || "");
  }
  updateRetryButton();
}

function bounceToLogin() {
  // Session expired (or logged out in another tab) -> go to login page.
  window.location = "/login";
}

async function requestJson(url, options = {}) {
  let res;
  try {
    res = await fetch(url, { credentials: "same-origin", ...options });
  } catch (error) {
    error.status = 0;
    throw error;
  }
  if (res.status === 401) {
    bounceToLogin();
    const error = new Error("Session expired — please log in again.");
    error.status = 401;
    throw error;
  }

  let data;
  try {
    data = await res.json();
  } catch (error) {
    data = {};
  }
  if (!res.ok || data.ok === false) {
    const error = new Error(data.error || `HTTP ${res.status}`);
    error.status = res.status;
    throw error;
  }
  return data;
}

function api(url, body) {
  return requestJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
}

async function fetchState() {
  try {
    const data = await requestJson("/api/state");
    columns = Array.isArray(data.columns) && data.columns.length
      ? data.columns : [...DEFAULT_COLUMNS];
    entries = data.entries && typeof data.entries === "object" ? data.entries : {};
    overlayPendingCells();
    render();
    persistLocalState();
    if (pendingCells.size) {
      setStatus("Unsynced edits are saved on this device.", "busy");
      syncPendingCells();
    } else {
      setStatus("Saved ✓", "ok");
    }
    return true;
  } catch (error) {
    if (error.status === 401) return false;
    if (!columns.length) columns = [...DEFAULT_COLUMNS];
    overlayPendingCells();
    render();
    setStatus(
      pendingCells.size
        ? "Offline — edits are saved on this device and will sync when connected."
        : "Offline — showing the last cached planner state.",
      "busy"
    );
    persistLocalState();
    return false;
  }
}

/* ---------------- durable cell-save queue ---------------- */
function cancelRetryTimer() {
  if (retryTimer) clearTimeout(retryTimer);
  retryTimer = null;
}

function scheduleRetry() {
  if (retryTimer || !pendingCells.size) return;
  const delay = retryDelayMs;
  retryDelayMs = Math.min(retryDelayMs * 2, 60000);
  retryTimer = setTimeout(() => {
    retryTimer = null;
    syncPendingCells();
  }, delay);
}

function queueCellSave(key, col, done, note, delay = 600) {
  const queued = {
    date: key,
    column: col,
    done: !!done,
    note: String(note || ""),
    revision: `${Date.now()}-${Math.random()}`,
  };
  pendingCells.set(cellKey(key, col), queued);
  const queueSaved = persistPendingCells();
  const stateSaved = persistLocalState();
  cancelRetryTimer();
  retryDelayMs = 1000;

  if (!queueSaved || !stateSaved) {
    setStatus("Local storage is full/unavailable; keep this tab open until sync completes.", "err");
  } else if (navigator.onLine) {
    setStatus("Saved on this device — syncing…", "busy");
  } else {
    setStatus("Offline — saved on this device; will sync when connected.", "busy");
  }

  const keyForTimer = cellKey(key, col);
  clearTimeout(saveTimers[keyForTimer]);
  saveTimers[keyForTimer] = setTimeout(() => {
    delete saveTimers[keyForTimer];
    syncPendingCells();
  }, delay);
}

function commitCellLocally(item) {
  if (!item.done && !item.note.trim()) {
    if (entries[item.date]) {
      delete entries[item.date][item.column];
      if (!Object.keys(entries[item.date]).length) delete entries[item.date];
    }
  }
}

async function syncPendingCells() {
  if (syncInProgress || !pendingCells.size || !navigator.onLine) return;
  syncInProgress = true;
  let transientFailure = false;
  let blockedByServer = false;

  try {
    for (const [key, item] of [...pendingCells.entries()]) {
      const current = pendingCells.get(key);
      if (!current || current.revision !== item.revision) continue;
      try {
        await api("/api/cell", {
          date: item.date,
          column: item.column,
          done: item.done,
          note: item.note,
        });
      } catch (error) {
        if (error.status === 401) {
          blockedByServer = true;
          setStatus("Please log in again; unsynced edits remain saved on this device.", "err");
          return;
        }
        if (!error.status || error.status >= 500) {
          transientFailure = true;
          setStatus("Sync failed — your edits remain saved here; retrying…", "err");
          scheduleRetry();
        } else {
          blockedByServer = true;
          setStatus(`Server rejected an edit (${error.message}); the draft is still saved here.`, "err");
        }
        return;
      }

      const latest = pendingCells.get(key);
      if (latest && latest.revision === item.revision) {
        pendingCells.delete(key);
        commitCellLocally(item);
        persistPendingCells();
        persistLocalState();
      }
    }
  } finally {
    syncInProgress = false;
    updateRetryButton();
    if (!pendingCells.size) {
      cancelRetryTimer();
      retryDelayMs = 1000;
      persistLocalState();
      setStatus("Saved ✓", "ok");
    } else if (!transientFailure && !blockedByServer && navigator.onLine) {
      // A newer edit may have arrived while an older value was in flight.
      scheduleRetry();
    }
  }
}

function hasPendingCellsForDate(dateKey) {
  return [...pendingCells.values()].some(item => item.date === dateKey);
}

function removePendingCellsForDate(dateKey) {
  for (const [key, item] of pendingCells.entries()) {
    if (item.date === dateKey) pendingCells.delete(key);
  }
  persistPendingCells();
}

window.addEventListener("online", () => {
  retryDelayMs = 1000;
  cancelRetryTimer();
  if (pendingCells.size) syncPendingCells();
  else fetchState();
});
window.addEventListener("offline", () => {
  if (pendingCells.size) {
    setStatus("Offline — unsynced edits are saved on this device.", "busy");
  }
});

/* ---------------- streaks & progress ---------------- */
function computeStreak(col) {
  let streak = 0;
  let day = new Date();
  day.setHours(0, 0, 0, 0);
  while (true) {
    const cell = (entries[iso(day)] || {})[col];
    if (cell && cell.done) { streak++; day = addDays(day, -1); }
    else break;
  }
  return streak;
}

function updateProgress() {
  const dates = weekDates();
  const todayKey = iso(new Date());
  let weekDone = 0;
  const weekTotal = 7 * columns.length;

  dates.forEach((day, index) => {
    const key = iso(day);
    const dayData = entries[key] || {};
    const done = columns.filter(col => dayData[col] && dayData[col].done).length;
    weekDone += done;
    document.getElementById("dayprog-" + index).textContent =
      columns.length ? `(${done}/${columns.length} done)` : "(–)";
  });

  const pct = weekTotal ? Math.round(weekDone * 100 / weekTotal) : 0;
  document.getElementById("weekLabel").textContent =
    `Week of ${fmtLong(dates[0])} — ${fmtLong(dates[6])}   [${weekDone}/${weekTotal} done (${pct}%)]`;

  columns.forEach((col, index) => {
    const streak = computeStreak(col);
    document.getElementById(`streak-${index}`).textContent =
      streak === 0 ? "" : streak === 1 ? "🔥 1-day streak" : `🔥 ${streak}-day streak`;
  });
}

/* ---------------- grid rendering ---------------- */
function render() {
  const grid = document.getElementById("grid");
  if (!grid) return;
  const dates = weekDates();
  const todayKey = iso(new Date());
  grid.style.gridTemplateColumns = `170px repeat(${columns.length}, minmax(190px, 1fr))`;

  let html = '<div class="corner">Day</div>';
  columns.forEach((col, index) => {
    html += `<div class="col-header"><div>${esc(col)}</div><div class="streak" id="streak-${index}"></div></div>`;
  });

  dates.forEach((day, dayIndex) => {
    const key = iso(day);
    const isToday = key === todayKey;
    html += `<div class="day-cell${isToday ? " today" : ""}">
      <div class="day-name">${DAY_NAMES[dayIndex]}${isToday ? '<span class="today-badge">TODAY</span>' : ""}</div>
      <div class="day-date">${fmtLong(day)}</div>
      <div class="day-prog" id="dayprog-${dayIndex}"></div>
      <button class="small" data-clear="${dayIndex}">Clear day</button>
    </div>`;
    columns.forEach((col, colIndex) => {
      const cell = (entries[key] || {})[col] || { done: false, note: "" };
      html += `<div class="cell${cell.done ? " done" : ""}" id="cell-${dayIndex}-${colIndex}">
        <label class="done-row"><input type="checkbox" data-day="${dayIndex}" data-col="${colIndex}" ${cell.done ? "checked" : ""}> done</label>
        <textarea data-day="${dayIndex}" data-col="${colIndex}" rows="3" maxlength="2000" placeholder="Write a note…">${esc(cell.note || "")}</textarea>
      </div>`;
    });
  });

  grid.innerHTML = html;

  grid.querySelectorAll('input[type="checkbox"]').forEach(checkbox => {
    checkbox.addEventListener("change", () => {
      const dayIndex = +checkbox.dataset.day;
      const colIndex = +checkbox.dataset.col;
      const key = iso(weekDates()[dayIndex]);
      const col = columns[colIndex];
      entries[key] = entries[key] || {};
      const previousNote = (entries[key][col] || {}).note || "";
      entries[key][col] = { done: checkbox.checked, note: previousNote };
      document.getElementById(`cell-${dayIndex}-${colIndex}`).classList.toggle("done", checkbox.checked);
      updateProgress();
      queueCellSave(key, col, checkbox.checked, previousNote, 0);
    });
  });

  grid.querySelectorAll("textarea").forEach(textarea => {
    textarea.addEventListener("input", () => {
      const dayIndex = +textarea.dataset.day;
      const colIndex = +textarea.dataset.col;
      const key = iso(weekDates()[dayIndex]);
      const col = columns[colIndex];
      entries[key] = entries[key] || {};
      const previousDone = !!((entries[key][col] || {}).done);
      entries[key][col] = { done: previousDone, note: textarea.value };
      queueCellSave(key, col, previousDone, textarea.value, 600);
    });
  });

  grid.querySelectorAll("[data-clear]").forEach(button => {
    button.addEventListener("click", async () => {
      const dayIndex = +button.dataset.clear;
      const key = iso(weekDates()[dayIndex]);
      if (entries[key] && !confirm(`Clear all entries for ${key}?`)) return;
      setStatus("Saving…", "busy");
      try {
        await syncPendingCells();
        if (hasPendingCellsForDate(key)) {
          setStatus("Sync this day's pending edits before clearing it.", "err");
          return;
        }
        await api("/api/clear-day", { date: key });
        removePendingCellsForDate(key);
        delete entries[key];
        persistLocalState();
        render();
        setStatus("Saved ✓", "ok");
      } catch (error) {
        setStatus("Error: " + error.message, "err");
      }
    });
  });

  updateProgress();
}

/* ---------------- week navigation ---------------- */
document.getElementById("prevBtn").onclick = () => {
  currentMonday = addDays(currentMonday, -7); render();
};
document.getElementById("nextBtn").onclick = () => {
  currentMonday = addDays(currentMonday, 7); render();
};
document.getElementById("todayBtn").onclick = () => {
  currentMonday = mondayOf(new Date()); render();
};

/* ---------------- toolbar actions ---------------- */
document.getElementById("copyBtn").onclick = async () => {
  if (!confirm("Copy last week's notes into this week (unchecked)?")) return;
  setStatus("Copying…", "busy");
  try {
    const result = await api("/api/copy-prev-week", { week_start: iso(currentMonday) });
    await fetchState();
    setStatus("Saved ✓", "ok");
    alert(`Copied ${result.copied} task(s) from last week.\nSkipped ${result.skipped} day(s) that already had data.`);
  } catch (error) {
    setStatus("Error: " + error.message, "err");
  }
};

document.getElementById("exportBtn").onclick = () => {
  window.location = "/api/export";
};

document.getElementById("statsBtn").onclick = async () => {
  document.getElementById("statsModal").classList.remove("hidden");
  const body = document.getElementById("statsBody");
  body.textContent = "Loading…";
  try {
    const stats = await requestJson("/api/stats");
    body.innerHTML =
      `Days tracked: <b>${stats.days_tracked}</b><br>` +
      `Tasks done: <b>${stats.tasks_done}</b> (${stats.pct}%)<br>` +
      `Best day: <b>${stats.best_day || "–"}</b> (${stats.best_count} done)<br>` +
      `Best current streak: <b>${stats.best_streak_col ? esc(stats.best_streak_col) : "–"}</b> (${stats.best_streak} days)`;
  } catch (error) {
    body.textContent = "Error: " + error.message;
  }
};
document.getElementById("statsClose").onclick = () =>
  document.getElementById("statsModal").classList.add("hidden");

/* ---------------- backup history ---------------- */
function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

async function loadBackupHistory() {
  const body = document.getElementById("backupsBody");
  body.textContent = "Loading backups…";
  try {
    const data = await requestJson("/api/backups");
    body.replaceChildren();
    if (!data.backups.length) {
      const empty = document.createElement("p");
      empty.className = "backup-empty";
      empty.textContent = "No saved versions yet. A snapshot is created before each save.";
      body.appendChild(empty);
      return;
    }

    for (const backup of data.backups) {
      const row = document.createElement("div");
      row.className = "backup-item";
      const metadata = document.createElement("div");
      metadata.className = "backup-meta";
      const timestamp = document.createElement("div");
      timestamp.className = "backup-date";
      timestamp.textContent = new Date(backup.created_at).toLocaleString();
      const size = document.createElement("div");
      size.className = "backup-size";
      size.textContent = formatBytes(backup.size_bytes);
      metadata.append(timestamp, size);

      const restoreButton = document.createElement("button");
      restoreButton.className = "small";
      restoreButton.textContent = "Restore";
      restoreButton.addEventListener("click", async () => {
        if (pendingCells.size) {
          setStatus("Sync or resolve pending edits before restoring a backup.", "err");
          return;
        }
        if (!confirm(`Restore the planner from ${timestamp.textContent}? The current version will be backed up first.`)) return;
        restoreButton.disabled = true;
        try {
          await api("/api/backups/restore", { backup_id: backup.id });
          await fetchState();
          await loadBackupHistory();
          setStatus("Backup restored ✓", "ok");
        } catch (error) {
          setStatus("Could not restore backup: " + error.message, "err");
          restoreButton.disabled = false;
        }
      });
      row.append(metadata, restoreButton);
      body.appendChild(row);
    }
  } catch (error) {
    body.textContent = "Could not load backup history: " + error.message;
  }
}

document.getElementById("backupsBtn").onclick = () => {
  document.getElementById("backupsModal").classList.remove("hidden");
  loadBackupHistory();
};
document.getElementById("backupsClose").onclick = () =>
  document.getElementById("backupsModal").classList.add("hidden");

/* ---------------- columns modal ---------------- */
const colsModal = document.getElementById("colsModal");
document.getElementById("colsBtn").onclick = () => {
  renderColsList();
  colsModal.classList.remove("hidden");
};
document.getElementById("colsClose").onclick = () => colsModal.classList.add("hidden");
[colsModal, document.getElementById("statsModal"), document.getElementById("backupsModal")].forEach(modal => {
  modal.addEventListener("click", event => {
    if (event.target === modal) modal.classList.add("hidden");
  });
});

function renderColsList() {
  const list = document.getElementById("colsList");
  list.innerHTML = "";
  columns.forEach(name => {
    const item = document.createElement("li");
    const span = document.createElement("span");
    span.className = "name";
    span.textContent = name;
    const buttons = document.createElement("div");
    buttons.className = "row-btns";
    const renameButton = document.createElement("button");
    renameButton.className = "small";
    renameButton.textContent = "Rename";
    renameButton.onclick = async () => {
      const value = prompt(`New name for '${name}':`, name);
      if (!value || !value.trim() || value.trim() === name) return;
      try {
        await api("/api/columns/rename", { old: name, new: value.trim() });
        await fetchState();
        renderColsList();
      } catch (error) {
        alert(error.message);
      }
    };
    const deleteButton = document.createElement("button");
    deleteButton.className = "small danger";
    deleteButton.textContent = "Delete";
    deleteButton.onclick = async () => {
      if (!confirm(`Delete '${name}' and ALL its data?`)) return;
      try {
        await api("/api/columns/delete", { name });
        await fetchState();
        renderColsList();
      } catch (error) {
        alert(error.message);
      }
    };
    buttons.append(renameButton, deleteButton);
    item.append(span, buttons);
    list.appendChild(item);
  });
}

async function addColumn() {
  const input = document.getElementById("newColInput");
  const name = input.value.trim();
  if (!name) return;
  try {
    await api("/api/columns/add", { name });
    input.value = "";
    await fetchState();
    renderColsList();
  } catch (error) {
    alert(error.message);
  }
}
document.getElementById("addColBtn").onclick = addColumn;
document.getElementById("newColInput").addEventListener("keydown", event => {
  if (event.key === "Enter") addColumn();
});

/* ---------------- dark mode ---------------- */
const themeButton = document.getElementById("themeBtn");
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  themeButton.textContent = theme === "dark" ? "☀️" : "🌙";
  try {
    localStorage.setItem("planner-theme", theme);
  } catch (error) {
    // Theme preference is optional when browser storage is disabled.
  }
}
themeButton.onclick = () =>
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
let savedTheme = "light";
try {
  savedTheme = localStorage.getItem("planner-theme") || "light";
} catch (error) {
  // Use the default theme when browser storage is disabled.
}
applyTheme(savedTheme);

document.getElementById("retryBtn").onclick = () => {
  cancelRetryTimer();
  retryDelayMs = 1000;
  syncPendingCells();
};

/* ---------------- init & offline app shell ---------------- */
restoreLocalState();
restorePendingCells();
if (!columns.length) columns = [...DEFAULT_COLUMNS];
overlayPendingCells();
render();
if (pendingCells.size) {
  setStatus("Unsynced edits are saved on this device.", "busy");
}
fetchState();

if ("serviceWorker" in navigator) {
  navigator.serviceWorker.register("/service-worker.js").catch(error => {
    console.warn("Offline app-shell caching could not be enabled.", error);
  });
}

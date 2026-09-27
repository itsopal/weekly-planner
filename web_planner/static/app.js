/* Weekly Planner - frontend logic (no framework, plain JS) */

const DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

let columns = [];
let entries = {};               // { "2026-09-28": { "Work": {done, note} } }
let currentMonday = mondayOf(new Date());
const saveTimers = {};

/* ---------------- date helpers ---------------- */
function pad(n) { return String(n).padStart(2, "0"); }
function iso(d) { return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`; }
function mondayOf(d) {
  const x = new Date(d);
  x.setHours(0, 0, 0, 0);
  const dow = (x.getDay() + 6) % 7;   // Monday = 0
  x.setDate(x.getDate() - dow);
  return x;
}
function addDays(d, n) { const x = new Date(d); x.setDate(x.getDate() + n); return x; }
function fmtLong(d) { return `${MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`; }
function weekDates() { return Array.from({ length: 7 }, (_, i) => addDays(currentMonday, i)); }

function esc(s) {
  return String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
                  .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/* ---------------- api helpers ---------------- */
function setStatus(msg, cls) {
  const el = document.getElementById("saveStatus");
  el.textContent = msg;
  el.className = "save-status " + (cls || "");
}
async function api(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  const data = await res.json();
  if (!res.ok || !data.ok) throw new Error(data.error || ("HTTP " + res.status));
  return data;
}

async function fetchState() {
  const res = await fetch("/api/state");
  const data = await res.json();
  columns = data.columns;
  entries = data.entries;
  render();
}

/* ---------------- streaks & progress ---------------- */
function computeStreak(col) {
  let s = 0;
  let d = new Date();
  d.setHours(0, 0, 0, 0);
  while (true) {
    const cell = (entries[iso(d)] || {})[col];
    if (cell && cell.done) { s++; d = addDays(d, -1); }
    else break;
  }
  return s;
}

function updateProgress() {
  const dates = weekDates();
  const todayKey = iso(new Date());
  let weekDone = 0;
  const weekTotal = 7 * columns.length;

  dates.forEach((d, i) => {
    const key = iso(d);
    const dayData = entries[key] || {};
    const done = columns.filter(c => dayData[c] && dayData[c].done).length;
    weekDone += done;
    document.getElementById("dayprog-" + i).textContent =
      columns.length ? `(${done}/${columns.length} done)` : "(–)";
  });

  const pct = weekTotal ? Math.round(weekDone * 100 / weekTotal) : 0;
  document.getElementById("weekLabel").textContent =
    `Week of ${fmtLong(dates[0])} — ${fmtLong(dates[6])}   [${weekDone}/${weekTotal} done (${pct}%)]`;

  columns.forEach((c, j) => {
    const s = computeStreak(c);
    document.getElementById("streak-" + j).textContent =
      s === 0 ? "" : s === 1 ? "🔥 1-day streak" : `🔥 ${s}-day streak`;
  });
}

/* ---------------- grid rendering ---------------- */
function render() {
  const grid = document.getElementById("grid");
  const dates = weekDates();
  const todayKey = iso(new Date());

  grid.style.gridTemplateColumns = `170px repeat(${columns.length}, minmax(190px, 1fr))`;

  let html = `<div class="corner">Day</div>`;
  columns.forEach((c, j) => {
    html += `<div class="col-header"><div>${esc(c)}</div><div class="streak" id="streak-${j}"></div></div>`;
  });

  dates.forEach((d, i) => {
    const key = iso(d);
    const isToday = key === todayKey;
    html += `<div class="day-cell${isToday ? " today" : ""}">
      <div class="day-name">${DAY_NAMES[i]}${isToday ? '<span class="today-badge">TODAY</span>' : ""}</div>
      <div class="day-date">${fmtLong(d)}</div>
      <div class="day-prog" id="dayprog-${i}"></div>
      <button class="small" data-clear="${i}">Clear day</button>
    </div>`;
    columns.forEach((c, j) => {
      const cell = (entries[key] || {})[c] || { done: false, note: "" };
      html += `<div class="cell${cell.done ? " done" : ""}" id="cell-${i}-${j}">
        <label class="done-row"><input type="checkbox" data-day="${i}" data-col="${j}" ${cell.done ? "checked" : ""}> done</label>
        <textarea data-day="${i}" data-col="${j}" rows="3" maxlength="2000" placeholder="Write a note…">${esc(cell.note || "")}</textarea>
      </div>`;
    });
  });

  grid.innerHTML = html;

  // checkbox: save immediately
  grid.querySelectorAll('input[type="checkbox"]').forEach(chk => {
    chk.addEventListener("change", () => {
      const i = +chk.dataset.day, j = +chk.dataset.col;
      const key = iso(weekDates()[i]);
      const col = columns[j];
      entries[key] = entries[key] || {};
      const prevNote = (entries[key][col] || {}).note || "";
      entries[key][col] = { done: chk.checked, note: prevNote };
      document.getElementById(`cell-${i}-${j}`).classList.toggle("done", chk.checked);
      updateProgress();
      saveCell(key, col, chk.checked, prevNote);
    });
  });

  // textarea: debounced save (600ms after typing stops)
  grid.querySelectorAll("textarea").forEach(ta => {
    ta.addEventListener("input", () => {
      const i = +ta.dataset.day, j = +ta.dataset.col;
      const key = iso(weekDates()[i]);
      const col = columns[j];
      entries[key] = entries[key] || {};
      const prevDone = !!((entries[key][col] || {}).done);
      entries[key][col] = { done: prevDone, note: ta.value };
      const tkey = key + "|" + col;
      clearTimeout(saveTimers[tkey]);
      setStatus("Saving…", "busy");
      saveTimers[tkey] = setTimeout(() => saveCell(key, col, prevDone, ta.value), 600);
    });
  });

  // clear-day buttons
  grid.querySelectorAll("[data-clear]").forEach(btn => {
    btn.addEventListener("click", async () => {
      const i = +btn.dataset.clear;
      const key = iso(weekDates()[i]);
      if (entries[key] && !confirm(`Clear all entries for ${key}?`)) return;
      setStatus("Saving…", "busy");
      try {
        await api("/api/clear-day", { date: key });
        delete entries[key];
        render();
        setStatus("Saved ✓", "ok");
      } catch (e) { setStatus("Error: " + e.message, "err"); }
    });
  });

  updateProgress();
}

async function saveCell(key, col, done, note) {
  setStatus("Saving…", "busy");
  try {
    await api("/api/cell", { date: key, column: col, done, note });
    setStatus("Saved ✓", "ok");
  } catch (e) {
    setStatus("Error: " + e.message, "err");
  }
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
    const r = await api("/api/copy-prev-week", { week_start: iso(currentMonday) });
    await fetchState();
    setStatus("Saved ✓", "ok");
    alert(`Copied ${r.copied} task(s) from last week.\nSkipped ${r.skipped} day(s) that already had data.`);
  } catch (e) { setStatus("Error: " + e.message, "err"); }
};

document.getElementById("exportBtn").onclick = () => {
  window.location = "/api/export";
};

document.getElementById("statsBtn").onclick = async () => {
  document.getElementById("statsModal").classList.remove("hidden");
  const body = document.getElementById("statsBody");
  body.textContent = "Loading…";
  try {
    const r = await (await fetch("/api/stats")).json();
    body.innerHTML =
      `Days tracked: <b>${r.days_tracked}</b><br>` +
      `Tasks done: <b>${r.tasks_done}</b> (${r.pct}%)<br>` +
      `Best day: <b>${r.best_day || "–"}</b> (${r.best_count} done)<br>` +
      `Best current streak: <b>${r.best_streak_col ? esc(r.best_streak_col) : "–"}</b> (${r.best_streak} days)`;
  } catch (e) { body.textContent = "Error: " + e.message; }
};
document.getElementById("statsClose").onclick = () =>
  document.getElementById("statsModal").classList.add("hidden");

/* ---------------- columns modal ---------------- */
const colsModal = document.getElementById("colsModal");
document.getElementById("colsBtn").onclick = () => {
  renderColsList();
  colsModal.classList.remove("hidden");
};
document.getElementById("colsClose").onclick = () => colsModal.classList.add("hidden");
[colsModal, document.getElementById("statsModal")].forEach(m => {
  m.addEventListener("click", e => { if (e.target === m) m.classList.add("hidden"); });
});

function renderColsList() {
  const ul = document.getElementById("colsList");
  ul.innerHTML = "";
  columns.forEach(name => {
    const li = document.createElement("li");
    const span = document.createElement("span");
    span.className = "name";
    span.textContent = name;
    const btns = document.createElement("div");
    btns.className = "row-btns";
    const rn = document.createElement("button");
    rn.className = "small"; rn.textContent = "Rename";
    rn.onclick = async () => {
      const v = prompt(`New name for '${name}':`, name);
      if (!v || !v.trim() || v.trim() === name) return;
      try {
        await api("/api/columns/rename", { old: name, new: v.trim() });
        await fetchState(); renderColsList();
      } catch (e) { alert(e.message); }
    };
    const del = document.createElement("button");
    del.className = "small danger"; del.textContent = "Delete";
    del.onclick = async () => {
      if (!confirm(`Delete '${name}' and ALL its data?`)) return;
      try {
        await api("/api/columns/delete", { name });
        await fetchState(); renderColsList();
      } catch (e) { alert(e.message); }
    };
    btns.append(rn, del);
    li.append(span, btns);
    ul.appendChild(li);
  });
}

async function addColumn() {
  const inp = document.getElementById("newColInput");
  const name = inp.value.trim();
  if (!name) return;
  try {
    await api("/api/columns/add", { name });
    inp.value = "";
    await fetchState(); renderColsList();
  } catch (e) { alert(e.message); }
}
document.getElementById("addColBtn").onclick = addColumn;
document.getElementById("newColInput").addEventListener("keydown", e => {
  if (e.key === "Enter") addColumn();
});

/* ---------------- dark mode ---------------- */
const themeBtn = document.getElementById("themeBtn");
function applyTheme(t) {
  document.documentElement.dataset.theme = t;
  themeBtn.textContent = t === "dark" ? "☀️" : "🌙";
  localStorage.setItem("planner-theme", t);
}
themeBtn.onclick = () =>
  applyTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
applyTheme(localStorage.getItem("planner-theme") || "light");

/* ---------------- init ---------------- */
fetchState().catch(() => {
  document.getElementById("grid").innerHTML =
    "<p style='grid-column:1/-1'>Could not reach the server. Is Flask running?</p>";
});

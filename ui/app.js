// LocalNotes UI. Talks to Python through window.pywebview.api.*;
// Python pushes live updates by calling window.onEvent({type, ...}).
"use strict";
const $ = (id) => document.getElementById(id);
const ic = (name) => `<svg class="i"><use href="#i-${name}"/></svg>`;
let api;

const S = {
  settings: {}, user: "",
  currentId: null, meeting: null,
  recordingId: null, recStarted: null,
  pausedAt: null, pausedTotal: 0,      // pause bookkeeping, epoch seconds
  templates: [],
  followup: { mid: null, streaming: false, buffer: "" },
  detected: null,              // [app, title]
  view: "mine",                // mine | ai
  drawerTab: "transcript",
  drawerOpen: true,
  aiBuffer: "", streaming: false, notesProgress: "", notesError: "",
  pipeline: {},                // meeting_id -> stage
  refineText: {},              // meeting_id -> progress text
  speakerText: {},             // meeting_id -> speaker-identification progress
  chats: {},                   // meeting_id -> [{role, content}]
  asking: false,
  ollama: {},
  homeVisible: true,
};

// ---------------------------------------------------------------- helpers
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const pad = (n) => String(n).padStart(2, "0");
const clock = (sec) => { sec = Math.max(0, Math.floor(sec)); const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s = sec % 60; return h ? `${h}:${pad(m)}:${pad(s)}` : `${pad(m)}:${pad(s)}`; };
const debounce = (fn, ms) => { let h; return (...a) => { clearTimeout(h); h = setTimeout(() => fn(...a), ms); }; };
const timeOf = (t) => new Date(t * 1000).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
const dayOf = (t) => new Date(t * 1000).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
const minutes = (m) => Math.max(1, Math.round(((m.ended_at || Date.now() / 1000) - m.started_at) / 60));
const durLabel = (mins) => mins >= 60 ? `${Math.floor(mins / 60)} h ${mins % 60 ? (mins % 60) + " min" : ""}`.trim() : `${mins} min`;
const APP_NAMES = { zoom: "Zoom", teams: "Teams", meet: "Google Meet", webex: "Webex", slack: "Slack", discord: "Discord", manual: "" };
function appChip(app, live) {
  const cls = live ? "live" : (["zoom", "teams", "meet"].includes(app) ? app : "");
  const name = live ? "wave" : (app && app !== "manual" ? "video" : "pen");
  return `<div class="app-chip ${cls}">${ic(name)}</div>`;
}
function dayGroup(t) {
  const d = new Date(t * 1000), now = new Date();
  const start = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  if (t >= start) return "Today";
  if (t >= start - 86400) return "Yesterday";
  if (t >= start - 6 * 86400) return "Previous 7 days";
  if (d.getFullYear() === now.getFullYear()) return d.toLocaleDateString(undefined, { month: "long" });
  return d.toLocaleDateString(undefined, { month: "long", year: "numeric" });
}
function autosize(el) { el.style.height = "auto"; el.style.height = el.scrollHeight + "px"; }
async function copyText(text) {
  try { await navigator.clipboard.writeText(text); return true; }
  catch {
    const t = document.createElement("textarea"); t.value = text; document.body.appendChild(t); t.select();
    const ok = document.execCommand("copy"); t.remove(); return ok;
  }
}

// ---------------------------------------------------------------- toasts
const toasts = new Map();
function toast(text, { kind = "ok", timeout = 3200, id, action } = {}) {
  if (id && toasts.has(id)) {
    const el = toasts.get(id);
    el.querySelector(".t-text").textContent = text;
    el.className = `toast ${kind}`;
    el.querySelector(".t-ic").innerHTML = kind === "progress" ? '<span class="spinner"></span>' : ic(kind === "error" ? "alert" : "check");
    clearTimeout(el._t);
    if (timeout) el._t = setTimeout(() => dismissToast(id), timeout);
    return;
  }
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.innerHTML = `<span class="t-ic">${kind === "progress" ? '<span class="spinner"></span>' : ic(kind === "error" ? "alert" : "check")}</span><span class="t-text"></span>`;
  el.querySelector(".t-text").textContent = text;
  const key = id || Symbol();
  if (action) {
    const b = document.createElement("button");
    b.className = "t-action"; b.textContent = action.label;
    b.onclick = () => { action.run(); dismissToast(key); };
    el.appendChild(b);
  }
  $("toasts").appendChild(el);
  toasts.set(key, el);
  if (timeout) el._t = setTimeout(() => dismissToast(key), timeout);
}
function dismissToast(id) {
  const el = toasts.get(id);
  if (!el) return;
  toasts.delete(id);
  el.classList.add("out");
  setTimeout(() => el.remove(), 260);
}

// ---------------------------------------------------------------- markdown
const TASK_RE = /^\s*[-*]\s+\[( |x|X)\]\s+(.*)$/;
function inline(s) {
  return esc(s)
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>")
    .replace(/(^|\W)_([^_]+)_(?=\W|$)/g, "$1<em>$2</em>");
}
// Renders notes Markdown. opts.notes: tidy the LLM's boilerplate and make tasks interactive.
function renderMarkdown(md, opts = {}) {
  let lines = md.replace(/\r/g, "").split("\n");
  let leadNext = false;
  if (opts.notes) {
    const first = lines.findIndex((l) => l.trim());
    if (first >= 0) {
      const h = lines[first].match(/^#{1,4}\s+(.*)/);
      if (h && (/meeting notes|^notes$/i.test(h[1].trim()) || h[1].trim().toLowerCase() === (opts.title || "").toLowerCase())) lines.splice(first, 1);
    }
  }
  const out = [];
  let list = null, taskIdx = 0, sawLead = false;
  const close = () => { if (list) { out.push(`</${list}>`); list = null; } };
  for (const raw of lines) {
    const line = raw.trimEnd();
    let m;
    if (!line.trim()) { close(); continue; }
    if ((m = line.match(/^(#{1,4})\s+(.*)/))) {
      close();
      if (opts.notes && !sawLead && /^(summary|overview|tl;?dr)$/i.test(m[2].trim())) { leadNext = true; continue; }
      const lvl = Math.min(3, m[1].length === 4 ? 3 : m[1].length);
      out.push(`<h${lvl}>${inline(m[2])}</h${lvl}>`);
      continue;
    }
    if (/^\s*(-{3,}|\*{3,})\s*$/.test(line)) { close(); out.push("<hr>"); continue; }
    if ((m = line.match(TASK_RE))) {
      if (list !== "ul") { close(); out.push("<ul>"); list = "ul"; }
      const done = m[1].trim() !== "";
      out.push(`<li class="task ${done ? "done" : ""}"><input type="checkbox" class="check" data-task="${taskIdx++}" ${done ? "checked" : ""} ${opts.interactive ? "" : "disabled"}><span>${inline(m[2])}</span></li>`);
      continue;
    }
    if ((m = line.match(/^\s*[-*•]\s+(.*)/))) {
      if (leadNext && !sawLead) { close(); out.push(`<p class="lead">${inline(m[1])}</p>`); sawLead = true; leadNext = false; continue; }
      if (list !== "ul") { close(); out.push("<ul>"); list = "ul"; }
      out.push(`<li>${inline(m[1])}</li>`); continue;
    }
    if ((m = line.match(/^\s*\d+[.)]\s+(.*)/))) {
      if (list !== "ol") { close(); out.push("<ol>"); list = "ol"; }
      out.push(`<li>${inline(m[1])}</li>`); continue;
    }
    if ((m = line.match(/^>\s?(.*)/))) { close(); out.push(`<blockquote>${inline(m[1])}</blockquote>`); continue; }
    close();
    let text = line.trim();
    if (opts.notes && !sawLead && (leadNext || out.length === 0)) {
      text = text.replace(/^\*{0,2}(one-line )?summary\*{0,2}\s*:\s*\*{0,2}/i, "");
      out.push(`<p class="lead">${inline(text)}</p>`); sawLead = true; leadNext = false; continue;
    }
    out.push(`<p>${inline(text)}</p>`);
  }
  close();
  return out.join("");
}

// ---------------------------------------------------------------- theme
function applyStyle(style) {
  document.documentElement.dataset.style = style === "classic" ? "classic" : "glass";
}
// Glass sheen follows the cursor across cards and buttons.
document.addEventListener("pointermove", (e) => {
  const el = e.target.closest?.(".recent, .stat, .hero, .task-list, .btn, .m-item, .task, .search-trigger");
  if (!el) return;
  const r = el.getBoundingClientRect();
  el.style.setProperty("--mx", `${e.clientX - r.left}px`);
  el.style.setProperty("--my", `${e.clientY - r.top}px`);
}, { passive: true });

function applyTheme(theme) {
  if (theme === "light" || theme === "dark") document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  const dark = theme === "dark" || (theme !== "light" && matchMedia("(prefers-color-scheme: dark)").matches);
  $("btn-theme").innerHTML = ic(dark ? "sun" : "moon");
}
$("btn-theme").addEventListener("click", async () => {
  const dark = document.documentElement.dataset.theme === "dark" ||
    (!document.documentElement.dataset.theme && matchMedia("(prefers-color-scheme: dark)").matches);
  S.settings = await api.save_settings({ theme: dark ? "light" : "dark" });
  applyTheme(S.settings.theme);
});

// ---------------------------------------------------------------- segmented controls
function setSeg(seg, attr, value) {
  seg.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset[attr] === value));
  const btn = seg.querySelector(`button[data-${attr}="${value}"]`);
  const thumb = seg.querySelector(".thumb");
  if (btn && thumb) { thumb.style.left = btn.offsetLeft + "px"; thumb.style.width = btn.offsetWidth + "px"; }
}

// ---------------------------------------------------------------- sidebar
async function loadList() {
  const rows = await api.list_meetings();
  if (!rows.length) {
    $("list").innerHTML = `<div class="list-empty">Your meetings will appear here.</div>`;
    return;
  }
  let html = "", group = "";
  for (const m of rows) {
    const g = dayGroup(m.started_at);
    if (g !== group) { html += `<div class="group-label">${esc(g)}</div>`; group = g; }
    const live = m.id === S.recordingId;
    const sub = live ? `<span style="color:var(--live);font-weight:600">Recording…</span>`
      : `${timeOf(m.started_at)}${m.ended_at ? " · " + durLabel(minutes(m)) : ""}${APP_NAMES[m.app] ? " · " + APP_NAMES[m.app] : ""}`;
    html += `<div class="m-item ${m.id === S.currentId ? "active" : ""}" data-id="${m.id}">${appChip(m.app, live)}
      <div class="m-body"><div class="m-title">${esc(m.title)}</div><div class="m-sub">${sub}</div></div></div>`;
  }
  $("list").innerHTML = html;
}
$("list").addEventListener("click", (e) => {
  const it = e.target.closest(".m-item");
  if (it) openMeeting(Number(it.dataset.id));
});
$("nav-home").addEventListener("click", () => showHome());

function renderDetectStatus() {
  const dot = document.querySelector("#detect-status .pulse");
  let text, cls = "";
  if (S.recordingId) { text = "Recording"; cls = "live"; }
  else if (!S.settings.detect_meetings) { text = "Meeting detection off"; cls = "off"; }
  else if (S.detected) text = `${S.detected[1]} detected`;
  else text = "Listening for meetings";
  dot.className = `pulse ${cls}`;
  $("detect-text").textContent = text;
}
$("detect-status").addEventListener("click", async () => {
  S.settings = await api.save_settings({ detect_meetings: !S.settings.detect_meetings });
  toast(S.settings.detect_meetings ? "Meeting detection is on" : "Meeting detection is off");
  renderDetectStatus();
  if (S.homeVisible) renderHome();
});

// ---------------------------------------------------------------- home
function greeting() {
  const h = new Date().getHours();
  const part = h < 5 ? "Working late" : h < 12 ? "Good morning" : h < 17 ? "Good afternoon" : h < 22 ? "Good evening" : "Working late";
  return S.user ? `${part}, ${S.user}` : part;
}
function heroHtml() {
  const apps = `<div class="apps">${appChip("zoom")}${appChip("teams")}${appChip("meet")}</div>`;
  if (S.recordingId) return `<div class="hero recording"><div class="orb">${ic("wave")}</div>
    <div><h2>Recording in progress</h2><p>Your live note is capturing the call.</p></div>
    <div class="hero-actions"><button class="btn primary" data-act="open-live">${ic("file")}Open live note</button></div></div>`;
  if (S.detected) return `<div class="hero detected"><div class="orb">${ic("video")}</div>
    <div><h2>${esc(S.detected[1])} detected</h2><p>Want LocalNotes to take notes for this call?</p></div>
    <div class="hero-actions"><button class="btn" data-act="dismiss">Not now</button><button class="btn primary lg" data-act="start-detected">${ic("wave")}Start notes</button></div></div>`;
  if (!S.settings.detect_meetings) return `<div class="hero idle"><div class="orb">${ic("video")}</div>
    <div><h2>Meeting detection is off</h2><p>Turn it on and LocalNotes will offer to take notes when a call starts.</p></div>
    <div class="hero-actions"><button class="btn primary" data-act="detect-on">Turn on</button></div></div>`;
  return `<div class="hero"><div class="orb">${ic("video")}</div>
    <div><h2>Listening for your next call</h2><p>Join a Zoom, Teams or Meet call and LocalNotes will be ready — or press
      <kbd>${esc((S.settings.hotkey_record || "").toUpperCase())}</kbd> in any app to start recording.</p>${apps}</div>
    <div class="hero-actions"><button class="btn primary lg" data-act="new">${ic("wave")}Start now</button></div></div>`;
}
async function renderHome() {
  const [home, st] = await Promise.all([api.get_home(), api.get_state()]);
  S.ollama = st.ollama;
  const s = home.stats;
  $("nav-tasks").textContent = s.open_tasks || "";
  const date = new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
  let sys = "";
  if (!S.ollama.running) sys = `<div class="sys-check">${ic("alert")}<span>Ollama isn't running, so AI notes can't be written. Open the <b>Ollama</b> app from the Start menu.</span></div>`;
  else if (!S.ollama.model_ready) sys = `<div class="sys-check">${ic("alert")}<span>The AI model isn't downloaded yet. Run <code>ollama pull ${esc(S.settings.ollama_model)}</code> in a terminal.</span></div>`;

  const hours = Math.floor(s.week_minutes / 60), mins = s.week_minutes % 60;
  const stats = `<div class="stats">
    <div class="stat"><div class="k">Meetings this week</div><div class="v">${s.week_meetings}</div></div>
    <div class="stat"><div class="k">Time captured</div><div class="v">${hours ? `${hours}<small>h</small> ` : ""}${mins}<small>min</small></div></div>
    <div class="stat"><div class="k">Open action items</div><div class="v">${s.open_tasks}</div></div></div>`;

  let body;
  if (!s.total) {
    body = `<div class="section-title">How it works</div><div class="onboard">
      <div class="step-card"><div class="n">1</div><b>Join a call</b><p>LocalNotes notices Zoom, Teams or Meet and offers to take notes. Or press <kbd>Ctrl N</kbd>.</p></div>
      <div class="step-card"><div class="n">2</div><b>Jot rough notes</b><p>Type the few things that matter to you. The transcript captures the rest.</p></div>
      <div class="step-card"><div class="n">3</div><b>Get clean notes</b><p>When the call ends, local AI writes notes and action items — nothing leaves this PC.</p></div></div>`;
  } else {
    const tasks = home.tasks.length ? home.tasks.map((t) => `
      <div class="task" data-mid="${t.meeting_id}" data-idx="${t.index}">
        <input type="checkbox" class="check">
        <div class="t-text"><div class="t-main">${inline(t.text)}</div><div class="t-meta" data-open="${t.meeting_id}">${esc(t.meeting)} · ${dayOf(t.started_at)}</div></div>
      </div>`).join("") : `<div class="empty-card">${ic("check")}<br>No open action items. Nice.</div>`;
    const recent = home.recent.map((m) => `
      <div class="card recent" data-open="${m.id}">${appChip(m.app, m.live)}
        <div style="min-width:0"><div class="r-title">${esc(m.title)}</div>
        <div class="r-sub">${dayOf(m.started_at)} · ${m.live ? "recording now" : timeOf(m.started_at) + (m.ended_at ? " · " + durLabel(minutes(m)) : "")}</div>
        ${m.summary ? `<div class="r-sum">${inline(m.summary)}</div>` : ""}</div></div>`).join("");
    body = `<div class="home-grid">
      <div><div class="section-title">Action items <span class="count">${s.open_tasks || ""}</span></div><div class="card task-list">${tasks}</div></div>
      <div><div class="section-title">Recent notes</div><div class="recent-list">${recent}</div></div></div>`;
  }
  $("view-home").innerHTML = `<div class="home">
    <div class="home-head"><div><h1>${esc(greeting())}</h1><div class="date">${date}</div></div></div>
    ${heroHtml()}${sys}${stats}${body}</div>`;
}
$("view-home").addEventListener("click", async (e) => {
  const act = e.target.closest("[data-act]")?.dataset.act;
  if (act === "new") return startRecording();
  if (act === "start-detected") { const id = await api.start_from_detection(); if (id) onRecordingStarted(id); return; }
  if (act === "dismiss") { await api.dismiss_detection(); return; }
  if (act === "open-live") return openMeeting(S.recordingId);
  if (act === "detect-on") { $("detect-status").click(); return; }
  const open = e.target.closest("[data-open]");
  if (open && !e.target.classList.contains("check")) return openMeeting(Number(open.dataset.open));
});
$("view-home").addEventListener("change", async (e) => {
  if (!e.target.classList.contains("check")) return;
  const row = e.target.closest(".task");
  await api.toggle_task(Number(row.dataset.mid), Number(row.dataset.idx), e.target.checked);
  row.classList.toggle("done", e.target.checked);
  if (e.target.checked) { toast("Action item done"); setTimeout(() => S.homeVisible && renderHome(), 900); }
});

function showHome() {
  S.homeVisible = true; S.currentId = null; S.meeting = null;
  $("view-meeting").hidden = true;
  $("view-home").hidden = false;
  $("nav-home").classList.add("active");
  document.querySelectorAll(".m-item.active").forEach((x) => x.classList.remove("active"));
  renderBanner();
  renderHome();
}

// ---------------------------------------------------------------- meeting view
async function openMeeting(id, { view } = {}) {
  const m = await api.get_meeting(id);
  if (!m) { toast("That meeting no longer exists", { kind: "error" }); return showHome(); }
  const switching = S.currentId !== id;
  S.currentId = id; S.meeting = m; S.homeVisible = false;
  S.notesError = "";
  if (switching) { S.streaming = false; S.aiBuffer = ""; }
  if (!S.streaming) S.aiBuffer = m.ai_notes;
  if (m.pipeline) S.pipeline[id] = m.pipeline;
  if (switching) speakerPalette = new Map((m.speakers || []).filter((s) => !["Me", "Them"].includes(s.name))
    .map((s, i) => [s.name, SPEAKER_COLORS[i % SPEAKER_COLORS.length]]));
  $("view-home").hidden = true;
  $("view-meeting").hidden = false;
  $("nav-home").classList.remove("active");

  $("title").value = m.title; autosize($("title"));
  $("editor").value = m.user_notes; autosize($("editor"));
  $("save-ind").textContent = "";
  renderChips();
  renderStepper();
  setView(view || (m.live ? "mine" : (m.ai_notes || S.streaming ? "ai" : "mine")));
  renderAI();
  renderTranscript();
  renderChat();
  updateToolbar();
  setDrawer(S.drawerOpen);
  document.querySelectorAll(".m-item").forEach((x) => x.classList.toggle("active", Number(x.dataset.id) === id));
  renderBanner();
  if (switching) $("doc-scroll").scrollTop = 0;
}

function renderChips() {
  const m = S.meeting; if (!m) return;
  const chips = [`<span class="chip">${ic("cal")}${dayOf(m.started_at)}, ${timeOf(m.started_at)}</span>`];
  if (m.id === S.recordingId) chips.push(`<span class="chip live ${S.pausedAt ? "paused" : ""}" id="live-chip"><span class="dot"></span>${S.pausedAt ? "Paused · " : ""}${clock(elapsed())}</span>`);
  else if (m.ended_at) chips.push(`<span class="chip">${ic("clock")}${durLabel(minutes(m))}</span>`);
  if (APP_NAMES[m.app]) chips.push(`<span class="chip">${ic("video")}${APP_NAMES[m.app]}</span>`);
  if (m.speakers?.length) {
    const names = m.speakers.map((s) => s.name);
    const shown = names.slice(0, 4).join(", ") + (names.length > 4 ? ` +${names.length - 4}` : "");
    chips.push(`<button class="chip clickable" data-act="people" title="People — click to rename">${ic("users")}${esc(shown)}</button>`);
  } else {
    const speakers = new Set((m.segments || []).map((s) => s.speaker));
    if (speakers.size) chips.push(`<span class="chip">${ic("users")}${esc([...speakers].sort().join(" + "))}</span>`);
  }
  chips.push(templateChip(m));
  $("chips").innerHTML = chips.join("");
}

function setView(v) {
  S.view = v;
  setSeg($("view-seg"), "v", v);
  requestAnimationFrame(() => setSeg($("view-seg"), "v", S.view));
  $("pane-mine").hidden = v !== "mine";
  $("pane-ai").hidden = v !== "ai";
  if (v === "mine") requestAnimationFrame(() => autosize($("editor")));
  $("ai-badge").hidden = true;
}
$("view-seg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setView(b.dataset.v); });

function updateToolbar() {
  const m = S.meeting; if (!m) return;
  const live = m.id === S.recordingId;
  const busy = S.streaming || ["notes", "renotes", "transcribing"].includes(S.pipeline[m.id]);
  const g = $("btn-generate");
  g.hidden = live;
  g.disabled = busy;
  g.querySelector("span").textContent = busy ? "Writing…" : (m.ai_notes ? "Regenerate" : "Generate notes");
  g.classList.toggle("primary", !m.ai_notes && !busy);
  $("btn-followup").hidden = live;
  $("tab-transcript").classList.toggle("editable", canEditTranscript());
}

// ---------------------------------------------------------------- templates
function templateChip(m) {
  const tpl = S.templates.find((t) => t.id === m.template_id) || S.templates.find((t) => t.id === "general");
  return `<button class="chip clickable tpl-chip" data-act="template" title="Note template — click to change">${ic(tpl?.icon || "file")}${esc(tpl?.name || "Template")}<svg class="i chev"><use href="#i-chev"/></svg></button>`;
}
function openTemplateMenu(anchor) {
  const m = S.meeting; if (!m) return;
  const menu = $("menu");
  menu.innerHTML = `<div class="people-title">Note template</div>` + S.templates.map((t) => `
      <button class="tpl-item" data-tpl="${esc(t.id)}">${ic(t.icon)}
        <span class="t-body"><span class="t-name">${esc(t.name)}</span><span class="t-desc">${esc(t.description)}</span></span>
        <span class="t-check">${t.id === (m.template_id || "general") ? ic("check") : ""}</span></button>`).join("") +
    `<hr><button data-manage>${ic("gear")}Manage templates…</button>`;
  menu.hidden = false;
  const r = anchor.getBoundingClientRect();
  menu.style.top = `${r.bottom + 6}px`;
  menu.style.left = `${Math.max(12, Math.min(window.innerWidth - menu.offsetWidth - 12, r.right - menu.offsetWidth))}px`;
  menu.onclick = async (ev) => {
    if (ev.target.closest("[data-manage]")) { closeMenu(); return openSettings("tpl-new"); }
    const b = ev.target.closest("[data-tpl]"); if (!b) return;
    closeMenu();
    const tid = b.dataset.tpl;
    if (tid === m.template_id) return;
    await api.set_meeting_template(m.id, tid);
    m.template_id = tid; renderChips();
    const name = S.templates.find((t) => t.id === tid)?.name;
    if (m.id !== S.recordingId && ((m.segments || []).length || m.user_notes.trim())) {
      toast(`Rewriting notes as “${name}”…`, { kind: "progress", id: "tpl", timeout: 2500 });
      generate();
    } else toast(`Notes will use the “${name}” template`);
  };
}

// title + notes editing
const saveTitle = debounce(async () => {
  if (!S.meeting) return;
  const t = $("title").value.trim() || "Untitled meeting";
  await api.update_meeting(S.meeting.id, { title: t });
  S.meeting.title = t;
  loadList();
}, 500);
$("title").addEventListener("input", () => { autosize($("title")); saveTitle(); });
$("title").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); setView("mine"); $("editor").focus(); } });

const saveNotes = debounce(async (id, text) => {
  await api.update_meeting(id, { user_notes: text });
  if (S.meeting && S.meeting.id === id) S.meeting.user_notes = text;
  $("save-ind").textContent = "Saved";
  setTimeout(() => { if ($("save-ind").textContent === "Saved") $("save-ind").textContent = ""; }, 1500);
}, 450);
$("editor").addEventListener("input", () => {
  autosize($("editor"));
  $("save-ind").textContent = "Saving…";
  saveNotes(S.currentId, $("editor").value);
});
// Bullet continuation and indentation, like a real notes editor.
$("editor").addEventListener("keydown", (e) => {
  const ta = e.target, { selectionStart: a, value } = ta;
  const lineStart = value.lastIndexOf("\n", a - 1) + 1;
  const line = value.slice(lineStart, a);
  if (e.key === "Enter" && !e.shiftKey) {
    const m = line.match(/^(\s*)([-*•]|\d+[.)]|- \[ \])\s+/);
    if (!m) return;
    e.preventDefault();
    if (line.trim() === m[0].trim()) {                   // empty bullet: end the list
      ta.setRangeText("", lineStart, a, "end");
    } else {
      let marker = m[2];
      const num = marker.match(/^(\d+)([.)])$/);
      if (num) marker = `${Number(num[1]) + 1}${num[2]}`;
      ta.setRangeText(`\n${m[1]}${marker} `, a, ta.selectionEnd, "end");
    }
    ta.dispatchEvent(new Event("input"));
  } else if (e.key === "Tab") {
    e.preventDefault();
    if (e.shiftKey) {
      if (value.slice(lineStart, lineStart + 2) === "  ") { ta.setRangeText("", lineStart, lineStart + 2, "end"); }
    } else {
      ta.setRangeText("  ", lineStart, lineStart, "end");
      ta.selectionStart = ta.selectionEnd = a + 2;
    }
    ta.dispatchEvent(new Event("input"));
  }
});

// AI notes pane
let aiRaf = 0;
function renderAI() {
  if (aiRaf) return;
  aiRaf = requestAnimationFrame(() => { aiRaf = 0; drawAI(); });
}
function drawAI() {
  const m = S.meeting, pane = $("pane-ai");
  if (!m) return;
  const live = m.id === S.recordingId;
  if (S.streaming && !S.aiBuffer) {
    pane.innerHTML = `<div class="skeleton"><div class="sk-label"><span class="spinner" style="width:14px;height:14px;border-radius:50%;border:2px solid var(--accent);border-top-color:transparent;animation:spin .8s linear infinite"></span>${esc(S.notesProgress || "Reading the transcript…")}</div>
      <div class="sk" style="width:92%"></div><div class="sk" style="width:76%"></div><div class="sk h"></div><div class="sk" style="width:88%"></div><div class="sk" style="width:64%"></div><div class="sk" style="width:80%"></div></div>`;
    return;
  }
  if (S.streaming) {
    pane.innerHTML = `<div class="md caret-host">${renderMarkdown(S.aiBuffer, { notes: true, title: m.title })}</div>`;
    const last = pane.querySelector(".md > :last-child");
    (last?.lastElementChild?.tagName === "LI" ? last.lastElementChild : last)?.classList.add("caret");
    return;
  }
  if (S.notesError) {
    pane.innerHTML = `<div class="error-box">${esc(S.notesError)}</div><p><button class="btn sm" data-act="regen">${ic("refresh")}Try again</button></p>`;
    return;
  }
  if (m.ai_notes) {
    pane.innerHTML = `<div class="md">${renderMarkdown(m.ai_notes, { notes: true, title: m.title, interactive: true })}</div>`;
    return;
  }
  const hasContent = (m.segments || []).length || m.user_notes.trim();
  pane.innerHTML = live
    ? `<div class="empty-doc"><div class="big-ic">${ic("sparkle")}</div><h3>Notes appear after the call</h3><p>Keep jotting in <b>My notes</b>. When you stop, AI combines them with the transcript.</p></div>`
    : hasContent
      ? `<div class="empty-doc"><div class="big-ic">${ic("sparkle")}</div><h3>Turn this into clean notes</h3><p>Local AI will combine your notes and the transcript.</p><button class="btn primary lg" data-act="regen">${ic("sparkle")}Generate notes</button></div>`
      : `<div class="empty-doc"><div class="big-ic">${ic("mic")}</div><h3>Nothing captured</h3><p>This meeting has no transcript or notes.</p></div>`;
}
$("pane-ai").addEventListener("click", (e) => { if (e.target.closest("[data-act=regen]")) generate(); });
$("pane-ai").addEventListener("change", async (e) => {
  const cb = e.target.closest("[data-task]"); if (!cb || !S.meeting) return;
  const notes = await api.toggle_task(S.meeting.id, Number(cb.dataset.task), cb.checked);
  if (notes !== null) S.meeting.ai_notes = notes;
  cb.closest("li").classList.toggle("done", cb.checked);
});
async function generate() {
  if (!S.meeting) return;
  await api.update_meeting(S.meeting.id, { user_notes: $("editor").value });
  S.notesError = "";
  const ok = await api.generate_notes(S.meeting.id);
  if (ok) setView("ai");
}
$("btn-generate").addEventListener("click", generate);

// pipeline stepper
function renderStepper() {
  const m = S.meeting, host = $("stepper");
  const stage = m && S.pipeline[m.id];
  if (!stage) { host.innerHTML = ""; return; }
  const steps = [["transcribing", "Transcript"]];
  if (S.settings.identify_speakers) steps.push(["speakers", "Speakers"]);
  steps.push(["notes", "Writing notes"]);
  if (S.settings.refine_model !== "off") steps.push(["refining", "Accuracy pass"], ["renotes", "Final notes"]);
  const cur = stage === "done" ? steps.length : steps.findIndex(([k]) => k === stage);
  host.innerHTML = `<div class="stepper">${steps.map(([k, label], i) => {
    const st = i < cur ? "done" : i === cur ? "active" : "";
    const subText = k === "refining" ? S.refineText[m.id] : k === "speakers" ? S.speakerText[m.id] : "";
    const sub = st === "active" && subText ? ` <span class="s-sub">${esc(subText)}</span>` : "";
    return `${i ? `<span class="step-line ${i <= cur ? "done" : ""}"></span>` : ""}<span class="step ${st}"><span class="s-ic"></span>${label}${sub}</span>`;
  }).join("")}</div>`;
}

// ---------------------------------------------------------------- drawer: transcript
function setDrawer(open) {
  S.drawerOpen = open;
  try { localStorage.setItem("drawerOpen", open ? "1" : "0"); } catch {}
  $("drawer").classList.toggle("closed", !open);
  $("btn-drawer").classList.toggle("active", open);
  if (open) requestAnimationFrame(() => setSeg($("drawer-seg"), "t", S.drawerTab));
}
$("btn-drawer").addEventListener("click", () => setDrawer(!S.drawerOpen));
$("btn-drawer-close").addEventListener("click", () => setDrawer(false));
function setDrawerTab(t) {
  S.drawerTab = t;
  setSeg($("drawer-seg"), "t", t);
  $("tab-transcript").hidden = t !== "transcript";
  $("jump-live").hidden = true;
  $("tab-ask").hidden = t !== "ask";
  if (t === "ask") setTimeout(() => $("ask-input").focus(), 50);
}
$("drawer-seg").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) setDrawerTab(b.dataset.t); });

let lastSeg = null;
const SPEAKER_COLORS = ["#2d6ae3", "#c2410c", "#7c3aed", "#0f766e", "#be185d", "#a16207", "#4d7c0f", "#0369a1"];
// Colours are handed out in order of appearance within the open meeting, so its speakers never share one.
let speakerPalette = new Map();
function speakerColor(name) {
  if (name === "Me") return "var(--accent)";
  if (name === "Them") return "var(--them)";
  if (!speakerPalette.has(name)) speakerPalette.set(name, SPEAKER_COLORS[speakerPalette.size % SPEAKER_COLORS.length]);
  return speakerPalette.get(name);
}
function renderTranscript() {
  const box = $("tab-transcript"), m = S.meeting;
  lastSeg = null;
  box.classList.toggle("refining", !!(m && S.pipeline[m.id] === "refining"));
  const segs = m?.segments || [];
  if (!segs.length) {
    box.innerHTML = m && m.id === S.recordingId
      ? `<div class="drawer-empty"><div class="listening"><i></i><i></i><i></i><i></i><i></i></div>Listening…<br>The transcript appears a few seconds after people speak.</div>`
      : `<div class="drawer-empty">No transcript for this meeting.</div>`;
    return;
  }
  box.innerHTML = "";
  segs.forEach((s) => appendSegment(s, false));
  if (canEditTranscript()) box.insertAdjacentHTML("beforeend", `<div class="edit-hint">Double-click any line to fix a word · Enter to save · Esc to cancel</div>`);
  box.scrollTop = box.scrollHeight;
}
function appendSegment(s, animate = true) {
  const box = $("tab-transcript");
  box.querySelector(".drawer-empty")?.remove();
  const nearBottom = box.scrollHeight - box.scrollTop - box.clientHeight < 80;
  const p = document.createElement("p");
  p.textContent = s.text;
  p.dataset.id = s.id;
  const key = `${s.speaker}|${s.cluster || ""}`;
  if (lastSeg && lastSeg.key === key && s.t_start - lastSeg.t_end < 25) {
    lastSeg.el.appendChild(p);
  } else {
    const row = document.createElement("div");
    const me = (s.channel || s.speaker) === "Me";
    const col = speakerColor(s.speaker);
    row.className = `bubble-row ${me ? "me" : "them"}`;
    if (!animate) row.style.animation = "none";
    const label = s.cluster
      ? `<button class="spk" data-cluster="${esc(s.cluster)}" data-name="${esc(s.speaker)}" style="color:${col}" title="Rename this voice">${esc(s.speaker)}</button>`
      : `<b style="color:${col}">${esc(s.speaker)}</b>`;
    row.innerHTML = `<div class="bubble-meta">${label}<time>${clock(s.t_start)}</time></div><div class="bubble"></div>`;
    if (!me && s.speaker !== "Them") row.querySelector(".bubble").style.background = `color-mix(in srgb, ${col} 12%, var(--surface))`;
    row.querySelector(".bubble").appendChild(p);
    box.appendChild(row);
    lastSeg = { key, el: row.querySelector(".bubble"), t_end: s.t_end };
  }
  lastSeg.t_end = s.t_end;
  if (nearBottom || !animate) box.scrollTop = box.scrollHeight;
  else if (S.drawerTab === "transcript") $("jump-live").hidden = false;
}
$("tab-transcript").addEventListener("click", (e) => {
  const b = e.target.closest(".spk");
  if (b) openRename(b, b.dataset.cluster, b.dataset.name);
});

// ---------------------------------------------------------------- speakers: rename + people menu
async function openRename(anchor, cluster, name) {
  const voices = await api.list_voices();
  const unnamed = /^Speaker \d+$/.test(name);
  const menu = $("menu");
  menu.innerHTML = `<div class="rename">
      <div class="rename-title">Who is this?</div>
      <input id="rename-input" list="voice-names" value="${unnamed ? "" : esc(name)}" placeholder="Type their name…" autocomplete="off">
      <datalist id="voice-names">${voices.map((v) => `<option value="${esc(v.name)}">`).join("")}</datalist>
      ${voices.length ? `<div class="rename-known">${voices.filter((v) => !v.is_self).slice(0, 6).map((v) =>
        `<button class="name-pill" data-pick="${esc(v.name)}"><i style="background:${speakerColor(v.name)}"></i>${esc(v.name)}</button>`).join("")}</div>` : ""}
      <div class="rename-hint">${ic("headphones")}LocalNotes will remember this voice and name them automatically in future meetings.</div>
      <div class="rename-actions"><button class="btn sm ghost" data-x>Cancel</button><button class="btn sm primary" data-ok>Save</button></div>
    </div>`;
  menu.hidden = false;
  const r = anchor.getBoundingClientRect();
  menu.style.top = `${Math.min(window.innerHeight - menu.offsetHeight - 12, r.bottom + 6)}px`;
  menu.style.left = `${Math.max(12, Math.min(window.innerWidth - menu.offsetWidth - 12, r.left))}px`;
  const input = $("rename-input");
  input.focus(); input.select();
  const save = async (value) => {
    value = (value ?? input.value).trim();
    if (!value) return input.focus();
    closeMenu();
    const m = await api.rename_speaker(S.meeting.id, cluster, value);
    if (m) {
      toast(value === "Me" ? "Updated" : `Got it — ${value}'s voice will be recognised from now on`);
      openMeeting(m.id, { view: S.view });
    }
  };
  menu.onclick = (e) => {
    if (e.target.closest("[data-x]")) closeMenu();
    else if (e.target.closest("[data-ok]")) save();
    else if (e.target.closest("[data-pick]")) save(e.target.closest("[data-pick]").dataset.pick);
  };
  input.onkeydown = (e) => { if (e.key === "Enter") { e.preventDefault(); save(); } else if (e.key === "Escape") closeMenu(); };
}

function openPeople(anchor) {
  const m = S.meeting; if (!m?.speakers?.length) return;
  const menu = $("menu");
  menu.innerHTML = `<div class="people-title">People in this meeting</div>` + m.speakers.map((s, i) => `
    <button data-i="${i}" class="person"><i style="background:${speakerColor(s.name)}"></i>
      <span class="p-name">${esc(s.name)}${s.source === "profile" ? ` <small>recognised</small>` : s.source === "you" ? " <small>you</small>" : ""}</span>
      <span class="p-time">${clock(s.seconds)}</span><span class="p-edit">${ic("pen")}</span></button>`).join("") +
    `<div class="people-hint">Click someone to rename them. Their voice is remembered for next time.</div>`;
  menu.hidden = false;
  const r = anchor.getBoundingClientRect();
  menu.style.top = `${r.bottom + 6}px`;
  menu.style.left = `${Math.min(window.innerWidth - menu.offsetWidth - 12, r.left)}px`;
  menu.onclick = (e) => {
    const b = e.target.closest("button[data-i]"); if (!b) return;
    const s = m.speakers[Number(b.dataset.i)];
    openRename(anchor, s.cluster, s.name);
  };
}
$("chips").addEventListener("click", (e) => {
  const c = e.target.closest("[data-act]"); if (!c) return;
  if (c.dataset.act === "people") openPeople(c);
  else if (c.dataset.act === "template") openTemplateMenu(c);
});

$("tab-transcript").addEventListener("scroll", (e) => {
  const b = e.target;
  if (b.scrollHeight - b.scrollTop - b.clientHeight < 80) $("jump-live").hidden = true;
});
$("jump-live").addEventListener("click", () => { const b = $("tab-transcript"); b.scrollTop = b.scrollHeight; $("jump-live").hidden = true; });

// ---------------------------------------------------------------- drawer: ask
const SUGGESTIONS = [
  ["check", "What did we decide?"],
  ["users", "What are my action items?"],
  ["send", "Draft a follow-up email to the attendees"],
  ["lines", "Summarize this in 3 bullet points"],
];
function renderChat() {
  const chat = $("chat"), m = S.meeting; if (!m) return;
  const msgs = S.chats[m.id] || [];
  if (!msgs.length) {
    chat.innerHTML = `<div class="suggest"><div class="suggest-title">Ask about this meeting</div>${SUGGESTIONS.map(([i, q]) => `<button data-q="${esc(q)}">${ic(i)}${esc(q)}</button>`).join("")}</div>`;
    return;
  }
  chat.innerHTML = msgs.map((msg, i) => msg.role === "user"
    ? `<div class="msg user">${esc(msg.content)}</div>`
    : `<div class="msg ai"><div class="md ${msg.streaming ? "caret-host" : ""}">${msg.error ? `<div class="error-box">${esc(msg.error)}</div>` : (renderMarkdown(msg.content || "") || '<p class="caret"></p>')}</div></div>`).join("");
  const last = chat.querySelector(".caret-host > :last-child");
  last?.classList.add("caret");
  chat.scrollTop = chat.scrollHeight;
}
$("chat").addEventListener("click", (e) => { const b = e.target.closest("[data-q]"); if (b) ask(b.dataset.q); });
async function ask(q) {
  q = q.trim();
  if (!q || !S.meeting || S.asking) return;
  const mid = S.meeting.id;
  const msgs = (S.chats[mid] ||= []);
  const history = msgs.filter((m) => !m.error).map(({ role, content }) => ({ role, content }));
  msgs.push({ role: "user", content: q }, { role: "assistant", content: "", streaming: true });
  $("ask-input").value = ""; autosize($("ask-input"));
  S.asking = true;
  renderChat();
  const ok = await api.ask(mid, q, history);
  if (!ok) S.asking = false;
}
$("ask-send").addEventListener("click", () => ask($("ask-input").value));
$("ask-input").addEventListener("input", (e) => autosize(e.target));
$("ask-input").addEventListener("keydown", (e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); ask(e.target.value); } });
function lastAssistant(mid) { const msgs = S.chats[mid] || []; return msgs[msgs.length - 1]; }

// ---------------------------------------------------------------- follow-up email
let fuTone = "friendly";
function splitEmail(text) {
  const m = (text || "").match(/^\s*Subject:\s*(.*)\n+([\s\S]*)$/i);
  return m ? { subject: m[1].trim(), body: m[2].trim() } : { subject: "", body: (text || "").trim() };
}
function setTone(t) {
  fuTone = t;
  $("fu-tone").querySelectorAll("button").forEach((b) => b.classList.toggle("active", b.dataset.tone === t));
}
function renderFollowup(final = false) {
  const { subject, body } = splitEmail(S.followup.buffer);
  if (S.followup.streaming || final) {
    $("fu-subject").value = subject;
    $("fu-text").value = body;
    if (S.followup.streaming) $("fu-text").scrollTop = $("fu-text").scrollHeight;
  }
  $("fu-text").classList.toggle("streaming", S.followup.streaming);
  $("fu-status").textContent = S.followup.streaming ? "Writing your email…" : "Drafted locally from your notes — review before sending.";
  ["fu-regen", "fu-copy", "fu-open"].forEach((id) => { $(id).disabled = S.followup.streaming; });
  $("fu-tone").querySelectorAll("button").forEach((b) => { b.disabled = S.followup.streaming; });
}
async function draftFollowup() {
  const ok = await api.draft_followup(S.followup.mid, fuTone);
  if (ok) { S.followup.streaming = true; S.followup.buffer = ""; renderFollowup(true); }
}
function openFollowup() {
  const m = S.meeting; if (!m) return;
  if (!m.ai_notes && !(m.segments || []).length) return toast("Nothing to follow up on yet — record or generate notes first", { kind: "error" });
  S.followup = { mid: m.id, streaming: false, buffer: m.followup || "" };
  setTone(S.settings.followup_tone || "friendly");
  renderFollowup(true);
  $("followup").showModal();
  if (!m.followup) draftFollowup();
}
$("btn-followup").addEventListener("click", openFollowup);
$("fu-tone").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b && b.dataset.tone !== fuTone) { setTone(b.dataset.tone); draftFollowup(); } });
$("fu-regen").addEventListener("click", draftFollowup);
const currentEmail = () => ({ subject: $("fu-subject").value.trim(), body: $("fu-text").value.trim() });
const saveFollowup = debounce(() => {
  const { subject, body } = currentEmail();
  const text = `Subject: ${subject}\n\n${body}`;
  api.save_followup(S.followup.mid, text);
  if (S.meeting?.id === S.followup.mid) S.meeting.followup = text;
}, 500);
$("fu-subject").addEventListener("input", saveFollowup);
$("fu-text").addEventListener("input", saveFollowup);
$("fu-copy").addEventListener("click", async () => {
  const { subject, body } = currentEmail();
  toast((await copyText(`Subject: ${subject}\n\n${body}`)) ? "Email copied" : "Couldn't copy", { kind: "ok" });
});
$("fu-open").addEventListener("click", () => { const { subject, body } = currentEmail(); api.open_email(subject, body); toast("Opened in your mail app"); });
document.querySelectorAll("dialog.sheet").forEach((d) => d.addEventListener("click", (e) => { if (e.target.closest("[data-close]")) d.close(); }));

// ---------------------------------------------------------------- transcript editing
function canEditTranscript() { const m = S.meeting; return !!m && m.id !== S.recordingId && !S.pipeline[m.id]; }
$("tab-transcript").addEventListener("dblclick", (e) => {
  const p = e.target.closest(".bubble p"); if (!p || p.isContentEditable) return;
  if (!canEditTranscript()) return toast("You can edit the transcript once the call has been processed");
  editLine(p);
});
function editLine(p) {
  const original = p.textContent;
  p.contentEditable = "true";
  p.focus();
  const range = document.createRange(); range.selectNodeContents(p); range.collapse(false);
  const sel = getSelection(); sel.removeAllRanges(); sel.addRange(range);
  const finish = async (save) => {
    p.onblur = p.onkeydown = null;
    p.contentEditable = "false";
    const text = p.textContent.trim();
    if (!save || text === original) { p.textContent = original; return; }
    const id = Number(p.dataset.id);
    const r = await api.update_segment(id, text);
    if (!r) { p.textContent = original; return toast("Couldn't save that edit", { kind: "error" }); }
    const segs = S.meeting?.segments || [];
    if (r.deleted) {
      const bubble = p.parentElement; p.remove();
      if (!bubble.children.length) bubble.closest(".bubble-row").remove();
      S.meeting.segments = segs.filter((s) => s.id !== id);
      return toast("Line removed");
    }
    p.textContent = r.text;
    const seg = segs.find((s) => s.id === id); if (seg) seg.text = r.text;
    if (r.suggest?.length) {
      const words = r.suggest.map((w) => `“${w}”`).join(", ");
      toast(`Saved. Add ${words} to your vocabulary so it's spelled right next time?`, {
        timeout: 10000, action: { label: "Add", run: async () => { S.settings.vocabulary = await api.add_vocabulary(r.suggest); toast("Added to vocabulary"); } },
      });
    } else toast("Transcript updated");
  };
  p.onkeydown = (ev) => {
    if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); p.blur(); }
    else if (ev.key === "Escape") { ev.preventDefault(); finish(false); }
  };
  p.onblur = () => finish(true);
}

// ---------------------------------------------------------------- more menu
function openMenu(anchor, items) {
  const menu = $("menu");
  menu.innerHTML = items.map((it, i) => it === "-" ? "<hr>" :
    `<button data-i="${i}" class="${it.danger ? "danger" : ""}">${ic(it.icon)}${esc(it.label)}${it.kbd ? `<kbd>${it.kbd}</kbd>` : ""}</button>`).join("");
  menu.hidden = false;
  const r = anchor.getBoundingClientRect();
  menu.style.top = `${r.bottom + 6}px`;
  menu.style.left = `${Math.min(window.innerWidth - menu.offsetWidth - 12, r.right - menu.offsetWidth)}px`;
  menu.onclick = (e) => {
    const b = e.target.closest("button[data-i]"); if (!b) return;
    const it = items[Number(b.dataset.i)];
    if (it.danger && !b.classList.contains("armed")) {
      b.classList.add("armed"); b.lastChild.textContent = "Click again to delete"; return;
    }
    closeMenu(); it.run();
  };
}
function closeMenu() { $("menu").hidden = true; }
document.addEventListener("mousedown", (e) => { if (!$("menu").hidden && !e.target.closest("#menu, #btn-more, .spk, [data-act]")) closeMenu(); });

$("btn-more").addEventListener("click", (e) => {
  if (!$("menu").hidden) return closeMenu();
  const m = S.meeting; if (!m) return;
  const live = m.id === S.recordingId;
  const items = [
    { icon: "copy", label: "Copy notes as Markdown", kbd: "Ctrl Shift C", run: copyNotes },
    { icon: "download", label: "Export to file…", run: async () => { const p = await api.export_meeting(m.id); if (p) toast(`Exported to ${p}`); } },
  ];
  if (!live) items.push({ icon: "sparkle", label: m.ai_notes ? "Regenerate AI notes" : "Generate AI notes", run: generate });
  if (m.has_audio && !live) items.push({ icon: "headphones", label: "Improve transcript", run: () => api.refine_meeting(m.id) });
  if (!live) items.push("-", { icon: "trash", label: "Delete meeting", danger: true, run: deleteMeeting });
  openMenu(e.currentTarget, items);
});
async function copyNotes() {
  const m = S.meeting; if (!m) return;
  const text = `# ${m.title}\n\n${(m.ai_notes || m.user_notes || "").trim()}\n`;
  toast((await copyText(text)) ? "Notes copied" : "Couldn't copy", { kind: "ok" });
}
async function deleteMeeting() {
  const id = S.meeting?.id; if (!id) return;
  if (await api.delete_meeting(id)) { toast("Meeting deleted"); showHome(); loadList(); }
}

// ---------------------------------------------------------------- recording + dock
async function startRecording() {
  if (S.recordingId) return openMeeting(S.recordingId);
  const id = await api.start_recording("", "manual");
  if (id) onRecordingStarted(id);
}
function onRecordingStarted(id) {
  S.recordingId = id;
  S.recStarted = S.recStarted || Date.now() / 1000;
  setDrawer(true); setDrawerTab("transcript");
  openMeeting(id, { view: "mine" }).then(() => { $("editor").focus(); });
}
$("btn-new").addEventListener("click", startRecording);
$("dock-stop").addEventListener("click", async () => {
  $("dock-stop").disabled = true;
  await api.stop_recording();
  $("dock-stop").disabled = false;
});
$("dock-open").addEventListener("click", () => openMeeting(S.recordingId));

function elapsed() {
  if (!S.recStarted) return 0;
  const now = Date.now() / 1000;
  return now - S.recStarted - S.pausedTotal - (S.pausedAt ? now - S.pausedAt : 0);
}
$("dock-pause").addEventListener("click", () => (S.pausedAt ? api.resume_recording() : api.pause_recording()));

function renderDock() {
  const live = !!S.recordingId;
  $("dock").hidden = !live;
  $("dock").classList.toggle("paused", !!S.pausedAt);
  $("dock-pause").innerHTML = S.pausedAt ? `${ic("play")}<span>Resume</span>` : `${ic("pause")}<span>Pause</span>`;
  $("dock-pause").title = S.pausedAt ? "Resume recording" : "Pause recording (off the record)";
  document.body.classList.toggle("recording", live);
  $("dock-open").hidden = !live || S.currentId === S.recordingId;
  $("btn-new").disabled = live;
}
const meterHist = { Me: new Array(28).fill(0), Them: new Array(28).fill(0) };
function drawMeter(canvas, hist, color) {
  const ctx = canvas.getContext("2d"), w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  const n = hist.length, bw = w / n;
  ctx.fillStyle = color;
  hist.forEach((v, i) => {
    const bh = Math.max(4, Math.min(h, Math.sqrt(v) * h * 4.2));
    const x = i * bw + bw * 0.2, y = (h - bh) / 2, r = Math.min(bw * 0.3, bh / 2);
    ctx.globalAlpha = 0.35 + 0.65 * (i / n);
    ctx.beginPath(); ctx.roundRect(x, y, bw * 0.6, bh, r); ctx.fill();
  });
  ctx.globalAlpha = 1;
}
setInterval(async () => {
  if (!api || !S.recordingId) return;
  const { levels, pending } = await api.levels();
  meterHist.Me.push(levels.Me || 0); meterHist.Me.shift();
  meterHist.Them.push(levels.Them || 0); meterHist.Them.shift();
  const css = getComputedStyle(document.documentElement);
  drawMeter($("meter-me"), meterHist.Me, css.getPropertyValue("--accent").trim());
  drawMeter($("meter-them"), meterHist.Them, css.getPropertyValue("--them").trim());
  $("dock-pending").textContent = pending > 2 ? "catching up…" : "";
}, 120);
setInterval(() => {
  if (!S.recordingId || !S.recStarted) return;
  const t = clock(elapsed());
  $("dock-time").textContent = S.pausedAt ? `Paused · ${t}` : t;
  const chip = $("live-chip"); if (chip) chip.lastChild.textContent = S.pausedAt ? `Paused · ${t}` : t;
}, 500);

// ---------------------------------------------------------------- detected-meeting banner
function renderBanner() {
  const show = !!S.detected && !S.recordingId && !S.homeVisible;
  $("banner").hidden = !show;
  if (show) {
    $("banner-title").textContent = `${S.detected[1]} detected`;
    $("banner-chip").className = `app-chip ${["zoom", "teams", "meet"].includes(S.detected[0]) ? S.detected[0] : ""}`;
  }
}
$("banner-yes").addEventListener("click", async () => { const id = await api.start_from_detection(); if (id) onRecordingStarted(id); });
$("banner-no").addEventListener("click", () => api.dismiss_detection());

// ---------------------------------------------------------------- command palette
let pSel = 0, pItems = [];
// FTS snippets come from raw Markdown; strip its syntax and keep only the <mark> highlights.
function snippetHtml(s) {
  const clean = s.replace(/-\s*\[[ xX]\]\s*/g, "").replace(/(^|\s)#{1,4}\s*/g, "$1").replace(/[*`_]{1,2}/g, "").replace(/\s+/g, " ");
  return esc(clean).replace(/&lt;mark&gt;/g, "<mark>").replace(/&lt;\/mark&gt;/g, "</mark>");
}
const ACTIONS = [
  { icon: "wave", title: "New note", sub: "Start recording now", kbd: "Ctrl N", run: () => startRecording() },
  { icon: "home", title: "Go home", sub: "Dashboard and action items", run: () => showHome() },
  { icon: "sidebar", title: "Toggle transcript panel", kbd: "Ctrl J", run: () => S.meeting && setDrawer(!S.drawerOpen) },
  { icon: "moon", title: "Toggle dark mode", run: () => $("btn-theme").click() },
  { icon: "gear", title: "Settings", kbd: "Ctrl ,", run: () => openSettings() },
];
function openPalette() {
  $("palette").hidden = false;
  $("palette-input").value = "";
  $("palette-input").focus();
  updatePalette();
}
function closePalette() { $("palette").hidden = true; }
const updatePalette = debounce(async () => {
  const q = $("palette-input").value.trim();
  const acts = ACTIONS.filter((a) => !q || a.title.toLowerCase().includes(q.toLowerCase()));
  const meetings = q ? await api.search(q) : (await api.list_meetings()).slice(0, 6);
  pItems = [
    ...acts.map((a) => ({ ...a, group: "Actions" })),
    ...meetings.map((m) => ({
      group: q ? "Meetings" : "Recent", icon: m.app && m.app !== "manual" ? "video" : "file",
      title: m.title, subHtml: m.snippet ? snippetHtml(m.snippet) : `${dayOf(m.started_at)} · ${timeOf(m.started_at)}`,
      run: () => openMeeting(m.id),
    })),
  ];
  pSel = 0;
  drawPalette();
}, 90);
function drawPalette() {
  let group = "", html = "";
  pItems.forEach((it, i) => {
    if (it.group !== group) { html += `<div class="p-group">${it.group}</div>`; group = it.group; }
    html += `<div class="p-item ${i === pSel ? "sel" : ""}" data-i="${i}"><div class="p-ic">${ic(it.icon)}</div>
      <div class="p-body"><div class="p-title">${esc(it.title)}</div><div class="p-sub">${it.subHtml || esc(it.sub || "")}</div></div>${it.kbd ? `<kbd>${it.kbd}</kbd>` : ""}</div>`;
  });
  $("palette-results").innerHTML = html || `<div class="list-empty">No matches</div>`;
  $("palette-results").querySelector(".sel")?.scrollIntoView({ block: "nearest" });
}
$("palette-input").addEventListener("input", updatePalette);
$("palette-input").addEventListener("keydown", (e) => {
  if (e.key === "ArrowDown") { e.preventDefault(); pSel = Math.min(pItems.length - 1, pSel + 1); drawPalette(); }
  else if (e.key === "ArrowUp") { e.preventDefault(); pSel = Math.max(0, pSel - 1); drawPalette(); }
  else if (e.key === "Enter") { e.preventDefault(); const it = pItems[pSel]; if (it) { closePalette(); it.run(); } }
});
$("palette-results").addEventListener("click", (e) => { const it = e.target.closest(".p-item"); if (it) { closePalette(); pItems[Number(it.dataset.i)].run(); } });
$("palette-results").addEventListener("mousemove", (e) => {
  const it = e.target.closest(".p-item"); if (!it || Number(it.dataset.i) === pSel) return;
  pSel = Number(it.dataset.i); drawPalette();
});
$("palette").addEventListener("mousedown", (e) => { if (e.target === $("palette")) closePalette(); });
$("btn-search").addEventListener("click", openPalette);

// ---------------------------------------------------------------- settings
let pendingTheme = "system";
let pendingStyle = "glass";
async function openSettings(focusId) {
  const st = await api.get_state();
  S.settings = st.settings; S.ollama = st.ollama;
  const dlg = $("settings");
  await renderTemplates();
  $("enc-title").textContent = st.encrypted ? "Encrypted on this PC" : "Not encrypted";
  $("app-version").textContent = `· LocalNotes v${st.version}`;
  const hk = $("hotkey-warning");
  hk.hidden = !st.hotkeys_failed?.length;
  if (st.hotkeys_failed?.length) hk.innerHTML = `${ic("alert")}<span>${esc(st.hotkeys_failed.join(", ").toUpperCase())} is already used by another app. Click the box and press a different shortcut.</span>`;
  if (focusId) setTimeout(() => $(focusId)?.scrollIntoView({ block: "center", behavior: "smooth" }), 80);
  dlg.querySelectorAll("[name]").forEach((el) => {
    const v = S.settings[el.name];
    if (el.type === "checkbox") el.checked = !!v; else el.value = v ?? "";
  });
  pendingTheme = S.settings.theme;
  pendingStyle = S.settings.ui_style || "glass";
  dlg.querySelectorAll("#theme-pick button").forEach((b) => b.classList.toggle("active", b.dataset.theme === pendingTheme));
  dlg.querySelectorAll("#style-pick button").forEach((b) => b.classList.toggle("active", b.dataset.ui === pendingStyle));
  $("ollama-models").innerHTML = (st.ollama.models || []).map((m) => `<option value="${esc(m)}">`).join("");
  renderVoices();
  $("ollama-line").textContent = !st.ollama.running ? "Ollama isn't running — start the Ollama app."
    : st.ollama.model_ready ? "Runs locally — nothing leaves this PC." : "Model not downloaded yet.";
  dlg.showModal();
}
$("btn-settings").addEventListener("click", () => openSettings());

// templates manager
async function renderTemplates() {
  S.templates = await api.list_templates();
  $("default-template").innerHTML = `<option value="auto">Auto — from the meeting title</option>` +
    S.templates.map((t) => `<option value="${esc(t.id)}">${esc(t.name)}</option>`).join("");
  $("default-template").value = S.settings.default_template || "auto";
  const custom = S.templates.filter((t) => !t.builtin);
  $("tpl-list").innerHTML = custom.length ? custom.map((t) => `
    <div class="voice-row"><span class="tpl-ic">${ic(t.icon)}</span>
      <div class="v-text"><b>${esc(t.name)}</b><span>${esc(t.description || "Custom template")}</span></div>
      <button class="btn sm ghost" data-edit="${esc(t.id)}">Edit</button></div>`).join("")
    : `<div class="voice-empty">${ic("file")}No custom templates yet — ${S.templates.length} built-in ones are ready to use.</div>`;
}
let editingTpl = null;
function openTplEditor(t) {
  editingTpl = t?.id || null;
  $("tpl-title").textContent = t ? "Edit template" : "New template";
  $("tpl-name").value = t?.name || "";
  $("tpl-desc").value = t?.description || "";
  $("tpl-prompt").value = t?.prompt || "";
  $("tpl-delete").hidden = !t;
  $("tpl-delete").classList.remove("armed"); $("tpl-delete").textContent = "Delete";
  $("tpl-starters").innerHTML = S.templates.filter((x) => x.builtin).map((x) => `<button data-start="${esc(x.id)}">Start from ${esc(x.name)}</button>`).join("");
  $("tpl-editor").showModal();
  $("tpl-name").focus();
}
$("tpl-new").addEventListener("click", () => openTplEditor(null));
$("tpl-list").addEventListener("click", (e) => { const b = e.target.closest("[data-edit]"); if (b) openTplEditor(S.templates.find((t) => t.id === b.dataset.edit)); });
$("tpl-starters").addEventListener("click", (e) => {
  const b = e.target.closest("[data-start]"); if (!b) return;
  const t = S.templates.find((x) => x.id === b.dataset.start);
  $("tpl-prompt").value = t.prompt;
  if (!$("tpl-desc").value) $("tpl-desc").value = t.description;
});
$("tpl-save").addEventListener("click", async () => {
  const id = await api.save_template({ id: editingTpl, name: $("tpl-name").value, description: $("tpl-desc").value, prompt: $("tpl-prompt").value });
  if (!id) return toast("Give the template a name and a structure", { kind: "error" });
  $("tpl-editor").close();
  await renderTemplates();
  toast(editingTpl ? "Template updated" : "Template created — pick it from the template menu on any meeting");
});
$("tpl-delete").addEventListener("click", async () => {
  const b = $("tpl-delete");
  if (!b.classList.contains("armed")) { b.classList.add("armed"); b.textContent = "Click again to delete"; return; }
  await api.delete_template(editingTpl);
  $("tpl-editor").close();
  await renderTemplates();
  toast("Template deleted");
});

// hotkey capture: click the box, press the combination
document.querySelectorAll(".hotkey-input").forEach((inp) => {
  inp.addEventListener("focus", () => { inp.dataset.prev = inp.value; inp.value = ""; inp.placeholder = "Press keys…"; inp.classList.add("capturing"); });
  inp.addEventListener("blur", () => { if (!inp.value) inp.value = inp.dataset.prev || ""; inp.classList.remove("capturing"); inp.placeholder = "Click, then press keys"; });
  inp.addEventListener("keydown", (e) => {
    e.preventDefault(); e.stopPropagation();
    if (e.key === "Escape") { inp.value = inp.dataset.prev || ""; return inp.blur(); }
    if (["Control", "Alt", "Shift", "Meta"].includes(e.key)) return;
    const mods = [e.ctrlKey && "ctrl", e.altKey && "alt", e.shiftKey && "shift", e.metaKey && "win"].filter(Boolean);
    if (!mods.length) return toast("Use at least one of Ctrl, Alt, Shift or Win", { kind: "error" });
    const key = e.code.startsWith("Key") ? e.code.slice(3).toLowerCase()
      : e.code.startsWith("Digit") ? e.code.slice(5)
      : /^F\d{1,2}$/.test(e.key) ? e.key.toLowerCase()
      : e.key === " " ? "space" : null;
    if (!key) return;
    inp.value = [...mods, key].join("+");
    inp.blur();
  });
});

async function renderVoices() {
  const voices = await api.list_voices();
  $("voice-list").innerHTML = voices.length ? voices.map((v) => `
    <div class="voice-row"><i style="background:${speakerColor(v.is_self ? "Me" : v.name)}"></i>
      <div class="v-text"><b>${esc(v.name)}${v.is_self ? " <small>you</small>" : ""}</b>
      <span>Heard in ${v.meetings} meeting${v.meetings === 1 ? "" : "s"} · ${v.samples} voice sample${v.samples === 1 ? "" : "s"}</span></div>
      <button class="btn sm ghost" data-del="${esc(v.name)}">Forget</button></div>`).join("")
    : `<div class="voice-empty">${ic("headphones")}No voices saved yet. After a call, click a speaker's name in the transcript to tag them.</div>`;
}
$("voice-list").addEventListener("click", async (e) => {
  const b = e.target.closest("[data-del]"); if (!b) return;
  if (!b.classList.contains("armed")) { b.classList.add("armed"); b.textContent = "Click to confirm"; return; }
  await api.delete_voice(b.dataset.del);
  toast(`Forgot ${b.dataset.del}'s voice`);
  renderVoices();
});
$("theme-pick").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  pendingTheme = b.dataset.theme;
  $("theme-pick").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
  applyTheme(pendingTheme);
});
$("style-pick").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  pendingStyle = b.dataset.ui;
  $("style-pick").querySelectorAll("button").forEach((x) => x.classList.toggle("active", x === b));
  applyStyle(pendingStyle);
});
const revertLook = () => { applyTheme(S.settings.theme); applyStyle(S.settings.ui_style); };
$("settings").addEventListener("click", (e) => { if (e.target.closest("[data-close]")) { revertLook(); $("settings").close(); } });
$("settings").addEventListener("cancel", revertLook);
$("settings-save").addEventListener("click", async () => {
  const next = { theme: pendingTheme, ui_style: pendingStyle };
  $("settings").querySelectorAll("[name]").forEach((el) => { next[el.name] = el.type === "checkbox" ? el.checked : el.value.trim(); });
  S.settings = await api.save_settings(next);
  applyTheme(S.settings.theme);
  applyStyle(S.settings.ui_style);
  $("settings").close();
  toast("Settings saved");
  renderDetectStatus();
  if (S.homeVisible) renderHome();
});

// ---------------------------------------------------------------- keyboard
document.addEventListener("keydown", (e) => {
  const k = e.key.toLowerCase();
  if (e.key === "Escape") {
    if (!$("palette").hidden) return closePalette();
    if (!$("menu").hidden) return closeMenu();
  }
  if (!e.ctrlKey) return;
  if (k === "k") { e.preventDefault(); $("palette").hidden ? openPalette() : closePalette(); }
  else if (k === "n") { e.preventDefault(); startRecording(); }
  else if (k === ",") { e.preventDefault(); openSettings(); }
  else if (k === "j" && S.meeting) { e.preventDefault(); setDrawer(!S.drawerOpen); }
  else if (k === "e" && S.meeting) { e.preventDefault(); setView(S.view === "mine" ? "ai" : "mine"); }
  else if (k === "c" && e.shiftKey && S.meeting) { e.preventDefault(); copyNotes(); }
});

$("doc-scroll").addEventListener("scroll", () => {
  $("toolbar").classList.toggle("stuck", $("toolbar").getBoundingClientRect().top <= $("doc-scroll").getBoundingClientRect().top + 1);
});
const remeasure = () => {
  if (S.meeting) { setSeg($("view-seg"), "v", S.view); setSeg($("drawer-seg"), "t", S.drawerTab); autosize($("editor")); autosize($("title")); }
};
window.addEventListener("resize", remeasure);
document.fonts?.ready.then(remeasure);   // segmented-control highlights are measured in px; redo once fonts settle
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(S.settings.theme));

// ---------------------------------------------------------------- events from Python
const isCur = (e) => S.meeting && e.meeting_id === S.meeting.id;
window.onEvent = (e) => {
  switch (e.type) {
    case "status":
      if (e.text) toast(e.text, { id: "status", kind: /error/i.test(e.text) ? "error" : "progress", timeout: /error/i.test(e.text) ? 6000 : 0 });
      else dismissToast("status");
      break;

    case "segment":
      if (isCur(e)) {
        S.meeting.segments.push(e.segment);
        appendSegment(e.segment);
        if (S.meeting.segments.length === 1) renderChips();
      }
      break;

    case "recording":
      S.pausedAt = null; S.pausedTotal = 0;
      if (e.recording) {
        S.recordingId = e.meeting_id; S.recStarted = e.started; S.detected = null;
        const devs = e.devices || {};
        toast(`Recording · Me: ${devs.Me ? "mic on" : "off"} · Them: ${devs.Them ? "computer audio on" : "off"}`);
      } else {
        S.recordingId = null; S.recStarted = null;
        toast("Call ended — writing your notes…", { kind: "progress", id: "wrap", timeout: 5000 });
      }
      renderDock(); renderDetectStatus(); renderBanner(); loadList();
      if (isCur(e)) { renderChips(); updateToolbar(); renderAI(); }
      if (S.homeVisible) renderHome();
      break;

    case "pipeline":
      S.pipeline[e.meeting_id] = e.stage;
      if (isCur(e)) { renderStepper(); updateToolbar(); $("tab-transcript").classList.toggle("refining", e.stage === "refining"); }
      if (e.stage === "done") {
        delete S.refineText[e.meeting_id];
        setTimeout(() => {
          delete S.pipeline[e.meeting_id];
          if (isCur(e)) openMeeting(e.meeting_id, { view: S.view });
        }, 2200);
        loadList();
        if (S.homeVisible) renderHome();
      }
      break;

    case "meeting_detected":
      S.detected = [e.app, e.title];
      renderDetectStatus(); renderBanner();
      if (S.homeVisible) renderHome();
      break;
    case "meeting_dismissed":
    case "meeting_ended":
      S.detected = null;
      renderDetectStatus(); renderBanner();
      if (S.homeVisible) renderHome();
      break;
    case "open_meeting":
      openMeeting(e.meeting_id);
      break;

    case "notes_start":
      if (isCur(e)) {
        S.streaming = true; S.aiBuffer = ""; S.notesError = ""; S.notesProgress = "";
        if (S.meeting.id !== S.recordingId) setView("ai");
        updateToolbar(); renderAI();
      }
      break;
    case "notes_progress":
      if (isCur(e)) { S.notesProgress = e.text; renderAI(); }
      break;
    case "notes_token":
      if (isCur(e)) { S.aiBuffer += e.text; renderAI(); }
      break;
    case "notes_done":
      dismissToast("wrap");
      if (isCur(e)) {
        S.streaming = false; S.meeting.ai_notes = e.notes; S.aiBuffer = e.notes;
        if (S.view !== "ai") $("ai-badge").hidden = false;
        updateToolbar(); renderAI();
      } else toast("AI notes are ready");
      if (S.homeVisible) renderHome();
      break;
    case "notes_error":
      dismissToast("wrap");
      if (isCur(e)) { S.streaming = false; S.notesError = e.error; updateToolbar(); renderAI(); }
      toast(e.error, { kind: "error", timeout: 7000 });
      break;

    case "refine_start":
      if (isCur(e)) $("tab-transcript").classList.add("refining");
      break;
    case "refine_progress": {
      const pct = e.text.match(/(\d+)%/);
      S.refineText[e.meeting_id] = pct ? `${pct[1]}%` : "";
      if (isCur(e)) renderStepper();
      if (!S.pipeline[e.meeting_id]) toast(e.text, { id: "refine", kind: "progress", timeout: 0 });
      break;
    }
    case "refine_done":
      dismissToast("refine");
      if (!S.pipeline[e.meeting_id]) toast(`Transcript improved (${e.count} lines)`);
      if (isCur(e) && !S.pipeline[e.meeting_id]) openMeeting(e.meeting_id, { view: S.view });
      break;
    case "refine_error":
      dismissToast("refine");
      if (isCur(e)) $("tab-transcript").classList.remove("refining");
      toast(`Couldn't improve the transcript: ${e.error}`, { kind: "error", timeout: 7000 });
      break;

    case "paused":
      S.pausedAt = e.paused_at; S.pausedTotal = e.paused_total;
      renderDock();
      if (isCur(e)) renderChips();
      toast(e.paused ? "Paused — nothing is being recorded" : "Recording resumed", { id: "pause" });
      break;
    case "hotkey_failed":
      toast(`Shortcut ${e.combos.join(", ").toUpperCase()} is taken by another app — pick a different one in Settings`, { kind: "error", timeout: 8000 });
      break;
    case "followup_start":
      if (S.followup.mid === e.meeting_id) { S.followup.streaming = true; S.followup.buffer = ""; renderFollowup(); }
      break;
    case "followup_token":
      if (S.followup.mid === e.meeting_id) { S.followup.buffer += e.text; renderFollowup(); }
      break;
    case "followup_done":
      if (S.meeting && S.meeting.id === e.meeting_id) S.meeting.followup = e.text;
      if (S.followup.mid === e.meeting_id) { S.followup.streaming = false; S.followup.buffer = e.text; renderFollowup(true); }
      break;
    case "followup_error":
      if (S.followup.mid === e.meeting_id) { S.followup.streaming = false; renderFollowup(); }
      toast(e.error, { kind: "error", timeout: 7000 });
      break;

    case "speakers_progress":
      S.speakerText[e.meeting_id] = e.text.replace(/…$/, "");
      if (isCur(e)) renderStepper();
      break;
    case "speakers_done":
      delete S.speakerText[e.meeting_id];
      if (e.recognised?.length) toast(`Recognised ${e.recognised.join(", ")} by voice`);
      else if (e.count > 1) toast(`Found ${e.count} voices — click a name in the transcript to tag who's who`, { timeout: 6000 });
      if (isCur(e)) openMeeting(e.meeting_id, { view: S.view });
      break;
    case "speakers_error":
      toast(`Couldn't separate speakers: ${e.error}`, { kind: "error", timeout: 7000 });
      break;

    case "ask_start": break;
    case "ask_token": {
      const msg = lastAssistant(e.meeting_id);
      if (msg) { msg.content += e.text; if (isCur(e)) renderChat(); }
      break;
    }
    case "ask_done":
    case "ask_error": {
      S.asking = false;
      const msg = lastAssistant(e.meeting_id);
      if (msg) { msg.streaming = false; if (e.type === "ask_error") msg.error = e.error; else msg.content = e.text; }
      if (isCur(e)) renderChat();
      break;
    }
  }
};

// ---------------------------------------------------------------- boot
window.addEventListener("pywebviewready", async () => {
  api = window.pywebview.api;
  try { S.drawerOpen = localStorage.getItem("drawerOpen") !== "0"; } catch {}
  const st = await api.get_state();
  S.settings = st.settings; S.user = st.user; S.ollama = st.ollama;
  S.recordingId = st.recording ? st.meeting_id : null;
  S.recStarted = st.recording_started;
  S.pausedAt = st.paused_at; S.pausedTotal = st.paused_total || 0;
  S.templates = await api.list_templates();
  if (st.hotkeys_failed?.length) window.onEvent({ type: "hotkey_failed", combos: st.hotkeys_failed });
  S.detected = st.detected;
  applyTheme(S.settings.theme);
  applyStyle(S.settings.ui_style);
  renderDetectStatus(); renderDock();
  await loadList();
  if (S.recordingId) openMeeting(S.recordingId, { view: "mine" });
  else showHome();
});

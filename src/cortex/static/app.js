/* Cortex GUI. Builds DOM with textContent; only server-rendered note HTML
   (markdown-it with raw HTML disabled) is inserted as HTML. Every API error has
   the shape {error: {code, message, request_id}}. */
"use strict";

const byId = (id) => document.getElementById(id);
const state = { status: null, graph: null, graphView: null, tab: "brain", key: null, notePath: null, role: null };
const ADMIN_TABS = ["connect", "settings"];
const SETUP_POLL_MS = 1000;
const STATUS_POLL_MS = 15000;

function element(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") node.className = value;
    else if (name.startsWith("on")) node.addEventListener(name.slice(2), value);
    else node.setAttribute(name, value === true ? "" : value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

class ApiError extends Error {
  constructor(status, detail) {
    super(detail.message || `HTTP ${status}`);
    this.status = status;
    this.code = detail.code || "";
    this.requestId = detail.request_id || "";
  }
}

async function api(path, { method = "GET", body } = {}) {
  const options = { method, credentials: "same-origin", headers: {} };
  if (method !== "GET") options.headers["X-Cortex-CSRF"] = "1";
  if (body !== undefined) {
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(body);
  }
  const response = await fetch(path, options);
  let data = {};
  try { data = await response.json(); } catch (_) { /* empty body */ }
  if (response.ok) return data;
  const failure = new ApiError(response.status, data.error || {});
  if (failure.code === "unauthorized" && path !== "/api/login") show("login");
  if (failure.code === "not_configured") openSetup();
  throw failure;
}

let toastTimer;
function toast(message, bad = false) {
  const box = byId("toast");
  box.textContent = message;
  box.classList.toggle("bad", bad);
  box.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (box.hidden = true), bad ? 6000 : 3000);
}

function reportError(error) {
  if (error.code === "unauthorized" || error.code === "not_configured") return;
  toast(error.requestId ? `${error.message} (request ${error.requestId})` : error.message, true);
}

function show(view) {
  for (const name of ["login", "setup", "app"]) byId(`view-${name}`).hidden = name !== view;
  if (view === "login") byId("login-password").focus();
}

function when(iso) {
  if (!iso) return "never";
  const moment = new Date(iso);
  const seconds = (Date.now() - moment.getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h ago`;
  return moment.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

/* ---------- boot ---------- */

async function boot() {
  const session = await api("/api/session");
  byId("login-version").textContent = `v${session.version}`;
  if (!session.authenticated) return show("login");
  state.role = session.role;
  // The server refuses guests every change; this only hides the controls they can't use.
  document.body.classList.toggle("guest", state.role === "guest");
  if (state.role === "guest" && ADMIN_TABS.includes(state.tab)) state.tab = "brain";
  await refreshStatus();
  if (!state.status.configured) {
    if (state.role !== "guest") return openSetup();
    await api("/api/logout", { method: "POST" });
    show("login");
    byId("login-error").textContent = "Cortex isn't set up yet: the admin needs to sign in first.";
    byId("login-error").hidden = false;
    return;
  }
  show("app");
  switchTab(state.tab);
}

byId("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  byId("login-error").hidden = true;
  try {
    await api("/api/login", { method: "POST", body: { password: byId("login-password").value } });
    byId("login-password").value = "";
    await boot();
  } catch (error) {
    byId("login-error").textContent = error.message;
    byId("login-error").hidden = false;
  }
});

byId("logout").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST" });
  show("login");
});

async function refreshStatus() {
  state.status = await api("/api/status");
  const status = state.status;
  byId("version").textContent = `v${status.version}`;
  byId("power").classList.toggle("off", status.power !== "on");
  byId("power-label").textContent = status.power === "on" ? "Brain on" : "Brain off";
  const waiting = status.pending + status.approved;
  byId("badge-synapse").hidden = !waiting;
  byId("badge-synapse").textContent = waiting;
  return status;
}

byId("power").addEventListener("click", async () => {
  if (state.role === "guest") return;
  const next = state.status.power === "on" ? "off" : "on";
  try {
    await api("/api/power", { method: "POST", body: { state: next } });
    await refreshStatus();
    toast(next === "on" ? "Brain on: clients can use it again." : "Brain off: tools answer 'off' until you turn it back on.");
  } catch (error) {
    reportError(error);
  }
});

/* ---------- tabs ---------- */

function switchTab(tab) {
  state.tab = tab;
  for (const button of byId("tabs").querySelectorAll("button")) button.classList.toggle("active", button.dataset.tab === tab);
  for (const name of ["brain", "synapse", "connect", "activity", "settings"]) byId(`tab-${name}`).hidden = name !== tab;
  const loaders = { brain: loadBrain, synapse: loadSynapse, connect: loadConnect, activity: loadActivity, settings: loadSettings };
  loaders[tab]().catch(reportError);
}

byId("tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-tab]");
  if (button) switchTab(button.dataset.tab);
});

/* ---------- brain ---------- */

async function loadBrain() {
  const [graph] = await Promise.all([api("/api/graph"), refreshStatus()]);
  state.graph = graph;
  if (!state.graphView) {
    state.graphView = new BrainGraph(byId("graph"), { onSelect: openNote, settings: loadGraphSettings() });
    byId("graph-fit").addEventListener("click", () => state.graphView.fit());
    buildGraphOptions();
  }
  requestAnimationFrame(() => {
    state.graphView.resize();
    state.graphView.setData(graph);
  });
  renderStats(graph);
  renderLegend(graph.groups);
  byId("note-list").replaceChildren(...graph.nodes.map((node) => element("option", { value: node.id })));
}

function renderStats(graph) {
  const status = state.status;
  const stat = (count, label, attention, onclick) =>
    element(onclick ? "button" : "div", { class: `stat${attention ? " attn" : ""}`, onclick },
      element("div", { class: "n" }, count), element("div", { class: "l" }, label));
  byId("stats").replaceChildren(
    stat(graph.nodes.length, "notes"),
    stat(graph.links.length, "links"),
    stat(graph.dead.length, "dead links", graph.dead.length > 0, graph.dead.length ? () => showDead(graph.dead) : null),
    stat(graph.orphans.length, "orphans"),
    stat(status.pending, "pending review", status.pending > 0, () => switchTab("synapse")),
    stat(status.approved, "approved, to write", status.approved > 0, () => switchTab("synapse")),
  );
}

function showDead(dead) {
  byId("note-empty").hidden = true;
  byId("note").hidden = false;
  byId("note-path").textContent = "Dead links";
  byId("note-tags").replaceChildren();
  byId("note-body").replaceChildren(
    element("p", { class: "muted" }, "Links that don't resolve to a note. Fix them in the source note."),
    element("ul", {}, dead.map((link) => element("li", {},
      element("a", { href: `#note=${encodeURIComponent(link.source)}` }, link.source), " → ", element("code", {}, link.target)))),
  );
  byId("note-out").replaceChildren();
  byId("note-in").replaceChildren();
}

/* Legend entries toggle their color group on and off; "other" is notes in no group. */
function renderLegend(groups) {
  const entries = groups.map((group) => ({ color: group.color, name: group.kind === "tag" ? `#${group.value}` : group.value }));
  if (entries.length && state.graph.nodes.some((node) => !node.color)) entries.push({ color: "", name: "other" });
  const hidden = new Set(state.graphView.settings.hiddenColors);
  byId("legend").replaceChildren(
    ...entries.map((entry) => {
      const dot = element("i");
      dot.style.background = entry.color || "var(--node)";
      const button = element("button", { type: "button", class: hidden.has(entry.color) ? "off" : null, title: "Show or hide these notes" }, dot, entry.name);
      button.addEventListener("click", () => {
        const now = new Set(state.graphView.settings.hiddenColors);
        if (now.has(entry.color)) now.delete(entry.color); else now.add(entry.color);
        button.classList.toggle("off", now.has(entry.color));
        changeGraph({ hiddenColors: [...now] });
      });
      return button;
    }),
  );
}

/* ---------- graph options (like Obsidian's graph settings) ---------- */

const GRAPH_SETTINGS_KEY = "cortex.graph";
const GRAPH_OPTIONS = [
  { section: "Filters", controls: [
    { key: "search", label: "Search", type: "text", placeholder: "path, name or #tag" },
    { key: "orphans", label: "Show orphans", type: "check" },
    { key: "local", label: "Local graph (selected note only)", type: "check" },
    { key: "depth", label: "Local depth", type: "range", min: 1, max: 4, step: 1 },
  ] },
  { section: "Display", controls: [
    { key: "nodeSize", label: "Node size", type: "range", min: 0.2, max: 2, step: 0.05 },
    { key: "linkWidth", label: "Link thickness", type: "range", min: 0.2, max: 4, step: 0.1 },
    { key: "linkOpacity", label: "Link opacity", type: "range", min: 0.05, max: 1, step: 0.05 },
    { key: "arrows", label: "Arrows", type: "check" },
    { key: "labels", label: "Always show labels", type: "check" },
    { key: "textFade", label: "Labels appear at zoom", type: "range", min: 0.3, max: 4, step: 0.1 },
    { key: "highlight", label: "Dim unrelated notes on hover", type: "check" },
  ] },
  { section: "Forces", controls: [
    { key: "center", label: "Center force", type: "range", min: 0, max: 0.2, step: 0.005 },
    { key: "repel", label: "Repel force", type: "range", min: 0, max: 600, step: 10 },
    { key: "linkStrength", label: "Link force", type: "range", min: 0, max: 2, step: 0.05 },
    { key: "linkDistance", label: "Link distance", type: "range", min: 10, max: 300, step: 5 },
  ] },
];

// Per-browser convenience only: storage may be blocked, so every access is guarded.
function loadGraphSettings() {
  try {
    const saved = JSON.parse(localStorage.getItem(GRAPH_SETTINGS_KEY) || "{}");
    return Object.fromEntries(Object.entries(saved).filter(([key, value]) => key in GRAPH_DEFAULTS && typeof value === typeof GRAPH_DEFAULTS[key]));
  } catch {
    return {};
  }
}

function changeGraph(changes) {
  state.graphView.setSettings(changes);
  try {
    localStorage.setItem(GRAPH_SETTINGS_KEY, JSON.stringify(state.graphView.settings));
  } catch {
    /* not remembered; the graph still changes */
  }
}

function buildGraphOptions() {
  const panel = byId("graph-options");
  const toggle = byId("graph-options-toggle");
  const inputs = {};
  const shown = (control, value) => control.type === "range" ? String(Number(value.toFixed(3))) : "";
  const sync = () => {
    const settings = state.graphView.settings;
    for (const [key, { control, input, output }] of Object.entries(inputs)) {
      if (control.type === "check") input.checked = settings[key]; else input.value = settings[key];
      if (output) output.textContent = shown(control, settings[key]);
    }
    inputs.depth.input.disabled = !settings.local;
  };
  const sections = GRAPH_OPTIONS.map(({ section, controls }) => element("details", { open: "" },
    element("summary", {}, section),
    ...controls.map((control) => {
      const input = element("input", control.type === "check" ? { type: "checkbox" }
        : control.type === "text" ? { type: "search", placeholder: control.placeholder }
        : { type: "range", min: control.min, max: control.max, step: control.step });
      const output = control.type === "range" ? element("output") : null;
      inputs[control.key] = { control, input, output };
      input.addEventListener("input", () => {
        const value = control.type === "check" ? input.checked : control.type === "range" ? Number(input.value) : input.value;
        changeGraph({ [control.key]: value });
        sync();
      });
      return element("label", { class: `option ${control.type}` },
        control.type === "check" ? input : null,
        element("span", {}, control.label, output ? " " : null, output),
        control.type === "check" ? null : input);
    }),
  ));
  const reset = element("button", { type: "button", class: "ghost small", onclick: () => {
    changeGraph({ ...GRAPH_DEFAULTS });
    sync();
    renderLegend(state.graph.groups);
  } }, "Reset to defaults");
  panel.replaceChildren(...sections, element("div", { class: "option-actions" }, reset));
  sync();
  toggle.addEventListener("click", () => {
    panel.hidden = !panel.hidden;
    toggle.setAttribute("aria-expanded", String(!panel.hidden));
    toggle.classList.toggle("active", !panel.hidden);
    state.graphView.setInsetRight(panel.hidden ? 0 : panel.offsetWidth + 24);
  });
}

async function openNote(path) {
  try {
    const note = await api(`/api/note?path=${encodeURIComponent(path)}`);
    state.notePath = note.path;
    byId("note-empty").hidden = true;
    byId("note").hidden = false;
    byId("note-path").textContent = note.path;
    byId("note-tags").replaceChildren(
      ...note.tags.map((tag) => element("span", { class: "chip" }, `#${tag}`)),
      ...(note.protected ? [element("span", { class: "chip lock", title: "Changes only through an approved SYNAPSE commit" }, "protected")] : []),
    );
    const body = byId("note-body");
    body.innerHTML = note.html; // server-rendered, raw HTML disabled
    for (const link of body.querySelectorAll("a[href]")) {
      if (/^https?:/i.test(link.getAttribute("href"))) { link.target = "_blank"; link.rel = "noopener noreferrer"; }
    }
    const list = (paths) => paths.length
      ? paths.map((notePath) => element("li", {}, element("a", { href: `#note=${encodeURIComponent(notePath)}` }, notePath)))
      : [element("li", { class: "muted small" }, "none")];
    byId("note-out").replaceChildren(...list(note.outgoing));
    byId("note-in").replaceChildren(...list(note.backlinks));
    byId("note-panel").scrollTop = 0;
    if (state.graphView) state.graphView.select(note.path);
  } catch (error) {
    reportError(error);
  }
}

document.addEventListener("click", (event) => {
  const link = event.target.closest("a[href^='#note='], a[href^='#missing=']");
  if (!link) return;
  event.preventDefault();
  const [kind, value] = link.getAttribute("href").slice(1).split("=");
  const target = decodeURIComponent(value);
  if (kind === "missing") return toast(`No note named "${target}" yet.`, true);
  openNote(target);
  if (state.graphView && state.tab === "brain") state.graphView.focus(target);
});

byId("note-close").addEventListener("click", () => {
  byId("note").hidden = true;
  byId("note-empty").hidden = false;
  state.notePath = null;
  if (state.graphView) state.graphView.select(null);
});

byId("graph-search").addEventListener("change", (event) => {
  const query = event.target.value.trim().toLowerCase();
  if (!query || !state.graph) return;
  const nodes = state.graph.nodes;
  const hit = nodes.find((node) => node.id.toLowerCase() === query) || nodes.find((node) => node.id.toLowerCase().includes(query));
  if (!hit) return toast("No matching note", true);
  state.graphView.focus(hit.id);
  openNote(hit.id);
});

/* ---------- synapse ---------- */

async function loadSynapse() {
  const [data] = await Promise.all([api("/api/synapse"), refreshStatus()]);
  byId("syn-path").textContent = state.status.synapse_path;
  byId("eng-path").textContent = state.status.engram_path;
  byId("count-pending").textContent = `(${data.pending.length})`;
  byId("count-approved").textContent = `(${data.approved.length})`;
  const none = (text) => [element("div", { class: "none" }, text)];
  byId("list-pending").replaceChildren(...(data.pending.length ? data.pending.map((entry) => entryCard(entry, false)) : none("Nothing waiting for review.")));
  byId("list-approved").replaceChildren(...(data.approved.length ? data.approved.map((entry) => entryCard(entry, true)) : none("Nothing waiting to be written.")));
  byId("list-trail").replaceChildren(...(data.trail.length
    ? data.trail.map((entry) => element("div", { class: entry.status }, entry.raw.replace(/^- \[[x-]\] /i, "")))
    : none("No history yet.")));
}

function entryCard(entry, approved) {
  const actions = element("div", { class: "entry-actions" });
  const messages = {
    approve: `${entry.id} approved: your AI writes it on the next brain load.`,
    unapprove: `${entry.id} sent back to review.`,
    reject: `${entry.id} rejected and recorded in ENGRAM.`,
  };
  const act = async (action, body) => {
    try {
      await api(`/api/synapse/${encodeURIComponent(entry.id)}/${action}`, { method: "POST", body });
      toast(messages[action]);
      await loadSynapse();
    } catch (error) {
      reportError(error);
    }
  };
  const rejectFlow = () => {
    const reason = element("input", { placeholder: "Why? (recorded so it isn't proposed again)", maxlength: 300 });
    const confirmButton = element("button", { class: "danger", onclick: () => reason.value.trim() ? act("reject", { reason: reason.value.trim() }) : reason.focus() }, "Reject");
    reason.addEventListener("keydown", (event) => { if (event.key === "Enter") confirmButton.click(); });
    actions.replaceChildren(reason, confirmButton, element("button", { class: "ghost", onclick: () => actions.replaceChildren(...buttons()) }, "Cancel"));
    reason.focus();
  };
  const buttons = () => approved
    ? [element("button", { onclick: () => act("unapprove") }, "Send back"), element("button", { class: "danger ghost", onclick: rejectFlow }, "Reject")]
    : [element("button", { class: "primary", onclick: () => act("approve") }, "Approve"), element("button", { class: "danger", onclick: rejectFlow }, "Reject")];
  if (state.role !== "guest") actions.replaceChildren(...buttons());
  return element("div", { class: `entry${approved ? " approved" : ""}` },
    element("div", { class: "entry-top" },
      element("span", { class: "entry-id" }, entry.id),
      entry.target ? element("span", { class: "entry-target" }, entry.target.replace(/^→\s*/, "→ ")) : null,
      element("span", { class: "muted small" }, [entry.date, entry.source].filter(Boolean).join(" · ")),
    ),
    element("div", { class: "entry-change" }, entry.change),
    entry.why && entry.why !== "-" ? element("div", { class: "entry-why" }, `Why: ${entry.why}`) : null,
    actions,
  );
}

/* ---------- connect ---------- */

async function loadConnect() {
  const [{ keys }] = await Promise.all([api("/api/keys"), refreshStatus()]);
  const rows = byId("keys-table").querySelector("tbody");
  rows.replaceChildren(...(keys.length ? keys.map((key) => element("tr", {},
    element("td", {}, key.label),
    element("td", { class: "mono" }, key.hint),
    element("td", {}, when(key.created)),
    element("td", {}, when(key.last_used)),
    element("td", {}, element("button", { class: "danger small", onclick: () => revokeKey(key) }, "Revoke")),
  )) : [element("tr", {}, element("td", { colspan: 5, class: "muted" }, "No keys yet. Create one per client."))]));
  renderSnippets();
}

function renderSnippets() {
  const url = `${state.status.public_url}/mcp`;
  const key = state.key || "<YOUR_CORTEX_KEY>";
  byId("snip-cli").textContent = `claude mcp add --transport http --scope user cortex ${url} \\\n  --header "Authorization: Bearer ${key}"`;
  byId("snip-json").textContent = JSON.stringify({ mcpServers: { cortex: { type: "http", url, headers: { Authorization: "Bearer ${CORTEX_API_KEY}" } } } }, null, 2);
  byId("snip-desktop").textContent = JSON.stringify({ mcpServers: { cortex: { command: "npx", args: ["-y", "mcp-remote", url, "--header", "Authorization:${CORTEX_AUTH}"], env: { CORTEX_AUTH: `Bearer ${key}` } } } }, null, 2);
  byId("snip-claude-ai").textContent = `Name:                   Cortex\nRemote MCP server URL:  ${url}\nRequest header name:    x-auth-token\nRequest header value:   ${key}`;
}

byId("key-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    const { key } = await api("/api/keys", { method: "POST", body: { label: byId("key-label").value } });
    state.key = key;
    byId("key-raw").textContent = key;
    byId("key-new").hidden = false;
    byId("key-label").value = "";
    await loadConnect();
  } catch (error) {
    reportError(error);
  }
});

async function revokeKey(key) {
  if (!confirm(`Revoke "${key.label}"? That client loses access immediately.`)) return;
  try {
    await api(`/api/keys/${encodeURIComponent(key.id)}`, { method: "DELETE" });
    toast(`Revoked ${key.label}`);
    await loadConnect();
  } catch (error) {
    reportError(error);
  }
}

document.addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-copy]");
  if (!button) return;
  const text = byId(button.dataset.copy).textContent;
  try {
    await navigator.clipboard.writeText(text);
  } catch (_) {
    // Plain-HTTP pages (a LAN address) have no clipboard API: fall back to a selection copy.
    const area = element("textarea");
    area.value = text;
    document.body.append(area);
    area.select();
    document.execCommand("copy");
    area.remove();
  }
  button.textContent = "Copied";
  setTimeout(() => (button.textContent = "Copy"), 1500);
});

/* ---------- activity ---------- */

async function loadActivity() {
  const { events } = await api("/api/activity");
  byId("activity-table").querySelector("tbody").replaceChildren(...(events.length ? events.map((event) => element("tr", { class: event.level && event.level !== "INFO" ? "warn" : null },
    element("td", { title: `${event.ts} · request ${event.request_id}` }, when(event.ts)),
    element("td", {}, event.who),
    element("td", {}, event.action),
    element("td", { class: "detail mono" }, event.detail),
  )) : [element("tr", {}, element("td", { colspan: 4, class: "muted" }, "Nothing yet."))]));
}

/* ---------- settings ---------- */

async function loadSettings() {
  const status = await refreshStatus();
  byId("set-dir").textContent = status.brain_dir;
  byId("set-cortex").value = status.cortex_path;
  byId("set-synapse").value = status.synapse_path;
  byId("set-engram").value = status.engram_path;
  byId("set-protected").value = (status.protected || []).join("\n");
}

byId("settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/api/settings", { method: "POST", body: {
      cortex_path: byId("set-cortex").value.trim(),
      synapse_path: byId("set-synapse").value.trim() || null,
      engram_path: byId("set-engram").value.trim() || null,
      protected: byId("set-protected").value.split("\n").map((line) => line.trim()).filter(Boolean),
    } });
    toast("Settings saved");
    await loadSettings();
  } catch (error) {
    reportError(error);
  }
});

byId("rerun-setup").addEventListener("click", openSetup);

/* ---------- setup ---------- */

let setupOpening = false;
async function openSetup() {
  if (setupOpening) return;
  setupOpening = true;
  try {
    show("setup");
    const scan = await api("/api/setup");
    byId("setup-dir").textContent = scan.brain_dir;
    byId("setup-repo").textContent = scan.template_repo;
    byId("setup-found").hidden = !scan.candidates.length;
    byId("setup-candidates").replaceChildren(...scan.candidates.map((path, index) =>
      element("label", {}, element("input", { type: "radio", name: "cortex-candidate", value: path, checked: index === 0 }), element("code", {}, path))));
  } finally {
    setupOpening = false;
  }
}

function setupBusy(busy, message = "Working…") {
  byId("setup-busy").textContent = message;
  byId("setup-busy").hidden = !busy;
  for (const button of byId("view-setup").querySelectorAll("button")) button.disabled = busy;
}

async function waitForSetupJob() {
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, SETUP_POLL_MS));
    const job = await api("/api/setup/job");
    if (job.status !== "running") return job;
  }
}

async function runSetup(body) {
  byId("setup-error").hidden = true;
  setupBusy(true, body.mode === "template" ? "Downloading the template from GitHub…" : "Working…");
  try {
    let job = await api("/api/setup", { method: "POST", body });
    if (job.status === "running") job = await waitForSetupJob();
    if (job.status === "failed") throw new Error(job.error);
    toast("Brain connected");
    await boot();
  } catch (error) {
    byId("setup-error").textContent = error.message;
    byId("setup-error").hidden = false;
  } finally {
    setupBusy(false);
  }
}

byId("setup-use").addEventListener("click", () => {
  const picked = document.querySelector("input[name='cortex-candidate']:checked");
  if (picked) runSetup({ mode: "existing", cortex_path: picked.value });
});
byId("setup-template").addEventListener("click", () => runSetup({ mode: "template" }));
byId("setup-blank").addEventListener("click", () => runSetup({ mode: "blank" }));
byId("setup-custom").addEventListener("click", () => {
  const path = byId("setup-path").value.trim();
  if (path) runSetup({ mode: "existing", cortex_path: path });
});

/* ---------- live updates ---------- */

setInterval(() => {
  if (!byId("view-app").hidden && document.visibilityState === "visible") refreshStatus().catch(() => {});
}, STATUS_POLL_MS);

boot().catch(reportError);

"use strict";

const $ = (sel, el = document) => el.querySelector(sel);

const CELL = 36;                    // base tile size in px (matches --cell in app.css)
const MIN_ZOOM = 0.3, MAX_ZOOM = 1.8;
const FRAME = CELL * 0.28 * 2;      // map border, both sides

/* Rotating prompt-bar hints per scene kind: slash commands first, plain language after. */
const HINTS = {
  world: ["/newmap Ashfall Hold dungeon 40 30", "/move Port Valen 12 20", "/town Valesom near Highmark", "/island Isle of Crows west", "/road from Port Valen to Highmark", "/expand east 16",
          "/army 2nd Crown Legion near Highmark", "/rumor DC 15 The king is dying", "Add a town named Valesom in the east"],
  region: ["/town Pinewick near the bridge", "/ruins Old Harrowgate north", "/road from the mine to the bridge",
           "/beast Graymane Pack near the river", "/dungeon The Hollow Deep east", "Add a mountain pass to the east"],
  exterior: ["/building tavern The Black Pearl near the square", "/building shop Ashby's Goods near the well", "/npc Pip | Peddler | sells stolen maps",
             "/road to the north gate", "/expand north 8", "Add an alchemist shop near the square"],
  room: ["/npc Wren | Bard | stole the lute she plays", "/rumor DC 12 The cellar door is never locked", "/beast Cellar Rat near the barrels",
         "Add a nervous bard called Wren by the hearth"],
  dungeon: ["/npc The Drowned Warden | Guardian", "/beast Ghoul near the altar", "/rumor DC 14 The walls whisper at night"],
};
const GLYPHS = { site: "✦", camp: "▲", temple: "✚", ruins: "✧", bfloor: "🏠", port: "⚓", capital: "♛", marches: "⚑", citadel: "☠", mine: "⛏", tower: "♜", town: "⌂", tavern: "🍺", door: "🚪", gate: "🚪", landmark: "📍" };
const KIND_LABEL = { bfloor: "Building", capital: "Capital city", landmark: "Landmark", door: "Doorway", gate: "Gateway", tavern: "Building", tower: "Tower", edge: "Border crossing" };

const state = {
  players: 0, lan: null, proposal: null, pick: null, repo: null, idrag: null, scenes: [], cidr: null, subnet: null, nodes: [], trail: [],
  selectedId: null, selectedItem: null, flashId: null,
  drag: null, pan: null, suppressClick: false,
  pollTimer: null, tab: "map", busy: false,
  view: { s: 1, tx: 0, ty: 0, touched: false, for: null },
};

const mapEl = $("#map");
const stageEl = $("#stage");
const panEl = $("#pan");
const dossierEl = $("#dossier");
const ghost = document.createElement("div");
ghost.className = "ghost";
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

/* ---------------- helpers ---------------- */
const CLIENT_ID = Math.random().toString(36).slice(2, 10);   // lets this page ignore the live events its own actions cause

async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json", "X-Client-Id": CLIENT_ID }, ...opts });
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (_) {}
    if (Array.isArray(msg)) msg = msg.map((m) => `${(m.loc || []).slice(1).join(".")}: ${m.msg}`).join("; ");   // pydantic validation list
    throw new Error(typeof msg === "string" ? msg : "Request failed");
  }
  return res.json();
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

let toastTimer;
function toast(msg, warn = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("warn", warn);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}

const tokens = () => state.nodes.filter((n) => n.type !== "rumor" && n.x >= 0);
const byId = (id) => state.nodes.find((n) => n.id === id);
const initials = (name) => name.split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();
const shortLabel = (label) => (label || "").split(" — ")[0];

function blockedSet() {
  const s = new Set();
  for (const it of state.subnet.layout) {          // later items win: painted land carves blocking beneath it
    const op = it.block ? "add" : it.carve ? "delete" : null;
    if (!op) continue;
    for (let dx = 0; dx < it.w; dx++) for (let dy = 0; dy < it.h; dy++) s[op](`${it.x + dx},${it.y + dy}`);
  }
  return s;
}

function cellFree(x, y, selfId) {
  const { cols, rows } = state.subnet;
  if (x < 0 || y < 0 || x >= cols || y >= rows) return false;
  if (blockedSet().has(`${x},${y}`)) return false;
  return !tokens().some((n) => n.id !== selfId && n.x === x && n.y === y);
}

/* ---------------- viewport: pan + wheel zoom ---------------- */
const view = state.view;

function applyView() {
  panEl.style.transform = `translate(${view.tx}px, ${view.ty}px) scale(${view.s})`;
  mapEl.style.setProperty("--zoom", view.s.toFixed(3));
  $("#zoom-label").textContent = Math.round(view.s * 100) + "%";
}

function smoothly(fn) {
  panEl.classList.add("smooth");
  fn();
  setTimeout(() => panEl.classList.remove("smooth"), 320);
}

function fitView(animate = false) {
  if (!state.subnet) return;
  const w = state.subnet.cols * CELL + FRAME, h = state.subnet.rows * CELL + FRAME;
  const vw = stageEl.clientWidth, vh = stageEl.clientHeight;
  const s = clamp(Math.min((vw - 60) / w, (vh - 60) / h), MIN_ZOOM, MAX_ZOOM);
  const apply = () => { view.s = s; view.tx = (vw - w * s) / 2; view.ty = (vh - h * s) / 2; applyView(); };
  animate ? smoothly(apply) : apply();
  view.touched = false;
}

function zoomAt(factor, cx, cy, animate = false) {
  const ns = clamp(view.s * factor, MIN_ZOOM, MAX_ZOOM);
  if (ns === view.s) return;
  const apply = () => {
    view.tx = cx - (cx - view.tx) * (ns / view.s);
    view.ty = cy - (cy - view.ty) * (ns / view.s);
    view.s = ns;
    applyView();
  };
  animate ? smoothly(apply) : apply();
  view.touched = true;
}

function zoomCenter(factor) { zoomAt(factor, stageEl.clientWidth / 2, stageEl.clientHeight / 2, true); }

stageEl.addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = stageEl.getBoundingClientRect();
  zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0016)), e.clientX - r.left, e.clientY - r.top);
}, { passive: false });

stageEl.addEventListener("pointerdown", (e) => {
  if (e.target.closest(".zoom-ctl") || e.target.closest(".token")) return;
  if (e.button !== 0 && e.button !== 1) return;
  if (e.button === 1) e.preventDefault();          // no middle-click autoscroll
  state.pan = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty, moved: false };
  window.addEventListener("pointermove", onPanMove);
  window.addEventListener("pointerup", onPanEnd);
  window.addEventListener("pointercancel", onPanEnd);
});

function onPanMove(e) {
  const p = state.pan;
  if (!p) return;
  const dx = e.clientX - p.x, dy = e.clientY - p.y;
  if (!p.moved && Math.hypot(dx, dy) > 4) { p.moved = true; stageEl.classList.add("panning"); }
  if (p.moved) { view.tx = p.tx + dx; view.ty = p.ty + dy; view.touched = true; applyView(); }
}

function onPanEnd() {
  const p = state.pan;
  state.pan = null;
  window.removeEventListener("pointermove", onPanMove);
  window.removeEventListener("pointerup", onPanEnd);
  window.removeEventListener("pointercancel", onPanEnd);
  stageEl.classList.remove("panning");
  if (p && p.moved) { state.suppressClick = true; setTimeout(() => { state.suppressClick = false; }, 0); }
}

stageEl.addEventListener("click", (e) => {            // click on bare terrain dismisses the card
  if (state.suppressClick) return;
  if (e.target.closest(".zoom-ctl")) return;
  if (state.pick) { if (!e.target.closest(".token")) handlePick(e); return; }
  if (state.repo) { if (!e.target.closest(".token")) placeFromEvent(e); return; }
  if (e.target.closest(".token, .item.linkable")) return;
  closeDossier();
});

/* Pan (never zoom) so a tile is comfortably on screen; only moves when it is off-screen or near the edge. */
function focusTile(x, y) {
  const sx = view.tx + (FRAME / 2 + (x + 0.5) * CELL) * view.s;
  const sy = view.ty + (FRAME / 2 + (y + 0.5) * CELL) * view.s;
  const vw = stageEl.clientWidth, vh = stageEl.clientHeight, m = 120;
  if (sx > m && sx < vw - m - 360 && sy > m && sy < vh - m) return;     // 360 = inspection card width
  smoothly(() => {
    view.tx += (vw - 360) / 2 - sx;
    view.ty += vh / 2 - sy;
    view.touched = true;
    applyView();
  });
}

/* After the map grows north/west its origin moves; shift the view so the existing content stays put. */
function holdViewAfterGrowth(added) {
  if (!added) return;
  view.tx -= (added.w || 0) * CELL * view.s;
  view.ty -= (added.n || 0) * CELL * view.s;
  applyView();
}

const EXPAND_STEP = 8;
const expandMenu = $("#expand-menu");
const expandBtn = $("#zoom-expand");

function buildExpandMenu() {
  expandMenu.replaceChildren();
  const opts = [["north", "⬆ +8 tiles North"], ["south", "⬇ +8 tiles South"], ["east", "➡ +8 tiles East"],
                ["west", "⬅ +8 tiles West"], ["all", "✛ +8 tiles on All Sides"]];
  for (const [dir, label] of opts) {
    const b = el("button", "", label);
    b.type = "button";
    b.addEventListener("click", () => { closeExpandMenu(); expandMap(dir); });
    expandMenu.append(b);
  }
}

function openExpandMenu() { expandMenu.hidden = false; expandBtn.setAttribute("aria-expanded", "true"); }
function closeExpandMenu() { expandMenu.hidden = true; expandBtn.setAttribute("aria-expanded", "false"); }

async function expandMap(dir) {
  const { cols, rows } = state.subnet;
  const body = { subnet: state.cidr, direction: dir };
  if (dir === "all") { body.width = cols + EXPAND_STEP * 2; body.height = rows + EXPAND_STEP * 2; }
  else if (dir === "east" || dir === "west") body.width = cols + EXPAND_STEP;
  else body.height = rows + EXPAND_STEP;
  try {
    const r = await api("/api/scene/resize", { method: "POST", body: JSON.stringify(body) });
    holdViewAfterGrowth(r.added);
    await loadScene();
    if (!view.touched) fitView(true);
    toast(`${r.message} (+${EXPAND_STEP} ${dir === "all" ? "on every side" : dir})`);
  } catch (e) { toast(e.message, true); }
}

expandBtn.addEventListener("click", (e) => { e.stopPropagation(); expandMenu.hidden ? openExpandMenu() : closeExpandMenu(); });
document.addEventListener("click", (e) => { if (!e.target.closest("#expand-menu, #zoom-expand")) closeExpandMenu(); });
buildExpandMenu();

$("#zoom-in").addEventListener("click", () => zoomCenter(1.25));
$("#zoom-out").addEventListener("click", () => zoomCenter(0.8));
$("#zoom-fit").addEventListener("click", () => fitView(true));

/* ---------------- scenes, tabs, breadcrumb ---------------- */
async function loadScenes() {
  state.scenes = (await api("/api/scenes")).scenes;
  renderSceneTabs();
}

function renderSceneTabs() {
  const box = $("#scene-tabs");
  box.replaceChildren();
  for (const sc of state.scenes) {
    const on = state.tab === "map" && sc.cidr === state.cidr;
    const b = el("button", "tab scene" + (on ? " active" : ""));
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", on);
    b.title = `${sc.name} — ${sc.tokens} token${sc.tokens === 1 ? "" : "s"}`;
    b.append(el("span", "icon", sc.icon), document.createTextNode(" " + sc.short));
    b.addEventListener("click", () => { showTab("map"); if (sc.cidr !== state.cidr) switchScene(sc.cidr); });
    box.append(b);
  }
}

function renderCrumbs() {
  const nav = $("#crumbs");
  nav.replaceChildren();
  const here = state.trail.findIndex((c) => c.cidr === state.cidr);
  state.trail.forEach((c, i) => {
    if (i) nav.append(el("span", "sep", ">"));
    const b = el("button", "crumb");
    b.append(el("span", "icon", c.icon), document.createTextNode(" " + c.name));
    if (c.cidr === state.cidr) b.setAttribute("aria-current", "location");
    else b.addEventListener("click", () => { showTab("map"); switchScene(c.cidr, { dir: i > here ? "in" : "out" }); });
    nav.append(b);
  });
}

/* Fast fade-through to another scene. opts: dir ("in"|"out"), keepSelection, flashId */
async function switchScene(cidr, opts = {}) {
  if (state.drag || state.busy) return;
  const changing = cidr !== state.cidr;
  if (changing) { cancelBlueprint(true); endPick(true); if (state.repo) { state.repo = null; stageEl.classList.remove("repositioning"); } }
  if (changing) {
    let deeper = opts.dir ? opts.dir === "in" : null;
    if (deeper === null) {
      const order = state.scenes.map((sc) => sc.cidr);
      deeper = order.indexOf(cidr) > order.indexOf(state.cidr);
    }
    mapEl.classList.toggle("zoom-in", deeper);
    mapEl.classList.toggle("zoom-out", !deeper);
    mapEl.classList.add("leaving");
    await sleep(160);
  }
  if (!opts.keepSelection) { state.selectedId = null; state.selectedItem = null; }
  state.flashId = opts.flashId ?? null;
  try {
    await loadScene(cidr);
    await loadScenes();
  } catch (e) {
    toast(e.message, true);
  } finally {
    mapEl.classList.remove("leaving", "zoom-in", "zoom-out");
  }
}

/* ---------------- scene loading ---------------- */
async function loadScene(cidr = state.cidr) {
  const url = cidr ? `/api/scene?subnet=${encodeURIComponent(cidr)}` : "/api/scene";
  const data = await api(url);
  state.subnet = data.subnet;
  state.cidr = data.subnet.cidr;
  state.trail = data.subnet.trail || [];
  state.nodes = data.nodes;
  if (!state.drag) renderAll();
  schedulePoll();
}

function schedulePoll() {
  clearTimeout(state.pollTimer);
  if (state.nodes.some((n) => n.portrait_status === "pending")) {
    state.pollTimer = setTimeout(() => loadScene().catch(() => {}), 3000);
  }
}

async function loadHealth() {
  try {
    const h = await api("/api/health");
    const box = $("#status");
    box.replaceChildren();
    const mk = (label, val, title) => { const d = el("span"); if (title) d.title = title; d.append(label + " "); d.append(el("b", "", val)); return d; };
    const m = h.model || {};
    const parser = mk("parser", h.engine, m.error ? `Local model not in use: ${m.error}. Click to retry.` : (m.present ? "" : `Model file not found. Put it at ${m.path} and click to retry.`));
    if (!m.present || m.state !== "ready") {
      parser.classList.add("clickable");
      parser.addEventListener("click", async () => {
        toast("Looking for the local model…");
        try { const st = await api("/api/model/reload", { method: "POST" }); toast(st.state === "ready" ? "Local model loaded." : "Still using the rule-based parser (no model file yet).", st.state !== "ready"); }
        catch (e) { toast(e.message, true); }
        loadHealth();
      });
    }
    box.append(parser, mk("portraits", h.images ? "on" : "off"), mk("db", h.journal_mode));
  } catch (_) {}
}

/* ---------------- rendering ---------------- */
function renderAll() {
  const kind = state.subnet.kind;
  for (const k of ["exterior", "region", "world", "dungeon"]) mapEl.classList.toggle(k, kind === k);
  stageEl.dataset.kind = kind;
  const bg = state.subnet.bg;
  mapEl.classList.toggle("has-bg", !!bg);
  mapEl.style.setProperty("--grid-op", bg ? bg.opacity : 0.35);
  mapEl.style.setProperty("--cols", state.subnet.cols);
  mapEl.style.setProperty("--rows", state.subnet.rows);
  mapEl.setAttribute("aria-label", state.subnet.name);
  setHintKind(kind);
  if (view.for !== state.cidr) { view.for = state.cidr; fitView(false); focusArrival(); }
  renderMap();
  renderSceneTools();
  renderRoster();
  renderRumors();
  renderSceneTabs();
  renderCrumbs();
  if (state.selectedItem !== null && state.subnet.layout[state.selectedItem]) renderItemDossier(state.selectedItem);
  else if (state.selectedId && byId(state.selectedId)) renderDossier();
  else closeDossier();
}

function renderMap() {
  mapEl.replaceChildren();
  const bg = state.subnet.bg;
  if (bg) {                                           // imported battle map under a snapping grid overlay
    const img = el("div", "mapbg");
    img.style.backgroundImage = `url("${bg.url}")`;
    img.style.left = `calc(var(--cell) * ${bg.ox})`;
    img.style.top = `calc(var(--cell) * ${bg.oy})`;
    img.style.width = `calc(var(--cell) * ${bg.cols})`;
    img.style.height = `calc(var(--cell) * ${bg.rows})`;
    mapEl.append(img, el("div", "gridlines"));
  }
  state.subnet.layout.forEach((it, i) => {
    const linkable = !!it.link || it.kind === "landmark" || it.kind === "capital";
    const d = el("div", `item kind-${it.kind} tile-${it.kind}` + (it.variant ? ` lm-${it.variant}` : "") +
      (it.portal ? " portal" : "") + (it.kind === "border" && it.h > it.w ? " vert" : "") +
      (linkable ? " linkable" : "") + (linkable || it.portal ? " keep" : "") + (state.selectedItem === i ? " selected-item" : "") +
      (state.repo && state.repo.index === i ? " repositioning" : ""));
    if (it.block) d.dataset.block = "1";
    d.style.left = `calc(var(--cell) * ${it.x})`;
    d.style.top = `calc(var(--cell) * ${it.y})`;
    d.style.width = `calc(var(--cell) * ${it.w})`;
    d.style.height = `calc(var(--cell) * ${it.h})`;
    if (it.label && it.kind !== "areaname") d.title = shortLabel(it.label);
    if (["landmark", "capital", "areaname"].includes(it.kind) && it.label) d.dataset.label = shortLabel(it.label);
    if (linkable) {
      d.addEventListener("click", (e) => {
        if (state.suppressClick) return;
        e.stopPropagation();
        if (state.pick) return handlePick(e);
        if (state.repo) return state.repo.index === i ? undefined : placeFromEvent(e);
        selectItem(i);
      });
    }
    if (state.repo && state.repo.index === i) d.addEventListener("pointerdown", (e) => startPinDrag(e, i, it, d));
    mapEl.append(d);
  });
  const arr = state.subnet.arrival;
  if (arr) {                                           // GM-only marker for the scene's arrival tile
    const pin = el("div", "arrival-pin", "⚑");
    pin.title = `Arrival tile ${arr.x},${arr.y}: tokens entering this scene land here`;
    pin.style.left = `calc(var(--cell) * ${arr.x})`;
    pin.style.top = `calc(var(--cell) * ${arr.y})`;
    mapEl.append(pin);
  }
  mapEl.append(ghost);
  for (const n of tokens()) mapEl.append(buildToken(n));
  state.flashId = null;
}

function buildToken(n) {
  const t = el("div", `token type-${n.type}`);
  t.dataset.id = n.id;
  t.style.left = `calc(var(--cell) * ${n.x})`;
  t.style.top = `calc(var(--cell) * ${n.y})`;
  if (n.id === state.selectedId) t.classList.add("selected");
  if (n.portrait_status === "pending") t.classList.add("pending");
  if (n.id === state.flashId) t.classList.add("arrive");
  const face = el("div", "face", n.portrait ? "" : initials(n.name));
  if (n.portrait) face.style.backgroundImage = `url("${n.portrait}")`;
  t.append(face, el("div", "plate", n.name));
  t.addEventListener("pointerdown", (e) => startDrag(e, n, t));
  return t;
}

function portraitStyle(node, target) {
  if (node.portrait) { target.style.backgroundImage = `url("${node.portrait}")`; target.textContent = ""; }
  else target.textContent = initials(node.name);
}

const RING = { pc: "#7ec8ff", barkeep: "#f39c12", guard: "#8fa6bd", patron: "#9bb078", npc: "#b58bd6", party: "#f7d774", beast: "#c0583a", army: "#d8b04a" };

function renderRoster() {
  const box = $("#roster");
  box.replaceChildren();
  const list = tokens();
  if (!list.length) { box.append(el("div", "empty", `No one is in ${state.subnet.short} yet. Type a character into the prompt bar.`)); return; }
  for (const n of list) {
    const c = el("button", "card");
    c.style.setProperty("--ring", RING[n.type] || "#7a5b34");
    const mini = el("div", "mini");
    portraitStyle(n, mini);
    c.append(mini, el("strong", "", n.name), el("small", "", `${n.state.role || n.type} · ${n.ip}`));
    c.addEventListener("click", () => selectNode(n.id));
    box.append(c);
  }
}

function renderRumors() {
  const box = $("#rumors");
  box.replaceChildren();
  const list = state.nodes.filter((n) => n.type === "rumor");
  if (!list.length) { box.append(el("div", "empty", `No rumors logged for ${state.subnet.short}. Try: rumor: the miller's cart was found empty.`)); return; }
  for (const n of list) {
    const c = el("div", "card rumor");
    c.append(el("strong", "", n.name), el("span", "dc", `DC ${n.state.dc}`), el("p", "", n.state.secret));
    box.append(c);
  }
}

/* ---------------- inspection cards ---------------- */
function selectNode(id) {
  if (state.repo) stopRepo(true);
  state.selectedId = id;
  state.selectedItem = null;
  renderMap();
  renderDossier();
}

function selectItem(i) {
  if (state.repo && state.repo.index !== i) stopRepo(true);
  state.selectedItem = i;
  state.selectedId = null;
  renderMap();
  renderItemDossier(i);
}

function dossierShell() {
  dossierEl.replaceChildren();
  const close = el("button", "close", "×");
  close.setAttribute("aria-label", "Close card");
  close.addEventListener("click", closeDossier);
  dossierEl.append(close);
}

function openDossier() {
  dossierEl.classList.add("open");
  dossierEl.setAttribute("aria-hidden", "false");
}

function renderItemDossier(i) {
  const it = state.subnet.layout[i];
  if (!it) return closeDossier();
  dossierShell();
  const link = it.link;

  const art = el("div", "portrait glyph", GLYPHS[it.variant] || GLYPHS[it.kind] || "📍");
  const chips = el("div", "chips");
  chips.append(el("span", "chip", KIND_LABEL[it.kind] || "Landmark"), el("span", "chip", `tile ${it.x}, ${it.y}`));
  if (link) chips.append(el("span", "chip", link.exists ? "mapped" : "unmapped"));

  const actions = el("div", "actions");
  if (link && link.exists) {
    const up = link.relation === "up";
    const go = el("button", "btn primary", up ? `⬆ Leave to ${link.name}` : "🚪 Enter Settlement / Delve Inside");
    go.addEventListener("click", () => { showTab("map"); switchScene(link.cidr, { dir: up ? "out" : "in" }); });
    actions.append(go);
  } else if (link) {
    const gen = el("button", "btn primary alt", "➕ Generate Scene / Map");
    gen.addEventListener("click", () => generateScene(it, gen));
    actions.append(gen);
  }

  if (it.kind === "landmark" || it.kind === "capital") {
    const on = !!(state.repo && state.repo.index === i);
    const rp = el("button", "btn toggle", on ? "✥ Done Repositioning" : "✥ Reposition Pin");
    rp.type = "button";
    rp.setAttribute("aria-pressed", String(on));
    rp.title = "Drag the pin, or click a tile, to line it up with your artwork";
    rp.addEventListener("click", () => toggleRepo(i));
    actions.append(rp);
  }

  dossierEl.append(
    art, el("h2", "", shortLabel(it.label) || it.kind), el("p", "role", link ? `Leads to ${link.name}` : "Landmark"), chips,
    el("h3", "", "About"),
    el("p", "", it.desc || "Nothing of note is recorded here yet."),
    ...(link && !link.exists ? [el("p", "note", `${link.name} has no map yet. Generating one adds it to the hierarchy and takes you straight in.`)] : []),
    ...(!link ? [el("p", "note", "Nothing lies beyond this landmark.")] : []),
    ...(state.repo && state.repo.index === i ? [el("p", "note", "Drag the pin, or click the tile where it belongs. Esc or Done stops.")] : []),
    actions
  );
  openDossier();
}

async function generateScene(item, btn) {
  btn.disabled = true;
  btn.textContent = "Generating…";
  try {
    const r = await api("/api/scenes/generate", { method: "POST", body: JSON.stringify({ parent: state.cidr, x: item.x, y: item.y }) });
    await loadScenes();
    toast(r.scene.created ? `${r.scene.name} generated` : `${r.scene.name} already existed`);
    await switchScene(r.scene.cidr, { dir: "in" });
  } catch (e) {
    toast(e.message, true);
    btn.disabled = false;
    btn.textContent = "➕ Generate Scene / Map";
  }
}

function renderDossier() {
  const n = byId(state.selectedId);
  if (!n) return closeDossier();
  dossierShell();

  const portrait = el("div", "portrait");
  portraitStyle(n, portrait);

  const chips = el("div", "chips");
  chips.append(el("span", "chip", n.ip), el("span", "chip", n.type), el("span", "chip", `tile ${n.x}, ${n.y}`));
  if (n.state.disposition) chips.append(el("span", `chip ${n.state.disposition}`, n.state.disposition));

  const secret = el("div", "secret hidden");
  secret.append(el("span", "dc", `DC ${n.state.dc}`), el("div", "text", n.state.secret || "—"), el("div", "hint", "Click to reveal"));
  secret.addEventListener("click", () => secret.classList.toggle("hidden"));

  const actions = el("div", "actions");
  const regen = el("button", "btn", n.portrait_status === "pending" ? "Painting…" : "Paint portrait");
  regen.disabled = n.portrait_status === "pending";
  regen.addEventListener("click", async () => {
    try {
      const r = await api(`/api/nodes/${n.id}/portrait`, { method: "POST" });
      if (!r.queued) return toast(r.reason, true);
      toast("Portrait queued");
      setTimeout(() => loadScene().catch(() => {}), 800);
    } catch (e) { toast(e.message, true); }
  });
  actions.append(regen);

  dossierEl.append(
    portrait, el("h2", "", n.name), el("p", "role", n.state.role || n.type), chips,
    el("h3", "", "Motivation"), el("p", "", n.state.motivation || "Unknown."),
    el("h3", "", "Secret"), secret, actions
  );
  openDossier();
}

function closeDossier() {
  if (state.repo) { state.repo = null; stageEl.classList.remove("repositioning"); }
  for (const t of mapEl.querySelectorAll(".item.repositioning")) t.classList.remove("repositioning");
  state.selectedId = null;
  state.selectedItem = null;
  dossierEl.classList.remove("open");
  dossierEl.setAttribute("aria-hidden", "true");
  for (const t of mapEl.querySelectorAll(".token.selected")) t.classList.remove("selected");
  for (const t of mapEl.querySelectorAll(".item.selected-item")) t.classList.remove("selected-item");
}

/* ---------------- dragging tokens (scale-aware) ---------------- */
function startDrag(e, node, tokenEl) {
  if (e.button !== 0 || state.busy) return;
  e.preventDefault();
  e.stopPropagation();                                // do not start a map pan
  tokenEl.setPointerCapture(e.pointerId);
  state.drag = { node, tokenEl, sx: e.clientX, sy: e.clientY, moved: false, gx: node.x, gy: node.y };
  tokenEl.addEventListener("pointermove", onDragMove);
  tokenEl.addEventListener("pointerup", onDragEnd);
  tokenEl.addEventListener("pointercancel", onDragCancel);
}

function mapPoint(e) {
  const r = mapEl.getBoundingClientRect();          // already scaled by the pan layer
  return { px: (e.clientX - r.left) / view.s - mapEl.clientLeft, py: (e.clientY - r.top) / view.s - mapEl.clientTop };
}

function onDragMove(e) {
  const d = state.drag;
  if (!d) return;
  if (!d.moved && Math.hypot(e.clientX - d.sx, e.clientY - d.sy) > 5) {
    d.moved = true;
    d.tokenEl.classList.add("dragging");
    ghost.style.display = "block";
  }
  if (!d.moved) return;
  const { px, py } = mapPoint(e);
  d.tokenEl.style.left = `${px - CELL / 2}px`;
  d.tokenEl.style.top = `${py - CELL / 2}px`;
  d.gx = Math.floor(px / CELL);
  d.gy = Math.floor(py / CELL);
  ghost.style.left = `${d.gx * CELL}px`;
  ghost.style.top = `${d.gy * CELL}px`;
  ghost.classList.toggle("bad", !cellFree(d.gx, d.gy, d.node.id));
}

function endDragListeners(d) {
  d.tokenEl.removeEventListener("pointermove", onDragMove);
  d.tokenEl.removeEventListener("pointerup", onDragEnd);
  d.tokenEl.removeEventListener("pointercancel", onDragCancel);
  ghost.style.display = "none";
  state.drag = null;
}

async function onDragEnd() {
  const d = state.drag;
  if (!d) return;
  const { node, moved, gx, gy } = d;
  endDragListeners(d);
  if (!moved) return selectNode(node.id);
  if ((gx !== node.x || gy !== node.y) && cellFree(gx, gy, node.id)) {
    try {
      const r = await api("/api/nodes/move", { method: "POST", body: JSON.stringify({ id: node.id, x: gx, y: gy }) });
      if (r.transition) {                             // stepped through a door / landmark / border
        toast(`${node.name} goes through to ${r.transition.short}`);
        state.selectedId = node.id;
        state.selectedItem = null;
        return switchScene(r.transition.cidr, { keepSelection: true, flashId: node.id });
      }
      node.x = r.node.x; node.y = r.node.y;
    } catch (err) { toast(err.message, true); }
  } else if (gx !== node.x || gy !== node.y) {
    toast("That tile is blocked or occupied.", true);
  }
  renderAll();
}

function onDragCancel() {
  const d = state.drag;
  if (!d) return;
  endDragListeners(d);
  renderMap();
}

/* ---------------- view tabs (roster / rumors) ---------------- */
function showTab(name) {
  state.tab = name;
  for (const b of document.querySelectorAll(".tab[data-tab]")) {
    const on = b.dataset.tab === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on);
  }
  for (const v of document.querySelectorAll(".view")) v.classList.toggle("active", v.id === `view-${name}`);
  renderSceneTabs();
  if (name === "map" && !view.touched) requestAnimationFrame(() => fitView(false));
}
document.querySelectorAll(".tab[data-tab]").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

/* ---------------- build results (shared by instant commands and confirmed blueprints) ---------------- */
async function showBuildResult(o) {
  if (o.grown) holdViewAfterGrowth(o.grown);          // north/west growth moves the origin: keep the view in place
  state.selectedId = o.tokenId || null;
  state.selectedItem = null;
  state.flashId = o.tokenId || null;
  await loadScene();
  loadScenes().catch(() => {});
  if (o.grown && !view.touched) fitView(true);
  if (o.rumorsOnly) showTab("rumors"); else if (state.tab !== "map") showTab("map");
  if (o.landmark && !o.rumorsOnly) {                  // a new place is inspectable straight away
    const idx = state.subnet.layout.findIndex((it) => it.link && shortLabel(it.label) === o.landmark && it.kind !== "door");
    if (idx >= 0) selectItem(idx);
  }
  if (o.focus) focusTile(o.focus[0], o.focus[1]);
  toast(o.message);
}

/* ---------------- propose & confirm ---------------- */
const bpEl = $("#blueprint");

function cancelBlueprint(silent = false) {
  if (!state.proposal) return;
  state.proposal = null;
  renderBlueprint();
  if (!silent) $("#prompt").focus();
}

function renderBlueprint() {
  const p = state.proposal;
  bpEl.replaceChildren();
  if (!p) { bpEl.hidden = true; return; }
  const head = el("div", "bp-head");
  head.append(el("h2", "", "Proposed Blueprint"), el("span", "bp-engine", `via ${p.engine}`));
  const list = el("ul", "bp-actions");
  for (const a of p.actions) {
    const li = el("li", a.ok ? "ok" : "bad");
    li.append(el("span", "mark", a.ok ? "✓" : "✕"), el("span", "txt", a.label || a.action));
    list.append(li);
  }
  const buttons = el("div", "bp-buttons");
  const build = el("button", "btn primary", p.busy ? "Building…" : "🔨 Build It");
  build.type = "button";
  build.disabled = !p.ok || p.busy;
  build.addEventListener("click", applyBlueprint);
  const cancel = el("button", "btn neutral", "✕ Cancel");
  cancel.type = "button";
  cancel.disabled = !!p.busy;
  cancel.addEventListener("click", () => cancelBlueprint());
  buttons.append(cancel, build);
  bpEl.append(head, el("p", "bp-summary", p.summary));
  if (p.actions.length) bpEl.append(list);
  if (p.warnings && p.warnings.length) {
    const w = el("ul", "bp-warn");
    for (const t of p.warnings) w.append(el("li", "", t));
    bpEl.append(w);
  }
  if (p.error) bpEl.append(el("p", "bp-error", p.error));
  bpEl.append(el("p", "bp-note", p.ok ? "Nothing has been changed yet. Esc cancels." : "Nothing was built. Rephrase the request, or use a /command."), buttons);
  bpEl.hidden = false;
  if (!p.busy) (p.ok ? build : cancel).focus();
}

async function applyBlueprint() {
  const p = state.proposal;
  if (!p || p.busy || !p.ok) return;
  p.busy = true;
  p.error = "";
  renderBlueprint();
  try {
    const r = await api("/api/blueprint/apply", { method: "POST", body: JSON.stringify({ subnet: p.cidr, actions: p.actions }) });
    state.proposal = null;
    renderBlueprint();
    $("#prompt").value = "";
    const tok = [...r.results].reverse().find((x) => x.node_id);
    await showBuildResult({
      grown: r.expand_added, focus: r.focus, landmark: r.landmark, tokenId: tok ? tok.node_id : null,
      rumorsOnly: r.results.every((x) => x.op === "LOG_RUMOR"), message: r.message,
    });
    loadHealth();
  } catch (err) {
    p.busy = false;
    p.error = err.message;                             // the blueprint is atomic: nothing was changed
    renderBlueprint();
    toast(err.message, true);
  }
}

/* ---------------- prompt bar (routes to the scene in view) ---------------- */
$("#prompt-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = $("#prompt"), btn = $("#prompt-send");
  const text = input.value.trim();
  if (!text || !state.cidr) return;
  if (/^\/(help|\?)\s*$/i.test(text)) { input.value = ""; hideSuggest(); toggleCheat(true); return; }
  hideSuggest();
  const instant = text.startsWith("/");               // slash commands run at once; plain words are proposed first
  btn.disabled = true;
  const label = btn.textContent;
  btn.textContent = instant ? "Running…" : "Thinking…";
  let proposed = false;
  try {
    if (instant) {
      const r = await api("/api/directive", { method: "POST", body: JSON.stringify({ text, subnet: state.cidr }) });
      input.value = "";
      if (r.scene) {                                   // /newmap: jump straight into the new blank map
        await loadScenes();
        toast(`Created ${r.scene.name}${r.scene.pinned ? ", with a pin on the map you came from" : ""}`);
        await switchScene(r.scene.cidr, { dir: "in" });
      } else if (r.change) {
        const ch = r.change;
        await showBuildResult({ grown: ch.kind === "expand" ? ch.added : null, focus: ch.focus, landmark: ch.landmark, message: `${ch.message} (${r.engine})` });
      } else {
        const isRumor = r.node.type === "rumor";
        await showBuildResult({
          tokenId: isRumor ? null : r.node.id, rumorsOnly: isRumor,
          message: isRumor ? `Rumor logged in ${state.subnet.short}: ${r.node.name}` : `${r.node.name} joins ${state.subnet.short} (${r.engine})`,
        });
      }
      loadHealth();
    } else {
      cancelBlueprint(true);
      const r = await api("/api/blueprint/propose", { method: "POST", body: JSON.stringify({ text, subnet: state.cidr }) });
      state.proposal = { ...r.proposal, cidr: state.cidr, text };
      renderBlueprint();
      proposed = true;
      loadHealth();
    }
  } catch (err) {
    toast(err.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = label;
    if (!proposed) input.focus();
  }
});

document.addEventListener("keydown", (e) => {
  const typing = ["INPUT", "TEXTAREA"].includes(document.activeElement.tagName);
  if (e.key === "Escape") {
    if (!bcPop.hidden) { bcPop.hidden = true; return; }
    if (!nmEl.hidden) { closeNewMap(); return; }
    if (state.pick) { endPick(); return; }
    if (state.repo) { stopRepo(); return; }
    if (!importEl.hidden) { closeImport(); return; }
    if (state.proposal) { cancelBlueprint(); return; }          // Esc discards the proposal; the board is untouched
    closeDossier(); closeExpandMenu(); toggleCheat(false); hideSuggest();
  }
  if (typing) return;
  if (e.key === "/") { e.preventDefault(); const p = $("#prompt"); p.focus(); p.value = "/"; refreshSuggest(); }
  if (e.key === "?") { e.preventDefault(); toggleCheat(); }
  if (e.key === "+" || e.key === "=") zoomCenter(1.25);
  if (e.key === "-") zoomCenter(0.8);
  if (e.key === "0") fitView(true);
});

new ResizeObserver(() => { if (state.tab === "map" && !view.touched) fitView(false); }).observe(stageEl);

/* ---------------- slash commands: autocomplete, cheatsheet, rotating hints ---------------- */
// [command, usage, example, what it does, alias?]
const COMMANDS = [
  ["/town", "/town <name> [near <place> | north|south|east|west]", "/town Valesom near Highmark", "Town landmark on cleared ground"],
  ["/city", "/city <name> [near <place> | direction]", "/city Aurelmere south", "Walled city with a gate"],
  ["/castle", "/castle <name> [near <place> | direction]", "/castle Blackthorn east", "Fortified castle"],
  ["/dungeon", "/dungeon <name> [near <place> | direction]", "/dungeon The Hollow Deep", "Entrance to an underground map"],
  ["/ruins", "/ruins <name> [near <place> | direction]", "/ruins Old Harrowgate north", "Ruined site linked to a dungeon map"],
  ["/island", "/island <name> [north|south|east|west]", "/island Isle of Crows west", "Paints a landmass and its landmark"],
  ["/building", "/building <tavern|shop|smithy|temple|house|barracks|guild> <name>", "/building tavern The Black Pearl near the square", "Walled building with a door and interior"],
  ["/road", "/road [from <place>] [to <place>]  |  /road <direction>", "/road from Valesom to Highmark", "Lays road or trail tiles"],
  ["/npc", "/npc <name> [role] [secret]   (or  Name | Role | Secret)", "/npc Mira Vale | Innkeeper | DC 14 skims the till", "A character token, nothing else"],
  ["/beast", "/beast <name> [near <place>]", "/beast Graymane Pack near the river", "Monster token"],
  ["/army", "/army <name> [near <place>]", "/army 2nd Crown Legion near Highmark", "Faction troop token"],
  ["/expand", "/expand <north|south|east|west|all> <tiles>", "/expand east 16", "Grow the map canvas"],
  ["/rumor", "/rumor [DC <n>] <text>", "/rumor DC 15 The king is dying", "Logs a rumor card"],
  ["/newmap", "/newmap <name> [world|region|town|interior|dungeon] [cols] [rows]", "/newmap Ashfall Hold dungeon 40 30", "Blank child map with a pin on this one"],
  ["/move", "/move <landmark name> <x> <y>", "/move Port Valen 12 20", "Reposition a landmark pin (top-left tile)"],
  ["/pc", "/pc <name> [class]", "/pc Ysolde ranger", "A player-character token that players can move"],
  ["/help", "/help   (or press ?)", "", "Open this sheet"],
];
for (const a of ["village", "hamlet", "outpost", "fort", "keep", "citadel", "camp", "temple", "monastery", "lighthouse", "port"]) {
  COMMANDS.push([`/${a}`, `/${a} <name> [near <place> | direction]`, `/${a} Greywatch`, `Same as /town, as a ${a}`, true]);
}
const PLAIN_EXAMPLES = [
  "Add a town named Valesom in the east", "Add a stone tavern named The Black Pearl near the square",
  "Add an island named Isle of Crows in the west sea", "Carve a river running south from the lake",
  "Add a mountain pass to the east", "Expand the realm map east by 16 tiles for a new desert kingdom",
  "Add a wolf pack by the river", "rumor: the miller's cart was found empty",
];

const promptEl = $("#prompt");
const sugEl = $("#cmd-suggest");
const cheatEl = $("#cheatsheet");
const cheatBtn = $("#cheat-btn");
let sugItems = [], sugIdx = -1, sugMode = "none";
let hintKind = "room", hintI = 0;

function setHintKind(kind) {
  if (kind === hintKind && promptEl.dataset.hinted) return;
  hintKind = kind; hintI = 0; showHint();
}
function showHint() {
  const list = HINTS[hintKind] || HINTS.room;
  promptEl.placeholder = `Try  ${list[hintI % list.length]}     (? lists every command)`;
  promptEl.dataset.hinted = "1";
}
setInterval(() => { if (!promptEl.value) { hintI++; showHint(); } }, 5500);

function hideSuggest() { sugEl.hidden = true; sugItems = []; sugIdx = -1; sugMode = "none"; }

function renderSuggest() {
  sugEl.replaceChildren();
  sugItems.forEach((c, i) => {
    const li = el("li", i === sugIdx ? "on" : "");
    li.setAttribute("role", "option");
    li.append(el("code", "", c[0]), el("span", "", c[3]));
    li.addEventListener("mousedown", (e) => { e.preventDefault(); completeCommand(c); });
    sugEl.append(li);
  });
  sugEl.hidden = !sugItems.length;
}

function refreshSuggest() {
  const v = promptEl.value;
  if (!v.startsWith("/")) return hideSuggest();
  const partial = v.match(/^\/(\w*)$/);
  if (partial) {                                      // still typing the command word: offer matches
    const q = "/" + partial[1].toLowerCase();
    sugItems = COMMANDS.filter((c) => c[0].startsWith(q));
    sugMode = "complete";
    if (sugIdx >= sugItems.length) sugIdx = -1;
    return renderSuggest();
  }
  const done = v.match(/^\/(\w+)\s/);                 // command chosen: show its usage line
  const c = done && COMMANDS.find((x) => x[0] === "/" + done[1].toLowerCase());
  if (c) { sugItems = [[c[1], "", "", c[2] ? `e.g. ${c[2]}` : ""]]; sugMode = "usage"; sugIdx = -1; renderSuggest(); sugEl.firstChild.classList.add("usage"); }
  else hideSuggest();
}

function completeCommand(c) {
  promptEl.value = c[0] + " ";
  promptEl.focus();
  sugIdx = -1;
  refreshSuggest();
}

promptEl.addEventListener("input", () => { sugIdx = -1; refreshSuggest(); });
promptEl.addEventListener("blur", () => setTimeout(hideSuggest, 120));
promptEl.addEventListener("keydown", (e) => {
  const open = !sugEl.hidden && sugMode === "complete";
  if (e.key === "Tab" && open) { e.preventDefault(); completeCommand(sugItems[Math.max(sugIdx, 0)]); }
  else if (e.key === "ArrowDown" && open) { e.preventDefault(); sugIdx = (sugIdx + 1) % sugItems.length; renderSuggest(); }
  else if (e.key === "ArrowUp" && open) { e.preventDefault(); sugIdx = (sugIdx - 1 + sugItems.length) % sugItems.length; renderSuggest(); }
  else if (e.key === "Enter" && open && sugIdx >= 0) { e.preventDefault(); completeCommand(sugItems[sugIdx]); }
});

function buildCheatsheet() {
  cheatEl.replaceChildren();
  const head = el("div", "cheat-head");
  const close = el("button", "close", "×");
  close.type = "button"; close.setAttribute("aria-label", "Close cheatsheet");
  close.addEventListener("click", () => toggleCheat(false));
  head.append(el("h2", "", "Commands"), close);

  const use = (text) => { promptEl.value = text; toggleCheat(false); promptEl.focus(); refreshSuggest(); };
  const table = el("table");
  for (const c of COMMANDS.filter((x) => !x[4])) {
    const tr = el("tr");
    const a = el("td"), b = el("td"), d = el("td");
    const cmd = el("button", "cmd", c[0]); cmd.type = "button";
    cmd.addEventListener("click", () => use(c[0] === "/help" ? "" : c[0] + " "));
    a.append(cmd);
    b.append(el("code", "usage", c[1].replace(/^\/\w+\s?/, "") || "—"));
    d.append(el("span", "what", c[3]));
    if (c[2]) { const ex = el("button", "ex", c[2]); ex.type = "button"; ex.title = "Use this example"; ex.addEventListener("click", () => use(c[2])); d.append(ex); }
    tr.append(a, b, d);
    table.append(tr);
  }
  const more = el("p", "more", "Also: /village  /hamlet  /outpost  /fort  /keep  /citadel  /camp  /temple  /monastery  /lighthouse  /port: same syntax as /town.");
  const plain = el("ul", "plain");
  for (const t of PLAIN_EXAMPLES) {
    const li = el("li"); const b = el("button", "ex", t); b.type = "button"; b.addEventListener("click", () => use(t)); li.append(b); plain.append(li);
  }
  cheatEl.append(
    head,
    el("p", "lede", "Slash commands run instantly and never go through the language model. Plain words are turned into a blueprint you review first: nothing is built until you press Build It. Click any command or example to load it."),
    table, more,
    el("h3", "", "Or just say it"), plain,
    el("p", "keys", "Tab completes a command  ·  ↑ ↓ choose  ·  Esc closes  ·  ? opens this sheet  ·  near <place> uses any landmark's name")
  );
}

function toggleCheat(force) {
  const show = force ?? cheatEl.hidden;
  cheatEl.hidden = !show;
  cheatBtn.setAttribute("aria-expanded", String(show));
  if (show) hideSuggest();
}
cheatBtn.addEventListener("click", (e) => { e.stopPropagation(); toggleCheat(); });
document.addEventListener("click", (e) => { if (!e.target.closest("#cheatsheet, #cheat-btn")) toggleCheat(false); });
buildCheatsheet();

/* ---------------- map importer ---------------- */
const importEl = $("#import-modal");
const imp = { file: null, natural: null, url: null };
const impCols = $("#imp-cols"), impRows = $("#imp-rows"), impOp = $("#imp-opacity"), impNote = $("#import-note");
const MAP_TYPES = ["image/png", "image/jpeg", "image/webp"];
const SQUARE_PX = 70;                                  // typical battle-map square, used only to suggest a grid size

function setImportNote(msg, bad = false) { impNote.textContent = msg; impNote.classList.toggle("bad", bad); }

function openImport() {
  if (!state.subnet) return;
  const bg = state.subnet.bg;
  imp.file = null; imp.natural = null;
  impCols.value = bg ? bg.cols : state.subnet.cols;
  impRows.value = bg ? bg.rows : state.subnet.rows;
  impOp.value = Math.round((bg ? bg.opacity : 0.35) * 100);
  $("#imp-opacity-out").textContent = impOp.value + "%";
  const prev = $("#import-preview");
  prev.onload = null;                                          // a previous file's auto-suggest must not overwrite saved values
  prev.hidden = !bg;
  if (bg) prev.src = bg.url;
  $("#import-drop").classList.toggle("has-file", !!bg);
  $("#import-remove").hidden = !bg;
  $("#import-title").textContent = `Import map for ${state.subnet.short}`;
  setImportNote(bg ? "A map is already attached. Choose a new file to replace it, or just recalibrate." : "Choose a battle map or region image. It is stretched over the grid; the tiles underneath turn transparent.");
  importEl.hidden = false;
  $("#import-drop").focus();
}

function closeImport() {
  importEl.hidden = true;
  if (imp.url) { URL.revokeObjectURL(imp.url); imp.url = null; }
  const bg = state.subnet && state.subnet.bg;                 // undo any live opacity preview
  mapEl.style.setProperty("--grid-op", bg ? bg.opacity : 0.35);
  $("#prompt").focus();
}

function chooseMapFile(file) {
  if (!file) return;
  if (!MAP_TYPES.includes(file.type)) return setImportNote("That is not a .png, .jpg or .webp image.", true);
  if (file.size > 25 * 1024 * 1024) return setImportNote("That image is over 25 MB.", true);
  imp.file = file;
  if (imp.url) URL.revokeObjectURL(imp.url);
  imp.url = URL.createObjectURL(file);
  const prev = $("#import-preview");
  prev.onload = () => {
    imp.natural = [prev.naturalWidth, prev.naturalHeight];
    impCols.value = clamp(Math.round(prev.naturalWidth / SQUARE_PX), 4, 128);
    impRows.value = clamp(Math.round(prev.naturalHeight / SQUARE_PX), 4, 96);
    setImportNote(`${file.name}: ${prev.naturalWidth}×${prev.naturalHeight} px. Suggested ${impCols.value}×${impRows.value} squares at ${SQUARE_PX} px each; set these to match the grid printed on your map.`);
  };
  prev.src = imp.url;
  prev.hidden = false;
  $("#import-drop").classList.add("has-file");
}

async function saveImport() {
  const cols = +impCols.value, rows = +impRows.value, opacity = impOp.value / 100;
  const bg = state.subnet.bg;
  if (!imp.file && !bg) return setImportNote("Choose an image first.", true);
  const btn = $("#import-save");
  btn.disabled = true; btn.textContent = "Saving…";
  try {
    let r;
    if (imp.file) {
      const q = new URLSearchParams({ subnet: state.cidr, cols, rows, opacity });
      const res = await fetch(`/api/scene/upload-map?${q}`, { method: "POST", headers: { "Content-Type": imp.file.type }, body: imp.file });
      if (!res.ok) { let m = res.statusText; try { m = (await res.json()).detail || m; } catch (_) {} throw new Error(m); }
      r = await res.json();
    } else {
      r = await api("/api/scene/map-settings", { method: "POST", body: JSON.stringify({ subnet: state.cidr, cols, rows, opacity }) });
    }
    closeImport();
    await loadScene();
    if (!view.touched) fitView(true);
    toast(`Map saved: ${r.bg.cols}×${r.bg.rows} squares on a ${r.cols}×${r.rows} grid`);
  } catch (err) {
    setImportNote(err.message, true);
  } finally {
    btn.disabled = false; btn.textContent = "Save map";
  }
}

async function removeImport() {
  try {
    await api(`/api/scene/map?subnet=${encodeURIComponent(state.cidr)}`, { method: "DELETE" });
    closeImport();
    await loadScene();
    toast("Imported map removed");
  } catch (err) { setImportNote(err.message, true); }
}

$("#zoom-import").addEventListener("click", openImport);
$("#import-close").addEventListener("click", closeImport);
$("#import-cancel").addEventListener("click", closeImport);
importEl.addEventListener("mousedown", (e) => { if (e.target === importEl) closeImport(); });
$("#import-save").addEventListener("click", saveImport);
$("#import-remove").addEventListener("click", removeImport);
$("#import-file").addEventListener("change", (e) => chooseMapFile(e.target.files[0]));
impOp.addEventListener("input", () => {                 // live preview of the grid overlay
  $("#imp-opacity-out").textContent = impOp.value + "%";
  mapEl.style.setProperty("--grid-op", impOp.value / 100);
});
const dropEl = $("#import-drop");
dropEl.addEventListener("dragover", (e) => { e.preventDefault(); dropEl.classList.add("over"); });
dropEl.addEventListener("dragleave", () => dropEl.classList.remove("over"));
dropEl.addEventListener("drop", (e) => { e.preventDefault(); dropEl.classList.remove("over"); chooseMapFile(e.dataTransfer.files[0]); });
dropEl.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#import-file").click(); } });

/* ---------------- repositioning landmark pins ---------------- */
function tileFromEvent(e) {
  const { px, py } = mapPoint(e);
  const x = Math.floor(px / CELL), y = Math.floor(py / CELL);
  return x < 0 || y < 0 || x >= state.subnet.cols || y >= state.subnet.rows ? null : { x, y };
}

function pinOrigin(it, tx, ty) {                       // the pointer tile becomes the pin's centre tile
  return [clamp(tx - Math.floor((it.w - 1) / 2), 0, state.subnet.cols - it.w), clamp(ty - Math.floor((it.h - 1) / 2), 0, state.subnet.rows - it.h)];
}

function toggleRepo(i) {
  if (state.repo && state.repo.index === i) return stopRepo();
  state.repo = { index: i };
  stageEl.classList.add("repositioning");
  renderMap();
  renderItemDossier(i);
  toast("Drag the pin, or click a tile, to line it up with your map. Esc stops.");
}

function stopRepo(silent = false) {
  if (!state.repo) return;
  state.repo = null;
  stageEl.classList.remove("repositioning");
  if (silent) return;
  renderMap();
  if (state.selectedItem !== null && state.subnet.layout[state.selectedItem]) renderItemDossier(state.selectedItem);
}

async function placePin(index, x, y) {
  try {
    const r = await api("/api/scene/layout/move", { method: "POST", body: JSON.stringify({ subnet: state.cidr, index, x, y }) });
    await loadScene();
    toast(r.message);
  } catch (e) {
    toast(e.message, true);
    renderMap();
  }
}

function placeFromEvent(e) {
  const t = tileFromEvent(e);
  if (!t || !state.repo) return;
  const it = state.subnet.layout[state.repo.index];
  const [x, y] = pinOrigin(it, t.x, t.y);
  placePin(state.repo.index, x, y);
}

function startPinDrag(e, i, it, d) {
  if (e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();                                 // dragging the pin must not pan the map
  d.setPointerCapture(e.pointerId);
  state.idrag = { i, it, d, sx: e.clientX, sy: e.clientY, moved: false, x: it.x, y: it.y };
  d.addEventListener("pointermove", onPinMove);
  d.addEventListener("pointerup", onPinUp);
  d.addEventListener("pointercancel", onPinUp);
}

function onPinMove(e) {
  const g = state.idrag;
  if (!g) return;
  if (!g.moved && Math.hypot(e.clientX - g.sx, e.clientY - g.sy) > 5) {
    g.moved = true;
    g.d.classList.add("dragging-pin");
    ghost.style.display = "block";
    ghost.style.width = `${g.it.w * CELL}px`;
    ghost.style.height = `${g.it.h * CELL}px`;
  }
  if (!g.moved) return;
  const t = tileFromEvent(e);
  if (!t) return;
  [g.x, g.y] = pinOrigin(g.it, t.x, t.y);
  g.d.style.left = `${g.x * CELL}px`;
  g.d.style.top = `${g.y * CELL}px`;
  ghost.style.left = `${g.x * CELL}px`;
  ghost.style.top = `${g.y * CELL}px`;
}

async function onPinUp() {
  const g = state.idrag;
  if (!g) return;
  state.idrag = null;
  for (const ev of ["pointermove", "pointerup", "pointercancel"]) g.d.removeEventListener(ev, ev === "pointermove" ? onPinMove : onPinUp);
  ghost.style.display = "none";
  ghost.style.width = ghost.style.height = "";
  if (!g.moved) return;
  state.suppressClick = true;
  setTimeout(() => { state.suppressClick = false; }, 0);
  if (g.x !== g.it.x || g.y !== g.it.y) await placePin(g.i, g.x, g.y); else renderMap();
}

/* ---------------- arrival tile ---------------- */
function focusArrival() {                              // entering a scene lands the view on its arrival tile
  const a = state.subnet.arrival;
  if (!a) return;
  const s = clamp(Math.max(view.s, 1), MIN_ZOOM, MAX_ZOOM);
  view.s = s;
  view.tx = stageEl.clientWidth / 2 - (FRAME / 2 + (a.x + 0.5) * CELL) * s;
  view.ty = stageEl.clientHeight / 2 - (FRAME / 2 + (a.y + 0.5) * CELL) * s;
  view.touched = true;
  applyView();
}

function renderSceneTools() {
  const box = $("#scene-tools");
  box.replaceChildren();
  const a = state.subnet.arrival;
  const set = el("button", "tool" + (state.pick ? " on" : ""), state.pick ? "Click a tile…  (Esc cancels)" : a ? `⚑ Arrival ${a.x},${a.y}` : "⚑ Set Arrival Tile");
  set.type = "button";
  set.title = "Where tokens land when they enter this scene through a door, gate or portal, and where the view opens";
  set.addEventListener("click", () => (state.pick ? endPick() : startPick()));
  box.append(set);
  if (a && !state.pick) {
    const clr = el("button", "tool quiet", "✕");
    clr.type = "button"; clr.title = "Clear the arrival tile"; clr.setAttribute("aria-label", "Clear arrival tile");
    clr.addEventListener("click", clearArrival);
    box.append(clr);
  }
}

function startPick() {
  if (state.repo) stopRepo(true);
  state.pick = { mode: "arrival" };
  stageEl.classList.add("picking");
  renderSceneTools();
  toast("Click the tile where tokens should arrive in this scene.");
}

function endPick(silent = false) {
  if (!state.pick) return;
  state.pick = null;
  stageEl.classList.remove("picking");
  if (!silent && state.subnet) renderSceneTools();
}

async function handlePick(e) {
  const t = tileFromEvent(e);
  if (!t) return;
  try {
    const r = await api("/api/scene/arrival", { method: "POST", body: JSON.stringify({ subnet: state.cidr, x: t.x, y: t.y }) });
    endPick(true);
    await loadScene();
    toast(`Arrival tile set to ${r.arrival.x},${r.arrival.y}`);
  } catch (err) { toast(err.message, true); }
}

async function clearArrival() {
  try {
    await api("/api/scene/arrival", { method: "POST", body: JSON.stringify({ subnet: state.cidr }) });
    await loadScene();
    toast("Arrival tile cleared");
  } catch (err) { toast(err.message, true); }
}

/* ---------------- new blank map ("Start Fresh") ---------------- */
const nmEl = $("#newmap-modal");
const nm = { file: null, fillTouched: false };
const nmNote = $("#nm-note");
const SCOPE_FILL = { world: "ocean", region: "forest", town: "dirt", interior: "stone", dungeon: "stone" };
const radio = (name) => document.querySelector(`input[name="${name}"]:checked`).value;

function setNmNote(msg, bad = false) { nmNote.textContent = msg; nmNote.classList.toggle("bad", bad); }

function syncNewMapForm() {
  const image = radio("nm-bg") === "image";
  $("#nm-fill-wrap").hidden = image;
  $("#nm-image-wrap").hidden = !image;
  $("#nm-pin-wrap").hidden = radio("nm-parent") === "root";
}

function openNewMap() {
  nm.file = null; nm.fillTouched = false;
  $("#nm-name").value = "";
  document.querySelector('input[name="nm-scope"][value="region"]').checked = true;
  document.querySelector('input[name="nm-bg"][value="fill"]').checked = true;
  document.querySelector('input[name="nm-parent"][value="child"]').checked = true;
  $("#nm-cols").value = 32; $("#nm-rows").value = 24;
  $("#nm-fill").value = SCOPE_FILL.region;
  $("#nm-pin").checked = true;
  $("#nm-child-label").textContent = `Child of ${state.subnet ? state.subnet.short : "this scene"}`;
  $("#nm-file-name").textContent = "No image chosen";
  setNmNote("A new map gets its own subnet and a Party token at 1,1, then you step straight in.");
  syncNewMapForm();
  nmEl.hidden = false;
  $("#nm-name").focus();
}

function closeNewMap() { nmEl.hidden = true; $("#prompt").focus(); }

function chooseNewMapFile(file) {
  if (!file) return;
  if (!MAP_TYPES.includes(file.type)) return setNmNote("That is not a .png, .jpg or .webp image.", true);
  if (file.size > 25 * 1024 * 1024) return setNmNote("That image is over 25 MB.", true);
  nm.file = file;
  $("#nm-file-name").textContent = file.name;
  setNmNote(`${file.name} will be stretched over the ${$("#nm-cols").value}×${$("#nm-rows").value} grid. Match the columns and rows to the squares on your map.`);
}

async function createMap() {
  const name = $("#nm-name").value.trim();
  const image = radio("nm-bg") === "image";
  if (!name) return setNmNote("Give the new map a name.", true);
  if (image && !nm.file) return setNmNote("Choose a map image, or switch Background to a terrain fill.", true);
  const cols = +$("#nm-cols").value, rows = +$("#nm-rows").value;
  const root = radio("nm-parent") === "root";
  const btn = $("#nm-create");
  btn.disabled = true; btn.textContent = "Creating…";
  try {
    const r = await api("/api/scenes/create", { method: "POST", body: JSON.stringify({
      name, scope: radio("nm-scope"), cols, rows, fill: image ? "none" : $("#nm-fill").value, root, parent: state.cidr, pin: $("#nm-pin").checked,
    }) });
    let extra = "";
    if (image) {                                       // second step: attach the artwork to the new scene
      const q = new URLSearchParams({ subnet: r.scene.cidr, cols, rows, opacity: 0.35 });
      const res = await fetch(`/api/scene/upload-map?${q}`, { method: "POST", headers: { "Content-Type": nm.file.type }, body: nm.file });
      if (!res.ok) { let m = res.statusText; try { m = (await res.json()).detail || m; } catch (_) {} extra = ` (the map image failed: ${m})`; }
    }
    closeNewMap();
    await loadScenes();
    toast(`Created ${r.scene.name} (${r.scene.cidr})${r.scene.pinned ? ", pin dropped on the map you came from" : ""}${extra}`, !!extra);
    await switchScene(r.scene.cidr, { dir: root ? "out" : "in" });
  } catch (err) {
    setNmNote(err.message, true);
  } finally {
    btn.disabled = false; btn.textContent = "Create map";
  }
}

$("#zoom-new").addEventListener("click", openNewMap);
$("#nm-close").addEventListener("click", closeNewMap);
$("#nm-cancel").addEventListener("click", closeNewMap);
nmEl.addEventListener("mousedown", (e) => { if (e.target === nmEl) closeNewMap(); });
$("#nm-create").addEventListener("click", createMap);
$("#nm-name").addEventListener("keydown", (e) => { if (e.key === "Enter") { e.preventDefault(); createMap(); } });
document.querySelectorAll('input[name="nm-bg"], input[name="nm-parent"]').forEach((r) => r.addEventListener("change", syncNewMapForm));
document.querySelectorAll('input[name="nm-scope"]').forEach((r) => r.addEventListener("change", () => { if (!nm.fillTouched) $("#nm-fill").value = SCOPE_FILL[r.value]; }));
$("#nm-fill").addEventListener("change", () => { nm.fillTouched = true; });
$("#nm-file").addEventListener("change", (e) => chooseNewMapFile(e.target.files[0]));
const nmDrop = $("#nm-drop");
nmDrop.addEventListener("dragover", (e) => { e.preventDefault(); nmDrop.classList.add("over"); });
nmDrop.addEventListener("dragleave", () => nmDrop.classList.remove("over"));
nmDrop.addEventListener("drop", (e) => { e.preventDefault(); nmDrop.classList.remove("over"); chooseNewMapFile(e.dataTransfer.files[0]); });

/* ---------------- live table: players moving tokens, scene follows ---------------- */
let live = null, liveRetry = 600, reloadTimer = null;

function scheduleReload() {                              // coalesce bursts of remote changes into one refresh
  clearTimeout(reloadTimer);
  reloadTimer = setTimeout(() => {
    if (state.drag || state.idrag) return scheduleReload();
    loadScene().catch(() => {});
    loadScenes().catch(() => {});
  }, 200);
}

async function followParty(sceneId, nodeId) {
  if (!state.subnet || sceneId === state.subnet.id) return scheduleReload();
  let sc = state.scenes.find((s) => s.id === sceneId);
  if (!sc) { await loadScenes().catch(() => {}); sc = state.scenes.find((s) => s.id === sceneId); }
  if (!sc) return;
  showTab("map");
  toast(`The party moved to ${sc.short}.`);
  switchScene(sc.cidr, { flashId: nodeId ?? null });
}

function handleLive(ev) {
  if (ev.by && ev.by === CLIENT_ID) return;               // our own action: this page already shows it
  switch (ev.event) {
    case "HELLO": case "PRESENCE": state.players = ev.players ?? 0; renderBroadcast(); break;
    case "TOKEN_MOVED": {
      if (!state.subnet || ev.scene_id !== state.subnet.id) break;
      const n = byId(ev.node_id);
      if (!n) { scheduleReload(); break; }
      n.x = ev.x; n.y = ev.y;
      if (!state.drag) { renderMap(); if (state.selectedId === n.id) renderDossier(); }
      break;
    }
    case "SCENE_CHANGED": followParty(ev.scene_id, ev.node_id); break;
    case "SCENE_UPDATED": scheduleReload(); break;
    default: break;
  }
}

function connectLive() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/tabletop`);
  live = ws;
  ws.onopen = () => { liveRetry = 600; };
  ws.onmessage = (m) => { try { handleLive(JSON.parse(m.data)); } catch (_) {} };
  ws.onclose = () => { state.players = 0; renderBroadcast(); if (live === ws) setTimeout(connectLive, (liveRetry = Math.min(liveRetry * 1.7, 5000))); };
  ws.onerror = () => ws.close();
}
setInterval(() => { if (live && live.readyState === WebSocket.OPEN) live.send('{"action":"PING"}'); }, 20000);

/* ---------------- broadcast / player link ---------------- */
const bcBtn = $("#broadcast-btn"), bcPop = $("#broadcast-pop");

async function loadLan() {
  try { state.lan = await api("/api/lan"); } catch (_) { state.lan = null; }
  renderBroadcast();
}

function renderBroadcast() {
  const n = state.players;
  bcBtn.replaceChildren(document.createTextNode("📡 Broadcast / Player Link"), ...(n ? [el("span", "bc-count", String(n))] : []));
  bcBtn.title = n ? `${n} player${n === 1 ? "" : "s"} connected` : "Share the player link";
  if (bcPop.hidden) return;
  const lan = state.lan;
  bcPop.replaceChildren();
  bcPop.append(el("h3", "", "Player link"));
  if (!lan) { bcPop.append(el("p", "bc-note", "Could not work out this computer's address.")); return; }
  const row = el("div", "bc-row");
  const input = el("input"); input.type = "text"; input.readOnly = true; input.value = lan.url; input.setAttribute("aria-label", "Player link");
  input.addEventListener("focus", () => input.select());
  const copy = el("button", "btn", "Copy"); copy.type = "button";
  copy.addEventListener("click", async () => {
    try { await navigator.clipboard.writeText(lan.url); } catch (_) { input.select(); document.execCommand("copy"); }
    copy.textContent = "Copied ✓"; setTimeout(() => { copy.textContent = "Copy"; }, 1500);
  });
  row.append(input, copy);
  bcPop.append(row, el("p", "bc-line", `${state.players} player${state.players === 1 ? "" : "s"} connected`));
  bcPop.append(el("p", "bc-note", !lan.detected
    ? "No Wi-Fi address found. Connect to your network, or swap the placeholder for this computer's IP address."
    : !lan.reachable ? "The server is bound to this computer only. Restart with HOST=0.0.0.0 so other devices can reach it."
    : "Players on the same Wi-Fi open this link. They see the map and can only move the Party and player-character tokens. Everything else stays with you."));
}

bcBtn.addEventListener("click", async (e) => {
  e.stopPropagation();
  bcPop.hidden = !bcPop.hidden;
  if (!bcPop.hidden) { await loadLan(); }
});
document.addEventListener("click", (e) => { if (!e.target.closest("#broadcast")) bcPop.hidden = true; });

/* ---------------- boot ---------------- */
connectLive();
loadLan();
loadScene()
  .then(() => Promise.all([loadScenes(), loadHealth()]))
  .catch((e) => toast(e.message, true));
setTimeout(loadHealth, 4000);  // refresh once the local model finishes warming up

"use strict";
/* Player view. Deliberately small: it can read the sanitised scene from /api/play/scene, listen to /ws/tabletop and
   send exactly one thing, MOVE_TOKEN for a token the server marked movable. The server re-checks every move. */

const $ = (sel, el = document) => el.querySelector(sel);
const CELL = 36, MIN_ZOOM = 0.3, MAX_ZOOM = 1.8, FRAME = CELL * 0.28 * 2;
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

const state = {
  sceneId: null, scene: null, nodes: [], selected: null,        // selected: {kind:"token", id} | {kind:"item", i}
  ws: null, connected: false, retry: 500, pending: new Map(),    // pending: node_id -> {x, y, timer} optimistic moves
  drag: null, pan: null, pointers: new Map(), pinch: null, suppressClick: false, focus: null,
  view: { s: 1, tx: 0, ty: 0, touched: false },
};
const view = state.view;
const stageEl = $("#stage"), panEl = $("#pan"), mapEl = $("#map"), cardEl = $("#card");
const ghost = document.createElement("div");
ghost.className = "ghost";

const GLYPHS = { site: "✦", camp: "▲", temple: "✚", ruins: "✧", port: "⚓", capital: "♛", marches: "⚑", citadel: "☠", mine: "⛏", tower: "♜", town: "⌂", landmark: "📍" };
const TYPE_LABEL = { party: "Party", pc: "Player character", army: "Faction", beast: "Creature", guard: "Guard", barkeep: "Innkeeper", patron: "Patron", npc: "Character" };

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}
const initials = (name) => name.split(/\s+/).map((w) => w[0]).slice(0, 2).join("").toUpperCase();

let toastTimer;
function toast(msg, warn = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.classList.toggle("warn", warn);
  t.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.remove("show"), 3200);
}

/* ---------------- viewport: pan, wheel / pinch zoom ---------------- */
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
  if (!state.scene) return;
  const w = state.scene.cols * CELL + FRAME, h = state.scene.rows * CELL + FRAME;
  const vw = stageEl.clientWidth, vh = stageEl.clientHeight;
  const s = clamp(Math.min((vw - 24) / w, (vh - 24) / h), MIN_ZOOM, MAX_ZOOM);
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

function centerOn(x, y, minScale = 1) {
  const s = clamp(Math.max(view.s, minScale), MIN_ZOOM, MAX_ZOOM);
  smoothly(() => {
    view.s = s;
    view.tx = stageEl.clientWidth / 2 - (FRAME / 2 + (x + 0.5) * CELL) * s;
    view.ty = stageEl.clientHeight / 2 - (FRAME / 2 + (y + 0.5) * CELL) * s;
    view.touched = true;
    applyView();
  });
}

stageEl.addEventListener("wheel", (e) => {
  e.preventDefault();
  const r = stageEl.getBoundingClientRect();
  zoomAt(Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0016)), e.clientX - r.left, e.clientY - r.top);
}, { passive: false });

stageEl.addEventListener("pointerdown", (e) => {
  if (e.target.closest(".zoom-ctl, .token.movable")) return;
  if (e.pointerType === "mouse" && e.button !== 0 && e.button !== 1) return;
  state.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (state.pointers.size === 2) {                                   // two fingers: pinch to zoom
    const [a, b] = [...state.pointers.values()];
    state.pinch = { d: Math.hypot(a.x - b.x, a.y - b.y), s: view.s };
    state.pan = null;
  } else if (state.pointers.size === 1) {
    state.pan = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty, moved: false };
  }
  window.addEventListener("pointermove", onPointerMove);
  window.addEventListener("pointerup", onPointerEnd);
  window.addEventListener("pointercancel", onPointerEnd);
});

function onPointerMove(e) {
  if (!state.pointers.has(e.pointerId)) return;
  state.pointers.set(e.pointerId, { x: e.clientX, y: e.clientY });
  if (state.pinch && state.pointers.size >= 2) {
    const [a, b] = [...state.pointers.values()];
    const d = Math.hypot(a.x - b.x, a.y - b.y);
    const r = stageEl.getBoundingClientRect();
    const target = clamp(state.pinch.s * (d / state.pinch.d), MIN_ZOOM, MAX_ZOOM);
    zoomAt(target / view.s, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top);
    state.suppressClick = true;
    return;
  }
  const p = state.pan;
  if (!p) return;
  const dx = e.clientX - p.x, dy = e.clientY - p.y;
  if (!p.moved && Math.hypot(dx, dy) > 4) { p.moved = true; stageEl.classList.add("panning"); }
  if (p.moved) { view.tx = p.tx + dx; view.ty = p.ty + dy; view.touched = true; applyView(); }
}

function onPointerEnd(e) {
  state.pointers.delete(e.pointerId);
  if (state.pointers.size < 2) state.pinch = null;
  if (state.pointers.size === 0) {
    window.removeEventListener("pointermove", onPointerMove);
    window.removeEventListener("pointerup", onPointerEnd);
    window.removeEventListener("pointercancel", onPointerEnd);
    stageEl.classList.remove("panning");
    if ((state.pan && state.pan.moved) || state.suppressClick) setTimeout(() => { state.suppressClick = false; }, 0);
    state.pan = null;
  }
}

stageEl.addEventListener("click", (e) => {
  if (state.suppressClick) return;
  if (!e.target.closest(".token, .item.linkable")) closeCard();
});

const zoomCenter = (f) => zoomAt(f, stageEl.clientWidth / 2, stageEl.clientHeight / 2, true);
$("#zoom-in").addEventListener("click", () => zoomCenter(1.25));
$("#zoom-out").addEventListener("click", () => zoomCenter(0.8));
$("#zoom-fit").addEventListener("click", () => fitView(true));
$("#zoom-me").addEventListener("click", () => { const m = myToken(); if (m) centerOn(m.x, m.y); else toast("There is no token for you to follow here yet."); });
new ResizeObserver(() => { if (!view.touched) fitView(false); }).observe(stageEl);

/* ---------------- scene data ---------------- */
let loading = false, again = false;
async function loadScene() {
  if (loading) { again = true; return; }
  loading = true;
  try {
    const res = await fetch("/api/play/scene", { cache: "no-store" });
    if (!res.ok) throw new Error(res.statusText);
    const data = await res.json();
    const changed = data.subnet.id !== state.sceneId;
    state.scene = data.subnet;
    state.sceneId = data.subnet.id;
    state.nodes = data.nodes;
    if (changed) state.pending.clear();
    render();
    if (changed) {
      fitView(false);
      const f = state.focus, m = myToken();
      const at = f && f.x != null ? f : m ? { x: m.x, y: m.y } : null;
      if (at) centerOn(at.x, at.y, 1);                       // land where the party arrived
      state.focus = null;
    }
  } catch (_) {
    /* the socket status tells the player when we are offline */
  } finally {
    loading = false;
    if (again) { again = false; loadScene(); }
  }
}

let reloadTimer;
const scheduleReload = () => { clearTimeout(reloadTimer); reloadTimer = setTimeout(loadScene, 150); };
const myToken = () => state.nodes.find((n) => n.movable && n.type === "pc") || state.nodes.find((n) => n.movable);

/* ---------------- rendering ---------------- */
function blockedSet() {
  const s = new Set();
  for (const it of state.scene.layout) {
    const op = it.block ? "add" : it.carve ? "delete" : null;
    if (!op) continue;
    for (let dx = 0; dx < it.w; dx++) for (let dy = 0; dy < it.h; dy++) s[op](`${it.x + dx},${it.y + dy}`);
  }
  return s;
}

function cellFree(x, y, selfId) {
  const { cols, rows } = state.scene;
  if (x < 0 || y < 0 || x >= cols || y >= rows) return false;
  if (blockedSet().has(`${x},${y}`)) return false;
  return !state.nodes.some((n) => n.id !== selfId && n.x === x && n.y === y);
}

function render() {
  if (state.drag) { state.dirty = true; return; }                    // never redraw under a dragging finger
  const sc = state.scene;
  for (const k of ["exterior", "region", "world", "dungeon"]) mapEl.classList.toggle(k, sc.kind === k);
  stageEl.dataset.kind = sc.kind;
  mapEl.classList.toggle("has-bg", !!sc.bg);
  mapEl.style.setProperty("--grid-op", sc.bg ? sc.bg.opacity : 0.35);
  mapEl.style.setProperty("--cols", sc.cols);
  mapEl.style.setProperty("--rows", sc.rows);
  $("#scene-name").textContent = `${sc.icon} ${sc.short}`;
  document.title = `${sc.short} · Player View`;
  renderMap();
  if (state.selected) renderCard();
}

function renderMap() {
  const sc = state.scene;
  mapEl.replaceChildren();
  if (sc.bg) {
    const img = el("div", "mapbg");
    img.style.backgroundImage = `url("${sc.bg.url}")`;
    img.style.left = `calc(var(--cell) * ${sc.bg.ox})`;
    img.style.top = `calc(var(--cell) * ${sc.bg.oy})`;
    img.style.width = `calc(var(--cell) * ${sc.bg.cols})`;
    img.style.height = `calc(var(--cell) * ${sc.bg.rows})`;
    mapEl.append(img, el("div", "gridlines"));
  }
  sc.layout.forEach((it, i) => {
    const inspect = it.kind === "landmark" || it.kind === "capital";
    const d = el("div", `item kind-${it.kind} tile-${it.kind}` + (it.variant ? ` lm-${it.variant}` : "") + (it.portal ? " portal" : "") +
      (it.kind === "border" && it.h > it.w ? " vert" : "") + (inspect ? " linkable" : "") + (inspect || it.portal ? " keep" : "") +
      (state.selected && state.selected.kind === "item" && state.selected.i === i ? " selected-item" : ""));
    if (it.block) d.dataset.block = "1";
    d.style.left = `calc(var(--cell) * ${it.x})`;
    d.style.top = `calc(var(--cell) * ${it.y})`;
    d.style.width = `calc(var(--cell) * ${it.w})`;
    d.style.height = `calc(var(--cell) * ${it.h})`;
    if (it.label && it.kind !== "areaname") d.title = it.label;
    if (["landmark", "capital", "areaname"].includes(it.kind) && it.label) d.dataset.label = it.label;
    if (inspect) d.addEventListener("click", (e) => { if (state.suppressClick) return; e.stopPropagation(); select({ kind: "item", i }); });
    mapEl.append(d);
  });
  mapEl.append(ghost);
  for (const n of state.nodes) mapEl.append(buildToken(n));
}

function buildToken(n) {
  const t = el("div", `token type-${n.type} ${n.movable ? "movable" : "locked"}`);
  t.dataset.id = n.id;
  t.style.left = `calc(var(--cell) * ${n.x})`;
  t.style.top = `calc(var(--cell) * ${n.y})`;
  if (state.selected && state.selected.kind === "token" && state.selected.id === n.id) t.classList.add("selected");
  const face = el("div", "face", n.portrait ? "" : initials(n.name));
  if (n.portrait) face.style.backgroundImage = `url("${n.portrait}")`;
  t.append(face, el("div", "plate", n.name));
  if (n.movable) t.addEventListener("pointerdown", (e) => startDrag(e, n, t));
  else t.addEventListener("click", (e) => { if (state.suppressClick) return; e.stopPropagation(); select({ kind: "token", id: n.id }); });
  return t;
}

/* ---------------- inspection card: names and roles only ---------------- */
function select(sel) { state.selected = sel; renderMap(); renderCard(); }

function closeCard() {
  state.selected = null;
  cardEl.classList.remove("open");
  cardEl.setAttribute("aria-hidden", "true");
  for (const t of mapEl.querySelectorAll(".selected, .selected-item")) t.classList.remove("selected", "selected-item");
}

function renderCard() {
  const sel = state.selected;
  cardEl.replaceChildren();
  const close = el("button", "close", "×");
  close.setAttribute("aria-label", "Close");
  close.addEventListener("click", closeCard);
  cardEl.append(close);
  if (sel.kind === "token") {
    const n = state.nodes.find((x) => x.id === sel.id);
    if (!n) return closeCard();
    const portrait = el("div", "portrait", n.portrait ? "" : initials(n.name));
    if (n.portrait) portrait.style.backgroundImage = `url("${n.portrait}")`;
    const chips = el("div", "chips");
    chips.append(el("span", "chip", TYPE_LABEL[n.type] || "Character"));
    cardEl.append(portrait, el("h2", "", n.name), el("p", "role", n.role || ""), chips);
    if (n.movable) cardEl.append(el("p", "note", "You can drag this token to move it."));
  } else {
    const it = state.scene.layout[sel.i];
    if (!it) return closeCard();
    const chips = el("div", "chips");
    chips.append(el("span", "chip", it.kind === "capital" ? "Capital city" : "Landmark"));
    cardEl.append(el("div", "portrait glyph", GLYPHS[it.variant] || GLYPHS[it.kind] || "📍"), el("h2", "", it.label || "Landmark"), chips);
  }
  cardEl.classList.add("open");
  cardEl.setAttribute("aria-hidden", "false");
}

/* ---------------- moving my own token ---------------- */
function mapPoint(e) {
  const r = mapEl.getBoundingClientRect();
  return { px: (e.clientX - r.left) / view.s - mapEl.clientLeft, py: (e.clientY - r.top) / view.s - mapEl.clientTop };
}

function startDrag(e, node, tokenEl) {
  if (e.button !== undefined && e.pointerType === "mouse" && e.button !== 0) return;
  e.preventDefault();
  e.stopPropagation();
  tokenEl.setPointerCapture(e.pointerId);
  state.drag = { node, tokenEl, sx: e.clientX, sy: e.clientY, moved: false, gx: node.x, gy: node.y };
  tokenEl.addEventListener("pointermove", onDragMove);
  tokenEl.addEventListener("pointerup", onDragEnd);
  tokenEl.addEventListener("pointercancel", onDragCancel);
}

function onDragMove(e) {
  const d = state.drag;
  if (!d) return;
  if (!d.moved && Math.hypot(e.clientX - d.sx, e.clientY - d.sy) > 6) {
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

function endDrag(d) {
  d.tokenEl.removeEventListener("pointermove", onDragMove);
  d.tokenEl.removeEventListener("pointerup", onDragEnd);
  d.tokenEl.removeEventListener("pointercancel", onDragCancel);
  ghost.style.display = "none";
  state.drag = null;
  if (state.dirty) { state.dirty = false; render(); }
}

function onDragEnd() {
  const d = state.drag;
  if (!d) return;
  const { node, moved, gx, gy } = d;
  endDrag(d);
  if (!moved) return select({ kind: "token", id: node.id });
  if ((gx !== node.x || gy !== node.y) && cellFree(gx, gy, node.id)) sendMove(node, gx, gy);
  else if (gx !== node.x || gy !== node.y) toast("You can't go there.", true);
  renderMap();
}

function onDragCancel() {
  const d = state.drag;
  if (!d) return;
  endDrag(d);
  renderMap();
}

function sendMove(node, x, y) {
  if (!state.ws || state.ws.readyState !== WebSocket.OPEN) { toast("Disconnected from the table. Reconnecting…", true); return; }
  const prev = { x: node.x, y: node.y };
  node.x = x; node.y = y;                                           // optimistic: the server has the last word
  const timer = setTimeout(() => { state.pending.delete(node.id); loadScene(); }, 4000);
  state.pending.set(node.id, { ...prev, timer });
  state.ws.send(JSON.stringify({ action: "MOVE_TOKEN", node_id: node.id, x, y }));
}

/* ---------------- live connection ---------------- */
function setConn(on) {
  state.connected = on;
  const c = $("#conn");
  c.classList.toggle("bad", !on);
  c.replaceChildren(document.createTextNode(on ? "🟢 " : "🔴 "), el("span", "", on ? "Connected" : "Disconnected"));
  c.title = on ? "Connected to the GM's table" : "Not connected to the GM's table. Trying again…";
}

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/tabletop?role=player`);
  state.ws = ws;
  ws.onopen = () => { setConn(true); state.retry = 500; loadScene(); };
  ws.onmessage = (m) => { try { handle(JSON.parse(m.data)); } catch (_) {} };
  ws.onclose = () => {
    setConn(false);
    if (state.ws === ws) setTimeout(connect, state.retry = Math.min(state.retry * 1.7, 5000));
  };
  ws.onerror = () => ws.close();
}

function clearPending(id) {
  const p = state.pending.get(id);
  if (p) { clearTimeout(p.timer); state.pending.delete(id); }
}

function handle(ev) {
  switch (ev.event) {
    case "HELLO": loadScene(); break;
    case "TOKEN_MOVED": {
      clearPending(ev.node_id);
      if (ev.scene_id !== state.sceneId) break;
      const n = state.nodes.find((x) => x.id === ev.node_id);
      if (!n) { scheduleReload(); break; }
      if (n.x !== ev.x || n.y !== ev.y) {
        n.x = ev.x; n.y = ev.y;
        if (!state.drag) renderMap();
      }
      break;
    }
    case "SCENE_CHANGED":                                            // the GM opened another scene, or the party went through a door
      state.focus = { x: ev.arrival_x, y: ev.arrival_y };
      loadScene();
      break;
    case "SCENE_UPDATED": scheduleReload(); break;
    case "MOVE_REJECTED": {
      toast(ev.message || "That move isn't allowed.", true);
      if (ev.node_id != null) {
        clearPending(ev.node_id);
        const n = state.nodes.find((x) => x.id === ev.node_id);
        if (n && ev.x != null) { n.x = ev.x; n.y = ev.y; }
      }
      renderMap();
      break;
    }
    default: break;
  }
}

setInterval(() => { if (state.ws && state.ws.readyState === WebSocket.OPEN) state.ws.send('{"action":"PING"}'); }, 20000);
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeCard(); });
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadScene(); });   // phones sleep: catch up on wake

setConn(false);
connect();

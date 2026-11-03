"""GM Workstation engine — FastAPI + SQLite WAL.

GET  /api/scenes           list locations (Flagon, Timberflats Square, ...)
POST /api/scenes/generate create the child scene behind a landmark (Port Valen, Sunken Citadel, ...)
GET  /api/scene?subnet=    scene layout, grid, tokens, rumors (default: last active)
POST /api/nodes/move       update a token's (x, y); portal tiles move it to another scene
POST /api/directive        instant path: slash commands (and legacy plain text) -> token / rumor / terrain / building / map change
POST /api/blueprint/propose plain English -> BlueprintProposal (summary + previewed actions); nothing is changed
POST /api/blueprint/apply  the approved actions, run as ONE atomic transaction
POST /api/scene/upload-map raw image body (?subnet=&cols=&rows=&opacity=) -> assets/maps/<subnet_id>.png
POST /api/scene/map-settings   recalibrate the imported map (cols, rows, grid opacity)
DELETE /api/scene/map      remove the imported map
POST /api/model/reload     retry loading models/qwen2.5-3b-instruct-q4_k_m.gguf without restarting
POST /api/scenes/create    blank new scene (Start Fresh): subnet + optional terrain fill + a Party token at (1,1)
POST /api/scene/layout/move   reposition a landmark pin (stored as an override, survives layout refreshes)
POST /api/scene/arrival    set / clear the scene's arrival tile (where portal and door arrivals land)
POST /api/scene/resize     grow a scene (width/height = new total tiles, direction = which edge)
POST /api/nodes/{id}/portrait   (re)queue portrait generation
GET  /api/health           parser engine, image availability, journal mode

Player broadcast (local Wi-Fi):
GET  /play                 the player view (player.html): read-only map, can only drag Party / player-character tokens
GET  /api/play/scene       sanitised active scene for players (no secrets, IPs, CIDRs, links or GM notes)
WS   /ws/tabletop          live hub. Server -> clients: HELLO, PRESENCE, TOKEN_MOVED, SCENE_CHANGED, SCENE_UPDATED,
                           MOVE_REJECTED. Client -> server: {"action": "MOVE_TOKEN", node_id, x, y}
GET  /api/lan              GM only: the Player Link (http://<LAN IP>:<port>/play) and how many players are connected

Access: the server binds to 0.0.0.0 so phones and tablets can join, but only the host machine can reach GM tools.
Everyone else gets /play and its assets (set GM_ALLOW_LAN=1 to let other devices act as GM, e.g. an iPad).
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from typing import Literal
from urllib.parse import urlparse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import database as db
import gemini_client
import parser as directive_parser

ROOT = Path(__file__).parent


# --------------------------------------------------------------------------- #
# Network: who is the GM, what is the LAN address
# --------------------------------------------------------------------------- #
GM_LAN = os.environ.get("GM_ALLOW_LAN") == "1"
_addr_cache: tuple[float, set] = (0.0, set())


def lan_ip() -> str | None:
    """This machine's address on the local network (no packets are sent)."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.2)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        if ip and not ip.startswith("127."):
            return ip
    except OSError:
        pass
    try:
        ip = socket.gethostbyname(socket.gethostname())
        return None if ip.startswith("127.") else ip
    except OSError:
        return None


def _local_addresses() -> set:
    global _addr_cache
    if time.time() - _addr_cache[0] < 30:
        return _addr_cache[1]
    addrs = {"127.0.0.1", "::1", "localhost", "testclient"}        # "testclient" is Starlette's in-process test client
    if (ip := lan_ip()):
        addrs.add(ip)
    try:
        addrs |= {i[4][0] for i in socket.getaddrinfo(socket.gethostname(), None)}
    except OSError:
        pass
    _addr_cache = (time.time(), addrs)
    return addrs


def is_gm_client(host: str | None) -> bool:
    """GM tools are for the machine running the server (or anyone, if GM_ALLOW_LAN=1)."""
    if GM_LAN:
        return True
    host = (host or "").removeprefix("::ffff:")
    return host in _local_addresses()


class Hub:
    """Fan-out of live events to every connected browser (GM and players)."""

    def __init__(self) -> None:
        self.clients: dict[WebSocket, dict] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    def count(self, role: str = "player") -> int:
        return sum(1 for c in self.clients.values() if c["role"] == role)

    async def _send(self, ws: WebSocket, data: str) -> bool:
        info = self.clients.get(ws)
        if info is None:
            return False
        try:
            async with info["lock"]:                 # one writer per socket at a time
                await ws.send_text(data)
            return True
        except Exception:
            self.clients.pop(ws, None)
            return False

    async def send(self, ws: WebSocket, msg: dict) -> None:
        await self._send(ws, json.dumps(msg))

    async def broadcast(self, msg: dict) -> None:
        data = json.dumps(msg)
        await asyncio.gather(*(self._send(ws, data) for ws in list(self.clients)), return_exceptions=True)

    def publish(self, msg: dict) -> None:
        """Thread-safe broadcast, for code that runs outside the event loop."""
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self.broadcast(msg), self.loop)

    async def presence(self) -> None:
        await self.broadcast({"event": "PRESENCE", "players": self.count("player")})


hub = Hub()


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    hub.loop = asyncio.get_running_loop()

    async def portrait_ready(node_id: int) -> None:         # a finished portrait is a visible change for players too
        await hub.broadcast({"event": "SCENE_UPDATED", "scene_id": await run_in_threadpool(db.active_scene_id)})
    gemini_client.on_update = portrait_ready
    tasks = [
        asyncio.create_task(asyncio.to_thread(directive_parser.warmup)),
        asyncio.create_task(gemini_client.backfill()),
    ]
    yield
    for t in tasks:
        t.cancel()


app = FastAPI(title="GM Workstation", lifespan=lifespan)
(ROOT / "assets" / "media").mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
app.mount("/assets", StaticFiles(directory=ROOT / "assets"), name="assets")


PLAYER_STATIC = {"/static/player.js", "/static/player.css", "/static/app.css"}
QUIET = ("/api/nodes/move", "/api/blueprint/propose", "/api/model/reload", "/api/lan")      # no generic SCENE_UPDATED


def _player_allowed(path: str) -> bool:
    return (path in ("/play", "/play/", "/favicon.ico") or path in PLAYER_STATIC
            or path.startswith("/assets/") or path.startswith("/api/play/"))


@app.middleware("http")
async def lan_guard(request: Request, call_next):
    """Binding to 0.0.0.0 must not hand the GM's controls to the whole network. Other devices get /play and nothing
    else; they cannot even download the GM page's code."""
    path = request.url.path
    host = request.client.host if request.client else ""
    if not is_gm_client(host):
        if path == "/":
            return RedirectResponse("/play")
        if request.method not in ("GET", "HEAD") or not _player_allowed(path):
            return JSONResponse({"detail": "GM tools are only available on the machine running the server."}, status_code=403)
    resp = await call_next(request)
    if (request.method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith("/api/")
            and resp.status_code < 400 and not path.startswith(QUIET) and not path.endswith("/portrait")):
        await hub.broadcast({"event": "SCENE_UPDATED", "scene_id": await run_in_threadpool(db.active_scene_id),
                             "by": request.headers.get("x-client-id")})
    return resp


class MoveBody(BaseModel):
    id: int
    x: int
    y: int


class ResizeBody(BaseModel):
    subnet: str | None = None       # cidr or id; default = scene in view
    width: int | None = Field(default=None, ge=1, le=512)     # new total size in tiles (maps only grow)
    height: int | None = Field(default=None, ge=1, le=512)
    direction: Literal["north", "south", "east", "west", "all"] = "all"


class CreateSceneBody(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    scope: Literal["world", "region", "town", "interior", "dungeon"] = "region"
    cols: int = Field(default=32, ge=8, le=128)
    rows: int = Field(default=24, ge=8, le=96)
    fill: Literal["ocean", "forest", "dirt", "stone", "none"] | None = None   # None = the scope's usual terrain
    root: bool = False                                    # True: a new root world instead of a child of `parent`
    parent: str | None = None                             # cidr/id of the scene it nests under (default: scene in view)
    pin: bool = True                                      # drop an enterable pin for it on the parent map


class LayoutMoveBody(BaseModel):
    subnet: str | None = None
    index: int = Field(ge=0)                              # index into the scene payload's layout list
    x: int = Field(ge=0, le=200)                          # new top-left tile
    y: int = Field(ge=0, le=200)


class ArrivalBody(BaseModel):
    subnet: str | None = None
    x: int | None = Field(default=None, ge=0, le=200)     # omit / null to clear
    y: int | None = Field(default=None, ge=0, le=200)


class GenerateBody(BaseModel):
    parent: str                    # cidr or id of the scene containing the landmark
    x: int                         # landmark's top-left tile
    y: int


class ProposeBody(BaseModel):
    text: str = Field(min_length=1, max_length=600)
    subnet: str | None = None


class ApplyBody(BaseModel):
    subnet: str | None = None
    actions: list[dict] = Field(min_length=1, max_length=8)      # re-validated server-side; extra keys are dropped


class MapSettingsBody(BaseModel):
    subnet: str | None = None
    cols: int = Field(ge=4, le=128)
    rows: int = Field(ge=4, le=96)
    opacity: float = Field(default=0.35, ge=0, le=1)


class DirectiveBody(BaseModel):
    text: str = Field(min_length=1, max_length=400)
    subnet: str | None = None      # cidr or id of the scene currently in view


async def publish_move(node: dict, transition: dict | None, by: str | None = None) -> None:
    """Tell everyone what a move did. If the party went through a door, everybody follows it."""
    if not transition:
        await hub.broadcast({"event": "TOKEN_MOVED", "node_id": node["id"], "x": node["x"], "y": node["y"],
                             "scene_id": node["scene_id"], "by": by})
    elif node["type"] in db.PLAYER_MOVABLE:
        await hub.broadcast({"event": "SCENE_CHANGED", "scene_id": node["scene_id"], "arrival_x": node["x"],
                             "arrival_y": node["y"], "reason": "party", "node_id": node["id"], "by": by})
    else:                                                    # a monster left or entered the scene in view
        await hub.broadcast({"event": "SCENE_UPDATED", "scene_id": await run_in_threadpool(db.active_scene_id), "by": by})


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/scenes")
def scenes():
    return {"scenes": db.list_scenes()}


@app.post("/api/scenes/create")
async def create_scene(body: CreateSceneBody):
    try:
        return {"scene": await run_in_threadpool(db.create_scene, body.name, body.scope, body.cols, body.rows,
                                                 body.fill, body.parent, body.root, body.pin)}
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.post("/api/scene/layout/move")
async def layout_move(body: LayoutMoveBody):
    try:
        return await run_in_threadpool(db.move_item, body.subnet, body.index, body.x, body.y)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/scene/arrival")
async def scene_arrival(body: ArrivalBody):
    try:
        return await run_in_threadpool(db.set_arrival, body.subnet, body.x, body.y)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.post("/api/scenes/generate")
def generate_scene(body: GenerateBody):
    try:
        return {"scene": db.generate_child(body.parent, body.x, body.y)}
    except LookupError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/scene/resize")
def resize_scene(body: ResizeBody):
    try:
        return db.resize_scene(body.subnet, body.width, body.height, body.direction)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/scene")
async def scene(request: Request, subnet: str | None = None):
    before = await run_in_threadpool(db.active_scene_id)
    try:
        data = await run_in_threadpool(db.get_scene, subnet)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    if data["subnet"]["id"] != before:                      # the GM opened a different scene: players follow
        arr = data["subnet"].get("arrival") or {}
        await hub.broadcast({"event": "SCENE_CHANGED", "scene_id": data["subnet"]["id"],
                             "arrival_x": arr.get("x"), "arrival_y": arr.get("y"), "reason": "gm",
                             "by": request.headers.get("x-client-id")})
    return data


@app.post("/api/nodes/move")
async def move(body: MoveBody, request: Request):
    try:
        node, transition = await run_in_threadpool(db.move_and_follow, body.id, body.x, body.y)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    await publish_move(node, transition, by=request.headers.get("x-client-id"))
    return {"node": node, "transition": transition}


@app.post("/api/directive")
async def directive(body: DirectiveBody, bg: BackgroundTasks):
    try:
        ctx = await run_in_threadpool(db.scene_context, body.subnet)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    try:
        packet, engine = await run_in_threadpool(directive_parser.parse_directive, body.text, ctx)
    except directive_parser.CommandError as exc:       # bad /command: tell the GM what to type instead
        raise HTTPException(422, str(exc))
    if packet["action"] in directive_parser.INSTANT_ACTIONS:     # /newmap and /move
        try:
            if packet["action"] == "NEW_MAP":
                scene = await run_in_threadpool(db.create_scene, packet["name"], packet["style"] or "region",
                                                packet["w"] or 32, packet["h"] or 24, None, body.subnet, False, True)
                return {"packet": packet, "node": None, "change": None, "scene": scene, "engine": engine}
            change = await run_in_threadpool(db.move_landmark_by_name, body.subnet, packet["name"], packet["x"], packet["y"])
            return {"packet": packet, "node": None, "change": change, "engine": engine}
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(409, str(exc))
    if packet["action"] in directive_parser.WORLD_ACTIONS:       # PAINT_TERRAIN / PLACE_BUILDING / EXPAND_MAP
        try:
            change = await run_in_threadpool(db.apply_world_action, packet, body.subnet)
        except LookupError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"packet": packet, "node": None, "change": change, "engine": engine}
    node = await run_in_threadpool(db.provision_host, packet, body.subnet)
    if node["type"] != "rumor" and packet["prompt_portrait"] is not None:
        bg.add_task(gemini_client.generate_portrait, node["id"], packet["prompt_portrait"] or node["name"])
    return {"packet": packet, "node": node, "change": None, "engine": engine}


@app.post("/api/blueprint/propose")
async def blueprint_propose(body: ProposeBody):
    """Plain English -> proposal. The actions are dry-run inside a transaction that is rolled back, so the
    preview shows real positions and real errors while the board stays untouched."""
    try:
        ctx = await run_in_threadpool(db.scene_context, body.subnet)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    proposal = await run_in_threadpool(directive_parser.propose_blueprint, body.text, ctx)
    actions = proposal["actions"]
    dry = await run_in_threadpool(db.run_blueprint, actions, body.subnet, False) if actions else {"ok": False, "results": []}
    for a, r in zip(actions, dry["results"]):
        a["label"], a["ok"], a["error"] = r["label"], r["ok"], r.get("error")
    proposal["ok"] = bool(actions) and dry["ok"]
    proposal["subnet"] = body.subnet
    return {"proposal": proposal}


@app.post("/api/blueprint/apply")
async def blueprint_apply(body: ApplyBody, bg: BackgroundTasks):
    """Execute an approved blueprint atomically: every step succeeds or nothing changes."""
    try:
        ctx = await run_in_threadpool(db.scene_context, body.subnet)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    actions = []
    for a in body.actions:
        if a.get("action") not in directive_parser.BLUEPRINT_ACTIONS:
            raise HTTPException(422, f"Unknown action {a.get('action')!r}.")
        actions.append(directive_parser.normalize_packet(a, ctx))
    res = await run_in_threadpool(db.run_blueprint, actions, body.subnet, True)
    if not res["ok"]:
        bad = next((i for i, r in enumerate(res["results"]) if not r["ok"]), 0)
        raise HTTPException(409, f"Step {bad + 1} could not be built: {res['results'][bad].get('error') or 'unknown error'} Nothing was changed.")
    results = res["results"]
    for a, r in zip(actions, results):
        if r.get("node_id"):
            bg.add_task(gemini_client.generate_portrait, r["node_id"], r.get("portrait_prompt") or a["name"])
    grown = {k: sum(r["expand_added"].get(k, 0) for r in results if r.get("expand_added")) for k in "nsew"}
    focus = next((r["focus"] for r in reversed(results) if r.get("focus")), None)
    landmark = next((r["landmark"] for r in reversed(results) if r.get("landmark")), None)
    return {"results": results, "message": "Built: " + "; ".join(r["label"] for r in results),
            "focus": focus, "landmark": landmark, "expand_added": grown if any(grown.values()) else None}


@app.post("/api/scene/upload-map")
async def upload_map(request: Request, subnet: str | None = None, cols: int = 24, rows: int = 16, opacity: float = 0.35):
    """Raw image bytes in the body (no multipart dependency). Saved as assets/maps/<subnet_id>.png."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > db.MAX_MAP_BYTES + 4096:
        raise HTTPException(413, "That image is over 25 MB.")
    data = await request.body()
    try:
        return await run_in_threadpool(db.save_scene_map, subnet, data, cols, rows, opacity)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.post("/api/scene/map-settings")
async def map_settings(body: MapSettingsBody):
    try:
        return await run_in_threadpool(db.update_scene_map, body.subnet, body.cols, body.rows, body.opacity)
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(422, str(exc))


@app.delete("/api/scene/map")
async def remove_map(subnet: str | None = None):
    try:
        return await run_in_threadpool(db.clear_scene_map, subnet)
    except LookupError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/model/reload")
async def reload_model():
    return await run_in_threadpool(directive_parser.reload_model)


@app.get("/play")
def play_page():
    return FileResponse(ROOT / "static" / "player.html", headers={"Cache-Control": "no-cache"})


@app.get("/api/play/scene")
async def play_scene():
    return await run_in_threadpool(db.get_play_scene)


@app.get("/api/lan")
async def lan_info(request: Request):
    """GM only (the guard blocks everyone else): the link to hand to players."""
    ip = lan_ip()
    port = request.url.port or int(os.environ.get("PORT", "8000"))
    bind = os.environ.get("GM_BIND_HOST", "0.0.0.0")
    return {"ip": ip, "port": port, "url": f"http://{ip}:{port}/play" if ip else f"http://<this computer's address>:{port}/play",
            "detected": bool(ip), "reachable": bind not in ("127.0.0.1", "localhost", "::1"), "players": hub.count("player")}


MAX_WS_MSG = 4096


@app.websocket("/ws/tabletop")
async def tabletop(ws: WebSocket, role: str = "gm"):
    """One socket for everyone. The server, not the client, decides the role: only the host machine can be the GM,
    and anyone can ask to be treated as a player (the GM's own /play tab does)."""
    origin = ws.headers.get("origin")
    if origin and urlparse(origin).netloc.lower() != ws.headers.get("host", "").lower():
        await ws.close(code=1008)                            # a page on another site must not drive this socket
        return
    eff = "gm" if (role != "player" and is_gm_client(ws.client.host if ws.client else "")) else "player"
    await ws.accept()
    hub.clients[ws] = {"role": eff, "lock": asyncio.Lock(), "win": (0.0, 0)}
    await hub.send(ws, {"event": "HELLO", "role": eff, "scene_id": await run_in_threadpool(db.active_scene_id),
                        "players": hub.count("player")})
    await hub.presence()
    try:
        while True:
            raw = await ws.receive_text()
            now = time.time()
            start, n = hub.clients[ws]["win"]
            n = n + 1 if now - start < 1 else 1
            hub.clients[ws]["win"] = (start if now - start < 1 else now, n)
            if n > 30 or len(raw) > MAX_WS_MSG:               # flood / oversized frame: ignore
                continue
            try:
                msg = json.loads(raw)
                action = msg.get("action")
            except (ValueError, AttributeError):
                continue
            if action == "PING":
                await hub.send(ws, {"event": "PONG"})
            elif action == "MOVE_TOKEN":
                await ws_move(ws, eff, msg)
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        hub.clients.pop(ws, None)
        await hub.presence()


async def ws_move(ws: WebSocket, role: str, msg: dict) -> None:
    try:
        node_id, x, y = int(msg["node_id"]), int(msg["x"]), int(msg["y"])
    except (KeyError, TypeError, ValueError):
        return await hub.send(ws, {"event": "MOVE_REJECTED", "message": "That move was not understood."})
    try:
        node, transition = await run_in_threadpool(db.play_move, node_id, x, y, role)
    except (PermissionError, LookupError, ValueError) as exc:
        cur = await run_in_threadpool(db.get_node, node_id)  # tell the sender where the token really is
        return await hub.send(ws, {"event": "MOVE_REJECTED", "node_id": node_id, "message": str(exc),
                                   "x": cur["x"] if cur else None, "y": cur["y"] if cur else None,
                                   "scene_id": cur["scene_id"] if cur else None})
    await publish_move(node, transition)



@app.post("/api/nodes/{node_id}/portrait")
async def portrait(node_id: int, bg: BackgroundTasks):
    node = await run_in_threadpool(db.get_node, node_id)
    if not node or node["type"] == "rumor":
        raise HTTPException(404, "token not found")
    if not gemini_client.available():
        return {"queued": False, "reason": "Set GEMINI_API_KEY and install google-genai to generate portraits."}
    bg.add_task(gemini_client.generate_portrait, node_id, node["state"].get("prompt_portrait") or node["name"])
    return {"queued": True}


@app.get("/api/health")
def health():
    return {
        "engine": directive_parser.engine_name(),
        "model": directive_parser.model_status(),
        "images": gemini_client.available(),
        "journal_mode": db.journal_mode(),
    }

"""campaign.db — SQLite (WAL) data layer.

Tables
  subnets      scenes/locations, addressed like networks (10.1.1.0/24)
  nodes        entities/tokens, addressed like hosts inside a subnet
  media_cache  portrait generation status + file path per node

Scenes (Grand Realm > Wilds > Town Square > Flagon) are defined in code (SCENES below);
further child scenes (Port Valen, the Sunken Citadel, ...) are generated on demand from templates. On every start, missing scenes are
inserted and seeded; existing scenes get their layout refreshed, and their
nodes are left untouched, so a campaign.db from an earlier build upgrades in place.
Portals: a layout item with a "portal" moves any token that steps on it to
another scene.
"""
from __future__ import annotations

import ipaddress
import json
import math
import os
import random
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).parent
DB_PATH = Path(os.environ.get("CAMPAIGN_DB", ROOT / "campaign.db"))
MEDIA_DIR = ROOT / "assets" / "media"
MAPS_DIR = ROOT / "assets" / "maps"           # imported battle maps: assets/maps/<subnet_id>.png

FLAGON = "10.1.1.0/24"
TOWN = "10.1.2.0/24"
WILDS = "10.1.0.0/16"
REALM = "10.0.0.0/8"

# tab order, widest scope first
SCOPE_ORDER = [REALM, WILDS, TOWN, FLAGON]

SCHEMA = """
CREATE TABLE IF NOT EXISTS subnets (
    id          INTEGER PRIMARY KEY,
    cidr        TEXT UNIQUE NOT NULL,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'room',
    grid_cols   INTEGER NOT NULL,
    grid_rows   INTEGER NOT NULL,
    layout_json TEXT NOT NULL DEFAULT '[]',
    active      INTEGER NOT NULL DEFAULT 0,
    parent_id   INTEGER REFERENCES subnets(id),   -- scene hierarchy: realm > region > town > building
    meta_json   TEXT NOT NULL DEFAULT '{}',       -- icon/short/anchors/origin for generated scenes
    visited     REAL,                             -- last time the GM viewed it (breadcrumb memory)
    custom_json TEXT NOT NULL DEFAULT '[]',       -- GM edits: painted terrain, stamped buildings, expansion fill
    pad_json    TEXT NOT NULL DEFAULT '{}'        -- tiles added per side {"n","s","e","w"}; edits survive layout refreshes
);
CREATE TABLE IF NOT EXISTS nodes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    subnet_id       INTEGER NOT NULL REFERENCES subnets(id),
    ip              TEXT NOT NULL,
    name            TEXT NOT NULL,
    type            TEXT NOT NULL,
    x               INTEGER NOT NULL DEFAULT -1,
    y               INTEGER NOT NULL DEFAULT -1,
    state_json      TEXT NOT NULL DEFAULT '{}',
    visibility_mask INTEGER NOT NULL DEFAULT 3,   -- bit 1: players, bit 2: GM
    created_at      REAL NOT NULL,
    UNIQUE (subnet_id, ip)
);
CREATE TABLE IF NOT EXISTS media_cache (
    node_id    INTEGER PRIMARY KEY REFERENCES nodes(id) ON DELETE CASCADE,
    path       TEXT,
    prompt     TEXT,
    status     TEXT NOT NULL,                     -- pending | ready | failed
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nodes_subnet ON nodes(subnet_id);
"""


# --------------------------------------------------------------------------- #
# connection
# --------------------------------------------------------------------------- #
def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=10000;")
    conn.execute("PRAGMA foreign_keys=ON;")
    conn.execute("PRAGMA synchronous=NORMAL;")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def journal_mode() -> str:
    with db() as conn:
        return conn.execute("PRAGMA journal_mode;").fetchone()[0]


# --------------------------------------------------------------------------- #
# layout helpers
# --------------------------------------------------------------------------- #
def _i(kind, x, y, w=1, h=1, block=False, label=None, portal=None, variant=None, enter=None, desc=None):
    item = {"kind": kind, "x": x, "y": y, "w": w, "h": h, "block": block}
    if label:
        item["label"] = label
    if portal:
        item["portal"] = portal          # stepping on it moves the token to another scene
    if variant:
        item["variant"] = variant
    if enter:
        item["enter"] = enter            # clicking it offers Enter / Generate for the GM's view
    if desc:
        item["desc"] = desc
    return item


def _gen(name, template, icon="📍", short=None):
    """enter-spec for a child scene that is created the first time the GM delves into it."""
    return {"generate": {"name": name, "template": template, "icon": icon, "short": short or name}}


# --------------------------------------------------------------------------- #
# Scene 1 — The Rusty Flagon (interior, 24 x 16)
# --------------------------------------------------------------------------- #
FLAGON_LAYOUT = [
    _i("rug", 6, 8, 6, 5),
    _i("shelf", 4, 0, 10, 2, True, "Back shelves"),
    _i("hearth", 0, 5, 2, 4, True, "Hearth"),
    _i("bar", 4, 4, 10, 1, True, "Bar counter"),
    _i("bar", 13, 2, 1, 2, True, "Bar counter"),
    _i("stool", 5, 5), _i("stool", 7, 5), _i("stool", 9, 5), _i("stool", 11, 5),
    _i("barrel", 0, 0, 1, 1, True), _i("barrel", 1, 0, 1, 1, True), _i("barrel", 0, 1, 1, 1, True),
    _i("barrel", 21, 0, 1, 1, True), _i("barrel", 22, 0, 1, 1, True), _i("barrel", 23, 0, 1, 1, True),
    _i("barrel", 0, 12, 1, 1, True), _i("barrel", 0, 13, 1, 1, True), _i("barrel", 1, 14, 1, 1, True),
    _i("table", 16, 3, 2, 2, True, "Corner table"),
    _i("stool", 15, 3), _i("stool", 18, 4), _i("stool", 16, 5),
    _i("rtable", 8, 9, 2, 2, True, "Round table"),
    _i("stool", 7, 10), _i("stool", 10, 10), _i("stool", 8, 11), _i("stool", 9, 8),
    _i("table", 20, 6, 2, 4, True, "Booth table"),
    _i("bench", 22, 6, 1, 4, False, "Booth"),
    _i("table", 15, 10, 3, 2, True, "Long table"),
    _i("stool", 14, 10), _i("stool", 14, 11), _i("stool", 18, 10), _i("stool", 18, 11),
    _i("door", 11, 15, 2, 1, False, "Front door — leads to Timberflats Square",
       portal={"to": TOWN, "x": 11, "y": 11}, enter={"to": TOWN},
       desc="The front door of the Flagon. Beyond it lies the Square."),
]

FLAGON_ANCHORS = [
    [r"behind (the )?bar", [9, 3]],
    [r"\b(bar|stool|counter)\b", [10, 6]],
    [r"\b(hearth|fire|fireplace)\b", [3, 6]],
    [r"\b(door|entrance|entry)\b", [11, 14]],
    [r"\b(booth|corner)\b", [22, 8]],
    [r"\b(table)\b", [11, 10]],
    [r"\b(cellar|barrels?)\b", [2, 13]],
]

SEED_NODES = [
    dict(name="Barnaby", type="barkeep", x=7, y=3,
         state=dict(role="Innkeeper", dc=13,
                    motivation="Keep the Flagon open and his regulars out of trouble.",
                    secret="Knows the smuggler's lantern signal and quietly leaves the cellar door unbarred on moonless nights.",
                    prompt_portrait="Friendly stubborn middle-aged innkeeper, mustache, green waistcoat, rolled sleeves")),
    dict(name="Kaelen", type="npc", x=22, y=7,
         state=dict(role="Hooded Stranger", dc=15,
                    motivation="Wait for a courier who is three nights late.",
                    secret="Carries a sealed writ bearing the Duke's forged signature; he is the smuggler's contact.",
                    prompt_portrait="Hooded dark-clad stranger, shadowed face, watchful eyes, leather cowl")),
    dict(name="Hodge", type="guard", x=10, y=14,
         state=dict(role="Watchman", dc=12,
                    motivation="Finish his shift without another brawl.",
                    secret="Takes two silver a week from Barnaby to look away from the cellar.",
                    prompt_portrait="Weary town watchman, steel helm, chainmail, green tabard, grizzled beard")),
    dict(name="Pell", type="guard", x=13, y=14,
         state=dict(role="Watchman", dc=14,
                    motivation="Prove himself to Captain Hodge.",
                    secret="Has been tailing Kaelen for two nights and has not reported it.",
                    prompt_portrait="Young nervous watchman, oversized helm, spear, patched blue cloak")),
    dict(name="Mirela", type="patron", x=9, y=5,
         state=dict(role="Traveling Patron", dc=11,
                    motivation="Drink until the road stops moving.",
                    secret="Overheard Kaelen asking Barnaby about the old mill.",
                    prompt_portrait="Weathered traveling merchant woman, braided hair, road cloak, tired smile")),
    dict(name="Old Tuck", type="patron", x=7, y=10,
         state=dict(role="Regular", dc=10,
                    motivation="Tell anyone who will listen about the good old days.",
                    secret="Saw lantern light at the mill every new moon.",
                    prompt_portrait="Elderly bearded regular, red-nosed, worn tunic, mug in hand, cheerful")),
]

SEED_RUMORS = [
    dict(name="The Smuggler's Lantern", state=dict(
        role="rumor", dc=12, motivation="",
        secret="A green lantern in the old mill window means a shipment has landed.")),
    dict(name="The Duke's Writ", state=dict(
        role="clue", dc=15, motivation="",
        secret="Someone in the Flagon carries a writ with a forged ducal seal.")),
]

# --------------------------------------------------------------------------- #
# Scene 2 — Timberflats Town Square (exterior, 24 x 16)
# --------------------------------------------------------------------------- #
TOWN_LAYOUT = [
    # ground: wagon tracks, roads, market square, porch
    _i("track", 0, 12, 6, 2, False, "Wagon track"),
    _i("track", 17, 12, 7, 2, False, "Wagon track"),
    _i("track", 21, 6, 2, 7, False, "Wagon track"),
    _i("cobble", 10, 2, 4, 10, False, "Main road"),
    _i("cobble", 7, 5, 11, 6, False, "Market square"),
    _i("porch", 9, 11, 6, 1, False, "Tavern porch"),
    # north watch gate
    _i("wall", 0, 0, 9, 1, True, "Town wall"),
    _i("wall", 15, 0, 9, 1, True, "Town wall"),
    _i("tower", 9, 0, 2, 3, True, "Watch tower"),
    _i("tower", 13, 0, 2, 3, True, "Watch tower"),
    _i("gate", 11, 0, 2, 2, False, "North watch gate — leads to the Timberflats Wilds",
       portal={"to": WILDS, "x": 11, "y": 14}, enter={"to": WILDS},
       desc="Timberflats' north gate. Past it lie the Wilds."),
    # The Rusty Flagon, seen from outside (roof is visual; blockers carry collision)
    _i("tavern", 6, 12, 11, 4, False, "The Rusty Flagon", enter={"to": FLAGON},
       desc="A cozy tavern with a warm hearth and a cellar its owner would rather you left alone."),
    _i("blocker", 6, 12, 5, 4, True),
    _i("blocker", 13, 12, 4, 4, True),
    _i("blocker", 11, 13, 2, 3, True),
    _i("door", 11, 12, 2, 1, False, "Flagon entrance — step in to go inside",
       portal={"to": FLAGON, "x": 11, "y": 14}, enter={"to": FLAGON},
       desc="The tavern's front entrance."),
    # center well
    _i("well", 11, 7, 2, 2, True, "Stone well"),
    # market
    _i("stall", 16, 3, 3, 2, True, "Produce stall"),
    _i("stall", 20, 3, 3, 2, True, "Market stall"),
    _i("crate", 15, 4, 1, 1, True), _i("crate", 19, 5, 1, 1, True), _i("crate", 23, 5, 1, 1, True),
    # smithy
    _i("forge", 1, 3, 3, 2, True, "Blacksmith forge"),
    _i("anvil", 2, 6, 1, 1, True, "Anvil"),
    _i("barrel", 5, 3, 1, 1, True), _i("barrel", 5, 4, 1, 1, True),
    _i("barrel", 22, 9, 1, 1, True),
]

TOWN_ANCHORS = [
    [r"\b(gate|wall|tower|north)\b", [12, 3]],
    [r"\b(well|cistern|fountain)\b", [13, 9]],
    [r"\b(stall|market|vendor|produce)\b", [20, 5]],
    [r"\b(forge|anvil|smith\w*)\b", [4, 6]],
    [r"\b(tavern|porch|flagon|door|entrance|inn)\b", [11, 11]],
    [r"\b(track|wagon|cart)\b", [20, 13]],
    [r"\b(road|square|plaza|center|centre)\b", [14, 8]],
]

TOWN_NODES = [
    dict(name="Captain Rebecca", type="guard", x=10, y=3,
         state=dict(role="Town Watch Captain", dc=14,
                    motivation="Keep the north gate secure and the tolls collected.",
                    secret="Has been ordered to look away when a certain covered wagon passes the gate after dark.",
                    prompt_portrait="Stern female watch captain, short cropped hair, polished steel breastplate, blue town-watch cloak")),
    dict(name="Old Corin", type="npc", x=17, y=5,
         state=dict(role="Produce Vendor", dc=11,
                    motivation="Sell the last of the autumn apples before dusk.",
                    secret="Trades in gossip as readily as turnips: a hooded stranger has been asking about the mill road, and the watch captain's pay has quietly doubled.",
                    prompt_portrait="Cheerful elderly produce vendor, weathered face, straw hat, apron full of apples")),
    dict(name="Korgan Ironhand", type="npc", x=3, y=6,
         state=dict(role="Blacksmith", dc=13,
                    motivation="Finish the Duke's order of two hundred arrowheads by week's end.",
                    secret="Has been forging hidden lantern shutters for the smuggler, paid in unmarked silver.",
                    prompt_portrait="Broad-shouldered blacksmith, soot-streaked face, leather apron, braided beard, glowing forge behind")),
]

TOWN_RUMORS = [
    dict(name="Doubled Wages", state=dict(
        role="rumor", dc=12, motivation="",
        secret="Captain Rebecca's pay doubled last month and nobody will say who is paying it.")),
]

# --------------------------------------------------------------------------- #
# Scene 3 — Timberflats Wilds (region, 24 x 16; one tile is roughly a league)
# --------------------------------------------------------------------------- #
WILDS_LAYOUT = [
    # --- ground cover (walkable, drawn first) ---
    _i("forest", 0, 9, 9, 7, False, "Pine forest (difficult terrain)"),
    _i("forest", 15, 9, 9, 7, False, "Pine forest (difficult terrain)"),
    _i("forest", 12, 0, 12, 6, False, "Dense hill woods"),
    _i("forest", 17, 6, 7, 2, False, "Pine forest (difficult terrain)"),
    _i("scrub", 13, 9, 2, 5, False, "Rolling scrubland"),
    _i("scrub", 9, 11, 2, 3, False, "Rolling scrubland"),
    _i("scrub", 13, 6, 4, 2, False, "Riverbank scrub"),
    _i("scrub", 9, 3, 2, 2, False, "Rolling scrubland"),
    _i("foothill", 4, 5, 5, 2, False, "Craggy foothills"),
    # --- Black River (visual) + collision blockers, leaving the bridge open ---
    _i("river", 0, 7, 6, 1, False, "Black River"),
    _i("river", 6, 7, 1, 2, False, "Black River"),
    _i("river", 7, 8, 17, 1, False, "Black River"),
    # --- winding gravel trade route, south to north, with branches ---
    _i("gravel", 11, 11, 2, 3, False, "Gravel trade route"),
    _i("gravel", 10, 9, 3, 2, False, "Gravel trade route"),
    _i("gravel", 11, 6, 2, 2, False, "Gravel trade route"),
    _i("gravel", 10, 5, 2, 1, False, "Gravel trade route"),
    _i("gravel", 12, 4, 2, 2, False, "Gravel trade route"),
    _i("gravel", 11, 2, 2, 2, False, "Gravel trade route"),
    _i("gravel", 10, 0, 3, 2, False, "Gravel trade route"),
    _i("gravel", 7, 5, 4, 1, False, "Mine track"),
    _i("gravel", 13, 3, 6, 1, False, "Watchtower track"),
    # --- Old Stone Bridge ---
    _i("bridge", 11, 8, 2, 1, False, "Old Stone Bridge"),
    # --- mountains (impassable) ---
    _i("mountain", 0, 0, 9, 3, True, "Iron Teeth mountains"),
    _i("mountain", 0, 3, 4, 4, True, "Iron Teeth mountains"),
    _i("mountain", 6, 3, 3, 2, True, "Iron Teeth mountains"),
    # --- river collision (bridge cells stay free) ---
    _i("blocker", 0, 7, 6, 1, True), _i("blocker", 6, 7, 1, 2, True),
    _i("blocker", 7, 8, 4, 1, True), _i("blocker", 13, 8, 11, 1, True),
    # --- south border: leave the region for the world map ---
    _i("edge", 0, 15, 24, 1, False, "South edge — leave the Wilds for the Grand Realm",
       portal={"to": REALM, "x": 24, "y": 20}),          # the Timberflats Marches landmark on the 48x32 Realm
    # --- landmarks ---
    _i("landmark", 11, 14, 2, 1, False, "Timberflats — step in to enter the town",
       portal={"to": TOWN, "x": 11, "y": 2}, variant="town", enter={"to": TOWN},
       desc="The frontier town of Timberflats: a gate, a market square and a tavern."),
    _i("landmark", 4, 3, 2, 2, False, "Abandoned Iron Mine", variant="mine",
       enter=_gen("The Abandoned Iron Mine", "mine", "⛏", "Iron Mine"),
       desc="Collapsed timbers, a cold adit and, some say, a bell that still rings."),
    _i("landmark", 19, 3, 2, 2, False, "Ruined Watchtower", variant="tower",
       enter=_gen("The Ruined Watchtower", "tower", "♜", "Watchtower"),
       desc="A fallen border tower hidden in the hill woods."),
]

WILDS_ANCHORS = [
    [r"\b(bridge)\b", [11, 7]],
    [r"\b(mine|cave|adit)\b", [5, 5]],
    [r"\b(watchtower|tower|ruins?)\b", [18, 4]],
    [r"\b(town|timberflats|gate|outskirts)\b", [11, 13]],
    [r"\b(river|riverbank|bank|shore)\b", [14, 7]],
    [r"\b(mountains?|foothills?|crags?|cliffs?)\b", [6, 5]],
    [r"\b(forest|pines?|woods?|trees?)\b", [6, 11]],
    [r"\b(road|trail|route|path|track)\b", [12, 11]],
]

WILDS_NODES = [
    dict(name="The Party", type="party", x=11, y=13,
         state=dict(role="Travel Party", dc=10, disposition="allied",
                    motivation="Reach the Abandoned Iron Mine before the next moonless night.",
                    secret="Marching order, supplies and the watch rota are tracked here.",
                    prompt_portrait="Group of adventurers on a forest road, lantern light, packs and weapons, painterly fantasy")),
    dict(name="Torben the Trapper", type="npc", x=7, y=9,
         state=dict(role="Scout", dc=12, disposition="neutral",
                    motivation="Wait out the wolves under the pines and trade news for tobacco.",
                    secret="Saw lantern light moving inside the Abandoned Iron Mine last week and has told no one.",
                    prompt_portrait="Grizzled bearded trapper, fur-trimmed hood, bow across his back, weathered face, pine forest")),
    dict(name="Graymane Wolf Pack", type="beast", x=15, y=7,
         state=dict(role="Hostile Beasts", dc=11, disposition="hostile",
                    motivation="Hunt anything that crosses the river after dusk.",
                    secret="The alpha limps from an old snare and avoids the bridge because of the iron smell.",
                    prompt_portrait="Huge gray wolf with a scarred muzzle and silver mane, snarling in pine shadows, glowing eyes")),
]

WILDS_RUMORS = [
    dict(name="The Old Mine Bell", state=dict(
        role="rumor", dc=13, motivation="",
        secret="Locals swear the alarm bell at the Abandoned Iron Mine still rings on moonless nights.")),
]

# --------------------------------------------------------------------------- #
# Scene 4 — The Grand Realm (world map, 48 x 32; one tile is roughly 10 leagues)
# --------------------------------------------------------------------------- #
REALM_LAYOUT = [
    # --- The Cerulean Deep (impassable open ocean; coast bends around Port Valen) ---
    _i("ocean", 0, 0, 8, 32, True, "The Cerulean Deep"),
    _i("ocean", 8, 0, 6, 5, True, "The Cerulean Deep"),
    _i("ocean", 8, 5, 3, 6, True, "The Cerulean Deep"),
    _i("ocean", 8, 11, 2, 10, True, "The Cerulean Deep"),
    _i("ocean", 8, 21, 3, 6, True, "The Cerulean Deep"),
    _i("ocean", 8, 27, 5, 5, True, "The Cerulean Deep"),
    # --- ground cover (walkable) ---
    _i("scrub", 14, 0, 5, 4, False, "Rolling scrubland"),
    _i("scrub", 30, 0, 5, 2, False, "Rolling scrubland"),
    _i("scrub", 14, 13, 5, 4, False, "Rolling scrubland"),
    _i("scrub", 28, 18, 6, 4, False, "Rolling scrubland"),
    _i("scrub", 31, 9, 4, 3, False, "Rolling scrubland"),
    _i("scrub", 11, 22, 3, 3, False, "Rolling scrubland"),
    _i("forest", 13, 6, 6, 5, False, "Crownwood"),
    _i("forest", 32, 3, 4, 6, False, "Crownwood"),
    _i("forest", 29, 13, 4, 5, False, "Highmark pines"),
    _i("forest", 16, 22, 7, 8, False, "Marchwood"),
    _i("forest", 27, 22, 7, 8, False, "Marchwood"),
    _i("forest", 13, 24, 3, 6, False, "Marchwood"),
    _i("ashland", 40, 5, 6, 5, False, "Ash Peak roost"),
    _i("ashland", 39, 10, 8, 10, False, "The Sunken Marsh"),
    # --- The High Road and royal roads ---
    _i("highroad", 25, 12, 2, 8, False, "The High Road"),
    _i("highroad", 12, 19, 13, 1, False, "The High Road"),
    _i("highroad", 13, 12, 12, 1, False, "Royal road"),
    _i("highroad", 13, 13, 1, 6, False, "Royal road"),
    _i("highroad", 27, 19, 8, 1, False, "Eastern track"),
    _i("highroad", 22, 9, 8, 1, False, "Royal road"),
    _i("highroad", 25, 9, 2, 2, False, "Royal road"),
    _i("highroad", 25, 3, 2, 3, False, "Royal road"),
    # --- kingdom borders (drawn over roads) ---
    _i("border", 14, 15, 19, 1, False, "Border of the Crown lands"),
    _i("border", 34, 10, 1, 22, False, "Eastern march border"),
    # --- Highmark: walls, gate, quarters ---
    _i("citywall", 20, 2, 11, 1, True, "Highmark walls"),
    _i("citywall", 20, 3, 1, 8, True, "Highmark walls"),
    _i("citywall", 30, 3, 1, 8, True, "Highmark walls"),
    _i("citywall", 20, 11, 5, 1, True, "Highmark walls"),
    _i("citywall", 27, 11, 4, 1, True, "Highmark walls"),
    _i("gate", 25, 11, 2, 1, False, "Highmark gate"),
    _i("tavern", 21, 3, 3, 2, True, "Crown quarter"),
    _i("tavern", 27, 3, 3, 2, True, "Crown quarter"),
    _i("tavern", 21, 7, 2, 2, True, "Guild row"),
    _i("tavern", 28, 7, 2, 2, True, "Guild row"),
    # --- Dragon's Spine Peaks (impassable), pockets for the roost and the Citadel left open ---
    _i("snowpeak", 36, 0, 12, 5, True, "Dragon's Spine Peaks"),
    _i("snowpeak", 36, 5, 4, 5, True, "Dragon's Spine Peaks"),
    _i("snowpeak", 46, 5, 2, 5, True, "Dragon's Spine Peaks"),
    _i("snowpeak", 36, 10, 3, 10, True, "Dragon's Spine Peaks"),
    _i("snowpeak", 47, 10, 1, 10, True, "Dragon's Spine Peaks"),
    _i("snowpeak", 36, 20, 12, 12, True, "Dragon's Spine Peaks"),
    # --- area names ---
    _i("areaname", 0, 12, 8, 4, False, "The Cerulean Deep"),
    _i("areaname", 37, 22, 10, 4, False, "Dragon's Spine Peaks"),
    _i("areaname", 16, 26, 7, 2, False, "The Marchwood"),
    _i("areaname", 14, 7, 5, 2, False, "Crownwood"),
    # --- settlements and landmarks (click for Enter / Generate) ---
    _i("capital", 24, 6, 3, 3, False, "Highmark: Seat of the Crown",
       enter=_gen("Highmark", "capital", "♛", "Highmark"),
       desc="The walled royal capital. Court intrigue, guild houses and the throne of the Crown."),
    _i("landmark", 10, 18, 2, 2, False, "Port Valen", variant="port",
       enter=_gen("Port Valen", "port", "⚓", "Port Valen"),
       desc="A busy harbor town on the Cerulean Deep. Cogs, smugglers and salt-stained taverns."),
    _i("landmark", 24, 20, 3, 3, False, "Timberflats Marches — step in to travel down into the Wilds",
       portal={"to": WILDS, "x": 11, "y": 12}, variant="marches", enter={"to": WILDS},
       desc="The frontier. Beyond the Marches lie the Timberflats Wilds, the town and its tavern."),
    _i("landmark", 41, 13, 3, 3, False, "The Sunken Citadel", variant="citadel",
       enter=_gen("The Sunken Citadel", "citadel", "☠", "Sunken Citadel"),
       desc="A drowned fortress shielded by the Dragon's Spine. The oath-stone is said to lie within."),
]

REALM_ANCHORS = [
    [r"\b(highmark|capital|crown|king|palace|castle)\b", [25, 13]],
    [r"\b(port|harbou?r|valen|coast|ocean|sea|cerulean)\b", [12, 19]],
    [r"\b(timberflats|marches|frontier)\b", [25, 18]],
    [r"\b(citadel|sunken|marsh)\b", [42, 17]],
    [r"\b(dragon|wyrm|peaks?|spine|roost)\b", [42, 7]],
    [r"\b(border)\b", [20, 15]],
    [r"\b(forest|woods?|marchwood|crownwood)\b", [14, 8]],
    [r"\b(road|highway|high road)\b", [25, 15]],
]

REALM_SEED_POS = {"The Party": (25, 19), "1st Crown Legion": (25, 13), "Ash Peak Wyrm": (42, 7)}

REALM_NODES = [
    dict(name="The Party", type="party", x=25, y=19,
         state=dict(role="Travel Party", dc=10, disposition="allied",
                    motivation="Reach the Abandoned Iron Mine before the next moonless night.",
                    secret="Marching order, supplies and the watch rota are tracked here.",
                    prompt_portrait="Group of adventurers on a paved highway, banner and packs, painterly fantasy")),
    dict(name="1st Crown Legion", type="army", x=25, y=13,
         state=dict(role="Faction Army", dc=14, disposition="royal",
                    motivation="Hold the High Road and enforce the Crown's embargo.",
                    secret="The legion is under strength: a third of its levies deserted after the embargo was announced.",
                    prompt_portrait="Royal legion banner, rows of armored soldiers with crimson and gold standards, marching on a stone road")),
    dict(name="Ash Peak Wyrm", type="beast", x=42, y=7,
         state=dict(role="Ancient Dragon", dc=17, disposition="hostile",
                    motivation="Guard its hoard in the Dragon's Spine and burn whatever climbs too high.",
                    secret="Bound by an oath-stone in the Sunken Citadel; while the stone stands, the wyrm cannot leave the Spine.",
                    prompt_portrait="Ancient black dragon coiled on a snowy peak, ember-lit scales, glowing eyes, storm clouds")),
]

REALM_RUMORS = [
    dict(name="The King's Tithe", state=dict(
        role="rumor", dc=15, motivation="",
        secret="Highmark has declared an embargo on eastern timber shipments.")),
]

SCENES = [
    dict(cidr=FLAGON, parent=TOWN, name="The Rusty Flagon", short="The Rusty Flagon", icon="🍺", kind="room",
         cols=24, rows=16, layout=FLAGON_LAYOUT, nodes=SEED_NODES, rumors=SEED_RUMORS,
         anchors=FLAGON_ANCHORS, default=[11, 7],
         notes="Interior. Floor is open between landmarks. The front door is on the bottom wall and leads outside."),
    dict(cidr=TOWN, parent=WILDS, name="Timberflats Town Square", short="Timberflats Square", icon="🏘", kind="exterior",
         cols=24, rows=16, layout=TOWN_LAYOUT, nodes=TOWN_NODES, rumors=TOWN_RUMORS,
         anchors=TOWN_ANCHORS, default=[14, 8],
         notes="Outdoor town square. Cobbled roads and open ground between landmarks. The north watch gate is at the top; the tavern is at the bottom."),
    dict(cidr=WILDS, parent=REALM, name="Timberflats Wilds", short="Timberflats Wilds", icon="🌲", kind="region",
         cols=24, rows=16, layout=WILDS_LAYOUT, nodes=WILDS_NODES, rumors=WILDS_RUMORS,
         anchors=WILDS_ANCHORS, default=[12, 6],
         notes="Region map; one tile is about a league. A gravel trade route runs south to north from the town of Timberflats (bottom) over the Old Stone Bridge. A river crosses east-west. Mountains and the Abandoned Iron Mine are north-west; hill woods and the Ruined Watchtower are north-east. Creatures and beasts are tokens too."),
    dict(cidr=REALM, parent=None, name="The Grand Realm", short="The Grand Realm", icon="👑", kind="world",
         cols=48, rows=32, layout=REALM_LAYOUT, nodes=REALM_NODES, rumors=REALM_RUMORS,
         anchors=REALM_ANCHORS, default=[25, 15],
         notes="World map (48x32); one tile is about ten leagues. The Cerulean Deep ocean is west, with the harbor Port Valen. The royal capital Highmark is walled, center-north. The High Road runs south from Highmark to the frontier Timberflats Marches and west to Port Valen. The Dragon's Spine Peaks in the east shield the Sunken Citadel. Armies, fleets and dragons are tokens (PROVISION_HOST, role like 'Faction Army'); decrees, troop movements and world events are LOG_RUMOR."),
]
SCENE_BY_CIDR = {s["cidr"]: s for s in SCENES}


# --------------------------------------------------------------------------- #
# init / seed
# --------------------------------------------------------------------------- #
def init_db() -> None:
    MEDIA_DIR.mkdir(parents=True, exist_ok=True)
    MAPS_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
    with db() as conn:
        conn.executescript(SCHEMA)
        _ensure_columns(conn)
        sync_scenes(conn)


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Upgrade databases created by earlier builds."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(subnets)")}
    for name, ddl in (("parent_id", "INTEGER"), ("meta_json", "TEXT NOT NULL DEFAULT '{}'"), ("visited", "REAL"),
                      ("custom_json", "TEXT NOT NULL DEFAULT '[]'"), ("pad_json", "TEXT NOT NULL DEFAULT '{}'")):
        if name not in have:
            conn.execute(f"ALTER TABLE subnets ADD COLUMN {name} {ddl}")


def _remap_realm_tokens(conn, subnet_id: int) -> None:
    """The Realm grew from 24x16 to 48x32: carry existing tokens over."""
    sub = conn.execute("SELECT * FROM subnets WHERE id=?", (subnet_id,)).fetchone()
    toks = conn.execute("SELECT id, name, x, y FROM nodes WHERE subnet_id=? AND x>=0", (subnet_id,)).fetchall()
    conn.execute("UPDATE nodes SET x=-1, y=-1 WHERE subnet_id=? AND x>=0", (subnet_id,))
    for t in toks:
        tx, ty = REALM_SEED_POS.get(t["name"], (t["x"] * 2, t["y"] * 2))
        x, y = _nearest_free(conn, sub, tx, ty)
        conn.execute("UPDATE nodes SET x=?, y=? WHERE id=?", (x, y, t["id"]))


def sync_scenes(conn: sqlite3.Connection) -> None:
    for sc in SCENES:
        row = conn.execute("SELECT * FROM subnets WHERE cidr=?", (sc["cidr"],)).fetchone()
        if row:
            pad = _pad(row)   # keep GM expansions: effective size = code size + padding
            conn.execute(
                "UPDATE subnets SET name=?, kind=?, grid_cols=?, grid_rows=?, layout_json=? WHERE id=?",
                (sc["name"], sc["kind"], sc["cols"] + pad["w"] + pad["e"], sc["rows"] + pad["n"] + pad["s"],
                 json.dumps(sc["layout"]), row["id"]),
            )
            if sc["cidr"] == REALM and row["grid_cols"] == 24 and sc["cols"] == 48:
                _remap_realm_tokens(conn, row["id"])
            continue
        has_active = conn.execute("SELECT 1 FROM subnets WHERE active=1").fetchone() is not None
        conn.execute(
            "INSERT INTO subnets (cidr, name, kind, grid_cols, grid_rows, layout_json, active) VALUES (?,?,?,?,?,?,?)",
            (sc["cidr"], sc["name"], sc["kind"], sc["cols"], sc["rows"], json.dumps(sc["layout"]), 0 if has_active else 1),
        )
        subnet = conn.execute("SELECT * FROM subnets WHERE cidr=?", (sc["cidr"],)).fetchone()
        for n in sc["nodes"]:
            _insert_node(conn, subnet, n["name"], n["type"], n["x"], n["y"], n["state"])
        for r in sc["rumors"]:
            _insert_node(conn, subnet, r["name"], "rumor", -1, -1, r["state"])
    for sc in SCENES:   # hierarchy
        conn.execute(
            "UPDATE subnets SET parent_id=(SELECT id FROM subnets WHERE cidr=?) WHERE cidr=?",
            (sc.get("parent"), sc["cidr"]),
        )
    if not conn.execute("SELECT 1 FROM subnets WHERE active=1").fetchone():
        conn.execute("UPDATE subnets SET active=1 WHERE id=(SELECT MIN(id) FROM subnets)")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _resolve(conn, ref=None, activate=False) -> sqlite3.Row:
    """Find a scene by cidr or numeric id; default to the last active one."""
    if ref not in (None, ""):
        ref = str(ref).strip()
        row = conn.execute(
            "SELECT * FROM subnets WHERE cidr=? OR id=?", (ref, int(ref) if ref.isdigit() else -1)
        ).fetchone()
        if not row:
            raise LookupError("scene not found")
        if activate:
            conn.execute("UPDATE subnets SET active = (id = ?)", (row["id"],))
            conn.execute("UPDATE subnets SET visited=? WHERE id=?", (time.time(), row["id"]))
            row = conn.execute("SELECT * FROM subnets WHERE id=?", (row["id"],)).fetchone()
        return row
    row = conn.execute("SELECT * FROM subnets WHERE active=1 LIMIT 1").fetchone()
    return row or conn.execute("SELECT * FROM subnets ORDER BY id LIMIT 1").fetchone()


def _pad(subnet) -> dict:
    try:
        p = json.loads(subnet["pad_json"] or "{}")
    except (KeyError, ValueError, TypeError):
        p = {}
    return {k: int(p.get(k, 0)) for k in "nsew"}


def _moves(subnet) -> dict:
    """Landmark repositioning overrides, kept in meta so they survive the code layouts being refreshed at startup."""
    try:
        return json.loads(subnet["meta_json"] or "{}").get("moves") or {}
    except (KeyError, ValueError, TypeError):
        return {}


def _raw_items(subnet) -> list:
    """[(stable key, item)]: code/template layout then the GM's custom items, before any repositioning."""
    out = [(f"b|{it['kind']}|{it.get('label', '')}|{it['x']},{it['y']}", it) for it in json.loads(subnet["layout_json"])]
    try:
        custom = json.loads(subnet["custom_json"] or "[]")
    except (KeyError, ValueError, TypeError):
        custom = []
    out += [(f"c|{i}", it) for i, it in enumerate(custom)]
    return out


def _base_items(subnet) -> list[dict]:
    """Layout plus custom items in the *base frame* (no padding), with repositioned landmarks at their new tiles."""
    moves, out = _moves(subnet), []
    for key, it in _raw_items(subnet):
        mv = moves.get(key)
        if mv:
            c = dict(it)
            en = c.get("enter")
            if en and en.get("generate") and not en["generate"].get("origin"):
                # the child scene is keyed by the ORIGINAL tile; keep that key stable when the pin moves
                c["enter"] = {**en, "generate": {**en["generate"], "origin": f"{it['x']},{it['y']}"}}
            c["x"], c["y"] = mv["to"]
            c["carve"] = True                      # a repositioned pin is always standable
            it = c
        out.append(it)
    return out


def _layout(subnet) -> list[dict]:
    """Effective layout in grid coordinates: base + custom items shifted by the expansion padding."""
    pad = _pad(subnet)
    out = []
    for it in _base_items(subnet):
        c = dict(it)
        c["x"] += pad["w"]
        c["y"] += pad["n"]
        out.append(c)
    return out


def _target_xy(target, portal) -> tuple[int, int]:
    """Portal destinations are stored in the target's base frame; they follow a landmark that was repositioned."""
    pad = _pad(target)
    px, py = portal["x"], portal["y"]
    for mv in _moves(target).values():
        if [px, py] in mv.get("past", [mv["from"]]) or [px, py] == mv["from"]:
            px, py = mv["to"]
            break
    return px + pad["w"], py + pad["n"]


def _blocked(subnet) -> set[tuple[int, int]]:
    cells = set()
    for it in _layout(subnet):          # later items win: `carve` land clears blocking under it
        if it.get("block"):
            op = cells.add
        elif it.get("carve"):
            op = cells.discard
        else:
            continue
        for dx in range(it["w"]):
            for dy in range(it["h"]):
                op((it["x"] + dx, it["y"] + dy))
    return cells


def _portal_at(subnet, x: int, y: int) -> dict | None:
    for it in _layout(subnet):
        if it.get("portal") and it["x"] <= x < it["x"] + it["w"] and it["y"] <= y < it["y"] + it["h"]:
            return it["portal"]
    return None


def _auto_door_at(subnet, x: int, y: int) -> dict | None:
    """A stamped building's door: stepping on it generates the interior and carries the token in (base frame)."""
    pad = _pad(subnet)
    bx, by = x - pad["w"], y - pad["n"]
    for it in _base_items(subnet):
        if it.get("auto") and (it.get("enter") or {}).get("generate") \
                and it["x"] <= bx < it["x"] + it["w"] and it["y"] <= by < it["y"] + it["h"]:
            return it
    return None


def _occupied(conn, subnet, exclude_id: int | None = None) -> set[tuple[int, int]]:
    rows = conn.execute(
        "SELECT id, x, y FROM nodes WHERE subnet_id=? AND x>=0 AND y>=0", (subnet["id"],)
    ).fetchall()
    return {(r["x"], r["y"]) for r in rows if r["id"] != exclude_id}


def _nearest_free(conn, subnet, x: int, y: int) -> tuple[int, int]:
    cols, rows = subnet["grid_cols"], subnet["grid_rows"]
    x = max(0, min(cols - 1, int(x)))
    y = max(0, min(rows - 1, int(y)))
    portals = {
        (it["x"] + dx, it["y"] + dy)
        for it in _layout(subnet) if it.get("portal") or it.get("auto")
        for dx in range(it["w"]) for dy in range(it["h"])
    }
    taken = _blocked(subnet) | _occupied(conn, subnet) | portals   # never spawn onto a portal
    if (x, y) not in taken:
        return x, y
    for ring in range(1, max(cols, rows)):
        for dx in range(-ring, ring + 1):
            for dy in range(-ring, ring + 1):
                if max(abs(dx), abs(dy)) != ring:
                    continue
                cx, cy = x + dx, y + dy
                if 0 <= cx < cols and 0 <= cy < rows and (cx, cy) not in taken:
                    return cx, cy
    return x, y


def _next_ip(conn, subnet) -> str:
    net = ipaddress.ip_network(subnet["cidr"])
    used = {r["ip"] for r in conn.execute("SELECT ip FROM nodes WHERE subnet_id=?", (subnet["id"],))}
    base = int(net.network_address)
    for host in net.hosts():
        if int(host) - base >= 10 and str(host) not in used:
            return str(host)
    raise RuntimeError("subnet exhausted")


def _insert_node(conn, subnet, name, type_, x, y, state, mask: int = 3) -> int:
    cur = conn.execute(
        "INSERT INTO nodes (subnet_id, ip, name, type, x, y, state_json, visibility_mask, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?)",
        (subnet["id"], _next_ip(conn, subnet), name, type_, x, y, json.dumps(state), mask, time.time()),
    )
    return cur.lastrowid


NODE_SELECT = (
    "SELECT n.*, m.status AS m_status, m.updated_at AS m_updated "
    "FROM nodes n LEFT JOIN media_cache m ON m.node_id = n.id"
)


def _node(row) -> dict:
    ready = row["m_status"] == "ready"
    return {
        "id": row["id"],
        "ip": row["ip"],
        "name": row["name"],
        "type": row["type"],
        "x": row["x"],
        "y": row["y"],
        "state": json.loads(row["state_json"]),
        "visibility_mask": row["visibility_mask"],
        "scene_id": row["subnet_id"],
        "portrait": f"/assets/media/{row['id']}.png?v={int(row['m_updated'])}" if ready else None,
        "portrait_status": row["m_status"] or "none",
    }


def _meta(subnet) -> dict:
    try:
        return json.loads(subnet["meta_json"] or "{}")
    except (KeyError, ValueError):
        return {}


def _scene_meta(subnet) -> dict:
    d = SCENE_BY_CIDR.get(subnet["cidr"])
    if d:
        return {"short": d["short"], "icon": d["icon"]}
    m = _meta(subnet)
    return {"short": m.get("short", subnet["name"]), "icon": m.get("icon", "📍")}


def _by_id(conn, sid):
    return conn.execute("SELECT * FROM subnets WHERE id=?", (sid,)).fetchone() if sid else None


def _ancestors(conn, subnet) -> list[sqlite3.Row]:
    chain, cur, guard = [], _by_id(conn, subnet["parent_id"]), 0
    while cur and guard < 12:
        chain.insert(0, cur)
        cur, guard = _by_id(conn, cur["parent_id"]), guard + 1
    return chain


def _crumb(row) -> dict:
    return {"cidr": row["cidr"], "name": row["name"], **_scene_meta(row)}


def _trail(conn, subnet) -> list[dict]:
    """Ancestors > current > the most recently visited path below it."""
    chain = _ancestors(conn, subnet) + [subnet]
    cur = subnet
    for _ in range(8):
        kid = conn.execute(
            "SELECT * FROM subnets WHERE parent_id=? AND visited IS NOT NULL ORDER BY visited DESC LIMIT 1", (cur["id"],)
        ).fetchone()
        if not kid:
            break
        chain.append(kid)
        cur = kid
    return [_crumb(r) for r in chain]


def _origin_key(item, off=(0, 0)) -> str:
    g = (item.get("enter") or {}).get("generate") or {}
    return g.get("origin") or f"{item['x'] - off[0]},{item['y'] - off[1]}"


def _find_child(conn, subnet, item, off=(0, 0)) -> sqlite3.Row | None:
    key = _origin_key(item, off)
    for kid in conn.execute("SELECT * FROM subnets WHERE parent_id=?", (subnet["id"],)):
        if _meta(kid).get("origin") == key:
            return kid
    return None


def _decorate(conn, subnet, layout: list[dict]) -> list[dict]:
    """Attach `link` to every enterable item: where it leads and whether that scene exists yet."""
    ancestors = {r["cidr"] for r in _ancestors(conn, subnet)}
    pad = _pad(subnet)
    for it in layout:
        en = it.get("enter")
        if not en:
            continue
        if en.get("to"):
            t = conn.execute("SELECT * FROM subnets WHERE cidr=?", (en["to"],)).fetchone()
            if t:
                it["link"] = {"cidr": t["cidr"], "name": t["name"], "exists": True,
                              "relation": "up" if t["cidr"] in ancestors else "down"}
        elif en.get("generate"):
            kid = _find_child(conn, subnet, it, (pad["w"], pad["n"]))
            if kid:
                it["link"] = {"cidr": kid["cidr"], "name": kid["name"], "exists": True, "relation": "down"}
            else:
                it["link"] = {"name": en["generate"]["name"], "exists": False, "relation": "down"}
    return layout


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #
def list_scenes() -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM nodes n WHERE n.subnet_id=s.id AND n.type!='rumor') AS tokens, "
            "(SELECT p.cidr FROM subnets p WHERE p.id=s.parent_id) AS parent "
            "FROM subnets s ORDER BY s.id"
        ).fetchall()
        out = [
            {"id": r["id"], "cidr": r["cidr"], "name": r["name"], "kind": r["kind"], "parent": r["parent"],
             "active": bool(r["active"]), "tokens": r["tokens"], **_scene_meta(r)}
            for r in rows
        ]
        out.sort(key=lambda sc: (SCOPE_ORDER.index(sc["cidr"]) if sc["cidr"] in SCOPE_ORDER else len(SCOPE_ORDER), sc["id"]))
        return out


def get_scene(ref=None) -> dict:
    """Scene by cidr/id; viewing a scene makes it the active (last-viewed) one."""
    with db() as conn:
        subnet = _resolve(conn, ref, activate=True)
        rows = conn.execute(NODE_SELECT + " WHERE n.subnet_id=? ORDER BY n.id", (subnet["id"],)).fetchall()
        parent = _by_id(conn, subnet["parent_id"])
        return {
            "subnet": {
                "id": subnet["id"], "cidr": subnet["cidr"], "name": subnet["name"], "kind": subnet["kind"],
                "cols": subnet["grid_cols"], "rows": subnet["grid_rows"],
                "layout": _decorate(conn, subnet, _layout(subnet)),
                "parent": parent["cidr"] if parent else None,
                "trail": _trail(conn, subnet),
                "bg": _bg_payload(subnet),
                "arrival": _arrival_payload(subnet),
                **_scene_meta(subnet),
            },
            "nodes": [_node(r) for r in rows],
        }


def scene_context(ref=None) -> dict:
    """Compact room description handed to the directive parser."""
    with db() as conn:
        subnet = _resolve(conn, ref)
        marks, seen = [], set()
        for it in _layout(subnet):
            label = shortlabel = (it.get("label") or "").split(" — ")[0]
            if label and it["kind"] not in ("areaname", "bwall") and label not in seen:
                seen.add(label)
                marks.append(f"{label} (x{it['x']}-{it['x'] + it['w'] - 1}, y{it['y']}-{it['y'] + it['h'] - 1})")
        skip = {"areaname", "bwall", "citywall", "wall", "snowpeak", "mountain", "blocker", "ocean", "river",
                "border", "track", "gravel", "highroad", "scrub", "forest", "rug"}
        places, pseen = [], set()
        for it in _layout(subnet):
            lab = (it.get("label") or "").split(" — ")[0]
            if lab and lab not in pseen and it["kind"] not in skip:
                pseen.add(lab)
                places.append({"name": lab, "x": it["x"] + it["w"] // 2, "y": it["y"] + it["h"] // 2})
        d = SCENE_BY_CIDR.get(subnet["cidr"]) or _meta(subnet)
        return {"places": places[:80],
            "room": subnet["name"], "kind": subnet["kind"], "cols": subnet["grid_cols"], "rows": subnet["grid_rows"],
            "landmarks": marks[:40], "anchors": d.get("anchors"), "default": d.get("default"), "notes": d.get("notes"),
        }


def move_node(node_id: int, x: int, y: int) -> tuple[dict, dict | None]:
    """Move a token. Returns (node, transition); transition is set when a portal was used."""
    with db() as conn:
        row = conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        if not row:
            raise LookupError("node not found")
        if row["x"] < 0:
            raise ValueError("this entry is not a token")
        subnet = conn.execute("SELECT * FROM subnets WHERE id=?", (row["subnet_id"],)).fetchone()
        if not (0 <= x < subnet["grid_cols"] and 0 <= y < subnet["grid_rows"]):
            raise ValueError("outside the scene")
        if (x, y) in _blocked(subnet):
            raise ValueError("that tile is blocked")
        if (x, y) in _occupied(conn, subnet, exclude_id=node_id):
            raise ValueError("another token is there")

        transition = None
        portal = _portal_at(subnet, x, y)
        if portal:
            target = conn.execute("SELECT * FROM subnets WHERE cidr=?", (portal["to"],)).fetchone()
            if target:
                px, py = _target_xy(target, portal)
                transition = _teleport(conn, row, target, px, py)
        else:
            door = _auto_door_at(subnet, x, y)
            if door:
                child, _ = _ensure_child(conn, subnet, door)
                ax, ay = _meta(child).get("arrive") or _meta(child).get("default") or [12, 12]
                transition = _teleport(conn, row, child, ax, ay)
        if not transition:
            conn.execute("UPDATE nodes SET x=?, y=? WHERE id=?", (x, y, node_id))
        out = conn.execute(NODE_SELECT + " WHERE n.id=?", (node_id,)).fetchone()
        return _node(out), transition


def _teleport(conn, row, target, px: int, py: int) -> dict:
    if row["type"] == "party":   # one party marker: retire the placeholder in the destination
        conn.execute(
            "DELETE FROM nodes WHERE subnet_id=? AND type='party' AND name=? AND id!=?",
            (target["id"], row["name"], row["id"]),
        )
    arrival = _meta(target).get("arrival")          # GM-configured arrival tile wins over the portal's own target
    if arrival:
        pad = _pad(target)
        px, py = arrival[0] + pad["w"], arrival[1] + pad["n"]
    tx, ty = _nearest_free(conn, target, px, py)
    conn.execute(
        "UPDATE nodes SET subnet_id=?, ip=?, x=?, y=? WHERE id=?",
        (target["id"], _next_ip(conn, target), tx, ty, row["id"]),
    )
    meta = _scene_meta(target)
    return {"cidr": target["cidr"], "name": target["name"], "short": meta["short"], "x": tx, "y": ty}


_TYPE_KEYWORDS = [
    ("barkeep", ("barkeep", "bartender", "innkeeper", "publican")),
    ("pc", ("player character",)),
    ("guard", ("guard", "watch", "soldier", "sentry")),
    ("army", ("army", "legion", "regiment", "warband", "fleet", "faction")),
    ("beast", ("beast", "wolf", "wolves", "bear", "boar", "hound", "creature", "monster", "pack", "dragon", "wyrm", "drake")),
    ("party", ("party", "adventurer")),
    ("patron", ("patron", "drunk", "regular", "traveler", "traveller", "customer", "merchant", "bard", "minstrel")),
]


def _type_for(role: str) -> str:
    r = role.lower()
    for type_, words in _TYPE_KEYWORDS:
        if any(w in r for w in words):
            return type_
    return "npc"


def _provision(conn, packet: dict, ref=None) -> dict:
    """Create a token or rumor on an open connection (shared by instant commands and blueprints)."""
    secret = str(packet.get("secret", "")).strip()
    m = re.search(r"\bDC\s*(\d{1,2})\b", secret, re.I)
    dc = int(m.group(1)) if m else 12
    secret = re.sub(r"^\s*DC\s*\d{1,2}\s*[:\-–]\s*", "", secret, flags=re.I)
    is_rumor = packet.get("action") == "LOG_RUMOR"
    state = {
        "role": packet.get("role", "") or ("rumor" if is_rumor else "Patron"),
        "motivation": packet.get("motivation", ""),
        "secret": secret,
        "dc": dc,
        "prompt_portrait": packet.get("prompt_portrait", ""),
    }
    subnet = _resolve(conn, ref)
    if is_rumor:
        nid = _insert_node(conn, subnet, packet["name"], "rumor", -1, -1, state)
    else:
        type_ = _type_for(state["role"])
        if type_ == "beast":
            state["disposition"] = "hostile"
        x, y = _nearest_free(conn, subnet, packet.get("x", 11), packet.get("y", 7))
        nid = _insert_node(conn, subnet, packet["name"], type_, x, y, state)
    return _node(conn.execute(NODE_SELECT + " WHERE n.id=?", (nid,)).fetchone())


def provision_host(packet: dict, ref=None) -> dict:
    """Create a token or rumor in the scene identified by ref (default: active scene)."""
    with db() as conn:
        return _provision(conn, packet, ref)


def get_node(node_id: int) -> dict | None:
    with db() as conn:
        row = conn.execute(NODE_SELECT + " WHERE n.id=?", (node_id,)).fetchone()
        return _node(row) if row else None


def set_media(node_id: int, status: str, path: str | None = None, prompt: str | None = None) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO media_cache (node_id, path, prompt, status, updated_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(node_id) DO UPDATE SET status=excluded.status, "
            "path=COALESCE(excluded.path, media_cache.path), "
            "prompt=COALESCE(excluded.prompt, media_cache.prompt), updated_at=excluded.updated_at",
            (node_id, path, prompt, status, time.time()),
        )


def nodes_needing_portrait() -> list[tuple[int, str, str]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT n.id, n.name, n.state_json FROM nodes n "
            "LEFT JOIN media_cache m ON m.node_id = n.id "
            "WHERE n.type != 'rumor' AND (m.node_id IS NULL OR m.status = 'failed') ORDER BY n.id"
        ).fetchall()
        return [(r["id"], r["name"], json.loads(r["state_json"]).get("prompt_portrait", "")) for r in rows]


# --------------------------------------------------------------------------- #
# Scene generation: child scenes created the first time the GM delves in
# --------------------------------------------------------------------------- #
def _exit(parent, it, x, y, w, h, label, kind="gate"):
    """A passage back to the parent scene (arrives on the landmark the GM came from)."""
    g = (it.get("enter") or {}).get("generate") or {}
    tx, ty = g.get("exit") or (it["x"], it["y"])
    return _i(kind, x, y, w, h, False, label,
              portal={"to": parent, "x": tx, "y": ty}, enter={"to": parent},
              desc="The way back out.")


def _npc(name, role, x, y, dc, motivation, secret, portrait, type_="npc", disposition="neutral"):
    return dict(name=name, type=type_, x=x, y=y,
                state=dict(role=role, dc=dc, disposition=disposition, motivation=motivation,
                           secret=secret, prompt_portrait=portrait))


def _tpl_port(name, parent, it):
    layout = [
        _i("ocean", 0, 0, 2, 16, True, "Cerulean Deep"),
        _i("river", 2, 0, 2, 16, False, "Shallows"),
        _i("dock", 2, 4, 2, 1, False, "Pier"), _i("dock", 2, 10, 2, 1, False, "Pier"),
        _i("dock", 4, 1, 2, 14, False, "Quay"),
        _i("ship", 0, 2, 2, 3, True, "Merchant cog"), _i("ship", 0, 9, 2, 4, True, "Black-sailed brig"),
        _i("cobble", 6, 7, 18, 2, False, "Harbor Street"),
        _i("cobble", 12, 3, 6, 10, False, "Market quay"),
        _i("tower", 6, 0, 2, 2, True, "Valen Light", desc="The lighthouse that has guided ships onto the rocks, and off them."),
        _i("tavern", 7, 3, 4, 3, True, "Salt warehouse"), _i("tavern", 7, 10, 4, 3, True, "Cargo warehouse"),
        _i("tavern", 19, 2, 4, 4, True, "The Salted Gull"), _i("tavern", 19, 10, 4, 3, True, "Chandlery"),
        _i("barrel", 11, 13, 1, 1, True), _i("crate", 6, 6, 1, 1, True), _i("crate", 17, 6, 1, 1, True),
        _exit(parent, it, 23, 7, 1, 2, "Road out of Port Valen"),
    ]
    return dict(kind="exterior", cols=24, rows=16, layout=layout, default=[15, 8],
                anchors=[[r"\b(dock|pier|quay|ship|boat)\b", [5, 8]], [r"\b(market|square|plaza)\b", [15, 8]],
                         [r"\b(inn|tavern|gull)\b", [20, 7]], [r"\b(gate|road|exit)\b", [22, 8]]],
                notes="Harbor town. Ocean and ships on the west, a quay and warehouses, a market quay, the Salted Gull inn in the east. The road out is east.",
                nodes=[_npc("Harbormaster Elsa Varn", "Harbormaster", 13, 6, 12,
                            "Keep the quay clear and the tariffs flowing.",
                            "Quietly lets an unregistered black-sailed brig dock after dark.",
                            "Stern middle-aged harbormaster, weathered face, navy coat, brass spyglass")])


def _tpl_capital(name, parent, it):
    layout = [
        _i("cobble", 7, 5, 10, 8, False, "Royal courtyard"), _i("cobble", 11, 5, 2, 10, False, "Crown Avenue"),
        _i("wall", 0, 0, 8, 1, True, "Highmark wall"), _i("wall", 16, 0, 8, 1, True, "Highmark wall"),
        _i("wall", 0, 1, 1, 14, True, "Highmark wall"), _i("wall", 23, 1, 1, 14, True, "Highmark wall"),
        _i("wall", 0, 15, 11, 1, True, "Highmark wall"), _i("wall", 13, 15, 11, 1, True, "Highmark wall"),
        _i("tower", 8, 0, 8, 4, True, "The Royal Keep", desc="Seat of the Crown: throne room, war council and a very full dungeon."),
        _i("tavern", 1, 3, 4, 3, True, "Guildhall"), _i("tavern", 1, 8, 4, 3, True, "Counting house"),
        _i("tavern", 19, 3, 4, 3, True, "Barracks"), _i("tavern", 19, 8, 4, 3, True, "Embassy row"),
        _i("well", 11, 8, 2, 2, True, "Crown Fountain"),
        _exit(parent, it, 11, 15, 2, 1, "Highmark's south gate"),
    ]
    return dict(kind="exterior", cols=24, rows=16, layout=layout, default=[12, 11],
                anchors=[[r"\b(keep|throne|palace|king|crown)\b", [12, 5]], [r"\b(gate|exit|road)\b", [12, 14]],
                         [r"\b(barracks|guard)\b", [18, 6]], [r"\b(fountain|courtyard|square)\b", [14, 10]]],
                notes="Walled royal capital. The Royal Keep is at the top, a courtyard and fountain in the middle, guild and barracks quarters on the sides, the south gate at the bottom.",
                nodes=[_npc("Lord Chamberlain Aldric", "Court Chamberlain", 12, 6, 15,
                            "Keep the Crown's embargo orders out of the common gossip.",
                            "Is selling the embargo schedule to eastern timber merchants.",
                            "Elegant older courtier, silver chain of office, calculating eyes, crimson velvet")])


def _tpl_citadel(name, parent, it):
    layout = [
        _i("wall", 0, 0, 24, 1, True, "Citadel wall"), _i("wall", 0, 1, 1, 14, True, "Citadel wall"),
        _i("wall", 23, 1, 1, 14, True, "Citadel wall"),
        _i("wall", 0, 15, 11, 1, True, "Citadel wall"), _i("wall", 13, 15, 11, 1, True, "Citadel wall"),
        _i("river", 3, 3, 4, 3, True, "Flooded nave"), _i("river", 17, 10, 4, 3, True, "Flooded crypt"),
        _i("pillar", 5, 8, 1, 1, True), _i("pillar", 9, 5, 1, 1, True), _i("pillar", 14, 5, 1, 1, True),
        _i("pillar", 18, 5, 1, 1, True), _i("pillar", 9, 11, 1, 1, True), _i("pillar", 14, 11, 1, 1, True),
        _i("altar", 11, 2, 2, 2, True, "The Oath-Stone", desc="A black stone etched with a binding oath. The wyrm's chain runs through it."),
        _i("crate", 2, 12, 1, 1, True), _i("crate", 3, 13, 1, 1, True),
        _exit(parent, it, 11, 15, 2, 1, "Climb back to the surface", kind="door"),
    ]
    return dict(kind="dungeon", cols=24, rows=16, layout=layout, default=[12, 8],
                anchors=[[r"\b(altar|stone|oath)\b", [12, 5]], [r"\b(door|exit|stairs)\b", [12, 14]],
                         [r"\b(pool|water|nave|crypt)\b", [8, 7]]],
                notes="Drowned fortress hall. The Oath-Stone altar is at the top, flooded pools left and right, pillars between, and the exit door at the bottom.",
                nodes=[_npc("The Drowned Warden", "Citadel Warden", 12, 8, 16,
                            "Keep the oath-stone from being broken.",
                            "Would gladly be freed; it only needs the oath-stone shattered at the new moon.",
                            "Waterlogged ghostly knight, corroded armor, pale blue light in the visor",
                            disposition="hostile")])


def _tpl_mine(name, parent, it):
    layout = [
        _i("mountain", 0, 0, 24, 2, True, "Rock"), _i("mountain", 0, 2, 2, 12, True, "Rock"),
        _i("mountain", 22, 2, 2, 12, True, "Rock"),
        _i("mountain", 0, 14, 10, 2, True, "Rock"), _i("mountain", 14, 14, 10, 2, True, "Rock"),
        _i("mountain", 5, 4, 4, 3, True, "Collapsed stope"), _i("mountain", 14, 3, 5, 3, True, "Rock"),
        _i("mountain", 10, 8, 5, 3, True, "Pillar of ore"),
        _i("river", 16, 10, 4, 3, True, "Flooded shaft"),
        _i("crate", 3, 9, 1, 1, True, "Ore cart"), _i("crate", 19, 8, 1, 1, True, "Ore cart"),
        _i("anvil", 12, 3, 1, 1, True, "The alarm bell", desc="A rust-green bell on a timber frame. It should not be able to ring."),
        _exit(parent, it, 10, 14, 4, 2, "The adit — back to daylight", kind="door"),
    ]
    return dict(kind="dungeon", cols=24, rows=16, layout=layout, default=[12, 7],
                anchors=[[r"\b(bell|alarm)\b", [12, 4]], [r"\b(exit|adit|door|entrance)\b", [12, 13]],
                         [r"\b(shaft|water|pool)\b", [15, 9]], [r"\b(cart|ore)\b", [4, 9]]],
                notes="Abandoned iron mine. Timbered tunnels around rock pillars, an alarm bell at the top, a flooded shaft on the right, the adit exit at the bottom.",
                nodes=[_npc("Ghostly Foreman", "Mine Foreman (spirit)", 12, 6, 14,
                            "Warn trespassers off, then finish his shift.",
                            "Rings the bell on moonless nights to warn of a collapse that already happened.",
                            "Translucent miner in a hard hat of hide, lantern glow, hollow eyes", disposition="hostile")])


def _tpl_tower(name, parent, it):
    layout = [
        _i("forest", 0, 0, 24, 16, False, "Hill woods"),
        _i("wall", 8, 4, 8, 1, True, "Ruined wall"), _i("wall", 8, 5, 1, 6, True, "Ruined wall"),
        _i("wall", 15, 5, 1, 6, True, "Ruined wall"),
        _i("wall", 8, 10, 3, 1, True, "Ruined wall"), _i("wall", 13, 10, 3, 1, True, "Ruined wall"),
        _i("tower", 11, 5, 2, 3, True, "The fallen watchtower", desc="Half the tower lies in the courtyard. A lamp still burns near the top."),
        _i("crate", 10, 8, 1, 1, True), _i("crate", 14, 8, 1, 1, True),
        _i("edge", 0, 15, 24, 1, False, "South edge — back to the Wilds",
           portal={"to": parent, "x": it["x"], "y": it["y"]}),
    ]
    return dict(kind="exterior", cols=24, rows=16, layout=layout, default=[12, 12],
                anchors=[[r"\b(tower|lamp|top)\b", [12, 9]], [r"\b(wall|ruins?)\b", [9, 8]],
                         [r"\b(woods?|forest|trees?)\b", [4, 8]], [r"\b(exit|path|road)\b", [12, 13]]],
                notes="Ruined watchtower in dense hill woods. A broken wall ring surrounds the fallen tower; the way out is the south edge.",
                nodes=[_npc("Squatter Jory", "Hermit Signaller", 13, 12, 12,
                            "Keep the lamp lit and the strangers away.",
                            "Signals to the mine at night with a shuttered lantern; he has never met who answers.",
                            "Gaunt hermit, patchwork cloak, lantern in hand, wary squint")])


def _tpl_generic(name, parent, it):
    layout = [
        _i("wall", 0, 0, 24, 1, True, "Wall"), _i("wall", 0, 1, 1, 14, True, "Wall"), _i("wall", 23, 1, 1, 14, True, "Wall"),
        _i("wall", 0, 15, 11, 1, True, "Wall"), _i("wall", 13, 15, 11, 1, True, "Wall"),
        _i("table", 10, 6, 4, 2, True, "Table"),
        _exit(parent, it, 11, 15, 2, 1, "Way out", kind="door"),
    ]
    return dict(kind="room", cols=24, rows=16, layout=layout, default=[12, 10],
                anchors=[[r"\b(door|exit)\b", [12, 14]], [r"\b(table)\b", [12, 9]]],
                notes="A plain walled room with a table and an exit door at the bottom.", nodes=[])


TEMPLATES = {"port": _tpl_port, "capital": _tpl_capital, "citadel": _tpl_citadel,
             "mine": _tpl_mine, "tower": _tpl_tower, "generic": _tpl_generic}


def _next_free_cidr(conn) -> str:
    used = {r["cidr"] for r in conn.execute("SELECT cidr FROM subnets")}
    for n in range(2, 250):
        cand = f"10.{n}.0.0/24"
        if cand not in used:
            return cand
    raise RuntimeError("no free subnet")


def _summary(row, created: bool) -> dict:
    return {"id": row["id"], "cidr": row["cidr"], "name": row["name"], "kind": row["kind"],
            "created": created, **_scene_meta(row)}


def _ensure_child(conn, parent, item) -> tuple[sqlite3.Row, bool]:
    """Child scene behind an enterable item (base-frame coordinates); create it from its template if missing."""
    existing = _find_child(conn, parent, item)
    if existing:
        return existing, False
    g = item["enter"]["generate"]
    spec = TEMPLATES.get(g.get("template"), _tpl_generic)(g["name"], parent["cidr"], item)
    meta = {"icon": g.get("icon", "📍"), "short": g.get("short", g["name"]), "anchors": spec["anchors"],
            "default": spec["default"], "notes": spec["notes"], "origin": _origin_key(item),
            "template": g.get("template"), "arrive": spec.get("arrive")}
    cidr = _next_free_cidr(conn)
    conn.execute(
        "INSERT INTO subnets (cidr, name, kind, grid_cols, grid_rows, layout_json, active, parent_id, meta_json) "
        "VALUES (?,?,?,?,?,?,0,?,?)",
        (cidr, g["name"], spec["kind"], spec["cols"], spec["rows"], json.dumps(spec["layout"]), parent["id"], json.dumps(meta)),
    )
    row = conn.execute("SELECT * FROM subnets WHERE cidr=?", (cidr,)).fetchone()
    for n in spec["nodes"]:
        _insert_node(conn, row, n["name"], n["type"], n["x"], n["y"], n["state"])
    return row, True


def generate_child(parent_ref, x: int, y: int) -> dict:
    """Create (or return) the child scene behind the enterable item at grid tile (x, y) of the parent."""
    with db() as conn:
        parent = _resolve(conn, parent_ref)
        pad = _pad(parent)
        bx, by = x - pad["w"], y - pad["n"]
        item = next((it for it in _base_items(parent)
                     if it["x"] == bx and it["y"] == by and (it.get("enter") or {}).get("generate")), None)
        if not item:
            raise LookupError("nothing to generate at that spot")
        row, created = _ensure_child(conn, parent, item)
        return _summary(row, created)


# --------------------------------------------------------------------------- #
# Building interiors (stamped buildings generate one of these on first entry)
# --------------------------------------------------------------------------- #
_FIRST = ["Marta", "Orrin", "Delia", "Bram", "Ysolde", "Cass", "Tobin", "Wynn", "Perrin", "Isa"]


def _tpl_interior(name, parent, it):
    g = (it.get("enter") or {}).get("generate") or {}
    role = (g.get("role") or "house").lower()
    rng = random.Random(name)
    first = rng.choice(_FIRST)
    layout = [
        _i("wall", 0, 0, 24, 1, True, "Wall"), _i("wall", 0, 1, 1, 14, True, "Wall"), _i("wall", 23, 1, 1, 14, True, "Wall"),
        _i("wall", 0, 15, 11, 1, True, "Wall"), _i("wall", 13, 15, 11, 1, True, "Wall"),
        _exit(parent, it, 11, 15, 2, 1, "Back outside", kind="door"),
    ]
    if any(w in role for w in ("tavern", "inn", "pub", "alehouse")):
        layout += [_i("rug", 8, 8, 8, 5), _i("shelf", 6, 1, 10, 2, True, "Shelves"), _i("bar", 6, 4, 10, 1, True, "Bar counter"),
                   _i("hearth", 1, 5, 2, 4, True, "Hearth"), _i("barrel", 21, 1, 1, 1, True), _i("barrel", 21, 2, 1, 1, True),
                   _i("rtable", 6, 9, 2, 2, True, "Round table"), _i("rtable", 15, 9, 2, 2, True, "Round table"),
                   _i("table", 18, 4, 3, 2, True, "Corner table")]
        layout += [_i("stool", x, 5) for x in (7, 9, 11, 13)] + [_i("stool", 5, 10), _i("stool", 8, 11), _i("stool", 14, 10), _i("stool", 17, 11)]
        npc = _npc(f"{first} the Keeper", "Barkeep", 10, 3, 12, "Keep the regulars happy and the books quiet.",
                   "Waters the cheap ale and pockets the difference.", "Cheerful barkeep, rolled sleeves, stained apron", "barkeep")
        arrive, notes = [12, 13], "Tavern interior: shelves and a bar counter at the top, hearth on the left, tables on the floor, exit door at the bottom."
    elif any(w in role for w in ("shop", "store", "alchem", "apothec", "bakery", "market", "library", "guild")):
        layout += [_i("shelf", 3, 1, 18, 2, True, "Wares"), _i("bar", 6, 6, 12, 1, True, "Counter"),
                   _i("rug", 8, 9, 8, 5), _i("crate", 2, 10, 1, 1, True), _i("crate", 2, 11, 1, 1, True),
                   _i("crate", 20, 10, 1, 1, True), _i("table", 16, 10, 3, 2, True, "Work table"), _i("barrel", 21, 4, 1, 1, True)]
        npc = _npc(f"{first} the Proprietor", "Shopkeeper", 12, 4, 13, "Sell at a profit and ask no questions.",
                   "Stocks a few items that nobody should be able to buy.", "Sharp-eyed shopkeeper, spectacles, ink-stained fingers")
        arrive, notes = [12, 13], "Shop interior: shelves of wares along the top, a counter across the middle, crates and a work table, exit door at the bottom."
    elif any(w in role for w in ("temple", "chapel", "church", "shrine", "abbey", "monastery")):
        layout += [_i("altar", 11, 2, 2, 2, True, "Altar"),
                   _i("bench", 5, 6, 5, 1, True, "Pews"), _i("bench", 14, 6, 5, 1, True, "Pews"),
                   _i("bench", 5, 9, 5, 1, True, "Pews"), _i("bench", 14, 9, 5, 1, True, "Pews"),
                   _i("pillar", 3, 3, 1, 1, True), _i("pillar", 20, 3, 1, 1, True), _i("pillar", 3, 11, 1, 1, True), _i("pillar", 20, 11, 1, 1, True)]
        npc = _npc(f"{first} the Keeper of the Flame", "Priest", 12, 5, 14, "Tend the faithful and the offering box.",
                   "Has not heard the god answer in years.", "Gentle priest, grey robes, candle-lit face")
        arrive, notes = [12, 13], "Temple interior: altar at the top, four rows of pews, pillars in the corners, exit door at the bottom."
    elif any(w in role for w in ("smith", "forge", "foundry")):
        layout += [_i("hearth", 1, 4, 2, 4, True, "Forge"), _i("anvil", 6, 6, 1, 1, True, "Anvil"),
                   _i("crate", 20, 2, 1, 1, True), _i("crate", 21, 2, 1, 1, True), _i("barrel", 20, 12, 1, 1, True),
                   _i("table", 15, 9, 3, 2, True, "Work bench")]
        npc = _npc(f"{first} the Smith", "Blacksmith", 8, 7, 12, "Finish the order before the quench cools.",
                   "Forges a second set of weapons for someone who pays in cash.", "Broad blacksmith, leather apron, soot-streaked arms")
        arrive, notes = [12, 13], "Smithy interior: forge on the left, anvil beside it, work bench on the right, exit door at the bottom."
    else:
        layout += [_i("rug", 8, 7, 8, 5), _i("hearth", 1, 6, 2, 3, True, "Hearth"), _i("rtable", 11, 8, 2, 2, True, "Table"),
                   _i("bench", 10, 11, 4, 1, True, "Bench"), _i("crate", 20, 2, 1, 1, True), _i("barrel", 21, 2, 1, 1, True)]
        npc = _npc(f"{first} the Resident", "Resident", 6, 10, 11, "Be left alone.",
                   "Knows something about the cellar that they will not discuss.", "Weathered local, patched clothes, wary smile")
        arrive, notes = [12, 13], "Small dwelling: hearth on the left, a table in the middle, exit door at the bottom."
    return dict(kind="room", cols=24, rows=16, layout=layout, default=[12, 10], arrive=arrive,
                anchors=[[r"\b(door|exit)\b", [12, 14]], [r"\b(table)\b", [12, 11]], [r"\b(hearth|fire)\b", [4, 8]]],
                notes=notes, nodes=[npc])


TEMPLATES["interior"] = _tpl_interior


# --------------------------------------------------------------------------- #
# World-builder: paint terrain, stamp buildings, expand the map
# --------------------------------------------------------------------------- #
MAX_COLS, MAX_ROWS = 128, 96

# anything that is not ground cover stops a building being stamped on top of it
GROUND_KINDS = {"cobble", "track", "scrub", "forest", "gravel", "highroad", "border", "rug", "ashland", "foothill",
                "areaname", "dock", "land", "desert", "swamp", "river", "ocean", "porch", "bfloor", "dirt"}

TERRAIN_SYNONYMS = [
    ("road", r"\b(road|trail|highway)\b"),
    ("pass", r"\b(pass|gap|col)\b"), ("mountain", r"\b(mountains?|peaks?|range|ridge|hills?)\b"),
    ("river", r"\b(river|stream|creek)\b"), ("lake", r"\b(lakes?|pond)\b"),
    ("island", r"\b(islands?|isles?|archipelago|atoll)\b"),
    ("kingdom", r"\b(kingdom|empire|realm|nation|continent|landmass|peninsula|land)\b"),
    ("forest", r"\b(forests?|woods?|woodlands?|jungle)\b"), ("desert", r"\b(deserts?|dunes?|wastes?)\b"),
    ("swamp", r"\b(swamps?|marsh(?:es)?|bog|fen)\b"), ("plains", r"\b(plains?|grasslands?|meadows?|steppe|prairie|farmland)\b"),
    ("sea", r"\b(seas?|oceans?|bay|gulf|strait)\b"),
]
DEFAULT_SIZE = {"island": (6, 5), "plains": (10, 7), "kingdom": (14, 10), "desert": (10, 8), "swamp": (8, 6),
                "forest": (8, 6), "mountain": (8, 6), "lake": (6, 4), "sea": (12, 8), "pass": (6, 12), "river": (14, 2), "road": (1, 1)}
TITLE = {"island": "Island", "plains": "Plains", "kingdom": "Kingdom", "desert": "Desert", "swamp": "Swamp",
         "forest": "Forest", "mountain": "Mountains", "lake": "Lake", "sea": "Sea", "pass": "Mountain pass", "river": "River", "road": "Road"}


def _canon_terrain(text: str) -> str | None:
    low = (text or "").lower()
    for key, pat in TERRAIN_SYNONYMS:
        if key == low or re.search(pat, low):
            return key
    return None


def _template_for(terrain: str, role: str, name: str) -> str:
    blob = f"{terrain} {role} {name}".lower()
    if re.search(r"citadel|dungeon|crypt|catacomb|vault", blob): return "citadel"
    if re.search(r"kingdom|city|capital|castle|palace|fortress|keep", blob): return "capital"
    if re.search(r"island|isle|port|harbou?r|dock|coast", blob): return "port"
    if re.search(r"mine|cave|mountain|pass|quarry", blob): return "mine"
    if re.search(r"tower|ruin|watch|forest|wood", blob): return "tower"
    return "generic"


def _paint_kind(terrain: str, scene_kind: str) -> tuple[str, bool, bool]:
    """(tile kind, blocks movement, carves blocking from what lies beneath)"""
    if terrain in ("mountain", "pass"):
        return ("snowpeak" if scene_kind == "world" else "mountain"), True, False
    if terrain in ("river", "lake"):
        return "river", True, False
    if terrain == "sea":
        return "ocean", True, False
    return {"desert": "desert", "swamp": "swamp", "forest": "forest"}.get(terrain, "land"), False, True


def _blob_runs(rng, cx, cy, w, h, cols, rows):
    runs = []
    for r in range(h):
        t = (r + 0.5) / h * 2 - 1
        half = (w / 2.0) * math.sqrt(max(0.0, 1 - t * t)) * (0.84 + 0.3 * rng.random())
        n = max(2, int(round(half * 2)))
        x0 = int(round(cx - n / 2.0)) + rng.choice((-1, 0, 0, 1))
        y0 = cy - h // 2 + r
        x0, x1 = max(0, x0), min(cols, x0 + n)
        if 0 <= y0 < rows and x1 > x0:
            runs.append((x0, y0, x1 - x0))
    return runs


def _save_custom(conn, subnet, items_eff: list[dict]) -> None:
    pad = _pad(subnet)
    custom = json.loads(subnet["custom_json"] or "[]")
    for it in items_eff:
        c = dict(it)
        c["x"] -= pad["w"]
        c["y"] -= pad["n"]
        custom.append(c)
    conn.execute("UPDATE subnets SET custom_json=? WHERE id=?", (json.dumps(custom), subnet["id"]))


def _icon_for(role: str, terrain: str = "") -> str:
    b = f"{role} {terrain}".lower()
    for pat, ic in ((r"lighthouse", "🗼"), (r"ruin", "🏚"), (r"dungeon|crypt|catacomb", "🕳"), (r"camp", "⛺"),
                    (r"monastery|abbey", "⛪"), (r"tavern|inn|pub", "🍺"), (r"shop|store|alchem|apothec|market", "⚖"),
                    (r"temple|chapel|church|shrine", "⛪"), (r"smith|forge", "⚒"), (r"citadel", "☠"),
                    (r"castle|fort|keep", "🏰"), (r"tower", "♜"), (r"port|harbou?r|island", "⚓"),
                    (r"kingdom|capital|city", "♛"), (r"town|village|hamlet|outpost", "🏘"), (r"mine|cave|pass|mountain", "⛏")):
        if re.search(pat, b):
            return ic
    return "🏠" if role else "📍"


def _landmark_item(name, template, role, x, y, size, variant, icon, desc):
    g = _gen(name, template, icon, name)
    g["generate"]["role"] = role
    lm = _i("landmark", x, y, size, size, False, name, variant=variant, enter=g, desc=desc)
    lm["carve"] = True            # the landmark tile is always standable, even on rock
    return lm


def _site_landmark(name, terrain, role, cx, cy, desc=None):
    return _landmark_item(name, _template_for(terrain, role, name), role, cx - 1, cy - 1, 2, "site",
                          _icon_for(role, terrain), desc or f"{name}: a new place on the map. Generate it to explore inside.")


def _paint(conn, subnet, packet) -> dict:
    cols, rows, kind = subnet["grid_cols"], subnet["grid_rows"], subnet["kind"]
    name = (packet.get("name") or "").strip()
    role = (packet.get("role") or "").strip()
    terrain = _canon_terrain(packet.get("style")) or _canon_terrain(role) or "island"
    if terrain == "road":
        return _paint_road(conn, subnet, packet)
    is_kingdom = terrain == "kingdom" or "kingdom" in role.lower()
    if is_kingdom and terrain == "kingdom":
        terrain = "plains"
    scale = max(cols, rows) / 24.0
    dw, dh = DEFAULT_SIZE["kingdom" if is_kingdom else terrain]
    w = int(packet.get("w") or 0) or max(3, int(dw * scale))
    h = int(packet.get("h") or 0) or max(2, int(dh * scale))
    w, h = min(w, cols - 2), min(h, rows - 2)
    cx = max(w // 2 + 1, min(cols - w // 2 - 2, int(packet.get("x", cols // 2))))
    cy = max(h // 2 + 1, min(rows - h // 2 - 2, int(packet.get("y", rows // 2))))
    rng = random.Random(f"{name}{terrain}{cx}{cy}")
    tile, block, carve = _paint_kind(terrain, kind)
    label = name or TITLE.get(terrain, terrain.title())
    group = f"p{int(time.time() * 1000) % 10**9}"
    items: list[dict] = []

    def run(k, x0, y0, rw, rh, blk, crv, lab=label):
        it = _i(k, x0, y0, rw, rh, blk, lab)
        if crv:
            it["carve"] = True
        it["group"] = group
        items.append(it)

    if terrain == "river":
        d = (packet.get("direction") or "south").lower()
        d = d if d in ("north", "south", "east", "west") else "south"
        length = int(packet.get("h") or packet.get("w") or 0) or max(8, int(14 * scale))
        dx, dy = {"south": (0, 1), "north": (0, -1), "east": (1, 0), "west": (-1, 0)}[d]
        x, y, lat = int(packet.get("x", cx)), int(packet.get("y", cy)), 0
        struct = {(it["x"] + i, it["y"] + j) for it in _layout(subnet) if it["kind"] not in GROUND_KINDS
                  for i in range(it["w"]) for j in range(it["h"])}
        for step in range(length):
            if step and step % 3 == 0:
                lat += rng.choice((-1, 0, 1))
            px, py = (x + lat, y + dy * step) if dx == 0 else (x + dx * step, y + lat)
            rw, rh = (2, 1) if dx == 0 else (1, 2)
            cells = {(px + i, py + j) for i in range(rw) for j in range(rh)}
            if 0 <= px and px + rw <= cols and 0 <= py and py + rh <= rows and not (cells & struct):
                run("river", px, py, rw, rh, True, False)
        cx, cy = x, y
    elif terrain == "pass":
        d = (packet.get("direction") or "east").lower()
        vertical = d in ("east", "west", "")           # a tall ridge you cross east-west
        rw, rh = (max(4, w), max(6, h)) if vertical else (max(6, h), max(4, w))
        lo, hi = (cy - 1, cy) if vertical else (cx - 1, cx)
        for x0, y0, n in _blob_runs(rng, cx, cy, rw, rh, cols, rows):
            if vertical:
                if lo <= y0 <= hi:
                    run("gravel", x0, y0, n, 1, False, True, "Mountain pass")
                else:
                    run(tile, x0, y0, n, 1, True, False, "Mountains")
            else:
                a_, b_ = x0, x0 + n
                gl, gr = max(lo, a_), min(hi + 1, b_)
                if gr <= gl:
                    run(tile, a_, y0, n, 1, True, False, "Mountains")
                else:
                    if gl > a_: run(tile, a_, y0, gl - a_, 1, True, False, "Mountains")
                    run("gravel", gl, y0, gr - gl, 1, False, True, "Mountain pass")
                    if b_ > gr: run(tile, gr, y0, b_ - gr, 1, True, False, "Mountains")
    else:
        base_kind = tile
        if is_kingdom:
            style_t = _canon_terrain(packet.get("style"))
            base_kind = {"desert": "desert", "swamp": "swamp", "forest": "forest"}.get(style_t or "", "land")
            block, carve = False, True
        for x0, y0, n in _blob_runs(rng, cx, cy, w, h, cols, rows):
            run(base_kind, x0, y0, n, 1, block, carve)
        if is_kingdom:   # dotted border around the landmass
            xs = [i["x"] for i in items]; xe = [i["x"] + i["w"] for i in items]; ys = [i["y"] for i in items]
            x0, x1, y0, y1 = min(xs), max(xe), min(ys), max(ys)
            bl = f"Border of {name or 'the kingdom'}"
            for rect in ((x0, y0, x1 - x0, 1), (x0, y1, x1 - x0, 1), (x0, y0, 1, y1 - y0 + 1), (x1 - 1, y0, 1, y1 - y0 + 1)):
                b = _i("border", *rect, False, bl)
                b["group"] = group
                items.append(b)

    landmark = None
    if name and terrain not in ("river", "sea", "lake"):
        landmark = _site_landmark(name, terrain if not is_kingdom else "kingdom", role or terrain, cx, cy)
        items.append(landmark)
    if not items:
        raise ValueError("Nothing could be painted there.")
    _save_custom(conn, subnet, items)
    where = f" with {name} on it" if landmark else ""
    return {"kind": "terrain", "terrain": terrain, "added": len(items), "focus": [cx, cy],
            "landmark": name if landmark else None,
            "message": f"Painted {TITLE.get(terrain, terrain).lower()}{where}"}


BUILD_SIZES = [(r"tavern|inn|pub", (7, 5)), (r"temple|church|abbey", (7, 6)), (r"keep|castle|fortress|manor", (8, 7)),
               (r"warehouse|barn|stable|mill", (7, 5)), (r"shop|store|alchem|apothec|smith|forge|bakery|library|guild", (5, 4)),
               (r"tower|chapel|shrine", (4, 4)), (r"house|cottage|hut|cabin|lodge", (4, 4))]
BUILD_SIZES.insert(0, (r"barracks", (8, 5)))


def _solid_cells(conn, subnet) -> set[tuple[int, int]]:
    cells = set(_blocked(subnet)) | _occupied(conn, subnet)
    for it in _layout(subnet):
        if it.get("portal") or it["kind"] not in GROUND_KINDS:
            for dx in range(it["w"]):
                for dy in range(it["h"]):
                    cells.add((it["x"] + dx, it["y"] + dy))
    return cells


def _find_site(conn, subnet, x, y, w, h):
    cols, rows = subnet["grid_cols"], subnet["grid_rows"]
    solid = _solid_cells(conn, subnet)

    def fits(bx, by):
        if bx < 0 or by < 0 or bx + w > cols or by + h >= rows:    # one spare row below for the door step
            return False
        if (bx + w // 2, by + h) in solid:
            return False
        return all((bx + i, by + j) not in solid for i in range(w) for j in range(h))

    sx, sy = x - w // 2, y - h // 2
    if fits(sx, sy):
        return sx, sy
    for ring in range(1, max(cols, rows)):
        for dx in range(-ring, ring + 1):
            for dy in range(-ring, ring + 1):
                if max(abs(dx), abs(dy)) == ring and fits(sx + dx, sy + dy):
                    return sx + dx, sy + dy
    return None


def _build(conn, subnet, packet) -> dict:
    cols, rows, kind = subnet["grid_cols"], subnet["grid_rows"], subnet["kind"]
    role = (packet.get("role") or "House").strip()
    name = (packet.get("name") or "").strip() or f"Unnamed {role.lower()}"
    material = "stone" if re.search(r"stone|brick|marble", (packet.get("style") or "").lower()) else "wood"
    x = max(1, min(cols - 2, int(packet.get("x", cols // 2))))
    y = max(1, min(rows - 2, int(packet.get("y", rows // 2))))

    if kind == "world":   # at world scale a building is a settlement-sized landmark
        pos = _find_site(conn, subnet, x, y, 3, 3)
        if not pos:
            raise ValueError("No clear ground for that near the spot.")
        lm = _site_landmark(name, "", role, pos[0] + 1, pos[1] + 1,
                            desc=f"{name}: a {role.lower()} marked on the world map.")
        _save_custom(conn, subnet, [lm])
        return {"kind": "building", "added": 1, "focus": [pos[0] + 1, pos[1] + 1], "landmark": name,
                "message": f"Marked {name} on the map"}

    w, h = int(packet.get("w") or 0), int(packet.get("h") or 0)
    if not (w and h):
        w, h = next((sz for pat, sz in BUILD_SIZES if re.search(pat, role.lower())), (6, 5))
    w, h = max(4, min(14, w)), max(4, min(12, h))
    pos = _find_site(conn, subnet, x, y, w, h)
    if not pos:
        raise ValueError(f"No clear {w}x{h} ground near there. Try another spot or expand the map.")
    bx, by = pos
    pad = _pad(subnet)
    dx = bx + w // 2                                   # door sits mid-way along the south wall
    door_base = (dx - pad["w"], by + h - 1 - pad["n"])
    spec = _gen(name, "interior", _icon_for(role), name)
    spec["generate"].update(role=role, origin=f"{door_base[0]},{door_base[1]}", exit=[door_base[0], door_base[1] + 1])
    desc = f"{role}. Step through the door, or press Enter, to go inside."
    items = [
        _i("bfloor", bx, by, w, h, False, name, variant=material, enter=json.loads(json.dumps(spec)), desc=desc),
        _i("bwall", bx, by, w, 1, True, f"{name} walls", variant=material),
        _i("bwall", bx, by + 1, 1, h - 2, True, f"{name} walls", variant=material),
        _i("bwall", bx + w - 1, by + 1, 1, h - 2, True, f"{name} walls", variant=material),
        _i("bwall", bx, by + h - 1, dx - bx, 1, True, f"{name} walls", variant=material),
        _i("bwall", dx + 1, by + h - 1, bx + w - dx - 1, 1, True, f"{name} walls", variant=material),
        _i("door", dx, by + h - 1, 1, 1, False, f"{name} — door", variant=material, enter=spec, desc=desc),
    ]
    items[-1]["auto"] = True
    items = [it for it in items if it["w"] > 0 and it["h"] > 0]
    _save_custom(conn, subnet, items)
    return {"kind": "building", "added": len(items), "focus": [bx + w // 2, by + h // 2], "landmark": name,
            "message": f"Built {name} ({role.lower()}, {material})"}


def _resize(conn, subnet, width, height, direction) -> dict:
    cols, rows = subnet["grid_cols"], subnet["grid_rows"]
    W = int(width) if width else cols
    H = int(height) if height else rows
    if W > MAX_COLS or H > MAX_ROWS:
        raise ValueError(f"The largest map is {MAX_COLS}x{MAX_ROWS} tiles.")
    if W < cols or H < rows:
        raise ValueError("Maps can only grow. Pick a larger size.")
    ax, ay = W - cols, H - rows
    d = (direction or "all").lower()
    de = dw = dn = ds = 0
    if d == "all":
        dw, de, dn, ds = ax // 2, ax - ax // 2, ay // 2, ay - ay // 2
    elif d == "east": de = ax
    elif d == "west": dw = ax
    elif d == "north": dn = ay
    elif d == "south": ds = ay
    else:
        raise ValueError("direction must be north, south, east, west or all")
    if not (dw or de or dn or ds):
        raise ValueError("That size does not add any tiles in that direction.")

    pad = _pad(subnet)
    new = {"n": pad["n"] + dn, "s": pad["s"] + ds, "e": pad["e"] + de, "w": pad["w"] + dw}
    nc, nr = cols + dw + de, rows + dn + ds
    kind = subnet["kind"]
    ambient = {"world": ("ocean", True), "region": ("forest", False), "exterior": ("scrub", False), "dungeon": ("mountain", True)}.get(kind)
    fills = []
    if ambient:
        k, blk = ambient
        label = {"ocean": "The Cerulean Deep", "forest": "Pine forest (difficult terrain)", "scrub": "Rolling scrubland", "mountain": "Rock"}[k]
        for rect in ((0, 0, dw, nr), (nc - de, 0, de, nr), (dw, 0, nc - dw - de, dn), (dw, nr - ds, nc - dw - de, ds)):
            if rect[2] > 0 and rect[3] > 0:
                fills.append(_i(k, *rect, blk, label))
    # tokens ride along when the origin moves (north/west growth)
    conn.execute("UPDATE nodes SET x=x+?, y=y+? WHERE subnet_id=? AND x>=0", (dw, dn, subnet["id"]))
    custom = json.loads(subnet["custom_json"] or "[]")
    for it in fills:                                   # stored in the base frame
        c = dict(it); c["x"] -= new["w"]; c["y"] -= new["n"]; custom.append(c)
    conn.execute("UPDATE subnets SET pad_json=?, custom_json=?, grid_cols=?, grid_rows=? WHERE id=?",
                 (json.dumps(new), json.dumps(custom), nc, nr, subnet["id"]))
    return {"kind": "expand", "cols": nc, "rows": nr, "added": {"n": dn, "s": ds, "e": de, "w": dw},
            "message": f"Map is now {nc}x{nr}"}


def resize_scene(ref, width=None, height=None, direction="all") -> dict:
    with db() as conn:
        subnet = _resolve(conn, ref)
        return _resize(conn, subnet, width, height, direction)


def _expand_from_packet(conn, subnet, packet) -> dict:
    d = (packet.get("direction") or "all").lower()
    d = d if d in ("north", "south", "east", "west", "all") else "all"
    amount = max(1, min(64, int(packet.get("w") or 8)))
    cols, rows = subnet["grid_cols"], subnet["grid_rows"]
    if d == "all":
        W, H = cols + 2 * amount, rows + 2 * amount
    else:
        W, H = (cols + amount, rows) if d in ("east", "west") else (cols, rows + amount)
    res = _resize(conn, subnet, W, H, d)
    style = _canon_terrain(packet.get("style")) if packet.get("style") else None
    kingdom = "kingdom" in (packet.get("role") or "").lower()
    if d != "all" and (style or kingdom or packet.get("name")):
        sub = conn.execute("SELECT * FROM subnets WHERE id=?", (subnet["id"],)).fetchone()
        nc, nr = sub["grid_cols"], sub["grid_rows"]
        a = res["added"]
        t = style if style not in (None, "kingdom") else "plains"
        if d in ("east", "west"):
            px = nc - amount // 2 - 1 if d == "east" else amount // 2 + 1
            p = {"x": px, "y": nr // 2, "w": max(6, amount - 3), "h": max(8, int(nr * 0.6))}
        else:
            py = nr - amount // 2 - 1 if d == "south" else amount // 2 + 1
            p = {"x": nc // 2, "y": py, "w": max(8, int(nc * 0.5)), "h": max(5, amount - 3)}
        nm = (packet.get("name") or "").strip() or (f"The {t.title()} Kingdom" if kingdom else "")
        paint = _paint(conn, sub, {**p, "style": t, "role": "kingdom" if kingdom else "", "name": nm})
        res["message"] += f"; {paint['message'].lower()}"
        res["focus"] = paint["focus"]
        res["landmark"] = paint.get("landmark")
    return res



# --------------------------------------------------------------------------- #
# Roads and landmarks (settlements, forts, ruins, ...)
# --------------------------------------------------------------------------- #
def _paint_road(conn, subnet, packet) -> dict:
    """x,y = start; w,h = destination (tile coordinates). Wanders a little; never paves over structures or sea."""
    cols, rows, kind = subnet["grid_cols"], subnet["grid_rows"], subnet["kind"]
    cl = lambda v, hi: max(0, min(hi, int(v)))
    x0, y0 = cl(packet.get("x", 0), cols - 1), cl(packet.get("y", 0), rows - 1)
    x1, y1 = cl(packet.get("w", 0), cols - 1), cl(packet.get("h", 0), rows - 1)
    if (x0, y0) == (x1, y1):
        raise ValueError("A road needs two different points.")
    rng = random.Random(f"road{x0}{y0}{x1}{y1}")
    x, y, cells = x0, y0, [(x0, y0)]
    for _ in range(cols + rows):
        if (x, y) == (x1, y1):
            break
        rx, ry = abs(x1 - x), abs(y1 - y)
        go_x = rx and (not ry or (rx >= ry and rng.random() > 0.25) or (rx < ry and rng.random() < 0.25))
        if go_x:
            x += 1 if x1 > x else -1
        else:
            y += 1 if y1 > y else -1
        cells.append((x, y))
    hard = set()
    for it in _layout(subnet):
        if it["kind"] in ("ocean", "snowpeak", "mountain") or (it["kind"] not in GROUND_KINDS):
            for i in range(it["w"]):
                for j in range(it["h"]):
                    hard.add((it["x"] + i, it["y"] + j))
    paved = sorted({c for c in cells if c not in hard}, key=lambda c: (c[1], c[0]))
    tile = {"world": "highroad", "region": "gravel", "exterior": "cobble"}.get(kind, "gravel")
    items, label = [], "Road"
    for cx_, cy_ in paved:
        last = items[-1] if items else None
        if last and last["y"] == cy_ and last["x"] + last["w"] == cx_:
            last["w"] += 1
        else:
            it = _i(tile, cx_, cy_, 1, 1, False, label)
            it["carve"] = True
            items.append(it)
    if not items:
        raise ValueError("No open ground to lay a road on between those points.")
    _save_custom(conn, subnet, items)
    mid = cells[len(cells) // 2]
    return {"kind": "terrain", "terrain": "road", "added": len(items), "focus": list(mid), "landmark": None,
            "message": f"Laid a road ({len(paved)} tiles)"}


# role -> footprint (w, h), foundation tile, walls?, landmark size, map glyph variant, scene template
LANDMARK_TYPES = {
    "town":      dict(size=(5, 5), found="cobble",  walls=False, lm=2, variant="town",    tpl="settlement"),
    "village":   dict(size=(4, 4), found="track",   walls=False, lm=2, variant="town",    tpl="settlement"),
    "hamlet":    dict(size=(3, 3), found="track",   walls=False, lm=2, variant="town",    tpl="settlement"),
    "outpost":   dict(size=(3, 3), found="track",   walls=False, lm=2, variant="town",    tpl="settlement"),
    "camp":      dict(size=(3, 3), found="track",   walls=False, lm=2, variant="camp",    tpl="settlement"),
    "city":      dict(size=(7, 7), found="cobble",  walls=True,  lm=3, variant="town",    tpl="capital"),
    "castle":    dict(size=(6, 6), found="cobble",  walls=True,  lm=2, variant="tower",   tpl="capital"),
    "fort":      dict(size=(4, 4), found="track",   walls=True,  lm=2, variant="tower",   tpl="capital"),
    "keep":      dict(size=(4, 4), found="cobble",  walls=True,  lm=2, variant="tower",   tpl="capital"),
    "citadel":   dict(size=(6, 6), found="foothill", walls=True, lm=2, variant="citadel", tpl="citadel"),
    "ruins":     dict(size=(4, 4), found="foothill", walls=False, lm=2, variant="ruins",  tpl="citadel"),
    "dungeon":   dict(size=(3, 3), found="foothill", walls=False, lm=2, variant="citadel", tpl="citadel"),
    "temple":    dict(size=(4, 4), found="cobble",  walls=False, lm=2, variant="temple",  tpl="interior"),
    "monastery": dict(size=(5, 5), found="cobble",  walls=True,  lm=2, variant="temple",  tpl="interior"),
    "lighthouse": dict(size=(3, 3), found="dock",   walls=False, lm=2, variant="tower",   tpl="interior", coastal=True),
    "port":      dict(size=(5, 4), found="dock",    walls=False, lm=2, variant="port",    tpl="port",     coastal=True),
}


def _find_area(conn, subnet, x, y, w, h, coastal=False):
    """Top-left of a free w x h patch nearest (x, y); coastal=True prefers a patch touching open sea."""
    cols, rows = subnet["grid_cols"], subnet["grid_rows"]
    solid = _solid_cells(conn, subnet)
    sea = {(it["x"] + i, it["y"] + j) for it in _layout(subnet) if it["kind"] == "ocean"
           for i in range(it["w"]) for j in range(it["h"])} if coastal else set()

    def fits(bx, by):
        return 0 <= bx and 0 <= by and bx + w <= cols and by + h <= rows and \
            all((bx + i, by + j) not in solid for i in range(w) for j in range(h))

    def touches_sea(bx, by):
        edge = [(bx - 1, by + j) for j in range(h)] + [(bx + w, by + j) for j in range(h)] + \
               [(bx + i, by - 1) for i in range(w)] + [(bx + i, by + h) for i in range(w)]
        return any(c in sea for c in edge)

    sx, sy = x - w // 2, y - h // 2
    for need_sea in ((True, False) if sea else (False,)):
        for ring in range(0, max(cols, rows)):
            for dx in range(-ring, ring + 1):
                for dy in range(-ring, ring + 1):
                    if max(abs(dx), abs(dy)) == ring and fits(sx + dx, sy + dy) and \
                            (not need_sea or touches_sea(sx + dx, sy + dy)):
                        return sx + dx, sy + dy
    return None


def _landmark(conn, subnet, packet) -> dict:
    """PLACE_LANDMARK: foundation terrain (+ walls and gate when fortified) + an enterable landmark tile."""
    cols, rows = subnet["grid_cols"], subnet["grid_rows"]
    role = (packet.get("role") or "town").lower()
    role = role if role in LANDMARK_TYPES else "town"
    spec = LANDMARK_TYPES[role]
    name = (packet.get("name") or "").strip() or f"Unnamed {role}"
    w, h = spec["size"]
    pw, ph = int(packet.get("w") or 0), int(packet.get("h") or 0)
    if pw and ph:
        w, h = max(3, min(16, pw)), max(3, min(16, ph))
    x = max(1, min(cols - 2, int(packet.get("x", cols // 2))))
    y = max(1, min(rows - 2, int(packet.get("y", rows // 2))))
    pos = _find_area(conn, subnet, x, y, w, h, coastal=spec.get("coastal", False))
    if not pos:
        raise ValueError(f"No clear {w}x{h} ground near there for a {role}. Try elsewhere or expand the map.")
    bx, by = pos
    items = [_i(spec["found"], bx, by, w, h, False, f"{name} grounds")]
    if spec["walls"]:
        gw = 2 if w >= 5 else 1
        gx = bx + (w - gw) // 2
        wl = f"{name} walls"
        for rect in ((bx, by, w, 1), (bx, by + 1, 1, h - 2), (bx + w - 1, by + 1, 1, h - 2),
                     (bx, by + h - 1, gx - bx, 1), (gx + gw, by + h - 1, bx + w - gx - gw, 1)):
            if rect[2] > 0 and rect[3] > 0:
                items.append(_i("citywall", *rect, True, wl))
        items.append(_i("gate", gx, by + h - 1, gw, 1, False, f"{name} gate"))
    lm = spec["lm"]
    lx, ly = bx + (w - lm) // 2, by + (h - lm) // 2
    desc = f"{name}: a {role}. Press Enter to go inside, or Generate to map it first."
    items.append(_landmark_item(name, spec["tpl"], role.title() if spec["tpl"] == "interior" else role, lx, ly, lm,
                                spec["variant"], _icon_for(role), desc))
    _save_custom(conn, subnet, items)
    return {"kind": "landmark", "added": len(items), "focus": [lx, ly], "landmark": name,
            "message": f"Founded {role} {name}"}


def _tpl_settlement(name, parent, it):
    g = (it.get("enter") or {}).get("generate") or {}
    role = (g.get("role") or "town").lower()
    n = {"hamlet": 3, "outpost": 3, "camp": 2, "village": 5}.get(role, 7)
    spots = [(1, 2, 3, 3), (5, 2, 4, 3), (15, 2, 4, 3), (20, 2, 3, 3), (1, 11, 3, 3), (5, 11, 4, 3), (15, 11, 4, 3)]
    layout = [_i("cobble", 10, 2, 4, 12, False, "Main street"), _i("cobble", 2, 7, 20, 2, False, "Cross street"),
              _i("well", 11, 7, 2, 2, True, f"{name} well")]
    layout += [_i("tavern", *sp, True, f"House {k + 1}") for k, sp in enumerate(spots[:n])]
    if role in ("town", "city"):
        layout += [_i("stall", 14, 5, 3, 2, True, "Market stall"), _i("crate", 17, 5, 1, 1, True)]
    layout.append(_exit(parent, it, 23, 7, 1, 2, f"Road out of {name}"))
    rng = random.Random(name)
    return dict(kind="exterior", cols=24, rows=16, layout=layout, default=[12, 5],
                anchors=[[r"\b(well|centre|center|square)\b", [12, 9]], [r"\b(gate|road|exit)\b", [21, 8]],
                         [r"\b(house|home)\b", [7, 6]]],
                notes=f"A {role}: a main street and a cross street meeting at the well, houses on either side, the road out is east.",
                nodes=[_npc(f"{rng.choice(_FIRST)} the Reeve", "Reeve", 12, 5, 12,
                            f"Keep {name} quiet and the taxes paid.", "Quietly pays tribute to whoever controls the road.",
                            "Tired local reeve, ledger under arm, practical coat")])


TEMPLATES["settlement"] = _tpl_settlement


def _world(conn, subnet, packet) -> dict:
    action = packet.get("action")
    if action == "PAINT_TERRAIN":
        return _paint(conn, subnet, packet)
    if action == "PLACE_BUILDING":
        return _build(conn, subnet, packet)
    if action == "PLACE_LANDMARK":
        return _landmark(conn, subnet, packet)
    if action == "EXPAND_MAP":
        return _expand_from_packet(conn, subnet, packet)
    raise ValueError(f"unknown world action {action}")


def apply_world_action(packet: dict, ref=None) -> dict:
    """PAINT_TERRAIN / PLACE_BUILDING / PLACE_LANDMARK / EXPAND_MAP -> change the scene in view."""
    with db() as conn:
        return _world(conn, _resolve(conn, ref), packet)


# --------------------------------------------------------------------------- #
# Blueprints: every change is previewed (dry run, rolled back) before it is applied (one atomic commit)
# --------------------------------------------------------------------------- #
def _exec_action(conn, a: dict, ref=None) -> dict:
    """Run one blueprint action on an open transaction. Returns a report; raises ValueError/LookupError if it cannot."""
    act = a.get("action")
    subnet = _resolve(conn, ref)
    if act in ("PROVISION_HOST", "LOG_RUMOR"):
        node = _provision(conn, a, ref)
        if node["type"] == "rumor":
            return {"op": act, "label": f"Log rumor “{node['name']}” (DC {node['state']['dc']})", "focus": None,
                    "landmark": None, "node_id": None}
        return {"op": act, "label": f"Add {node['name']}, {node['state'].get('role') or node['type']}, at tile {node['x']},{node['y']}",
                "focus": [node["x"], node["y"]], "landmark": None, "node_id": node["id"],
                "portrait_prompt": a.get("prompt_portrait") or node["name"]}
    if act == "GENERATE_SCENE":
        want = (a.get("name") or "").strip().lower()
        item = next((it for it in _base_items(subnet)
                     if (it.get("enter") or {}).get("generate") and (it.get("label") or "").split(" — ")[0].lower() == want), None)
        if not item:
            raise ValueError(f"There is no landmark called “{a.get('name')}” to generate a map for.")
        row, created = _ensure_child(conn, subnet, item)
        return {"op": act, "label": f"{'Generate' if created else 'Reuse'} the map for {row['name']}", "focus": None,
                "landmark": None, "node_id": None}
    r = _world(conn, subnet, a)
    focus = r.get("focus")
    label = r["message"] + (f" at tile {focus[0]},{focus[1]}" if focus and act != "EXPAND_MAP" else "")
    return {"op": act, "label": label, "focus": focus, "landmark": r.get("landmark"), "node_id": None,
            "expand_added": r.get("added") if isinstance(r.get("added"), dict) else None}


def run_blueprint(actions: list[dict], ref=None, commit: bool = False) -> dict:
    """Run the actions in order inside ONE transaction. commit=False previews and rolls back (the board is untouched);
    commit=True keeps the changes only if every action succeeded, otherwise nothing changes at all."""
    conn = _connect()
    results, ok = [], True
    try:
        conn.execute("BEGIN IMMEDIATE")
        for a in actions:
            if not ok:
                results.append({"op": a.get("action"), "ok": False, "label": "Skipped because an earlier step failed",
                                "error": "Skipped because an earlier step failed."})
                continue
            try:
                results.append({**_exec_action(conn, a, ref), "ok": True})
            except (ValueError, LookupError) as exc:
                ok = False
                results.append({"op": a.get("action"), "ok": False, "label": str(exc), "error": str(exc)})
        if ok and commit and actions:
            conn.commit()
        else:
            conn.rollback()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"ok": ok and bool(actions), "results": results}


# --------------------------------------------------------------------------- #
# Imported battle maps (background image per scene)
# --------------------------------------------------------------------------- #
MAX_MAP_BYTES = 25 * 1024 * 1024
MAX_MAP_PX = 4096


def _set_meta(conn, subnet, **updates) -> dict:
    meta = _meta(subnet)
    for k, v in updates.items():
        if v is None:
            meta.pop(k, None)
        else:
            meta[k] = v
    conn.execute("UPDATE subnets SET meta_json=? WHERE id=?", (json.dumps(meta), subnet["id"]))
    return meta


def _sniff_image(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _grow_to(conn, subnet, cols: int, rows: int):
    """Make the grid at least cols x rows (east/south growth keeps everything else where it is)."""
    sub = subnet
    if cols > sub["grid_cols"]:
        _resize(conn, sub, cols, sub["grid_rows"], "east")
        sub = conn.execute("SELECT * FROM subnets WHERE id=?", (subnet["id"],)).fetchone()
    if rows > sub["grid_rows"]:
        _resize(conn, sub, sub["grid_cols"], rows, "south")
        sub = conn.execute("SELECT * FROM subnets WHERE id=?", (subnet["id"],)).fetchone()
    return sub


def _bg_payload(subnet) -> dict | None:
    bg = _meta(subnet).get("bg")
    if not bg:
        return None
    path = ROOT / "assets" / bg["file"]
    if not path.exists():
        return None
    pad = _pad(subnet)
    return {"url": f"/assets/{bg['file']}?v={int(path.stat().st_mtime)}", "cols": bg["cols"], "rows": bg["rows"],
            "opacity": bg["opacity"], "ox": pad["w"], "oy": pad["n"]}


def _clean_calibration(cols, rows, opacity):
    cols, rows = int(cols), int(rows)
    if not (4 <= cols <= MAX_COLS and 4 <= rows <= MAX_ROWS):
        raise ValueError(f"Columns must be 4-{MAX_COLS} and rows 4-{MAX_ROWS}.")
    return cols, rows, max(0.0, min(1.0, float(opacity)))


def save_scene_map(ref, data: bytes, cols: int, rows: int, opacity: float) -> dict:
    """Store an uploaded map as assets/maps/<subnet_id>.png and calibrate it to a cols x rows tile grid."""
    if len(data) > MAX_MAP_BYTES:
        raise ValueError("That image is over 25 MB.")
    kind = _sniff_image(data)
    if not kind:
        raise ValueError("Unsupported file. Use a .png, .jpg or .webp image.")
    cols, rows, opacity = _clean_calibration(cols, rows, opacity)
    with db() as conn:
        subnet = _resolve(conn, ref)
        MAPS_DIR.mkdir(parents=True, exist_ok=True)
        for old in MAPS_DIR.glob(f"{subnet['id']}.*"):
            old.unlink()
        try:
            from PIL import Image
            import io
            im = Image.open(io.BytesIO(data))
            im.load()
            im.thumbnail((MAX_MAP_PX, MAX_MAP_PX))
            im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
            fname = f"{subnet['id']}.png"
            im.save(MAPS_DIR / fname, "PNG", optimize=True)
        except ImportError:                       # no Pillow: keep the original bytes under their true extension
            fname = f"{subnet['id']}.{kind}"
            (MAPS_DIR / fname).write_bytes(data)
        except Exception as exc:
            raise ValueError(f"That image could not be read ({exc}).")
        subnet = _grow_to(conn, subnet, cols, rows)
        _set_meta(conn, subnet, bg={"file": f"maps/{fname}", "cols": cols, "rows": rows, "opacity": opacity})
        row = conn.execute("SELECT * FROM subnets WHERE id=?", (subnet["id"],)).fetchone()
        return {"bg": _bg_payload(row), "cols": row["grid_cols"], "rows": row["grid_rows"]}


def update_scene_map(ref, cols: int, rows: int, opacity: float) -> dict:
    cols, rows, opacity = _clean_calibration(cols, rows, opacity)
    with db() as conn:
        subnet = _resolve(conn, ref)
        bg = _meta(subnet).get("bg")
        if not bg:
            raise LookupError("This scene has no imported map.")
        subnet = _grow_to(conn, subnet, cols, rows)
        _set_meta(conn, subnet, bg={**bg, "cols": cols, "rows": rows, "opacity": opacity})
        row = conn.execute("SELECT * FROM subnets WHERE id=?", (subnet["id"],)).fetchone()
        return {"bg": _bg_payload(row), "cols": row["grid_cols"], "rows": row["grid_rows"]}


def clear_scene_map(ref) -> dict:
    with db() as conn:
        subnet = _resolve(conn, ref)
        for old in MAPS_DIR.glob(f"{subnet['id']}.*"):
            old.unlink()
        _set_meta(conn, subnet, bg=None)
        return {"bg": None}



# --------------------------------------------------------------------------- #
# Player broadcast: what players may see and move
# --------------------------------------------------------------------------- #
PLAYER_MOVABLE = ("party", "pc")        # the only token types a player can drag


def active_scene_id() -> int | None:
    with db() as conn:
        row = conn.execute("SELECT id FROM subnets WHERE active=1 LIMIT 1").fetchone()
        return row["id"] if row else None


def set_active_scene(ref) -> int:
    """Make a scene the one players are watching (what get_scene does when the GM opens it)."""
    with db() as conn:
        return _resolve(conn, ref, activate=True)["id"]


def _play_item(it: dict) -> dict:
    """Map art only: no links, portal targets, descriptions or other GM notes."""
    out = {k: it[k] for k in ("kind", "x", "y", "w", "h", "block", "carve", "variant") if k in it}
    if it.get("label") and it["kind"] != "bwall":
        out["label"] = _short_label(it)              # drops hints like "— leads to Timberflats Square"
    if it.get("portal") or it.get("auto"):
        out["portal"] = True                          # keeps the door glow, without saying where it goes
    return out


def _play_node(n: dict) -> dict:
    """A token as players see it: name, role, art and position. No IP, secrets, DC, motivation or disposition."""
    return {"id": n["id"], "name": n["name"], "type": n["type"], "x": n["x"], "y": n["y"],
            "role": n["state"].get("role", ""), "portrait": n["portrait"], "movable": n["type"] in PLAYER_MOVABLE}


def get_play_scene() -> dict:
    """The active scene, sanitised for the /play view. Read-only: never changes which scene is active."""
    with db() as conn:
        subnet = _resolve(conn)
        rows = conn.execute(NODE_SELECT + " WHERE n.subnet_id=? ORDER BY n.id", (subnet["id"],)).fetchall()
        nodes = [_play_node(_node(r)) for r in rows
                 if r["x"] >= 0 and r["type"] != "rumor" and (r["visibility_mask"] & 1)]
        meta = _scene_meta(subnet)
        return {
            "subnet": {"id": subnet["id"], "name": subnet["name"], "short": meta["short"], "icon": meta["icon"],
                       "kind": subnet["kind"], "cols": subnet["grid_cols"], "rows": subnet["grid_rows"],
                       "layout": [_play_item(it) for it in _layout(subnet)], "bg": _bg_payload(subnet)},
            "nodes": nodes,
        }


def move_and_follow(node_id: int, x: int, y: int) -> tuple[dict, dict | None]:
    """move_node, and if the party (or a player character) went through a portal the players follow it:
    the destination becomes the active scene."""
    node, transition = move_node(node_id, x, y)
    if transition and node["type"] in PLAYER_MOVABLE:
        set_active_scene(transition["cidr"])
    return node, transition


def play_move(node_id: int, x: int, y: int, role: str = "player") -> tuple[dict, dict | None]:
    """A move requested over the socket. Players may only move party / player-character tokens that they can see,
    in the scene being played. The GM role may move anything."""
    if role != "gm":
        with db() as conn:
            row = conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
            if not row:
                raise LookupError("That token does not exist.")
            if row["type"] not in PLAYER_MOVABLE or not (row["visibility_mask"] & 1):
                raise PermissionError("Players can only move the party and player characters.")
            if row["subnet_id"] != _resolve(conn)["id"]:
                raise ValueError("That token is not in the scene being played.")
    return move_and_follow(node_id, x, y)


# --------------------------------------------------------------------------- #
# Repositioning landmark pins and the scene arrival tile
# --------------------------------------------------------------------------- #
MOVABLE = ("landmark", "capital")


def _short_label(it) -> str:
    return (it.get("label") or it["kind"]).split(" — ")[0]


def _apply_move(conn, subnet, index: int, x: int, y: int) -> dict:
    """Move the pin at effective-layout `index` so its top-left is tile (x, y). Stored as an override in meta."""
    raw = _raw_items(subnet)
    if not (0 <= index < len(raw)):
        raise LookupError("That item no longer exists.")
    key, it = raw[index]
    if it["kind"] not in MOVABLE:
        raise ValueError("Only landmark pins can be repositioned.")
    cols, rows = subnet["grid_cols"], subnet["grid_rows"]
    if not (0 <= x <= cols - it["w"] and 0 <= y <= rows - it["h"]):
        raise ValueError(f"The pin is {it['w']}x{it['h']} tiles: keep it inside the {cols}x{rows} map.")
    pad = _pad(subnet)
    to = [x - pad["w"], y - pad["n"]]
    moves = dict(_moves(subnet))
    mv = moves.get(key)
    origin = mv["from"] if mv else [it["x"], it["y"]]
    past = list(mv.get("past", [origin])) if mv else [origin]
    if mv and mv["to"] not in past:
        past.append(mv["to"])
    moves[key] = {"from": origin, "to": to, "past": past}
    _set_meta(conn, subnet, moves=moves)
    label = _short_label(it)
    return {"kind": "move", "index": index, "x": x, "y": y, "landmark": label, "focus": [x, y],
            "message": f"Moved {label} to tile {x},{y}"}


def move_item(ref, index: int, x: int, y: int) -> dict:
    with db() as conn:
        return _apply_move(conn, _resolve(conn, ref), index, x, y)


def move_landmark_by_name(ref, name: str, x: int, y: int) -> dict:
    """/move <landmark name> <x> <y>"""
    with db() as conn:
        subnet = _resolve(conn, ref)
        want = (name or "").strip().lower()
        pins = [(i, it) for i, it in enumerate(_layout(subnet)) if it["kind"] in MOVABLE]
        exact = [p for p in pins if _short_label(p[1]).lower() == want]
        near = exact or [p for p in pins if want and want in _short_label(p[1]).lower()]
        if not near:
            raise LookupError(f"No landmark called “{name}” on this map.")
        if len(near) > 1:
            raise ValueError("More than one landmark matches: " + ", ".join(_short_label(p[1]) for p in near[:4]) + ". Be more specific.")
        return _apply_move(conn, subnet, near[0][0], x, y)


def _arrival_payload(subnet) -> dict | None:
    arr = _meta(subnet).get("arrival")
    if not arr:
        return None
    pad = _pad(subnet)
    return {"x": arr[0] + pad["w"], "y": arr[1] + pad["n"]}


def set_arrival(ref, x, y) -> dict:
    """Where tokens land when they enter this scene through a door, gate or other portal. x=None clears it."""
    with db() as conn:
        subnet = _resolve(conn, ref)
        if x is None or y is None:
            _set_meta(conn, subnet, arrival=None)
            return {"arrival": None}
        if not (0 <= x < subnet["grid_cols"] and 0 <= y < subnet["grid_rows"]):
            raise ValueError("That tile is outside the map.")
        if (x, y) in _blocked(subnet):
            raise ValueError("That tile is blocked, so tokens could not stand there.")
        pad = _pad(subnet)
        _set_meta(conn, subnet, arrival=[x - pad["w"], y - pad["n"]])
        return {"arrival": {"x": x, "y": y}}


# --------------------------------------------------------------------------- #
# New blank scenes ("Start Fresh")
# --------------------------------------------------------------------------- #
SCOPE_KIND = {"world": "world", "region": "region", "town": "exterior", "interior": "room", "dungeon": "dungeon"}
SCOPE_ICON = {"world": "🌍", "region": "🌲", "town": "🏘", "interior": "🏠", "dungeon": "🕳"}
SCOPE_FILL = {"world": "ocean", "region": "forest", "town": "dirt", "interior": "stone", "dungeon": "stone"}
FILLS = {"ocean": ("ocean", True, "Ocean"), "forest": ("forest", False, "Forest"), "dirt": ("dirt", False, "Packed earth"),
         "stone": ("bfloor", False, "Stone floor")}


def create_scene(name: str, scope: str = "region", cols: int = 32, rows: int = 24, fill: str | None = None,
                 parent_ref=None, root: bool = False, pin: bool = True) -> dict:
    """Provision a blank scene: new subnet, optional terrain fill, a Party token at (1,1). A child scene can also
    drop an enterable pin for itself on the parent map."""
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("Give the new map a name.")
    if scope not in SCOPE_KIND:
        raise ValueError("Scope must be world, region, town, interior or dungeon.")
    cols, rows = int(cols or 32), int(rows or 24)
    if not (8 <= cols <= MAX_COLS and 8 <= rows <= MAX_ROWS):
        raise ValueError(f"Columns must be 8-{MAX_COLS} and rows 8-{MAX_ROWS}.")
    fill = SCOPE_FILL[scope] if fill is None else fill
    if fill not in FILLS and fill != "none":
        raise ValueError("Fill must be ocean, forest, dirt, stone or none.")
    with db() as conn:
        parent = None if root else _resolve(conn, parent_ref)
        used = {r["cidr"] for r in conn.execute("SELECT cidr FROM subnets")}
        if root:
            cidr = "10.0.0.0/8" if "10.0.0.0/8" not in used else \
                next(f"10.{n}.0.0/16" for n in range(100, 250) if f"10.{n}.0.0/16" not in used)
        else:
            cidr = _next_free_cidr(conn)
        layout = []
        if fill != "none":
            kind, blocks, label = FILLS[fill]
            layout.append(_i(kind, 0, 0, cols, rows, blocks, label, variant="stone" if kind == "bfloor" else None))
            if fill == "ocean":                      # a small beach so the Party can stand somewhere
                beach = _i("land", 0, 0, 4, 4, False, "Landing")
                beach["carve"] = True
                layout.append(beach)
        meta = {"icon": SCOPE_ICON[scope], "short": name, "anchors": [], "default": [cols // 2, rows // 2],
                "notes": f"A blank {scope} map called {name}, {cols} wide by {rows} tall.", "created": True, "scope": scope}
        conn.execute(
            "INSERT INTO subnets (cidr, name, kind, grid_cols, grid_rows, layout_json, active, parent_id, meta_json) "
            "VALUES (?,?,?,?,?,?,0,?,?)",
            (cidr, name, SCOPE_KIND[scope], cols, rows, json.dumps(layout), parent["id"] if parent else None, json.dumps(meta)),
        )
        row = conn.execute("SELECT * FROM subnets WHERE cidr=?", (cidr,)).fetchone()
        _insert_node(conn, row, "The Party", "party", 1, 1, {
            "role": "Travel Party", "dc": 10, "disposition": "allied", "motivation": f"Explore {name}.",
            "secret": "Marching order, supplies and the watch rota are tracked here.",
            "prompt_portrait": "Group of adventurers with packs and lanterns, painterly fantasy"})
        pinned = False
        if parent and pin:
            cx, cy = (SCENE_BY_CIDR.get(parent["cidr"]) or _meta(parent)).get("default") or (parent["grid_cols"] // 2, parent["grid_rows"] // 2)
            pos = _find_area(conn, parent, int(cx), int(cy), 2, 2)
            if pos:
                lm = _i("landmark", pos[0], pos[1], 2, 2, False, name, variant="site", enter={"to": cidr},
                        desc=f"{name}: a map you created. Press Enter to go inside.")
                lm["carve"] = True
                _save_custom(conn, parent, [lm])
                pinned = True
        return {**_summary(row, True), "pinned": pinned, "root": parent is None}

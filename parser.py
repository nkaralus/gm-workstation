"""GM directive parser.

Primary:  Qwen2.5-3B-Instruct (GGUF, Q4_K_M) through llama-cpp-python, with a GBNF
          grammar so the output is always one well-formed action packet.
Fallback: deterministic rule-based parser (used when the GGUF or llama-cpp-python is absent).

Packet (every key is always present):
  { "action": PROVISION_HOST | LOG_RUMOR | PAINT_TERRAIN | PLACE_BUILDING | EXPAND_MAP,
    "name", "role", "x", "y", "w", "h", "style", "direction", "motivation", "secret", "prompt_portrait" }
  PAINT_TERRAIN  x,y centre, w,h size (0 = default), style = terrain (island, mountain, pass, river, forest, desert, ...),
                 role "kingdom" adds a border, a named result gets an enterable landmark
  PLACE_BUILDING x,y wanted spot, role = building type, style = stone|wood, w,h footprint (0 = by type)
  PLACE_LANDMARK role = town|city|village|hamlet|outpost|fort|castle|citadel|keep|ruins|dungeon|camp|temple|
                 monastery|lighthouse|port; paints a foundation and drops an enterable landmark tile
  (roads: PAINT_TERRAIN style "road", x,y = start, w,h = destination)
Slash commands (/town, /npc, /road, ...) bypass all of this: they are parsed deterministically and run instantly.
Plain English goes through propose_blueprint(): it returns a BlueprintProposal
  { "summary": "This will ...", "actions": [packet, ...] }   (packets as above, plus GENERATE_SCENE: name = a landmark)
that the GM previews and confirms before anything touches the board.
  EXPAND_MAP     direction north|south|east|west|all, w = tiles to add, style/role/name = optional new land to paint
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import random
import re
import shlex
import subprocess
import sys
import threading
from pathlib import Path

ROOT = Path(__file__).parent
MODEL_PATH = Path(os.environ.get("QWEN_GGUF_PATH", ROOT / "models" / "qwen2.5-3b-instruct-q4_k_m.gguf"))

# GBNF notes (llama.cpp): a newline ENDS a rule unless it is inside ( ... ), so every rule here is built on ONE line,
# and only the universally supported repetition operators * + ? are used (no {n,m}, which older llama.cpp rejects).
_FIELDS = [("action", "action"), ("name", "string"), ("role", "string"), ("x", "int"), ("y", "int"), ("w", "int"),
           ("h", "int"), ("style", "string"), ("direction", "direction"), ("motivation", "string"),
           ("secret", "string"), ("prompt_portrait", "string")]


def _alt(names) -> str:
    # alternatives of JSON string literals:  "\"A\"" | "\"B\""
    return " | ".join('"\\"%s\\""' % n for n in names)


def _key(k: str) -> str:
    return '"\\"%s\\""' % k


# one packet, a single line:  "{" ws "\"action\"" ws ":" ws action "," ws "\"name\"" ...
_PACKET = '"{" ws ' + ' "," ws '.join(f'{_key(k)} ws ":" ws {t}' for k, t in _FIELDS) + ' ws "}"'
_RULES = "\n".join([
    "direction ::= " + _alt(["", "north", "south", "east", "west", "all"]),
    # strings use *: nested optionals / {n,m} to cap their length were tried and are either rejected by older llama.cpp
    # or make grammar sampling dramatically slower. Runaway output is bounded by max_tokens and falls back to the rules.
    r'''ch ::= [^"\\\x7F\x00-\x1F] | "\\" ["\\/bfnrt]''',
    r'''string ::= "\"" ch* "\""''',
    'int ::= "-"? [0-9] [0-9]? [0-9]?',
    r'''ws ::= [ \t\n]?''',
]) + "\n"
_PACKET_ACTIONS = ["PROVISION_HOST", "LOG_RUMOR", "PAINT_TERRAIN", "PLACE_BUILDING", "PLACE_LANDMARK", "EXPAND_MAP"]

# one packet (instant path)
GBNF = "root ::= " + _PACKET + "\naction ::= " + _alt(_PACKET_ACTIONS) + "\n" + _RULES

# a BlueprintProposal: {"summary": "...", "actions": [packet, ...]}; the list is capped at 4 in code after parsing
GBNF_BLUEPRINT = (
    "root ::= " + '"{" ws ' + _key("summary") + ' ws ":" ws string "," ws ' + _key("actions") +
    ' ws ":" ws "[" ws ( packet ( ws "," ws packet )* )? ws "]" ws "}"\n'
    "packet ::= " + _PACKET + "\naction ::= " + _alt(_PACKET_ACTIONS + ["GENERATE_SCENE"]) + "\n" + _RULES
)


def gbnf_problems(text: str) -> list:
    """Static check for the mistakes that crash llama.cpp, no llama.cpp needed: continuation lines (a rule must be
    ONE line), {n,m} repetition, unbalanced parentheses."""
    problems = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if not re.match(r"^[A-Za-z][\w-]*\s*::=\s*\S", line):
            problems.append(f"line {n} is not a complete rule (a newline ends a rule outside parentheses): {line.strip()[:50]!r}")
            continue
        bare = re.sub(r'"(?:\\.|[^"\\])*"', '""', line)                 # drop literals
        bare = re.sub(r"\[(?:\\.|[^\]\\])*\]", "[]", bare)               # drop character classes
        if re.search(r"\{\s*\d*\s*,?\s*\d*\s*\}", bare):
            problems.append(f"line {n} uses {{n,m}} repetition; use * + ? instead")
        if bare.count("(") != bare.count(")"):
            problems.append(f"line {n} has unbalanced parentheses")
    return problems


SYSTEM = """You convert a Game Master's instruction into ONE JSON action packet. Every key must be present.

Actions
- PROVISION_HOST: add a character, creature, army or fleet token. Wild animals use role "Hostile Beasts", dragons "Ancient Dragon", armies "Faction Army".
- LOG_RUMOR: a rumor, clue, decree, troop movement or world event (x=-1, y=-1, role "rumor").
- PAINT_TERRAIN: add geography (island, mountain, mountain pass, river, lake, sea, forest, desert, swamp, plains, kingdom, road). For a road, style "road", x,y = start, w,h = destination x,y. style = the terrain word. A kingdom has role "kingdom". name only if the GM names it. x,y = centre; w,h = size in tiles or 0 for default. For a river, direction = the way it flows.
- PLACE_LANDMARK: add a settlement or point of interest: town, city, village, hamlet, outpost, fort, castle, citadel, keep, ruins, dungeon, camp, temple, monastery, lighthouse, port. role = that word in lower case. These are places, NEVER PROVISION_HOST.
- PLACE_BUILDING: add a building. role = building type (Tavern, Shop, Alchemist Shop, Smithy, Temple, House, Tower, Keep). style = "stone" or "wood". x,y = wanted spot next to the place the GM mentions. w,h = 0 unless sized.
- GENERATE_SCENE: create the map or interior behind a landmark or building that exists or is placed earlier in the same blueprint. name = that landmark's name; every other field empty.
- EXPAND_MAP: grow the scene. direction = north, south, east, west or all; w = tiles to add. style/role/name describe optional new land (for example style "desert", role "kingdom").

Fields
- x, y are grid cells inside the scene; w, h are tile counts; unused numbers are 0 (use -1,-1 for rumors); unused strings are "".
- direction is "" unless the action needs one.
- secret MUST start with "DC <number>: " for tokens and rumors (10 easy, 15 hard, 20 very hard); "" for world actions.
- prompt_portrait: one-line face and clothing description for tokens; "" otherwise.

Examples
GM: add a nervous bard near the hearth called Wren
{"action":"PROVISION_HOST","name":"Wren","role":"Nervous Bard","x":3,"y":10,"w":0,"h":0,"style":"","direction":"","motivation":"Earn enough coin to leave town tonight.","secret":"DC 14: Wren stole the lute she is playing from a noble's carriage.","prompt_portrait":"Young nervous bard, freckles, patched green doublet, lute strap across chest"}
GM: add an island named Isle of Crows in the west sea
{"action":"PAINT_TERRAIN","name":"Isle of Crows","role":"","x":5,"y":16,"w":0,"h":0,"style":"island","direction":"","motivation":"","secret":"","prompt_portrait":""}
GM: add a town named Valesom in the east
{"action":"PLACE_LANDMARK","name":"Valesom","role":"town","x":40,"y":16,"w":0,"h":0,"style":"","direction":"","motivation":"","secret":"","prompt_portrait":""}
GM: add a stone tavern named The Black Pearl near the square
{"action":"PLACE_BUILDING","name":"The Black Pearl","role":"Tavern","x":14,"y":10,"w":0,"h":0,"style":"stone","direction":"","motivation":"","secret":"","prompt_portrait":""}
GM: expand the map east by 16 tiles for a new desert kingdom
{"action":"EXPAND_MAP","name":"","role":"kingdom","x":0,"y":0,"w":16,"h":0,"style":"desert","direction":"east","motivation":"","secret":"","prompt_portrait":""}"""


_DOC = SYSTEM[SYSTEM.index("Actions"):SYSTEM.index("Examples")]
BP_SYSTEM = (
    "You turn a Game Master's request into a BLUEPRINT: a one-sentence summary and a short list of concrete actions. "
    "The GM will read it and press Build or Cancel, so be exact and add ONLY what was asked. "
    "If the request is unclear, or is not about building or adding something, return an empty actions list.\n\n"
    + _DOC +
    "Blueprint rules\n"
    "- summary: one sentence starting with \"This will\".\n"
    "- actions: 0 to 4 packets, in the order they should happen. Every key present in every packet.\n"
    "- Moving, renaming, or deleting things is not possible: return no actions for those.\n\n"
    "Examples\n"
    "GM: add a town named Valesom in the east and generate its map\n"
    '{"summary":"This will found the town Valesom in the east and generate its map.","actions":['
    '{"action":"PLACE_LANDMARK","name":"Valesom","role":"town","x":40,"y":16,"w":0,"h":0,"style":"","direction":"","motivation":"","secret":"","prompt_portrait":""},'
    '{"action":"GENERATE_SCENE","name":"Valesom","role":"","x":0,"y":0,"w":0,"h":0,"style":"","direction":"","motivation":"","secret":"","prompt_portrait":""}]}\n'
    "GM: add an island named Isle of Crows in the west sea with a hermit called Ysolde\n"
    '{"summary":"This will paint the island Isle of Crows in the west sea and add the hermit Ysolde.","actions":['
    '{"action":"PAINT_TERRAIN","name":"Isle of Crows","role":"","x":5,"y":16,"w":0,"h":0,"style":"island","direction":"","motivation":"","secret":"","prompt_portrait":""},'
    '{"action":"PROVISION_HOST","name":"Ysolde","role":"Hermit","x":7,"y":16,"w":0,"h":0,"style":"","direction":"","motivation":"Keep the island quiet.","secret":"DC 13: Ysolde fled the capital after a forbidden ritual.","prompt_portrait":"Weathered hermit, grey cloak, driftwood staff"}]}\n'
    "GM: hello there\n"
    '{"summary":"This will not change anything: that was not a build request.","actions":[]}'
)


# --------------------------------------------------------------------------- #
# local model
# --------------------------------------------------------------------------- #
_llm = None
_llm_state = "unloaded"        # unloaded | ready | unavailable
_llm_error = ""                # why the local model is not in use (shown in the UI tooltip)
_llm_failures = 0
_lock = threading.Lock()
log = logging.getLogger("gm.parser")

# Parsing a grammar happens in C. A bad grammar can SEGFAULT the interpreter, which no try/except can catch, so the
# grammars are first parsed in a throw-away child process: if that child dies or errors, the model is simply not used.
_PROBE = ("import sys, json\nfrom llama_cpp import LlamaGrammar\n"
          "for g in json.load(sys.stdin):\n    LlamaGrammar.from_string(g, verbose=False)\nprint('GRAMMAR_OK')\n")


def _grammars_ok():
    problems = [p for g in (GBNF, GBNF_BLUEPRINT) for p in gbnf_problems(g)]
    if problems:
        return False, "grammar lint failed: " + "; ".join(problems[:2])
    if os.environ.get("GM_SKIP_GRAMMAR_PROBE") == "1":
        return True, ""
    try:
        r = subprocess.run([sys.executable, "-c", _PROBE], input=json.dumps([GBNF, GBNF_BLUEPRINT]),
                           capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return False, f"grammar probe could not run: {exc}"
    if r.returncode != 0 or "GRAMMAR_OK" not in r.stdout:
        how = f"killed by signal {-r.returncode}" if r.returncode < 0 else f"exit code {r.returncode}"
        return False, f"llama.cpp rejected the grammar ({how}): {(r.stderr or r.stdout).strip()[-160:]}"
    return True, ""


def _disable(reason: str) -> None:
    global _llm, _llm_state, _llm_error
    _llm, _llm_state, _llm_error = None, "unavailable", reason
    log.warning("local model disabled, using the rule-based parser: %s", reason)


def _load() -> None:
    global _llm, _llm_state, _llm_error
    if _llm_state != "unloaded":
        return
    if not MODEL_PATH.exists():
        _llm_state, _llm_error = "unavailable", f"model file not found at {MODEL_PATH}"
        return
    ok, why = _grammars_ok()
    if not ok:
        return _disable(why)
    try:
        from llama_cpp import Llama, LlamaGrammar

        _llm = {
            "model": Llama(
                model_path=str(MODEL_PATH),
                n_ctx=4096,
                n_threads=os.cpu_count() or 4,
                n_gpu_layers=int(os.environ.get("LLAMA_GPU_LAYERS", "-1")),
                verbose=False,
            ),
            "grammar": LlamaGrammar.from_string(GBNF, verbose=False),
            "bp_grammar": LlamaGrammar.from_string(GBNF_BLUEPRINT, verbose=False),
        }
        _llm_state, _llm_error = "ready", ""
    except Exception as exc:  # missing wheel, bad file, OOM, grammar error...
        _disable(f"{type(exc).__name__}: {exc}")


def _model_ok() -> None:
    global _llm_failures
    _llm_failures = 0


def _model_failed(exc: Exception) -> None:
    """Log and fall back for this request; after 3 failures in a row stop trying the model at all."""
    global _llm_failures
    _llm_failures += 1
    log.warning("model call failed (%d/3), using rules for this request: %s: %s", _llm_failures, type(exc).__name__, exc)
    if _llm_failures >= 3:
        _disable(f"3 consecutive model failures; last: {exc}")


def warmup() -> None:
    with _lock:
        _load()


def model_status() -> dict:
    """What the UI shows about the local model: engine in use, file location, whether it is present/loaded."""
    return {"engine": engine_name(), "state": _llm_state, "path": str(MODEL_PATH), "present": MODEL_PATH.exists(),
            "error": _llm_error}


def reload_model() -> dict:
    """Retry loading after the .gguf has been dropped into models/ (no server restart needed)."""
    global _llm, _llm_state, _llm_error, _llm_failures
    with _lock:
        _llm, _llm_state, _llm_error, _llm_failures = None, "unloaded", "", 0
        _load()
    return model_status()


def engine_name() -> str:
    return "qwen2.5-3b" if _llm_state == "ready" else "rules"


def _context_block(ctx: dict) -> str:
    lines = [
        f"Scene: {ctx['room']} ({ctx.get('kind', 'room')}). Grid {ctx['cols']} wide x {ctx['rows']} tall (x 0-{ctx['cols'] - 1}, y 0-{ctx['rows'] - 1}).",
        "Landmarks: " + "; ".join(ctx["landmarks"]) + ".",
        ctx.get("notes") or "Floor is open between landmarks.",
    ]
    return "\n".join(lines)


def _run_llm(text: str, ctx: dict) -> dict:
    with _lock:
        out = _llm["model"].create_chat_completion(
            messages=[
                {"role": "system", "content": SYSTEM + "\n\n" + _context_block(ctx)},
                {"role": "user", "content": f"GM: {text}"},
            ],
            grammar=_llm["grammar"],
            temperature=0.4,
            max_tokens=480,
        )
    return json.loads(out["choices"][0]["message"]["content"])


# --------------------------------------------------------------------------- #
# rule-based world builder: PAINT_TERRAIN / PLACE_BUILDING / EXPAND_MAP
# --------------------------------------------------------------------------- #
VERB = r"(?:please\s+)?(?:add|build|place|put|construct|erect|raise|stamp|create|carve|paint|draw|form|make|dig|open|set\s+up)"
PERSON = (r"(?!\s+(?:keeper|owner|patron|maid|wench|regular|guard|guards|bard|master|girl|boy|man|woman|cat|dog|crier|elder|"
          r"mayor|folk|watch|watchman|captain|priest|soldier|resident|citizen|lord|lady|baker|clerk|drunk|merchant|beggar))")
PREFIX = r"(?:(?!named\b|called\b|dubbed\b)[\w'-]+\s+)"      # up to two descriptive words before the noun

LANDMARK_NOUNS = [
    (r"town(?:ship)?", "town"), (r"city|metropolis", "city"), (r"village", "village"), (r"hamlet", "hamlet"),
    (r"outpost", "outpost"), (r"fortress|fort|stronghold", "fort"), (r"castle", "castle"), (r"citadel", "citadel"),
    (r"keep", "keep"), (r"ruins?", "ruins"), (r"dungeon|crypt|catacombs?", "dungeon"), (r"camp|encampment", "camp"),
    (r"monastery|abbey", "monastery"), (r"temple|church|chapel|shrine", "temple"), (r"lighthouse", "lighthouse"),
    (r"port|harbou?r", "port"),
]
LANDMARK_ROLES = tuple(r for _, r in LANDMARK_NOUNS)
PLACE_NAMES = {
    "town": ["Valdry", "Marrowgate", "Highfen", "Oakhollow", "Thistlewick", "Ravensford"],
    "city": ["Aurelmere", "Stormhaven", "Caldris", "Vethryn"], "village": ["Brindlemoor", "Fenwick", "Little Ashby"],
    "hamlet": ["Pennywell", "Coldharbor", "Dunmere"], "outpost": ["Watch Post Seven", "Gray Picket", "Ashfall Post"],
    "fort": ["Fort Merrow", "Fort Dunhallow"], "castle": ["Castle Blackthorn", "Castle Ravensmoor"],
    "citadel": ["The Ashen Citadel"], "keep": ["Gorran's Keep", "Thornkeep"], "ruins": ["The Weeping Ruins", "Old Harrowgate"],
    "dungeon": ["The Hollow Deep", "The Drowned Vault"], "camp": ["Ember Camp", "Red Fox Camp"],
    "temple": ["Temple of the Dawn", "The Quiet Shrine"], "monastery": ["Saint Orrin's Abbey", "Greyvale Monastery"],
    "lighthouse": ["The Valen Light", "Gull Rock Light"], "port": ["Saltmere", "Gullhaven"],
}


def _default_place_name(role: str, seed: str) -> str:
    return random.Random(seed).choice(PLACE_NAMES.get(role) or [f"Unnamed {role}"])


def _landmark_role(text: str) -> str:
    low = (text or "").lower()
    for pat, role in LANDMARK_NOUNS:
        if re.search(rf"\b(?:{pat})\b", low):
            return role
    return "town"


BUILD_NOUNS = [
    (r"alchemist(?:'s)?(?:\s+shop)?|apothecary", "Alchemist Shop"), (r"smithy|blacksmith|forge", "Smithy"),
    (r"tavern|inn|pub|alehouse", "Tavern"), (r"barracks", "Barracks"),
    (r"bakery", "Bakery"), (r"library", "Library"), (r"guild\s*hall", "Guildhall"),
    (r"shop|store", "Shop"), (r"warehouse|barn", "Warehouse"), (r"stable", "Stable"), (r"mill", "Mill"),
    (r"watchtower|tower", "Tower"), (r"manor", "Manor"),
    (r"house|cottage|hut|cabin|lodge|home", "House"),
]
STONE_ROLES = {"Temple", "Tower", "Keep", "Guildhall", "Library"}

TERRAINS = [
    ("road", r"\b(?:road|trail|highway)\b"),
    ("pass", r"\b(?:pass|gap|col)\b"), ("mountain", r"\b(?:mountains?|peaks?|range|ridge|hills?)\b"),
    ("river", r"\b(?:river|stream|creek)\b"), ("lake", r"\b(?:lakes?|pond)\b"),
    ("island", r"\b(?:islands?|isles?|archipelago|atoll)\b"),
    ("kingdom", r"\b(?:kingdom|empire|nation|continent|landmass|peninsula)\b"),
    ("forest", r"\b(?:forests?|woods?|woodlands?|jungle)\b"), ("desert", r"\b(?:deserts?|dunes?|wastes?)\b"),
    ("swamp", r"\b(?:swamps?|marsh(?:es)?|bog|fen)\b"), ("plains", r"\b(?:plains?|grasslands?|meadows?|steppe|prairie|farmland)\b"),
    ("sea", r"\b(?:seas?|oceans?|bay|gulf|strait)\b"),
]
TERRAIN_SIZE = {"island": (6, 5), "plains": (10, 7), "kingdom": (14, 10), "desert": (10, 8), "swamp": (8, 6),
                "forest": (8, 6), "mountain": (8, 6), "lake": (6, 4), "sea": (12, 8), "pass": (6, 12)}
SIZE_WORDS = {"tiny": 0.4, "small": 0.6, "little": 0.6, "large": 1.5, "big": 1.5, "huge": 2.0, "vast": 2.5, "enormous": 2.5}
CARDINAL_POINTS = {"north": (.5, .12), "south": (.5, .88), "east": (.88, .5), "west": (.12, .5),
                   "northeast": (.85, .15), "northwest": (.15, .15), "southeast": (.85, .85), "southwest": (.15, .85)}
CARDINAL_RE = r"\b(north[\s-]?east|north[\s-]?west|south[\s-]?east|south[\s-]?west|north|south|east|west)(?:ern|ward|wards)?\b"


def _pk(action, **kw) -> dict:
    p = {"action": action, "name": "", "role": "", "x": 0, "y": 0, "w": 0, "h": 0, "style": "", "direction": "",
         "motivation": "", "secret": "", "prompt_portrait": ""}
    p.update(kw)
    return p


def _extract_name(t: str) -> str:
    m = re.search(r"\b(?:named|called|dubbed)\s+(.+?)(?=\s+(?:in|on|to|near|by|at|from|along|beside|next|north|south|east|west|running|above|below|behind|around|with|for)\b|[.,;!]|$)", t, re.I)
    if m:
        return m.group(1).strip(" '\"")
    m = re.search(r"\b((?:kingdom|empire|isle|island|republic|duchy)\s+of\s+[A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+)?)", t)
    return m.group(1) if m else ""


def _cardinal_point(low: str, ctx: dict):
    m = re.search(CARDINAL_RE, low)
    if not m:
        return None
    fx, fy = CARDINAL_POINTS[re.sub(r"[\s-]", "", m.group(1))]
    return int(ctx["cols"] * fx), int(ctx["rows"] * fy)


def _anchor_point(low: str, ctx: dict):
    for pattern, pos in (ctx.get("anchors") or ANCHORS):
        if re.search(pattern, low):
            return int(pos[0]), int(pos[1])
    return None


def _resolve_place(text: str, ctx: dict):
    """'near <text>' -> a named landmark in the scene, else an anchor keyword."""
    t = re.sub(r"^(?:the|a|an)\s+", "", (text or "").strip().lower())
    if not t:
        return None
    places = ctx.get("places") or []
    for p in places:                                     # exact, then containment either way
        if p["name"].lower() == t:
            return p["x"], p["y"]
    for p in places:
        n = p["name"].lower()
        if t in n or n in t:
            return p["x"], p["y"]
    return _anchor_point(t, ctx)


def _place_point(stripped: str, ctx: dict):
    """Where a thing should go: 'near X' (named place or anchor) > compass word > any anchor word."""
    m = re.search(r"\b(?:near|by|beside|next to|close to|around|outside)\s+(?:the\s+)?([\w' -]+)", stripped)
    if m:
        p = _resolve_place(m.group(1), ctx)
        if p:
            return p
    return _cardinal_point(stripped, ctx) or _anchor_point(stripped, ctx)


def _explicit_size(low: str):
    m = re.search(r"\b(\d{1,2})\s*(?:x|×|by)\s*(\d{1,2})\b", low)
    return (int(m.group(1)), int(m.group(2))) if m else None


BUILDING_NAMES = {
    "Tavern": ["The Gilded Flagon", "The Crooked Anchor", "The Laughing Boar", "The Sleeping Fox"],
    "Alchemist Shop": ["The Bubbling Retort", "Moonroot Apothecary", "Vael & Daughter, Alchemists"],
    "Smithy": ["Ironhand's Forge", "The Cinder Anvil", "Redhill Smithy"],
    "Temple": ["Chapel of the Lantern", "Shrine of Quiet Waters", "Temple of the Dawn"],
    "Shop": ["Pennyfarthing & Sons", "The Curious Satchel", "Ashby's Goods"],
    "House": ["Old Hollis' House", "The Willow Cottage", "Marrow Lane Cottage"],
    "Tower": ["The Grey Spire", "Watcher's Tower"], "Keep": ["Blackthorn Keep", "Fort Merrow"],
}


def _default_building_name(role: str, low: str) -> str:
    pool = BUILDING_NAMES.get(role) or [f"The Old {role}"]
    return random.Random(low).choice(pool)


def _rule_world(t: str, low: str, ctx: dict):
    cols, rows = ctx["cols"], ctx["rows"]
    centre = tuple(ctx.get("default") or (cols // 2, rows // 2))
    scale = max(cols, rows) / 24.0

    # ---- EXPAND_MAP ----
    if re.match(r"\s*(?:please\s+)?(?:expand|enlarge|extend|grow|widen|increase)\b", low) and \
            re.search(r"\b(map|realm|world|scene|region|town|area|canvas|grid|square|wilds?)\b", low):
        if re.search(r"\b(all\s+sides|every\s+side|all\s+directions|all\s+around|everywhere|on\s+all)\b", low):
            direction = "all"
        else:
            m = re.search(r"\b(north|south|east|west)(?:ern|ward|wards)?\b", low)
            direction = m.group(1) if m else "all"
        m = re.search(r"\bby\s+(\d+)", low) or re.search(r"(\d+)\s+(?:more\s+)?(?:tiles?|squares?|cells?)", low) or re.search(r"(\d+)", low)
        amount = max(1, min(64, int(m.group(1)))) if m else 8
        found = {k for k, pat in TERRAINS if re.search(pat, low)}
        style = next((k for k in ("desert", "forest", "swamp", "plains", "mountain", "island") if k in found), "")
        kingdom = "kingdom" in found
        return _pk("EXPAND_MAP", name=_extract_name(t), role="kingdom" if kingdom else "", w=amount,
                   style=style, direction=direction)

    verb = re.match(rf"\s*{VERB}\s+", low)
    if not verb:
        return None
    rest = low[verb.end():]
    head = " ".join(rest.split()[:5])

    # ---- PLACE_LANDMARK (towns, forts, ruins, ... are places, never people) ----
    for pat, role in LANDMARK_NOUNS:
        if re.match(rf"(?:a|an|the|some)?\s*{PREFIX}{{0,2}}(?:{pat})\b{PERSON}", rest):
            name = _extract_name(t) or _default_place_name(role, low)
            stripped = re.sub(rf"\b(?:{pat})\b", " ", low.replace(name.lower(), " "))
            pos = _place_point(stripped, ctx) or centre
            w, h = _explicit_size(low) or (0, 0)
            return _pk("PLACE_LANDMARK", name=name, role=role, x=pos[0], y=pos[1], w=w, h=h)

    # ---- PLACE_BUILDING ----
    for pat, role in BUILD_NOUNS:
        if re.match(rf"(?:a|an|the|some)?\s*{PREFIX}{{0,2}}(?:{pat})\b{PERSON}", rest):
            name = _extract_name(t)
            stripped = low.replace(name.lower(), " ") if name else low
            stripped = re.sub(rf"\b(?:{pat})\b", " ", stripped)        # the building's own noun is not a place
            pos = _anchor_point(stripped, ctx) or _cardinal_point(stripped, ctx) or centre
            if not name:
                name = _default_building_name(role, low)
            mat = re.search(r"\b(stone|brick|marble|wooden|wood|timber|log)\b", low)
            style = "stone" if (mat and mat.group(1) in ("stone", "brick", "marble")) else \
                    ("wood" if mat else ("stone" if role in STONE_ROLES else "wood"))
            w, h = _explicit_size(low) or (0, 0)
            return _pk("PLACE_BUILDING", name=name or "", role=role, x=pos[0], y=pos[1], w=w, h=h, style=style)

    # ---- PAINT_TERRAIN ----
    for key, pat in TERRAINS:
        if re.search(pat, head):
            if key == "road":
                return _road_packet(re.split(pat, low, maxsplit=1)[1], ctx)
            name = _extract_name(t)
            stripped = low.replace(name.lower(), " ") if name else low
            direction = ""
            if key == "river":
                m = re.search(r"\b(?:running|flowing|runs|flows|heading|going|toward|towards|to)\s+(?:the\s+)?(north|south|east|west)", low)
                direction = m.group(1) if m else "south"
                pos = _anchor_point(stripped, ctx)
                if not pos:
                    pos = {"south": (int(cols * .7), int(rows * .2)), "north": (int(cols * .7), int(rows * .8)),
                           "east": (int(cols * .2), int(rows * .7)), "west": (int(cols * .8), int(rows * .7))}[direction]
            else:
                pos = _cardinal_point(stripped, ctx) or _anchor_point(stripped, ctx) or centre
                if key == "pass":
                    m = re.search(CARDINAL_RE, low)
                    direction = re.sub(r"[\s-]", "", m.group(1)) if m else "east"
                    direction = "east" if direction in ("northeast", "southeast") else "west" if direction in ("northwest", "southwest") else direction
            style, role = key, ""
            if key == "kingdom":
                role = "kingdom"
                adj = next((k for k in ("desert", "forest", "swamp", "plains") if re.search(dict(TERRAINS)[k], low)), "")
                style = adj or "kingdom"
                if not name:
                    nm = re.search(r"\bof\s+([A-Z][\w'’-]+)", t)
                    name = f"Kingdom of {nm.group(1)}" if nm else ""
            elif re.search(r"\bkingdom\b", low):
                role = "kingdom"
            w = h = 0
            sz = _explicit_size(low)
            if sz:
                w, h = sz
            else:
                mod = next((v for k, v in SIZE_WORDS.items() if re.search(rf"\b{k}\b", low)), None)
                if mod and key in TERRAIN_SIZE:
                    dw, dh = TERRAIN_SIZE[key]
                    w, h = max(3, int(dw * scale * mod)), max(2, int(dh * scale * mod))
            return _pk("PAINT_TERRAIN", name=name, role=role, x=pos[0], y=pos[1], w=w, h=h, style=style, direction=direction)
    return None



def _run_llm_blueprint(text: str, ctx: dict) -> dict:
    with _lock:
        out = _llm["model"].create_chat_completion(
            messages=[
                {"role": "system", "content": BP_SYSTEM + "\n\n" + _context_block(ctx)},
                {"role": "user", "content": f"GM: {text}"},
            ],
            grammar=_llm["bp_grammar"],
            temperature=0.3,
            max_tokens=900,
        )
    return json.loads(out["choices"][0]["message"]["content"])


# --------------------------------------------------------------------------- #
# rule-based fallback
# --------------------------------------------------------------------------- #
ROLE_KEYWORDS = [
    (("dragon", "wyrm", "drake"), "Ancient Dragon"),
    (("wolf", "wolves", "bear", "boar", "hound", "beast", "monster"), "Hostile Beasts"),
    (("legion", "army", "regiment", "warband", "fleet"), "Faction Army"),
    (("barkeep", "bartender", "innkeeper", "publican"), "Barkeep"),
    (("watchman", "watchmen", "guard", "sentry", "watch"), "Watchman"),
    (("smuggler",), "Smuggler"),
    (("assassin",), "Assassin"),
    (("bard", "minstrel", "singer"), "Bard"),
    (("merchant", "trader", "peddler"), "Merchant"),
    (("thief", "rogue", "cutpurse"), "Rogue"),
    (("hermit", "recluse", "pilgrim", "wanderer"), "Hermit"),
    (("mage", "wizard", "sorcerer", "witch"), "Mage"),
    (("priest", "cleric", "monk", "friar"), "Cleric"),
    (("knight", "paladin", "soldier", "mercenary"), "Mercenary"),
    (("noble", "lord", "lady", "duke"), "Noble"),
    (("stranger", "hooded", "cloaked"), "Hooded Stranger"),
    (("drunk", "regular", "patron", "traveler", "traveller", "customer"), "Patron"),
]

ANCHORS = [  # (regex, (x, y)) — first match wins; tuned to The Rusty Flagon
    (r"behind (the )?bar", (9, 3)),
    (r"\b(bar|stool|counter)\b", (10, 6)),
    (r"\b(hearth|fire|fireplace)\b", (3, 6)),
    (r"\b(door|entrance|entry)\b", (11, 14)),
    (r"\b(booth|corner)\b", (22, 8)),
    (r"\b(table)\b", (11, 10)),
    (r"\b(cellar|barrels?)\b", (2, 13)),
]

NAME_POOL = ["Brannoch", "Isolde", "Tam", "Verrick", "Sable", "Corwin", "Hesper", "Dunstan", "Maeve", "Orsin"]

MOTIVATIONS = {
    "Hermit": "Be left alone with their thoughts.",
    "Ancient Dragon": "Guard its hoard and burn what climbs too high.",
    "Faction Army": "Hold its ground and obey the crown's orders.",
    "Hostile Beasts": "Hunt anything that wanders into their territory.",
    "Barkeep": "Keep the peace and the till full.",
    "Watchman": "Get through the shift without incident.",
    "Smuggler": "Move the cargo before dawn.",
    "Assassin": "Wait for the mark to be alone.",
    "Bard": "Earn enough coin for a bed tonight.",
    "Merchant": "Sell something to someone who cannot afford it.",
    "Rogue": "Find an easy purse and a quick exit.",
    "Mage": "Find a quiet corner to finish a delicate ritual.",
    "Cleric": "Tend the sick and avoid the spirits.",
    "Mercenary": "Find a paying contract before the coin runs out.",
    "Noble": "Be seen, but not recognized.",
    "Hooded Stranger": "Wait for someone who is late.",
    "Patron": "Drink, listen, and forget the road.",
}

SECRETS = {
    "Hermit": "DC 13: Fled something terrible, and it may have followed.",
    "Ancient Dragon": "DC 17: Bound by an oath it is desperate to break.",
    "Faction Army": "DC 14: Supplies are short and morale is worse than the banners suggest.",
    "Hostile Beasts": "DC 12: They are driven from their den and starving, so they will not retreat.",
    "Barkeep": "DC 13: Owes a dangerous debt and is skimming the till to pay it.",
    "Watchman": "DC 12: Takes bribes to ignore the back cellar.",
    "Smuggler": "DC 14: The cargo in the cellar is not what the manifest says.",
    "Assassin": "DC 16: Carries a poisoned needle sewn into the cuff.",
    "Bard": "DC 12: Every song contains a coded message for someone in the room.",
    "Merchant": "DC 13: The goods are stolen from a temple.",
    "Rogue": "DC 14: Already lifted a ring from the last table.",
    "Mage": "DC 15: Is being hunted by the order that trained them.",
    "Cleric": "DC 13: Lost their faith but not their habit.",
    "Mercenary": "DC 14: Deserted the Duke's company.",
    "Noble": "DC 15: Disowned and traveling under a false name.",
    "Hooded Stranger": "DC 15: Carries a letter meant for someone else in this room.",
    "Patron": "DC 11: Saw something on the road they have told no one.",
}


def _first_cap_name(text: str) -> str | None:
    m = re.search(r"\b(?:named|called)\s+([A-Z][\w'’-]*(?:\s+[A-Z][\w'’-]*)?)", text)
    if m:
        return m.group(1)
    words = re.findall(r"\b[A-Z][a-z'’-]{2,}\b", text)
    words = [w for w in words if w.lower() not in {"add", "put", "place", "spawn", "log", "note", "the", "a", "an"}]
    return words[0] if words else None


def _rule_parse(text: str, ctx: dict) -> dict:
    t = text.strip()
    low = t.lower()

    if not re.match(r"\s*(rumou?r|clue|event|gossip|news)\b", low):
        world = _rule_world(t, low, ctx)
        if world:
            return world

    moves = re.search(r"\b(embargo|decree[sd]?|declares?|marches|marching|advances?|mobili[sz]es?|besieges?|sails?)\b", low)
    placing = re.match(r"\s*(add|place|put|spawn|station|summon)\b", low)
    if re.search(r"\b(rumou?r|clue|event|gossip|whisper|news)\b", low) or (moves and not placing):
        body = re.sub(
            r"^\s*((add|log|record|note|create)\s+)?(a\s+|an\s+|the\s+)?(rumou?r|clue|event|gossip|whisper|news)\s*(that|about|of|:|-)?\s*",
            "", t, flags=re.I,
        ).strip(" .:") or t
        title = " ".join(w[:1].upper() + w[1:] for w in body.split()[:5]).strip(" ,.;")
        return {
            "action": "LOG_RUMOR", "name": title or "Unnamed Rumor", "role": "rumor", "x": -1, "y": -1,
            "motivation": "", "secret": f"DC 12: {body[0].upper() + body[1:] if body else t}",
            "prompt_portrait": "",
        }

    role = "Patron"
    for words, label in ROLE_KEYWORDS:
        if any(re.search(rf"\b{w}s?\b", low) for w in words):
            role = label
            break

    name = _first_cap_name(t)
    if role == "Faction Army" and not re.search(r"\b(named|called)\b", t):
        m = re.search(r"((?:\d+(?:st|nd|rd|th)\s+)?(?:[A-Z][\w'’-]*\s+)*(?:Legion|Army|Regiment|Warband|Fleet))", t)
        name = m.group(1) if m else name
    if not name and role in ("Hostile Beasts", "Ancient Dragon"):
        m = re.search(r"\b(wolf pack|wolf|wolves|bear|boar|hound|beast|monster|dragon|wyrm|drake)s?\b", low)
        name = m.group(0).title()
    name = name or random.Random(low).choice(NAME_POOL)

    x, y = ctx.get("default") or (11, 7)
    coords = re.search(r"\(?\s*\bx?\s*(\d{1,2})\s*[, ]\s*y?\s*(\d{1,2})\s*\)?", low) if re.search(r"\d", low) else None
    if coords:
        x, y = int(coords.group(1)), int(coords.group(2))
    else:
        for pattern, pos in (ctx.get("anchors") or ANCHORS):
            if re.search(pattern, low):
                x, y = pos
                break

    flavor = re.search(r"\b(?:who|with|wearing|carrying|holding)\s+(.+)$", t, re.I)
    look = f", {flavor.group(1).strip(' .')}" if flavor else ""
    return {
        "action": "PROVISION_HOST", "name": name, "role": role, "x": x, "y": y,
        "motivation": MOTIVATIONS.get(role, MOTIVATIONS["Patron"]),
        "secret": SECRETS.get(role, SECRETS["Patron"]),
        "prompt_portrait": f"{role.lower()} in a medieval fantasy tavern{look}",
    }


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# slash commands: deterministic, no guessing
# --------------------------------------------------------------------------- #
class CommandError(ValueError):
    """A malformed or unknown slash command; the message tells the GM how to fix it."""


DIR_WORDS = r"(north[\s-]?east|north[\s-]?west|south[\s-]?east|south[\s-]?west|north|south|east|west)"
LANDMARK_ALIASES = {"township": "town", "fortress": "fort", "stronghold": "fort", "ruin": "ruins", "crypt": "dungeon",
                    "abbey": "monastery", "harbor": "port", "harbour": "port", "encampment": "camp",
                    "church": "temple", "shrine": "temple", "chapel": "temple"}
BUILDING_TYPES = {
    "tavern": "Tavern", "inn": "Tavern", "pub": "Tavern", "shop": "Shop", "store": "Shop", "smithy": "Smithy",
    "smith": "Smithy", "forge": "Smithy", "temple": "Temple", "house": "House", "cottage": "House", "home": "House",
    "barracks": "Barracks", "guild": "Guildhall", "guildhall": "Guildhall", "alchemist": "Alchemist Shop",
    "apothecary": "Alchemist Shop", "warehouse": "Warehouse", "stable": "Stable", "library": "Library",
    "bakery": "Bakery", "tower": "Tower", "manor": "Manor",
}
SLASH_USAGE = {
    "town": "/town <name> [near <place> | north|south|east|west]", "city": "/city <name> [near <place> | direction]",
    "castle": "/castle <name> [near <place> | direction]", "dungeon": "/dungeon <name> [near <place> | direction]",
    "ruins": "/ruins <name> [near <place> | direction]", "island": "/island <name> [north|south|east|west]",
    "building": "/building <tavern|shop|smithy|temple|house|barracks|guild> <name> [near <place>]",
    "road": "/road [from <place>] [to <place>]   or   /road <north|south|east|west>",
    "npc": "/npc <name> [role] [secret]     (or: /npc Name | Role | Secret)",
    "beast": "/beast <name> [near <place>]", "army": "/army <name> [near <place>]",
    "expand": "/expand <north|south|east|west|all> <tiles>", "rumor": "/rumor [DC <n>] <text>",
    "pc": "/pc <name> [class]     (a player-character token that players can move)",
    "newmap": "/newmap <name> [world|region|town|interior|dungeon] [cols] [rows]",
    "move": "/move <landmark name> <x> <y>",
}
SLASH_ALL = sorted(set(SLASH_USAGE) | set(LANDMARK_ROLES) | set(LANDMARK_ALIASES) | {"help"})


def _dir_point(word: str, ctx: dict):
    fx, fy = CARDINAL_POINTS[re.sub(r"[\s-]", "", word.lower())]
    return int(ctx["cols"] * fx), int(ctx["rows"] * fy)


def _where(rest: str, ctx: dict, cmd: str):
    """Split '<name…> [at x,y | near <place> | <direction>]' into (name text, point or None)."""
    r = " " + rest.strip()
    pt = None
    m = re.search(r"\s+at\s+(\d{1,3})\s*[, ]\s*(\d{1,3})\s*$", r)
    if m:
        pt, r = (int(m.group(1)), int(m.group(2))), r[:m.start()]
    direction = None
    m = re.search(rf"\s+(?:(?:to|in|on)\s+the\s+)?{DIR_WORDS}\s*$", r, re.I)
    if m and r[:m.start()].strip():
        direction, r = m.group(1), r[:m.start()]
    m = re.search(r"\s+near\s+(?:the\s+)?(.+?)\s*$", r, re.I)
    if m and not pt:
        pt = _resolve_place(m.group(1), ctx)
        if not pt:
            raise CommandError(f"I can't find '{m.group(1)}' on this map. Use a landmark's name, or a direction: {SLASH_USAGE.get(cmd, '/help')}")
        r = r[:m.start()]
    elif m:
        r = r[:m.start()]
    if direction and not pt:
        pt = _dir_point(direction, ctx)
    return r.strip().strip("\"'"), pt


def _centre(ctx: dict):
    return tuple(ctx.get("default") or (ctx["cols"] // 2, ctx["rows"] // 2))


def _road_packet(args: str, ctx: dict) -> dict:
    a = args.strip()
    usage = SLASH_USAGE["road"]
    centre = _centre(ctx)
    cols, rows = ctx["cols"], ctx["rows"]
    mf = re.search(r"\bfrom\s+(?:the\s+)?(.+?)(?=\s+to\s+|\s*$)", a, re.I)
    mt = re.search(r"\bto\s+(?:the\s+)?(.+?)(?=\s+from\s+|\s*$)", a, re.I)
    md = re.fullmatch(rf"(?:(?:to|towards?)\s+(?:the\s+)?)?{DIR_WORDS}", a, re.I)
    if mf or (mt and not md):
        A = _resolve_place(mf.group(1), ctx) if mf else None
        B = _resolve_place(mt.group(1), ctx) if mt else None
        if mf and not A:
            raise CommandError(f"I can't find '{mf.group(1)}' on this map. {usage}")
        if mt and not B:
            raise CommandError(f"I can't find '{mt.group(1)}' on this map. {usage}")
        A = A or centre
        if not B:      # only a start was given: run to the nearest other place
            others = [(abs(p["x"] - A[0]) + abs(p["y"] - A[1]), (p["x"], p["y"])) for p in ctx.get("places", [])
                      if abs(p["x"] - A[0]) + abs(p["y"] - A[1]) > 3]
            B = min(others)[1] if others else (A[0], rows - 2)
    elif md:
        k = re.sub(r"[\s-]", "", md.group(1).lower())
        A = centre
        B = (int(cols * 0.95) if "east" in k else int(cols * 0.05) if "west" in k else A[0],
             int(rows * 0.95) if "south" in k else int(rows * 0.05) if "north" in k else A[1])
    else:
        raise CommandError(f"Usage: {usage}")
    if tuple(A) == tuple(B):
        raise CommandError("Those two points are the same place. Pick two different ones.")
    return _pk("PAINT_TERRAIN", style="road", x=A[0], y=A[1], w=B[0], h=B[1])


def _npc_parts(rest: str):
    if "|" in rest or ";" in rest:
        parts = [p.strip() for p in re.split(r"[|;]", rest)]
        return (parts + ["", ""])[:3]
    try:
        toks = shlex.split(rest)
    except ValueError:
        toks = rest.split()
    return [toks[0] if toks else "", toks[1] if len(toks) > 1 else "", " ".join(toks[2:])]


def parse_slash(text: str, ctx: dict) -> dict:
    m = re.match(r"\s*/(\w+|\?)\s*(.*)$", text, re.S)
    if not m:
        raise CommandError("Commands start with a slash, for example /town Valesom near Highmark. Type /help for the list.")
    cmd, rest = m.group(1).lower(), m.group(2).strip()
    if cmd in ("help", "?"):
        raise CommandError("Commands: " + "  ".join("/" + c for c in sorted(SLASH_USAGE)) + "  (plus /village /fort /port /temple ... same as /town)")
    cmd = LANDMARK_ALIASES.get(cmd, cmd)
    centre = _centre(ctx)

    if cmd in LANDMARK_ROLES:                                        # /town /city /castle /dungeon /ruins ...
        name, pt = _where(rest, ctx, cmd if cmd in SLASH_USAGE else "town")
        return _pk("PLACE_LANDMARK", name=name or _default_place_name(cmd, rest + cmd), role=cmd, x=(pt or centre)[0], y=(pt or centre)[1])

    if cmd == "island":
        name, pt = _where(rest, ctx, cmd)
        pt = pt or centre
        return _pk("PAINT_TERRAIN", name=name or f"Isle of {random.Random(rest).choice(['Crows', 'Mists', 'Embers', 'Gulls'])}",
                   style="island", x=pt[0], y=pt[1])

    if cmd == "building":
        name, pt = _where(rest, ctx, cmd)
        mm = re.match(r"(?:(stone|brick|marble|wood|wooden|timber)\s+)?(\w+)\s*(.*)$", name, re.I)
        if not mm or mm.group(2).lower() not in BUILDING_TYPES:
            raise CommandError(f"Building types: {', '.join(sorted(set(BUILDING_TYPES)))}. Usage: {SLASH_USAGE['building']}")
        role = BUILDING_TYPES[mm.group(2).lower()]
        bname = mm.group(3).strip().strip("\"'") or _default_building_name(role, rest)
        mat = (mm.group(1) or "").lower()
        style = "stone" if mat in ("stone", "brick", "marble") else "wood" if mat else \
                ("stone" if role in STONE_ROLES | {"Barracks", "Tower", "Manor"} else "wood")
        pt = pt or centre
        w, h = _explicit_size(rest) or (0, 0)
        return _pk("PLACE_BUILDING", name=bname, role=role, x=pt[0], y=pt[1], w=w, h=h, style=style)

    if cmd == "road":
        return _road_packet(rest, ctx)

    if cmd == "pc":
        body, pt = _where(rest, ctx, cmd)
        pt = pt or centre
        name, cls, _ = _npc_parts(body)
        if not name:
            raise CommandError(f"Usage: {SLASH_USAGE['pc']}")
        return _pk("PROVISION_HOST", name=name, role="Player Character", x=pt[0], y=pt[1],
                   motivation="Play their next scene.", secret="DC 10: Notes about this player's character go here.",
                   prompt_portrait=f"{cls or 'adventurer'} named {name}, heroic fantasy portrait")

    if cmd in ("npc", "beast", "army"):
        body, pt = _where(rest, ctx, cmd)
        pt = pt or centre
        if cmd == "npc":
            name, role, secret = _npc_parts(body)
            if not name:
                raise CommandError(f"Usage: {SLASH_USAGE['npc']}")
            role = role or "Townsfolk"
            canon = next((lbl for words, lbl in ROLE_KEYWORDS if any(re.search(rf"\b{w}s?\b", role.lower()) for w in words)), None)
            return _pk("PROVISION_HOST", name=name, role=role, x=pt[0], y=pt[1],
                       motivation=MOTIVATIONS.get(canon or "", "Go about their business."),
                       secret=secret or SECRETS.get(canon or "", "DC 11: Knows more than they let on."),
                       prompt_portrait=f"{role.lower()} named {name}, medieval fantasy portrait")
        role = "Hostile Beasts" if cmd == "beast" else "Faction Army"
        name = body.strip("\"'") or ("Wolf Pack" if cmd == "beast" else "Crown Legion")
        return _pk("PROVISION_HOST", name=name, role=role, x=pt[0], y=pt[1],
                   motivation=MOTIVATIONS[role], secret=SECRETS[role], prompt_portrait=f"{name}, {role.lower()}, fantasy illustration")

    if cmd == "expand":
        mm = re.match(r"(north|south|east|west|all)\s+(\d{1,3})\b\s*(.*)$", rest, re.I)
        if not mm:
            raise CommandError(f"Usage: {SLASH_USAGE['expand']}")
        tail = mm.group(3).lower()
        found = {k for k, pat in TERRAINS if re.search(pat, tail)}
        style = next((k for k in ("desert", "forest", "swamp", "plains", "mountain", "island") if k in found), "")
        return _pk("EXPAND_MAP", direction=mm.group(1).lower(), w=max(1, min(64, int(mm.group(2)))), style=style,
                   role="kingdom" if "kingdom" in found else "", name=_extract_name(mm.group(3)))

    if cmd == "newmap":                                              # a blank scene, child of the one in view
        mm = re.match(r"(.+?)(?:\s+(world|region|town|interior|dungeon))?(?:\s+(\d{1,3}))?(?:\s+(\d{1,3}))?\s*$", rest, re.I | re.S)
        name = (mm.group(1) if mm else "").strip().strip("\"'")
        if not name:
            raise CommandError(f"Usage: {SLASH_USAGE['newmap']}")
        cols = int(mm.group(3)) if mm.group(3) else 0
        rows = int(mm.group(4)) if mm.group(4) else (round(cols * 0.75) if cols else 0)
        if (cols and not 8 <= cols <= 128) or (rows and not 8 <= rows <= 96):
            raise CommandError("Columns must be 8-128 and rows 8-96. " + SLASH_USAGE["newmap"])
        return _pk("NEW_MAP", name=name, style=(mm.group(2) or "region").lower(), w=cols, h=rows)

    if cmd == "move":                                                # reposition a landmark pin (top-left tile)
        mm = re.match(r"(.+?)\s+(?:to\s+|at\s+)?(\d{1,3})\s*[, ]\s*(\d{1,3})\s*$", rest, re.I | re.S)
        if not mm:
            raise CommandError(f"Usage: {SLASH_USAGE['move']}")
        return _pk("MOVE_LANDMARK", name=mm.group(1).strip().strip("\"'"), x=int(mm.group(2)), y=int(mm.group(3)))

    if cmd == "rumor":
        mm = re.match(r"(?:DC\s*(\d{1,2})\s+)?(.+)$", rest, re.I | re.S)
        if not mm:
            raise CommandError(f"Usage: {SLASH_USAGE['rumor']}")
        body = mm.group(2).strip()
        title = " ".join(w[:1].upper() + w[1:] for w in body.split()[:5]).strip(" ,.;")
        return _pk("LOG_RUMOR", name=title, role="rumor", x=-1, y=-1, secret=f"DC {mm.group(1) or 12}: {body[:1].upper()}{body[1:]}")

    near = difflib.get_close_matches(cmd, SLASH_ALL, n=1)
    raise CommandError(f"Unknown command /{cmd}." + (f" Did you mean /{near[0]}?" if near else " Type /help for the list."))


WORLD_ACTIONS = ("PAINT_TERRAIN", "PLACE_BUILDING", "PLACE_LANDMARK", "EXPAND_MAP")
BLUEPRINT_ACTIONS = ("PROVISION_HOST", "LOG_RUMOR") + WORLD_ACTIONS + ("GENERATE_SCENE",)
INSTANT_ACTIONS = ("NEW_MAP", "MOVE_LANDMARK")        # slash-only: never proposed by the model, never part of a blueprint
ACTIONS = BLUEPRINT_ACTIONS + INSTANT_ACTIONS


def normalize_packet(p: dict, ctx: dict) -> dict:
    """Public: clamp and sanitise a packet (also used to re-validate packets sent back by the client)."""
    return _normalize(p, ctx)


def _normalize(p: dict, ctx: dict) -> dict:
    action = p.get("action") if p.get("action") in ACTIONS else "PROVISION_HOST"
    def clamp(v, hi):
        try:
            return max(0, min(hi, int(v or 0)))
        except (TypeError, ValueError):
            return 0
    direction = str(p.get("direction") or "").lower()
    out = {
        "action": action,
        "name": str(p.get("name") or ("" if action in WORLD_ACTIONS else "Unnamed")).strip()[:60],
        "role": str(p.get("role") or "").strip()[:60],
        "w": clamp(p.get("w"), 127), "h": clamp(p.get("h"), 127),
        "style": str(p.get("style") or "").strip().lower()[:30],
        "direction": direction if direction in ("north", "south", "east", "west", "all") else "",
        "motivation": str(p.get("motivation") or "").strip()[:300],
        "secret": str(p.get("secret") or "").strip()[:500],
        "prompt_portrait": str(p.get("prompt_portrait") or "").strip()[:300],
    }
    if action == "PLACE_LANDMARK":
        out["role"] = _landmark_role(out["role"] or out["name"])
    if action == "LOG_RUMOR":
        out.update(x=-1, y=-1, role=out["role"] or "rumor")
    elif action == "EXPAND_MAP":
        out.update(x=0, y=0, w=max(1, out["w"]) if out["w"] else 8, direction=out["direction"] or "all")
    else:
        out.update(x=clamp(p.get("x", 11), ctx["cols"] - 1), y=clamp(p.get("y", 7), ctx["rows"] - 1))
    if action == "GENERATE_SCENE":
        out.update(x=0, y=0, w=0, h=0, role="", style="", direction="")
    if action == "NEW_MAP":
        out.update(x=0, y=0, role="", direction="")
    if action in WORLD_ACTIONS or action in ("GENERATE_SCENE",) + INSTANT_ACTIONS:
        out["secret"] = out["motivation"] = out["prompt_portrait"] = ""
    elif not re.match(r"^\s*DC\s*\d", out["secret"], re.I):
        out["secret"] = f"DC 12: {out['secret']}".strip()
    return out


# --------------------------------------------------------------------------- #
# Propose & confirm: natural language -> BlueprintProposal (never touches the board)
# --------------------------------------------------------------------------- #
CLAUSE_SPLIT = re.compile(
    r"\s*;\s*|\s+(?:and\s+then|then)\s+|\s*,\s+and\s+"
    r"|\s+and\s+(?=(?:also\s+)?(?:add|build|place|put|create|carve|paint|expand|raise|spawn|summon|station|log|generate|map)\b)"
    r"|\s+and\s+(?=(?:an?|the)\s+(?:[\w'-]+\s+){0,3}?(?:named|called)\b)", re.I)
CREATE_VERB = re.compile(r"^\s*(?:please\s+|also\s+|can you\s+|could you\s+)?(?:add|place|put|spawn|summon|station|introduce|create|bring in|build|raise|stamp|carve|paint|draw|form|make|dig|open|set up|expand|enlarge|extend|grow|widen|increase)\b", re.I)
GENERATE_CLAUSE = re.compile(r"^\s*(?:please\s+|also\s+)?(?:(?:generate|create|build|make|draw)\s+(?:its|the|a|an|their)?\s*(?:own\s+)?(?:map|scene|interior|layout)|map\s+(?:it|that|them)\s+out)\b", re.I)
MOVE_VERB = re.compile(r"^\s*(?:please\s+)?(?:move|shift|send|relocate|teleport|drag|march)\b", re.I)
REMOVE_VERB = re.compile(r"^\s*(?:please\s+)?(?:remove|delete|erase|destroy|clear|undo|rename|kill)\b", re.I)
NOTHING = "This will not change anything: I couldn't tell what to build. Try “Add a town named Valesom in the east” or type /help."
NAMED_ACTIONS = ("PLACE_LANDMARK", "PLACE_BUILDING", "PAINT_TERRAIN")


def _has_role(low: str) -> bool:
    return any(re.search(rf"\b{w}s?\b", low) for words, _ in ROLE_KEYWORDS for w in words)


def _rules_blueprint(text: str, ctx: dict):
    """Deterministic clause parser. It only acts on clear cues; anything vague becomes a warning, never a guess."""
    actions, warnings, wants_scene = [], [], False
    for clause in [c.strip(" .!?\n") for c in CLAUSE_SPLIT.split(text) if c and c.strip(" .!?")]:
        clause = re.sub(r"^(?:then|and|also)\s+", "", clause, flags=re.I)
        if actions and re.match(r"(?:an?|the)\s", clause, re.I) and not CREATE_VERB.match(clause):
            clause = "Add " + clause                     # "…and a hermit called Ysolde" inherits the verb
        low = clause.lower()
        if GENERATE_CLAUSE.match(clause):
            wants_scene = True
            continue
        if MOVE_VERB.match(clause):
            warnings.append(f"“{clause}”: moving tokens is not a build action. Drag the token on the map instead.")
            continue
        if REMOVE_VERB.match(clause):
            warnings.append(f"“{clause}”: removing or renaming things isn't supported yet, so I left it out.")
            continue
        try:
            world = _rule_world(clause, low, ctx)
        except CommandError as exc:
            warnings.append(f"“{clause}”: {exc}")
            continue
        if world:
            actions.append(world)
            continue
        is_rumor = re.search(r"\b(rumou?r|clue|event|gossip|whisper|news)\b", low) or \
            re.search(r"\b(embargo|decree[sd]?|declares?|marches|marching|advances?|mobili[sz]es?|besieges?|sails?)\b", low)
        if is_rumor:
            actions.append(_rule_parse(clause, ctx))
            continue
        if CREATE_VERB.match(clause) and (_has_role(low) or re.search(r"\b(?:named|called)\b", low)):
            actions.append(_rule_parse(clause, ctx))
            continue
        warnings.append(f"“{clause}”: I didn't recognise what to add, so I left it out.")
    if wants_scene:
        target = next((a for a in reversed(actions) if a["action"] in NAMED_ACTIONS and a.get("name")), None)
        if target:
            actions.append(_pk("GENERATE_SCENE", name=target["name"]))
        else:
            warnings.append("“generate its map”: there is no named place in this request to generate a map for.")
    return actions[:6], warnings


def _summarize(actions: list) -> str:
    if not actions:
        return NOTHING
    parts = []
    for a in actions:
        n, r, act = a.get("name", ""), (a.get("role") or "").lower(), a["action"]
        if act == "PLACE_LANDMARK":
            parts.append(f"found the {r or 'town'} {n}")
        elif act == "PLACE_BUILDING":
            parts.append(f"build {n}, a {r or 'building'}")
        elif act == "PAINT_TERRAIN":
            kind = a.get("style") or "terrain"
            parts.append("lay a road" if kind == "road" else f"paint {'the ' + kind + ' ' + n if n else 'a ' + kind}")
        elif act == "EXPAND_MAP":
            parts.append(f"expand the map {a.get('direction') or 'all around'} by {a.get('w') or 8} tiles")
        elif act == "PROVISION_HOST":
            parts.append(f"add {n}, {r or 'a character'}")
        elif act == "LOG_RUMOR":
            parts.append(f"log the rumor “{n}”")
        elif act == "GENERATE_SCENE":
            parts.append(f"generate the map for {n}")
    return "This will " + (", ".join(parts[:-1]) + " and " + parts[-1] if len(parts) > 1 else parts[0]) + "."


def propose_blueprint(text: str, ctx: dict) -> dict:
    """Turn plain English into a BlueprintProposal. Pure: no database access, nothing is executed."""
    text = text.strip()
    with _lock:
        _load()
    rule_actions, warnings = _rules_blueprint(text, ctx)
    rule_actions = [_normalize(a, ctx) for a in rule_actions]
    if _llm_state == "ready":
        try:
            raw = _run_llm_blueprint(text, ctx)
            acts = [_normalize(a, ctx) for a in (raw.get("actions") or [])[:4] if isinstance(a, dict)]
            # The keyword rules are authoritative for what KIND of thing a named place is (a town is never a person,
            # an island is never a town), and a map is only generated when the GM asked for one.
            raw_acts = list(acts)
            kind_of = {a["name"].lower(): a["action"] for a in rule_actions if a["action"] in WORLD_ACTIONS and a["name"]}
            wants_map = bool(re.search(r"\b(generate|its map|their map|interior|layout|map it|scene)\b", text, re.I))
            acts = [a for a in acts
                    if not (a["action"] == "GENERATE_SCENE" and not wants_map)
                    and not (a["action"] != "GENERATE_SCENE" and a["name"].lower() in kind_of and a["action"] != kind_of[a["name"].lower()])]
            seen, uniq = set(), []
            for a in acts:                                   # drop exact duplicates
                key = (a["action"], a["name"].lower(), a["x"], a["y"])
                if key not in seen:
                    seen.add(key); uniq.append(a)
            acts = uniq
            have = {(a["action"], a["name"].lower()) for a in acts}
            acts += [a for a in rule_actions if a["action"] in WORLD_ACTIONS and (a["action"], a["name"].lower()) not in have]
            changed = acts != raw_acts
            if acts:
                summary = re.sub(r"\s+", " ", str(raw.get("summary") or "")).strip()[:240]
                _model_ok()
                return {"summary": summary if (summary.lower().startswith("this will") and not changed) else _summarize(acts),
                        "actions": acts, "warnings": warnings, "engine": "qwen2.5-3b"}
            if rule_actions:
                return {"summary": _summarize(rule_actions), "actions": rule_actions, "warnings": warnings, "engine": "qwen2.5-3b+rules"}
            return {"summary": NOTHING, "actions": [], "warnings": warnings, "engine": "qwen2.5-3b"}
        except CommandError:
            raise
        except Exception as exc:
            _model_failed(exc)
    return {"summary": _summarize(rule_actions), "actions": rule_actions, "warnings": warnings, "engine": "rules"}


def parse_directive(text: str, ctx: dict) -> tuple[dict, str]:
    """Returns (packet, engine_name). Slash commands never touch the model. Raises CommandError for bad commands."""
    if text.lstrip().startswith("/"):
        return _normalize(parse_slash(text, ctx), ctx), "command"
    with _lock:
        _load()
    if _llm_state == "ready":
        try:
            packet = _normalize(_run_llm(text, ctx), ctx)
            if packet["action"] == "PROVISION_HOST":      # towns, forts, ruins... are places: trust the keyword rules
                rule = _rule_world(text.strip(), text.strip().lower(), ctx)
                if rule and rule["action"] in WORLD_ACTIONS:
                    return _normalize(rule, ctx), "qwen2.5-3b+rules"
            _model_ok()
            return packet, "qwen2.5-3b"
        except CommandError:
            raise
        except Exception as exc:
            _model_failed(exc)
    return _normalize(_rule_parse(text, ctx), ctx), "rules"

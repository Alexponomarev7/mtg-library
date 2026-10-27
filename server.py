#!/usr/bin/env python3
"""
Card Library - a local app that tracks where your physical Magic cards live.

Run:   python3 server.py          (Windows: py server.py)
Open:  http://127.0.0.1:8765

Needs only Python 3.8+ - no extra packages. Card and set data come from
Scryfall (internet needed for lookups; everything you add is cached locally).
Your collection is stored in library.db next to this file - back it up by copying it.
"""
import csv
import io
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("CARDLIB_DB", os.path.join(HERE, "library.db"))
PORT = int(os.environ.get("CARDLIB_PORT", "8765"))
STATIC = os.path.join(HERE, "static")
SCRYFALL = "https://api.scryfall.com"
UA = "CardLibraryLocal/1.0 (personal collection tool)"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sets(
  code TEXT PRIMARY KEY, name TEXT, released_at TEXT, card_count INT,
  printed_size INT, set_type TEXT, icon TEXT, cards_loaded INT DEFAULT 0);
CREATE TABLE IF NOT EXISTS cards(
  id TEXT PRIMARY KEY, name TEXT, set_code TEXT, collector_number TEXT,
  rarity TEXT, type_line TEXT, image TEXT, image_large TEXT, booster INT);
CREATE INDEX IF NOT EXISTS idx_cards_set ON cards(set_code);
CREATE INDEX IF NOT EXISTS idx_cards_name ON cards(name);
CREATE TABLE IF NOT EXISTS storages(
  id INTEGER PRIMARY KEY, name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('binder','box')),
  sheets INT, rows INT, cols INT, double_sided INT DEFAULT 1,
  mode TEXT DEFAULT 'set_order', capacity INT, color TEXT, notes TEXT,
  created_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS storage_sets(
  storage_id INT REFERENCES storages(id) ON DELETE CASCADE, set_code TEXT,
  position INT, new_page INT DEFAULT 1, include_extras INT DEFAULT 0,
  PRIMARY KEY(storage_id, set_code));
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, card_id TEXT REFERENCES cards(id),
  storage_id INT REFERENCES storages(id), slot INT, section TEXT,
  foil INT DEFAULT 0, condition TEXT DEFAULT 'NM', lang TEXT DEFAULT 'en',
  qty INT DEFAULT 1, added_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_items_storage ON items(storage_id);
CREATE INDEX IF NOT EXISTS idx_items_card ON items(card_id);
"""


class ApiError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status
        self.msg = msg


def connect():
    db = sqlite3.connect(DB_PATH, timeout=30)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    return db


def init_db():
    db = connect()
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    if "booster" not in {r[1] for r in db.execute("PRAGMA table_info(cards)")}:
        # older databases: add the column and reload set card lists so it gets filled in
        db.execute("ALTER TABLE cards ADD COLUMN booster INT")
        db.execute("UPDATE sets SET cards_loaded=0")
    db.commit()
    db.close()


# ---------------------------------------------------------------- Scryfall

_sf_lock = threading.Lock()
_sf_last = [0.0]


def sf_get(path_or_url, params=None):
    """GET from Scryfall. Returns parsed JSON, or None on 404."""
    url = path_or_url if path_or_url.startswith("http") else SCRYFALL + path_or_url
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
    with _sf_lock:  # Scryfall asks for ~10 requests/second max
        wait = 0.11 - (time.time() - _sf_last[0])
        if wait > 0:
            time.sleep(wait)
        _sf_last[0] = time.time()
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise ApiError(502, f"Scryfall answered with error {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError):
        raise ApiError(502, "Can't reach Scryfall - an internet connection is needed for card lookups.")


def card_row(c):
    faces = c.get("card_faces") or [{}]
    img = c.get("image_uris") or faces[0].get("image_uris") or {}
    return (c["id"], c["name"], c["set"], c["collector_number"], c.get("rarity"),
            c.get("type_line") or faces[0].get("type_line"),
            img.get("small") or img.get("normal"), img.get("normal") or img.get("large"),
            None if c.get("booster") is None else int(c["booster"]))


def save_cards(db, cards):
    db.executemany("INSERT OR REPLACE INTO cards VALUES(?,?,?,?,?,?,?,?,?)", [card_row(c) for c in cards])


def ensure_sets(db, force=False):
    if not force and db.execute("SELECT COUNT(*) FROM sets").fetchone()[0]:
        return
    data = sf_get("/sets") or {"data": []}
    rows = [(s["code"], s["name"], s.get("released_at"), s.get("card_count"), s.get("printed_size"),
             s.get("set_type"), s.get("icon_svg_uri")) for s in data["data"] if not s.get("digital")]
    db.executemany(
        """INSERT INTO sets(code,name,released_at,card_count,printed_size,set_type,icon) VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(code) DO UPDATE SET name=excluded.name, released_at=excluded.released_at,
           card_count=excluded.card_count, printed_size=excluded.printed_size,
           set_type=excluded.set_type, icon=excluded.icon""", rows)


def get_set(db, code):
    ensure_sets(db)
    row = db.execute("SELECT * FROM sets WHERE code=?", (code,)).fetchone()
    if row is None:  # maybe a brand-new set: refresh once
        ensure_sets(db, force=True)
        row = db.execute("SELECT * FROM sets WHERE code=?", (code,)).fetchone()
    if row is None:
        raise ApiError(404, f"Unknown set code '{code}'")
    return row


def ensure_set_cards(db, code, force=False):
    row = get_set(db, code)
    if row["cards_loaded"] and not force:
        return
    cards = []
    data = sf_get("/cards/search", {"q": f"e:{code}", "unique": "prints", "order": "set",
                                    "include_extras": "true", "include_variations": "true"})
    while data:
        cards += data.get("data", [])
        data = sf_get(data["next_page"]) if data.get("has_more") else None
    save_cards(db, cards)
    db.execute("UPDATE sets SET cards_loaded=1 WHERE code=?", (code,))


# ---------------------------------------------------------------- binder geometry

def sides(st):
    return 2 if st["double_sided"] else 1


def per_page(st):
    return st["rows"] * st["cols"]


def page_count(st):
    return st["sheets"] * sides(st)


def capacity(st):
    if st["kind"] == "binder":
        return page_count(st) * per_page(st)
    return st["capacity"]


def slot_loc(st, s):
    pp = per_page(st)
    p, w, sd = s // pp, s % pp, sides(st)
    return {"page": p + 1, "row": w // st["cols"] + 1, "col": w % st["cols"] + 1,
            "sheet": p // sd + 1, "side": (("front", "back")[p % sd] if sd == 2 else None)}


def loc_text(st, slot=None, section=None):
    if st["kind"] == "binder":
        if slot is None:
            return f'{st["name"]} (no pocket assigned)'
        l = slot_loc(st, slot)
        return f'{st["name"]} - Page {l["page"]}, Row {l["row"]}, Column {l["col"]}'
    return st["name"] + (f' - section "{section}"' if section else "")


def cn_key(cn):
    m = re.match(r"^(\d+)(.*)$", cn or "")
    return (int(m.group(1)), m.group(2)) if m else (10 ** 9, cn or "")


def is_main(cn, printed_size, booster=None):
    if not (cn or "").isdigit():
        return False
    if printed_size:
        return int(cn) <= printed_size
    # Scryfall leaves printed_size empty for most modern sets; showcase/borderless/bundle
    # variants are the ones not found in regular boosters
    return booster != 0


def binder_layout(db, st):
    """For 'set order' binders: every assigned set gets a run of pockets in collector-number order.
    Returns (slot_by_card, card_by_slot, sections, end_slot)."""
    by_card, by_slot, sections, offset = {}, {}, [], 0
    if st["kind"] != "binder" or st["mode"] != "set_order":
        return by_card, by_slot, sections, offset
    pp = per_page(st)
    assigned = db.execute(
        """SELECT ss.*, s.name, s.printed_size, s.cards_loaded FROM storage_sets ss JOIN sets s ON s.code=ss.set_code
           WHERE storage_id=? ORDER BY position""", (st["id"],)).fetchall()
    for a in assigned:
        if not a["cards_loaded"]:
            try:
                ensure_set_cards(db, a["set_code"])
            except ApiError:
                pass  # offline: lay out with the cards we already have
        cards = db.execute("SELECT id, collector_number, booster FROM cards WHERE set_code=?",
                           (a["set_code"],)).fetchall()
        if not a["include_extras"]:
            # brand-new sets can have no booster data yet (every card false) - then the flag tells us nothing
            tagged = any(c["booster"] for c in cards)
            cards = [c for c in cards
                     if is_main(c["collector_number"], a["printed_size"], c["booster"] if tagged else None)]
        cards.sort(key=lambda c: cn_key(c["collector_number"]))
        if a["new_page"] and offset % pp:
            offset += pp - offset % pp
        start = offset
        for c in cards:
            by_card[c["id"]] = offset
            by_slot[offset] = c["id"]
            offset += 1
        sections.append({"set_code": a["set_code"], "name": a["name"], "start": start,
                         "end": offset - 1, "count": len(cards)})
    return by_card, by_slot, sections, offset


def first_free(taken, start, cap, reserved=()):
    s = start
    while cap is not None and s < cap:
        if s not in taken and s not in reserved:
            return s
        s += 1
    return None


def taken_slots(db, sid):
    return {r[0]: r[1] for r in db.execute(
        "SELECT slot, card_id FROM items WHERE storage_id=? AND slot IS NOT NULL", (sid,))}


def box_count(db, sid):
    return db.execute("SELECT COALESCE(SUM(qty),0) FROM items WHERE storage_id=?", (sid,)).fetchone()[0]


def place_in(db, st, card, notes):
    """Where would this card go in this storage? Returns dict or None (reason added to notes)."""
    if st["kind"] == "box":
        cap = st["capacity"]
        if cap:
            n = box_count(db, st["id"])
            if n >= cap:
                notes.append(f'{st["name"]} is full ({n}/{cap}).')
                return None
        return {"section": card["set_name"] or card["set_code"].upper(), "reason": "Box section for this set"}
    cap = capacity(st)
    taken = taken_slots(db, st["id"])
    if st["mode"] == "set_order":
        by_card, by_slot, _, end = binder_layout(db, st)
        s = by_card.get(card["id"])
        if s is not None:
            if s >= cap:
                l = slot_loc(st, s)
                notes.append(f'{st["name"]}: this card belongs on page {l["page"]}, '
                             f'but the binder only has {page_count(st)} pages.')
                return None
            if s not in taken:
                return {"slot": s, "reason": "Its own pocket (set order by collector number)"}
            if taken[s] == card["id"]:
                notes.append(f"{loc_text(st, s)} already holds a copy of this card.")
                return None
        s = first_free(taken, end, cap, by_slot)
        if s is None:
            notes.append(f'{st["name"]} has no free pocket left.')
            return None
        return {"slot": s, "reason": "Next free pocket after the set pages"}
    s = first_free(taken, 0, cap)
    if s is None:
        notes.append(f'{st["name"]} is full.')
        return None
    return {"slot": s, "reason": "Next empty pocket"}


def relayout(db, st):
    """After a set-order binder's sets change, recompute pockets. Returns the physical moves to make."""
    if st["kind"] != "binder" or st["mode"] != "set_order":
        return []
    by_card, by_slot, _, end = binder_layout(db, st)
    cap = capacity(st)
    items = [dict(r) for r in db.execute(
        """SELECT i.id, i.card_id, i.slot, c.name, c.set_code, c.collector_number
           FROM items i JOIN cards c ON c.id=i.card_id WHERE storage_id=? ORDER BY i.id""", (st["id"],))]
    new, taken = {}, set()
    for it in items:  # cards that have their own pocket
        s = by_card.get(it["card_id"])
        if s is not None and s < cap and s not in taken:
            new[it["id"]] = s
            taken.add(s)
    for it in items:  # others keep their pocket if it's not reserved
        if it["id"] in new:
            continue
        s = it["slot"]
        if s is not None and s < cap and s not in taken and s not in by_slot:
            new[it["id"]] = s
            taken.add(s)
    for it in items:  # the rest go to the first free pocket after the set pages
        if it["id"] in new:
            continue
        s = first_free(taken, end, cap, by_slot)
        if s is None:
            s = first_free(taken, 0, cap)
        new[it["id"]] = s
        if s is not None:
            taken.add(s)
    moves = []
    for it in items:
        s = new[it["id"]]
        if s != it["slot"]:
            db.execute("UPDATE items SET slot=? WHERE id=?", (s, it["id"]))
            moves.append({"name": it["name"], "set_code": it["set_code"], "collector_number": it["collector_number"],
                          "from": loc_text(st, it["slot"]) if it["slot"] is not None else "no pocket",
                          "to": loc_text(st, s) if s is not None else "no room left - take it out of this binder"})
    return moves


# ---------------------------------------------------------------- views

def get_storage(db, sid):
    st = db.execute("SELECT * FROM storages WHERE id=?", (sid,)).fetchone()
    if not st:
        raise ApiError(404, "Binder/box not found")
    return dict(st)


def storage_summary(db, st, detail=False):
    st = dict(st)
    st["sets"] = [dict(r) for r in db.execute(
        """SELECT ss.set_code, ss.new_page, ss.include_extras, ss.position, s.name, s.icon
           FROM storage_sets ss LEFT JOIN sets s ON s.code=ss.set_code WHERE storage_id=? ORDER BY position""",
        (st["id"],))]
    st["count"] = box_count(db, st["id"])
    st["capacity_total"] = capacity(st)
    if st["kind"] == "binder":
        st["page_count"] = page_count(st)
    if detail and st["kind"] == "binder" and st["mode"] == "set_order":
        _, by_slot, sections, end = binder_layout(db, st)
        taken = taken_slots(db, st["id"])
        pp = per_page(st)
        for sec in sections:
            sec["page_from"] = sec["start"] // pp + 1
            sec["page_to"] = max(sec["end"], sec["start"]) // pp + 1
            sec["owned"] = sum(1 for s in range(sec["start"], sec["end"] + 1) if taken.get(s) == by_slot.get(s))
        st["sections"] = sections
        st["layout_pages"] = -(-end // pp) if end else 0
    return st


ITEM_SQL = """SELECT i.*, c.name, c.set_code, c.collector_number, c.rarity, c.image, c.image_large, c.type_line,
  s.name AS set_name, st.name AS storage_name, st.kind, st.rows, st.cols, st.sheets, st.double_sided, st.color
  FROM items i JOIN cards c ON c.id=i.card_id LEFT JOIN sets s ON s.code=c.set_code
  JOIN storages st ON st.id=i.storage_id"""


def item_dict(r):
    d = dict(r)
    st = {"name": d["storage_name"], "kind": d["kind"], "rows": d["rows"], "cols": d["cols"],
          "sheets": d["sheets"], "double_sided": d["double_sided"]}
    d["loc"] = slot_loc(st, d["slot"]) if d["kind"] == "binder" and d["slot"] is not None else None
    d["location"] = loc_text(st, d["slot"], d["section"])
    return d


def item_view(db, iid):
    r = db.execute(ITEM_SQL + " WHERE i.id=?", (iid,)).fetchone()
    return item_dict(r) if r else None


def card_view(db, cid):
    r = db.execute("SELECT c.*, s.name AS set_name FROM cards c LEFT JOIN sets s ON s.code=c.set_code WHERE c.id=?",
                   (cid,)).fetchone()
    if not r:
        raise ApiError(404, "Unknown card - search for it first")
    return dict(r)


def suggest(db, card_id):
    card = card_view(db, card_id)
    sugg, notes = [], []
    for st in db.execute("SELECT * FROM storages ORDER BY id").fetchall():
        st = dict(st)
        codes = {r[0] for r in db.execute("SELECT set_code FROM storage_sets WHERE storage_id=?", (st["id"],))}
        if codes and card["set_code"] not in codes:
            continue
        general = not codes
        p = place_in(db, st, card, notes)
        if not p:
            continue
        slot = p.get("slot")
        sugg.append({"storage_id": st["id"], "storage_name": st["name"], "kind": st["kind"], "color": st["color"],
                     "slot": slot, "section": p.get("section"), "reason": p["reason"], "general": general,
                     "rows": st["rows"], "cols": st["cols"],
                     "loc": slot_loc(st, slot) if slot is not None else None,
                     "text": loc_text(st, slot, p.get("section")),
                     "rank": (0 if st["kind"] == "binder" else 1) + (2 if general else 0)})
    sugg.sort(key=lambda x: (x["rank"], x["storage_id"]))
    owned = [item_dict(r) for r in db.execute(ITEM_SQL + " WHERE i.card_id=? ORDER BY i.id", (card_id,))]
    if not db.execute("SELECT 1 FROM storages LIMIT 1").fetchone():
        notes.append("You have no binders or boxes yet - create one in the Storage tab.")
    elif not sugg:
        notes.append("No binder or box is assigned to this set and none is general-purpose. "
                     "Assign the set to a binder/box, or pick a place manually below.")
    return {"card": card, "suggestions": sugg, "notes": notes, "owned": owned}


# ---------------------------------------------------------------- routes

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def as_int(v, name, lo=None, hi=None, default=None):
    if v in (None, ""):
        if default is not None:
            return default
        raise ApiError(400, f"{name} is required")
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ApiError(400, f"{name} must be a number")
    if (lo is not None and v < lo) or (hi is not None and v > hi):
        raise ApiError(400, f"{name} must be between {lo} and {hi}")
    return v


@route("GET", "/api/stats")
def r_stats(db, q, b):
    return {"cards": db.execute("SELECT COALESCE(SUM(qty),0) FROM items").fetchone()[0],
            "unique": db.execute("SELECT COUNT(DISTINCT card_id) FROM items").fetchone()[0],
            "storages": db.execute("SELECT COUNT(*) FROM storages").fetchone()[0]}


@route("GET", "/api/sets")
def r_sets(db, q, b):
    ensure_sets(db)
    text = (q.get("q") or "").strip()
    rows = db.execute(
        """SELECT code,name,released_at,card_count,printed_size,set_type,icon FROM sets
           WHERE (?='' OR name LIKE ? OR code LIKE ?) ORDER BY (code=?) DESC, released_at DESC LIMIT 40""",
        (text, f"%{text}%", f"{text}%", text.lower()))
    return [dict(r) for r in rows]


@route("POST", "/api/sets/refresh")
def r_sets_refresh(db, q, b):
    ensure_sets(db, force=True)
    db.execute("UPDATE sets SET cards_loaded=0")  # card lists reload next time they're needed
    for (sid,) in db.execute(
            "SELECT DISTINCT ss.storage_id FROM storage_sets ss JOIN storages s ON s.id=ss.storage_id "
            "WHERE s.kind='binder' AND s.mode='set_order'").fetchall():
        for (code,) in db.execute("SELECT set_code FROM storage_sets WHERE storage_id=?", (sid,)).fetchall():
            ensure_set_cards(db, code)
    return {"ok": True, "sets": db.execute("SELECT COUNT(*) FROM sets").fetchone()[0]}


@route("GET", "/api/cards/search")
def r_card_search(db, q, b):
    text = (q.get("q") or "").strip()
    setc = (q.get("set") or "").strip().lower()
    if not text and not setc:
        return []
    cards = None
    m = re.match(r"^([a-z0-9]{2,6})\s+(\d+\S*)$", text, re.I)
    if m and not setc:  # "blb 123" -> exact printing
        c = sf_get(f"/cards/{m.group(1).lower()}/{urllib.parse.quote(m.group(2).lower())}")
        if c:
            cards = [c]
    if cards is None:
        qq = (text + (f" e:{setc}" if setc else "")).strip()
        data = sf_get("/cards/search", {"q": qq, "unique": "prints", "order": "set" if setc else "released",
                                        "dir": "asc" if setc else "desc", "include_extras": "true"})
        cards = (data or {}).get("data", [])
    cards = [c for c in cards if not c.get("digital")][:120]
    save_cards(db, cards)
    ensure_sets(db)
    ids = [c["id"] for c in cards]
    owned = {}
    if ids:
        owned = {r[0]: r[1] for r in db.execute(
            f"SELECT card_id, SUM(qty) FROM items WHERE card_id IN ({','.join('?' * len(ids))}) GROUP BY card_id", ids)}
    out = []
    for cid in ids:
        d = card_view(db, cid)
        d["owned"] = owned.get(cid, 0)
        out.append(d)
    return out


@route("POST", "/api/suggest")
def r_suggest(db, q, b):
    return suggest(db, b.get("card_id"))


@route("GET", "/api/storages")
def r_storages(db, q, b):
    return [storage_summary(db, st) for st in db.execute("SELECT * FROM storages ORDER BY kind, name")]


@route("GET", r"/api/storages/(\d+)")
def r_storage(db, q, b, sid):
    return storage_summary(db, get_storage(db, int(sid)), detail=True)


def clean_storage(b, existing=None):
    e = existing or {}
    kind = b.get("kind", e.get("kind"))
    if kind not in ("binder", "box"):
        raise ApiError(400, "Type must be binder or box")
    name = (b.get("name", e.get("name")) or "").strip()
    if not name:
        raise ApiError(400, "Give it a name")
    d = {"name": name, "kind": kind, "color": b.get("color", e.get("color")) or None,
         "notes": b.get("notes", e.get("notes")) or None}
    if kind == "binder":
        d["sheets"] = as_int(b.get("sheets", e.get("sheets")), "Sheets", 1, 500)
        d["rows"] = as_int(b.get("rows", e.get("rows")), "Rows", 1, 8)
        d["cols"] = as_int(b.get("cols", e.get("cols")), "Columns", 1, 8)
        d["double_sided"] = 1 if b.get("double_sided", e.get("double_sided", 1)) else 0
        d["mode"] = b.get("mode", e.get("mode")) or "set_order"
        if d["mode"] not in ("set_order", "sequential"):
            raise ApiError(400, "Unknown filling mode")
        d["capacity"] = None
    else:
        cap = b.get("capacity", e.get("capacity"))
        d["capacity"] = as_int(cap, "Capacity", 1, 100000) if cap not in (None, "") else None
        d.update(sheets=None, rows=None, cols=None, double_sided=0, mode="sequential")
    return d


@route("POST", "/api/storages")
def r_storage_create(db, q, b):
    d = clean_storage(b)
    cur = db.execute(
        "INSERT INTO storages(name,kind,sheets,rows,cols,double_sided,mode,capacity,color,notes) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (d["name"], d["kind"], d["sheets"], d["rows"], d["cols"], d["double_sided"], d["mode"], d["capacity"],
         d["color"], d["notes"]))
    return storage_summary(db, get_storage(db, cur.lastrowid), detail=True)


@route("PUT", r"/api/storages/(\d+)")
def r_storage_update(db, q, b, sid):
    old = get_storage(db, int(sid))
    b = dict(b)
    b["kind"] = old["kind"]  # type can't change
    d = clean_storage(b, old)
    has_items = box_count(db, old["id"]) > 0
    for f in ("rows", "cols", "double_sided", "mode"):
        if has_items and d[f] != old[f]:
            raise ApiError(409, "Pocket grid, sides and filling mode can't change while the binder has cards in it.")
    if old["kind"] == "box" and d["capacity"] and box_count(db, old["id"]) > d["capacity"]:
        raise ApiError(409, f'{old["name"]} already holds {box_count(db, old["id"])} cards - '
                            f'capacity can\'t be lower than that.')
    if old["kind"] == "binder" and d["mode"] == "sequential":
        cap = capacity({**old, **d})
        n = db.execute("SELECT COUNT(*) FROM items WHERE storage_id=? AND slot>=?", (old["id"], cap)).fetchone()[0]
        if n:
            raise ApiError(409, f"{n} card(s) sit on pages that would no longer exist. Move them out first.")
    db.execute("UPDATE storages SET name=?,sheets=?,rows=?,cols=?,double_sided=?,mode=?,capacity=?,color=?,notes=? WHERE id=?",
               (d["name"], d["sheets"], d["rows"], d["cols"], d["double_sided"], d["mode"], d["capacity"],
                d["color"], d["notes"], old["id"]))
    st = get_storage(db, old["id"])
    if st["kind"] == "binder" and st["mode"] == "set_order":
        for (code,) in db.execute("SELECT set_code FROM storage_sets WHERE storage_id=?", (st["id"],)).fetchall():
            ensure_set_cards(db, code)
    moves = relayout(db, st)
    return {"storage": storage_summary(db, st, detail=True), "moves": moves}


@route("DELETE", r"/api/storages/(\d+)")
def r_storage_delete(db, q, b, sid):
    st = get_storage(db, int(sid))
    n = box_count(db, st["id"])
    if n:
        raise ApiError(409, f"{st['name']} still holds {n} card(s). Move or remove them first.")
    db.execute("DELETE FROM storage_sets WHERE storage_id=?", (st["id"],))
    db.execute("DELETE FROM storages WHERE id=?", (st["id"],))
    return {"ok": True}


@route("POST", r"/api/storages/(\d+)/sets")
def r_assign(db, q, b, sid):
    st = get_storage(db, int(sid))
    code = (b.get("set_code") or "").strip().lower()
    if not code:
        raise ApiError(400, "Choose a set")
    get_set(db, code)
    if db.execute("SELECT 1 FROM storage_sets WHERE storage_id=? AND set_code=?", (st["id"], code)).fetchone():
        raise ApiError(409, "That set is already assigned here")
    if st["kind"] == "binder" and st["mode"] == "set_order":
        ensure_set_cards(db, code)
    pos = db.execute("SELECT COALESCE(MAX(position),0)+1 FROM storage_sets WHERE storage_id=?", (st["id"],)).fetchone()[0]
    db.execute("INSERT INTO storage_sets(storage_id,set_code,position,new_page,include_extras) VALUES(?,?,?,?,?)",
               (st["id"], code, pos, 1 if b.get("new_page", True) else 0, 1 if b.get("include_extras") else 0))
    moves = relayout(db, st)
    return {"storage": storage_summary(db, st, detail=True), "moves": moves}


@route("PUT", r"/api/storages/(\d+)/sets/([a-z0-9]+)")
def r_assign_update(db, q, b, sid, code):
    st = get_storage(db, int(sid))
    for f in ("new_page", "include_extras"):
        if f in b:
            db.execute(f"UPDATE storage_sets SET {f}=? WHERE storage_id=? AND set_code=?",
                       (1 if b[f] else 0, st["id"], code))
    moves = relayout(db, st)
    return {"storage": storage_summary(db, st, detail=True), "moves": moves}


@route("POST", r"/api/storages/(\d+)/sets/([a-z0-9]+)/move")
def r_assign_move(db, q, b, sid, code):
    st = get_storage(db, int(sid))
    order = [r[0] for r in db.execute("SELECT set_code FROM storage_sets WHERE storage_id=? ORDER BY position", (st["id"],))]
    if code not in order:
        raise ApiError(404, "Set not assigned here")
    i = order.index(code)
    j = i + (1 if int(b.get("dir", 1)) > 0 else -1)
    if 0 <= j < len(order):
        order[i], order[j] = order[j], order[i]
        for pos, c in enumerate(order, 1):
            db.execute("UPDATE storage_sets SET position=? WHERE storage_id=? AND set_code=?", (pos, st["id"], c))
    moves = relayout(db, st)
    return {"storage": storage_summary(db, st, detail=True), "moves": moves}


@route("DELETE", r"/api/storages/(\d+)/sets/([a-z0-9]+)")
def r_unassign(db, q, b, sid, code):
    st = get_storage(db, int(sid))
    db.execute("DELETE FROM storage_sets WHERE storage_id=? AND set_code=?", (st["id"], code))
    moves = relayout(db, st)
    return {"storage": storage_summary(db, st, detail=True), "moves": moves}


@route("GET", r"/api/storages/(\d+)/page")
def r_page(db, q, b, sid):
    st = get_storage(db, int(sid))
    if st["kind"] != "binder":
        raise ApiError(400, "Only binders have pages")
    pc = page_count(st)
    n = max(1, min(as_int(q.get("n"), "Page", default=1), pc))
    pp = per_page(st)
    start = (n - 1) * pp
    _, by_slot, sections, _ = binder_layout(db, st)
    occ = {r["slot"]: item_dict(r) for r in db.execute(
        ITEM_SQL + " WHERE i.storage_id=? AND i.slot BETWEEN ? AND ?", (st["id"], start, start + pp - 1))}
    exp_ids = [by_slot[s] for s in range(start, start + pp) if s in by_slot]
    exp = {}
    if exp_ids:
        exp = {r["id"]: dict(r) for r in db.execute(
            f"SELECT c.*, s.name AS set_name FROM cards c LEFT JOIN sets s ON s.code=c.set_code "
            f"WHERE c.id IN ({','.join('?' * len(exp_ids))})", exp_ids)}
    slots = []
    for s in range(start, start + pp):
        l = slot_loc(st, s)
        e = {"slot": s, "row": l["row"], "col": l["col"]}
        if s in occ:
            e["item"] = occ[s]
        if s in by_slot:
            e["expected"] = exp.get(by_slot[s])
        slots.append(e)
    page_sections = [x["name"] for x in sections if x["count"] and x["start"] < start + pp and x["end"] >= start]
    l = slot_loc(st, start)
    return {"page": n, "page_count": pc, "sheet": l["sheet"], "side": l["side"], "slots": slots,
            "sections": page_sections}


@route("GET", "/api/items")
def r_items(db, q, b):
    where, args = [], []
    text = (q.get("q") or "").strip()
    if text:
        m = re.match(r"^([a-z0-9]{2,6})\s+(\S+)$", text, re.I)
        if m:
            where.append("(c.name LIKE ? OR (c.set_code=? AND c.collector_number=?))")
            args += [f"%{text}%", m.group(1).lower(), m.group(2)]
        else:
            where.append("c.name LIKE ?")
            args.append(f"%{text}%")
    if q.get("storage"):
        where.append("i.storage_id=?")
        args.append(int(q["storage"]))
    if q.get("set"):
        where.append("c.set_code=?")
        args.append(q["set"].lower())
    sql = ITEM_SQL + (" WHERE " + " AND ".join(where) if where else "")
    if q.get("storage"):
        sql += " ORDER BY i.section, c.set_code, CAST(c.collector_number AS INT), c.name"
    elif text:
        sql += " ORDER BY c.name, i.id"
    else:
        sql += " ORDER BY i.id DESC"
    limit = 2000 if q.get("storage") else 300
    return [item_dict(r) for r in db.execute(sql + f" LIMIT {limit}", args)]


@route("POST", "/api/items")
def r_item_add(db, q, b):
    card = card_view(db, b.get("card_id"))
    st = get_storage(db, as_int(b.get("storage_id"), "Storage"))
    foil = 1 if b.get("foil") else 0
    cond = b.get("condition") or "NM"
    lang = b.get("lang") or "en"
    qty = as_int(b.get("qty"), "Quantity", 1, 999, default=1)
    if st["kind"] == "binder":
        slot = b.get("slot")
        if slot in (None, "", "auto"):
            notes = []
            p = place_in(db, st, card, notes)
            if not p:
                raise ApiError(409, " ".join(notes) or "No free pocket")
            slot = p["slot"]
        else:
            slot = as_int(slot, "Pocket", 0, capacity(st) - 1)
            if db.execute("SELECT 1 FROM items WHERE storage_id=? AND slot=?", (st["id"], slot)).fetchone():
                raise ApiError(409, "That pocket is already filled")
        cur = db.execute("INSERT INTO items(card_id,storage_id,slot,foil,condition,lang,qty) VALUES(?,?,?,?,?,?,1)",
                         (card["id"], st["id"], slot, foil, cond, lang))
        iid = cur.lastrowid
    else:
        if st["capacity"] and box_count(db, st["id"]) + qty > st["capacity"]:
            raise ApiError(409, f'{st["name"]} would go over its capacity ({st["capacity"]}).')
        section = (b.get("section") or "").strip() or card["set_name"] or card["set_code"].upper()
        same = db.execute("""SELECT id FROM items WHERE storage_id=? AND card_id=? AND foil=? AND condition=? AND lang=?
                             AND COALESCE(section,'')=?""", (st["id"], card["id"], foil, cond, lang, section)).fetchone()
        if same:
            db.execute("UPDATE items SET qty=qty+? WHERE id=?", (qty, same[0]))
            iid = same[0]
        else:
            iid = db.execute("INSERT INTO items(card_id,storage_id,section,foil,condition,lang,qty) VALUES(?,?,?,?,?,?,?)",
                             (card["id"], st["id"], section, foil, cond, lang, qty)).lastrowid
    return {"item": item_view(db, iid)}


@route("PUT", r"/api/items/(\d+)")
def r_item_move(db, q, b, iid):
    it = db.execute("SELECT * FROM items WHERE id=?", (int(iid),)).fetchone()
    if not it:
        raise ApiError(404, "Card not found in collection")
    st = get_storage(db, as_int(b.get("storage_id"), "Storage"))
    card = card_view(db, it["card_id"])
    if st["kind"] == "binder":
        if it["qty"] > 1:  # split one copy off
            db.execute("UPDATE items SET qty=qty-1 WHERE id=?", (it["id"],))
            new_id = db.execute("INSERT INTO items(card_id,storage_id,foil,condition,lang,qty) VALUES(?,?,?,?,?,1)",
                                (it["card_id"], st["id"], it["foil"], it["condition"], it["lang"])).lastrowid
        else:
            new_id = it["id"]
            db.execute("UPDATE items SET slot=NULL, section=NULL, storage_id=? WHERE id=?", (st["id"], new_id))
        slot = b.get("slot")
        if slot in (None, "", "auto"):
            notes = []
            p = place_in(db, st, card, notes)
            if not p:
                raise ApiError(409, " ".join(notes) or "No free pocket")
            slot = p["slot"]
        else:
            slot = as_int(slot, "Pocket", 0, capacity(st) - 1)
            if db.execute("SELECT 1 FROM items WHERE storage_id=? AND slot=?", (st["id"], slot)).fetchone():
                raise ApiError(409, "That pocket is already filled")
        db.execute("UPDATE items SET slot=? WHERE id=?", (slot, new_id))
    else:
        if st["id"] == it["storage_id"]:
            raise ApiError(409, "It's already in that box")
        if st["capacity"] and box_count(db, st["id"]) + it["qty"] > st["capacity"]:
            raise ApiError(409, f'{st["name"]} would go over its capacity ({st["capacity"]}).')
        section = (b.get("section") or "").strip() or card["set_name"] or card["set_code"].upper()
        same = db.execute("""SELECT id FROM items WHERE storage_id=? AND card_id=? AND foil=? AND condition=? AND lang=?
                             AND COALESCE(section,'')=?""",
                          (st["id"], it["card_id"], it["foil"], it["condition"], it["lang"], section)).fetchone()
        if same:  # join the identical stack already in that box
            db.execute("UPDATE items SET qty=qty+? WHERE id=?", (it["qty"], same[0]))
            db.execute("DELETE FROM items WHERE id=?", (it["id"],))
            new_id = same[0]
        else:
            db.execute("UPDATE items SET storage_id=?, slot=NULL, section=? WHERE id=?", (st["id"], section, it["id"]))
            new_id = it["id"]
    return {"item": item_view(db, new_id)}


@route("DELETE", r"/api/items/(\d+)")
def r_item_delete(db, q, b, iid):
    it = db.execute("SELECT * FROM items WHERE id=?", (int(iid),)).fetchone()
    if not it:
        raise ApiError(404, "Card not found in collection")
    if q.get("one") and it["qty"] > 1:
        db.execute("UPDATE items SET qty=qty-1 WHERE id=?", (it["id"],))
    else:
        db.execute("DELETE FROM items WHERE id=?", (it["id"],))
    return {"ok": True}


@route("GET", "/api/export.csv")
def r_export(db, q, b):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "set", "collector_number", "foil", "condition", "language", "quantity", "storage",
                "page", "row", "column", "section", "scryfall_id"])
    for r in db.execute(ITEM_SQL + " ORDER BY st.name, i.slot, c.name"):
        d = item_dict(r)
        l = d["loc"] or {}
        w.writerow([d["name"], d["set_code"].upper(), d["collector_number"], "yes" if d["foil"] else "",
                    d["condition"], d["lang"], d["qty"], d["storage_name"], l.get("page", ""), l.get("row", ""),
                    l.get("col", ""), d["section"] or "", d["card_id"]])
    return ("raw", buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8",
            {"Content-Disposition": 'attachment; filename="card-library.csv"'})


# ---------------------------------------------------------------- HTTP plumbing

class Handler(BaseHTTPRequestHandler):
    server_version = "CardLibrary/1.0"

    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", headers=None):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(data)

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.normpath(os.path.join(STATIC, path.lstrip("/")))
        if not full.startswith(STATIC) or not os.path.isfile(full):
            return self._send(404, {"error": "Not found"})
        ext = os.path.splitext(full)[1]
        ctype = {".html": "text/html; charset=utf-8", ".js": "text/javascript", ".css": "text/css",
                 ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon"}.get(ext, "application/octet-stream")
        with open(full, "rb") as f:
            self._send(200, f.read(), ctype)

    def _handle(self, method):
        u = urllib.parse.urlparse(self.path)
        if method == "GET" and not u.path.startswith("/api/"):
            return self._static(u.path)
        for m, rx, fn in ROUTES:
            mm = rx.match(u.path)
            if m != method or not mm:
                continue
            q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
            body = {}
            n = int(self.headers.get("Content-Length") or 0)
            if n:
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                except ValueError:
                    return self._send(400, {"error": "Bad JSON"})
            db = connect()
            try:
                res = fn(db, q, body, *mm.groups())
                db.commit()
            except ApiError as e:
                db.rollback()
                return self._send(e.status, {"error": e.msg})
            except Exception as e:  # noqa
                db.rollback()
                traceback.print_exc()
                return self._send(500, {"error": f"Server error: {e}"})
            finally:
                db.close()
            if isinstance(res, tuple) and res and res[0] == "raw":
                return self._send(200, res[1], res[2], res[3])
            return self._send(200, res)
        self._send(404, {"error": "Not found"})

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")


def main():
    init_db()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"Card Library is running at {url}   (press Ctrl+C to stop)")
    print(f"Database: {DB_PATH}")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


if __name__ == "__main__":
    main()

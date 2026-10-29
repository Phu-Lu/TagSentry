"""SQLite data layer for the RFID gate demo.

Single source of truth for assets, locations, orders and events.
Current location of an asset = its latest event. Movement history = all its events.
"""
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

DB_PATH = os.environ.get("GATE_DB", "gate_demo.db")
_lock = threading.RLock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL DEFAULT 'WAREHOUSE'      -- WAREHOUSE | GATE | DEPT
);
CREATE TABLE IF NOT EXISTS assets (
    epc TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    color TEXT NOT NULL DEFAULT '',
    size TEXT NOT NULL DEFAULT '',
    home_location_id INTEGER REFERENCES locations(id),
    created_at TEXT NOT NULL,
    asset_code TEXT NOT NULL DEFAULT '',          -- the customer's own asset ID
    serial_number TEXT NOT NULL DEFAULT '',
    extra TEXT NOT NULL DEFAULT '{}'              -- any other imported columns, as JSON
);
CREATE TABLE IF NOT EXISTS asset_images (
    epc TEXT PRIMARY KEY REFERENCES assets(epc) ON DELETE CASCADE,
    mime TEXT NOT NULL,
    data BLOB NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,                          -- IN | OUT
    status TEXT NOT NULL DEFAULT 'OPEN',         -- OPEN | COMPLETED | DISCREPANCY
    reference TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    committed_at TEXT
);
CREATE TABLE IF NOT EXISTS order_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,                          -- CATEGORY | EPC
    category TEXT,
    epc TEXT,
    qty INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    epc TEXT NOT NULL,
    antenna INTEGER,
    rssi REAL,
    direction TEXT NOT NULL DEFAULT '',          -- In | Out | ''
    location_id INTEGER REFERENCES locations(id),
    order_id INTEGER REFERENCES orders(id),
    result TEXT NOT NULL DEFAULT ''              -- OK | UNKNOWN | UNEXPECTED | OVER | WRONG_LOCATION
);
CREATE INDEX IF NOT EXISTS idx_events_epc ON events(epc, id);
CREATE INDEX IF NOT EXISTS idx_events_order ON events(order_id);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE VIEW IF NOT EXISTS last_seen AS
    SELECT e.epc, e.location_id, e.ts, e.direction
    FROM events e
    WHERE e.id = (SELECT MAX(id) FROM events WHERE epc = e.epc);
"""

DEFAULT_SETTINGS = {
    "min_rssi": "-100",           # drop reads weaker than this (dBm)
    "event_interval_sec": "30",   # re-log a stationary tag at most this often
    "reader_location_id": "",     # where the reader is currently placed
    "active_order_id": "",        # order the gate is currently scanning against
}


def _now():
    return datetime.now()


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def norm_epc(epc):
    return str(epc or "").strip().upper()


@contextmanager
def conn():
    with _lock:
        c = sqlite3.connect(DB_PATH)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()


def init_db():
    with conn() as c:
        c.executescript(SCHEMA)
        have = {r["name"] for r in c.execute("PRAGMA table_info(assets)")}   # upgrade databases made by older versions
        for col, ddl in (("asset_code", "TEXT NOT NULL DEFAULT ''"), ("serial_number", "TEXT NOT NULL DEFAULT ''"),
                         ("extra", "TEXT NOT NULL DEFAULT '{}'")):
            if col not in have:
                c.execute(f"ALTER TABLE assets ADD COLUMN {col} {ddl}")
        for k, v in DEFAULT_SETTINGS.items():
            c.execute("INSERT OR IGNORE INTO settings(key, value) VALUES (?, ?)", (k, v))


# ---------------------------------------------------------------- settings
def get_setting(key, default=None):
    with conn() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r and r["value"] not in (None, "") else default


def set_setting(key, value):
    with conn() as c:
        c.execute("INSERT INTO settings(key, value) VALUES (?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                  (key, "" if value is None else str(value)))


def _int_setting(key):
    v = get_setting(key)
    return int(v) if v not in (None, "") else None


# --------------------------------------------------------------- locations
def add_location(name, type_="WAREHOUSE"):
    with conn() as c:
        cur = c.execute("INSERT INTO locations(name, type) VALUES (?, ?)", (name.strip(), type_.upper()))
        return cur.lastrowid


def list_locations():
    with conn() as c:
        rows = c.execute("""
            SELECT l.*,
              (SELECT COUNT(*) FROM assets a WHERE a.home_location_id = l.id) AS home_count,
              (SELECT COUNT(*) FROM last_seen s WHERE s.location_id = l.id
                 AND s.epc IN (SELECT epc FROM assets)) AS current_count
            FROM locations l ORDER BY l.id""").fetchall()
    return [dict(r) for r in rows]


def set_reader_location(location_id):
    set_setting("reader_location_id", location_id)


# ------------------------------------------------------------------ assets
_ASSET_SELECT = """
    SELECT a.*, hl.name AS home_name, cl.name AS current_name, s.ts AS last_seen_ts,
      (SELECT updated_at FROM asset_images i WHERE i.epc = a.epc) AS image_v,
      CASE WHEN s.epc IS NULL THEN 'UNSEEN'
           WHEN a.home_location_id IS NULL THEN 'NO_HOME'
           WHEN s.location_id = a.home_location_id THEN 'RIGHT'
           ELSE 'WRONG' END AS status
    FROM assets a
    LEFT JOIN locations hl ON hl.id = a.home_location_id
    LEFT JOIN last_seen s ON s.epc = a.epc
    LEFT JOIN locations cl ON cl.id = s.location_id
"""


def register_asset(epc, name, category="", home_location_id=None, color="", size="", asset_code="", serial_number=""):
    epc = norm_epc(epc)
    if not epc or not name.strip():
        raise ValueError("EPC and name are required")
    with conn() as c:
        c.execute("INSERT INTO assets(epc, name, category, color, size, home_location_id, created_at, asset_code, serial_number) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (epc, name.strip(), category.strip(), color, size, home_location_id, _iso(_now()),
                   asset_code.strip(), serial_number.strip()))
    return epc


def update_asset(epc, **fields):
    allowed = {"name", "category", "color", "size", "home_location_id", "asset_code", "serial_number"}
    sets = {k: v for k, v in fields.items() if k in allowed}
    if not sets:
        return
    with conn() as c:
        c.execute(f"UPDATE assets SET {', '.join(k + '=?' for k in sets)} WHERE epc=?",
                  (*sets.values(), norm_epc(epc)))


def set_asset_image(epc, mime, data):
    epc = norm_epc(epc)
    with conn() as c:
        if not c.execute("SELECT 1 FROM assets WHERE epc=?", (epc,)).fetchone():
            raise ValueError("Unknown asset")
        c.execute("INSERT INTO asset_images(epc, mime, data, updated_at) VALUES (?,?,?,?) "
                  "ON CONFLICT(epc) DO UPDATE SET mime=excluded.mime, data=excluded.data, updated_at=excluded.updated_at",
                  (epc, mime, data, datetime.now().isoformat(timespec="microseconds")))


def get_asset_image(epc):
    with conn() as c:
        r = c.execute("SELECT mime, data FROM asset_images WHERE epc=?", (norm_epc(epc),)).fetchone()
    return dict(r) if r else None


def delete_asset_image(epc):
    with conn() as c:
        c.execute("DELETE FROM asset_images WHERE epc=?", (norm_epc(epc),))


def list_assets(search=""):
    q = _ASSET_SELECT
    args = ()
    if search:
        q += " WHERE a.epc LIKE ? OR a.name LIKE ? OR a.category LIKE ? OR a.asset_code LIKE ? OR a.serial_number LIKE ?"
        like = f"%{search.strip()}%"
        args = (like.upper(), like, like, like, like)
    with conn() as c:
        out = [dict(r) for r in c.execute(q + " ORDER BY a.created_at DESC", args)]
    for a in out:
        try:
            a["extra"] = json.loads(a.get("extra") or "{}")
        except ValueError:
            a["extra"] = {}
    return out


def asset_names():
    """{epc: (name, category)} for quick lookups in the live view."""
    with conn() as c:
        return {r["epc"]: (r["name"], r["category"]) for r in c.execute("SELECT epc, name, category FROM assets")}


def reset_data(scope):
    """Delete demo data. 'events' clears reads and history; 'all' also clears assets, orders and locations."""
    if scope not in ("events", "all"):
        raise ValueError("scope must be 'events' or 'all'")
    tables = ["events"] if scope == "events" else \
        ["events", "order_lines", "orders", "asset_images", "assets", "locations"]
    with conn() as c:
        for t in tables:
            c.execute(f"DELETE FROM {t}")
        if scope == "all":
            c.execute("DELETE FROM sqlite_sequence WHERE name IN ('events','order_lines','orders','locations')")
    if scope == "all":
        set_setting("reader_location_id", None)
        set_setting("active_order_id", None)


def unregistered_tags():
    with conn() as c:
        rows = c.execute("""
            SELECT e.epc, e.rssi, e.ts AS last_seen_ts, e.antenna,
                   (SELECT COUNT(*) FROM events x WHERE x.epc = e.epc) AS reads
            FROM events e
            WHERE e.epc NOT IN (SELECT epc FROM assets)
              AND e.id = (SELECT MAX(id) FROM events WHERE epc = e.epc)
            ORDER BY e.id DESC""").fetchall()
    return [dict(r) for r in rows]


# ------------------------------------------------------------------ orders
def create_order(code, type_, reference=""):
    type_ = type_.upper()
    if type_ not in ("IN", "OUT"):
        raise ValueError("type must be IN or OUT")
    with conn() as c:
        cur = c.execute("INSERT INTO orders(code, type, reference, created_at) VALUES (?,?,?,?)",
                        (code.strip(), type_, reference, _iso(_now())))
        return cur.lastrowid


def add_line(order_id, kind, qty=1, category=None, epc=None):
    kind = kind.upper()
    with conn() as c:
        o = c.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o or o["status"] != "OPEN":
            raise ValueError("order is not open")
        if kind == "CATEGORY":
            category = (category or "").strip()
            if not category or qty < 1:
                raise ValueError("category and qty >= 1 required")
            ex = c.execute("SELECT id FROM order_lines WHERE order_id=? AND kind='CATEGORY' AND category=?",
                           (order_id, category)).fetchone()
            if ex:   # one line per category: merge quantities
                c.execute("UPDATE order_lines SET qty = qty + ? WHERE id=?", (qty, ex["id"]))
                return ex["id"]
            cur = c.execute("INSERT INTO order_lines(order_id, kind, category, qty) VALUES (?,?,?,?)",
                            (order_id, kind, category, qty))
        elif kind == "EPC":
            epc = norm_epc(epc)
            if not epc:
                raise ValueError("epc required")
            if c.execute("SELECT 1 FROM order_lines WHERE order_id=? AND kind='EPC' AND epc=?",
                         (order_id, epc)).fetchone():
                raise ValueError("EPC already on this order")
            cur = c.execute("INSERT INTO order_lines(order_id, kind, epc, qty) VALUES (?,?,?,1)",
                            (order_id, kind, epc))
        else:
            raise ValueError("kind must be CATEGORY or EPC")
        return cur.lastrowid


def delete_line(line_id):
    with conn() as c:
        c.execute("DELETE FROM order_lines WHERE id=? AND order_id IN "
                  "(SELECT id FROM orders WHERE status='OPEN')", (line_id,))


def set_active_order(order_id):
    """Select the order the gate scans against (None to stop)."""
    if order_id is not None:
        with conn() as c:
            o = c.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o or o["status"] != "OPEN":
            raise ValueError("order is not open")
    set_setting("active_order_id", order_id)


def _evaluate(c, order_id):
    """Reconcile the tags scanned in an order session against its lines.

    Rules per distinct tag, in order:
      1. EPC not registered                    -> UNKNOWN
      2. EPC listed on an EPC line             -> MATCHED
      3. asset category on a CATEGORY line     -> MATCHED, or OVER once the count exceeds qty
      4. anything else                         -> UNEXPECTED
    OUT orders ignore reads with direction 'In'; IN orders ignore 'Out'. Blank direction counts.
    """
    order = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not order:
        raise ValueError("order not found")
    lines = [dict(r) for r in c.execute("SELECT * FROM order_lines WHERE order_id=? ORDER BY id", (order_id,))]
    skip = "In" if order["type"] == "OUT" else "Out"
    tags = c.execute("""SELECT epc, MIN(id) AS first_id FROM events
                        WHERE order_id=? AND direction != ?
                        GROUP BY epc ORDER BY first_id""", (order_id, skip)).fetchall()
    epc_lines = {l["epc"]: l for l in lines if l["kind"] == "EPC"}
    cat_lines = {l["category"]: l for l in lines if l["kind"] == "CATEGORY"}
    for l in lines:
        l["expected"] = l["qty"]
        l["scanned"] = 0
    tag_status, tag_line, exceptions = {}, {}, []
    for t in tags:
        epc = t["epc"]
        a = c.execute("SELECT name, category FROM assets WHERE epc=?", (epc,)).fetchone()
        name = a["name"] if a else ""
        if a is None:
            st = "UNKNOWN"
        elif epc in epc_lines:
            epc_lines[epc]["scanned"] = 1
            tag_line[epc] = epc_lines[epc]["id"]
            st = "MATCHED"
        elif a["category"] in cat_lines:
            line = cat_lines[a["category"]]
            line["scanned"] += 1
            tag_line[epc] = line["id"]
            st = "MATCHED" if line["scanned"] <= line["expected"] else "OVER"
        else:
            st = "UNEXPECTED"
        tag_status[epc] = st
        if st != "MATCHED":
            exceptions.append({"epc": epc, "name": name, "reason": st})
    for l in lines:
        if l["scanned"] > l["expected"]:
            l["status"] = "OVER"
        elif l["scanned"] == l["expected"]:
            l["status"] = "DONE"
        else:
            l["status"] = "PENDING"
    expected_total = sum(l["expected"] for l in lines)
    matched_total = sum(min(l["scanned"], l["expected"]) for l in lines)
    return {
        "order": dict(order),
        "lines": lines,
        "tag_status": tag_status,
        "tag_line": tag_line,
        "exceptions": exceptions,
        "summary": {
            "total_tags": len(tags),
            "expected_total": expected_total,
            "matched_total": matched_total,
            "progress_pct": round(100 * matched_total / expected_total) if expected_total else 0,
            "exception_count": len(exceptions),
        },
    }


def evaluate_order(order_id):
    with conn() as c:
        return _evaluate(c, order_id)


def order_export_rows(order_id):
    """One row per scanned tag (with the line it counted against), then MISSING rows for what is still expected."""
    with conn() as c:
        ev = _evaluate(c, order_id)
        first = {r["epc"]: dict(r) for r in c.execute("""
            SELECT e.epc, e.ts, e.direction, e.antenna, e.rssi, l.name AS location_name
            FROM events e LEFT JOIN locations l ON l.id = e.location_id
            WHERE e.id IN (SELECT MIN(id) FROM events WHERE order_id=? GROUP BY epc)""", (order_id,))}
        info = {r["epc"]: (r["name"], r["category"]) for r in c.execute("SELECT epc, name, category FROM assets")}
    o, lines = ev["order"], {l["id"]: l for l in ev["lines"]}
    base = {"order": o["code"], "type": o["type"], "order_status": o["status"]}
    rows = []
    for epc, st in ev["tag_status"].items():
        f, ln = first.get(epc, {}), lines.get(ev["tag_line"].get(epc))
        name, cat = info.get(epc, ("", ""))
        rows.append({**base, "row_type": "SCANNED", "epc": epc, "name": name, "category": cat, "status": st,
                     "line": (ln["category"] if ln["kind"] == "CATEGORY" else "EPC line") if ln else "",
                     "line_expected": ln["expected"] if ln else "", "line_scanned": ln["scanned"] if ln else "",
                     "first_seen": f.get("ts", ""), "direction": f.get("direction", ""),
                     "antenna": f.get("antenna", ""), "rssi": f.get("rssi", ""), "location": f.get("location_name", "")})
    for l in ev["lines"]:
        if l["kind"] == "EPC" and l["scanned"] == 0:
            name, cat = info.get(l["epc"], ("", ""))
            rows.append({**base, "row_type": "MISSING", "epc": l["epc"], "name": name, "category": cat,
                         "status": "PENDING", "line": "EPC line", "line_expected": 1, "line_scanned": 0})
        elif l["kind"] == "CATEGORY" and l["scanned"] < l["expected"]:
            rows.append({**base, "row_type": "MISSING", "epc": "", "category": l["category"], "status": "PENDING",
                         "name": f"{l['expected'] - l['scanned']} more {l['category']} expected",
                         "line": l["category"], "line_expected": l["expected"], "line_scanned": l["scanned"]})
    return rows


def list_orders():
    with conn() as c:
        out = []
        for o in c.execute("SELECT id FROM orders ORDER BY id DESC").fetchall():
            ev = _evaluate(c, o["id"])
            out.append({**ev["order"], **ev["summary"]})
        return out


def commit_order(order_id, reference="", reason=""):
    with conn() as c:
        ev = _evaluate(c, order_id)
        if ev["order"]["status"] != "OPEN":
            raise ValueError("order already committed")
        clean = ev["summary"]["exception_count"] == 0 and all(l["status"] == "DONE" for l in ev["lines"])
        status = "COMPLETED" if clean else "DISCREPANCY"
        c.execute("UPDATE orders SET status=?, reference=?, reason=?, committed_at=? WHERE id=?",
                  (status, reference or ev["order"]["reference"], reason, _iso(_now()), order_id))
        ev["order"]["status"] = status
    if get_setting("active_order_id") == str(order_id):
        set_setting("active_order_id", None)
    return ev


# ------------------------------------------------------------------ ingest
def ingest_tag(epc, antenna=1, rssi=0, direction="", now=None):
    """Record one tag read. Returns the stored event dict, or None if filtered/deduped.

    Filters: RSSI below min_rssi is dropped. A tag is re-logged only when its
    location, direction or order changed, or event_interval_sec elapsed.
    """
    epc = norm_epc(epc)
    if not epc:
        return None
    try:
        rssi = float(rssi)
    except (TypeError, ValueError):
        rssi = 0.0
    if rssi < float(get_setting("min_rssi", "-100")):
        return None
    now = now or _now()
    direction = direction or ""
    loc_id = _int_setting("reader_location_id")
    order_id = _int_setting("active_order_id")
    interval = float(get_setting("event_interval_sec", "30"))
    with conn() as c:
        o = c.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone() if order_id else None
        if not o or o["status"] != "OPEN":
            order_id = None
        last = c.execute("SELECT * FROM events WHERE epc=? ORDER BY id DESC LIMIT 1", (epc,)).fetchone()
        if last is not None:
            same = (last["location_id"] == loc_id and last["direction"] == direction
                    and last["order_id"] == order_id)
            age = (now - datetime.fromisoformat(last["ts"])).total_seconds()
            if same and age < interval:
                return None
        cur = c.execute("INSERT INTO events(ts, epc, antenna, rssi, direction, location_id, order_id) "
                        "VALUES (?,?,?,?,?,?,?)", (_iso(now), epc, antenna, rssi, direction, loc_id, order_id))
        ev_id = cur.lastrowid
        asset = c.execute("SELECT * FROM assets WHERE epc=?", (epc,)).fetchone()
        if asset is None:
            result = "UNKNOWN"
        else:
            st = _evaluate(c, order_id)["tag_status"].get(epc) if order_id else None
            if st in ("UNEXPECTED", "OVER"):
                result = st
            elif st == "MATCHED":
                result = "OK"           # expected movement for this order
            elif asset["home_location_id"] and loc_id and asset["home_location_id"] != loc_id:
                result = "WRONG_LOCATION"
            else:
                result = "OK"
        c.execute("UPDATE events SET result=? WHERE id=?", (result, ev_id))
        return _event_row(c, ev_id)


def _event_row(c, ev_id):
    r = c.execute("""
        SELECT e.*, a.name, a.category, hl.name AS home_name, l.name AS location_name
        FROM events e
        LEFT JOIN assets a ON a.epc = e.epc
        LEFT JOIN locations hl ON hl.id = a.home_location_id
        LEFT JOIN locations l ON l.id = e.location_id
        WHERE e.id=?""", (ev_id,)).fetchone()
    return dict(r)


# ---------------------------------------------------------------- queries
def query_events(epc=None, location_id=None, result=None, limit=200):
    where, args = [], []
    if epc:
        where.append("(e.epc LIKE ? OR a.name LIKE ?)")
        args += [f"%{norm_epc(epc)}%", f"%{epc}%"]
    if location_id:
        where.append("e.location_id=?")
        args.append(location_id)
    if result:
        where.append("e.result=?")
        args.append(result)
    sql = """SELECT e.*, a.name, a.category, hl.name AS home_name, l.name AS location_name
             FROM events e
             LEFT JOIN assets a ON a.epc = e.epc
             LEFT JOIN locations hl ON hl.id = a.home_location_id
             LEFT JOIN locations l ON l.id = e.location_id"""
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY e.id DESC LIMIT ?"
    with conn() as c:
        return [dict(r) for r in c.execute(sql, (*args, limit))]


def asset_history(epc):
    """Movement history of one asset, oldest first."""
    with conn() as c:
        rows = c.execute("""
            SELECT e.ts, e.direction, e.result, e.antenna, l.name AS location_name,
                   (e.location_id = a.home_location_id) AS at_home
            FROM events e
            LEFT JOIN assets a ON a.epc = e.epc
            LEFT JOIN locations l ON l.id = e.location_id
            WHERE e.epc=? ORDER BY e.id""", (norm_epc(epc),)).fetchall()
    return [dict(r) for r in rows]


def dashboard():
    assets = list_assets()
    counts = {"RIGHT": 0, "WRONG": 0, "UNSEEN": 0, "NO_HOME": 0}
    for a in assets:
        counts[a["status"]] += 1
    return {
        "total_assets": len(assets),
        "right": counts["RIGHT"],
        "wrong": counts["WRONG"],
        "unseen": counts["UNSEEN"],
        "no_home": counts["NO_HOME"],
        "unregistered": len(unregistered_tags()),
        "wrong_list": [a for a in assets if a["status"] == "WRONG"][:10],
        "recent_events": query_events(limit=10),
    }


# -------------------------------------------------------------- CSV import
_HEX = re.compile(r"^[0-9A-F]+$")
ASSET_TEMPLATE = "epc,name,category,color,size,home_location,asset_id,serial_number\r\n"
ORDER_LINES_TEMPLATE = "kind,category,epc,qty\r\n"
_EPC_HINT = ("EPC must be hex (0-9, A-F). If it looks like 3.0E+23, Excel converted it: "
             "format the EPC column as Text before saving")


def _parse_csv(text, required):
    """Return [(row_number, {column: value})]. Accepts BOM, comma or semicolon delimiters."""
    text = (text or "").lstrip("\ufeff")
    first = text.splitlines()[0] if text.strip() else ""
    delim = ";" if first.count(";") > first.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delim)
    reader.fieldnames = [(h or "").strip().lower().replace(" ", "_") for h in (reader.fieldnames or [])]
    missing = [c for c in required if c not in reader.fieldnames]
    if missing:
        raise ValueError("Missing column(s): " + ", ".join(missing) + ". Download the template for the expected header")
    rows = []
    for i, r in enumerate(reader, start=2):
        clean = {k: (v or "").strip() for k, v in r.items() if k}
        if any(clean.values()):
            rows.append((i, clean))
    return rows


# ---------------------------------------------- flexible asset import
# (key, label, required, accepted header names after lowercasing and dropping punctuation)
FIELD_DEFS = [
    ("epc", "EPC", True, ["epc", "epchex", "tag", "tagid", "tagno", "tagnumber", "tagepc", "epcid", "epccode", "rfid", "rfidtag", "rfidepc", "idhex", "uid"]),
    ("name", "Name", True, ["name", "assetname", "itemname", "item", "description", "title"]),
    ("category", "Category", False, ["category", "type", "assettype", "group", "class"]),
    ("home_location", "Home location", False, ["homelocation", "location", "home", "site", "zone", "area", "department", "dept", "room"]),
    ("asset_code", "Asset ID", False, ["assetid", "assetcode", "assetno", "assetnumber", "id", "code"]),
    ("serial_number", "Serial number", False, ["serialnumber", "serial", "serialno", "sn"]),
    ("color", "Color", False, ["color", "colour"]),
    ("size", "Size", False, ["size"]),
    ("image", "Image (URL)", False, ["image", "imagepath", "imageurl", "photo", "photopath", "photourl", "picture"]),
]
_NOISE = {"lastrssi", "reads", "lastseen", "lastseents", "currentlocation", "status"}   # columns our own exports add


def norm_key(s):
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _parse_raw(text):
    """Return (headers, [(row_number, {header: value})]). Accepts a BOM and comma, semicolon or tab delimiters."""
    text = (text or "").lstrip("\ufeff")
    if not text.strip():
        raise ValueError("The file is empty")
    first = text.splitlines()[0]
    delim = max((",", ";", "\t"), key=first.count)
    reader = csv.reader(io.StringIO(text), delimiter=delim)
    headers = [h.strip() for h in next(reader, [])]
    rows = []
    for i, r in enumerate(reader, start=2):
        if any(c.strip() for c in r):
            rows.append((i, {h: (r[j].strip() if j < len(r) else "") for j, h in enumerate(headers) if h}))
    return headers, rows


def auto_map(headers):
    """Guess which file column feeds each of our fields. Each column is used at most once."""
    used, mapping = set(), {}
    norm = {h: norm_key(h) for h in headers}
    for key, _label, _req, aliases in FIELD_DEFS:
        hit = ""
        for alias in aliases:
            hit = next((h for h in headers if h and h not in used and norm[h] == alias), "")
            if hit:
                break
        mapping[key] = hit
        if hit:
            used.add(hit)
    return mapping


def _signature(headers):
    return hashlib.sha1("|".join(norm_key(h) for h in headers).encode()).hexdigest()[:12]


def save_import_profile(headers, mapping, extras):
    """Remember how this file layout was mapped, so the same template needs no setup next time."""
    set_setting("import_profile:" + _signature(headers), json.dumps({"mapping": mapping, "extras": extras}))


def load_import_profile(headers):
    raw = get_setting("import_profile:" + _signature(headers))
    try:
        prof = json.loads(raw) if raw else None
    except ValueError:
        return None
    if prof and all(v in headers for v in prof["mapping"].values() if v) and all(h in headers for h in prof["extras"]):
        return prof
    return None


def import_preview(text):
    headers, rows = _parse_raw(text)
    prof = load_import_profile(headers)
    mapping = prof["mapping"] if prof else auto_map(headers)
    if prof:
        extras = prof["extras"]
    else:
        mapped = {v for v in mapping.values() if v}
        extras = [h for h in headers if h and h not in mapped and norm_key(h) not in _NOISE]
    return {"headers": headers, "mapping": mapping, "extras": extras, "saved": bool(prof), "row_count": len(rows),
            "samples": [[r.get(h, "") for h in headers] for _, r in rows[:3]],
            "fields": [{"key": k, "label": l, "required": req} for k, l, req, _ in FIELD_DEFS]}


def import_assets_csv(text, mapping=None, extras=None):
    """Import assets from any CSV layout.

    mapping is {our field: file column}; without it the columns are guessed from their names.
    extras lists file columns to keep as custom fields. Every row is validated first and nothing is
    imported if any row is bad. Existing EPCs are updated (blank cells never overwrite). A new tag
    with no name is skipped, not an error.
    """
    headers, rows = _parse_raw(text)
    mapping = {k: (v or "") for k, v in (mapping if mapping is not None else auto_map(headers)).items()}
    for col in mapping.values():
        if col and col not in headers:
            raise ValueError(f"Column '{col}' is not in the file")
    if not mapping.get("epc"):
        raise ValueError("Choose which column holds the EPC")
    if not mapping.get("name"):
        raise ValueError("Choose which column holds the name")
    mapped_cols = {v for v in mapping.values() if v}
    extra_cols = [h for h in (extras or []) if h in headers and h not in mapped_cols]
    with conn() as c:
        existing = {r["epc"] for r in c.execute("SELECT epc FROM assets")}
    errors, seen, items, skipped = [], {}, [], 0
    for n, r in rows:
        v = {k: (r.get(col, "") if col else "") for k, col in mapping.items()}
        epc = norm_epc(v["epc"])
        if not epc or not _HEX.match(epc):
            errors.append({"row": n, "msg": _EPC_HINT})
        elif epc in seen:
            errors.append({"row": n, "msg": f"Duplicate EPC {epc} (also on row {seen[epc]})"})
        elif not v["name"] and epc not in existing:
            seen[epc] = n          # a new tag with no name yet: skipped, not an error
            skipped += 1
        else:
            seen[epc] = n
            items.append((epc, v, {h: r.get(h, "") for h in extra_cols if r.get(h, "")}))
    result = {"created": 0, "updated": 0, "locations_created": 0, "skipped": skipped, "errors": errors,
              "image_refs": [], "extras_kept": extra_cols}
    if errors:
        return result
    if not items:
        result["errors"] = [{"row": 0, "msg": "No row has a name. Fill in the name column for the tags you want to register"
                             if skipped else "The file has no data rows"}]
        return result
    with conn() as c:
        loc_ids = {}
        for _, v, _x in items:
            name = v["home_location"]
            if name and name.lower() not in loc_ids:
                row = c.execute("SELECT id FROM locations WHERE lower(name)=lower(?)", (name,)).fetchone()
                if row is None:
                    cur = c.execute("INSERT INTO locations(name, type) VALUES (?, 'WAREHOUSE')", (name,))
                    loc_ids[name.lower()] = cur.lastrowid
                    result["locations_created"] += 1
                else:
                    loc_ids[name.lower()] = row["id"]
        for epc, v, extra in items:
            home = loc_ids.get(v["home_location"].lower())
            row = c.execute("SELECT extra FROM assets WHERE epc=?", (epc,)).fetchone()
            if row:
                sets = {k: v[k] for k in ("name", "category", "color", "size", "asset_code", "serial_number") if v[k]}
                if home:
                    sets["home_location_id"] = home
                if extra:
                    try:
                        merged = json.loads(row["extra"] or "{}")
                    except ValueError:
                        merged = {}
                    merged.update(extra)
                    sets["extra"] = json.dumps(merged, ensure_ascii=False)
                if sets:
                    c.execute(f"UPDATE assets SET {', '.join(k + '=?' for k in sets)} WHERE epc=?", (*sets.values(), epc))
                result["updated"] += 1
            else:
                c.execute("INSERT INTO assets(epc, name, category, color, size, home_location_id, created_at, "
                          "asset_code, serial_number, extra) VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (epc, v["name"], v["category"], v["color"], v["size"], home, _iso(_now()),
                           v["asset_code"], v["serial_number"], json.dumps(extra, ensure_ascii=False)))
                result["created"] += 1
            if v["image"]:
                result["image_refs"].append({"epc": epc, "ref": v["image"]})
    return result


def import_order_lines_csv(order_id, text):
    """Validate every row first; add nothing if any row is bad."""
    rows = _parse_csv(text, [])
    with conn() as c:
        o = c.execute("SELECT status FROM orders WHERE id=?", (order_id,)).fetchone()
        if not o or o["status"] != "OPEN":
            raise ValueError("order is not open")
        registered = {r["epc"] for r in c.execute("SELECT epc FROM assets")}
        on_order = {r["epc"] for r in c.execute("SELECT epc FROM order_lines WHERE order_id=? AND kind='EPC'", (order_id,))}
    errors, items, seen = [], [], set()
    for n, r in rows:
        epc = norm_epc(r.get("epc", ""))
        kind = (r.get("kind") or ("EPC" if epc else "CATEGORY")).upper()
        if kind == "EPC":
            if not epc or not _HEX.match(epc):
                errors.append({"row": n, "msg": _EPC_HINT})
            elif epc not in registered:
                errors.append({"row": n, "msg": f"EPC {epc} is not a registered asset. Import assets first"})
            elif epc in on_order or epc in seen:
                errors.append({"row": n, "msg": f"EPC {epc} is already on this order"})
            else:
                seen.add(epc)
                items.append(("EPC", None, epc, 1))
        elif kind == "CATEGORY":
            try:
                qty = int(r.get("qty") or 1)
            except ValueError:
                qty = 0
            if not r.get("category"):
                errors.append({"row": n, "msg": "Category is required for a CATEGORY line"})
            elif qty < 1:
                errors.append({"row": n, "msg": "qty must be a whole number of at least 1"})
            else:
                items.append(("CATEGORY", r["category"], None, qty))
        else:
            errors.append({"row": n, "msg": "kind must be CATEGORY or EPC (or leave it blank)"})
    if errors:
        return {"added": 0, "errors": errors}
    if not items:
        return {"added": 0, "errors": [{"row": 0, "msg": "The file has no data rows"}]}
    for kind, category, epc, qty in items:
        add_line(order_id, kind, qty=qty, category=category, epc=epc)
    return {"added": len(items), "errors": []}

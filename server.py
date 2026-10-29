"""TagSentry: RFID gate control demo server.

Receives Zebra IoT Connector HTTP POSTs on /rfid, stores reads through db.ingest_tag(),
pushes live updates over Socket.IO, and exposes a REST API for the web UI.
"""
import base64
import csv
import io
import argparse
import json
import os
import re
import socket
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime
from functools import wraps

import requests
import urllib3
from flask import Flask, Response, jsonify, request, send_file
from flask_cors import CORS
from flask_socketio import SocketIO

import db

urllib3.disable_warnings()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SILENT_THRESHOLD = 30  # seconds without any reader data before "silent" alert

app = Flask(__name__)
app.config["SECRET_KEY"] = "zebra-rfid-demo"
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading")

state = {
    "last_rfid_time": None,
    "reader_ip": None,
    "reader_password": None,
    "token": None,
    "reader_status": {},
    "posts": deque(maxlen=20),   # summaries of the latest /rfid POSTs, for diagnosis
}
# Raw read counters for the Gate Portal "tags in range" view. Not stored: every report counts,
# even the ones the event log skips as duplicates.
live = {"since": datetime.now().isoformat(timespec="seconds"), "tags": {}}


def live_reset():
    live["since"] = datetime.now().isoformat(timespec="seconds")
    live["tags"] = {}


def live_note(tag):
    try:
        rssi = float(tag["rssi"])
    except (TypeError, ValueError):
        rssi = 0.0
    if rssi < float(db.get_setting("min_rssi", "-100")):
        return
    epc = db.norm_epc(tag["epc"])
    now = datetime.now().isoformat(timespec="seconds")
    t = live["tags"].setdefault(epc, {"epc": epc, "reads": 0, "first_ts": now})
    t.update(rssi=rssi, antenna=tag["antenna"], direction=tag["direction"], last_ts=now)
    t["reads"] += 1


def live_snapshot():
    names = db.asset_names()
    tags = sorted(live["tags"].values(), key=lambda t: t["last_ts"], reverse=True)[:200]
    out = [{**t, "name": names[t["epc"]][0] if t["epc"] in names else "",
            "category": names[t["epc"]][1] if t["epc"] in names else "", "registered": t["epc"] in names} for t in tags]
    return {"since": live["since"], "total_reads": sum(t["reads"] for t in live["tags"].values()),
            "unique": len(live["tags"]), "unregistered": sum(1 for t in out if not t["registered"]), "tags": out}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ------------------------------------------------------------ payload parsing
def first_value(*values):
    for v in values:
        if v not in (None, ""):
            return v
    return None


def normalize_direction(value):
    text = str(value).strip() if value is not None else ""
    lowered = text.lower()
    if lowered in ("in", "enter", "entry", "inbound"):
        return "In"
    if lowered in ("out", "exit", "outbound"):
        return "Out"
    return text


def is_gpi_event(event):
    if not isinstance(event, dict):
        return False
    etype = str(first_value(event.get("type"), event.get("event_type"), "")).lower()
    if etype == "gpi":
        return True
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    return (first_value(data.get("pin"), event.get("pin")) is not None
            and first_value(data.get("state"), event.get("state")) is not None
            and etype in ("", "gpi_event"))


def normalize_gpi_event(event):
    data = event.get("data") if isinstance(event.get("data"), dict) else {}
    pin = first_value(data.get("pin"), event.get("pin"))
    try:
        pin = int(pin)
    except (TypeError, ValueError):
        pass
    return {
        "pin": pin,
        "state": str(first_value(data.get("state"), event.get("state")) or "").upper(),
        "timestamp": first_value(event.get("timestamp"), datetime.now().strftime("%H:%M:%S")),
        "component": event.get("component", "RG"),
    }


def parse_tag(event):
    """Extract (epc, rssi, antenna, direction) from one ZIOT tag event, or None."""
    if not isinstance(event, dict):
        return None
    data = event.get("data")
    tag = data if isinstance(data, dict) else event
    epc = first_value(tag.get("idHex"), tag.get("epc"), tag.get("EPC"), tag.get("tagID"))
    if epc is None:
        return None
    antenna = first_value(tag.get("antenna"), tag.get("antennaPort"), 1)
    try:
        antenna = int(antenna)
    except (TypeError, ValueError):
        antenna = 1
    return {
        "epc": epc,
        "rssi": first_value(tag.get("peakRssi"), tag.get("rssi"), 0),
        "antenna": antenna,
        "direction": normalize_direction(first_value(
            tag.get("direction"), tag.get("Direction"), tag.get("DIRECTION"),
            event.get("direction"), event.get("Direction"), event.get("DIRECTION"))),
    }


# ----------------------------------------------------------------- /rfid
@app.route("/rfid", methods=["POST"])
def receive_rfid():
    state["last_rfid_time"] = time.time()
    try:
        data = request.get_json(force=True)
        events = data if isinstance(data, list) else [data]
        stored, gpi_count, heartbeat = [], 0, False
        counts = {"tags": 0, "ignored": 0}
        samples = []
        for event in events:
            if isinstance(event, dict) and event.get("event_type") == "HEARTBEAT":
                heartbeat = True
                info = event.get("reader_info", {})
                uptime = int(info.get("uptime", 0))
                socketio.emit("reader_heartbeat_event", {
                    "model": info.get("model", ""),
                    "firmware": info.get("firmware", ""),
                    "uptime": uptime,
                    "uptime_str": (f"{uptime // 3600}h {(uptime % 3600) // 60}m" if uptime > 3600
                                   else f"{uptime // 60}m {uptime % 60}s"),
                })
            elif is_gpi_event(event):
                gpi_count += 1
                gpi = normalize_gpi_event(event)
                socketio.emit("gpi_event", gpi)
                log(f"GPI{gpi['pin']} {gpi['state']}")
            else:
                tag = parse_tag(event)
                if tag:
                    counts["tags"] += 1
                    live_note(tag)
                    ev = db.ingest_tag(tag["epc"], tag["antenna"], tag["rssi"], tag["direction"])
                    if ev:
                        stored.append(ev)
                else:
                    counts["ignored"] += 1
                    if len(samples) < 3:
                        samples.append(json.dumps(event, default=str)[:300])
        if counts["tags"]:
            socketio.emit("live_update", live_snapshot())
        summary = {"time": datetime.now().strftime("%H:%M:%S"), "events": len(events), "tags": counts["tags"],
                   "stored": len(stored), "gpi": gpi_count, "heartbeat": heartbeat,
                   "ignored": counts["ignored"], "ignored_samples": samples}
        state["posts"].append(summary)
        log(f"POST /rfid: {len(events)} event(s) -> tags={counts['tags']} stored={len(stored)} "
            f"gpi={gpi_count} heartbeat={heartbeat} ignored={counts['ignored']}")
        for sample in samples:
            log(f"  ignored payload: {sample}")
        if stored:
            socketio.emit("tag_events", {"events": stored})
            for ev in stored:
                log(f"{ev['epc']} | {ev['result']} | {ev.get('direction') or '-'} | {ev.get('location_name') or '-'}")
            push_order_update()
        return jsonify({"status": "ok", "stored": len(stored), "gpi": gpi_count, "heartbeat": heartbeat,
                        "ignored": counts["ignored"]}), 200
    except Exception as e:
        log(f"/rfid error: {e}")
        socketio.emit("rfid_raw", {"raw": request.get_data(as_text=True)})
        return jsonify({"status": "error", "msg": str(e)}), 400


def push_order_update():
    oid = db.get_setting("active_order_id")
    if oid:
        socketio.emit("order_update", db.evaluate_order(int(oid)))


def push_state():
    socketio.emit("state_update", current_state())


def current_state():
    loc = db.get_setting("reader_location_id")
    oid = db.get_setting("active_order_id")
    return {
        "reader_location_id": int(loc) if loc else None,
        "active_order_id": int(oid) if oid else None,
        "settings": {
            "min_rssi": float(db.get_setting("min_rssi", "-100")),
            "event_interval_sec": float(db.get_setting("event_interval_sec", "30")),
        },
        "reader": {
            "ip": state["reader_ip"],
            "status": state["reader_status"],
            "seconds_ago": int(time.time() - state["last_rfid_time"]) if state["last_rfid_time"] else None,
            "silent_after": SILENT_THRESHOLD,
            "last_post": state["posts"][-1] if state["posts"] else None,
        },
    }


# ------------------------------------------------------- reader control
def get_token(ip, password):
    """Try the known login endpoints until one returns a JWT."""
    attempts = [
        ("POST", f"https://{ip}/login"),
        ("POST", f"https://{ip}/cloud/login"),
        ("GET", f"https://{ip}/cloud/localRestLogin"),
    ]
    for method, url in attempts:
        try:
            if method == "POST":
                resp = requests.post(url, json={"username": "admin", "password": password},
                                     verify=False, timeout=5)
            else:
                creds = base64.b64encode(f"admin:{password}".encode()).decode()
                resp = requests.get(url, headers={"Authorization": f"Basic {creds}"},
                                    verify=False, timeout=5)
            token = resp.cookies.get("IOTCToken")
            if token:
                return token
            try:
                j = resp.json()
                token = j.get("message") or j.get("token") or j.get("IOTCToken") or j.get("access_token")
                if token and str(token).startswith("ey"):
                    return str(token)
            except ValueError:
                pass
        except requests.RequestException as e:
            log(f"login {url} failed: {e}")
    return None


def reader_command(action, ip, password):
    """PUT /cloud/start or /cloud/stop. Returns (ok, message)."""
    if not ip:
        return False, "No reader IP"
    state["reader_ip"], state["reader_password"] = ip, password
    token = get_token(ip, password)
    if not token:
        return False, "Login failed. Check IP and password"
    state["token"] = token
    try:
        resp = requests.put(f"https://{ip}/cloud/{action}",
                            headers={"Authorization": f"Bearer {token}"}, verify=False, timeout=15)
    except requests.RequestException as e:
        return False, str(e)
    log(f"reader {action}: {resp.status_code}")
    if resp.status_code in (200, 201, 204, 422):  # 422 = already in that state
        socketio.emit("reader_state", {"running": action == "start"})
        return True, "ok"
    return False, f"Reader returned {resp.status_code}"


def _creds(body):
    return (body.get("ip") or "").strip(), (body.get("password") or "readerconfig").strip()


@app.route("/reader/start", methods=["POST"])
def reader_start():
    ok, msg = reader_command("start", *_creds(request.get_json(force=True)))
    return (jsonify({"status": "ok", "action": "start"}), 200) if ok else (jsonify({"status": "error", "msg": msg}), 400)


@app.route("/reader/stop", methods=["POST"])
def reader_stop():
    ok, msg = reader_command("stop", *_creds(request.get_json(force=True)))
    return (jsonify({"status": "ok", "action": "stop"}), 200) if ok else (jsonify({"status": "error", "msg": msg}), 400)


@app.route("/reader/heartbeat", methods=["GET"])
def reader_heartbeat():
    t = state["last_rfid_time"]
    if t is None:
        return jsonify({"status": "no_data", "seconds_ago": None, "silent": False})
    ago = int(time.time() - t)
    return jsonify({"status": "silent" if ago > SILENT_THRESHOLD else "alive",
                    "seconds_ago": ago, "silent": ago > SILENT_THRESHOLD})


# ---------------------------------------------------------- API helpers
def api(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except sqlite3.IntegrityError as e:
            msg = "Already exists" if "UNIQUE" in str(e) else "Invalid reference"
            return jsonify({"status": "error", "msg": msg}), 400
        except ValueError as e:
            return jsonify({"status": "error", "msg": str(e)}), 400
    return wrapper


def body():
    return request.get_json(force=True, silent=True) or {}


def opt_int(v):
    return int(v) if v not in (None, "") else None


# ------------------------------------------------------------------ API
@app.route("/api/live")
def api_live():
    return jsonify(live_snapshot())


@app.route("/api/live/reset", methods=["POST"])
def api_live_reset():
    live_reset()
    socketio.emit("live_update", live_snapshot())
    return jsonify({"status": "ok"})


@app.route("/api/reset", methods=["POST"])
@api
def api_reset():
    """Delete demo data. The UI asks for confirmation first; confirm must be sent explicitly."""
    b = body()
    if b.get("confirm") is not True:
        raise ValueError("confirm is required")
    db.reset_data(b.get("scope", ""))
    live_reset()
    socketio.emit("live_update", live_snapshot())
    socketio.emit("assets_changed", {})
    push_state()
    return jsonify({"status": "ok"})


@app.route("/api/debug/posts")
def api_debug_posts():
    """Newest first: what the reader has been sending, and what the server made of it."""
    return jsonify(list(state["posts"])[::-1])


@app.route("/api/state")
def api_state():
    return jsonify(current_state())


@app.route("/api/dashboard")
@api
def api_dashboard():
    return jsonify(db.dashboard())


@app.route("/api/settings", methods=["GET", "POST"])
@api
def api_settings():
    if request.method == "POST":
        b = body()
        for key in ("min_rssi", "event_interval_sec"):
            if key in b:
                float(b[key])  # validate
                db.set_setting(key, b[key])
        push_state()
    return jsonify(current_state()["settings"])


@app.route("/api/locations", methods=["GET", "POST"])
@api
def api_locations():
    if request.method == "POST":
        b = body()
        lid = db.add_location(b.get("name", ""), b.get("type", "WAREHOUSE"))
        return jsonify({"status": "ok", "id": lid}), 201
    return jsonify(db.list_locations())


@app.route("/api/reader-location", methods=["POST"])
@api
def api_reader_location():
    lid = opt_int(body().get("location_id"))
    if lid is not None and lid not in [l["id"] for l in db.list_locations()]:
        raise ValueError("Unknown location")
    db.set_reader_location(lid)
    push_state()
    return jsonify({"status": "ok", "reader_location_id": lid})


@app.route("/api/assets", methods=["GET", "POST"])
@api
def api_assets():
    if request.method == "POST":
        b = body()
        epc = db.register_asset(b.get("epc"), b.get("name", ""), b.get("category", ""),
                                opt_int(b.get("home_location_id")), b.get("color", ""), b.get("size", ""),
                                b.get("asset_code", ""), b.get("serial_number", ""))
        socketio.emit("assets_changed", {"epc": epc})
        return jsonify({"status": "ok", "epc": epc}), 201
    return jsonify(db.list_assets(request.args.get("search", "")))


@app.route("/api/assets/unregistered")
@api
def api_unregistered():
    return jsonify(db.unregistered_tags())


@app.route("/api/assets/<epc>", methods=["PUT"])
@api
def api_asset_update(epc):
    b = body()
    if "home_location_id" in b:
        b["home_location_id"] = opt_int(b["home_location_id"])
    db.update_asset(epc, **b)
    socketio.emit("assets_changed", {"epc": db.norm_epc(epc)})
    return jsonify({"status": "ok"})


@app.route("/api/assets/<epc>/image", methods=["GET", "PUT", "DELETE"])
@api
def api_asset_image(epc):
    """Optional product photo. The UI shrinks it to about 320 px before upload."""
    if request.method == "PUT":
        m = re.match(r"^data:(image/(?:jpeg|png|webp));base64,(.+)$", body().get("data_url", ""), re.S)
        if not m:
            raise ValueError("Unsupported image. Use JPEG, PNG or WebP")
        data = base64.b64decode(m.group(2))
        if len(data) > 600_000:
            raise ValueError("Image is too large (max about 600 KB)")
        db.set_asset_image(epc, m.group(1), data)
        socketio.emit("assets_changed", {"epc": db.norm_epc(epc)})
        return jsonify({"status": "ok"})
    if request.method == "DELETE":
        db.delete_asset_image(epc)
        socketio.emit("assets_changed", {"epc": db.norm_epc(epc)})
        return jsonify({"status": "ok"})
    img = db.get_asset_image(epc)
    if not img:
        return Response(status=404)
    return Response(img["data"], mimetype=img["mime"],
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.route("/api/assets/<epc>/history")
@api
def api_asset_history(epc):
    return jsonify(db.asset_history(epc))


@app.route("/api/orders", methods=["GET", "POST"])
@api
def api_orders():
    if request.method == "POST":
        b = body()
        oid = db.create_order(b.get("code", ""), b.get("type", ""), b.get("reference", ""))
        return jsonify({"status": "ok", "id": oid}), 201
    return jsonify(db.list_orders())


@app.route("/api/orders/<int:order_id>")
@api
def api_order(order_id):
    return jsonify(db.evaluate_order(order_id))


@app.route("/api/orders/<int:order_id>/lines", methods=["POST"])
@api
def api_add_line(order_id):
    b = body()
    lid = db.add_line(order_id, b.get("kind", ""), int(b.get("qty", 1)), b.get("category"), b.get("epc"))
    push_order_update()
    return jsonify({"status": "ok", "id": lid}), 201


@app.route("/api/lines/<int:line_id>", methods=["DELETE"])
@api
def api_delete_line(line_id):
    db.delete_line(line_id)
    push_order_update()
    return jsonify({"status": "ok"})


@app.route("/api/orders/<int:order_id>/commit", methods=["POST"])
@api
def api_commit(order_id):
    b = body()
    ev = db.commit_order(order_id, b.get("reference", ""), b.get("reason", ""))
    socketio.emit("order_update", ev)
    push_state()
    return jsonify(ev)


@app.route("/api/gate/start", methods=["POST"])
@api
def api_gate_start():
    """Start the reader, optionally selecting the order to scan against.

    Without order_id the reader just starts and reads are logged with no order attached,
    which is enough for a desk test with no GPIO and no order.
    """
    b = body()
    live_reset()
    ip, pw = _creds(b)
    oid = b.get("order_id")
    has_order = oid not in (None, "")
    if not has_order and not ip:
        raise ValueError("Enter the reader IP in Settings first")
    previous = db.get_setting("active_order_id")
    if has_order:
        db.set_active_order(int(oid))
    if ip:
        ok, msg = reader_command("start", ip, pw)
        if not ok:
            if has_order:
                db.set_setting("active_order_id", previous)
            return jsonify({"status": "error", "msg": msg}), 400
    push_state()
    push_order_update()
    return jsonify({"status": "ok", "reader": "started" if ip else "skipped", "order": has_order})


@app.route("/api/gate/stop", methods=["POST"])
@api
def api_gate_stop():
    """Pause reading. The order stays active until it is committed."""
    ip, pw = _creds(body())
    if ip:
        ok, msg = reader_command("stop", ip, pw)
        if not ok:
            return jsonify({"status": "error", "msg": msg}), 400
    return jsonify({"status": "ok", "reader": "stopped" if ip else "skipped"})


def csv_download(text, filename):
    return Response(text, mimetype="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={filename}"})


@app.route("/api/templates/assets.csv")
def api_template_assets():
    return csv_download(db.ASSET_TEMPLATE, "assets_template.csv")


@app.route("/api/templates/order-lines.csv")
def api_template_lines():
    return csv_download(db.ORDER_LINES_TEMPLATE, "order_lines_template.csv")


def fetch_image(url):
    """Download one product photo for an import. Raises ValueError with a short reason."""
    try:
        r = requests.get(url, timeout=5, stream=True, headers={"User-Agent": "TagSentry"})
    except requests.RequestException:
        raise ValueError("could not download")
    try:
        if r.status_code != 200:
            raise ValueError(f"HTTP {r.status_code}")
        mime = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if mime not in ("image/jpeg", "image/png", "image/webp"):
            raise ValueError("not a JPEG, PNG or WebP image")
        data = r.raw.read(600_001, decode_content=True)
        if len(data) > 600_000:
            raise ValueError("larger than 600 KB")
        return mime, data
    finally:
        r.close()


def import_images(refs, limit=50):
    """Best effort: URLs are downloaded, local file paths cannot be read from the server and are only counted."""
    out = {"images_imported": 0, "images_local": 0, "images_failed": 0, "image_errors": []}
    for x in refs:
        if not re.match(r"^https?://", x["ref"], re.I):
            out["images_local"] += 1
            continue
        try:
            if out["images_imported"] + out["images_failed"] >= limit:
                raise ValueError(f"over the limit of {limit} images per import")
            mime, data = fetch_image(x["ref"])
            db.set_asset_image(x["epc"], mime, data)
            out["images_imported"] += 1
        except ValueError as e:
            out["images_failed"] += 1
            if len(out["image_errors"]) < 3:
                out["image_errors"].append(f"{x['epc']}: {e}")
    return out


@app.route("/api/assets/import/preview", methods=["POST"])
@api
def api_assets_import_preview():
    """Read the CSV header and suggest which column feeds which field (using a saved mapping if this layout was seen before)."""
    return jsonify(db.import_preview(body().get("csv", "")))


@app.route("/api/assets/import", methods=["POST"])
@api
def api_assets_import():
    b = body()
    mapping, extras = b.get("mapping"), b.get("extras")
    result = db.import_assets_csv(b.get("csv", ""), mapping, extras)
    refs = result.pop("image_refs")
    if not result["errors"]:
        if mapping is not None:   # remember the layout so the same template needs no setup next time
            headers, _ = db._parse_raw(b.get("csv", ""))
            db.save_import_profile(headers, mapping, extras or [])
        result.update(import_images(refs))
        socketio.emit("assets_changed", {})
    return jsonify(result)


@app.route("/api/orders/<int:order_id>/lines/import", methods=["POST"])
@api
def api_lines_import(order_id):
    result = db.import_order_lines_csv(order_id, body().get("csv", ""))
    if not result["errors"]:
        push_order_update()
    return jsonify(result)


@app.route("/api/gate/clear", methods=["POST"])
@api
def api_gate_clear():
    """Detach the active order. Scans already recorded stay on the order."""
    db.set_active_order(None)
    push_state()
    return jsonify({"status": "ok"})


@app.route("/api/events")
@api
def api_events():
    return jsonify(db.query_events(
        epc=request.args.get("q") or None,
        location_id=opt_int(request.args.get("location_id")),
        result=request.args.get("result") or None,
        limit=int(request.args.get("limit", 200))))


def csv_text(cols, rows):
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") if r.get(c) is not None else "" for c in cols])
    return out.getvalue()


@app.route("/api/assets/unregistered.csv")
@api
def api_unregistered_csv():
    """Unregistered tags in the import-template layout: fill in name, category and home_location, then import."""
    rows = [{"epc": t["epc"], "last_rssi": t["rssi"], "reads": t["reads"], "last_seen": t["last_seen_ts"]}
            for t in db.unregistered_tags()]
    cols = ["epc", "name", "category", "color", "size", "home_location", "asset_id", "serial_number",
            "last_rssi", "reads", "last_seen"]
    return csv_download(csv_text(cols, rows), "unregistered_tags.csv")


@app.route("/api/assets.csv")
@api
def api_assets_csv():
    """Import-template columns first, then status columns, then any custom fields. Re-importable as is."""
    assets = db.list_assets(request.args.get("search", ""))
    rows = [{**a, "home_location": a["home_name"], "asset_id": a["asset_code"], "current_location": a["current_name"]}
            for a in assets]
    cols = ["epc", "name", "category", "color", "size", "home_location", "asset_id", "serial_number",
            "current_location", "status", "last_seen_ts"]
    for a in assets:                      # custom fields from imports, as their own columns
        for k in a["extra"]:
            if k not in cols:
                cols.append(k)
    for r in rows:
        r.update({k: v for k, v in r["extra"].items() if k not in ("epc", "name", "category", "color", "size",
                  "home_location", "asset_id", "serial_number", "current_location", "status", "last_seen_ts")})
    return csv_download(csv_text(cols, rows), "assets.csv")


@app.route("/api/orders/<int:order_id>.csv")
@api
def api_order_csv(order_id):
    """Reconciliation result: one row per scanned EPC, plus MISSING rows for what is still expected."""
    code = db.evaluate_order(order_id)["order"]["code"]
    cols = ["order", "type", "order_status", "row_type", "epc", "name", "category", "status", "line",
            "line_expected", "line_scanned", "first_seen", "direction", "antenna", "rssi", "location"]
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", code)
    return csv_download(csv_text(cols, db.order_export_rows(order_id)), f"order_{safe}.csv")


@app.route("/api/events.csv")
@api
def api_events_csv():
    rows = db.query_events(
        epc=request.args.get("q") or None,
        location_id=opt_int(request.args.get("location_id")),
        result=request.args.get("result") or None,
        limit=int(request.args.get("limit", 5000)))
    cols = ["ts", "epc", "name", "category", "antenna", "rssi", "direction",
            "location_name", "home_name", "order_id", "result"]
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(cols)
    for r in rows:
        w.writerow([r.get(c, "") for c in cols])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=gate_events.csv"})


@app.route("/")
def index():
    path = os.path.join(BASE_DIR, "index.html")
    if os.path.exists(path):
        return send_file(path)
    return "RFID gate demo API is running. The web UI (index.html) has not been added yet.", 200


# ------------------------------------------------------ background threads
def poll_reader_status():
    """Poll /cloud/status every 15 s using the token from the last start/stop."""
    while True:
        time.sleep(15)
        ip, token = state["reader_ip"], state["token"]
        if not ip or not token:
            continue
        try:
            resp = requests.get(f"https://{ip}/cloud/status",
                                headers={"Authorization": f"Bearer {token}"}, verify=False, timeout=8)
            if resp.status_code == 401:
                state["token"] = None
            elif resp.status_code == 200:
                d = resp.json()
                status = {
                    "radioActivity": d.get("radioActivity", "unknown"),
                    "radioConnection": d.get("radioConnection", "unknown"),
                    "numTagReads": d.get("numTagReads", 0),
                    "cpuUtilization": d.get("cpuUtilization", 0),
                    "temperature": d.get("temperature", {}),
                    "online": True,
                }
                state["reader_status"] = status
                socketio.emit("reader_status", status)
        except requests.RequestException as e:
            state["reader_status"] = {"online": False, "error": str(e)}
            socketio.emit("reader_status", state["reader_status"])


def heartbeat_monitor():
    """Emit reader_silent / reader_alive when data stops or resumes."""
    was_silent = False
    while True:
        time.sleep(5)
        t = state["last_rfid_time"]
        if t is None:
            continue
        ago = time.time() - t
        silent = ago > SILENT_THRESHOLD
        if silent and not was_silent:
            socketio.emit("reader_silent", {"seconds_ago": int(ago)})
            log(f"WARNING reader silent for {int(ago)}s")
        elif was_silent and not silent:
            socketio.emit("reader_alive", {"seconds_ago": int(ago)})
            log("reader back online")
        was_silent = silent
        socketio.emit("reader_heartbeat", {"seconds_ago": int(ago), "silent": silent})


DEFAULT_PORT = int(os.environ.get("TAGSENTRY_PORT", "7500"))   # 7500, like the FX7500. Change with --port


def lan_ip():
    """Best guess of this PC's LAN address, to print the URLs the reader and browsers should use."""
    for target in ("8.8.8.8", "10.255.255.255"):   # connecting a UDP socket sends nothing, it only picks the route
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sk:
                sk.connect((target, 80))
                ip = sk.getsockname()[0]
            if not ip.startswith("127."):
                return ip
        except OSError:
            pass
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError:
        return "<YOUR_PC_IP>"


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="TagSentry: RFID gate control demo")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to listen on (default {DEFAULT_PORT})")
    port = parser.parse_args().port
    db.init_db()
    ip = lan_ip()
    print("=" * 56)
    print("  TagSentry  |  RFID gate control")
    print("=" * 56)
    print(f"  Database      : {os.path.abspath(db.DB_PATH)}")
    print(f"  Open the UI   : http://{ip}:{port}")
    print(f"  Reader POST   : http://{ip}:{port}/rfid   <- set this URL in the reader")
    print("=" * 56)
    threading.Thread(target=heartbeat_monitor, daemon=True).start()
    threading.Thread(target=poll_reader_status, daemon=True).start()
    try:
        socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
    except (OSError, SystemExit) as e:    # werkzeug exits with code 1 when the port is taken
        if isinstance(e, OSError) or e.code not in (0, None):
            print(f"\nPort {port} is already in use (another demo?). Try:  python server.py --port {port + 1}")
        raise

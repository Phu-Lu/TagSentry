import os
import tempfile

import db

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
db.init_db()

import server  # noqa: E402

c = server.app.test_client()


def post(url, **j):
    return c.post(url, json=j)


def tag(epc, direction="Out", rssi=-55, antenna=1):
    return {"data": {"idHex": epc, "peakRssi": rssi, "antenna": antenna, "direction": direction}}


# --- master data
wh = post("/api/locations", name="Warehouse A", type="WAREHOUSE").get_json()["id"]
gate = post("/api/locations", name="Gate 1", type="GATE").get_json()["id"]
assert post("/api/locations", name="Gate 1", type="GATE").status_code == 400, "duplicate name rejected"
for i in (1, 2, 3):
    assert post("/api/assets", epc=f"e280{i}", name=f"Laptop {i}", category="Laptop", home_location_id=wh).status_code == 201
assert post("/api/assets", epc="E2801", name="dup").status_code == 400

# --- order and gate
oid = post("/api/orders", code="DO-001", type="OUT").get_json()["id"]
assert post(f"/api/orders/{oid}/lines", kind="CATEGORY", category="Laptop", qty=2).status_code == 201
assert post("/api/reader-location", location_id=999).status_code == 400
assert post("/api/reader-location", location_id=gate).status_code == 200
assert post("/api/gate/start", order_id=oid).get_json()["reader"] == "skipped"
assert c.get("/api/state").get_json()["active_order_id"] == oid

# --- reader traffic: tags, unknown tag, GPI, heartbeat, in one batch
batch = [tag("E2801"), tag("E2802"), tag("E2803"), tag("DEAD0001"),
         {"type": "gpi", "data": {"pin": 1, "state": "high"}},
         {"event_type": "HEARTBEAT", "reader_info": {"model": "FX7500", "uptime": 125}}]
r = c.post("/rfid", json=batch).get_json()
assert r["stored"] == 4 and r["gpi"] == 1 and r["heartbeat"] is True, r
assert c.post("/rfid", json=batch).get_json()["stored"] == 0, "repeat reads are deduped"

ev = c.get(f"/api/orders/{oid}").get_json()
assert ev["summary"]["progress_pct"] == 100
assert {e["reason"] for e in ev["exceptions"]} == {"OVER", "UNKNOWN"}, ev["exceptions"]
assert [t["epc"] for t in c.get("/api/assets/unregistered").get_json()] == ["DEAD0001"]

# --- register the unknown tag on screen, then commit
assert post("/api/assets", epc="DEAD0001", name="New laptop", category="Laptop", home_location_id=wh).status_code == 201
assert c.get("/api/assets/unregistered").get_json() == []
res = post(f"/api/orders/{oid}/commit", reason="demo").get_json()
assert res["order"]["status"] == "DISCREPANCY"
assert post(f"/api/orders/{oid}/lines", kind="CATEGORY", category="Laptop", qty=1).status_code == 400, "locked after commit"

# --- read-side endpoints
d = c.get("/api/dashboard").get_json()
assert d["total_assets"] == 4 and d["wrong"] == 4, d  # all four seen at Gate 1, home is Warehouse A
assert len(c.get("/api/assets/E2801/history").get_json()) == 1
assert c.get("/api/events?result=OVER").get_json()[0]["epc"] == "E2803"
csv_text = c.get("/api/events.csv").data.decode()
assert csv_text.splitlines()[0].startswith("ts,epc") and "E2803" in csv_text
assert c.get("/api/orders").get_json()[0]["status"] == "DISCREPANCY"
assert post("/api/settings", min_rssi=-70).get_json()["min_rssi"] == -70
assert c.get("/").status_code == 200
assert c.post("/rfid", data="not json").status_code == 400
print("all server tests passed")

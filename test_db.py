import os
import tempfile
from datetime import datetime, timedelta

import db

db.DB_PATH = os.path.join(tempfile.mkdtemp(), "t.db")
db.init_db()
t0 = datetime(2026, 1, 1, 9, 0, 0)

wh = db.add_location("Warehouse A", "WAREHOUSE")
gate = db.add_location("Gate 1", "GATE")
dept = db.add_location("Dept B", "DEPT")

for i in (1, 2, 3):
    db.register_asset(f"E280{i}", f"Laptop {i}", "Laptop", wh)
db.register_asset("E2809", "Monitor", "Monitor", wh)
db.register_asset("E2808", "Phone", "Phone", dept)

# Order OUT: 2 laptops by category + the monitor by EPC
oid = db.create_order("DO-001", "OUT")
db.add_line(oid, "CATEGORY", qty=2, category="Laptop")
db.add_line(oid, "EPC", epc="e2809")
db.set_reader_location(gate)
db.set_active_order(oid)

r = lambda epc, **k: db.ingest_tag(epc, now=t0 + timedelta(seconds=k.pop("s", 0)), **k)

assert r("E2801", direction="Out")["result"] == "OK"
assert r("E2802", direction="Out")["result"] == "OK"
assert r("E2803", direction="Out")["result"] == "OVER", "3rd laptop must be over-counted"
assert r("E2809", direction="Out")["result"] == "OK"
assert r("E2808", direction="Out")["result"] == "UNEXPECTED", "phone not on order"
assert r("FFFF0001", direction="Out")["result"] == "UNKNOWN"
assert r("E2801", direction="Out", s=5) is None, "dedup within interval"
assert r("E2801", direction="Out", s=60) is not None, "re-log after interval"
assert r("E2801", direction="In", s=61) is not None, "direction change logs"
db.set_setting("min_rssi", "-70")
assert r("E2802", rssi=-80, s=200) is None, "weak RSSI filtered"
db.set_setting("min_rssi", "-100")

ev = db.evaluate_order(oid)
assert ev["summary"]["progress_pct"] == 100, ev["summary"]
assert {e["reason"] for e in ev["exceptions"]} == {"OVER", "UNEXPECTED", "UNKNOWN"}
assert "E2801" in ev["tag_status"] and ev["tag_status"]["E2801"] == "MATCHED"

done = db.commit_order(oid, reason="demo")
assert done["order"]["status"] == "DISCREPANCY"
assert db.get_setting("active_order_id") is None

# No order: asset seen away from home -> WRONG_LOCATION, and the event knows its home
db.set_reader_location(dept)
e = db.ingest_tag("E2809", now=t0 + timedelta(minutes=5))
assert e["result"] == "WRONG_LOCATION" and e["home_name"] == "Warehouse A", e

d = db.dashboard()
assert d["total_assets"] == 5 and d["wrong"] >= 1 and d["unregistered"] == 1, d
hist = db.asset_history("E2809")
assert [h["location_name"] for h in hist] == ["Gate 1", "Dept B"], hist
assert hist[0]["at_home"] == 0
print("all db tests passed")

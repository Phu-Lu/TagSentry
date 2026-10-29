# TagSentry: RFID Gate Control

A live demo for Zebra fixed readers (FX7500 / FX9600). It reads real tags at a gate, tells
you which way they moved (In / Out), reconciles them against an order, and warns when an
asset is in the wrong place, including where it belongs.

No fake data: assets are registered on screen from real tags during the demo.

## Project status

| Part | File | Status |
|---|---|---|
| Data layer (SQLite, ingest filters, order reconciliation, dashboard queries) | `db.py`, `test_db.py` | Done, self-tested |
| UI layout spec (6 screens, light sidebar, Senitron-style Gate Portal) | `UI_LAYOUT.md` | Approved |
| Server: `/rfid` receiver wired to the data layer, REST API, gate control, CSV export | `server.py`, `test_server.py` | Done, tested with simulated reader POSTs |
| Web UI (six screens, live updates over Socket.IO) | `index.html` | Done, checked in headless Chromium |
| Alert light/buzzer driven by app logic (USB or network relay) | - | Optional, after UI |

Out of scope: LLRP/MQTT direct connection, GPO control from the PC, SKU layer,
Excel import, user roles.

## Architecture

```text
Tag -> Antenna -> Zebra fixed reader
Reader RFID engine / LLRP layer
Directionality Data App (on-reader, pyziotc)  -- determines In/Out from 2x GPI
ZIOT Connector -> HTTP POST (tag data w/ direction, GPI events, heartbeat)
PC Flask app /rfid  ->  db.ingest_tag()  ->  SQLite (gate_demo.db)
Socket.IO + REST  ->  Browser UI (Dashboard, Gate Portal, Assets, ...)
```

This demo uses Zebra IoT Connector HTTP POST, not a direct PC-side LLRP client.
Direction detection happens **on the reader**, inside the `Directionality` Data
App, not in the PC app and not as a plain reader config setting.

The default port is **7500** (like the FX7500). Whatever port you run on, the reader's HTTP POST URL must use the same one; the console prints it at startup.

Do not configure the same GPI trigger in both LLRP GPIO and ZIOT GPIO for this demo.

## Features

- **Dashboard**: total assets, right place / wrong place / unseen, assets in the wrong place, latest events, unregistered tags being read.
- **Gate Portal**: pick an order, Start, watch Scanned vs Expected per line with a progress bar and alerts, then Commit. Stop pauses the reader; Detach stops scanning against the order without committing.
- **Tags in range (live)** on the Gate Portal counts every read report per tag in real time, even the ones the event log skips as duplicates.
- **Assets**: direct EPC mapping (no SKU layer). Register real tags on screen (name, category, color, size, Home Location) or bulk import from CSV. Each asset can have an optional photo, shown as a thumbnail in Assets and in the Image column of the Gate Portal.
- **Locations**: warehouses, gates and departments. "Reader is placed at" simulates the reader at another gate or dept.
- **Orders**: IN or OUT orders with lines by category (count) and/or by specific EPC.
- **History**: filterable event log, CSV export, and a Movement history timeline per asset.

Screen-by-screen wireframes: see `UI_LAYOUT.md`.

## Data model (SQLite)

| Table | Purpose |
|---|---|
| `locations` | id, name, type (WAREHOUSE / GATE / DEPT) |
| `assets` | `epc` (key), name, category, color, size, home location |
| `orders`, `order_lines` | order header (IN/OUT, status) and lines (by CATEGORY with qty, or by EPC) |
| `asset_images` | optional photo per asset (JPEG/PNG/WebP, shrunk to about 320 px in the browser before upload) |
| `events` | one row per logged read: time, EPC, antenna, RSSI, direction, location, order, result |
| `settings` | min RSSI, event interval, reader location, active order |

There is no separate "current location" table. An asset's current location is its latest
event, and its Movement history is all of its events.

Asset status on the Dashboard:

| Status | Meaning |
|---|---|
| Right place | Latest event is at the asset's Home Location |
| Wrong place | Latest event is at any other location |
| Unseen | Never read yet |

## Reconciliation rules

Every distinct tag read during an order session is classified in this order:

1. EPC is not registered: `UNKNOWN`
2. EPC is on an EPC line: `MATCHED`
3. Asset category is on a category line: `MATCHED`, or `OVER` once the count passes the line's quantity
4. Anything else: `UNEXPECTED`

EPC lines are checked before category lines, so a named item is never double counted
into a category line. `OUT` orders ignore reads with direction `In`; `IN` orders ignore
`Out`. A blank direction counts for both.

Commit closes the order as `COMPLETED` (every line done, no exceptions) or `DISCREPANCY`.

With no active order, a registered tag seen away from its Home Location is logged as
`WRONG_LOCATION` and the alert shows where it belongs. A tag that matches the active order is
treated as expected movement and is not flagged.

## CSV import: bring your own template

**Assets -> Import CSV** accepts the CSV layouts you already use in other RFID apps, unchanged. Pick the file and a
**Match columns** step shows the first rows and which file column feeds each field. The guess is made from the column
names (for example `location`, `site` or `dept` become Home location; `asset_id` becomes Asset ID; `serial`, `sn` or
`serial_number` become Serial number; `tag no`, `rfid` or `epc` become EPC), and you can change any of them.

| Our field | Notes |
|---|---|
| EPC | Required, hex only. |
| Name | Required. A new tag with no name is skipped (it stays in the unregistered list). |
| Category, Color, Size | Optional. |
| Home location | Matched by name. A new name creates a location, so `Zebra Office/SaiGon` becomes one location with that exact name. |
| Asset ID, Serial number | Optional first-class fields: shown in the Assets table, searchable, exported. |
| Image (URL) | An `http(s)` link is downloaded (JPEG, PNG or WebP, up to 600 KB, 50 per import). A local path such as `C:\photos\a.jpg` cannot be read by the server and is not imported; add that photo in Edit. |
| Other columns | Any column that matches no field can be kept as a **custom field**: shown in the asset's Edit dialog and added back as a column in the Assets export. |

The mapping is remembered per column layout, so the same template needs no setup the next time. Re-importing an
existing EPC updates it; blank cells never overwrite existing values. CSV, `;` and tab delimiters and a UTF-8 BOM are
accepted. Every row is validated first: if any row is bad, nothing is imported and the dialog lists the rows to fix.

Order lines still use a fixed template (`kind, category, epc, qty`).

**Fast way to register many tags:** on the Assets page, press **Export CSV** in the "Unregistered tags" card. The file
uses our template layout with the EPCs already filled in. Fill in `name` (and optionally the rest) in Excel, then
**Import CSV**. Tags left without a name are skipped.

EPCs must be hex. Excel turns long EPCs into scientific notation (for example `3.0E+23`), so format the EPC column as
Text before saving.

Our template: `/api/templates/assets.csv` (`epc, name, category, color, size, home_location, asset_id, serial_number`) and
`/api/templates/order-lines.csv`.

## CSV export

| Where | File | Contents |
|---|---|---|
| Assets -> Unregistered tags -> Export CSV | `unregistered_tags.csv` | Tags the reader has seen that are not registered yet, in our template layout, with last RSSI, read count and last seen. |
| Assets -> Export CSV | `assets.csv` | Every registered asset: template columns, then current location, status and last seen, then any custom fields. Re-importable as is. |
| Gate Portal / Orders -> Export result | `order_<code>.csv` | One `SCANNED` row per EPC read for the order (name, category, status MATCHED / OVER / UNKNOWN / UNEXPECTED, the line it counted against, first seen, direction, antenna, RSSI, location), plus `MISSING` rows for what is still expected. |
| History -> Export CSV | `gate_events.csv` | The event log, honoring the filters on screen. |

## Event filtering

Applied in `ingest_tag()` before anything is stored:

- Reads with RSSI below `min_rssi` (default -100 dBm) are dropped.
- A tag sitting in front of the reader is not re-logged until its location, direction or
  order changes, or `event_interval_sec` (default 30 s) has passed.

## Run

In the UI, open Settings (gear icon, top right) and enter the reader IP and password once. They are kept in the browser. The Socket.IO client loads from cdnjs; if the PC is offline the UI falls back to refreshing every 3 seconds.

```powershell
pip install flask flask-socketio flask-cors requests
python test_db.py       # data-layer self-test
python test_server.py   # API test with simulated reader traffic
python server.py        # listens on port 7500; the console prints the exact URLs to use
python server.py --port 7600   # another port, if you run several demos (or set TAGSENTRY_PORT)
```

If Flask-SocketIO refuses Werkzeug, the server start line must include:

```python
socketio.run(app, host="0.0.0.0", port=port, debug=False, allow_unsafe_werkzeug=True)
```

The database file is `gate_demo.db` next to the server (override with the `GATE_DB`
environment variable). Delete it to start fresh.

## Directionality Data App (on-reader)

`Directionality` is a Python **Data App (DA)** that runs directly on the reader
via the ZIOT Connector's local app framework (`pyziotc`). It is not a mode you
toggle in the web UI. It's a package (`Directionality_1_0_0.deb`) you install.

### Install

1. Reader Web UI -> **Apps** tab -> upload `Directionality_1_0_0.deb`.
2. The app extracts to `/apps/Directionality_1.0.0/` with `config.ini`,
   `start_Directionality.sh`, `stop_Directionality.sh`.
3. Start it from the Apps tab (or `start_Directionality.sh`). Check reader
   app logs / `Log_Only_To_Console` in `config.ini` if it doesn't come up.

### How direction is actually determined (2 GPI pins, not 1)

The DA runs a small state machine driven by **two** GPI pins. If you only
wire/configure one GPI pin, direction will never resolve correctly.

```text
STATE_WAITING
  GPI1 HIGH first -> Direction = "Out", state -> WAIT_GPI2_ON, startInventory()
  GPI2 HIGH first -> Direction = "In",  state -> WAIT_GPI1_ON, startInventory()
WAIT_GPI(other)_ON
  other GPI goes HIGH -> state -> WAIT_OFF
WAIT_OFF
  both GPI1 and GPI2 back to LOW -> stopInventory(), publish tags with
  "direction" field set, clear tag buffer, state -> WAITING
```

Physically you need two sensors (e.g. two photo-eyes at the gate, triggered in
sequence depending on which way someone walks through) wired to GPI1 and GPI2.
Whichever one trips first sets the direction for that whole pass. Tags collected
between "first GPI on" and "both GPI off" get tagged with that direction and
pushed out as `MSG_OUT_DATA`. This is the JSON the Flask `/rfid` endpoint
receives, and `direction` maps straight into `normalize_direction()` in `server.py`.

### Quick test

1. Watch reader logs while manually toggling GPI1 then GPI2 (or vice versa)
   on the reader's GPIO/status page.
2. Confirm `Direction` flips to `Out` vs `In` depending on order.
3. Confirm tags read during the window show up at `/rfid` with a
   `direction` field populated (not blank).

If direction is always blank: almost always means only one GPI pin is wired
or only one is configured. Go re-check both.

## GPIO / Light Setup (reader-side, static rules)

**This is the only reliable way to drive GPO/lights from the reader.** There is
no supported way for the PC app to flip a GPO in real time over the network
(see "Why the PC can't set GPO directly" below). The reader has to react to
its own GPI on its own, via a rule you configure once in the reader Web UI
under **GPIO Config**.

Recommended default output states:

- `GPO1 = LOW`
- `GPO2 = LOW`
- `GPO3 = LOW`
- `GPO4 = LOW`
- `LED3 = OFF`

If default `GPO1 = HIGH`, the red light can stay on constantly.

Example reader-side light rule:

- Event: `GPI`
- Number: `1`
- State: `HIGH`
- Entity: `GPO1`
- State: `HIGH`
- Post Action State: `LOW`
- On: `100 ms`
- Off: `100 ms`
- Blink Duration: `1000 ms`

For first test, leave conditions unchecked. After it works, optionally add
`Radio Ongoing`.

Because the Directionality DA is already installed and using GPI1/GPI2 for
direction, keep any GPIO Config light rules on **separate, unused GPI/GPO
pins** so they don't fight over the same inputs.

## Why the PC can't set GPO directly

The `/cloud/gpo` (and `/cloud/config`, `/cloud/mode`, `/cloud/pass-through`)
endpoints are only ever called from `RestAPI.py` against
`http.client.HTTPConnection("127.0.0.1")`, i.e. they are **loopback-only**,
reachable solely by a Data App running on the reader itself. They are not
the same thing as `/cloud/start`, `/cloud/stop`, `/cloud/status`,
`/cloud/version`, which genuinely are public HTTPS endpoints with JWT auth
(that's why reader start/stop and status polling from the PC app work fine,
while every guessed `/cloud/gpo` URL variant 404s).

So the app does not try to guess GPO endpoints over the network: that call is
guaranteed to fail by design. If real-time PC-triggered GPO is needed later, it
has to be a Data App on the reader (calling `setGPO`/`setFastGPO` locally), not a
remote REST call. Treat that as a separate project so it doesn't collide with
the running Directionality app.

For a reliable demo light:

- Use the reader-side GPIO Config rule above for the physical light.
- Use the PC app UI for alerts (visual/audio in the browser, not a physical GPO).
- If the light must follow app logic exactly (order exceptions, wrong location,
  not just raw GPI), use a PC-controlled USB or network relay instead of reader GPO.

## Reader Interface Setup

In reader Web UI, configure:

- `Tag Data Interface 1`: HTTP POST
- URL: `http://<PC_IP>:7500/rfid`
- Data format: JSON
- `Management Events Interface`: HTTP POST
- URL: `http://<PC_IP>:7500/rfid`
- Format: JSON if available

Management Events are needed if the app should show raw GPI status.

## Troubleshooting

### App Shows GPI Waiting

If UI shows:

```text
GPI 1 Waiting
GPI 2 Waiting
```

Check:

1. Management Events Interface is set to HTTP POST.
2. URL is `http://<PC_IP>:7500/rfid`.
3. GPI physically changes in reader GPIO/status page.
4. Sensor has correct power and wiring.
5. Both GPI1 and GPI2 are wired and configured. Directionality needs both,
   not just one.

### Direction Always Blank on Tags

- Confirm both GPI1 and GPI2 are physically wired and triggering (check
  reader GPIO/status page directly, independent of the app).
- Confirm the Directionality DA is actually running (Apps tab / process
  status), not just installed.
- Confirm `Tag Data Interface` output includes the `direction` field. It's set
  by the DA (`tag["direction"] = Direction`) before publish, so if the DA isn't
  running you'll get tag data without direction at all.

### Tags Appear in History but Not in Assets

Expected for new tags. Unregistered EPCs are logged as `UNKNOWN` and listed under
"Unregistered tags" on the Assets page. Register them there.

### Delete Test Data

Settings (gear icon) -> "Delete data": clear reads and history only, or clear everything (assets, orders, locations, history). Reader IP and thresholds are kept. Asks for confirmation.

### Tag Is Read but Nothing New Shows Up

The ingest filter dropped it: RSSI is below `min_rssi`, or the tag is stationary and
`event_interval_sec` has not passed. Lower `min_rssi` or move the tag to trigger a
direction or location change.

### Wrong-Location Alert Shows the Wrong Place

Check "Reader is placed at" on the Locations page. Every new event is stamped with that
location, so it must match where the reader physically is.

### Reader Web UI Shows Invalid JSON Object Received

Usually one required ZIOT field is blank or the output format is not JSON.

Check:

- Trigger Interval is not blank.
- HTTP POST output format is JSON.
- Target URL is valid.
- Flask app is running.

### Red Light Constantly On

Check:

1. GPO default should be `LOW`.
2. Confirm NO/NC wiring on relay/light.
3. Confirm light is not powered always-on independent of GPO.
4. Confirm the reader-side GPIO Config rule (not a PC call) is what's
   driving it. See "Why the PC can't set GPO directly" above.

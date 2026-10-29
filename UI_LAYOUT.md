# TagSentry: UI Layout Spec

Approved wireframe, Senitron-inspired. Build order: backend first, UI last.
Placeholders in `[brackets]` are filled from live data; the demo never ships fake or seeded data.

## 1. Global shell

```text
+------------+--------------------------------------------------------------+
| TagSentry  | Home / <Page>            [Reader at: <Location>] [Reader ●]  |  <- top bar
|            +--------------------------------------------------------------+
| Dashboard  |                                                              |
| Gate Portal|                        PAGE CONTENT                          |
| Assets     |                                                              |
| Locations  |                                                              |
| Orders     |                                                              |
| History    |                                                              |
+------------+--------------------------------------------------------------+
```

| Element | Spec |
|---|---|
| Sidebar | 140px wide, **light** background (surface-1), hairline right border, secondary-colored text. Active item: light accent tint, accent text, 3px accent left border. Dark sidebar was rejected as too dark. |
| Top bar | Breadcrumb (left, flexible), chip "Reader at: [Location]" (accent), chip reader status (green online / red offline). Same bar on every page. |
| Content | Padding 12px. One page visible at a time, switched from the sidebar. |
| Theme | Follows the system light/dark mode through CSS variables. No hardcoded text colors. |

### Status vocabulary (use everywhere)

| Chip color | Meaning | Values |
|---|---|---|
| Green | OK | Online, Right place, Done, Completed, OK |
| Amber | Attention | Unseen, Over-counted, Unregistered tags |
| Red | Exception | Wrong place, Unexpected Item, Unknown, Discrepancy |
| Blue | Info / pending | Open order, Pending line, Reader location |

## 2. Dashboard

Purpose: the at-a-glance picture a customer sees first.

```text
[ Total assets ] [ Right place ] [ Wrong place ] [ Unseen ]      <- 4 metric cards
+-----------------------------+  +-----------------------------+
| Assets in the wrong place   |  | Latest events               |
| Asset | Home | Now          |  | Time | Asset | Event        |
+-----------------------------+  +-----------------------------+
| Unregistered tags being read: [n]   -> opens Assets             |
+-----------------------------------------------------------------+
```

Data: `db.dashboard()` (counts, `wrong_list`, `recent_events`, `unregistered`).

## 3. Gate Portal (main screen)

Modeled on the Senitron Transfer-slip screen.

```text
[Order: <select>] [Start] [Stop] [Clear]                [Commit order]
+-----------------------------------------------------------------+
| ! Alert: Unexpected Item / Over-counted / Wrong location        |  <- red/amber banner
+-----------------------------------------------------------------+
|  0            | Scanned | Expected | Type / EPC | Name | Status  |
|  Total tags   |   big   |          | By category|      | Done    |
|  [progress %] |   big   |          | By category|      | Over    |
|  Last dir:Out |   big   |    1     | By EPC     |      | Pending |
|               |   big   |    -     | [EPC]      |      | Unexpected |
(last column: Image, the asset photo thumbnail; dashed box when none)
+-----------------------------------------------------------------+
| Reference / Reason (notes saved on commit)                      |
+-----------------------------------------------------------------+
```

| Rule | Behavior |
|---|---|
| Scanned column | Large number (Senitron style), one row per order line plus one row per exception tag. |
| Start / Stop | Start sets the active order and starts the reader. Stop pauses reading. |
| Commit | Locks the order: `COMPLETED` if every line is done and there are no exceptions, otherwise `DISCREPANCY`. |
| Alert banner | Red for Unknown / Unexpected, amber for Over-counted, wrong-location shows the item's Home. |
| Direction | Last direction chip (In / Out) from the Directionality Data App. |

Data: `db.evaluate_order()`, `db.commit_order()`, live events over Socket.IO.

## 4. Assets

```text
[+ Register tag] [Import CSV] [ Search EPC or name ........ ]
+-----------------------------------------------------------------+
| Unregistered tags (live from the reader)                        |
| EPC | RSSI | Last read | [Register]                             |
+-----------------------------------------------------------------+
| EPC | Name | Category | Home | Now | Status                     |
+-----------------------------------------------------------------+
Click a row -> Movement history of that asset
```

Each asset has an optional photo (thumbnail column on the left; set in the Register/Edit dialog). Registering a tag happens on screen with a real tag: pick it from the unregistered list, enter name, category, Home Location, save.

Data: `db.list_assets()`, `db.unregistered_tags()`, `db.register_asset()`.

## 5. Locations

```text
+-----------------------------------------------------------------+
| Reader is placed at: [ <location select> ]                      |
| Changing this simulates the reader at another gate or dept.     |
+-----------------------------------------------------------------+
[+ Add location]
| Name | Type | Assets (Home) | Assets (currently here) |
```

Types: Warehouse, Gate, Dept. The "Reader is placed at" value is stamped on every new event.

Data: `db.list_locations()`, `db.set_reader_location()`.

## 6. Orders

```text
[+ Create order] [In] [Out]
| Code | Type | Status | Progress |
+-----------------------------------------------------------------+
| Lines of the open order                                         |
| [+ Add by category] [+ Add by EPC]                              |
| Kind | Category / EPC | Qty |
+-----------------------------------------------------------------+
```

| Line kind | Fields | Matching |
|---|---|---|
| By category | category, quantity | Counts registered assets of that category; beyond quantity is Over-counted. |
| By EPC | one registered EPC | Matched exactly; checked before category lines so it is never double counted. |

"Add by EPC" opens a picker of registered assets (no typing EPCs by hand). Lines are editable only while the order is Open.

Data: `db.create_order()`, `db.add_line()`, `db.list_orders()`.

## 7. History

```text
[ Search EPC/name ] [Location v] [Result v] [Export CSV]
+---------------------------------------+  +----------------------+
| Time | EPC | Ant | Dir | Location | R |  | Movement: <asset>    |
|                                       |  |  o Warehouse A  Home |
|                                       |  |  o Gate 1       Out  |
|                                       |  |  o Dept B  wrong     |
+---------------------------------------+  +----------------------+
```

Left: filterable event log. Right: timeline for the asset selected in the log or on the Assets page.

Data: `db.query_events()`, `db.asset_history()`.

## 8. Build notes

- Backend: Flask + Socket.IO, SQLite (`db.py`). The existing `/rfid` receiver, heartbeat, GPI handling and reader start/stop/status polling stay and feed `db.ingest_tag()`.
- UI labels in English. The UI is built last, after the API.
- Out of scope for now: LLRP/MQTT, GPO control from the app (relay later), SKU layer, Excel import, user roles.

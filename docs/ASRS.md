# ASRS simulation

The **ASRS simulation** area ports the existing fixed-aisle mini-load crane model into the web application. It reads a deployed warehouse through its authenticated integration API, pins an immutable published release, and keeps its own inventory and stock-flow ledger. Python and Pygame are not runtime dependencies. Layout and slotting calculations continue to use the C++ engine; event scheduling runs in a dedicated TypeScript worker.

## Prepare and run

1. Prepare a **Mini-load ASRS** warehouse in the planning workflow. Import `resources/map/asrs/asrs_grid.grid.json` for the supplied nine-aisle map. Calculate stock requirements and generate a fully assigned, validated layout.
2. In **Data & inventory**, register/synchronize the warehouse with the correct time zone and publish a release.
3. Import the original order workbook or CSV if its dataset has only aggregates. Exact source rows, signed EA quantities and references are required for stock-flow traceability.
4. Open **ASRS simulation → Connect**. The current server supplies a temporary read-only integration key automatically. For another deployed warehouse, supply its origin and a read API key; use a trusted SSH tunnel or protected deployment connection. API keys are kept in memory and are never written into simulation configuration or logs.
5. Choose the published warehouse and order dataset. Choose one observed date, several observed dates, one month, several months, or the full dataset. Date/month choices come exclusively from the input calendar; month selections include only observed dates in those months.
6. Set replenishment lead time and review motion settings. Press **Prepare simulation**. Preparation verifies the release checksum and copies selected original lines through the API. Review the pinned version, opening quantities and physical-data warnings, then press **Start**.
7. Change speed during playback, pause/resume, inspect the 3D or 2D view, select a crane, and use the stock-flow, order-line and tote-balance tables. **Stop** retains an explicitly incomplete ledger; use **Pause** when you intend to resume.

## Timing and scheduling

Each workstation owns one crane serving its two adjacent rack rows. Rack rows cannot be shared between cranes. The ported model supports one SKU in one single-slot tote per assignment. Unsupported multi-slot loads, missing slot geometry and unreachable rack rows fail preparation with an explanation.

Default motion assumptions match the original ASRS configuration: X 3 m/s and 1 m/s², Y 0.5 m/s and 0.5 m/s², Z 1 m/s and 0.5 m/s²; picking/refill handoff is 10 seconds. Additional extraction/replacement dwell defaults to zero. These are editable engineering assumptions. Manufacturer calibration is not supplied.

X rail travel and Z lift travel start together; their longer axis duration sets the travel time. Y forks extend/retract only while rail/lift travel is stopped. Acceleration equals deceleration. For distance `d`, speed `v`, and acceleration `a`, duration is `2√(d/a)` when `d ≤ v²/a`, otherwise `d/v + v/a`. Rendering samples the same analytical phases as event processing.

Each pick retrieves a tote, presents it at its own workstation, consumes the fulfilled EA quantities at the end of picking, and returns it to the same slot. The next cycle starts from the last return position. A presentation consolidates matching lines in one store/SKU batch. Oversized orders are split across presentations; another assigned tote is used when available before emergency replenishment. Allocation chooses estimated crane completion time, then crane ID/address; jobs in the active store batch use nearest-next delivery. This is a deterministic greedy heuristic.

Store/day batches are released at **local midnight**, preserving the original simulator’s workload convention. Source clock times are retained for auditing and do not control arrivals. Selected dates share continuous stock and queues; stock is never reset at midnight. Later selected dates arrive on their own calendar dates, including when earlier work is still queued. Unselected dates generate no demand. Timestamps are warehouse-local civil ISO strings without `Z`, accompanied by the warehouse IANA time zone. Calendar days use 86,400 simulation seconds; daylight-saving clock changes are not modeled.

Playback accepts **1× or any larger finite speed**, plus **Max**. Max advances between events as fast as CPU and durable audit storage allow. Work runs in short worker batches so controls and the HTTP server remain responsive. Speed and browser frame rate do not change logical event order or stock results. At high speed, views sample current state rather than attempting to display every movement. **Skip idle waiting** skips wall-clock playback delays only when all cranes are idle; full simulated lead times/calendar gaps remain in timestamps and metrics. Disable it for continuous real-time playback at 1×.

## Stock and replenishment

Opening quantities come from **ASSIGNED** rows of the pinned slotting release, one opening entry per tote. They are simulated quantities, independent of actual warehouse counts. The SKU target is the sum actually assigned; the threshold comes directly from `planning_inputs.stock[].minimum_buffer_stock_ea`. Missing thresholds require recalculating stock and republishing.

After picks, when total stock in a SKU’s assigned totes is **strictly below** its buffer, depleted totes receive coalesced refill requests. Each tote is filled back to its own original assigned quantity; other already-stocked totes are untouched. Requests are not duplicated while queued or executing. If all usable assigned stock is exhausted with demand still queued, emergency replenishment also prevents a zero-buffer deadlock. A partially depleted tote is never filled beyond its original quantity.

Replenishment preparation takes the configured lead time (default 60 seconds). A crane then retrieves the tote, takes it to its workstation, performs the refill handoff and returns it to its original slot. Requests do not add stock; confirmed workstation refill events do. Returned goods in receiving are used first; any remaining refill quantity comes from **unlimited external supply**. Supplier stockouts, procurement schedules and conveyor/station sharing are not modeled.

Positive source quantities are outbound demand. Negative quantities are modeled as returns into receiving; zero quantities are recorded as no movement. Returned stock is put away only within existing assigned capacity, with excess retained in receiving. Demand without an accessible assigned tote is recorded as unfulfilled and produces **completed with shortages**, rather than fabricated stock or a false successful fulfillment. Positive lines complete after the tote has returned and the forks have retracted; partial picks remain traceable before completion.

Quantities use exact integer millionths of EA. Each persisted batch checks `on hand = opening + external replenishment + returns − outbound`. Receiving-to-tote putaway and tote transport have zero overall on-hand delta. No simulator request posts to the real warehouse inventory API.

## Trace stock flow

The immutable ledger records opening stock, outbound picks, replenishment requests and receipts, returns, internal putaway, tote pickup/workstation/departure/return, zero lines and unfulfilled demand. It retains sequence, simulated local timestamp, source date/time and original file row, order reference, store, SKU, tote load, crane/cycle, quantity, exact inventory deltas, load/SKU balances and origin/destination.

An order line can appear in several outbound movements when split; summing its outbound quantities gives its fulfilled amount. Internal movements report transported quantity with zero global stock delta. Replenishment events retain the source line that triggered the request. Transport for a consolidated presentation can span several lines; its cycle ID joins it to the corresponding outbound events. Source references and source times can be blank if the original file did not provide them.

Filter by exact SKU, source row and movement kind. Follow the latest movements, or browse the complete ledger from the beginning. CSV exports include **all matching records**, beyond the current page, plus run/release/dataset IDs and time zone. The order-line view separately records fulfilled/returned quantities, status and unmet quantities. Tote balances include SKU buffer/initial-target summaries.

## Persistence and local endpoints

Runtime simulation data lives in **`.data/asrs.sqlite`** (or `DATA_DIR/asrs.sqlite`) alongside the warehouse’s `app.sqlite`. Inputs, pinned release, stock, events, completed cycles and checkpoints persist. A checkpoint and its stock-flow writes commit in one SQLite transaction. After a crash, an interrupted running simulation becomes paused and resumes from its last committed event without posting twice. Interrupted preparations become failed and must be prepared again. One simulation runs/prepares at a time; previously prepared or paused runs remain available.

Run provenance also records the simulation engine version. A run from a different algorithm version requires preparing a new run; saved results remain readable.

One application process may own a simulation database. Another server using the same `DATA_DIR` is refused before recovery can disturb an active run; use separate data directories for concurrent deployments. A stale owner left by a terminated process is reclaimed on startup.

These endpoints belong to the trusted local management application and use the same host/origin protection as its UI:

| Endpoint                             | Purpose                                                                                               |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------- |
| `POST /api/asrs/connect`             | Connect using `{base_url,token}`; local source needs no pasted key                                    |
| `POST /api/asrs/calendar`            | Read a dataset calendar using source connection and `dataset_id`                                      |
| `GET /api/asrs/runs`                 | Most recent 100 saved run headers                                                                     |
| `POST /api/asrs/runs`                | Prepare with source, warehouse/dataset IDs, name, selection and settings                              |
| `GET /api/asrs/runs/{id}`            | Status, provenance, progress, current crane/stock metrics                                             |
| `GET /api/asrs/runs/{id}/model`      | Pinned release and validated crane/tote model                                                         |
| `POST /api/asrs/runs/{id}/control`   | `{action:resume\|pause\|stop\|speed}`; speed accepts numeric ≥1 or `"max"`; optional `skip_idle`      |
| `GET /api/asrs/runs/{id}/events`     | Stock-flow cursor feed: `after,limit,sku,source_row,kind`; `latest=1` samples latest matching records |
| `GET /api/asrs/runs/{id}/orders`     | Source-line cursor feed: `after,limit,sku,status,date`                                                |
| `GET /api/asrs/runs/{id}/stock`      | Current tote balances; optional exact `sku`                                                           |
| `GET /api/asrs/runs/{id}/events.csv` | Stream every matching stock-flow record                                                               |
| `GET /api/asrs/runs/{id}/orders.csv` | Stream every matching source order line and fulfillment status                                        |

Selections use `{mode:"one_date",dates:["2023-01-03"]}`, `multiple_dates`, `{mode:"one_month",months:["2023-01"]}`, `multiple_months`, or `{mode:"full"}`. The settings contract and defaults are in `shared/asrs.ts`. The deployed warehouse’s authenticated calendar and cursor APIs are documented in [API.md](API.md).

For a complete backup, stop the server and archive the entire runtime data directory, including **both SQLite databases** and original uploads. The warehouse Overview database-only backup covers `app.sqlite`; it does not include the separate simulation ledger. Simulation results are runtime data and are not uploaded to Git.

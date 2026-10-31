# Warehouse integration API

The web application prepares warehouse geometry, inventory data and slotting. External warehouse systems consume published releases and may post confirmed inventory operations. The [ASRS simulation](ASRS.md) consumes this API and maintains separate simulated stock. Integration inventory operations represent confirmed real operations; they do not control equipment motion.

## Historical data preparation

Use **Historical data & analysis** before the four generation steps. **Data preparation** imports orders, bounds dates to the dataset, calculates demand and lists SKUs missing positive dimensions (metres) or weight (kilograms). **SKU velocity** provides ABC filtering, sorting and CSV export. **SKU affinity** explores a selected SKU and its strongest store-day co-order neighbors, with a minimum shared-day threshold, a displayed neighbor limit, keyboard navigation, a sortable table and SVG export. The graph queries all calculated pairs, including pairs outside the legacy top-100 relationship list.

**Save SKU details** atomically updates the current workspace and SKU master, preserving existing names, units and other master attributes. It replaces embedded imported physical requirements and invalidates the workspace's stock calculation and layout. **Proceed without changes** acknowledges incomplete profiles for the current inputs; changed demand or physical data requires another review. An assignment with incomplete physical data remains physically unverified.

The local-only `POST /api/manage/workspaces/{workspaceId}/sku-details` accepts `sku`, `values` (positive physical attribute numbers), `workspace_revision` and `master_revision` (or `null` for a new master SKU). It returns the updated workspace and rejects stale workspace or master revisions with HTTP 409. The integration API does not expose this planning edit endpoint.

## Connect

1. Start the application and open **Data & inventory → Overview**.
2. Set the warehouse IANA time zone (for example `Asia/Kuala_Lumpur`), then register the current workspace. This registers locations and imports missing planning SKUs; it does not create operational stock.
3. Record verified opening stock on **Inventory**, or import a CSV with `sku,quantity,location_id`. Optional columns are `uom,lot,expiry,status,container`.
4. Complete the layout workflow, synchronize the warehouse, then publish on **Publish**. Publication reruns C++ validation and rejects stale workspace revisions and changed master attributes.
5. Create a named integration key on **Integration**. Copy the secret once. `read` keys retrieve data; `operate` keys can also post inventory operations. Revocation takes effect immediately.

All integration requests require `Authorization: Bearer YOUR_API_KEY`. The server listens on loopback; use a trusted SSH tunnel for remote connections. The management UI and `/api/manage/` endpoints trust local access. Do not expose them through a public proxy; there is no public multi-user login service.

The machine-readable contract is served at **`GET /api/openapi.json`**.

## Read configuration and inventory

| Endpoint                                                 | Result                                                                          |
| -------------------------------------------------------- | ------------------------------------------------------------------------------- |
| `GET /api/v1/warehouses`                                 | Registered warehouses and their draft configuration hashes                      |
| `GET /api/v1/warehouses/{id}/published`                  | Current published release; retain its release ID                                |
| `GET /api/v1/releases/{releaseId}`                       | Complete immutable release                                                      |
| `GET /api/v1/releases/{releaseId}/warehouse`             | Pinned geometry, locations, equipment and container snapshot                    |
| `GET /api/v1/releases/{releaseId}/slotting`              | Pinned assignments, unassigned reasons and validation details                   |
| `GET /api/v1/releases/{releaseId}/inventory`             | Operational inventory as it was at publication                                  |
| `GET /api/v1/releases/{releaseId}/skus`                  | Pinned SKU master and UOM conversions                                           |
| `GET /api/v1/warehouses/{id}/inventory?q=SKU`            | Current operational inventory                                                   |
| `GET /api/v1/warehouses/{id}/inventory-snapshot`         | Consistent inventory plus its event-feed cursor                                 |
| `GET /api/v1/warehouses/{id}/locations`                  | Registered storage, receiving, dispatch and workstation locations               |
| `GET /api/v1/warehouses/{id}/containers`                 | Container locations and contents, each in its SKU's base UOM                    |
| `GET /api/v1/warehouses/{id}/equipment`                  | Equipment IDs and declared capabilities                                         |
| `GET /api/v1/warehouses/{id}/tasks`                      | Delivery, replenishment and relocation tasks with purpose and stock lines      |
| `GET /api/v1/warehouses/{id}/reservations`               | Outbound and task reservations                                                  |
| `GET /api/v1/warehouses/{id}/events?after=0&limit=200`   | Immutable events in ascending sequence order                                    |
| `GET /api/v1/warehouses/{id}/releases`                   | Published version history                                                       |
| `GET /api/v1/skus?q=SKU`                                 | Current SKU master and SKU-specific UOM conversions                             |
| `GET /api/v1/partners`                                   | Store and supplier master data                                                  |
| `GET /api/v1/datasets`                                   | Historical demand datasets and retained-line availability                       |
| `GET /api/v1/datasets/{id}/lines`                        | Parsed original order lines, including signed quantities and source row numbers |
| `GET /api/v1/datasets/{id}/daily`                        | Daily SKU aggregates used by the layout engine                                  |
| `GET /api/v1/datasets/{id}/baskets`                      | Store/day SKU groups used by movement estimates                                 |
| `GET /api/v1/datasets/{id}/calendar`                     | Observed dates/months and retained-line count                                   |
| `GET /api/v1/datasets/{id}/line-feed?after=0&limit=5000` | Retained lines using a stable original-source-row cursor                        |

Dataset views accept `limit` (1–500), `offset`, `sku`, `start` and `end` filters and return `{rows,total,limit,offset}`. Source clock times retain source-local clock values; no warehouse timezone is inferred. Original import files remain the source for unmodeled columns. Legacy datasets retain their aggregates; reimport the original workbook to retain line records.

The separate `line-feed` accepts `after` (last processed `source_row`, initially 0), `limit` (1–5000), and optional `start`/`end` calendar filters. It returns `{rows,next_after,done}`. Continue with `next_after` until `done` is true. It reads immutable, ready datasets without repeated offset/count scans and requires retained original lines. The `calendar` response is `{dataset_id,raw_count,dates,months}`; dates/months contain only values observed in the source. Warehouse summaries also include system type and current published release ID/version.

Authenticated server-to-server `/api/v1/` requests can arrive through a forwarded host/port (for example an SSH tunnel). Bearer authentication is still required; management endpoints retain strict local-host checks, and foreign browser origins remain rejected.

Release sections return `{release_id,version,checksum,units,data}`. A release checksum is SHA-256 of the complete bundle **excluding `checksum`**, serialized as compact UTF-8 JSON with recursively alphabetically sorted object keys and unchanged array order. Numbers and strings use ECMAScript `JSON.stringify` serialization; for example, `1.0` is serialized as `1`. Later inventory transactions and draft edits do not change existing releases.

Lengths use metres, weights use kilograms and event timestamps use ISO 8601 UTC. Expiry dates are evaluated in `warehouse.time_zone`; API registrations default to UTC when no zone is provided. The source clock times in historical datasets must be interpreted in the appropriate source zone by the consuming system. SKU and container codes are text. Inventory postings accept decimal quantity **strings** with up to six decimal places and convert them using the selected SKU's UOM. `quantity_micro` and `reserved_micro` are exact millionths of a base unit; use these or `quantity_exact`, `reserved_exact` and `available_exact` for stock accounting. Numeric quantity fields are provided for display.

`available` excludes reservations, expired or blocked stock, failed physical item checks, and stock in transit or dispatch. `ITEM_CHECKED` covers declared item dimensions/requirements only. `UNVERIFIED` means information is incomplete or the location has no storage profile. Neither status certifies container packing, total rack loading or equipment safety.

Location IDs are stable within a warehouse configuration. Location details include the engine's storage address, handling-unit ID, buffer ID and dynamic address so assignments can be joined to geometry. Registering a changed geometry is blocked while operational stock is present. Use a new workspace/configuration when preparing a different warehouse structure.

## Post inventory operations

Send `POST /api/v1/warehouses/{id}/inventory` with JSON and an `operate` key:

```json
{
  "request_id": "receipt-2026-0001",
  "kind": "receipt",
  "reason": "Goods receipt GR-0001",
  "sku": "001234",
  "quantity": "12",
  "uom": "EA",
  "location_id": "RECEIVING",
  "lot": "LOT-001",
  "container": "BIN-001",
  "status": "available"
}
```

Use a new `request_id` for each operation. Retry an uncertain request with the **identical body and same ID**: it returns the original result without posting twice. Reusing the ID with a different body returns `409`. The actor is recorded from the integration key's name.

| `kind`                         | Required operation fields                                                                           |
| ------------------------------ | --------------------------------------------------------------------------------------------------- |
| `opening`, `receipt`, `return` | `sku,quantity,location_id`; optional `uom,lot,expiry,status,container`                              |
| `adjustment`                   | `position_id,quantity`; quantity is the signed difference from the observed stock count             |
| `transfer`                     | `position_id,quantity,destination`; confirms an already completed loose-stock transfer              |
| `container_transfer`           | `container,destination`; transfers all unreserved contents together                                 |
| `status`                       | `position_id,quantity,status`                                                                       |
| `reserve`                      | `position_id,quantity`; reserves a selected position for the order named in `reason`                |
| `reserve_sku`                  | `sku,quantity`; allocates eligible stock by earliest expiry, with undated stock last                |
| `dispatch`                     | `reservation_id`; confirms shipment and removes its reserved stock                                  |
| `cancel_reservation`           | `reservation_id`                                                                                    |
| `task_create`                  | `position_id,quantity,destination`; optional `task_type` classifies the movement; a container task moves its full contents |
| `task_pickup`                  | `task_id`; confirms collection and moves all task stock into transit                                |
| `task_complete`                | `task_id`; confirms delivery, including recovery of an interrupted task                             |
| `task_cancel`                  | `task_id`; releases a task that has not been picked up                                              |
| `task_fail`                    | `task_id`; records an interruption while keeping stock in transit                                   |
| `task_redirect`                | `task_id,destination`; validates a recovery destination before delivery                             |

All operations require `request_id` and a nonempty `reason`. Position operations optionally accept `expected_revision` to reject a stale count or stock selection. Returns default to quarantine. Expired stock cannot be made available. Reservations belonging to movement tasks must be managed through task actions.

For `task_create`, supply `task_type: "delivery"` for a `DEL-<uuid>` ID or `"replenishment"` for an `RPL-<uuid>` ID. Explicit `"relocation"` generates `MOV-<uuid>`; omitting the field retains legacy relocation UUIDs. The response includes `task_id` and `task_type`. Use the full returned ID for pickup, completion, cancellation, failure and redirection. Purpose and ID remain stable throughout the task and on identical retries. Task lists and transaction history expose the same identity; existing task IDs remain valid. Task completion confirms movement into the chosen location; outbound dispatch remains a separate inventory operation.

Transfers update source and destination atomically. Whole-container operations preserve all SKU/lot contents. While a container belongs to an active task, ordinary receipts/transfers cannot alter its contents or bypass its location. A failed task retains its stock until an explicit recovery delivery. Equipment routing and execution remain the consuming system's responsibility.

Start synchronization from a published release or `/inventory-snapshot`, using its `inventory_event_sequence` as the initial cursor. The inventory and cursor are read in the same database transaction. Poll the event feed using the last processed `sequence` as `after`. Continue paging until fewer than `limit` events are returned. Retain your processed sequence durably; repeated reads do not mutate stock. Each event line contains its stable position ID, SKU/location/lot/container metadata and exact quantity/reservation deltas in millionths. Corrections are new events rather than edits to old transactions.

Errors return `{ "error": "message" }`: `400` invalid input, `401` invalid/revoked key, `403` insufficient scope, `404` missing record, `409` conflicting revision, idempotency key or stock availability.

## Persistence and recovery

The runtime database is `.data/app.sqlite` (or `DATA_DIR/app.sqlite`). On first startup, existing workspace and asset JSON files are copied into SQLite; originals are preserved, and migration is not repeated after it completes. Workspaces, normalized planning inputs, calculation snapshots, datasets, jobs, releases, API-key hashes and inventory records persist across restarts. Interrupted jobs are marked failed rather than presented as still running.

Use **Overview → Create backup** for a consistent database snapshot in `.data/backups/`. For complete deployment backup, stop the server and archive the entire `.data/` directory, including original uploads. To restore a database-only backup, stop the server, retain a copy of the current data directory, replace `app.sqlite` with the backup and remove the old database's `app.sqlite-wal` and `app.sqlite-shm` sidecars before restarting. Retain original uploads separately. Runtime databases, backups and generated reports are excluded from Git.

Master data can be bulk imported from **Master data**. SKU CSVs require `sku`; supported additional columns are `name,base_uom,max_item_length,max_item_width,max_item_height,max_item_weight,chilled,tablet,flammable,active`. Only supplied fields are updated; empty physical fields clear an unknown measurement. UOM CSVs use `sku,uom,factor`. Partner CSVs use `id,name,kind,contact,workstation_id`, with `kind` set to `store` or `supplier`. Each import validates and commits as one transaction. Planning currently expects EA-base SKUs; configure package units such as CTN as conversions from EA.

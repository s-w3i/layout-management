import { useEffect, useState } from "react";
import {
  MousePointer2,
  RectangleHorizontal,
  MapPin,
  PaintBucket,
  Eraser,
  Move,
  Undo2,
  Redo2,
  Link2,
  Trash2,
  Download,
} from "lucide-react";
import type { Row } from "../../shared/model";
import { MapCanvas } from "./MapCanvas";
import { Field, Panel, UploadButton, count } from "./components";
import { syncBuffers } from "./warehouse";
import { download } from "./api";

type Props = {
  project: Row;
  geometry: Row | null;
  onChange: (project: Row) => void;
  onImport: (file: File, kind: string) => void;
  selected: string;
  onSelect: (id: string) => void;
  disabled: boolean;
  onError: (message: string) => void;
};
const isPoint = (value: Row, node: Row) =>
  value.column === node.column && value.row === node.row;
export function WarehouseStep({
  project,
  geometry,
  onChange,
  onImport,
  selected,
  onSelect,
  disabled,
  onError,
}: Props) {
  const [tool, setTool] = useState("select"),
    [zone, setZone] = useState("Z01"),
    [tab, setTab] = useState("storage");
  const [past, setPast] = useState<Row[]>([]),
    [future, setFuture] = useState<Row[]>([]),
    [laneStart, setLaneStart] = useState<Row | null>(null);
  const [column, setColumn] = useState(0),
    [row, setRow] = useState(0),
    [role, setRole] = useState("rack"),
    [endpoint, setEndpoint] = useState("");
  const [pointX, setPointX] = useState(0),
    [pointY, setPointY] = useState(0);
  const [gridDraft, setGridDraft] = useState<Row>({ ...project.grid });
  useEffect(() => setGridDraft({ ...project.grid }), [project.grid]);
  const node = (geometry?.nodes ?? []).find((n: Row) => n.id === selected),
    rack = (geometry?.racks ?? []).find((r: Row) => r.id === selected);
  const zones = [
    ...new Set<string>(Object.values(project.zone_assignments ?? {})),
  ].sort();
  useEffect(() => {
    if (node) {
      setColumn(node.column);
      setRow(node.row);
      setRole(node.role || "rack");
      setEndpoint(node.endpoint || "");
      setPointX(node.x);
      setPointY(node.y);
      if (rack) setZone(rack.zone);
    }
  }, [selected, node?.x, node?.y]);
  const commit = (p: Row, refresh = true) => {
    try {
      const next = refresh ? syncBuffers(p) : p;
      setPast((previous) => [...previous.slice(-19), project]);
      setFuture([]);
      onChange(next);
    } catch (error) {
      onError(String((error as Error).message));
    }
  };
  const paint = (nodes: Row[], action: string) => {
    if (disabled) return;
    if (action === "lane") {
      const n = nodes[0];
      if (!laneStart) {
        setLaneStart(n);
        onSelect(n.id);
        return;
      }
      if (laneStart.id === n.id) return;
      const p = structuredClone(project),
        start = { column: laneStart.column, row: laneStart.row },
        end = { column: n.column, row: n.row };
      p.added_lanes ??= [];
      p.added_lanes.push({ start, end });
      p.deleted_lanes = (p.deleted_lanes ?? []).filter(
        (l: Row) =>
          !(
            (l.start.column === start.column &&
              l.start.row === start.row &&
              l.end.column === end.column &&
              l.end.row === end.row) ||
            (l.end.column === start.column &&
              l.end.row === start.row &&
              l.start.column === end.column &&
              l.start.row === end.row)
          ),
      );
      commit(p);
      setLaneStart(null);
      return;
    }
    const p = structuredClone(project),
      chosen = new Set(nodes.map((n) => n.id));
    p.zone_assignments ??= {};
    if (
      action === "rack" ||
      action === "workstation" ||
      action === "erase" ||
      action === "delete-point"
    )
      p.markers = p.markers.filter(
        (m: Row) => !chosen.has(`G${m.column}_${m.row}`),
      );
    for (const n of nodes) {
      if (action === "rack" || action === "workstation") {
        p.markers.push({
          column: n.column,
          row: n.row,
          role: action,
          endpoint_id: `${action === "rack" ? "RACK" : "WS"}_${n.column}_${n.row}`,
        });
        if (action === "rack") p.zone_assignments[n.id] = zone;
        else delete p.zone_assignments[n.id];
      }
      if (
        action === "zone" &&
        p.markers.some(
          (m: Row) =>
            m.role === "rack" && m.column === n.column && m.row === n.row,
        )
      )
        p.zone_assignments[n.id] = zone;
      if (action === "erase" || action === "delete-point")
        delete p.zone_assignments[n.id];
      if (action === "delete-point") {
        p.deleted_positions ??= [];
        p.deleted_positions.push({ column: n.column, row: n.row });
        p.coordinate_overrides = (p.coordinate_overrides ?? []).filter(
          (o: Row) => o.column !== n.column || o.row !== n.row,
        );
      }
    }
    if (!p.location_attributes[zone])
      p.location_attributes[zone] = { ...p.warehouse_storage_defaults };
    commit(p);
  };
  const changeStorage = (patch: Row) =>
    commit({
      ...project,
      storage_layout: { ...project.storage_layout, ...patch },
    });
  const zoneAttrs = project.location_attributes?.[zone] ?? {};
  const tools = [
    { id: "select", name: "Inspect", icon: MousePointer2 },
    { id: "rack", name: "Racks", icon: RectangleHorizontal },
    { id: "workstation", name: "Station", icon: MapPin },
    { id: "zone", name: "Zone", icon: PaintBucket },
    { id: "erase", name: "Erase", icon: Eraser },
    { id: "lane", name: "Lane", icon: Link2 },
    { id: "pan", name: "Pan", icon: Move },
  ];
  return (
    <div className="warehouse-layout">
      <div className="warehouse-main">
        <div
          className="editor-toolbar"
          role="toolbar"
          aria-label="Warehouse editing tools"
        >
          <div className="tool-group">
            {tools.map((t) => (
              <button
                key={t.id}
                className={tool === t.id ? "tool active" : "tool"}
                aria-label={t.name}
                aria-pressed={tool === t.id}
                disabled={disabled}
                onClick={() => {
                  setTool(t.id);
                  setLaneStart(null);
                }}
              >
                <t.icon size={16} />
                <span>{t.name}</span>
              </button>
            ))}
          </div>
          <div className="tool-group">
            <button
              className="icon-button"
              aria-label="Undo warehouse edit"
              disabled={!past.length || disabled}
              onClick={() => {
                setFuture((f) => [project, ...f]);
                onChange(past.at(-1)!);
                setPast((p) => p.slice(0, -1));
              }}
            >
              <Undo2 size={16} />
            </button>
            <button
              className="icon-button"
              aria-label="Redo warehouse edit"
              disabled={!future.length || disabled}
              onClick={() => {
                setPast((p) => [...p, project]);
                onChange(future[0]);
                setFuture((f) => f.slice(1));
              }}
            >
              <Redo2 size={16} />
            </button>
          </div>
        </div>
        {(tool === "zone" || tool === "rack") && (
          <div className="tool-options">
            <PaintBucket size={15} />
            <label>
              Place in zone{" "}
              <input
                list="zone-ids"
                aria-label="Active zone"
                value={zone}
                onChange={(e) => setZone(e.target.value)}
                pattern="[^/]+"
              />
            </label>
            <datalist id="zone-ids">
              {zones.map((z) => (
                <option key={z} value={z} />
              ))}
            </datalist>
            <span>Drag across points to apply a rectangle.</span>
          </div>
        )}
        {tool === "lane" && (
          <div className="tool-options">
            {laneStart
              ? `Start: ${laneStart.id}. Select the lane’s destination point.`
              : "Select the start point, then the destination point."}
          </div>
        )}
        <MapCanvas
          geometry={geometry}
          project={project}
          selected={selected}
          onSelect={onSelect}
          onPaint={paint}
          tool={tool}
        />
        <div className="warehouse-facts">
          <div>
            <strong>{count(geometry?.summary?.rack_count)}</strong>
            <span>Rack locations</span>
          </div>
          <div>
            <strong>{count(geometry?.summary?.workstation_count)}</strong>
            <span>Workstations</span>
          </div>
          <div>
            <strong>{zones.length}</strong>
            <span>Storage zones</span>
          </div>
          <div>
            <strong>{count(geometry?.summary?.capacity)}</strong>
            <span>Available cells</span>
          </div>
        </div>
        <Panel
          title="Point inspector"
          subtitle={
            selected
              ? `Selected ${selected}`
              : "Select a point on the map, or enter its grid coordinates."
          }
          className="point-panel"
        >
          <div className="form-grid columns-4">
            <Field label="Column">
              <input
                type="number"
                min="0"
                value={column}
                onChange={(e) => setColumn(Number(e.target.value))}
              />
            </Field>
            <Field label="Row">
              <input
                type="number"
                min="0"
                value={row}
                onChange={(e) => setRow(Number(e.target.value))}
              />
            </Field>
            <Field label="Point role">
              <select value={role} onChange={(e) => setRole(e.target.value)}>
                <option value="rack">Rack</option>
                <option value="workstation">Workstation</option>
                <option value="clear">Unmarked</option>
              </select>
            </Field>
            <Field label="Endpoint ID">
              <input
                value={endpoint}
                placeholder={`${role === "rack" ? "RACK" : "WS"}_${column}_${row}`}
                onChange={(e) => setEndpoint(e.target.value)}
              />
            </Field>
          </div>
          <div className="inline-actions">
            <button
              className="button"
              disabled={disabled}
              onClick={() => {
                const n = geometry?.nodes.find(
                  (n: Row) => n.column === column && n.row === row,
                );
                if (!n) {
                  onError("Choose an active grid point.");
                  return;
                }
                const p = structuredClone(project);
                p.markers = p.markers.filter(
                  (m: Row) => m.column !== column || m.row !== row,
                );
                if (role !== "clear")
                  p.markers.push({
                    column,
                    row,
                    role,
                    endpoint_id:
                      endpoint ||
                      `${role === "rack" ? "RACK" : "WS"}_${column}_${row}`,
                  });
                p.zone_assignments ??= {};
                if (role === "rack") p.zone_assignments[n.id] = zone;
                else delete p.zone_assignments[n.id];
                commit(p);
                onSelect(n.id);
              }}
            >
              Apply point
            </button>
            {node && (
              <button
                className="button text-danger"
                disabled={disabled}
                onClick={() => paint([node], "delete-point")}
              >
                <Trash2 size={14} />
                Delete point
              </button>
            )}
            <span className="muted">
              Deleted points bridge their surviving neighbors.
            </span>
          </div>
          {node && (
            <details>
              <summary>Edit physical coordinates and lane direction</summary>
              <div className="form-grid columns-3">
                <Field label="X coordinate (m)">
                  <input
                    type="number"
                    value={pointX}
                    onChange={(e) => setPointX(Number(e.target.value))}
                  />
                </Field>
                <Field label="Y coordinate (m)">
                  <input
                    type="number"
                    value={pointY}
                    onChange={(e) => setPointY(Number(e.target.value))}
                  />
                </Field>
                <button
                  className="button align-end"
                  onClick={() => {
                    const p = structuredClone(project);
                    p.coordinate_overrides = (
                      p.coordinate_overrides ?? []
                    ).filter(
                      (o: Row) =>
                        o.column !== node.column || o.row !== node.row,
                    );
                    p.coordinate_overrides.push({
                      column: node.column,
                      row: node.row,
                      x: pointX,
                      y: pointY,
                    });
                    commit(p);
                  }}
                >
                  Move point
                </button>
              </div>
              <p className="muted">Connected lanes</p>
              {(geometry?.lanes ?? [])
                .filter(
                  (l: Row) =>
                    geometry?.nodes[l[0]]?.id === node.id ||
                    geometry?.nodes[l[1]]?.id === node.id,
                )
                .map((l: Row) => {
                  const a = geometry!.nodes[l[0]],
                    b = geometry!.nodes[l[1]];
                  return (
                    <div className="lane-row" key={`${a.id}-${b.id}`}>
                      <span>
                        {a.id} — {b.id}
                      </span>
                      <select
                        aria-label={`Direction from ${a.id} to ${b.id}`}
                        value={l[2] ? "both" : "forward"}
                        onChange={(e) => {
                          const p = structuredClone(project);
                          const isPoint = (v: Row, n: Row) =>
                            v.column === n.column && v.row === n.row;
                          p.one_way_lanes = (p.one_way_lanes ?? []).filter(
                            (x: Row) =>
                              !(
                                (isPoint(x.start, a) && isPoint(x.end, b)) ||
                                (isPoint(x.start, b) && isPoint(x.end, a))
                              ),
                          );
                          if (e.target.value !== "both")
                            p.one_way_lanes.push({
                              start: {
                                column:
                                  e.target.value === "forward"
                                    ? a.column
                                    : b.column,
                                row:
                                  e.target.value === "forward" ? a.row : b.row,
                              },
                              end: {
                                column:
                                  e.target.value === "forward"
                                    ? b.column
                                    : a.column,
                                row:
                                  e.target.value === "forward" ? b.row : a.row,
                              },
                            });
                          commit(p);
                        }}
                      >
                        <option value="both">Both directions</option>
                        <option value="forward">
                          {a.id} → {b.id}
                        </option>
                        <option value="reverse">
                          {b.id} → {a.id}
                        </option>
                      </select>
                      <button
                        className="icon-button"
                        aria-label={`Delete lane ${a.id} to ${b.id}`}
                        onClick={() => {
                          const p = structuredClone(project);
                          p.deleted_lanes ??= [];
                          p.deleted_lanes.push({
                            start: { column: a.column, row: a.row },
                            end: { column: b.column, row: b.row },
                          });
                          p.added_lanes = (p.added_lanes ?? []).filter(
                            (x: Row) =>
                              !(
                                (isPoint(x.start, a) && isPoint(x.end, b)) ||
                                (isPoint(x.start, b) && isPoint(x.end, a))
                              ),
                          );
                          commit(p);
                        }}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  );
                })}
            </details>
          )}
        </Panel>
      </div>
      <aside className="configuration-panel">
        <div className="segment-tabs">
          <button
            className={tab === "storage" ? "selected" : ""}
            onClick={() => setTab("storage")}
          >
            Storage
          </button>
          <button
            className={tab === "zones" ? "selected" : ""}
            onClick={() => setTab("zones")}
          >
            Zones
          </button>
          <button
            className={tab === "grid" ? "selected" : ""}
            onClick={() => setTab("grid")}
          >
            Grid
          </button>
        </div>
        {tab === "storage" && (
          <>
            <h3>Rack configuration</h3>
            <p className="muted">Define the capacity the allocator can use.</p>
            <Field label="Storage system">
              <select
                value={project.storage_layout.system_type}
                onChange={(e) => {
                  const amr = e.target.value === "AMR";
                  commit({
                    ...project,
                    machine_carrying_capacity: amr
                      ? {
                          max_item_length: null,
                          max_item_width: null,
                          max_item_height: null,
                          max_item_weight: 150,
                        }
                      : {
                          max_item_length: 1,
                          max_item_width: 1,
                          max_item_height: 1.5,
                          max_item_weight: 25,
                        },
                    storage_layout: {
                      ...project.storage_layout,
                      system_type: e.target.value,
                    },
                  });
                }}
              >
                <option>AMR</option>
                <option>Mini-load ASRS</option>
                <option>Pallet ASRS</option>
              </select>
            </Field>
            <div className="form-grid">
              <Field label="Levels / rack">
                <input
                  type="number"
                  min="1"
                  max="64"
                  value={project.storage_layout.levels_per_rack}
                  onChange={(e) =>
                    changeStorage({ levels_per_rack: Number(e.target.value) })
                  }
                />
              </Field>
              <Field label="Slots / level">
                <input
                  type="number"
                  min="1"
                  max="64"
                  value={project.storage_layout.slots_per_level}
                  onChange={(e) =>
                    changeStorage({ slots_per_level: Number(e.target.value) })
                  }
                />
              </Field>
            </div>
            <Field label="Rack height (m)">
              <input
                type="number"
                min=".1"
                step=".1"
                value={project.storage_layout.rack_height_m ?? 3.1}
                onChange={(e) =>
                  changeStorage({ rack_height_m: Number(e.target.value) })
                }
              />
            </Field>
            <div className="config-divider" />
            <h3>Standard slot envelope</h3>
            <p className="muted">
              Used for stock sizing and physical classification.
            </p>
            <div className="form-grid">
              {Object.entries(project.warehouse_storage_defaults).map(
                ([key, value]) => (
                  <Field
                    key={key}
                    label={`${key.replace("max_item_", "").replace(/^./, (x) => x.toUpperCase())} (${key.endsWith("weight") ? "kg" : "m"})`}
                  >
                    <input
                      type="number"
                      min=".001"
                      step=".01"
                      value={Number(value)}
                      onChange={(e) =>
                        commit(
                          {
                            ...project,
                            warehouse_storage_defaults: {
                              ...project.warehouse_storage_defaults,
                              [key]: Number(e.target.value),
                            },
                          },
                          false,
                        )
                      }
                    />
                  </Field>
                ),
              )}
            </div>
            <Field
              label={
                project.storage_layout.system_type === "AMR"
                  ? "Whole-rack capacity (kg)"
                  : "Machine carrying weight (kg)"
              }
            >
              <input
                type="number"
                min=".001"
                value={
                  project.machine_carrying_capacity?.max_item_weight ?? 150
                }
                onChange={(e) =>
                  commit({
                    ...project,
                    machine_carrying_capacity: {
                      ...project.machine_carrying_capacity,
                      max_item_weight: Number(e.target.value),
                    },
                  })
                }
              />
            </Field>
            {project.storage_layout.system_type !== "AMR" && (
              <details>
                <summary>Machine envelope</summary>
                {["length", "width", "height"].map((k) => (
                  <Field key={k} label={`${k} (m)`}>
                    <input
                      type="number"
                      min=".001"
                      step=".01"
                      value={
                        project.machine_carrying_capacity?.[`max_item_${k}`] ??
                        1
                      }
                      onChange={(e) =>
                        commit({
                          ...project,
                          machine_carrying_capacity: {
                            ...project.machine_carrying_capacity,
                            [`max_item_${k}`]: Number(e.target.value),
                          },
                        })
                      }
                    />
                  </Field>
                ))}
              </details>
            )}
            <button
              className="button full-width"
              onClick={() => {
                const p = structuredClone(project);
                for (const z of zones)
                  p.location_attributes[z] = {
                    ...p.location_attributes[z],
                    ...p.warehouse_storage_defaults,
                  };
                commit(p);
              }}
            >
              Apply envelope to all zones
            </button>
          </>
        )}
        {tab === "zones" && (
          <>
            <h3>Storage zone rules</h3>
            <p className="muted">
              Rack memberships and local rules stay authoritative.
            </p>
            <Field label="Zone ID">
              <input
                list="zone-ids"
                value={zone}
                onChange={(e) => setZone(e.target.value)}
                placeholder="Z01"
              />
            </Field>
            <p className="zone-count">
              {
                (geometry?.racks ?? []).filter((r: Row) => r.zone === zone)
                  .length
              }{" "}
              racks in this zone
            </p>
            {(project.attribute_catalog ?? [])
              .filter((d: Row) => d.value_type === "boolean")
              .map((d: Row) => (
                <label className="checkbox-row" key={d.key}>
                  <input
                    type="checkbox"
                    checked={Boolean(zoneAttrs[d.key])}
                    onChange={(e) =>
                      commit(
                        {
                          ...project,
                          location_attributes: {
                            ...project.location_attributes,
                            [zone]: { ...zoneAttrs, [d.key]: e.target.checked },
                          },
                        },
                        false,
                      )
                    }
                  />
                  <span>{d.label}</span>
                </label>
              ))}
            <div className="config-divider" />
            {["length", "width", "height", "weight"].map((k) => (
              <Field
                key={k}
                label={`Maximum item ${k} (${k === "weight" ? "kg" : "m"})`}
              >
                <input
                  type="number"
                  min=".001"
                  step=".01"
                  placeholder="Unbounded"
                  value={zoneAttrs[`max_item_${k}`] ?? ""}
                  onChange={(e) =>
                    commit(
                      {
                        ...project,
                        location_attributes: {
                          ...project.location_attributes,
                          [zone]: {
                            ...zoneAttrs,
                            [`max_item_${k}`]:
                              e.target.value === ""
                                ? null
                                : Number(e.target.value),
                          },
                        },
                      },
                      false,
                    )
                  }
                />
              </Field>
            ))}
            <button
              className="button full-width"
              onClick={() => setTool("zone")}
            >
              <PaintBucket size={15} />
              Paint rack membership
            </button>
            <details>
              <summary>Advanced inherited attributes</summary>
              <p className="muted">
                Configure zone / aisle / bay / level / cell paths in the grid
                JSON. Export, edit and re-import to preserve local overrides.
              </p>
              <button
                className="button"
                onClick={() => download("warehouse.grid.json", project)}
              >
                Export grid JSON
              </button>
            </details>
          </>
        )}
        {tab === "grid" && (
          <>
            <Field label="Warehouse name">
              <input
                value={project.grid.map_name}
                onChange={(event) =>
                  commit(
                    {
                      ...project,
                      grid: { ...project.grid, map_name: event.target.value },
                    },
                    false,
                  )
                }
              />
            </Field>
            <h3>Warehouse dimensions</h3>
            <p className="muted">
              Creating a new grid clears existing markers and lanes.
            </p>
            <div className="form-grid">
              {[
                ["width_m", "Width (m)"],
                ["length_m", "Length (m)"],
                ["spacing_m", "X spacing (m)"],
                ["spacing_y_m", "Y spacing (m)"],
              ].map(([key, label]) => (
                <Field key={key} label={label}>
                  <input
                    type="number"
                    min=".1"
                    step=".1"
                    value={gridDraft[key] ?? gridDraft.spacing_m}
                    onChange={(e) =>
                      setGridDraft((d) => ({
                        ...d,
                        [key]: Number(e.target.value),
                      }))
                    }
                  />
                </Field>
              ))}
            </div>
            <button
              className="button full-width"
              onClick={() => {
                if (
                  !window.confirm(
                    "Create a new grid and clear the current rack, station and lane placements?",
                  )
                )
                  return;
                commit({
                  ...project,
                  grid: gridDraft,
                  markers: [],
                  zone_assignments: {},
                  deleted_positions: [],
                  deleted_lanes: [],
                  added_lanes: [],
                  one_way_lanes: [],
                  coordinate_overrides: [],
                });
              }}
            >
              Create grid
            </button>
            <div className="config-divider" />
            <UploadButton
              label="Import grid JSON"
              accept=".json"
              onFile={(f) => onImport(f, "grid")}
              disabled={disabled}
            />
            <button
              className="button full-width"
              onClick={() => download("warehouse.grid.json", project)}
            >
              <Download size={15} />
              Export grid JSON
            </button>
            <button
              className="button full-width"
              disabled={!geometry?.building}
              onClick={() =>
                download(
                  "warehouse.building.yaml",
                  geometry!.building,
                  "application/yaml",
                )
              }
            >
              <Download size={15} />
              Export RMF building YAML
            </button>
          </>
        )}
      </aside>
    </div>
  );
}

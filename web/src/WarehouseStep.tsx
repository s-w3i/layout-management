import { useEffect, useRef, useState, type InputHTMLAttributes } from "react";
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
import { syncBuffers, standard } from "./warehouse";
import { download } from "./api";
import {
  addLane,
  deleteLane,
  laneDirections,
  lanePair,
  setLaneDirection,
  zoneId,
  numericDraft,
  validateGrid,
  type LaneDirection,
} from "../../shared/editor";

type Props = {
  project: Row;
  geometry: Row | null;
  onChange: (project: Row) => void;
  onImport: (file: File, kind: string) => void;
  selected: string;
  onSelect: (id: string) => void;
  disabled: boolean;
  updating?: boolean;
  invalid?: boolean;
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
  updating = false,
  invalid = false,
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
  const [pendingGrid, setPendingGrid] = useState<Row | null>(null);
  const gridConfirmRef = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    if (pendingGrid) gridConfirmRef.current?.showModal();
    else gridConfirmRef.current?.close();
  }, [pendingGrid]);
  const lastProject = useRef(project);
  useEffect(() => {
    if (project !== lastProject.current) {
      setPast([]);
      setFuture([]);
      setLaneStart(null);
      setGridDraft({ ...project.grid });
      setPendingGrid(null);
    }
    lastProject.current = project;
  }, [project]);
  useEffect(
    () => setGridDraft({ ...project.grid }),
    [
      project.grid.width_m,
      project.grid.length_m,
      project.grid.spacing_m,
      project.grid.spacing_y_m,
    ],
  );
  const node = (geometry?.nodes ?? []).find((n: Row) => n.id === selected),
    rack = (geometry?.racks ?? []).find((r: Row) => r.id === selected);
  const zones = [
    ...new Set<string>([
      ...(Object.values(project.zone_assignments ?? {}) as string[]),
      ...Object.keys(project.location_attributes ?? {}).filter(
        (id) => !id.includes("/"),
      ),
    ]),
  ].sort();
  useEffect(() => {
    if (node) {
      setColumn(node.column);
      setRow(node.row);
      setRole(node.role || "clear");
      setEndpoint(node.endpoint || "");
      setPointX(node.x);
      setPointY(node.y);
      if (rack && tool !== "rack" && tool !== "zone") setZone(rack.zone);
    }
  }, [
    selected,
    node?.x,
    node?.y,
    node?.role,
    node?.endpoint,
    rack?.zone,
    tool,
  ]);
  const commit = (p: Row, refresh = false): boolean => {
    try {
      const next = refresh
        ? syncBuffers(p)
        : {
            ...p,
            storage_layout: {
              ...p.storage_layout,
              machine_carrying_capacity:
                p.machine_carrying_capacity ??
                p.storage_layout.machine_carrying_capacity,
            },
          };
      setPast((previous) => [...previous.slice(-19), project]);
      setFuture([]);
      lastProject.current = next;
      onError("");
      onChange(next);
      return true;
    } catch (error) {
      onError(String((error as Error).message));
      return false;
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
      commit(addLane(project, laneStart, n));
      setLaneStart(null);
      return;
    }
    const p = structuredClone(project),
      chosen = new Set(nodes.map((n) => n.id));
    let activeZone = zone;
    if (action === "rack" || action === "zone") {
      try {
        activeZone = zoneId(zone);
      } catch (error) {
        onError((error as Error).message);
        return;
      }
    }
    p.zone_assignments ??= {};
    p.markers ??= [];
    p.location_attributes ??= {};
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
        if (action === "rack") p.zone_assignments[n.id] = activeZone;
        else delete p.zone_assignments[n.id];
      }
      if (
        action === "zone" &&
        p.markers.some(
          (m: Row) =>
            m.role === "rack" && m.column === n.column && m.row === n.row,
        )
      )
        p.zone_assignments[n.id] = activeZone;
      if (action === "erase" || action === "delete-point")
        delete p.zone_assignments[n.id];
      if (action === "delete-point") {
        p.deleted_positions ??= [];
        if (!p.deleted_positions.some((point: Row) => isPoint(point, n)))
          p.deleted_positions.push({ column: n.column, row: n.row });
        p.coordinate_overrides = (p.coordinate_overrides ?? []).filter(
          (o: Row) => o.column !== n.column || o.row !== n.row,
        );
        for (const key of ["added_lanes", "deleted_lanes", "one_way_lanes"])
          p[key] = (p[key] ?? []).filter(
            (lane: Row) => !isPoint(lane.start, n) && !isPoint(lane.end, n),
          );
      }
    }
    if (
      (action === "rack" || action === "zone") &&
      !Object.hasOwn(p.location_attributes, activeZone)
    )
      p.location_attributes[activeZone] = { ...p.warehouse_storage_defaults };
    commit(p, action !== "zone");
  };
  const changeStorage = (patch: Row) =>
    commit(
      {
        ...project,
        storage_layout: { ...project.storage_layout, ...patch },
      },
      true,
    );
  const editZone = (patch: Row): boolean => {
    try {
      const id = zoneId(zone);
      return commit({
        ...project,
        location_attributes: {
          ...project.location_attributes,
          [id]: { ...project.location_attributes?.[id], ...patch },
        },
      });
    } catch (error) {
      onError((error as Error).message);
      return false;
    }
  };
  const zoneAttrs = project.location_attributes?.[zone] ?? {};
  const envelope = { ...standard, ...project.warehouse_storage_defaults },
    systemType = project.storage_layout.system_type ?? "AMR";
  const machine =
    project.machine_carrying_capacity ??
    project.storage_layout.machine_carrying_capacity ??
    {};
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
    <fieldset
      className="warehouse-layout"
      disabled={disabled}
      aria-label="Warehouse configuration"
    >
      <div className="warehouse-main">
        <div
          className="editor-toolbar"
          role="toolbar"
          aria-label="Warehouse editing tools"
        >
          <span className="geometry-status" role="status">
            {updating
              ? "Updating map…"
              : invalid
                ? "Map needs attention"
                : "Map up to date"}
          </span>
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
                lastProject.current = past.at(-1)!;
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
                lastProject.current = future[0];
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
                if (n) {
                  onSelect(n.id);
                  setRole(n.role || "clear");
                  setEndpoint(n.endpoint || "");
                } else onError("Choose an active grid point.");
              }}
            >
              Select point
            </button>
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
                p.markers ??= [];
                p.location_attributes ??= {};
                p.markers = p.markers.filter(
                  (m: Row) => m.column !== column || m.row !== row,
                );
                if (role !== "clear") {
                  const id =
                    endpoint.trim() ||
                    `${role === "rack" ? "RACK" : "WS"}_${column}_${row}`;
                  if (p.markers.some((m: Row) => m.endpoint_id === id)) {
                    onError(
                      "Endpoint IDs must be unique. Choose another endpoint ID.",
                    );
                    return;
                  }
                  p.markers.push({
                    column,
                    row,
                    role,
                    endpoint_id: id,
                  });
                }
                p.zone_assignments ??= {};
                if (role === "rack") {
                  let id;
                  try {
                    id = zoneId(zone);
                  } catch (error) {
                    onError((error as Error).message);
                    return;
                  }
                  p.zone_assignments[n.id] = id;
                  if (!Object.hasOwn(p.location_attributes, id))
                    p.location_attributes[id] = {
                      ...p.warehouse_storage_defaults,
                    };
                } else delete p.zone_assignments[n.id];
                commit(p, true);
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
                  <NumberInput
                    type="number"
                    value={pointX}
                    onValue={(value) => {
                      setPointX(Number(value));
                      return true;
                    }}
                  />
                </Field>
                <Field label="Y coordinate (m)">
                  <NumberInput
                    type="number"
                    value={pointY}
                    onValue={(value) => {
                      setPointY(Number(value));
                      return true;
                    }}
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
                    commit(p, true);
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
                  const {
                    start: a,
                    end: b,
                    key,
                  } = lanePair(geometry!.nodes[l[0]], geometry!.nodes[l[1]]);
                  return (
                    <div className="lane-row" key={key}>
                      <span>
                        {a.id} — {b.id}
                      </span>
                      <select
                        aria-label={`Direction from ${a.id} to ${b.id}`}
                        value={laneDirections(project).get(key) ?? "both"}
                        onChange={(e) => {
                          commit(
                            setLaneDirection(
                              project,
                              a,
                              b,
                              e.target.value as LaneDirection,
                            ),
                          );
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
                          commit(deleteLane(project, a, b));
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
                value={systemType}
                onChange={(e) => {
                  const amr = e.target.value === "AMR";
                  commit(
                    {
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
                    },
                    true,
                  );
                }}
              >
                <option>AMR</option>
                <option>Mini-load ASRS</option>
                <option>Pallet ASRS</option>
              </select>
            </Field>
            <div className="form-grid">
              <Field label="Levels / rack">
                <NumberInput
                  type="number"
                  min="1"
                  max={Math.min(
                    64,
                    Math.floor(
                      256 / (project.storage_layout.slots_per_level ?? 4),
                    ),
                  )}
                  integer
                  value={project.storage_layout.levels_per_rack ?? 3}
                  onValue={(value) =>
                    changeStorage({ levels_per_rack: Number(value) })
                  }
                />
              </Field>
              <Field label="Slots / level">
                <NumberInput
                  type="number"
                  min="1"
                  max={Math.min(
                    64,
                    Math.floor(
                      256 / (project.storage_layout.levels_per_rack ?? 3),
                    ),
                  )}
                  integer
                  value={project.storage_layout.slots_per_level ?? 4}
                  onValue={(value) =>
                    changeStorage({ slots_per_level: Number(value) })
                  }
                />
              </Field>
            </div>
            <Field label="Rack height (m)">
              <NumberInput
                type="number"
                min=".1"
                step=".1"
                value={project.storage_layout.rack_height_m ?? 3.1}
                onValue={(value) =>
                  changeStorage({ rack_height_m: Number(value) })
                }
              />
            </Field>
            <div className="config-divider" />
            <h3>Standard slot envelope</h3>
            <p className="muted">
              Used for stock sizing and physical classification.
            </p>
            <div className="form-grid">
              {Object.entries(envelope).map(([key, value]) => (
                <Field
                  key={key}
                  label={`${key.replace("max_item_", "").replace(/^./, (x) => x.toUpperCase())} (${key.endsWith("weight") ? "kg" : "m"})`}
                >
                  <NumberInput
                    type="number"
                    min=".001"
                    step=".01"
                    value={Number(value)}
                    onValue={(value) =>
                      commit(
                        {
                          ...project,
                          warehouse_storage_defaults: {
                            ...envelope,
                            [key]: Number(value),
                          },
                        },
                        false,
                      )
                    }
                  />
                </Field>
              ))}
            </div>
            <Field
              label={
                systemType === "AMR"
                  ? "Whole-rack capacity (kg)"
                  : "Machine carrying weight (kg)"
              }
            >
              <NumberInput
                type="number"
                min=".001"
                nullable
                placeholder="Unbounded"
                value={machine.max_item_weight ?? ""}
                onValue={(value) =>
                  commit({
                    ...project,
                    machine_carrying_capacity: {
                      ...machine,
                      max_item_weight: value,
                    },
                  })
                }
              />
            </Field>
            {systemType !== "AMR" && (
              <details>
                <summary>Machine envelope</summary>
                {["length", "width", "height"].map((k) => (
                  <Field key={k} label={`${k} (m)`}>
                    <NumberInput
                      type="number"
                      min=".001"
                      step=".01"
                      nullable
                      placeholder="Unbounded"
                      value={machine[`max_item_${k}`] ?? ""}
                      onValue={(value) =>
                        commit({
                          ...project,
                          machine_carrying_capacity: {
                            ...machine,
                            [`max_item_${k}`]: value,
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
                p.location_attributes ??= {};
                for (const z of zones)
                  p.location_attributes[z] = {
                    ...p.location_attributes[z],
                    ...envelope,
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
                    onChange={(e) => editZone({ [d.key]: e.target.checked })}
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
                <NumberInput
                  type="number"
                  min=".001"
                  step=".01"
                  placeholder="Unbounded"
                  nullable
                  value={zoneAttrs[`max_item_${k}`] ?? ""}
                  onValue={(value) => editZone({ [`max_item_${k}`]: value })}
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
                try {
                  validateGrid(gridDraft);
                } catch (error) {
                  onError((error as Error).message);
                  return;
                }
                setPendingGrid({
                  width_m: gridDraft.width_m,
                  length_m: gridDraft.length_m,
                  spacing_m: gridDraft.spacing_m,
                  spacing_y_m: gridDraft.spacing_y_m ?? gridDraft.spacing_m,
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
              disabled={!geometry?.building || updating || invalid}
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
      <dialog
        ref={gridConfirmRef}
        className="help-dialog"
        aria-labelledby="replace-grid-title"
        onCancel={(event) => {
          event.preventDefault();
          setPendingGrid(null);
        }}
        onClose={() => setPendingGrid(null)}
      >
        <div className="dialog-inner">
          <h2 id="replace-grid-title">Replace warehouse grid?</h2>
          <p>
            Create a {pendingGrid?.width_m} × {pendingGrid?.length_m} m grid
            with {pendingGrid?.spacing_m} × {pendingGrid?.spacing_y_m} m
            spacing.
          </p>
          <p>
            Existing racks, workstations, moved or deleted points and lane rules
            will be cleared. You can undo this change.
          </p>
          <div className="dialog-actions">
            <button
              className="button"
              autoFocus
              onClick={() => setPendingGrid(null)}
            >
              Cancel
            </button>
            <button
              className="button primary"
              onClick={() => {
                if (!pendingGrid) return;
                if (
                  commit(
                    {
                      ...project,
                      grid: { ...project.grid, ...pendingGrid },
                      markers: [],
                      zone_assignments: {},
                      deleted_positions: [],
                      deleted_lanes: [],
                      added_lanes: [],
                      one_way_lanes: [],
                      coordinate_overrides: [],
                    },
                    true,
                  )
                ) {
                  onSelect("");
                  setColumn(0);
                  setRow(0);
                  setRole("clear");
                  setEndpoint("");
                  setPendingGrid(null);
                }
              }}
            >
              Replace grid
            </button>
          </div>
        </div>
      </dialog>
    </fieldset>
  );
}

function NumberInput({
  value,
  onValue,
  min,
  max,
  integer = false,
  nullable = false,
  ...props
}: Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange"> & {
  value: number | string;
  onValue: (value: number | null) => boolean;
  integer?: boolean;
  nullable?: boolean;
}) {
  const [draft, setDraft] = useState(String(value)),
    [valid, setValid] = useState(true);
  useEffect(() => {
    setDraft(String(value));
    setValid(true);
  }, [value]);
  return (
    <input
      {...props}
      min={min}
      max={max}
      value={draft}
      aria-invalid={!valid && draft !== ""}
      onChange={(e) => {
        const raw = e.target.value;
        setDraft(raw);
        const n = numericDraft(
          raw,
          min == null ? undefined : Number(min),
          max == null ? undefined : Number(max),
          integer,
        );
        if (n === null && !(nullable && raw === "")) {
          setValid(false);
          return;
        }
        setValid(onValue(n));
      }}
      onBlur={() => {
        if (!valid) {
          setDraft(String(value));
          setValid(true);
        }
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter") e.currentTarget.blur();
      }}
    />
  );
}

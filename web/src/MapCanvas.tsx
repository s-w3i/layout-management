import { useEffect, useRef, useState } from "react";
import { Crosshair, ZoomIn, ZoomOut, Layers3 } from "lucide-react";
import type { Row } from "../../shared/model";
import { movementColor } from "../../shared/operations";

const zoneColors = [
  "#dae6f7",
  "#e3ddf1",
  "#d4eae3",
  "#f4e5cc",
  "#f0dce3",
  "#d3e9f0",
  "#e7e6cc",
  "#dce1ec",
];
const classColors: Record<string, string> = {
  A: "#e6a04c",
  B: "#719fc7",
  C: "#abbac8",
};
type Props = {
  geometry: Row | null;
  project: Row;
  result?: Row | null;
  selected: string;
  onSelect: (id: string) => void;
  onPaint?: (nodes: Row[], tool: string) => void;
  tool?: string;
  movementView?: boolean;
  highlighted?: string[];
};
export function MapCanvas({
  geometry,
  project,
  result,
  selected,
  onSelect,
  onPaint,
  tool = "select",
  movementView = false,
  highlighted = [],
}: Props) {
  const canvas = useRef<HTMLCanvasElement>(null),
    container = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 800, height: 450 }),
    [view, setView] = useState({ zoom: 1, x: 0, y: 0 }),
    [chosenColor, setColorBy] = useState("zones");
  const colorBy = movementView
    ? "movement"
    : chosenColor === "movement" && !result?.movement
      ? "zones"
      : chosenColor;
  const rackVisits: Record<string, number> =
    result?.movement?.rack_visit_counts ?? {};
  const maximumVisits = Math.max(0, ...Object.values(rackVisits));
  const drag = useRef<{
    x: number;
    y: number;
    start: Row | null;
    pan: boolean;
    view: { zoom: number; x: number; y: number };
  } | null>(null);
  const [hover, setHover] = useState<Row | null>(null),
    [selection, setSelection] = useState<{
      x: number;
      y: number;
      w: number;
      h: number;
    } | null>(null);
  const nodes: Row[] = geometry?.nodes ?? [],
    racks: Row[] = geometry?.racks ?? [];
  const bounds = (() => {
    const xs = nodes.map((n) => n.x),
      ys = nodes.map((n) => n.y);
    return {
      minX: Math.min(0, ...xs),
      maxX: Math.max(project.grid.width_m, ...xs),
      minY: Math.min(0, ...ys),
      maxY: Math.max(project.grid.length_m, ...ys),
    };
  })();
  const padding = 38,
    scale =
      Math.min(
        (size.width - 2 * padding) / (bounds.maxX - bounds.minX || 1),
        (size.height - 2 * padding) / (bounds.maxY - bounds.minY || 1),
      ) * view.zoom;
  const originX =
      size.width / 2 - ((bounds.minX + bounds.maxX) * scale) / 2 + view.x,
    originY =
      size.height / 2 + ((bounds.minY + bounds.maxY) * scale) / 2 + view.y;
  const screen = (n: Row) => ({
    x: originX + n.x * scale,
    y: originY - n.y * scale,
  });
  const nearest = (x: number, y: number) => {
    let best: Row | null = null,
      d = Infinity;
    for (const n of nodes) {
      const p = screen(n),
        distance = Math.hypot(p.x - x, p.y - y);
      if (distance < d) {
        d = distance;
        best = n;
      }
    }
    return d < Math.max(12, Math.min(24, scale * 0.7)) ? best : null;
  };
  const location = (event: { clientX: number; clientY: number }) => {
    const rect = canvas.current!.getBoundingClientRect();
    return { x: event.clientX - rect.left, y: event.clientY - rect.top };
  };
  useEffect(() => {
    const observer = new ResizeObserver((entries) => {
      const rect = entries[0].contentRect;
      setSize({ width: rect.width, height: rect.height });
    });
    if (container.current) observer.observe(container.current);
    return () => observer.disconnect();
  }, []);
  useEffect(() => {
    const element = canvas.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      const p = location(event),
        factor = event.deltaY > 0 ? 0.9 : 1.1;
      setView((old) => {
        const zoom = Math.max(0.55, Math.min(6, old.zoom * factor)),
          ratio = zoom / old.zoom;
        return {
          zoom,
          x: p.x - size.width / 2 - (p.x - size.width / 2 - old.x) * ratio,
          y: p.y - size.height / 2 - (p.y - size.height / 2 - old.y) * ratio,
        };
      });
    };
    element.addEventListener("wheel", wheel, { passive: false });
    return () => element.removeEventListener("wheel", wheel);
  }, [size]);
  useEffect(() => {
    const el = canvas.current;
    if (!el) return;
    const dpr = window.devicePixelRatio || 1;
    el.width = size.width * dpr;
    el.height = size.height * dpr;
    const ctx = el.getContext("2d")!;
    ctx.scale(dpr, dpr);
    ctx.clearRect(0, 0, size.width, size.height);
    ctx.fillStyle = "#fafbfd";
    ctx.fillRect(0, 0, size.width, size.height);
    const zoneIds = [...new Set(racks.map((r) => r.zone))].sort(),
      rackById = new Map(racks.map((r) => [r.id, r]));
    const occupied = new Map<string, Row[]>();
    for (const row of result?.assignments ?? [])
      if (row.assignment_status === "ASSIGNED") {
        const list = occupied.get(row.rack_id) ?? [];
        list.push(row);
        occupied.set(row.rack_id, list);
      }
    ctx.lineWidth = 0.65;
    ctx.strokeStyle = "#e3e8ef";
    for (const [a, b, bidi] of geometry?.lanes ?? []) {
      const p = screen(nodes[a]),
        q = screen(nodes[b]);
      ctx.beginPath();
      ctx.moveTo(p.x, p.y);
      ctx.lineTo(q.x, q.y);
      ctx.stroke();
      if (!bidi && view.zoom > 1.3) {
        const x = (p.x + q.x) / 2,
          y = (p.y + q.y) / 2,
          angle = Math.atan2(q.y - p.y, q.x - p.x);
        ctx.save();
        ctx.translate(x, y);
        ctx.rotate(angle);
        ctx.beginPath();
        ctx.moveTo(-3, -2);
        ctx.lineTo(1, 0);
        ctx.lineTo(-3, 2);
        ctx.strokeStyle = "#64748b";
        ctx.stroke();
        ctx.restore();
      }
    }
    ctx.strokeStyle = "#b9c5d3";
    ctx.setLineDash([4, 5]);
    ctx.strokeRect(
      originX,
      originY - project.grid.length_m * scale,
      project.grid.width_m * scale,
      project.grid.length_m * scale,
    );
    ctx.setLineDash([]);
    const spacing = Number(project.grid.spacing_m ?? 1),
      side = Math.max(4, Math.min(20, spacing * scale * 0.65));
    for (const n of nodes) {
      const p = screen(n);
      if (p.x < 0 || p.x > size.width || p.y < 0 || p.y > size.height) continue;
      if (n.role === "rack") {
        const r = rackById.get(n.id),
          rows = occupied.get(n.id) ?? [];
        let fill = zoneColors[zoneIds.indexOf(r?.zone) % zoneColors.length];
        if (colorBy === "abc" && result) {
          const cl = rows.find((r) => r.velocity_class === "A")
            ? "A"
            : rows.find((r) => r.velocity_class === "B")
              ? "B"
              : "C";
          fill = rows.length ? classColors[cl] : "#edf0f4";
        }
        if (colorBy === "occupancy" && result)
          fill = rows.length ? "#89b9a5" : "#e5e9ee";
        if (colorBy === "movement")
          fill = movementColor(Number(rackVisits[n.id] ?? 0), maximumVisits);
        ctx.fillStyle = fill;
        ctx.strokeStyle = "#9aaec4";
        ctx.lineWidth = 0.8;
        ctx.beginPath();
        ctx.roundRect(p.x - side / 2, p.y - side / 2, side, side, 2);
        ctx.fill();
        ctx.stroke();
        ctx.strokeStyle = "rgba(70,90,110,.25)";
        ctx.beginPath();
        ctx.moveTo(p.x - side / 2 + 2, p.y);
        ctx.lineTo(p.x + side / 2 - 2, p.y);
        ctx.stroke();
      } else if (n.role === "workstation") {
        ctx.fillStyle = "#26735b";
        ctx.beginPath();
        ctx.arc(p.x, p.y, Math.max(4, side * 0.5), 0, Math.PI * 2);
        ctx.fill();
        ctx.strokeStyle = "#fff";
        ctx.lineWidth = 1.2;
        ctx.beginPath();
        ctx.moveTo(p.x - 2, p.y);
        ctx.lineTo(p.x + 2, p.y);
        ctx.moveTo(p.x, p.y - 2);
        ctx.lineTo(p.x, p.y + 2);
        ctx.stroke();
      } else if (view.zoom > 1.3 || tool !== "select") {
        ctx.fillStyle = "#ccd5e1";
        ctx.beginPath();
        ctx.arc(p.x, p.y, 1.5, 0, Math.PI * 2);
        ctx.fill();
      }
      if (n.id === selected || highlighted.includes(n.id)) {
        ctx.strokeStyle = "#d36a23";
        ctx.lineWidth = 2.2;
        ctx.beginPath();
        ctx.roundRect(
          p.x - side / 2 - 4,
          p.y - side / 2 - 4,
          side + 8,
          side + 8,
          4,
        );
        ctx.stroke();
      }
    }
    ctx.fillStyle = "#57677e";
    ctx.font = "10px ui-monospace, monospace";
    ctx.textAlign = "center";
    const tick = Math.max(1, Math.ceil(project.grid.width_m / 8 / 5) * 5);
    for (let x = 0; x <= project.grid.width_m; x += tick) {
      const p = screen({ x, y: 0 });
      if (p.x > 20 && p.x < size.width - 20)
        ctx.fillText(`${x}m`, p.x, Math.min(size.height - 12, originY + 22));
    }
    ctx.textAlign = "right";
    const yt = Math.max(1, Math.ceil(project.grid.length_m / 6 / 5) * 5);
    for (let y = yt; y <= project.grid.length_m; y += yt) {
      const p = screen({ x: 0, y });
      if (p.y > 15 && p.y < size.height - 15)
        ctx.fillText(`${y}m`, Math.max(28, originX - 12), p.y + 3);
    }
  }, [
    geometry,
    project,
    result,
    selected,
    size,
    view,
    colorBy,
    tool,
    highlighted,
  ]);
  const apply = (chosen: Row[]) => {
    if (!chosen.length) return;
    onSelect(chosen[0].id);
    if (tool !== "select" && tool !== "pan") onPaint?.(chosen, tool);
  };
  return (
    <div className="map-shell">
      <div className="map-heading">
        <div>
          <span className="eyebrow">WAREHOUSE PLAN</span>
          <span className="map-dimensions">
            {project.grid.width_m} × {project.grid.length_m} m{" "}
            <span>· {racks.length} racks</span>
          </span>
        </div>
        <div className="map-controls">
          {!movementView && (
            <label className="view-select">
              <Layers3 size={14} />
              <span className="sr-only">Color map by</span>
              <select
                value={colorBy}
                onChange={(e) => setColorBy(e.target.value)}
              >
                <option value="zones">Storage zones</option>
                {result && (
                  <>
                    <option value="abc">ABC classes</option>
                    <option value="occupancy">Occupancy</option>
                    {result.movement && (
                      <option value="movement">Rack movement</option>
                    )}
                  </>
                )}
              </select>
            </label>
          )}
          <button
            className="icon-button"
            title="Zoom out"
            aria-label="Zoom out"
            onClick={() =>
              setView((v) => ({ ...v, zoom: Math.max(0.55, v.zoom * 0.8) }))
            }
          >
            <ZoomOut size={17} />
          </button>
          <button
            className="icon-button"
            title="Zoom in"
            aria-label="Zoom in"
            onClick={() =>
              setView((v) => ({ ...v, zoom: Math.min(6, v.zoom * 1.2) }))
            }
          >
            <ZoomIn size={17} />
          </button>
          <button
            className="icon-button"
            title="Fit warehouse"
            aria-label="Fit warehouse"
            onClick={() => setView({ zoom: 1, x: 0, y: 0 })}
          >
            <Crosshair size={17} />
          </button>
        </div>
      </div>
      <div className="map-container" ref={container}>
        <canvas
          ref={canvas}
          style={{
            width: size.width,
            height: size.height,
            cursor:
              tool === "pan"
                ? "grab"
                : tool === "select"
                  ? "default"
                  : "crosshair",
          }}
          tabIndex={0}
          role="img"
          aria-label={`Warehouse plan with ${racks.length} racks. Use arrow keys to select points and Enter to apply the active tool. A coordinate editor is also available.`}
          onContextMenu={(e) => e.preventDefault()}
          onPointerDown={(event) => {
            const p = location(event);
            canvas.current!.setPointerCapture(event.pointerId);
            drag.current = {
              ...p,
              start: nearest(p.x, p.y),
              pan: event.button === 2 || event.button === 1 || tool === "pan",
              view,
            };
          }}
          onPointerMove={(event) => {
            const p = location(event),
              d = drag.current;
            if (d) {
              if (d.pan)
                setView({
                  ...d.view,
                  x: d.view.x + p.x - d.x,
                  y: d.view.y + p.y - d.y,
                });
              else if (tool === "rack" || tool === "zone" || tool === "erase")
                setSelection({
                  x: Math.min(d.x, p.x),
                  y: Math.min(d.y, p.y),
                  w: Math.abs(p.x - d.x),
                  h: Math.abs(p.y - d.y),
                });
            } else setHover(nearest(p.x, p.y));
          }}
          onPointerUp={(event) => {
            const p = location(event),
              d = drag.current;
            drag.current = null;
            setSelection(null);
            if (!d || d.pan) return;
            const end = nearest(p.x, p.y);
            if (
              (tool === "rack" || tool === "zone" || tool === "erase") &&
              d.start &&
              end
            ) {
              const from = d.start;
              apply(
                nodes.filter(
                  (n) =>
                    n.column >= Math.min(from.column, end.column) &&
                    n.column <= Math.max(from.column, end.column) &&
                    n.row >= Math.min(from.row, end.row) &&
                    n.row <= Math.max(from.row, end.row),
                ),
              );
            } else if (end) apply([end]);
          }}
          onPointerCancel={() => {
            drag.current = null;
            setSelection(null);
          }}
          onKeyDown={(event) => {
            const current = nodes.find((n) => n.id === selected) ?? nodes[0];
            if (!current) return;
            const directions: Record<string, [number, number]> = {
              ArrowLeft: [-1, 0],
              ArrowRight: [1, 0],
              ArrowUp: [0, 1],
              ArrowDown: [0, -1],
            };
            if (directions[event.key]) {
              event.preventDefault();
              const [x, y] = directions[event.key];
              const n = nodes.find(
                (n) =>
                  n.column === current.column + x && n.row === current.row + y,
              );
              if (n) onSelect(n.id);
            }
            if (event.key === "Enter" || event.key === " ") {
              event.preventDefault();
              apply([current]);
            }
          }}
        />
        {selection && (
          <div
            className="selection-box"
            style={{
              left: selection.x,
              top: selection.y,
              width: selection.w,
              height: selection.h,
            }}
          />
        )}
        {!nodes.length && (
          <div className="map-empty">Checking warehouse geometry…</div>
        )}
        <div className="map-coordinate">
          {hover
            ? `${hover.id}  ·  ${hover.x.toFixed(1)}, ${hover.y.toFixed(1)} m${colorBy === "movement" ? ` · ${rackVisits[hover.id] ?? 0} estimated visits` : ""}`
            : "Scroll to zoom · right-drag to pan"}
        </div>
        <div className="map-north">
          Y ↑<br />
          <span>X →</span>
        </div>
      </div>
      <div className="map-legend">
        <span>
          <i className="legend-square" />
          Rack location
        </span>
        <span>
          <i className="legend-dot" />
          Workstation
        </span>
        {colorBy === "movement" && (
          <span className="movement-legend">
            <i style={{ background: "#e5e9ee" }} className="legend-square" />
            No estimated visits <i className="movement-gradient" />
            1–{maximumVisits.toLocaleString()} estimated visits
          </span>
        )}
        {result && colorBy !== "movement" && (
          <>
            <span>
              <i
                style={{ background: classColors.A }}
                className="legend-square"
              />
              A
            </span>
            <span>
              <i
                style={{ background: classColors.B }}
                className="legend-square"
              />
              B
            </span>
            <span>
              <i
                style={{ background: classColors.C }}
                className="legend-square"
              />
              C
            </span>
          </>
        )}
        <span className="legend-right">
          {project.grid.spacing_m} m ×{" "}
          {project.grid.spacing_y_m ?? project.grid.spacing_m} m grid
        </span>
      </div>
    </div>
  );
}

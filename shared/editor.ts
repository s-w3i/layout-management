import type { Row } from "./model.js";

export type LaneDirection = "both" | "forward" | "reverse";
const samePoint = (a: Row, b: Row) => a.column === b.column && a.row === b.row;
export function lanePair(a: Row, b: Row) {
  const [start, end] =
    a.column < b.column || (a.column === b.column && a.row < b.row)
      ? [a, b]
      : [b, a];
  return {
    start,
    end,
    key: `${start.column},${start.row}:${end.column},${end.row}`,
  };
}
export function laneDirections(project: Row): Map<string, LaneDirection> {
  const directions = new Map<string, LaneDirection>();
  for (const lane of project.one_way_lanes ?? []) {
    const pair = lanePair(lane.start, lane.end),
      direction = samePoint(lane.start, pair.start) ? "forward" : "reverse";
    if (directions.get(pair.key) !== "forward")
      directions.set(pair.key, direction);
  }
  return directions;
}
export function setLaneDirection(
  project: Row,
  a: Row,
  b: Row,
  direction: LaneDirection,
): Row {
  if (!["both", "forward", "reverse"].includes(direction))
    throw new Error("Choose a valid lane direction");
  const pair = lanePair(a, b),
    rules = (project.one_way_lanes ?? []).filter(
      (l: Row) => lanePair(l.start, l.end).key !== pair.key,
    );
  if (direction !== "both") {
    const from = direction === "forward" ? pair.start : pair.end,
      to = direction === "forward" ? pair.end : pair.start;
    rules.push({
      start: { column: from.column, row: from.row },
      end: { column: to.column, row: to.row },
    });
  }
  return { ...project, one_way_lanes: rules };
}
export function addLane(project: Row, a: Row, b: Row): Row {
  if (samePoint(a, b)) throw new Error("Choose two different lane endpoints");
  const pair = lanePair(a, b);
  return {
    ...project,
    added_lanes: [
      ...(project.added_lanes ?? []).filter(
        (l: Row) => lanePair(l.start, l.end).key !== pair.key,
      ),
      {
        start: { column: pair.start.column, row: pair.start.row },
        end: { column: pair.end.column, row: pair.end.row },
      },
    ],
    deleted_lanes: (project.deleted_lanes ?? []).filter(
      (l: Row) => lanePair(l.start, l.end).key !== pair.key,
    ),
  };
}
export function deleteLane(project: Row, a: Row, b: Row): Row {
  const pair = lanePair(a, b),
    p = setLaneDirection(project, a, b, "both");
  return {
    ...p,
    added_lanes: (p.added_lanes ?? []).filter(
      (l: Row) => lanePair(l.start, l.end).key !== pair.key,
    ),
    deleted_lanes: [
      ...(p.deleted_lanes ?? []).filter(
        (l: Row) => lanePair(l.start, l.end).key !== pair.key,
      ),
      {
        start: { column: pair.start.column, row: pair.start.row },
        end: { column: pair.end.column, row: pair.end.row },
      },
    ],
  };
}
export function zoneId(value: string): string {
  const id = value.trim();
  if (
    !id ||
    id.length > 120 ||
    /[\/\x00-\x1f]/.test(id) ||
    ["__proto__", "constructor", "prototype"].includes(id)
  )
    throw new Error(
      "Use a nonempty zone ID without slashes or control characters",
    );
  return id;
}
export function stockGeometryKey(project: Row): string {
  const s = project.storage_layout ?? {};
  return JSON.stringify([
    project.grid.spacing_m,
    project.grid.spacing_y_m ?? project.grid.spacing_m,
    project.warehouse_storage_defaults,
    project.machine_carrying_capacity ?? s.machine_carrying_capacity,
    s.system_type,
    s.levels_per_rack,
    s.slots_per_level,
    s.rack_height_m,
    s.clearance_m,
    project.location_attributes,
    project.attribute_catalog,
  ]);
}
export function numericDraft(
  text: string,
  min?: number,
  max?: number,
  integer = false,
): number | null {
  if (!text.trim()) return null;
  const value = Number(text);
  return !Number.isFinite(value) ||
    (min != null && value < min) ||
    (max != null && value > max) ||
    (integer && !Number.isInteger(value))
    ? null
    : value;
}
export function nextGridPoint(
  nodes: Row[],
  current: Row,
  dx: number,
  dy: number,
): Row | undefined {
  let next: Row | undefined,
    distance = Infinity;
  for (const node of nodes) {
    const delta = dx
      ? (node.column - current.column) * dx
      : (node.row - current.row) * dy;
    if (
      (dx ? node.row === current.row : node.column === current.column) &&
      delta > 0 &&
      delta < distance
    ) {
      next = node;
      distance = delta;
    }
  }
  return next;
}
export function validateGrid(grid: Row): void {
  const width = Number(grid.width_m),
    length = Number(grid.length_m),
    sx = Number(grid.spacing_m),
    sy = Number(grid.spacing_y_m ?? grid.spacing_m);
  if ([width, length, sx, sy].some((n) => !Number.isFinite(n) || n <= 0))
    throw new Error(
      "Warehouse dimensions and grid spacing must be positive numbers",
    );
  const cols = Math.ceil(width / sx - 1e-12),
    rows = Math.ceil(length / sy - 1e-12);
  if (cols > 9999 || rows > 9999 || (cols + 1) * (rows + 1) > 10000)
    throw new Error("A grid may contain at most 10,000 points");
}

import { useId, useState } from "react";
import { ChartNoAxesCombined } from "lucide-react";
import type { Row } from "../../shared/model";
import { dateRangeError, initialDateRange } from "../../shared/dates";
import { Badge, Empty, Field, Metric, Table, count } from "./components";
import { MapCanvas } from "./MapCanvas";

export default function MovementPanel({
  project,
  geometry,
  result,
  selected,
  onSelect,
  onCalculate,
  orders,
  settings,
  highlighted,
}: {
  project: Row;
  geometry: Row | null;
  result: Row;
  selected: string;
  onSelect: (id: string) => void;
  onCalculate: (range: Row) => void;
  orders: Row | null;
  settings: Row;
  highlighted?: string[];
}) {
  const bounds = {
    date_start: String(orders?.date_start ?? ""),
    date_end: String(orders?.date_end ?? ""),
  };
  const [range, setRange] = useState(() => initialDateRange(settings, bounds));
  const hasOrders = !!orders && !!bounds.date_start && !!bounds.date_end;
  const rangeError = hasOrders ? dateRangeError(range, bounds) : "";
  const movement = result.movement as Row | undefined;
  const errorId = useId();
  return (
    <div className="movement-panel">
      <div className="movement-controls">
        <Field label="Movement from date">
          <input
            type="date"
            aria-label="Movement from date"
            min={bounds.date_start}
            max={bounds.date_end}
            disabled={!hasOrders}
            aria-invalid={!!rangeError}
            aria-describedby={rangeError ? errorId : undefined}
            value={range.start_date}
            onChange={(e) => setRange({ ...range, start_date: e.target.value })}
          />
        </Field>
        <Field label="Movement through date">
          <input
            type="date"
            aria-label="Movement through date"
            min={bounds.date_start}
            max={bounds.date_end}
            disabled={!hasOrders}
            aria-invalid={!!rangeError}
            aria-describedby={rangeError ? errorId : undefined}
            value={range.end_date}
            onChange={(e) => setRange({ ...range, end_date: e.target.value })}
          />
        </Field>
        <button
          type="button"
          className="button primary"
          disabled={!hasOrders || !!rangeError}
          onClick={() => onCalculate(range)}
        >
          <ChartNoAxesCombined size={16} aria-hidden="true" />
          {movement ? "Recalculate movement" : "Calculate movement"}
        </button>
      </div>
      {hasOrders && (
        <p className="muted">
          Available order dates: {bounds.date_start} – {bounds.date_end}{" "}
          (inclusive).
        </p>
      )}
      {rangeError && (
        <p id={errorId} className="inline-warning" role="alert">
          {rangeError}
        </p>
      )}
      <p className="muted">
        Estimated handling-unit visits from distinct Store ID + Date orders.
        Racks are available for every order; inventory is not depleted. This
        estimate excludes robot congestion and queues.
      </p>
      {movement ? (
        <>
          <div className="metrics-grid three">
            <Metric
              label="Store/day orders"
              value={count(movement.fulfillment_group_count)}
              detail={`${movement.start_date || "First date"} – ${movement.end_date || "Last date"}`}
            />
            <Metric
              label="Estimated unit visits"
              value={count(movement.total_handling_unit_visits)}
              detail={`Ranked at ${movement.ranking_level} level`}
            />
            <Metric
              label="Active handling units"
              value={count(movement.unit_count)}
              detail="Movement A / B / C uses 80% / 95% of visits"
            />
          </div>
          {movement.groups_with_unassigned_skus > 0 && (
            <div className="notice warning" role="status">
              {count(movement.groups_with_unassigned_skus)} order groups include
              SKUs with no assigned stock. Their missing demand cannot be served
              by this layout.
            </div>
          )}
          <MapCanvas
            project={project}
            geometry={geometry}
            result={result}
            selected={selected}
            onSelect={onSelect}
            movementView
            highlighted={highlighted}
          />
          <p className="muted">
            {movement.ranking_level === "slot"
              ? "Rack color sums visits to its individual tote or pallet units. The table ranks each unit separately."
              : "Darker racks have more estimated visits. AMR dispatch chooses the shelf covering the most outstanding SKUs, then mean workstation distance and rack ID."}
          </p>
          {selected && (
            <p className="movement-selection" role="status">
              Selected rack <strong>{selected}</strong> ·{" "}
              {count(movement.rack_visit_counts?.[selected])} estimated visits
            </p>
          )}
          <Table
            rows={movement.units}
            searchable
            sortable
            label="Movement rankings"
            filters={[
              {
                key: "movement_class",
                label: "Movement class",
                options: ["A", "B", "C"].map((value) => ({
                  value,
                  label: value,
                })),
              },
            ]}
            columns={[
              { key: "visit_rank", label: "Rank" },
              {
                key: "rack_id",
                label: "Rack",
                render: (value) => (
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => onSelect(String(value))}
                  >
                    {value}
                  </button>
                ),
              },
              { key: "handling_unit_id", label: "Handling unit" },
              { key: "visit_count", label: "Estimated visits" },
              {
                key: "movement_class",
                label: "Movement class",
                render: (value) => (
                  <Badge tone={`class-${value}`}>{value}</Badge>
                ),
              },
              {
                key: "visit_share",
                label: "Visit share",
                render: (value) => `${(Number(value) * 100).toFixed(1)}%`,
              },
            ]}
          />
        </>
      ) : (
        <Empty
          title="Calculate the movement heat map"
          detail={
            hasOrders
              ? "Use order history to estimate shelf or slot visits for this allocation. Blank dates include all available history."
              : "Import order history in Demand or choose order history in Inventory operations, then calculate movement."
          }
        />
      )}
    </div>
  );
}

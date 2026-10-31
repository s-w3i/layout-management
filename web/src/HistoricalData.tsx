import { lazy, Suspense, useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  Download,
  Save,
  TriangleAlert,
} from "lucide-react";
import { csvString, physicalKeys, type Row } from "../../shared/model";
import {
  incompleteSkus,
  physicalLabels,
  preparationKey,
} from "../../shared/preparation";
import { dateRangeError } from "../../shared/dates";
import { demandFilters } from "../../shared/table";
import { api, download } from "./api";
import {
  Badge,
  Empty,
  Field,
  Metric,
  Panel,
  Table,
  UploadButton,
  count,
} from "./components";

const AffinityNetwork = lazy(() => import("./AffinityNetwork"));

type Props = {
  view: string;
  workspace: Row;
  busy: boolean;
  onImport: (file: File, kind: string) => void;
  onSettings: (values: Row) => void;
  onAnalyze: () => void;
  onContinue: (acknowledged: string) => void;
  onSaveSku: (
    sku: string,
    values: Row,
    revision: number | null,
  ) => Promise<boolean>;
  onGraph: (settings: Row) => Promise<Row | undefined>;
};
export default function HistoricalData(p: Props) {
  const w = p.workspace,
    orders = w.orders;
  const missing = useMemo(
    () => incompleteSkus(w.velocity, w.attributes),
    [w.velocity, w.attributes],
  );
  const reviewKey = useMemo(
    () => preparationKey(w.velocity, w.attributes, w.settings),
    [w.velocity, w.attributes, w.settings],
  );
  const [editing, setEditing] = useState<Row | null>(null),
    [draft, setDraft] = useState<Row>({});
  const [masterRevision, setMasterRevision] = useState<number | null>(null);
  const [message, setMessage] = useState(""),
    [loading, setLoading] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const dateError = orders ? dateRangeError(w.settings, orders) : "";
  useEffect(() => {
    if (editing) dialog.current?.showModal();
    else dialog.current?.close();
  }, [editing]);
  async function edit(row: Row) {
    setLoading(true);
    setMessage("");
    try {
      const masters = await api(
        `/api/manage/skus?q=${encodeURIComponent(row.sku)}`,
      );
      const master = masters.find((r: Row) => r.sku === row.sku);
      setMasterRevision(master?.revision ?? null);
      setDraft(
        Object.fromEntries(
          physicalKeys.map((key) => [
            key,
            Number(row.values[key]) > 0
              ? row.values[key]
              : (master?.attributes[key] ?? ""),
          ]),
        ),
      );
      setEditing(row);
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setLoading(false);
    }
  }
  const thresholdsValid =
    Number.isFinite(w.settings.a_limit) &&
    Number.isFinite(w.settings.b_limit) &&
    0 < w.settings.a_limit &&
    w.settings.a_limit < w.settings.b_limit &&
    w.settings.b_limit <= 1;
  const ready = w.velocity.length > 0 && !w.demandStale && !dateError;
  return (
    <div className="historical-data">
      {message && (
        <div className="notice warning" role="status">
          {message}
        </div>
      )}
      {p.view === "prepare" && (
        <>
          <div className="step-columns">
            <div className="primary-column">
              <Panel
                title="Order history"
                subtitle="Prepare historical demand before designing and slotting the warehouse."
                action={
                  <UploadButton
                    label={orders ? "Replace orders" : "Import orders"}
                    accept=".xlsx,.csv"
                    onFile={(f) => p.onImport(f, "orders")}
                  />
                }
              >
                {orders ? (
                  <>
                    <div className="source-file">
                      <div>
                        <strong>{orders.name}</strong>
                        <p>
                          {count(orders.line_count)} valid order lines ·{" "}
                          {orders.date_start} → {orders.date_end}
                        </p>
                      </div>
                      <Badge tone="green">Imported</Badge>
                    </div>
                    <div className="metrics-grid three">
                      <Metric label="SKUs" value={count(orders.sku_count)} />
                      <Metric
                        label="Store-days"
                        value={count(orders.store_day_count)}
                      />
                      <Metric
                        label="Stores"
                        value={count(orders.store_count)}
                      />
                    </div>
                    <div className="form-grid">
                      <Field
                        label="Start date"
                        hint={`Available from ${orders.date_start}`}
                      >
                        <input
                          type="date"
                          min={orders.date_start}
                          max={w.settings.end_date || orders.date_end}
                          value={w.settings.start_date}
                          onChange={(e) =>
                            p.onSettings({ start_date: e.target.value })
                          }
                        />
                      </Field>
                      <Field
                        label="End date"
                        hint={`Available through ${orders.date_end}`}
                      >
                        <input
                          type="date"
                          min={w.settings.start_date || orders.date_start}
                          max={orders.date_end}
                          value={w.settings.end_date}
                          onChange={(e) =>
                            p.onSettings({ end_date: e.target.value })
                          }
                        />
                      </Field>
                    </div>
                    {dateError && (
                      <div className="notice warning" role="alert">
                        {dateError}
                      </div>
                    )}
                    {orders.ignored_rows > 0 && (
                      <div className="notice warning">
                        <TriangleAlert size={18} />
                        <p>
                          {count(orders.ignored_rows)} rows with missing SKU or
                          invalid dates were excluded. Correct the source file
                          and reimport to include them.
                        </p>
                      </div>
                    )}
                  </>
                ) : (
                  <Empty
                    title="Import order history"
                    detail="Use an order workbook or CSV to calculate SKU velocity and co-order relationships."
                  />
                )}
              </Panel>
              <Panel
                title="Demand classification"
                subtitle="ABC classes use cumulative pick frequency; each order line counts as one pick."
                action={
                  <UploadButton
                    small
                    label="Import velocity CSV"
                    accept=".csv"
                    onFile={(f) => p.onImport(f, "velocity")}
                  />
                }
              >
                <div className="form-grid">
                  <Field label="Class A threshold (%)">
                    <input
                      type="number"
                      min="1"
                      max="99"
                      value={Math.round(w.settings.a_limit * 100)}
                      onChange={(e) =>
                        p.onSettings({ a_limit: Number(e.target.value) / 100 })
                      }
                    />
                  </Field>
                  <Field label="Class B threshold (%)">
                    <input
                      type="number"
                      min="2"
                      max="100"
                      value={Math.round(w.settings.b_limit * 100)}
                      onChange={(e) =>
                        p.onSettings({ b_limit: Number(e.target.value) / 100 })
                      }
                    />
                  </Field>
                </div>
                {!thresholdsValid && (
                  <div className="notice warning" role="alert">
                    ABC thresholds require 0 &lt; A &lt; B ≤ 100%.
                  </div>
                )}
                <button
                  className="button primary"
                  disabled={
                    !orders || !!dateError || !thresholdsValid || p.busy
                  }
                  onClick={p.onAnalyze}
                >
                  Analyze demand <ArrowRight size={15} />
                </button>
                {w.demandStale && (
                  <div className="notice warning">
                    Demand settings changed. Analyze again before using this
                    data.
                  </div>
                )}
                {w.velocity.length > 0 && (
                  <p className="muted">
                    {count(w.velocity.length)} analyzed SKUs. Open SKU velocity
                    or SKU affinity in the sidebar to explore them.
                  </p>
                )}
              </Panel>
            </div>
            <aside className="secondary-column">
              <Panel title="Preparation sequence">
                <ol className="preparation-sequence">
                  <li>Import historical orders and choose the period.</li>
                  <li>Analyze SKU velocity and review affinity groups.</li>
                  <li>
                    Complete missing physical details, or explicitly proceed
                    with the gaps.
                  </li>
                  <li>
                    Prepare warehouse geometry, stock coverage and slotting.
                  </li>
                </ol>
                <p className="muted">
                  Affinity is cosine similarity of binary SKU presence in each
                  store-day. Quantities determine stock coverage, rather than
                  affinity strength.
                </p>
              </Panel>
            </aside>
          </div>
          <Panel
            title="SKU data completeness"
            subtitle="Physical checks need positive length, width, height (m) and weight (kg). A zero weight remains unverified."
            action={
              <UploadButton
                small
                label="Import SKU attributes"
                accept=".csv"
                onFile={(f) => p.onImport(f, "attributes")}
              />
            }
          >
            {!w.velocity.length ? (
              <Empty
                title="Analyze demand first"
                detail="The review will check every SKU in the selected historical period."
              />
            ) : (
              <>
                <div className="metrics-grid three">
                  <Metric
                    label="Demand SKUs"
                    value={count(w.velocity.length)}
                  />
                  <Metric
                    label="Complete profiles"
                    value={count(w.velocity.length - missing.length)}
                  />
                  <Metric
                    label="Incomplete profiles"
                    value={count(missing.length)}
                  />
                </div>
                {missing.length ? (
                  <>
                    <div className="notice warning">
                      <TriangleAlert size={18} />
                      <p>
                        {count(missing.length)} SKUs have incomplete physical
                        data. They may still be assigned, but their physical fit
                        cannot be fully verified. Complete the details below, or
                        proceed without changes.
                      </p>
                    </div>
                    <Table
                      searchable
                      sortable
                      label="Incomplete SKU details"
                      rows={missing}
                      columns={[
                        { key: "sku", label: "SKU" },
                        { key: "velocity_class", label: "Class" },
                        {
                          key: "missing_fields",
                          label: "Missing or unverified",
                        },
                        {
                          key: "action",
                          label: "Details",
                          sortable: false,
                          render: (_, row) => (
                            <button
                              className="button button-small"
                              disabled={loading || w.demandStale}
                              onClick={() => void edit(row)}
                            >
                              Complete {row.sku}
                            </button>
                          ),
                        },
                      ]}
                    />
                  </>
                ) : (
                  <div className="notice">
                    All demand SKUs have complete physical profiles.
                  </div>
                )}
                <div className="inline-actions">
                  <button
                    className={`button ${missing.length ? "" : "primary"}`}
                    disabled={!ready}
                    onClick={() => p.onContinue(reviewKey)}
                  >
                    {missing.length
                      ? "Proceed without changes"
                      : "Continue to warehouse setup"}{" "}
                    <ArrowRight size={15} />
                  </button>
                </div>
                {w.preparationAcknowledged === reviewKey &&
                  missing.length > 0 && (
                    <p className="muted">
                      You have acknowledged these gaps for the current inputs.
                      Changed inputs require another review.
                    </p>
                  )}
              </>
            )}
          </Panel>
        </>
      )}
      {p.view === "velocity" && (
        <Panel
          title="SKU velocity"
          subtitle="Pick frequency and quantity for the selected historical period. Click a column heading to sort."
          action={
            <button
              className="button button-small"
              disabled={!w.velocity.length}
              onClick={() =>
                download(
                  "sku-velocity.csv",
                  csvString(
                    w.velocity.map(
                      ({ sku_requirements: _, ...row }: Row) => row,
                    ),
                  ),
                  "text/csv",
                )
              }
            >
              <Download size={14} /> Export CSV
            </button>
          }
        >
          {w.demandStale && (
            <div className="notice warning">
              This analysis is out of date. Return to Data preparation and
              analyze the changed period.
            </div>
          )}
          {w.velocity.length ? (
            <>
              <div className="metrics-grid three">
                {["A", "B", "C"].map((cl) => (
                  <Metric
                    key={cl}
                    label={`Class ${cl} SKUs`}
                    value={count(
                      w.velocity.filter((r: Row) => r.velocity_class === cl)
                        .length,
                    )}
                  />
                ))}
              </div>
              <Table
                searchable
                sortable
                label="SKU demand"
                rows={w.velocity}
                filters={demandFilters}
                columns={[
                  { key: "sku", label: "SKU" },
                  {
                    key: "velocity_class",
                    label: "Class",
                    render: (_, r) => (
                      <Badge
                        tone={
                          r.velocity_class === "A"
                            ? "blue"
                            : r.velocity_class === "B"
                              ? "amber"
                              : "neutral"
                        }
                      >
                        {r.velocity_class}
                      </Badge>
                    ),
                  },
                  { key: "pick_frequency", label: "Picks" },
                  { key: "total_quantity_ea", label: "Quantity (EA)" },
                  { key: "active_days", label: "Active days" },
                ]}
              />
            </>
          ) : (
            <Empty
              title="No velocity analysis"
              detail="Import and analyze orders, or import a velocity CSV in Data preparation."
            />
          )}
        </Panel>
      )}
      {p.view === "affinity" && (
        <Affinity workspace={w} load={p.onGraph} busy={p.busy} />
      )}
      <dialog
        aria-labelledby="complete-sku-title"
        ref={dialog}
        className="help-dialog"
        onCancel={() => setEditing(null)}
      >
        <form
          className="dialog-inner"
          onSubmit={async (e) => {
            e.preventDefault();
            if (!editing) return;
            setLoading(true);
            setMessage("");
            try {
              if (
                await p.onSaveSku(
                  editing.sku,
                  Object.fromEntries(
                    physicalKeys.map((key) => [key, Number(draft[key])]),
                  ),
                  masterRevision,
                )
              ) {
                setEditing(null);
                setMessage(
                  `Saved physical details for ${editing.sku}. Recalculate stock requirements before generating a new layout.`,
                );
              } else {
                setMessage(
                  "Save failed. Close this dialog to see the error, then reopen the SKU to refresh its saved details.",
                );
              }
            } catch (error) {
              setMessage((error as Error).message);
            } finally {
              setLoading(false);
            }
          }}
        >
          <h2 id="complete-sku-title">Complete SKU {editing?.sku}</h2>
          <p>
            Save physical details to this workspace and the SKU master. Existing
            names, units and other attributes are preserved.
          </p>
          <div className="form-grid">
            {physicalKeys.map((key) => (
              <Field key={key} label={`${physicalLabels[key]} *`}>
                <input
                  required
                  type="number"
                  min="0.000001"
                  step="any"
                  value={draft[key] ?? ""}
                  onChange={(e) =>
                    setDraft({ ...draft, [key]: e.target.value })
                  }
                />
              </Field>
            ))}
          </div>
          {message && (
            <p className="notice warning" role="alert">
              {message}
            </p>
          )}
          <p className="muted">
            Required fields are marked *. Available master data is prefilled for
            missing details.
          </p>
          <div className="dialog-actions">
            <button
              type="button"
              className="button"
              disabled={loading || p.busy}
              onClick={() => setEditing(null)}
            >
              Cancel
            </button>
            <button className="button primary" disabled={loading || p.busy}>
              <Save size={15} /> Save SKU details
            </button>
          </div>
        </form>
      </dialog>
    </div>
  );
}
function Affinity({
  workspace: w,
  load,
  busy,
}: {
  workspace: Row;
  load: Props["onGraph"];
  busy: boolean;
}) {
  const initial = w.analysis?.affinity_graph;
  const [graph, setGraph] = useState<Row | null>(initial || null);
  const [sku, setSku] = useState(
    initial?.focus_sku || w.velocity[0]?.sku || "",
  );
  const [minimum, setMinimum] = useState(initial?.minimum_shared || 1),
    [limit, setLimit] = useState(
      initial?.network_version &&
        [12, 24, 48, 96].includes(initial.neighbor_limit)
        ? initial.neighbor_limit
        : 48,
    );
  const [inspectSku, setInspectSku] = useState(
    initial?.focus_sku || w.velocity[0]?.sku || "",
  );
  useEffect(() => {
    setGraph(initial || null);
    setInspectSku(initial?.focus_sku || w.velocity[0]?.sku || "");
    setSku(initial?.focus_sku || w.velocity[0]?.sku || "");
  }, [w.analysis, w.velocity]);
  async function show(focus = sku) {
    const result = await load({
      affinity_focus_sku: focus,
      affinity_minimum_shared: minimum,
      affinity_neighbor_limit: limit,
    });
    if (result) {
      setGraph(result);
      setSku(focus);
      setInspectSku(focus);
    }
  }
  const focus = graph?.focus_sku,
    neighbors: Row[] = graph?.relationships || [];
  const dirty =
    graph &&
    (focus !== sku ||
      graph.minimum_shared !== minimum ||
      graph.neighbor_limit !== limit);
  return (
    <Panel
      title="3D SKU affinity network"
      subtitle="Explore an interconnected SKU neighborhood in 3D. Select a node to inspect its demand and connections, or make it the new focus."
    >
      {!w.orders || !w.velocity.length ? (
        <Empty
          title="Order history is needed"
          detail="Analyze historical orders in Data preparation to build the affinity graph. A velocity CSV alone does not contain co-order relationships."
        />
      ) : (
        <>
          <div className="affinity-controls">
            <Field label="Focus SKU">
              <select value={sku} onChange={(e) => setSku(e.target.value)}>
                {[...w.velocity]
                  .sort((a: Row, b: Row) => a.sku.localeCompare(b.sku))
                  .map((r: Row) => (
                    <option key={r.sku} value={r.sku}>
                      {r.sku} · {r.velocity_class} · {count(r.pick_frequency)}{" "}
                      picks
                    </option>
                  ))}
              </select>
            </Field>
            <Field label="Minimum shared store-days">
              <input
                type="number"
                min="1"
                step="1"
                value={minimum}
                onChange={(e) => setMinimum(Number(e.target.value))}
              />
            </Field>
            <Field label="Maximum neighbors">
              <select
                value={limit}
                onChange={(e) => setLimit(Number(e.target.value))}
              >
                {[12, 24, 48, 96].map((n) => (
                  <option key={n}>{n}</option>
                ))}
              </select>
            </Field>
            <button
              className="button primary"
              disabled={
                busy ||
                w.demandStale ||
                minimum < 1 ||
                !Number.isInteger(minimum)
              }
              onClick={() => void show()}
            >
              Show relationships
            </button>
          </div>
          {w.demandStale ? (
            <div className="notice warning">
              The historical inputs changed. Analyze demand again in Data
              preparation before exploring affinity.
            </div>
          ) : (
            <>
              {(dirty || (graph && !graph.network_version)) && (
                <div className="notice">
                  {graph && !graph.network_version
                    ? "This saved view contains focus links only. Select Show relationships to load connections between neighboring SKUs."
                    : "Filters changed. Select Show relationships to update the network."}
                </div>
              )}
              {graph && (
                <>
                  <p className="muted">
                    {graph.date_start || w.analysis?.summary?.date_start} →{" "}
                    {graph.date_end || w.analysis?.summary?.date_end} · Showing{" "}
                    {neighbors.length} of{" "}
                    {count(graph.eligible_relationship_count)} matching
                    neighbors for <strong>{focus}</strong>. The network includes
                    connections between these neighbors.
                  </p>
                  <Suspense
                    fallback={
                      <div className="network-loading" role="status">
                        Loading 3D network…
                      </div>
                    }
                  >
                    <AffinityNetwork
                      graph={graph}
                      velocity={w.velocity}
                      selected={inspectSku}
                      onSelect={setInspectSku}
                      onExplore={(sku) => void show(sku)}
                      busy={busy}
                    />
                  </Suspense>
                  {graph.network_relationship_count >
                    (graph.network_relationships?.length || 0) && (
                    <p className="muted">
                      Showing {graph.network_relationships.length} of{" "}
                      {count(graph.network_relationship_count)} connections in
                      this neighborhood, keeping every focus link and the
                      strongest remaining links.
                    </p>
                  )}
                  {!neighbors.length && (
                    <div className="notice">
                      No co-order neighbors meet this threshold. Reduce the
                      minimum shared store-days or choose another SKU.
                    </div>
                  )}
                  <Table
                    searchable
                    sortable
                    label="Affinity connections"
                    rows={
                      graph.network_relationships || graph.relationships || []
                    }
                    columns={[
                      { key: "sku_a", label: "SKU A" },
                      { key: "sku_b", label: "SKU B" },
                      { key: "shared_store_days", label: "Shared store-days" },
                      {
                        key: "cosine",
                        label: "Cosine similarity",
                        render: (_, r) => r.cosine.toFixed(4),
                      },
                      {
                        key: "group",
                        label: "Inspect",
                        sortable: false,
                        render: (_, r) => (
                          <div className="inline-actions">
                            <button
                              className="button button-small"
                              onClick={() => setInspectSku(r.sku_a)}
                            >
                              Inspect {r.sku_a}
                            </button>
                            <button
                              className="button button-small"
                              onClick={() => setInspectSku(r.sku_b)}
                            >
                              Inspect {r.sku_b}
                            </button>
                          </div>
                        ),
                      },
                    ]}
                  />
                </>
              )}
              {!graph && (
                <div className="notice">
                  Choose a SKU and select Show relationships to explore its
                  group.
                </div>
              )}
            </>
          )}
        </>
      )}
    </Panel>
  );
}

import { useEffect, useRef, useState } from "react";
import { Download, RefreshCw, Undo2 } from "lucide-react";
import type { Row } from "../../shared/model";
import { findInventory, scenarioRows } from "../../shared/operations";
import { api, download, engine, upload } from "./api";
import {
  Badge,
  Empty,
  Field,
  Panel,
  Table,
  UploadButton,
  count,
} from "./components";
import { MapCanvas } from "./MapCanvas";
import MovementPanel from "./MovementPanel";

export default function InventoryOperations({
  baseline,
  project,
  geometry,
  settings,
  orders,
}: {
  baseline: Row | null;
  project: Row;
  geometry: Row | null;
  settings: Row;
  orders: Row | null;
}) {
  const [draft, setDraft] = useState<Row | null>(null),
    [previous, setPrevious] = useState<{
      layout: Row;
      geometry: Row | null;
    } | null>(null);
  const [importedGeometry, setImportedGeometry] = useState<Row | null>(null);
  const [customHistory, setHistory] = useState<Row | null>(null);
  const history = customHistory ?? orders;
  const [query, setQuery] = useState(""),
    [selected, setSelected] = useState("");
  const [mode, setMode] = useState("load"),
    [source, setSource] = useState(""),
    [target, setTarget] = useState("");
  const [busy, setBusy] = useState(""),
    [error, setError] = useState("");
  const abort = useRef<AbortController | null>(null),
    job = useRef("");
  useEffect(
    () => () => {
      abort.current?.abort();
      if (job.current)
        void api(`/api/jobs/${job.current}`, { method: "DELETE" }).catch(
          () => {},
        );
    },
    [],
  );
  const current = draft ?? baseline;
  const requestFor = (layout: Row) => ({
    project: layout.project ?? project,
    settings: { ...settings, ...layout.settings },
    sku_rows: scenarioRows(layout),
  });
  const run = async (
    label: string,
    task: (
      progress: (text: string, id: string) => void,
      signal: AbortSignal,
    ) => Promise<void>,
  ) => {
    const controller = new AbortController();
    abort.current = controller;
    setBusy(label);
    setError("");
    try {
      await task((text, id) => {
        setBusy(text);
        job.current = id;
      }, controller.signal);
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    } finally {
      job.current = "";
      setBusy("");
      abort.current = null;
    }
  };
  const calculate = (range: Row) => {
    if (!current || !history) return;
    void run("Calculating movement", async (progress, signal) => {
      const movement = await engine(
        {
          command: "movement",
          assignments: current.assignments,
          handling_unit_type: current.handling_unit_type,
          settings: range,
          orders_id: history.id,
        },
        progress,
        signal,
      );
      setDraft({ ...current, movement });
    });
  };
  const swap = () => {
    if (!current) return;
    void run("Validating swap", async (progress, signal) => {
      const next = await engine(
        {
          ...requestFor(current),
          command: "swap",
          layout: current,
          mode,
          source,
          target,
        },
        progress,
        signal,
      );
      next.modified_at = new Date().toISOString();
      next.operation_log.at(-1).timestamp = next.modified_at;
      if (current.movement && history)
        next.movement = await engine(
          {
            command: "movement",
            assignments: next.assignments,
            handling_unit_type: next.handling_unit_type,
            orders_id: history.id,
            settings: {
              start_date: current.movement.start_date,
              end_date: current.movement.end_date,
            },
          },
          progress,
          signal,
        );
      setPrevious({ layout: current, geometry: importedGeometry });
      setDraft(next);
      setSource("");
      setTarget("");
    });
  };
  const open = (file: File) =>
    void run("Opening inventory scenario", async (progress, signal) => {
      if (file.size > 64 * 1024 * 1024)
        throw new Error("Layout files must be under 64 MB.");
      const data = JSON.parse(await file.text());
      const request = requestFor(data);
      const validation = await engine(
        { ...request, command: "validate", assignments: data.assignments },
        progress,
        signal,
      );
      if (!validation.passed)
        throw new Error(
          `Layout validation failed: ${validation.errors.join(" ")}`,
        );
      const nextGeometry = await engine(
        { command: "geometry", project: request.project },
        progress,
        signal,
      );
      setPrevious(null);
      setDraft({
        ...data,
        project: request.project,
        validation,
        movement: undefined,
      });
      setImportedGeometry(nextGeometry);
      setSelected("");
      setQuery("");
      setSource("");
      setTarget("");
    });
  const rows: Row[] = current?.assignments ?? [];
  const matches = findInventory(rows, query);
  const highlighted = query.trim()
    ? [
        ...new Set(
          matches
            .filter((row) => row.assignment_status === "ASSIGNED")
            .map((row) => String(row.rack_id)),
        ),
      ]
    : [];
  const inventory = selected
    ? matches.filter((row) => row.rack_id === selected)
    : matches;
  const assigned = rows.filter((row) => row.assignment_status === "ASSIGNED");
  const racks = [...new Set(assigned.map((row) => String(row.rack_id)))].sort();
  const choices =
    mode === "load"
      ? assigned
          .filter((row) => Number(row.occupied_slot_count) === 1)
          .map((row) => ({
            value: String(row.inventory_load_id),
            label: `${row.sku} · ${row.inventory_load_id} · ${row.storage_location_address}`,
          }))
      : racks.map((rack) => ({ value: rack, label: rack }));
  return (
    <div className="inventory-operations">
      <div className="notice">
        <p>
          Try changes in a separate scenario. Export the scenario JSON to save
          it; switching sections keeps it for this session. Changing planning
          inputs, generating a new layout or reloading starts from the saved
          baseline.
        </p>
      </div>
      {busy && (
        <div className="notice" role="status">
          <p>{busy}</p>
          <button
            type="button"
            className="button button-small"
            onClick={() => {
              abort.current?.abort();
              if (job.current)
                void api(`/api/jobs/${job.current}`, {
                  method: "DELETE",
                }).catch(() => {});
            }}
          >
            Cancel
          </button>
        </div>
      )}
      {error && (
        <div className="notice warning" role="alert">
          {error}
        </div>
      )}
      {(current?.validation?.warnings ?? []).map((warning: string) => (
        <div key={warning} className="notice warning" role="status">
          {warning}
        </div>
      ))}
      <fieldset disabled={!!busy} className="operations-fieldset">
        <Panel
          title="Inventory scenario"
          subtitle="Open a saved layout or work from the generated layout in this workspace."
          action={
            <Badge tone={draft ? "amber" : "green"}>
              {draft ? "Scenario copy" : "Generated baseline"}
            </Badge>
          }
        >
          <div className="inline-actions operations-actions">
            <UploadButton
              label="Open layout JSON"
              accept=".json"
              onFile={open}
              small
            />
            <UploadButton
              label="Choose order history"
              accept=".csv,.xlsx"
              small
              onFile={(file) =>
                void run("Importing order history", async (progress) => {
                  setHistory(await upload(file, "orders", progress));
                  if (current) setDraft({ ...current, movement: undefined });
                })
              }
            />
            <button
              type="button"
              className="button button-small"
              disabled={!current}
              onClick={() =>
                download("inventory-scenario.slotting.json", current)
              }
            >
              <Download size={15} aria-hidden="true" />
              Export scenario
            </button>
            <button
              type="button"
              className="button button-small"
              disabled={!previous}
              onClick={() => {
                setDraft(previous!.layout);
                setImportedGeometry(previous!.geometry);
                setPrevious(null);
                setSelected("");
                setSource("");
                setTarget("");
              }}
            >
              <Undo2 size={15} aria-hidden="true" />
              Undo last action
            </button>
            <button
              type="button"
              className="button button-small"
              disabled={!draft || !baseline}
              onClick={() => {
                setPrevious({ layout: current!, geometry: importedGeometry });
                setDraft(null);
                setImportedGeometry(null);
                setSelected("");
                setQuery("");
                setSource("");
                setTarget("");
                setError("");
              }}
            >
              <RefreshCw size={15} aria-hidden="true" />
              Reset to generated layout
            </button>
          </div>
          <p className="muted">
            {history
              ? `Order history ready · ${count(history.store_day_count ?? history.group_count ?? history.rows)} records/groups`
              : "Choose order history to enable movement estimates."}
          </p>
        </Panel>
        {current ? (
          <>
            <div className="step-columns operations-columns">
              <div className="primary-column">
                <Panel
                  title="Locate inventory"
                  subtitle="Exact SKU matches take priority. Select a highlighted rack to narrow the inventory table."
                >
                  <div className="operations-search">
                    <Field label="Find SKU">
                      <input
                        placeholder="SKU code or part of a code"
                        value={query}
                        onChange={(e) => {
                          setQuery(e.target.value);
                          setSelected("");
                        }}
                      />
                    </Field>
                    <Field label="Inspect rack">
                      <select
                        aria-label="Inspect rack"
                        value={selected}
                        onChange={(e) => setSelected(e.target.value)}
                      >
                        <option value="">All matching racks</option>
                        {racks.map((rack) => (
                          <option key={rack} value={rack}>
                            {rack}
                          </option>
                        ))}
                      </select>
                    </Field>
                  </div>
                  <p className="muted" role="status">
                    {count(matches.length)} matching loads ·{" "}
                    {query.trim() ? highlighted.length : racks.length} racks
                    {selected ? ` · inspecting ${selected}` : ""}
                  </p>
                  <MapCanvas
                    project={current.project ?? project}
                    geometry={importedGeometry ?? geometry}
                    result={current}
                    selected={selected}
                    onSelect={setSelected}
                    highlighted={highlighted}
                  />
                </Panel>
              </div>
              <aside className="secondary-column">
                <Panel
                  title="Trial position swap"
                  subtitle="Select exact inventory loads for SKU swaps, or two occupied AMR rack locations for a whole-shelf swap."
                >
                  <Field label="Swap type">
                    <select
                      aria-label="Swap type"
                      value={mode}
                      onChange={(e) => {
                        setMode(e.target.value);
                        setSource("");
                        setTarget("");
                      }}
                    >
                      <option value="load">SKU load positions</option>
                      {current.handling_unit_type === "AMR shelf" && (
                        <option value="shelf">Whole AMR shelves</option>
                      )}
                    </select>
                  </Field>
                  <Field
                    label={
                      mode === "load"
                        ? "Source inventory load"
                        : "Source rack location"
                    }
                  >
                    <select
                      aria-label={
                        mode === "load"
                          ? "Source inventory load"
                          : "Source rack location"
                      }
                      value={source}
                      onChange={(e) => setSource(e.target.value)}
                    >
                      <option value="">Choose source</option>
                      {choices.map((choice) => (
                        <option key={choice.value} value={choice.value}>
                          {choice.label}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field
                    label={
                      mode === "load"
                        ? "Target inventory load"
                        : "Target rack location"
                    }
                  >
                    <select
                      aria-label={
                        mode === "load"
                          ? "Target inventory load"
                          : "Target rack location"
                      }
                      value={target}
                      onChange={(e) => setTarget(e.target.value)}
                    >
                      <option value="">Choose target</option>
                      {choices
                        .filter((choice) => choice.value !== source)
                        .map((choice) => (
                          <option key={choice.value} value={choice.value}>
                            {choice.label}
                          </option>
                        ))}
                    </select>
                  </Field>
                  <p className="muted">
                    SKU swaps use single-cell loads. Shelf swaps preserve
                    complete footprints and shelf identities. Every swap checks
                    physical fit, attributes, weight, and same-SKU limits.
                  </p>
                  <button
                    type="button"
                    className="button primary"
                    disabled={!source || !target || source === target}
                    onClick={swap}
                  >
                    Validate and swap
                  </button>
                </Panel>
              </aside>
            </div>
            <Panel
              title={
                selected ? `Inventory at ${selected}` : "Matching inventory"
              }
              subtitle="Click an inventory load ID to choose it as the source, then a different load as the target."
            >
              <Table
                rows={inventory}
                label="Scenario inventory"
                sortable
                empty="No inventory matches this SKU and rack selection."
                columns={[
                  { key: "sku", label: "SKU" },
                  {
                    key: "inventory_load_id",
                    label: "Inventory load",
                    render: (value, row) => (
                      <button
                        type="button"
                        className="text-button"
                        disabled={
                          row.assignment_status !== "ASSIGNED" ||
                          Number(row.occupied_slot_count) !== 1
                        }
                        onClick={() => {
                          setMode("load");
                          if (mode !== "load" || !source || target) {
                            setSource(String(value));
                            setTarget("");
                          } else if (source !== value) setTarget(String(value));
                        }}
                      >
                        {value}
                      </button>
                    ),
                  },
                  { key: "quantity_ea", label: "EA" },
                  { key: "velocity_class", label: "ABC" },
                  {
                    key: "storage_location_address",
                    label: "Storage location",
                  },
                  { key: "occupied_dynamic_address", label: "Dynamic address" },
                  {
                    key: "assignment_status",
                    label: "Assignment",
                    render: (value, row) =>
                      value === "ASSIGNED" ? (
                        <Badge tone="green">Assigned</Badge>
                      ) : (
                        <details className="assignment-issue">
                          <summary>Unassigned</summary>
                          <p>{row.compatibility_issues?.join(" ")}</p>
                        </details>
                      ),
                  },
                  { key: "compatibility_status", label: "Physical check" },
                ]}
              />
            </Panel>
            <Panel
              title="Rack movement heat map"
              subtitle="Rank handling units using order history, then compare their expected activity after a trial swap."
            >
              <MovementPanel
                key={history?.id ?? "no-orders"}
                project={current.project ?? project}
                geometry={importedGeometry ?? geometry}
                result={current}
                selected={selected}
                onSelect={setSelected}
                onCalculate={calculate}
                orders={history}
                settings={settings}
                highlighted={highlighted}
              />
            </Panel>
            <Panel
              title="Operation log"
              subtitle="Successful trial swaps are included in the exported scenario."
            >
              <Table
                rows={current.operation_log ?? []}
                label="Operation log"
                columns={[
                  { key: "timestamp", label: "Time" },
                  { key: "message", label: "Operation" },
                ]}
                empty="No trial swaps in this scenario."
              />
            </Panel>
          </>
        ) : (
          <Empty
            title="Generate or open a layout"
            detail="Generate a layout in the planning workflow, or open a self-contained layout JSON with stock targets. Older exports use the warehouse configuration in this workspace."
          />
        )}
      </fieldset>
    </div>
  );
}

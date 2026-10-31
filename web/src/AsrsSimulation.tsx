import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import {
  Activity,
  CirclePause,
  Play,
  Square,
  Download,
  Link,
  LoaderCircle,
  RefreshCw,
} from "lucide-react";
import {
  defaultAsrsSettings,
  type AsrsSettings,
  type AsrsModel,
  type DateMode,
} from "../../shared/asrs";
import { exactQuantity } from "../../shared/inventory";
import type { Row } from "../../shared/model";
import { api } from "./api";
import { Badge, Field, Metric, Panel, Table, count } from "./components";

const Layout3D = lazy(() => import("./Layout3D"));
const ea = (q: unknown) => exactQuantity(Number(q ?? 0));
const modes: [DateMode, string][] = [
  ["one_date", "One date"],
  ["multiple_dates", "Multiple dates"],
  ["one_month", "One month"],
  ["multiple_months", "Multiple months"],
  ["full", "Full dataset"],
];
const terminal = new Set([
  "completed",
  "completed_with_shortages",
  "failed",
  "stopped",
]);
const eventKinds = [
  "OPENING",
  "ORDER_OUT",
  "REPLENISH_REQUEST",
  "REPLENISH_IN",
  "RETURN_IN",
  "RETURN_PUTAWAY",
  "UNFULFILLED",
  "TOTE_PICKUP",
  "WORKSTATION_ARRIVAL",
  "WORKSTATION_DEPARTURE",
  "TOTE_RETURN",
  "NO_MOVEMENT",
];

export default function AsrsSimulation({
  visible,
  workspaceId,
}: {
  visible: boolean;
  workspaceId: string;
}) {
  const [url, setUrl] = useState(window.location.origin),
    [token, setToken] = useState(""),
    [connection, setConnection] = useState<Row | null>(null);
  const [warehouse, setWarehouse] = useState(workspaceId),
    [dataset, setDataset] = useState(""),
    [calendar, setCalendar] = useState<Row | null>(null);
  const [mode, setMode] = useState<DateMode>("one_date"),
    [dates, setDates] = useState<string[]>([]),
    [months, setMonths] = useState<string[]>([]);
  const [settings, setSettings] = useState<AsrsSettings>(() =>
    structuredClone(defaultAsrsSettings),
  );
  const [name, setName] = useState("ASRS simulation"),
    [busy, setBusy] = useState(""),
    [error, setError] = useState("");
  const [history, setHistory] = useState<Row[]>([]),
    [id, setId] = useState(
      localStorage.getItem("layout-generator-asrs-run") || "",
    ),
    [run, setRun] = useState<Row | null>(null);
  const [configuration, setConfiguration] = useState<{
    id: string;
    release: Row;
    model: AsrsModel;
  } | null>(null);
  const [view, setView] = useState("3d"),
    [crane, setCrane] = useState(""),
    [rack, setRack] = useState("");
  const [tab, setTab] = useState("events"),
    [rows, setRows] = useState<Row[]>([]),
    [stock, setStock] = useState<Row[]>([]);
  const [sku, setSku] = useState(""),
    [sourceRow, setSourceRow] = useState(""),
    [kind, setKind] = useState("");
  const [cursor, setCursor] = useState(0),
    [cursors, setCursors] = useState<number[]>([]),
    [page, setPage] = useState<Row | null>(null),
    [follow, setFollow] = useState(true);
  const [received, setReceived] = useState(performance.now());
  const [setupOpen, setSetupOpen] = useState(
    !localStorage.getItem("layout-generator-asrs-run"),
  );
  const source = () => ({ base_url: url, token });
  const execute = async (label: string, action: () => Promise<void>) => {
    setBusy(label);
    setError("");
    try {
      await action();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy("");
    }
  };
  const selectRun = (next: string) => {
    setSetupOpen(!next);
    setId(next);
    setConfiguration(null);
    setRun(null);
    setRows([]);
    setStock([]);
    setCursor(0);
    setCursors([]);
    setFollow(true);
    localStorage.setItem("layout-generator-asrs-run", next);
  };
  const list = async () => {
    const values: Row[] = await api("/api/asrs/runs");
    setHistory(values);
    return values;
  };
  useEffect(() => {
    if (visible)
      void list()
        .then((values) => {
          if (!id && values.length) selectRun(values[0].id);
        })
        .catch((e) => setError(e.message));
  }, [visible]);
  useEffect(() => {
    if (!visible || !id) return;
    let stopped = false,
      timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const refresh = async () => {
      if (document.hidden) {
        timer = setTimeout(refresh, 1000);
        return;
      }
      try {
        const value = await api(`/api/asrs/runs/${id}`, {
          signal: controller.signal,
        });
        if (stopped) return;
        setRun(value);
        setReceived(performance.now());
        if (
          value.status !== "preparing" &&
          value.state &&
          configuration?.id !== id
        ) {
          const model = await api(`/api/asrs/runs/${id}/model`, {
            signal: controller.signal,
          });
          if (!stopped) {
            setConfiguration({ id, ...model });
            setCrane((current) =>
              model.model.cranes.some((c: Row) => c.id === current)
                ? current
                : model.model.cranes[0]?.id || "",
            );
          }
        }
        if (!stopped)
          timer = setTimeout(refresh, terminal.has(value.status) ? 10000 : 600);
      } catch (e) {
        if (!stopped) {
          setError((e as Error).message);
          timer = setTimeout(refresh, 3000);
        }
      }
    };
    void refresh();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [id, visible, configuration?.id]);
  useEffect(() => {
    if (!visible || !connection || !dataset) return;
    let active = true;
    setCalendar(null);
    void api("/api/asrs/calendar", {
      method: "POST",
      body: JSON.stringify({ ...source(), dataset_id: dataset }),
    })
      .then((value) => {
        if (!active) return;
        setCalendar(value);
        setDates(value.dates.slice(0, 1));
        setMonths(value.months.slice(0, 1));
      })
      .catch((e) => {
        if (active) setError(e.message);
      });
    return () => {
      active = false;
    };
  }, [connection, dataset]);
  useEffect(() => {
    setCursor(0);
    setCursors([]);
    setRows([]);
  }, [id, sku, sourceRow, kind, tab, follow]);
  const query = new URLSearchParams({
    sku,
    ...(tab === "events" ? { source_row: sourceRow, kind } : {}),
    after: String(cursor),
    limit: "200",
    ...(follow && tab === "events" ? { latest: "1" } : {}),
  }).toString();
  useEffect(() => {
    if (!visible || !id || !run?.state) return;
    let stopped = false,
      timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    const refresh = async () => {
      if (document.hidden) {
        timer = setTimeout(refresh, 1000);
        return;
      }
      try {
        if (tab === "stock") {
          const values = await api(
            `/api/asrs/runs/${id}/stock?sku=${encodeURIComponent(sku)}`,
            { signal: controller.signal },
          );
          if (!stopped) setStock(values);
        } else {
          const value = await api(`/api/asrs/runs/${id}/${tab}?${query}`, {
            signal: controller.signal,
          });
          if (!stopped) {
            setRows(value.rows);
            setPage(value);
          }
        }
      } catch (e) {
        if (!stopped) setError((e as Error).message);
      }
      if (!stopped && run.status === "running")
        timer = setTimeout(refresh, 1000);
    };
    void refresh();
    return () => {
      stopped = true;
      clearTimeout(timer);
      controller.abort();
    };
  }, [id, visible, tab, query, run?.status, !!run?.state]);
  const control = (action: string, extra: Row = {}) =>
    execute("Updating simulation", async () => {
      const value = await api(`/api/asrs/runs/${id}/control`, {
        method: "POST",
        body: JSON.stringify({ action, ...extra }),
      });
      setRun(value);
      setReceived(performance.now());
      await list();
    });
  const state = run?.state;
  const speed = run?.config.settings.speed ?? settings.speed;
  const live = useMemo(
    () =>
      state && configuration
        ? {
            cranes: state.cranes,
            now: state.now,
            received_at: received,
            axes: run!.config.settings.axes,
            speed: run!.config.settings.speed,
            running: visible && run!.status === "running",
            quantities: Object.fromEntries(
              configuration.model.loads.map((l, i) => [
                l.id,
                state.load_quantities[i],
              ]),
            ),
          }
        : undefined,
    [state, configuration, received, visible, run?.status],
  );
  const selectedCrane = state?.cranes.find((c: Row) => c.id === crane);
  const selectedWarehouse = connection?.warehouses.find(
    (w: Row) => w.id === warehouse,
  );
  const validWarehouse =
    selectedWarehouse?.system_type === "Mini-load ASRS" &&
    selectedWarehouse?.published_release_id;
  const chosen =
    mode === "full"
      ? (calendar?.dates?.length ?? 0)
      : mode.includes("month")
        ? (calendar?.dates ?? []).filter((d: string) =>
            months.includes(d.slice(0, 7)),
          ).length
        : dates.length;
  const finishedLines = state
    ? state.totals.completed +
      state.totals.returned +
      state.totals.zero +
      state.totals.unfulfilled
    : 0;
  const skuStock = useMemo(() => {
    const summary = new Map<string, Row>();
    for (const load of stock) {
      const row = summary.get(load.sku) ?? {
        sku: load.sku,
        initial: 0,
        current: 0,
        buffer: configuration?.model.buffers[load.sku] ?? 0,
        receiving: load.receiving_micro,
      };
      row.initial += load.initial_micro;
      row.current += load.quantity_micro;
      summary.set(load.sku, row);
    }
    return [...summary.values()];
  }, [stock, configuration]);
  return (
    <div className="asrs-page">
      {error && (
        <div className="notice notice-error" role="alert">
          <strong>Simulation needs attention</strong>
          <p>{error}</p>
          <button className="button button-small" onClick={() => setError("")}>
            Dismiss
          </button>
        </div>
      )}
      <details
        className="asrs-setup"
        open={setupOpen}
        onToggle={(e) => setSetupOpen(e.currentTarget.open)}
      >
        <summary>New simulation · connection and period settings</summary>
        <div className="asrs-setup-panels">
          <Panel
            title="Connect to a deployed warehouse"
            subtitle="Read a published layout and retained order lines through the warehouse API. Simulated inventory stays in its own ledger."
          >
            <div className="form-grid">
              <Field
                label="Warehouse API origin"
                hint="This server connects automatically. For another deployment, use its origin or your SSH tunnel."
              >
                <input
                  type="url"
                  value={url}
                  onChange={(e) => {
                    setUrl(e.target.value);
                    setConnection(null);
                    setCalendar(null);
                  }}
                />
              </Field>
              <Field
                label="Read API key"
                hint="Required for another deployment. Kept in memory only."
              >
                <input
                  type="password"
                  autoComplete="off"
                  value={token}
                  onChange={(e) => {
                    setToken(e.target.value);
                    setConnection(null);
                  }}
                />
              </Field>
            </div>
            <button
              className="button"
              disabled={!!busy}
              onClick={() =>
                void execute("Connecting", async () => {
                  const value = await api("/api/asrs/connect", {
                    method: "POST",
                    body: JSON.stringify(source()),
                  });
                  setConnection(value);
                  setWarehouse(
                    value.warehouses.some(
                      (w: Row) =>
                        w.id === workspaceId && w.published_release_id,
                    )
                      ? workspaceId
                      : value.warehouses.find(
                          (w: Row) =>
                            w.system_type === "Mini-load ASRS" &&
                            w.published_release_id,
                        )?.id || "",
                  );
                  setDataset(
                    value.datasets.find(
                      (d: Row) => d.raw_count > 0 && d.status === "ready",
                    )?.id || "",
                  );
                })
              }
            >
              <Link size={16} aria-hidden="true" />
              {busy === "Connecting" ? "Connecting…" : "Connect"}
            </button>
            {connection && (
              <p className="asrs-help" role="status">
                Connected to {connection.source_url}.{" "}
                {connection.warehouses.length} warehouses ·{" "}
                {
                  connection.datasets.filter(
                    (d: Row) => d.raw_count > 0 && d.status === "ready",
                  ).length
                }{" "}
                histories with original order lines.
              </p>
            )}
          </Panel>
          {connection && (
            <Panel
              title="Prepare a simulation"
              subtitle="Opening stock is the quantity actually assigned in the published slotting layout. Stock carries forward across every selected date."
            >
              <div className="form-grid">
                <Field label="Published ASRS warehouse">
                  <select
                    value={warehouse}
                    onChange={(e) => setWarehouse(e.target.value)}
                  >
                    <option value="">Choose a published ASRS warehouse</option>
                    {connection.warehouses.map((w: Row) => (
                      <option
                        key={w.id}
                        value={w.id}
                        disabled={
                          w.system_type !== "Mini-load ASRS" ||
                          !w.published_release_id
                        }
                      >
                        {w.name} · {w.system_type}
                        {w.published_release_id
                          ? ` · v${w.published_version}`
                          : " · publish first"}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Order history">
                  <select
                    value={dataset}
                    onChange={(e) => setDataset(e.target.value)}
                  >
                    <option value="">Choose retained order lines</option>
                    {connection.datasets.map((d: Row) => (
                      <option
                        key={d.id}
                        value={d.id}
                        disabled={!d.raw_count || d.status !== "ready"}
                      >
                        {d.name} · {count(d.raw_count)} retained lines
                        {!d.raw_count ? " · reimport original file" : ""}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field label="Run name">
                  <input
                    value={name}
                    maxLength={120}
                    onChange={(e) => setName(e.target.value)}
                  />
                </Field>
                <Field label="Period mode">
                  <select
                    value={mode}
                    onChange={(e) => {
                      const m = e.target.value as DateMode;
                      setMode(m);
                      if (m === "one_date") setDates(dates.slice(0, 1));
                      if (m === "one_month") setMonths(months.slice(0, 1));
                    }}
                  >
                    {modes.map(([v, label]) => (
                      <option key={v} value={v}>
                        {label}
                      </option>
                    ))}
                  </select>
                </Field>
              </div>
              {!validWarehouse && (
                <p className="asrs-help">
                  Load the ASRS grid in the planning workflow, calculate stock,
                  generate the layout, then register and publish it in Data &
                  inventory.
                </p>
              )}
              {calendar && (
                <>
                  {mode === "one_date" && (
                    <Field label="Observed date">
                      <select
                        value={dates[0] || ""}
                        onChange={(e) => setDates([e.target.value])}
                      >
                        {calendar.dates.map((d: string) => (
                          <option key={d}>{d}</option>
                        ))}
                      </select>
                    </Field>
                  )}
                  {mode === "one_month" && (
                    <Field label="Observed month">
                      <select
                        value={months[0] || ""}
                        onChange={(e) => setMonths([e.target.value])}
                      >
                        {calendar.months.map((d: string) => (
                          <option key={d}>{d}</option>
                        ))}
                      </select>
                    </Field>
                  )}
                  {(mode === "multiple_dates" ||
                    mode === "multiple_months") && (
                    <fieldset className="asrs-date-list">
                      <legend>
                        {mode === "multiple_dates"
                          ? "Observed dates"
                          : "Observed months"}
                      </legend>
                      <div>
                        {(mode === "multiple_dates"
                          ? calendar.dates
                          : calendar.months
                        ).map((d: string) => {
                          const values =
                              mode === "multiple_dates" ? dates : months,
                            setter =
                              mode === "multiple_dates" ? setDates : setMonths;
                          return (
                            <label key={d}>
                              <input
                                type="checkbox"
                                checked={values.includes(d)}
                                onChange={(e) =>
                                  setter(
                                    e.target.checked
                                      ? [...values, d]
                                      : values.filter((v) => v !== d),
                                  )
                                }
                              />
                              {d}
                            </label>
                          );
                        })}
                      </div>
                    </fieldset>
                  )}
                  <p className="asrs-help">
                    {chosen} observed dates selected · {calendar.dates[0]}{" "}
                    through {calendar.dates.at(-1)}. Store/day batches are
                    released at local midnight, matching the original ASRS
                    model; source times remain in the audit log.
                  </p>
                </>
              )}
              <div className="form-grid">
                <Field label="Initial playback speed">
                  <select
                    value={String(settings.speed)}
                    onChange={(e) =>
                      setSettings({
                        ...settings,
                        speed:
                          e.target.value === "max"
                            ? "max"
                            : Number(e.target.value),
                      })
                    }
                  >
                    {[
                      1,
                      2,
                      5,
                      10,
                      25,
                      50,
                      100,
                      250,
                      500,
                      1000,
                      10000,
                      "max",
                    ].map((n) => (
                      <option key={n} value={n}>
                        {n === "max" ? "Max · hardware limit" : `${n}×`}
                      </option>
                    ))}
                  </select>
                </Field>
                <Field
                  label="Replenishment preparation lead time (seconds)"
                  hint="External stock becomes ready at the workstation after this delay; crane travel and handoff time are additional."
                >
                  <input
                    type="number"
                    min="0"
                    step="1"
                    value={settings.replenishment_lead_seconds}
                    onChange={(e) =>
                      setSettings({
                        ...settings,
                        replenishment_lead_seconds: Number(e.target.value),
                      })
                    }
                  />
                </Field>
              </div>
              <label className="asrs-check">
                <input
                  type="checkbox"
                  checked={settings.skip_idle}
                  onChange={(e) =>
                    setSettings({ ...settings, skip_idle: e.target.checked })
                  }
                />
                Skip playback waiting when all cranes are idle; retain the full
                simulated timestamps.
              </label>
              <details className="asrs-assumptions">
                <summary>Motion settings and replenishment rules</summary>
                <p>
                  One fixed-aisle crane per workstation. X and Z move together;
                  forks move only after rail and lift travel stop. Engineering
                  assumptions are editable below.
                </p>
                <div className="form-grid">
                  {settings.axes.map((axis, i) => (
                    <Field
                      key={i}
                      label={`${["X rail", "Y fork", "Z lift"][i]} · speed m/s / acceleration m/s²`}
                    >
                      <span className="asrs-axis">
                        <input
                          aria-label={`${["X", "Y", "Z"][i]} speed`}
                          type="number"
                          min="0.001"
                          step="0.1"
                          value={axis.speed}
                          onChange={(e) =>
                            setSettings({
                              ...settings,
                              axes: settings.axes.map((a, j) =>
                                j === i
                                  ? { ...a, speed: Number(e.target.value) }
                                  : a,
                              ) as AsrsSettings["axes"],
                            })
                          }
                        />
                        <input
                          aria-label={`${["X", "Y", "Z"][i]} acceleration`}
                          type="number"
                          min="0.001"
                          step="0.1"
                          value={axis.acceleration}
                          onChange={(e) =>
                            setSettings({
                              ...settings,
                              axes: settings.axes.map((a, j) =>
                                j === i
                                  ? {
                                      ...a,
                                      acceleration: Number(e.target.value),
                                    }
                                  : a,
                              ) as AsrsSettings["axes"],
                            })
                          }
                        />
                      </span>
                    </Field>
                  ))}
                  {[
                    ["picking_seconds", "Picking / refill handoff seconds"],
                    ["extraction_seconds", "Extraction dwell seconds"],
                    ["replacement_seconds", "Replacement dwell seconds"],
                    ["workstation_height_m", "Workstation height metres"],
                  ].map(([key, label]) => (
                    <Field key={key} label={label}>
                      <input
                        type="number"
                        min="0"
                        step="0.1"
                        value={Number(settings[key as keyof AsrsSettings])}
                        onChange={(e) =>
                          setSettings({
                            ...settings,
                            [key]: Number(e.target.value),
                          })
                        }
                      />
                    </Field>
                  ))}
                </div>
                <p>
                  Replenishment starts when total stock in a SKU’s assigned
                  totes is strictly below its calculated minimum buffer, or a
                  queued order needs an empty tote. Each depleted tote is
                  refilled to its own initial assigned quantity. Requests are
                  coalesced. Refilling retrieves the tote, fills it at its
                  workstation and returns it to the same slot.
                </p>
                <p>
                  External supply is unlimited after the configured lead time.
                  Negative order quantities return to receiving; replenishment
                  uses returned stock first. Excess returns remain in receiving.
                  Unassigned demand is logged as unfulfilled.
                </p>
              </details>
              <button
                className="button button-primary"
                disabled={
                  !!busy ||
                  !validWarehouse ||
                  !calendar?.raw_count ||
                  !chosen ||
                  run?.status === "preparing" ||
                  run?.status === "running"
                }
                onClick={() =>
                  void execute("Preparing", async () => {
                    const value = await api("/api/asrs/runs", {
                      method: "POST",
                      body: JSON.stringify({
                        ...source(),
                        warehouse_id: warehouse,
                        dataset_id: dataset,
                        name,
                        selection: { mode, dates, months },
                        settings,
                      }),
                    });
                    selectRun(value.id);
                    await list();
                  })
                }
              >
                <Activity size={16} aria-hidden="true" />
                Prepare simulation
              </button>
            </Panel>
          )}
        </div>
      </details>
      <Panel
        title="Simulation runs"
        action={
          <button
            className="button button-small"
            disabled={!!busy}
            onClick={() =>
              void execute("Refreshing", async () => {
                await list();
              })
            }
          >
            <RefreshCw size={16} aria-hidden="true" />
            Refresh
          </button>
        }
      >
        <Field label="Saved run">
          <select value={id} onChange={(e) => selectRun(e.target.value)}>
            <option value="">Choose a run</option>
            {history.map((r) => (
              <option key={r.id} value={r.id}>
                {r.config.name} · {r.status.replaceAll("_", " ")} ·{" "}
                {r.created_at.slice(0, 19)}
              </option>
            ))}
          </select>
        </Field>
        {run && (
          <>
            <div className="asrs-run-heading">
              <Badge
                tone={
                  run.status === "running"
                    ? "green"
                    : terminal.has(run.status) && run.status !== "completed"
                      ? "amber"
                      : ""
                }
              >
                {run.status.replaceAll("_", " ")}
              </Badge>
              <span role="status">{run.progress}</span>
            </div>
            {run.status === "preparing" && (
              <p>
                <LoaderCircle className="spin" size={18} aria-hidden="true" />{" "}
                Copying the pinned configuration and selected order lines.
                Playback starts only when you press Start.
              </p>
            )}
            {state && (
              <>
                <p className="asrs-help">
                  {run.config.warehouse_name} · published v
                  {run.config.release_version} · {run.config.dates.length}{" "}
                  observed dates · {run.config.dates[0]} –{" "}
                  {run.config.dates.at(-1)} · {run.config.time_zone}
                </p>
                <div className="asrs-controls">
                  <button
                    className="button button-primary"
                    disabled={
                      !!busy ||
                      !["ready", "paused", "running"].includes(run.status)
                    }
                    onClick={() =>
                      void control(
                        run.status === "running" ? "pause" : "resume",
                      )
                    }
                  >
                    {run.status === "running" ? (
                      <CirclePause size={17} aria-hidden="true" />
                    ) : (
                      <Play size={17} aria-hidden="true" />
                    )}
                    {run.status === "running"
                      ? "Pause"
                      : run.status === "ready"
                        ? "Start"
                        : "Resume"}
                  </button>
                  <button
                    className="button"
                    disabled={!!busy || terminal.has(run.status)}
                    onClick={() => {
                      if (
                        window.confirm(
                          "Stop this run and retain its incomplete stock-flow ledger? Use Pause to continue later.",
                        )
                      )
                        void control("stop");
                    }}
                  >
                    <Square size={15} aria-hidden="true" />
                    Stop
                  </button>
                  <Field label="Playback speed">
                    <select
                      value={String(speed)}
                      disabled={!!busy || terminal.has(run.status)}
                      onChange={(e) =>
                        void control("speed", {
                          speed:
                            e.target.value === "max"
                              ? "max"
                              : Number(e.target.value),
                        })
                      }
                    >
                      {[
                        ...new Set([
                          1,
                          2,
                          5,
                          10,
                          25,
                          50,
                          100,
                          250,
                          500,
                          1000,
                          10000,
                          speed,
                          "max",
                        ]),
                      ].map((n) => (
                        <option key={n} value={n}>
                          {n === "max" ? "Max · hardware limit" : `${n}×`}
                        </option>
                      ))}
                    </select>
                  </Field>
                  <Field label="Custom speed ×">
                    <input
                      type="number"
                      min="1"
                      step="1"
                      placeholder="e.g. 2500"
                      disabled={!!busy || terminal.has(run.status)}
                      onKeyDown={(e) => {
                        if (e.key === "Enter") e.currentTarget.blur();
                      }}
                      onBlur={(e) => {
                        const text = e.target.value,
                          v = Number(text);
                        e.target.value = "";
                        if (text && v >= 1 && v !== speed)
                          void control("speed", { speed: v });
                        else if (text && v < 1)
                          setError("Playback speed must be at least 1×.");
                      }}
                    />
                  </Field>
                  <label className="asrs-check">
                    <input
                      type="checkbox"
                      disabled={!!busy || terminal.has(run.status)}
                      checked={run.config.settings.skip_idle}
                      onChange={(e) =>
                        void control("speed", {
                          speed,
                          skip_idle: e.target.checked,
                        })
                      }
                    />
                    Skip idle waiting
                  </label>
                </div>
                <div className="metrics-grid asrs-metrics">
                  <Metric
                    label="Simulation clock"
                    value={state.simulation_time_local
                      .replace("T", " ")
                      .slice(0, 19)}
                    detail={run.config.time_zone}
                  />
                  <Metric
                    label="Accounted order lines"
                    value={`${count(finishedLines)} / ${count(state.input_line_count)}`}
                    detail={`${count(state.totals.unfulfilled)} unfulfilled · ${count(state.totals.returned)} returned`}
                  />
                  <Metric
                    label="Shipped EA"
                    value={ea(state.totals.outbound)}
                    detail={`${ea(state.totals.inbound)} replenished from external supply`}
                  />
                  <Metric
                    label="On hand EA"
                    value={ea(state.onhand_micro)}
                    detail={`${count(state.totals.replenishments)} replenishments · ${count(state.totals.cycles)} crane cycles`}
                  />
                </div>
                <progress
                  aria-label="Accounted order lines"
                  max={state.input_line_count}
                  value={finishedLines}
                />
                <p className="asrs-help">
                  {Number(state.lines_per_second).toFixed(1)} accounted lines /
                  wall second · {Number(state.wall_seconds).toFixed(1)} wall
                  seconds running. Max processes events as fast as this machine
                  and audit storage allow; the view samples the latest state.
                </p>
              </>
            )}
          </>
        )}
      </Panel>
      {configuration && state && (
        <>
          {configuration.model.warnings.map((w) => (
            <div className="notice warning" key={w}>
              {w}
            </div>
          ))}
          <Panel
            title="ASRS floor and crane view"
            subtitle="The published geometry is fixed for this run. Orange totes are carried; the crane table supplies the same state as the animation."
            action={
              <div
                className="asrs-toggle"
                role="group"
                aria-label="Warehouse view"
              >
                <button
                  className="button button-small"
                  aria-pressed={view === "3d"}
                  onClick={() => setView("3d")}
                >
                  3D
                </button>
                <button
                  className="button button-small"
                  aria-pressed={view === "plan"}
                  onClick={() => setView("plan")}
                >
                  2D plan
                </button>
              </div>
            }
          >
            {view === "3d" ? (
              <Suspense fallback={<p role="status">Loading 3D renderer…</p>}>
                <Layout3D
                  project={configuration.release.warehouse.project}
                  geometry={configuration.release.warehouse.geometry}
                  result={configuration.release.slotting}
                  selected={rack}
                  onSelect={setRack}
                  simulation={live}
                />
              </Suspense>
            ) : (
              <AsrsPlan
                configuration={configuration}
                state={state}
                selected={crane}
              />
            )}
            <Field label="Inspect crane">
              <select value={crane} onChange={(e) => setCrane(e.target.value)}>
                {state.cranes.map((c: Row) => (
                  <option key={c.id} value={c.id}>
                    {c.id} · {c.workstation}
                  </option>
                ))}
              </select>
            </Field>
            {selectedCrane && (
              <AsrsElevation
                model={configuration.model}
                crane={selectedCrane}
              />
            )}
            <Table
              label="ASRS cranes"
              rows={state.cranes}
              columns={[
                { key: "id", label: "Crane" },
                { key: "stage", label: "Stage" },
                { key: "sku", label: "SKU" },
                { key: "load_id", label: "Tote load" },
                { key: "queue", label: "Queued groups" },
                { key: "pending_refills", label: "Refill requests" },
                { key: "cycles", label: "Cycles" },
                {
                  key: "position",
                  label: "X / Y / Z m",
                  render: (v) => v.map((n: number) => n.toFixed(2)).join(" / "),
                },
              ]}
            />
          </Panel>
          <Panel
            title="Stock-flow audit"
            subtitle="Immutable movements link simulated time, source row, order reference, SKU, quantity, tote, crane and origin/destination. Source row is the original file row number."
          >
            <div
              className="management-tabs"
              role="group"
              aria-label="Simulation data view"
            >
              {[
                ["events", "Stock flow"],
                ["orders", "Order lines"],
                ["stock", "Tote balances"],
              ].map(([value, label]) => (
                <button
                  key={value}
                  className={tab === value ? "active" : ""}
                  aria-pressed={tab === value}
                  onClick={() => setTab(value)}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="form-grid">
              <Field label="Exact SKU filter">
                <input
                  value={sku}
                  onChange={(e) => setSku(e.target.value)}
                  placeholder="All SKUs"
                />
              </Field>
              {tab === "events" && (
                <>
                  <Field label="Original source row">
                    <input
                      type="number"
                      min="1"
                      value={sourceRow}
                      onChange={(e) => setSourceRow(e.target.value)}
                      placeholder="All order lines"
                    />
                  </Field>
                  <Field label="Movement kind">
                    <select
                      value={kind}
                      onChange={(e) => setKind(e.target.value)}
                    >
                      <option value="">All movements</option>
                      {eventKinds.map((k) => (
                        <option key={k}>{k}</option>
                      ))}
                    </select>
                  </Field>
                </>
              )}
            </div>
            {tab === "events" && (
              <label className="asrs-check">
                <input
                  type="checkbox"
                  checked={follow}
                  onChange={(e) => setFollow(e.target.checked)}
                />
                Follow latest 200 movements. Clear to browse from the beginning.
              </label>
            )}
            {tab === "stock" ? (
              <>
                <p className="asrs-help">
                  Buffer checks compare the SKU’s total in assigned totes.
                  Receiving stock is shown separately and used first when
                  refilling.
                </p>
                <Table
                  label="SKU stock and buffer targets"
                  rows={skuStock}
                  columns={[
                    { key: "sku", label: "SKU" },
                    {
                      key: "initial",
                      label: "Initial assigned EA",
                      render: (v) => ea(v),
                    },
                    {
                      key: "current",
                      label: "Assigned-tote stock EA",
                      render: (v) => ea(v),
                    },
                    {
                      key: "buffer",
                      label: "Buffer stock EA",
                      render: (v) => ea(v),
                    },
                    {
                      key: "receiving",
                      label: "Receiving EA",
                      render: (v) => ea(v),
                    },
                    {
                      key: "buffer_state",
                      label: "Buffer check",
                      render: (_v, r) => (
                        <Badge tone={r.current < r.buffer ? "amber" : "green"}>
                          {r.current < r.buffer
                            ? "Below buffer"
                            : "At or above buffer"}
                        </Badge>
                      ),
                    },
                  ]}
                />
                <Table
                  label="Simulated tote balances"
                  rows={stock}
                  columns={[
                    { key: "sku", label: "SKU" },
                    { key: "id", label: "Tote load" },
                    {
                      key: "initial_micro",
                      label: "Initial EA",
                      render: (v) => ea(v),
                    },
                    {
                      key: "quantity_micro",
                      label: "Current EA",
                      render: (v) => ea(v),
                    },
                    { key: "location", label: "Current location" },
                    { key: "crane", label: "Crane" },
                  ]}
                />
              </>
            ) : (
              <>
                <div className="asrs-export">
                  <a
                    className="button button-small"
                    href={`/api/asrs/runs/${id}/${tab}.csv?${new URLSearchParams({ sku, ...(tab === "events" ? { source_row: sourceRow, kind } : {}) })}`}
                  >
                    <Download size={16} aria-hidden="true" />
                    Export {tab === "events" ? "stock flow" : "order lines"} CSV
                  </a>
                  <span>
                    Exports every matching record, including pages outside this
                    view.
                  </span>
                </div>
                {tab === "events" ? (
                  <Table
                    label="Simulation stock flow"
                    rows={rows}
                    columns={[
                      { key: "sequence", label: "Seq." },
                      {
                        key: "simulation_time_local",
                        label: "Warehouse local time",
                        render: (v) => v.replace("T", " "),
                      },
                      { key: "kind", label: "Movement" },
                      { key: "sku", label: "SKU" },
                      {
                        key: "quantity_micro",
                        label: "Qty EA",
                        render: (v) => ea(v),
                      },
                      {
                        key: "delta_micro",
                        label: "On-hand change EA",
                        render: (v) => ea(v),
                      },
                      {
                        key: "sku_balance_micro",
                        label: "SKU on hand EA",
                        render: (v) => ea(v),
                      },
                      { key: "source_row", label: "Source row" },
                      { key: "order_reference", label: "Order ref." },
                      { key: "store", label: "Store" },
                      { key: "from_location", label: "From" },
                      { key: "to_location", label: "To" },
                      { key: "crane", label: "Crane" },
                      { key: "cycle_id", label: "Cycle" },
                    ]}
                  />
                ) : (
                  <Table
                    label="Simulation order lines"
                    rows={rows}
                    columns={[
                      { key: "source_row", label: "Source row" },
                      { key: "date", label: "Order date" },
                      { key: "source_time", label: "Source time" },
                      { key: "order_reference", label: "Order ref." },
                      { key: "store", label: "Store" },
                      { key: "sku", label: "SKU" },
                      {
                        key: "quantity_micro",
                        label: "Source qty EA",
                        render: (v) => ea(v),
                      },
                      {
                        key: "fulfilled_micro",
                        label: "Processed EA",
                        render: (v) => ea(v),
                      },
                      { key: "status", label: "Status" },
                      {
                        key: "shortage_micro",
                        label: "Unfulfilled EA",
                        render: (v) => ea(v),
                      },
                    ]}
                  />
                )}
                {!(tab === "events" && follow) && (
                  <div className="asrs-pagination">
                    <button
                      className="button button-small"
                      disabled={!cursors.length}
                      onClick={() => {
                        setCursor(cursors.at(-1)!);
                        setCursors(cursors.slice(0, -1));
                      }}
                    >
                      Previous 200
                    </button>
                    <span>Records after cursor {cursor}</span>
                    <button
                      className="button button-small"
                      disabled={!page || page.done || !page.rows.length}
                      onClick={() => {
                        setCursors([...cursors, cursor]);
                        setCursor(page!.next_after);
                      }}
                    >
                      Next 200
                    </button>
                  </div>
                )}
              </>
            )}
          </Panel>
        </>
      )}
    </div>
  );
}

function AsrsPlan({
  configuration,
  state,
  selected,
}: {
  configuration: { release: Row; model: AsrsModel };
  state: Row;
  selected: string;
}) {
  const p = configuration.release.warehouse.project,
    sx = Number(p.grid.spacing_m),
    sy = Number(p.grid.spacing_y_m ?? sx);
  return (
    <div className="asrs-map">
      <svg
        viewBox={`-3 -2 ${Number(p.grid.width_m) + 6} ${Number(p.grid.length_m) + 4}`}
        role="img"
        aria-label={`ASRS plan. Rack rows and ${state.cranes.length} fixed-aisle cranes; use Inspect crane to select an aisle.`}
      >
        {p.markers
          .filter((m: Row) => m.role === "rack")
          .map((m: Row) => (
            <rect
              key={`r${m.column}_${m.row}`}
              x={m.column * sx - sx * 0.45}
              y={m.row * sy - sy * 0.35}
              width={sx * 0.9}
              height={sy * 0.7}
              className="asrs-plan-rack"
            />
          ))}
        {state.cranes.map((c: Row) => (
          <g key={c.id}>
            <line
              x1={-1}
              x2={c.home[0]}
              y1={c.home[1]}
              y2={c.home[1]}
              className={selected === c.id ? "asrs-rail selected" : "asrs-rail"}
            />
            <circle
              cx={c.position[0]}
              cy={c.position[1]}
              r="0.38"
              className={c.loaded ? "asrs-crane loaded" : "asrs-crane"}
            />
            <text x={c.home[0] + 0.6} y={c.home[1] + 0.18}>
              {c.id.replace("ASRS_", "")}
            </text>
          </g>
        ))}
      </svg>
      <p className="asrs-help">
        Green: crane without tote · orange: crane carrying tote · blue rail:
        selected aisle. Select an aisle using Inspect crane.
      </p>
    </div>
  );
}
function AsrsElevation({ model, crane }: { model: AsrsModel; crane: Row }) {
  const loads = model.loads.filter((l) => l.crane === crane.id),
    maxX = Math.max(crane.home[0], ...loads.map((l) => l.position[0])) + 2,
    minX = Math.min(-2, ...loads.map((l) => l.position[0] - 1)),
    height = Math.max(4, ...loads.map((l) => l.position[2] + 1));
  return (
    <div className="asrs-elevation">
      <svg
        viewBox={`${minX} -0.5 ${maxX - minX} ${height + 1}`}
        role="img"
        aria-label={`${crane.id} elevation: X ${crane.position[0].toFixed(2)} metres, Z ${crane.position[2].toFixed(2)} metres; ${crane.stage}`}
      >
        <g transform={`translate(0 ${height}) scale(1 -1)`}>
          {loads.map((l) => (
            <rect
              key={l.id}
              x={l.position[0] - 0.22}
              y={l.position[2] - 0.19}
              width=".44"
              height=".38"
              className="asrs-plan-rack"
            />
          ))}
          <line
            x1={crane.position[0]}
            x2={crane.position[0]}
            y1="0"
            y2={height}
            className="asrs-rail selected"
          />
          <rect
            x={crane.position[0] - 0.38}
            y={crane.position[2] - 0.13}
            width=".76"
            height=".26"
            className={crane.loaded ? "asrs-crane loaded" : "asrs-crane"}
          />
        </g>
      </svg>
      <p className="asrs-help">
        {crane.id} · {crane.stage.replaceAll("_", " ")} · X{" "}
        {crane.position[0].toFixed(2)} m / fork Y{" "}
        {(crane.position[1] - crane.home[1]).toFixed(2)} m / lift Z{" "}
        {crane.position[2].toFixed(2)} m{crane.sku ? ` · SKU ${crane.sku}` : ""}
        . Both rack faces are projected into this elevation.
      </p>
    </div>
  );
}

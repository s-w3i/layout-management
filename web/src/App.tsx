import { useEffect, useMemo, useRef, useState } from "react";
import {
  ArrowRight,
  ArrowLeft,
  Check,
  ChevronDown,
  Download,
  FolderOpen,
  Grid3X3,
  ChartNoAxesCombined,
  Boxes,
  SlidersHorizontal,
  CircleCheck,
  Plus,
  CircleHelp,
  LoaderCircle,
  X,
  Save,
  TriangleAlert,
  CheckCheck,
  FileJson,
  Workflow,
  ShieldCheck,
  CircleDashed,
  RefreshCw,
  MapPinned,
  PackageCheck,
} from "lucide-react";
import {
  defaultSettings,
  mergeRows,
  csvString,
  type Row,
  type Settings,
} from "../../shared/model";
import { api, engine, upload, download } from "./api";
import { augmentCatalog, newProject, syncBuffers } from "./warehouse";
import { WarehouseStep } from "./WarehouseStep";
import LayoutViewer from "./LayoutViewer";
import InventoryOperations from "./InventoryOperations";
import { dateRangeError } from "../../shared/dates";
import {
  demandFilters,
  inventoryFilters,
  stockFilters,
  stockTableRows,
} from "../../shared/table";
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

interface Workspace {
  name: string;
  project: Row;
  velocity: Row[];
  stock: Row[];
  attributes: Record<string, Row>;
  orders: Row | null;
  analysis: Row | null;
  stockResult: Row | null;
  settings: Settings;
  result: Row | null;
  reference: Row | null;
  stockStale: boolean;
  demandStale: boolean;
  example: boolean;
  step: number;
}
const blank = (): Workspace => ({
  name: "Untitled warehouse",
  project: newProject(),
  velocity: [],
  stock: [],
  attributes: {},
  orders: null,
  analysis: null,
  stockResult: null,
  settings: { ...defaultSettings, store_workstation_mapping: {} },
  result: null,
  reference: null,
  stockStale: false,
  demandStale: false,
  example: false,
  step: 0,
});
const steps = [
  { name: "Warehouse", detail: "Space, racks & zones", icon: Grid3X3 },
  {
    name: "Demand",
    detail: "Orders & ABC analysis",
    icon: ChartNoAxesCombined,
  },
  {
    name: "Stock requirements",
    detail: "Quantities & physical fit",
    icon: Boxes,
  },
  {
    name: "Generate layout",
    detail: "Strategy & constraints",
    icon: SlidersHorizontal,
  },
  {
    name: "Review & export",
    detail: "Validation & comparison",
    icon: CircleCheck,
  },
];
const headings = [
  [
    "Set up your warehouse.",
    "Create the space, then define where and how inventory can be stored.",
  ],
  [
    "Understand your demand.",
    "Turn order history into reliable SKU priorities and co-order relationships.",
  ],
  [
    "Give every SKU enough space.",
    "Calculate stock coverage and physical storage needs before allocation.",
  ],
  [
    "Choose how your layout works.",
    "Review the inputs and select the placement strategy for this warehouse.",
  ],
  [
    "A layout you can inspect.",
    "Review inventory coverage, check storage rules and compare the result.",
  ],
];
function exampleWorkspace(data: Row): Workspace {
  const w = blank();
  return {
    ...w,
    name: data.project.grid.map_name || "Existing warehouse",
    project: data.project,
    velocity: data.velocity,
    stock: data.stock,
    attributes: data.attributes,
    orders: data.orders ?? null,
    example: true,
    settings: {
      ...w.settings,
      minimum_stock_days: Number(data.stock[0]?.minimum_stock_days ?? 2),
      buffer_stock_days: Number(data.stock[0]?.buffer_stock_days ?? 1),
      maximum_same_sku_slots_per_rack:
        data.project.storage_layout.levels_per_rack *
        data.project.storage_layout.slots_per_level,
      store_workstation_mapping: data.mapping ?? {},
    },
  };
}

export default function App() {
  const [workspace, setWorkspace] = useState<Workspace>(blank),
    [ready, setReady] = useState(false),
    [geometry, setGeometry] = useState<Row | null>(null),
    [geometryError, setGeometryError] = useState("");
  const [area, setArea] = useState("planning");
  const [operationsOpened, setOperationsOpened] = useState(false);
  const [selected, setSelected] = useState(""),
    [busy, setBusy] = useState<{
      label: string;
      text: string;
      id: string;
    } | null>(null),
    [error, setError] = useState(""),
    [saveStatus, setSaveStatus] = useState("Saved locally"),
    [help, setHelp] = useState(false),
    [lastSaved, setLastSaved] = useState("");
  const [mappingQuery, setMappingQuery] = useState(""),
    [referenceComparison, setReferenceComparison] = useState<Row | null>(null),
    [workspaceId, setWorkspaceId] = useState("");
  const errorRef = useRef<HTMLDivElement>(null),
    titleRef = useRef<HTMLHeadingElement>(null),
    helpRef = useRef<HTMLDialogElement>(null);
  const saveVersion = useRef(0);
  const skipInitialSave = useRef(false);
  const w = workspace,
    step = w.step;
  useEffect(() => {
    let cancelled = false;
    (async () => {
      let id = localStorage.getItem("layout-generator-workspace");
      let saved: Row | null = null;
      if (id)
        try {
          saved = await api(`/api/workspaces/${id}`);
        } catch {}
      if (!id || !saved) {
        const created = await api("/api/workspaces", { method: "POST" });
        id = created.id;
        localStorage.setItem("layout-generator-workspace", id!);
      }
      if (cancelled) return;
      setWorkspaceId(id!);
      saveVersion.current = Number(saved?._save_revision ?? 0);
      skipInitialSave.current = Boolean(saved?.project);
      if (saved?.project)
        setWorkspace({
          ...blank(),
          ...saved,
          settings: { ...defaultSettings, ...saved.settings },
        } as Workspace);
      else setWorkspace(exampleWorkspace(await api("/api/examples/amr")));
      setReady(true);
    })().catch((e) => {
      setError(e.message);
      setReady(true);
    });
    return () => {
      cancelled = true;
    };
  }, []);
  useEffect(() => {
    if (!ready || !workspaceId) return;
    if (skipInitialSave.current) {
      skipInitialSave.current = false;
      return;
    }
    setSaveStatus("Saving…");
    const timer = setTimeout(() => {
      const revision = ++saveVersion.current;
      api(`/api/workspaces/${workspaceId}`, {
        method: "PUT",
        body: JSON.stringify({ ...w, _save_revision: revision }),
      })
        .then(() => {
          if (revision !== saveVersion.current) return;
          setSaveStatus("Saved locally");
          setLastSaved(
            new Date().toLocaleTimeString([], {
              hour: "2-digit",
              minute: "2-digit",
            }),
          );
        })
        .catch((e) => {
          if (revision !== saveVersion.current) return;
          setSaveStatus("Save failed");
          setError(`The workspace could not be saved: ${e.message}`);
        });
    }, 700);
    return () => clearTimeout(timer);
  }, [w, workspaceId, ready]);
  useEffect(() => {
    if (!ready) return;
    let jobId = "",
      cancelled = false;
    const abort = new AbortController();
    const timer = setTimeout(() => {
      engine(
        { command: "geometry", project: w.project },
        (_, id) => {
          jobId = id;
        },
        abort.signal,
      )
        .then((result) => {
          if (!cancelled) {
            setGeometry(result);
            setGeometryError("");
          }
        })
        .catch((e) => {
          if (!cancelled && e.name !== "AbortError")
            setGeometryError(e.message);
        });
    }, 180);
    return () => {
      cancelled = true;
      clearTimeout(timer);
      abort.abort();
      if (jobId)
        void api(`/api/jobs/${jobId}`, { method: "DELETE" }).catch(() => {});
    };
  }, [w.project, ready]);
  useEffect(() => {
    if (error) errorRef.current?.focus();
  }, [error]);
  useEffect(() => {
    if (help) helpRef.current?.showModal();
    else helpRef.current?.close();
  }, [help]);
  const merge = useMemo(() => {
    try {
      return { rows: mergeRows(w.velocity, w.stock, w.attributes), error: "" };
    } catch (e) {
      return { rows: [], error: (e as Error).message };
    }
  }, [w.velocity, w.stock, w.attributes]);
  const rows = merge.rows;
  const demandDateError = w.orders
    ? dateRangeError(w.settings, {
        date_start: w.orders.date_start,
        date_end: w.orders.date_end,
      })
    : "";
  const stockRows = useMemo(
    () => stockTableRows(w.stock, w.velocity),
    [w.stock, w.velocity],
  );
  const stockValid =
    rows.length > 0 &&
    !w.stockStale &&
    w.stock.length > 0 &&
    rows.every(
      (r) =>
        r.required_slots !== undefined &&
        r.required_slots !== null &&
        r.required_slots !== "" &&
        r.total_required_ea !== undefined &&
        r.total_slotted_ea !== undefined &&
        Number.isFinite(Number(r.required_slots)) &&
        Number(r.required_slots) >= 0 &&
        Number(r.total_slotted_ea) >= Number(r.total_required_ea),
    );
  const warehouseValid = Boolean(
    geometry?.summary.rack_count &&
      geometry?.summary.workstation_count &&
      !geometryError,
  );
  const activeStores: string[] = w.analysis?.stores ?? w.orders?.stores ?? [];
  const mappingValid =
    w.settings.strategy !== "abc_affinity" ||
    geometry?.summary.workstation_count === 1 ||
    Boolean(
      activeStores.length &&
        activeStores.every(
          (store: string) =>
            Object.hasOwn(w.settings.store_workstation_mapping, store) &&
            Object.hasOwn(
              geometry?.stations ?? {},
              w.settings.store_workstation_mapping[store],
            ),
        ),
    );
  const prerequisites = [
    warehouseValid,
    w.velocity.length > 0 && !w.demandStale,
    stockValid,
    Boolean(w.result),
    Boolean(w.result),
  ];
  const canGenerate =
    warehouseValid &&
    w.velocity.length > 0 &&
    !w.demandStale &&
    !demandDateError &&
    stockValid &&
    !merge.error &&
    mappingValid &&
    (w.settings.strategy !== "abc_affinity" || Boolean(w.orders));
  const estimatedLoads = rows.reduce(
    (sum, r) =>
      sum +
      (Number(r.total_slotted_ea) > 0
        ? Math.ceil(
            Number(r.required_slots ?? 0) /
              Math.max(1, Number(r.slots_per_unit || 1)),
          )
        : 0),
    0,
  );
  const patch = (values: Partial<Workspace>) =>
    setWorkspace((current) => ({ ...current, ...values }));
  const go = (next: number) => {
    setArea("planning");
    patch({ step: Math.max(0, Math.min(4, next)) });
    setError("");
    setTimeout(() => titleRef.current?.focus(), 0);
  };
  const changeSettings = (values: Partial<Settings>) =>
    setWorkspace((current) => {
      const demand = Object.keys(values).some((k) =>
          ["start_date", "end_date", "a_limit", "b_limit"].includes(k),
        ),
        stock =
          demand ||
          Object.keys(values).some((k) =>
            ["minimum_stock_days", "buffer_stock_days"].includes(k),
          );
      return {
        ...current,
        settings: { ...current.settings, ...values },
        result: null,
        demandStale: current.demandStale || demand,
        stockStale: current.stockStale || stock,
      };
    });
  const changeProject = (project: Row) =>
    setWorkspace((current) => {
      const physical =
        JSON.stringify([
          project.warehouse_storage_defaults,
          project.machine_carrying_capacity,
          project.storage_layout?.levels_per_rack,
          project.storage_layout?.slots_per_level,
        ]) !==
        JSON.stringify([
          current.project.warehouse_storage_defaults,
          current.project.machine_carrying_capacity,
          current.project.storage_layout?.levels_per_rack,
          current.project.storage_layout?.slots_per_level,
        ]);
      return {
        ...current,
        project,
        name: project.grid.map_name || current.name,
        result: null,
        stockStale: current.stockStale || physical,
      };
    });
  const run = async (
    label: string,
    action: (progress: (text: string, id: string) => void) => Promise<void>,
  ) => {
    setBusy({ label, text: "Starting…", id: "" });
    setError("");
    try {
      await action((text, id) => setBusy({ label, text, id }));
    } catch (e) {
      if ((e as Error).name !== "AbortError") setError((e as Error).message);
    } finally {
      setBusy(null);
    }
  };
  const importFile = (file: File, kind: string) =>
    void run(`Import ${kind}`, async (progress) => {
      const result = await upload(file, kind, progress);
      if (kind === "grid") {
        const project = result.data.storage_layout
          ? result.data
          : syncBuffers({
              ...result.data,
              storage_layout: newProject().storage_layout,
              machine_carrying_capacity:
                result.data.machine_carrying_capacity ??
                newProject().machine_carrying_capacity,
            });
        changeProject(project);
        setSelected("");
      }
      if (kind === "orders")
        patch({
          orders: result,
          velocity: [],
          stock: [],
          analysis: null,
          stockResult: null,
          result: null,
          demandStale: false,
          stockStale: false,
          settings: {
            ...w.settings,
            start_date: "",
            end_date: "",
            store_workstation_mapping: {},
          },
        });
      if (kind === "velocity")
        patch({
          velocity: result.rows,
          analysis: null,
          result: null,
          demandStale: false,
          stockStale: true,
        });
      if (kind === "stock")
        patch({
          stock: result.rows,
          stockResult: null,
          result: null,
          stockStale: false,
        });
      if (kind === "attributes")
        patch({
          attributes: result.attributes,
          project: augmentCatalog(w.project, result.attributes),
          stockStale: true,
          result: null,
        });
      if (kind === "mapping") {
        let mapping = result.data;
        mapping =
          mapping.store_workstation_mapping ??
          mapping.affinity_configuration?.store_workstation_mapping ??
          mapping.summary?.affinity_tuning?.store_workstation_mapping ??
          mapping;
        if (
          !mapping ||
          typeof mapping !== "object" ||
          Array.isArray(mapping) ||
          Object.values(mapping).some((v) => typeof v !== "string")
        )
          throw new Error(
            "Mapping must be an object of store IDs to workstation IDs",
          );
        changeSettings({ store_workstation_mapping: mapping });
      }
      if (kind === "reference") {
        patch({ reference: result });
        if (w.result)
          setReferenceComparison(
            await api("/api/compare", {
              method: "POST",
              body: JSON.stringify({
                layout: w.result,
                reference_id: result.id,
              }),
            }),
          );
      }
      if (kind === "session") {
        setOperationsOpened(false);
        setArea("planning");
        patch({
          ...blank(),
          ...result.data,
          settings: { ...defaultSettings, ...result.data.settings },
          step: 0,
        });
        setReferenceComparison(null);
      }
    });
  const analyze = () =>
    void run("Analyze demand", async (progress) => {
      const result = await engine(
        { command: "analyze", orders_id: w.orders!.id, settings: w.settings },
        progress,
      );
      patch({
        analysis: result,
        velocity: result.rows,
        stock: [],
        stockResult: null,
        result: null,
        demandStale: false,
        stockStale: false,
      });
    });
  const calculate = () =>
    void run("Calculate stock", async (progress) => {
      const result = await engine(
        {
          command: "stock",
          orders_id: w.orders!.id,
          project: w.project,
          attributes: w.attributes,
          settings: w.settings,
        },
        progress,
      );
      patch({
        stock: result.rows,
        stockResult: result,
        stockStale: false,
        result: null,
      });
    });
  const generate = () =>
    void run("Generate layout", async (progress) => {
      patch({ step: 4 });
      const result = await engine(
        {
          command: "generate",
          project: w.project,
          sku_rows: rows,
          orders_id:
            w.settings.strategy === "abc_affinity" ? w.orders?.id : undefined,
          settings: w.settings,
          reference_id: w.reference?.id,
        },
        progress,
      );
      patch({ result });
      setReferenceComparison(result.comparison ?? null);
    });
  const openExample = (kind: string) =>
    void run("Open warehouse example", async () => {
      patch(exampleWorkspace(await api(`/api/examples/${kind}`)));
      setSelected("");
      setReferenceComparison(null);
    });
  const exportWorkspace = () =>
    void run("Export workspace", async () => {
      download(
        `${w.name.replace(/[^a-z0-9_-]/gi, "_")}.layout-project.json`,
        JSON.stringify(
          await api("/api/export-workspace", {
            method: "POST",
            body: JSON.stringify(w),
          }),
        ),
      );
    });
  const inspection =
    w.result?.assignments?.filter((r: Row) => r.rack_id === selected) ?? [];
  const classes =
    w.analysis?.summary.classes ??
    w.velocity.reduce(
      (acc: Row, r: Row) => ({
        ...acc,
        [r.velocity_class]: (acc[r.velocity_class] ?? 0) + 1,
      }),
      {},
    );
  const stockTotal = w.stock.reduce(
      (sum, r) => sum + Number(r.total_slotted_ea ?? 0),
      0,
    ),
    stockSlots = w.stock.reduce(
      (sum, r) => sum + Number(r.required_slots ?? 0),
      0,
    );
  const comparison = referenceComparison ?? w.result?.comparison;

  if (!ready)
    return (
      <div className="boot">
        <div className="brand-mark">
          <Boxes size={23} />
        </div>
        <LoaderCircle className="spin" />
        <p>Opening your warehouse workspace…</p>
      </div>
    );
  return (
    <div className="app-shell">
      <a className="skip-link" href="#main-content">
        Skip to workspace
      </a>
      <aside className="sidebar">
        <a
          className="brand"
          href="#"
          onClick={(e) => {
            e.preventDefault();
            go(0);
          }}
        >
          <span className="brand-mark">
            <Boxes size={23} />
          </span>
          <span>
            layout<span className="brand-light">generator</span>
          </span>
        </a>
        <div className="sidebar-label">YOUR WORKSPACE</div>
        <button className="workspace-switch" onClick={() => setHelp(true)}>
          <span className="warehouse-icon">
            <Grid3X3 size={19} />
          </span>
          <span>
            <strong>{w.name}</strong>
            <small>
              {w.example ? "Existing warehouse example" : "Warehouse project"}
            </small>
          </span>
          <ChevronDown size={14} />
        </button>
        <div className="sidebar-label workflow-label">GENERATION WORKFLOW</div>
        <nav aria-label="Layout generation steps">
          {steps.map((s, i) => (
            <button
              key={s.name}
              aria-current={
                area === "planning" && step === i ? "step" : undefined
              }
              className={`step-link ${area === "planning" && step === i ? "current" : ""}`}
              aria-label={`Step ${i + 1}: ${s.name}`}
              onClick={() => go(i)}
            >
              <span className={`step-number ${prerequisites[i] ? "done" : ""}`}>
                {prerequisites[i] ? (
                  <Check size={14} />
                ) : (
                  String(i + 1).padStart(2, "0")
                )}
              </span>
              <span>
                <strong>{s.name}</strong>
                <small>{s.detail}</small>
              </span>
              {area === "planning" && step === i && (
                <span className="active-dot" />
              )}
            </button>
          ))}
        </nav>
        <div className="sidebar-label operations-label">AFTER GENERATION</div>
        <nav className="operations-nav" aria-label="Inventory tools">
          <button
            type="button"
            aria-label="Inventory operations"
            title="Inventory operations"
            className={`step-link ${area === "operations" ? "current" : ""}`}
            aria-pressed={area === "operations"}
            onClick={() => {
              setArea("operations");
              setOperationsOpened(true);
              setError("");
              setTimeout(() => titleRef.current?.focus(), 0);
            }}
          >
            <span className="step-number">
              <PackageCheck size={17} aria-hidden="true" />
            </span>
            <span>
              <strong>Inventory operations</strong>
              <small>Locate, rank & trial swaps</small>
            </span>
          </button>
        </nav>
        <div className="sidebar-guidance">
          <div className="guidance-icon">
            <Workflow size={19} />
          </div>
          <h3>Good inputs. Better layouts.</h3>
          <p>
            Follow the sequence to keep demand, capacity and placement rules in
            sync.
          </p>
          <button onClick={() => setHelp(true)}>
            How it works <ArrowRight size={13} />
          </button>
        </div>
        <div className="sidebar-bottom">
          <span className="status-dot" />
          <span>{saveStatus}</span>
          {lastSaved && <small>{lastSaved}</small>}
        </div>
      </aside>
      <div className="main-shell">
        <header className="topbar">
          <div className="breadcrumb">
            <span>Workspace</span>
            <span>/</span>
            <strong>
              {area === "operations"
                ? "Inventory operations"
                : steps[step].name}
            </strong>
          </div>
          <div className="top-actions">
            <button
              className="button button-small"
              aria-label="New workspace"
              disabled={!!busy}
              onClick={() => {
                if (
                  window.confirm(
                    "Start a new warehouse workspace? Export the current workspace first if you want a separate copy.",
                  )
                ) {
                  patch(blank());
                  setOperationsOpened(false);
                  setArea("planning");
                  setGeometry(null);
                  setSelected("");
                }
              }}
            >
              <Plus size={15} />
              <span>New workspace</span>
            </button>
            <UploadButton
              label="Open"
              accept=".json"
              small
              disabled={!!busy}
              onFile={(f) => importFile(f, "session")}
            />
            <button
              className="button button-small"
              aria-label="Save workspace copy"
              disabled={!!busy}
              onClick={exportWorkspace}
            >
              <Save size={15} />
              <span>Save copy</span>
            </button>
            <button
              className="icon-button"
              aria-label="Workflow help"
              onClick={() => setHelp(true)}
            >
              <CircleHelp size={19} />
            </button>
          </div>
        </header>
        <main id="main-content" className="main-content">
          <div className="page-intro">
            <div>
              <div className="eyebrow">
                {area === "operations" ? (
                  "INVENTORY OPERATIONS"
                ) : (
                  <>
                    STEP {String(step + 1).padStart(2, "0")} <span>/ 05</span>
                  </>
                )}
              </div>
              <h1 ref={titleRef} tabIndex={-1}>
                {area === "operations"
                  ? "Inspect inventory. Test position changes."
                  : headings[step][0]}
              </h1>
              <p>
                {area === "operations"
                  ? "Locate stock, estimate handling-unit movements and try validated swaps in a separate scenario."
                  : headings[step][1]}
              </p>
            </div>
            <div className="intro-right">
              <Badge tone={w.result ? "green" : ""}>
                {w.result ? (
                  <>
                    <CircleCheck size={12} />
                    Layout generated
                  </>
                ) : (
                  <>
                    <CircleDashed size={12} />
                    Configuration
                  </>
                )}
              </Badge>
              <span>{w.name}</span>
            </div>
          </div>
          {area === "planning" && w.example && step === 0 && (
            <div className="example-banner">
              <FolderOpen size={17} />
              <span>
                <strong>Your existing warehouse, ready to explore.</strong>{" "}
                Using the copied{" "}
                {w.project.storage_layout.system_type === "AMR"
                  ? "AMR"
                  : "ASRS"}{" "}
                grid, SKU attributes and saved stock inputs.
              </span>
              <select
                aria-label="Choose existing example"
                value={
                  w.project.storage_layout.system_type === "AMR"
                    ? "amr"
                    : "asrs"
                }
                disabled={!!busy}
                onChange={(e) => openExample(e.target.value)}
              >
                <option value="amr">AMR warehouse</option>
                <option value="asrs">ASRS warehouse</option>
              </select>
            </div>
          )}
          {(error || merge.error || geometryError) && (
            <div
              role="alert"
              tabIndex={-1}
              ref={errorRef}
              className="notice error"
            >
              <TriangleAlert size={18} />
              <div>
                <strong>There is a problem to resolve</strong>
                <p>{error || merge.error || geometryError}</p>
              </div>
              {error && (
                <button
                  className="icon-button"
                  aria-label="Dismiss error"
                  onClick={() => setError("")}
                >
                  <X size={17} />
                </button>
              )}
            </div>
          )}
          {busy && (
            <div className="notice progress" role="status" aria-live="polite">
              <LoaderCircle className="spin" size={19} />
              <div>
                <strong>{busy.label}</strong>
                <p>{busy.text}</p>
              </div>
              <button
                className="button button-small"
                disabled={!busy.id}
                onClick={() => {
                  if (busy.id)
                    void api(`/api/jobs/${busy.id}`, { method: "DELETE" });
                }}
              >
                Cancel
              </button>
            </div>
          )}
          <fieldset className="workspace-fieldset" disabled={!!busy}>
            {area === "planning" && (
              <>
                {step === 0 && (
                  <WarehouseStep
                    project={w.project}
                    geometry={geometry}
                    onChange={changeProject}
                    onImport={importFile}
                    selected={selected}
                    onSelect={setSelected}
                    disabled={!!busy}
                    onError={setError}
                  />
                )}
                {step === 1 && (
                  <div className="step-columns">
                    <div className="primary-column">
                      <Panel
                        title="Order history"
                        subtitle="One order line counts as one pick. Affinity groups distinct SKUs by store and day."
                        action={
                          <UploadButton
                            label={
                              w.orders ? "Replace orders" : "Import orders"
                            }
                            accept=".xlsx,.csv"
                            onFile={(f) => importFile(f, "orders")}
                          />
                        }
                      >
                        {w.orders ? (
                          <>
                            <div className="source-file">
                              <div className="source-icon">
                                <FileJson size={22} />
                              </div>
                              <div>
                                <strong>{w.orders.name}</strong>
                                <p>
                                  {count(w.orders.line_count)} valid order lines
                                  · {w.orders.date_start} → {w.orders.date_end}
                                </p>
                              </div>
                              <Badge tone="green">Ready</Badge>
                            </div>
                            <div className="metrics-grid three">
                              <Metric
                                label="Unique SKUs"
                                value={count(w.orders.sku_count)}
                              />
                              <Metric
                                label="Store / day groups"
                                value={count(w.orders.store_day_count)}
                              />
                              <Metric
                                label="Stores"
                                value={count(w.orders.store_count)}
                              />
                            </div>
                            <div className="form-grid">
                              <Field
                                label="From date"
                                hint={`Earliest available: ${w.orders.date_start}. Inclusive.`}
                              >
                                <input
                                  type="date"
                                  aria-label="From date"
                                  min={w.orders.date_start}
                                  max={w.orders.date_end}
                                  aria-invalid={!!demandDateError}
                                  aria-describedby={
                                    demandDateError
                                      ? "demand-date-error"
                                      : undefined
                                  }
                                  value={
                                    w.settings.start_date || w.orders.date_start
                                  }
                                  onChange={(e) =>
                                    changeSettings({
                                      start_date: e.target.value,
                                    })
                                  }
                                />
                              </Field>
                              <Field
                                label="Through date"
                                hint={`Latest available: ${w.orders.date_end}. Inclusive.`}
                              >
                                <input
                                  type="date"
                                  aria-label="Through date"
                                  min={w.orders.date_start}
                                  max={w.orders.date_end}
                                  aria-invalid={!!demandDateError}
                                  aria-describedby={
                                    demandDateError
                                      ? "demand-date-error"
                                      : undefined
                                  }
                                  value={
                                    w.settings.end_date || w.orders.date_end
                                  }
                                  onChange={(e) =>
                                    changeSettings({ end_date: e.target.value })
                                  }
                                />
                              </Field>
                            </div>
                            {demandDateError && (
                              <p
                                id="demand-date-error"
                                className="inline-warning"
                                role="alert"
                              >
                                {demandDateError}
                              </p>
                            )}
                          </>
                        ) : (
                          <Empty
                            title="Add your order history"
                            detail="Import an XLSX workbook or CSV with Date, Item or SKU, Quantity (in EA), and Store ID for affinity."
                          />
                        )}
                        <div className="config-divider" />
                        <div className="section-row">
                          <div>
                            <h3>ABC classification</h3>
                            <p className="muted">
                              Cumulative share of pick frequency, sorted by SKU
                              demand.
                            </p>
                          </div>
                          <UploadButton
                            label="Import velocity CSV"
                            accept=".csv"
                            onFile={(f) => importFile(f, "velocity")}
                            small
                          />
                        </div>
                        <div className="form-grid columns-3">
                          <Field label="Class A ends at (%)">
                            <input
                              type="number"
                              min="1"
                              max="99"
                              value={Number(
                                (w.settings.a_limit * 100).toFixed(2),
                              )}
                              onChange={(e) =>
                                changeSettings({
                                  a_limit: Number(e.target.value) / 100,
                                })
                              }
                            />
                          </Field>
                          <Field label="Class B ends at (%)">
                            <input
                              type="number"
                              min="2"
                              max="100"
                              value={Number(
                                (w.settings.b_limit * 100).toFixed(2),
                              )}
                              onChange={(e) =>
                                changeSettings({
                                  b_limit: Number(e.target.value) / 100,
                                })
                              }
                            />
                          </Field>
                          <button
                            className="button primary align-end"
                            disabled={!w.orders || !!demandDateError}
                            onClick={analyze}
                          >
                            <ChartNoAxesCombined size={16} />
                            Analyze demand
                          </button>
                        </div>
                        {w.demandStale && (
                          <div className="inline-warning">
                            <RefreshCw size={15} />
                            Demand parameters changed. Analyze again before
                            generation.
                          </div>
                        )}
                        {w.velocity.length > 0 && (
                          <>
                            <div className="class-summary">
                              {["A", "B", "C"].map((cl) => (
                                <div key={cl}>
                                  <Badge tone={`class-${cl}`}>{cl}</Badge>
                                  <strong>{count(classes[cl])}</strong>
                                  <span>SKUs</span>
                                </div>
                              ))}
                            </div>
                            <Table
                              rows={w.velocity}
                              searchable
                              label="SKU demand"
                              filters={demandFilters}
                              sortable
                              empty="No demand records match your search and filters."
                              columns={[
                                { key: "sku", label: "SKU" },
                                {
                                  key: "velocity_class",
                                  label: "Class",
                                  render: (v) => (
                                    <Badge tone={`class-${v}`}>{v}</Badge>
                                  ),
                                },
                                { key: "pick_frequency", label: "Pick lines" },
                                {
                                  key: "total_quantity_ea",
                                  label: "Quantity (EA)",
                                },
                                { key: "active_days", label: "Active days" },
                              ]}
                            />
                            <button
                              className="text-button"
                              onClick={() =>
                                download(
                                  "sku_velocity_summary.csv",
                                  csvString(
                                    w.velocity.map(
                                      ({ sku_requirements, ...row }) => row,
                                    ),
                                  ),
                                  "text/csv",
                                )
                              }
                            >
                              <Download size={13} />
                              Export velocity CSV
                            </button>
                          </>
                        )}
                      </Panel>
                      {w.analysis && (
                        <Panel
                          title="Strong co-order relationships"
                          subtitle="Cosine similarity of binary SKU presence within each store/day group."
                        >
                          <Table
                            rows={w.analysis.relationships}
                            label="Affinity relationships"
                            searchable
                            sortable
                            columns={[
                              { key: "sku_a", label: "SKU" },
                              { key: "sku_b", label: "Co-ordered SKU" },
                              {
                                key: "shared_store_days",
                                label: "Shared store/days",
                              },
                              {
                                key: "cosine",
                                label: "Similarity",
                                render: (v) => (
                                  <div className="similarity">
                                    <span
                                      style={{ width: `${Number(v) * 100}%` }}
                                    />
                                    <strong>{Number(v).toFixed(3)}</strong>
                                  </div>
                                ),
                              },
                            ]}
                          />
                        </Panel>
                      )}
                    </div>
                    <aside className="secondary-column">
                      <Panel title="The demand model">
                        <div className="numbered-note">
                          <span>01</span>
                          <div>
                            <strong>Frequency sets priority</strong>
                            <p>
                              ABC uses order-line frequency, rather than
                              multiplying picks by quantity.
                            </p>
                          </div>
                        </div>
                        <div className="numbered-note">
                          <span>02</span>
                          <div>
                            <strong>Quantities set capacity</strong>
                            <p>
                              Stock coverage uses quantity demand over the
                              inclusive calendar observation span.
                            </p>
                          </div>
                        </div>
                        <div className="numbered-note">
                          <span>03</span>
                          <div>
                            <strong>Co-orders inform affinity</strong>
                            <p>
                              Repeated lines for one SKU count once in each
                              store/day relationship.
                            </p>
                          </div>
                        </div>
                      </Panel>
                      <div className="subtle-card">
                        <ShieldCheck size={20} />
                        <h3>Your files stay local.</h3>
                        <p>
                          The app reads data on this computer and stores
                          workspace files inside this project.
                        </p>
                      </div>
                      {w.orders?.ignored_rows > 0 && (
                        <div className="notice warning">
                          <TriangleAlert size={18} />
                          <p>
                            {count(w.orders!.ignored_rows)} rows with missing
                            SKU or invalid dates were excluded.
                          </p>
                        </div>
                      )}
                    </aside>
                  </div>
                )}
                {step === 2 && (
                  <div className="step-columns">
                    <div className="primary-column">
                      <Panel
                        title="Stock coverage"
                        subtitle="Calculate the stock that needs a physical home in this layout."
                        action={
                          <UploadButton
                            small
                            label="Import stock CSV"
                            accept=".csv"
                            onFile={(f) => importFile(f, "stock")}
                          />
                        }
                      >
                        <div className="form-grid columns-3">
                          <Field
                            label="Minimum stock (days)"
                            hint="At least one day of demand."
                          >
                            <input
                              type="number"
                              min="1"
                              step="1"
                              value={w.settings.minimum_stock_days}
                              onChange={(e) =>
                                changeSettings({
                                  minimum_stock_days: Number(e.target.value),
                                })
                              }
                            />
                          </Field>
                          <Field
                            label="Buffer stock (days)"
                            hint="Additional demand coverage."
                          >
                            <input
                              type="number"
                              min="0"
                              step="1"
                              value={w.settings.buffer_stock_days}
                              onChange={(e) =>
                                changeSettings({
                                  buffer_stock_days: Number(e.target.value),
                                })
                              }
                            />
                          </Field>
                          <div className="coverage-preview">
                            <strong>
                              {w.settings.minimum_stock_days +
                                w.settings.buffer_stock_days}
                              <span>days</span>
                            </strong>
                            <small>Total coverage</small>
                          </div>
                        </div>
                        <div className="formula">
                          Required EA = ceil(average daily demand × total
                          coverage)
                          <br />
                          <span>
                            Round to complete purchasing units before
                            calculating physical loads.
                          </span>
                        </div>
                        <div className="section-row">
                          <p className="muted">
                            Slot and rack capacities come from Warehouse setup.
                          </p>
                          <button
                            className="button primary"
                            disabled={
                              !w.orders ||
                              !warehouseValid ||
                              w.demandStale ||
                              !!demandDateError
                            }
                            onClick={calculate}
                          >
                            <Boxes size={16} />
                            Calculate requirements
                          </button>
                        </div>
                        {!w.orders && (
                          <p className="muted">
                            Import order history to recalculate, or use a
                            complete stock CSV.
                          </p>
                        )}
                        {w.stockStale && (
                          <div className="inline-warning">
                            <RefreshCw size={15} />
                            Stock or physical inputs changed. Recalculate or
                            import matching stock requirements.
                          </div>
                        )}
                        {w.stock.length > 0 && (
                          <>
                            <div className="metrics-grid three">
                              <Metric
                                label="Stock quantity"
                                value={count(stockTotal)}
                                detail="EA after UOM rounding"
                              />
                              <Metric
                                label="Required cells"
                                value={count(stockSlots)}
                                detail={`${count(estimatedLoads)} independent loads`}
                              />
                              <Metric
                                label="Available cells"
                                value={count(geometry?.summary.capacity)}
                                detail={`${count(geometry?.summary.rack_count)} configured racks`}
                              />
                            </div>
                            <Table
                              rows={stockRows}
                              searchable
                              label="Stock requirements"
                              filters={stockFilters(stockRows)}
                              sortable
                              empty="No stock requirements match your search and filters."
                              columns={[
                                { key: "sku", label: "SKU" },
                                {
                                  key: "velocity_class",
                                  label: "ABC",
                                  render: (v) =>
                                    v ? (
                                      <Badge tone={`class-${v}`}>{v}</Badge>
                                    ) : (
                                      "—"
                                    ),
                                },
                                {
                                  key: "total_required_ea",
                                  label: "Required EA",
                                },
                                {
                                  key: "total_slotted_ea",
                                  label: "Slotted EA",
                                },
                                { key: "units_per_slot", label: "EA / cell" },
                                { key: "required_slots", label: "Cells" },
                                {
                                  key: "rack_calculation_status",
                                  label: "Physical data",
                                  render: (v) => (
                                    <Badge
                                      tone={
                                        String(v).includes("UNVERIFIED")
                                          ? "amber"
                                          : String(v) === "ITEM_DOES_NOT_FIT"
                                            ? "red"
                                            : "green"
                                      }
                                    >
                                      {String(v)
                                        .replace("CALCULATED", "Sized")
                                        .replaceAll("_", " ")
                                        .toLowerCase()}
                                    </Badge>
                                  ),
                                },
                              ]}
                            />
                            <button
                              className="text-button"
                              onClick={() =>
                                download(
                                  "minimum_stock_requirements.csv",
                                  csvString(
                                    w.stock.map(
                                      ({ sku_requirements, ...row }) => row,
                                    ),
                                  ),
                                  "text/csv",
                                )
                              }
                            >
                              <Download size={13} />
                              Export stock CSV
                            </button>
                          </>
                        )}
                      </Panel>
                      {w.stockResult && (
                        <Panel
                          title="Attribute combination requirements"
                          subtitle="These are constructive rack estimates; a minimum is claimed only when proven."
                        >
                          <Table
                            rows={w.stockResult.combinations}
                            label="Attribute combinations"
                            searchable
                            sortable
                            columns={[
                              {
                                key: "attribute_combination",
                                label: "Storage profile",
                              },
                              { key: "sku_count", label: "SKUs" },
                              { key: "required_slots", label: "Cells" },
                              { key: "required_racks", label: "Racks" },
                              {
                                key: "minimum_proven",
                                label: "Estimate",
                                render: (v) => (
                                  <Badge tone={v ? "green" : "amber"}>
                                    {v
                                      ? "Minimum proven"
                                      : "Feasible upper bound"}
                                  </Badge>
                                ),
                              },
                            ]}
                          />
                        </Panel>
                      )}
                    </div>
                    <aside className="secondary-column">
                      <Panel
                        title="SKU attributes"
                        subtitle="Dimensions, weight and storage requirements."
                        action={
                          <UploadButton
                            label="Import"
                            accept=".csv"
                            small
                            onFile={(f) => importFile(f, "attributes")}
                          />
                        }
                      >
                        <div className="attribute-count">
                          <PackageCheck size={24} />
                          <strong>
                            {count(Object.keys(w.attributes).length)}
                          </strong>
                          <span>SKU profiles loaded</span>
                        </div>
                        <div className="config-divider" />
                        <div className="spec-row">
                          <span>Standard envelope</span>
                          <strong>
                            {Object.entries(
                              w.project.warehouse_storage_defaults,
                            )
                              .filter(([k]) => !k.endsWith("weight"))
                              .map(([, v]) => v)
                              .join(" × ")}{" "}
                            m
                          </strong>
                        </div>
                        <div className="spec-row">
                          <span>Item weight maximum</span>
                          <strong>
                            {
                              w.project.warehouse_storage_defaults
                                .max_item_weight
                            }{" "}
                            kg
                          </strong>
                        </div>
                        <div className="spec-row">
                          <span>Rack capacity</span>
                          <strong>
                            {w.project.storage_layout.levels_per_rack} levels ×{" "}
                            {w.project.storage_layout.slots_per_level} cells
                          </strong>
                        </div>
                        <button className="text-button" onClick={() => go(0)}>
                          Edit warehouse capacities <ArrowRight size={13} />
                        </button>
                      </Panel>
                      <div className="subtle-card amber">
                        <TriangleAlert size={20} />
                        <h3>Unknown is visible.</h3>
                        <p>
                          Missing dimensions use conservative stock estimates.
                          Generated placements remain marked unverified until
                          physical data is provided.
                        </p>
                      </div>
                    </aside>
                  </div>
                )}
                {step === 3 && (
                  <div className="step-columns">
                    <div className="primary-column">
                      <Panel
                        title="Placement strategy"
                        subtitle="Both strategies apply the same physical and inventory constraints."
                      >
                        <div className="strategy-grid">
                          {[
                            {
                              id: "basic",
                              name: "Basic ABC",
                              tag: "Demand priority",
                              icon: ChartNoAxesCombined,
                              description:
                                "Place high-frequency SKUs near workstations. Keep compatible racks full and consolidate copies of each SKU.",
                            },
                            {
                              id: "abc_affinity",
                              name: "Affinity",
                              tag: "Co-order grouping",
                              icon: Workflow,
                              description:
                                "Pack complete store/day orders together, then improve rack positions and feasible multi-SKU bundles.",
                            },
                          ].map((s) => (
                            <button
                              className={`strategy-card ${w.settings.strategy === s.id ? "chosen" : ""}`}
                              key={s.id}
                              onClick={() =>
                                changeSettings({
                                  strategy: s.id as Settings["strategy"],
                                })
                              }
                              aria-pressed={w.settings.strategy === s.id}
                            >
                              <div className="strategy-top">
                                <span className="strategy-icon">
                                  <s.icon size={22} />
                                </span>
                                <span className="radio-indicator">
                                  {w.settings.strategy === s.id && <span />}
                                </span>
                              </div>
                              <h3>{s.name}</h3>
                              <span className="strategy-tag">{s.tag}</span>
                              <p>{s.description}</p>
                            </button>
                          ))}
                        </div>
                      </Panel>
                      <Panel
                        title="Allocation rules"
                        subtitle="These settings apply consistently across both strategies."
                      >
                        <div className="form-grid">
                          <Field
                            label="Maximum same-SKU cells per rack"
                            hint="Counts physical occupied cells, including multi-cell footprints."
                          >
                            <input
                              type="number"
                              min="1"
                              max="256"
                              value={w.settings.maximum_same_sku_slots_per_rack}
                              onChange={(e) =>
                                changeSettings({
                                  maximum_same_sku_slots_per_rack: Number(
                                    e.target.value,
                                  ),
                                })
                              }
                            />
                          </Field>
                          <div className="rule-description">
                            <Boxes size={20} />
                            <span>
                              A maximum, rather than a target. Lower values
                              spread inventory copies across more racks.
                            </span>
                          </div>
                        </div>
                        <label className="checkbox-card">
                          <input
                            type="checkbox"
                            checked={w.settings.zone_workload_enabled}
                            onChange={(e) =>
                              changeSettings({
                                zone_workload_enabled: e.target.checked,
                              })
                            }
                          />
                          <span>
                            <strong>Balance workload across zones</strong>
                            <small>
                              Use fixed zone capacities and quantity-weighted
                              demand. Affinity refinement also checks
                              complete-order rack visits.
                            </small>
                          </span>
                        </label>
                        <label className="checkbox-card">
                          <input
                            type="checkbox"
                            checked={w.settings.ergonomic_weight_heuristic}
                            onChange={(e) =>
                              changeSettings({
                                ergonomic_weight_heuristic: e.target.checked,
                              })
                            }
                          />
                          <span>
                            <strong>
                              Prefer ergonomic levels for known item weights
                            </strong>
                            <small>
                              Middle levels are a preference. Required levels
                              for overweight inventory remain mandatory.
                            </small>
                          </span>
                        </label>
                        <div className="quiet-note">
                          <ShieldCheck size={17} />
                          <p>
                            Unique cells, known physical fit, configured
                            attributes, contiguous footprints and whole-rack
                            weight are always enforced.
                          </p>
                        </div>
                      </Panel>
                      {w.settings.strategy === "abc_affinity" && (
                        <Panel
                          title="Store-to-workstation assignments"
                          subtitle="Affinity uses fixed assignments. It never silently reallocates stores."
                          action={
                            <UploadButton
                              small
                              label="Import mapping"
                              accept=".json"
                              onFile={(f) => importFile(f, "mapping")}
                            />
                          }
                        >
                          {w.orders ? (
                            <>
                              <div className="section-row">
                                <p className="muted">
                                  {activeStores.length} stores ·{" "}
                                  {Object.keys(geometry?.stations ?? {}).length}{" "}
                                  stations
                                </p>
                                <button
                                  className="button button-small"
                                  onClick={() => {
                                    const stations = Object.keys(
                                      geometry?.stations ?? {},
                                    ).sort();
                                    if (!stations.length) return;
                                    const mapping: Record<string, string> =
                                      Object.create(null);
                                    activeStores.forEach(
                                      (store: string, i: number) =>
                                        (mapping[store] =
                                          stations[i % stations.length]),
                                    );
                                    changeSettings({
                                      store_workstation_mapping: mapping,
                                    });
                                  }}
                                >
                                  Assign stores evenly
                                </button>
                              </div>
                              <Field label="Find a store">
                                <input
                                  placeholder="Search store ID…"
                                  value={mappingQuery}
                                  onChange={(e) =>
                                    setMappingQuery(e.target.value)
                                  }
                                />
                              </Field>
                              <div className="mapping-list">
                                {activeStores
                                  .filter((s: string) =>
                                    s
                                      .toLowerCase()
                                      .includes(mappingQuery.toLowerCase()),
                                  )
                                  .map((store: string) => (
                                    <label className="mapping-row" key={store}>
                                      <span>Store {store}</span>
                                      <select
                                        value={
                                          w.settings.store_workstation_mapping[
                                            store
                                          ] ?? ""
                                        }
                                        onChange={(e) =>
                                          changeSettings({
                                            store_workstation_mapping: {
                                              ...w.settings
                                                .store_workstation_mapping,
                                              [store]: e.target.value,
                                            },
                                          })
                                        }
                                      >
                                        <option value="">
                                          Choose workstation
                                        </option>
                                        {Object.keys(geometry?.stations ?? {})
                                          .sort()
                                          .map((s) => (
                                            <option key={s}>{s}</option>
                                          ))}
                                      </select>
                                    </label>
                                  ))}
                              </div>
                              {!mappingValid && (
                                <p className="inline-warning">
                                  Assign every store to a workstation before
                                  generating.
                                </p>
                              )}
                            </>
                          ) : (
                            <Empty
                              title="Order history is required"
                              detail="Add orders in the Demand step to build affinity relationships."
                            />
                          )}
                          <p className="muted small-print">
                            Affinity keeps a feasible stock plan while grouping
                            co-ordered SKUs. Rack and bundle refinement retain
                            full coverage and guard travel and workstation
                            workload.
                          </p>
                        </Panel>
                      )}
                    </div>
                    <aside className="secondary-column">
                      <Panel
                        title="Ready to generate"
                        subtitle="A final check of your current inputs."
                      >
                        <ul className="readiness-list">
                          {[
                            [
                              warehouseValid,
                              "Warehouse geometry",
                              `${count(geometry?.summary.rack_count)} racks · ${count(geometry?.summary.capacity)} cells`,
                            ],
                            [
                              w.velocity.length > 0 && !w.demandStale,
                              "SKU demand",
                              `${count(w.velocity.length)} classified SKUs`,
                            ],
                            [
                              stockValid,
                              "Stock requirements",
                              `${count(estimatedLoads)} loads to allocate`,
                            ],
                            [
                              mappingValid,
                              "Workstation assignments",
                              w.settings.strategy === "basic"
                                ? "Not required for Basic ABC"
                                : "Fixed store assignments",
                            ],
                          ].map(([ok, title, detail]) => (
                            <li key={String(title)}>
                              {ok ? (
                                <CheckCheck size={18} />
                              ) : (
                                <CircleDashed size={18} />
                              )}
                              <div>
                                <strong>{title}</strong>
                                <small>{detail}</small>
                              </div>
                            </li>
                          ))}
                        </ul>
                        {estimatedLoads >
                          Number(geometry?.summary.capacity ?? 0) && (
                          <div className="inline-warning">
                            <TriangleAlert size={16} />
                            Requested loads exceed cell capacity. Some stock may
                            remain unassigned.
                          </div>
                        )}
                        <button
                          className="button primary full-width generate-button"
                          disabled={!canGenerate}
                          onClick={generate}
                        >
                          Generate layout <ArrowRight size={17} />
                        </button>
                        {!canGenerate && (
                          <small className="muted">
                            Complete the missing or stale inputs first.
                          </small>
                        )}
                      </Panel>
                      <div className="subtle-card">
                        <MapPinned size={20} />
                        <h3>Map rules stay in charge.</h3>
                        <p>
                          Generation keeps your zones and rack memberships. It
                          does not create hidden storage capacity.
                        </p>
                      </div>
                    </aside>
                  </div>
                )}
                {step === 4 && (
                  <>
                    {!w.result ? (
                      <Empty
                        title={
                          busy
                            ? "Your layout is being generated"
                            : "Your generated layout will appear here"
                        }
                        detail={
                          busy
                            ? "The engine is placing inventory and checking the result. You can cancel the job above."
                            : "Complete the configuration to generate and inspect a layout."
                        }
                      >
                        {!busy && (
                          <button
                            className="button primary"
                            onClick={() => go(3)}
                          >
                            Review generation settings <ArrowRight size={16} />
                          </button>
                        )}
                      </Empty>
                    ) : (
                      <>
                        <div className="results-heading">
                          <div>
                            <Badge
                              tone={
                                w.result.validation.complete ? "green" : "amber"
                              }
                            >
                              {w.result.validation.complete ? (
                                <Check size={13} />
                              ) : (
                                <TriangleAlert size={13} />
                              )}{" "}
                              {w.result.validation.complete
                                ? "All requested inventory placed"
                                : "Partial inventory placement"}
                            </Badge>
                            <span className="muted">
                              {w.result.strategy === "basic"
                                ? "Basic ABC"
                                : "Affinity"}{" "}
                              ·{" "}
                              {Number(
                                w.result.summary.generation_ms / 1000,
                              ).toFixed(2)}{" "}
                              s engine time
                            </span>
                          </div>
                          <div className="inline-actions">
                            <button
                              className="button"
                              onClick={() =>
                                download(
                                  "layout.assignments.csv",
                                  csvString(w.result!.assignments),
                                  "text/csv",
                                )
                              }
                            >
                              <Download size={15} />
                              CSV
                            </button>
                            <button
                              className="button primary"
                              onClick={() =>
                                download("warehouse.slotting.json", w.result)
                              }
                            >
                              <Download size={15} />
                              Export layout JSON
                            </button>
                          </div>
                        </div>
                        <div className="metrics-grid four result-metrics">
                          <Metric
                            label="Assigned loads"
                            value={count(w.result.summary.assigned_count)}
                            detail={`of ${count(w.result.summary.inventory_load_count)} requested`}
                            tone="positive"
                          />
                          <Metric
                            label="Occupied rack locations"
                            value={count(
                              w.result.summary.final_occupied_rack_count,
                            )}
                            detail={`of ${count(w.result.summary.rack_count)} configured`}
                          />
                          <Metric
                            label="Cell utilization"
                            value={`${((w.result.summary.occupied_slot_count / w.result.summary.capacity) * 100).toFixed(1)}%`}
                            detail={`${count(w.result.summary.occupied_slot_count)} / ${count(w.result.summary.capacity)} cells`}
                          />
                          <Metric
                            label="Unverified physical data"
                            value={count(w.result.validation.unverified_loads)}
                            detail={
                              w.result.validation.unverified_loads
                                ? "Review before operational use"
                                : "Known physical inputs checked"
                            }
                            tone={
                              w.result.validation.unverified_loads
                                ? "caution"
                                : ""
                            }
                          />
                        </div>
                        {w.result.validation.warnings.map((message: string) => (
                          <div key={message} className="notice warning">
                            <TriangleAlert size={17} />
                            <p>{message}</p>
                          </div>
                        ))}
                        <div className="result-map-layout">
                          <LayoutViewer
                            geometry={geometry}
                            project={w.project}
                            result={w.result}
                            selected={selected}
                            onSelect={setSelected}
                            orders={w.orders}
                            settings={w.settings}
                            onCalculateMovement={(range) => {
                              if (!w.result || !w.orders) return;
                              void run(
                                "Calculate rack movement",
                                async (progress) => {
                                  const movement = await engine(
                                    {
                                      command: "movement",
                                      assignments: w.result!.assignments,
                                      handling_unit_type:
                                        w.result!.handling_unit_type,
                                      orders_id: w.orders!.id,
                                      settings: { ...w.settings, ...range },
                                    },
                                    progress,
                                  );
                                  patch({ result: { ...w.result, movement } });
                                },
                              );
                            }}
                          />
                          <Panel
                            title={selected || "Rack inspector"}
                            subtitle={
                              selected
                                ? "Inventory at this physical location."
                                : "Select a rack in the layout."
                            }
                          >
                            <div className="rack-inspector">
                              <strong>{inspection.length}</strong>
                              <span>inventory loads</span>
                            </div>
                            {inspection.length ? (
                              <div className="rack-loads">
                                {inspection.map((r: Row) => (
                                  <div key={r.inventory_load_id}>
                                    <Badge tone={`class-${r.velocity_class}`}>
                                      {r.velocity_class}
                                    </Badge>
                                    <div>
                                      <strong>{r.sku}</strong>
                                      <small>
                                        L{r.storage_level} · S{r.storage_slot} ·{" "}
                                        {count(r.quantity_ea)} EA
                                      </small>
                                    </div>
                                    <span>
                                      {r.inventory_load_weight_kg.toFixed(1)} kg
                                    </span>
                                  </div>
                                ))}
                              </div>
                            ) : (
                              <p className="muted">
                                Choose an occupied rack to see SKU quantities
                                and storage cells.
                              </p>
                            )}
                          </Panel>
                        </div>
                        <Panel
                          title="Validation report"
                          subtitle="Checks run against the requested inventory and warehouse configuration."
                        >
                          <div className="validation-checks">
                            {w.result.validation.checks.map((check: string) => (
                              <div key={check}>
                                <ShieldCheck size={16} />
                                <span>{check.replaceAll("_", " ")}</span>
                              </div>
                            ))}
                          </div>
                          <p className="muted small-print">
                            Structural validation passed. Missing physical data
                            and route reachability are reported separately
                            above; this is a static layout check.
                          </p>
                        </Panel>
                        {w.result.strategy === "abc_affinity" && (
                          <Panel
                            title="Affinity compared with its ABC starting layout"
                            subtitle="Complete store/day orders with fixed workstation assignments; no stock depletion or live traffic."
                          >
                            <div className="metrics-grid three">
                              <Metric
                                label={
                                  w.result.handling_unit_type === "AMR shelf"
                                    ? "Rack presentations"
                                    : "Tote / pallet presentations"
                                }
                                value={`${count(w.result.summary.affinity_pipeline.before.deliveries)} → ${count(w.result.summary.affinity_pipeline.after.deliveries)}`}
                              />
                              <Metric
                                label="Accepted feasible moves"
                                value={
                                  w.result.summary.affinity_pipeline
                                    .accepted_moves
                                }
                              />
                              <Metric
                                label="Uncovered order groups"
                                value={
                                  w.result.summary.affinity_pipeline.after
                                    .uncovered_groups
                                }
                              />
                              {[
                                [
                                  "Round-trip travel (m)",
                                  "round_trip_distance_m",
                                ],
                                [
                                  "Peak workstation work (s)",
                                  "peak_station_work_s",
                                ],
                                [
                                  "Peak zone work / rack (s)",
                                  "maximum_zone_work_s_per_rack",
                                ],
                              ].map(([label, key]) => (
                                <Metric
                                  key={key}
                                  label={label}
                                  value={`${count(w.result!.summary.affinity_pipeline.before[key])} → ${count(w.result!.summary.affinity_pipeline.after[key])}`}
                                />
                              ))}
                            </div>
                            {w.result.summary.affinity_pipeline.after
                              .maximum_zone_work_s_per_rack >
                              w.result.summary.affinity_pipeline.before
                                .maximum_zone_work_s_per_rack +
                                1e-6 && (
                              <p className="inline-warning">
                                Peak zone workload increased compared with the
                                Basic starting layout. Review the distribution
                                before selecting this layout.
                              </p>
                            )}
                          </Panel>
                        )}
                        <Panel
                          title="Compare with an existing layout"
                          subtitle="Import a saved slotting JSON to compare the same inventory and configuration."
                          action={
                            <UploadButton
                              label="Choose reference layout"
                              accept=".json"
                              onFile={(f) => importFile(f, "reference")}
                            />
                          }
                        >
                          {comparison ? (
                            <>
                              <div className="comparison-overview">
                                <Badge
                                  tone={
                                    comparison.parameters_match
                                      ? "green"
                                      : "amber"
                                  }
                                >
                                  {comparison.parameters_match
                                    ? "Listed parameters match"
                                    : "Some inputs differ — inspect checks"}
                                </Badge>
                                <span className="muted">
                                  {w.reference?.name}
                                </span>
                              </div>
                              <div className="metrics-grid three">
                                <Metric
                                  label="Identical load placements"
                                  value={`${count(comparison.identical_placements)} / ${count(comparison.comparable_loads)}`}
                                />
                                <Metric
                                  label="Assigned inventory quantity"
                                  value={
                                    comparison.inventory_quantities_equal
                                      ? "Matches"
                                      : "Differs"
                                  }
                                  tone={
                                    comparison.inventory_quantities_equal
                                      ? "positive"
                                      : "caution"
                                  }
                                />
                                <Metric
                                  label="Assignment status changes"
                                  value={count(
                                    comparison.assignment_status_changes,
                                  )}
                                />
                              </div>
                              <div className="parameter-checks">
                                {Object.entries(
                                  comparison.parameter_checks,
                                ).map(([key, value]) => (
                                  <span key={key}>
                                    {value ? (
                                      <Check size={13} />
                                    ) : (
                                      <TriangleAlert size={13} />
                                    )}{" "}
                                    {key.replaceAll("_", " ")}
                                  </span>
                                ))}
                              </div>
                              <p className="muted small-print">
                                Placement identity is reported separately from
                                feasibility. Historical outputs may use
                                different affinity implementations or stock
                                targets. A matching checklist does not prove
                                unrecorded historical settings are equal.
                              </p>
                              {comparison.differences.length > 0 && (
                                <Table
                                  rows={comparison.differences}
                                  label="Placement differences"
                                  columns={[
                                    { key: "load", label: "Inventory load" },
                                    {
                                      key: "reference",
                                      label: "Reference location",
                                    },
                                    { key: "current", label: "New location" },
                                  ]}
                                />
                              )}
                              <button
                                className="text-button"
                                onClick={() =>
                                  download("layout-comparison.json", comparison)
                                }
                              >
                                Download comparison report{" "}
                                <Download size={13} />
                              </button>
                            </>
                          ) : (
                            <div className="comparison-empty">
                              <FileJson size={28} />
                              <p>
                                Compare placement identities, assigned
                                quantities and configuration checks with an
                                existing result.
                              </p>
                            </div>
                          )}
                        </Panel>
                        <Panel
                          title="Inventory assignments"
                          subtitle="Filter by ABC class, assignment or physical check. Click a column heading to sort."
                        >
                          <Table
                            rows={w.result.assignments}
                            searchable
                            label="Inventory assignments"
                            filters={inventoryFilters}
                            sortable
                            empty="No assignments match your search and filters."
                            columns={[
                              { key: "sku", label: "SKU" },
                              { key: "quantity_ea", label: "EA" },
                              {
                                key: "velocity_class",
                                label: "ABC",
                                render: (v) => (
                                  <Badge tone={`class-${v}`}>{v}</Badge>
                                ),
                              },
                              {
                                key: "storage_location_address",
                                label: "Storage location",
                              },
                              {
                                key: "inventory_load_weight_kg",
                                label: "Load kg",
                              },
                              {
                                key: "assignment_status",
                                label: "Assignment",
                                render: (value, row) =>
                                  value === "ASSIGNED" ? (
                                    <Badge tone="green">Assigned</Badge>
                                  ) : (
                                    <details className="assignment-issue">
                                      <summary>Unassigned</summary>
                                      <p>
                                        {row.compatibility_issues?.join(" ")}
                                      </p>
                                    </details>
                                  ),
                              },
                              {
                                key: "compatibility_status",
                                label: "Physical check",
                                render: (v) => (
                                  <Badge
                                    tone={
                                      v === "UNVERIFIED"
                                        ? "amber"
                                        : v === "COMPATIBLE"
                                          ? "green"
                                          : "red"
                                    }
                                  >
                                    {v}
                                  </Badge>
                                ),
                              },
                            ]}
                          />
                        </Panel>
                      </>
                    )}
                  </>
                )}
              </>
            )}
            <div hidden={area !== "operations"}>
              {operationsOpened && (
                <InventoryOperations
                  key={String(
                    w.result?.generated_at ??
                      w.result?.summary?.generation_ms ??
                      "no-layout",
                  )}
                  baseline={w.result}
                  project={w.project}
                  geometry={geometry}
                  settings={w.settings}
                  orders={w.orders}
                />
              )}
            </div>
          </fieldset>
          {area === "planning" && (
            <footer className="workflow-footer">
              <div>
                <span className="footer-step">{step + 1} / 5</span>
                <span>
                  {step === 0
                    ? "Define the warehouse before analyzing inventory."
                    : step === 1
                      ? "Demand comes before stock sizing."
                      : step === 2
                        ? "Resolve stock requirements before placement."
                        : step === 3
                          ? "Ready inputs lead to a traceable layout."
                          : "Export a validated result for the next stage."}
                </span>
              </div>
              <div className="inline-actions">
                {step > 0 && (
                  <button
                    className="button"
                    disabled={!!busy}
                    onClick={() => go(step - 1)}
                  >
                    <ArrowLeft size={15} />
                    Back
                  </button>
                )}
                {step < 3 && (
                  <button
                    className="button primary"
                    disabled={!!busy || !prerequisites[step]}
                    onClick={() => go(step + 1)}
                  >
                    Continue to {steps[step + 1].name.toLowerCase()}{" "}
                    <ArrowRight size={15} />
                  </button>
                )}
                {step === 3 && (
                  <button
                    className="button primary"
                    disabled={!!busy || !canGenerate}
                    onClick={generate}
                  >
                    Generate layout <ArrowRight size={16} />
                  </button>
                )}
                {step === 4 && w.result && (
                  <button
                    className="button"
                    disabled={!!busy}
                    onClick={() => go(3)}
                  >
                    Adjust and regenerate <RefreshCw size={15} />
                  </button>
                )}
              </div>
            </footer>
          )}
        </main>
      </div>
      <dialog
        ref={helpRef}
        className="help-dialog"
        onCancel={() => setHelp(false)}
        onClick={(e) => {
          if (e.target === helpRef.current) setHelp(false);
        }}
      >
        <div className="dialog-inner">
          <button
            className="icon-button dialog-close"
            aria-label="Close workflow help"
            onClick={() => setHelp(false)}
          >
            <X size={20} />
          </button>
          <div className="eyebrow">LAYOUT GENERATOR</div>
          <h2>A clear path from inputs to layout.</h2>
          <p>
            Each step prepares the next. Changes that affect stock invalidate
            dependent results, so a stale layout is never presented as current.
          </p>
          <ol>
            {steps.map((s) => (
              <li key={s.name}>
                <strong>{s.name}</strong>
                <span>{s.detail}</span>
              </li>
            ))}
          </ol>
          <p>
            Traffic-aware slotting is deferred. Affinity groups complete
            store/day orders and refines rack positions and multi-SKU bundles
            using your workstation mapping.
          </p>
          <div className="dialog-actions">
            <button
              className="button"
              onClick={() => {
                setHelp(false);
                openExample("amr");
              }}
            >
              Open AMR example
            </button>
            <button className="button primary" onClick={() => setHelp(false)}>
              Back to workspace
            </button>
          </div>
        </div>
      </dialog>
    </div>
  );
}

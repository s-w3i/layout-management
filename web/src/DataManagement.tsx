import { useEffect, useRef, useState } from "react";
import {
  Database,
  RefreshCw,
  ShieldCheck,
  Copy,
  Download,
  LoaderCircle,
  CheckCircle2,
} from "lucide-react";
import type { Row } from "../../shared/model";
import { stockStatuses, exactQuantity } from "../../shared/inventory";
import { taskTypes, taskTypeForId } from "../../shared/tasks";
import { api, download, upload } from "./api";
import {
  Badge,
  Field,
  Metric,
  Panel,
  Table,
  UploadButton,
  count,
} from "./components";

const post = (path: string, value: Row) =>
  api(`/api/manage/${path}`, { method: "POST", body: JSON.stringify(value) });
const tabs = [
  "Overview",
  "Master data",
  "Inventory",
  "Tasks & activity",
  "Publish",
  "Integration",
];
export default function DataManagement({
  workspaceId,
  workspaceName,
  save,
  openWorkspace,
  useOrders,
  useAttributes,
}: {
  workspaceId: string;
  workspaceName: string;
  save: () => Promise<number>;
  openWorkspace: (id: string) => Promise<void>;
  useOrders: (orders: Row) => void;
  useAttributes: (attributes: Row) => void;
}) {
  const [tab, setTab] = useState("Overview"),
    [busy, setBusy] = useState(""),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [workspaces, setWorkspaces] = useState<Row[]>([]),
    [warehouses, setWarehouses] = useState<Row[]>([]),
    [skus, setSkus] = useState<Row[]>([]),
    [partners, setPartners] = useState<Row[]>([]),
    [datasets, setDatasets] = useState<Row[]>([]),
    [keys, setKeys] = useState<Row[]>([]);
  const [locations, setLocations] = useState<Row[]>([]),
    [inventory, setInventory] = useState<Row[]>([]),
    [tasks, setTasks] = useState<Row[]>([]),
    [events, setEvents] = useState<Row[]>([]),
    [reservations, setReservations] = useState<Row[]>([]),
    [releases, setReleases] = useState<Row[]>([]),
    [equipment, setEquipment] = useState<Row[]>([]);
  const [health, setHealth] = useState<Row>({}),
    [skuDraft, setSkuDraft] = useState<Row>({}),
    [partner, setPartner] = useState<Row>({ kind: "store" }),
    [machine, setMachine] = useState<Row>({ kind: "AMR" });
  const [operation, setOperation] = useState<Row>({
    kind: "receipt",
    actor: "Local operator",
    quantity: "1",
    status: "available",
  });
  const [releaseName, setReleaseName] = useState(""),
    [keyDraft, setKeyDraft] = useState({ name: "", scope: "read" }),
    [newToken, setNewToken] = useState("");
  const [lineView, setLineView] = useState<Row | null>(null),
    [lineOffset, setLineOffset] = useState(0),
    [lineSku, setLineSku] = useState("");
  const [eventsExhausted, setEventsExhausted] = useState(false);
  const errorRef = useRef<HTMLDivElement>(null),
    loadVersion = useRef(0),
    mounted = useRef(true);
  const pendingOperation = useRef<{ fingerprint: string; id: string } | null>(
    null,
  );
  const [timeZone, setTimeZone] = useState(
    Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC",
  );
  const registered = warehouses.some((w) => w.id === workspaceId);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  async function refresh() {
    const version = ++loadVersion.current;
    const [ws, wh, sku, ps, ds, ks, h] = await Promise.all(
      [
        "workspaces",
        "warehouses",
        "skus",
        "partners",
        "datasets",
        "keys",
        "health",
      ].map((p) => api(`/api/manage/${p}`)),
    );
    if (version !== loadVersion.current || !mounted.current) return;
    setWorkspaces(ws);
    setWarehouses(wh);
    setTimeZone(
      wh.find((w: Row) => w.id === workspaceId)?.time_zone ||
        Intl.DateTimeFormat().resolvedOptions().timeZone ||
        "UTC",
    );
    setSkus(sku);
    setPartners(ps);
    setDatasets(ds);
    setKeys(ks);
    setHealth(h);
    if (wh.some((w: Row) => w.id === workspaceId)) {
      const values = await Promise.all(
        [
          "locations",
          "inventory",
          "tasks",
          "events",
          "reservations",
          "releases",
          "equipment",
        ].map((p) => api(`/api/manage/warehouses/${workspaceId}/${p}`)),
      );
      if (version !== loadVersion.current || !mounted.current) return;
      setLocations(values[0]);
      setInventory(values[1]);
      setTasks(values[2]);
      setEvents(values[3]);
      setEventsExhausted(values[3].length < 200);
      setReservations(values[4]);
      setReleases(values[5]);
      setEquipment(values[6]);
    } else {
      setLocations([]);
      setInventory([]);
      setTasks([]);
      setEvents([]);
      setReservations([]);
      setReleases([]);
      setEquipment([]);
    }
  }
  async function run(
    label: string,
    action: () => Promise<unknown>,
    refreshAfter = true,
  ) {
    setBusy(label);
    setError("");
    setNotice("");
    try {
      await action();
      if (refreshAfter) await refresh();
      setNotice((previous) => previous || `${label} completed.`);
    } catch (e: any) {
      setError(e.message);
      setTimeout(() => errorRef.current?.focus(), 0);
    } finally {
      setBusy("");
    }
  }
  useEffect(() => {
    void run("Load warehouse data", refresh, false);
  }, [workspaceId]);
  const change = (
    setter: (value: Row) => void,
    draft: Row,
    key: string,
    value: unknown,
  ) => setter({ ...draft, [key]: value });
  const field = (
    label: string,
    draft: Row,
    setter: (value: Row) => void,
    key: string,
    options: {
      type?: string;
      required?: boolean;
      hint?: string;
      readOnly?: boolean;
      list?: string;
    } = {},
  ) => (
    <Field label={label} hint={options.hint}>
      <input
        type={options.type || "text"}
        value={draft[key] ?? ""}
        required={options.required}
        readOnly={options.readOnly}
        list={options.list}
        onChange={(e) => change(setter, draft, key, e.target.value)}
      />
    </Field>
  );
  const warehousePath = `warehouses/${workspaceId}`;
  const receipt = ["opening", "receipt", "return"].includes(operation.kind);
  const reservationAction = ["dispatch", "cancel_reservation"].includes(
    operation.kind,
  );
  const needsDestination = [
    "transfer",
    "task_create",
    "container_transfer",
    "task_redirect",
  ].includes(operation.kind);
  const wholeContainer = operation.kind === "container_transfer",
    redirect = operation.kind === "task_redirect";
  async function postOperation() {
    const value = {
      ...operation,
      ...(operation.kind === "task_create"
        ? { task_type: operation.task_type ?? "delivery" }
        : {}),
    };
    const fingerprint = JSON.stringify({
      warehouse_id: workspaceId,
      ...value,
    });
    if (pendingOperation.current?.fingerprint !== fingerprint)
      pendingOperation.current = { fingerprint, id: crypto.randomUUID() };
    const result = await post(`${warehousePath}/inventory`, {
      ...value,
      request_id: pendingOperation.current!.id,
      expected_revision: operation.expected_revision || undefined,
    });
    if (result.task_id)
      setNotice(
        `${taskTypes[taskTypeForId(result.task_id)].label} task assigned: ${result.task_id}`,
      );
    pendingOperation.current = null;
    setOperation({
      ...operation,
      request_id: "",
      reason: "",
      expected_revision: undefined,
    });
  }
  const csvUpload = (kind: string, label: string) => (
    <UploadButton
      label={label}
      accept=".csv"
      small
      onFile={(file) =>
        void run(label, async () => {
          await post(`${kind}/csv`, { csv: await file.text() });
        })
      }
    />
  );
  async function viewLines(id: string, offset = 0, sku = lineSku) {
    const result = await api(
      `/api/manage/datasets/${id}/lines?offset=${offset}&sku=${encodeURIComponent(sku)}`,
    );
    setLineOffset(offset);
    setLineView({ ...result, id });
  }
  return (
    <div className="data-management">
      <div className="management-heading">
        <div>
          <Database size={20} />
          <span>
            <strong>{workspaceName}</strong>
            <small>
              {registered
                ? "Operational warehouse registered"
                : "Planning workspace · register to manage inventory"}
            </small>
          </span>
        </div>
        <button
          className="button button-small"
          disabled={!!busy}
          onClick={() => void run("Refresh data", refresh, false)}
        >
          <RefreshCw size={15} />
          Refresh
        </button>
      </div>
      <div
        className="management-tabs"
        role="tablist"
        aria-label="Data management sections"
      >
        {tabs.map((t) => (
          <button
            type="button"
            id={`manage-tab-${t.replaceAll(" ", "-")}`}
            role="tab"
            key={t}
            aria-selected={tab === t}
            aria-controls="management-panel"
            tabIndex={tab === t ? 0 : -1}
            className={tab === t ? "active" : ""}
            onClick={() => setTab(t)}
            onKeyDown={(e) => {
              const i = tabs.indexOf(t);
              if (["ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) {
                e.preventDefault();
                const next =
                  e.key === "Home"
                    ? 0
                    : e.key === "End"
                      ? tabs.length - 1
                      : (i + (e.key === "ArrowRight" ? 1 : -1) + tabs.length) %
                        tabs.length;
                setTab(tabs[next]);
                document
                  .getElementById(
                    `manage-tab-${tabs[next].replaceAll(" ", "-")}`,
                  )
                  ?.focus();
              }
            }}
          >
            {t}
          </button>
        ))}
      </div>
      {busy && (
        <div className="notice progress" role="status">
          <LoaderCircle size={18} className="spin" />
          {busy}…
        </div>
      )}
      {error && (
        <div className="notice error" ref={errorRef} tabIndex={-1} role="alert">
          {error}
        </div>
      )}
      {notice && (
        <div className="notice success" role="status">
          <CheckCircle2 size={17} />
          {notice}
        </div>
      )}
      <fieldset disabled={!!busy} className="workspace-fieldset">
        <div
          id="management-panel"
          role="tabpanel"
          aria-labelledby={`manage-tab-${tab.replaceAll(" ", "-")}`}
        >
          {tab === "Overview" && (
            <>
              <div className="metrics-grid">
                <Metric label="SKU master" value={count(skus.length)} />
                <Metric
                  label="Storage locations"
                  value={count(
                    locations.filter((l) => l.kind === "storage").length,
                  )}
                />
                <Metric
                  label="Stock positions"
                  value={count(inventory.length)}
                />
                <Metric
                  label="Published releases"
                  value={count(releases.length)}
                />
              </div>
              <Panel
                title="Prepare a warehouse for operations"
                subtitle="Register locations and SKU master data before recording opening stock. Planning stock targets are kept separate from operational quantities."
              >
                <div className="management-fields">
                  <Field
                    label="Warehouse time zone"
                    hint="Used for expiry dates. Event timestamps remain UTC."
                  >
                    <input
                      required
                      value={timeZone}
                      onChange={(e) => setTimeZone(e.target.value)}
                      placeholder="Asia/Kuala_Lumpur"
                    />
                  </Field>
                </div>
                <div className="management-actions">
                  <button
                    className="button primary"
                    onClick={() =>
                      void run("Register warehouse", async () => {
                        await save();
                        await post(`${warehousePath}/sync`, {
                          time_zone: timeZone,
                        });
                        await post("skus/import", {
                          workspace_id: workspaceId,
                        });
                      })
                    }
                  >
                    {registered
                      ? "Synchronize warehouse"
                      : "Register this warehouse"}
                  </button>
                  <button
                    className="button"
                    onClick={() => setTab("Inventory")}
                  >
                    Record opening stock
                  </button>
                  <button className="button" onClick={() => setTab("Publish")}>
                    Review publishing
                  </button>
                </div>
                <p className="management-hint">
                  Registering copies the warehouse structure and adds missing
                  SKUs. Inventory starts empty. Geometry changes are blocked
                  while stock is present.
                </p>
              </Panel>
              <Panel
                title="Saved workspaces"
                subtitle="Workspaces are saved in SQLite. Open a workspace to continue its layout workflow."
              >
                <Table
                  label="Saved workspaces"
                  rows={workspaces}
                  searchable
                  sortable
                  columns={[
                    { key: "name", label: "Workspace" },
                    {
                      key: "id",
                      label: "Workspace ID",
                      render: (id: string) => (
                        <code title={id}>{id.slice(0, 8)}</code>
                      ),
                    },
                    { key: "revision", label: "Revision" },
                    { key: "updated_at", label: "Saved" },
                    {
                      key: "_actions",
                      sortable: false,
                      label: "Action",
                      render: (_: unknown, row: Row) => (
                        <button
                          className="button button-small"
                          disabled={row.id === workspaceId}
                          onClick={() =>
                            void run("Open workspace", () =>
                              openWorkspace(row.id),
                            )
                          }
                        >
                          Open workspace
                        </button>
                      ),
                    },
                  ]}
                />
              </Panel>
              <Panel
                title="Database and backup"
                subtitle="Backups include warehouse configuration, inventory, releases and transaction history. Keep original import files with your deployment backup."
              >
                <div className="management-actions">
                  <Badge tone="green">
                    SQLite {health.sqlite_version || "…"}
                  </Badge>
                  <Badge>Integrity: {health.integrity || "checking"}</Badge>
                  <button
                    className="button"
                    onClick={() =>
                      void run("Create database backup", async () => {
                        const r = await post("backups", {});
                        setNotice(`Backup saved in .data/backups/${r.name}`);
                      })
                    }
                  >
                    <Download size={15} />
                    Create backup
                  </button>
                </div>
              </Panel>
            </>
          )}
          {tab === "Master data" && (
            <>
              <Panel
                title="SKU master"
                subtitle="Manage base units, exact UOM conversions and physical storage requirements. Existing master records are preserved when importing planning SKUs."
                action={
                  <div className="management-actions">
                    <button
                      className="button button-small"
                      onClick={() =>
                        void run("Import planning SKUs", async () => {
                          await save();
                          await post("skus/import", {
                            workspace_id: workspaceId,
                          });
                        })
                      }
                    >
                      Import planning SKUs
                    </button>
                    {csvUpload("skus", "Import SKU CSV")}
                    {csvUpload("uoms", "Import UOM CSV")}
                  </div>
                }
              >
                <Table
                  label="SKU master"
                  rows={skus}
                  searchable
                  sortable
                  columns={[
                    { key: "sku", label: "SKU" },
                    { key: "name", label: "Name" },
                    { key: "base_uom", label: "Base UOM" },
                    {
                      key: "active",
                      label: "Active",
                      render: (v) => (v ? "Yes" : "No"),
                    },
                    { key: "revision", label: "Revision" },
                    {
                      key: "_actions",
                      sortable: false,
                      label: "Edit",
                      render: (_: string, row: Row) => (
                        <button
                          className="button button-small"
                          onClick={() =>
                            setSkuDraft({
                              ...row,
                              ...row.attributes,
                              uom_text: row.uoms
                                .filter((u: Row) => u.uom !== row.base_uom)
                                .map(
                                  (u: Row) =>
                                    `${u.uom},${u.factor_micro != null ? exactQuantity(u.factor_micro) : u.factor}`,
                                )
                                .join("\n"),
                            })
                          }
                        >
                          Edit SKU
                        </button>
                      ),
                    },
                  ]}
                />
                <form
                  className="management-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void run("Save SKU", async () => {
                      const attributes: Row = { ...skuDraft.attributes };
                      for (const key of [
                        "max_item_length",
                        "max_item_width",
                        "max_item_height",
                        "max_item_weight",
                      ])
                        if (skuDraft[key] !== "" && skuDraft[key] != null)
                          attributes[key] = Number(skuDraft[key]);
                        else delete attributes[key];
                      for (const key of ["chilled", "tablet", "flammable"])
                        if (skuDraft[key] !== undefined && skuDraft[key] !== "")
                          attributes[key] =
                            skuDraft[key] === true || skuDraft[key] === "true";
                        else delete attributes[key];
                      const uoms = String(skuDraft.uom_text || "")
                        .split("\n")
                        .filter(Boolean)
                        .map((line) => {
                          const [uom, factor] = line.split(",");
                          return { uom: uom.trim(), factor: factor?.trim() };
                        });
                      await post("skus", {
                        sku: skuDraft.sku,
                        name: skuDraft.name,
                        base_uom: skuDraft.base_uom || "EA",
                        attributes,
                        uoms,
                        revision: skuDraft.revision,
                        active: skuDraft.active !== 0,
                      });
                      setSkuDraft({});
                    });
                  }}
                >
                  <h3>{skuDraft.revision ? "Edit SKU" : "Add SKU"}</h3>
                  <div className="management-fields">
                    {field("SKU code", skuDraft, setSkuDraft, "sku", {
                      required: true,
                      readOnly: !!skuDraft.revision,
                    })}
                    {field("Name", skuDraft, setSkuDraft, "name", {
                      required: true,
                    })}
                    {field("Base UOM", skuDraft, setSkuDraft, "base_uom", {
                      hint: "Defaults to EA",
                    })}
                    <Field label="SKU status">
                      <select
                        value={skuDraft.active === 0 ? "inactive" : "active"}
                        onChange={(e) =>
                          change(
                            setSkuDraft,
                            skuDraft,
                            "active",
                            e.target.value === "active" ? 1 : 0,
                          )
                        }
                      >
                        <option value="active">Active</option>
                        <option value="inactive">Inactive</option>
                      </select>
                    </Field>
                    {[
                      ["max_item_length", "Item length (m)"],
                      ["max_item_width", "Item width (m)"],
                      ["max_item_height", "Item height (m)"],
                      ["max_item_weight", "Item weight (kg)"],
                    ].map(([key, label]) => (
                      <div key={key}>
                        {field(label, skuDraft, setSkuDraft, key, {
                          hint: "Leave empty when unknown",
                        })}
                      </div>
                    ))}
                    {["chilled", "tablet", "flammable"].map((key) => (
                      <Field key={key} label={key}>
                        <select
                          value={
                            skuDraft[key] === undefined
                              ? ""
                              : String(skuDraft[key])
                          }
                          onChange={(e) =>
                            change(setSkuDraft, skuDraft, key, e.target.value)
                          }
                        >
                          <option value="">Unknown</option>
                          <option value="true">Required</option>
                          <option value="false">Not required</option>
                        </select>
                      </Field>
                    ))}
                    <Field
                      label="Additional UOM conversions"
                      hint="One per line: CTN,12 means one CTN contains 12 base units."
                    >
                      <textarea
                        rows={3}
                        value={skuDraft.uom_text || ""}
                        onChange={(e) =>
                          change(
                            setSkuDraft,
                            skuDraft,
                            "uom_text",
                            e.target.value,
                          )
                        }
                      />
                    </Field>
                  </div>
                  <div className="management-actions">
                    <button className="button primary" type="submit">
                      Save SKU
                    </button>
                    <button
                      className="button"
                      type="button"
                      onClick={() => setSkuDraft({})}
                    >
                      Clear form
                    </button>
                    <button
                      className="button"
                      type="button"
                      onClick={() =>
                        useAttributes(
                          Object.fromEntries(
                            skus
                              .filter((s) => s.active)
                              .map((s) => [s.sku, s.attributes]),
                          ),
                        )
                      }
                    >
                      Use master attributes in planning
                    </button>
                  </div>
                </form>
              </Panel>
              <Panel
                title="Stores and suppliers"
                action={csvUpload("partners", "Import partner CSV")}
              >
                <Table
                  label="Stores and suppliers"
                  rows={partners}
                  searchable
                  sortable
                  columns={[
                    { key: "id", label: "Code" },
                    { key: "kind", label: "Type" },
                    { key: "name", label: "Name" },
                    {
                      key: "_actions",
                      sortable: false,
                      label: "Edit",
                      render: (_: string, row: Row) => (
                        <button
                          className="button button-small"
                          onClick={() => setPartner({ ...row, ...row.details })}
                        >
                          Edit
                        </button>
                      ),
                    },
                  ]}
                />
                <form
                  className="management-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void run("Save partner", async () => {
                      await post("partners", {
                        ...partner,
                        details: {
                          ...partner.details,
                          contact: partner.contact || "",
                          workstation_id: partner.workstation_id || "",
                        },
                      });
                      setPartner({ kind: "store" });
                    });
                  }}
                >
                  <div className="management-fields">
                    {field("Code", partner, setPartner, "id", {
                      required: true,
                      readOnly: !!partner.revision,
                    })}
                    {field("Name", partner, setPartner, "name", {
                      required: true,
                    })}
                    <Field label="Type">
                      <select
                        value={partner.kind}
                        onChange={(e) =>
                          change(setPartner, partner, "kind", e.target.value)
                        }
                      >
                        <option value="store">Store</option>
                        <option value="supplier">Supplier</option>
                      </select>
                    </Field>
                    {field("Contact", partner, setPartner, "contact")}
                    {field(
                      "Workstation ID",
                      partner,
                      setPartner,
                      "workstation_id",
                    )}
                  </div>
                  <button className="button primary" type="submit">
                    Save partner
                  </button>
                </form>
              </Panel>
              <Panel
                title="Equipment capabilities"
                subtitle="Define AMR and ASRS equipment for external systems. Motion and simulation execution are handled by the consuming system."
              >
                <Table
                  label="Equipment"
                  rows={equipment}
                  sortable
                  columns={[
                    { key: "id", label: "ID" },
                    { key: "name", label: "Name" },
                    { key: "kind", label: "Type" },
                    {
                      key: "_actions",
                      sortable: false,
                      label: "Edit",
                      render: (_: string, r: Row) => (
                        <button
                          className="button button-small"
                          onClick={() => setMachine({ ...r, ...r.details })}
                        >
                          Edit
                        </button>
                      ),
                    },
                  ]}
                />
                <form
                  className="management-form"
                  onSubmit={(e) => {
                    e.preventDefault();
                    void run("Save equipment", async () => {
                      await post("equipment", {
                        ...machine,
                        warehouse_id: workspaceId,
                        details: {
                          ...machine.details,
                          max_payload_kg: machine.max_payload_kg
                            ? Number(machine.max_payload_kg)
                            : null,
                          workstation_id: machine.workstation_id || "",
                        },
                      });
                      setMachine({ kind: "AMR" });
                    });
                  }}
                >
                  <div className="management-fields">
                    {field("Equipment ID", machine, setMachine, "id", {
                      required: true,
                      readOnly: !!machine.revision,
                    })}
                    {field("Name", machine, setMachine, "name", {
                      required: true,
                    })}
                    <Field label="Equipment type">
                      <select
                        value={machine.kind}
                        onChange={(e) =>
                          change(setMachine, machine, "kind", e.target.value)
                        }
                      >
                        <option>AMR</option>
                        <option>ASRS</option>
                      </select>
                    </Field>
                    {field(
                      "Maximum payload (kg)",
                      machine,
                      setMachine,
                      "max_payload_kg",
                    )}
                    {field(
                      "Workstation ID",
                      machine,
                      setMachine,
                      "workstation_id",
                    )}
                  </div>
                  <button
                    disabled={!registered}
                    className="button primary"
                    type="submit"
                  >
                    Save equipment
                  </button>
                </form>
              </Panel>
              <Panel
                title="Imported datasets"
                subtitle="New order imports retain original line records in SQLite. Older datasets may contain aggregated history only."
                action={
                  <UploadButton
                    label="Import order history"
                    accept=".csv,.xlsx"
                    onFile={(f) =>
                      void run("Import order history", () =>
                        upload(f, "orders", (text) => setBusy(text)),
                      )
                    }
                  />
                }
              >
                <Table
                  label="Imported datasets"
                  rows={datasets}
                  searchable
                  sortable
                  columns={[
                    { key: "name", label: "File" },
                    { key: "kind", label: "Type" },
                    { key: "status", label: "Status" },
                    {
                      key: "raw_count",
                      label: "Original lines",
                      render: count,
                    },
                    {
                      key: "metadata",
                      label: "Date range",
                      render: (m: Row) =>
                        m.date_start ? `${m.date_start} – ${m.date_end}` : "—",
                    },
                    {
                      key: "_actions",
                      sortable: false,
                      label: "Actions",
                      render: (_: string, r: Row) => (
                        <div className="management-actions">
                          {r.kind === "orders" && r.status === "ready" && (
                            <>
                              <button
                                className="button button-small"
                                onClick={() =>
                                  void run(
                                    "Select planning history",
                                    async () =>
                                      useOrders({
                                        ...r.metadata,
                                        id: r.id,
                                        name: r.name,
                                        kind: "orders",
                                      }),
                                  )
                                }
                              >
                                Use for planning
                              </button>
                              <button
                                className="button button-small"
                                onClick={() =>
                                  void run("Read original lines", () =>
                                    viewLines(r.id, 0),
                                  )
                                }
                              >
                                View lines
                              </button>
                            </>
                          )}
                        </div>
                      ),
                    },
                  ]}
                />
                {lineView && (
                  <div className="management-form">
                    <div className="management-actions">
                      <Field label="Exact SKU filter">
                        <input
                          value={lineSku}
                          onChange={(e) => setLineSku(e.target.value)}
                        />
                      </Field>
                      <button
                        className="button"
                        onClick={() =>
                          void run("Filter order lines", () =>
                            viewLines(lineView.id, 0),
                          )
                        }
                      >
                        Apply
                      </button>
                    </div>
                    <Table
                      label="Original order lines"
                      rows={lineView.rows}
                      sortable
                      empty="No retained original lines match. Aggregated legacy datasets remain available for planning."
                      columns={[
                        { key: "source_row", label: "Source row" },
                        { key: "date", label: "Date" },
                        { key: "sku", label: "SKU" },
                        { key: "store", label: "Store" },
                        { key: "quantity_ea", label: "Signed quantity" },
                        { key: "conversion", label: "UOM factor" },
                      ]}
                    />
                    <div className="management-actions">
                      <button
                        className="button button-small"
                        disabled={!lineOffset}
                        onClick={() =>
                          void run("Read previous lines", () =>
                            viewLines(
                              lineView.id,
                              Math.max(0, lineOffset - 100),
                            ),
                          )
                        }
                      >
                        Previous 100
                      </button>
                      <span>{count(lineView.total)} retained lines</span>
                      <button
                        className="button button-small"
                        disabled={lineOffset + 100 >= lineView.total}
                        onClick={() =>
                          void run("Read next lines", () =>
                            viewLines(lineView.id, lineOffset + 100),
                          )
                        }
                      >
                        Next 100
                      </button>
                    </div>
                  </div>
                )}
              </Panel>
            </>
          )}
          {tab === "Inventory" && (
            <>
              {!registered && (
                <div className="notice">
                  Register this workspace on the Overview tab before recording
                  inventory.
                </div>
              )}
              <Panel
                title="Operational inventory"
                subtitle="Recorded stock is separate from slotting targets. Reserved, expired, blocked and in-transit stock is excluded from available quantity."
              >
                <Table
                  label="Operational inventory"
                  rows={inventory}
                  searchable
                  sortable
                  filters={[
                    {
                      key: "status",
                      label: "Stock status",
                      options: stockStatuses.map((s) => ({
                        value: s,
                        label: s,
                      })),
                    },
                  ]}
                  columns={[
                    { key: "sku", label: "SKU" },
                    { key: "location_id", label: "Location" },
                    { key: "container", label: "Container" },
                    { key: "lot", label: "Lot" },
                    { key: "expiry", label: "Expiry" },
                    { key: "status", label: "Status" },
                    {
                      key: "quantity",
                      label: "On hand",
                      render: (_: unknown, r: Row) => r.quantity_exact,
                    },
                    { key: "base_uom", label: "UOM" },
                    {
                      key: "reserved",
                      label: "Reserved",
                      render: (_: unknown, r: Row) => r.reserved_exact,
                    },
                    {
                      key: "available",
                      label: "Available",
                      render: (_: unknown, r: Row) => r.available_exact,
                    },
                    {
                      key: "physical_check",
                      label: "Physical check",
                      render: (v: string, r: Row) => (
                        <span title={r.physical_reason}>
                          {v === "ITEM_CHECKED" ? "Item checks only" : v}
                        </span>
                      ),
                    },
                    {
                      key: "_actions",
                      sortable: false,
                      label: "Select",
                      render: (id: string, r: Row) => (
                        <button
                          className="button button-small"
                          onClick={() =>
                            setOperation({
                              ...operation,
                              kind: "transfer",
                              position_id: r.id,
                              sku: r.sku,
                              quantity: "1",
                              expected_revision: r.revision,
                              reservation_id: "",
                            })
                          }
                        >
                          Use stock
                        </button>
                      ),
                    },
                  ]}
                />
              </Panel>
              <Panel
                title="Record an inventory operation"
                subtitle="Every confirmed operation creates an immutable transaction. Create a movement task to track pickup and completion separately."
              >
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    void run("Post inventory operation", postOperation);
                  }}
                >
                  <div className="management-fields">
                    <Field label="Operation">
                      <select
                        value={operation.kind}
                        onChange={(e) =>
                          setOperation({
                            ...operation,
                            kind: e.target.value,
                            request_id: "",
                            status:
                              e.target.value === "return"
                                ? "quarantine"
                                : "available",
                          })
                        }
                      >
                        {[
                          ["opening", "Opening balance"],
                          ["receipt", "Receive stock"],
                          ["return", "Return stock"],
                          [
                            "adjustment",
                            "Stock count adjustment (signed delta)",
                          ],
                          ["transfer", "Confirm completed transfer"],
                          ["container_transfer", "Move whole container"],
                          ["task_redirect", "Redirect movement task"],
                          [
                            "task_create",
                            "Create delivery / replenishment task",
                          ],
                          ["status", "Change stock status"],
                          ["reserve", "Reserve a selected stock position"],
                          ["reserve_sku", "Reserve SKU by earliest expiry"],
                          ["dispatch", "Confirm dispatch of reservation"],
                          ["cancel_reservation", "Cancel outbound reservation"],
                        ].map(([v, l]) => (
                          <option key={v} value={v}>
                            {l}
                          </option>
                        ))}
                      </select>
                    </Field>
                    {operation.kind === "task_create" && (
                      <Field label="Task purpose">
                        <select
                          value={operation.task_type ?? "delivery"}
                          onChange={(e) =>
                            change(
                              setOperation,
                              operation,
                              "task_type",
                              e.target.value,
                            )
                          }
                        >
                          {Object.entries(taskTypes).map(([type, t]) => (
                            <option key={type} value={type}>
                              {t.label} · {t.prefix}-…
                            </option>
                          ))}
                        </select>
                      </Field>
                    )}
                    {field("Operator", operation, setOperation, "actor", {
                      required: true,
                    })}
                    {field(
                      "Reason / order reference",
                      operation,
                      setOperation,
                      "reason",
                      { required: true },
                    )}
                    {wholeContainer ? (
                      field(
                        "Container code",
                        operation,
                        setOperation,
                        "container",
                        { required: true },
                      )
                    ) : redirect ? (
                      <Field label="Active movement task">
                        <select
                          required
                          value={operation.task_id || ""}
                          onChange={(e) =>
                            change(
                              setOperation,
                              operation,
                              "task_id",
                              e.target.value,
                            )
                          }
                        >
                          <option value="">Choose task</option>
                          {tasks
                            .filter((t) =>
                              ["reserved", "in_transit", "failed"].includes(
                                t.status,
                              ),
                            )
                            .map((t) => (
                              <option key={t.id} value={t.id}>
                                {t.id} · {t.reference} · {t.status}
                              </option>
                            ))}
                        </select>
                      </Field>
                    ) : receipt || operation.kind === "reserve_sku" ? (
                      field("SKU", operation, setOperation, "sku", {
                        required: true,
                        list: "management-skus",
                      })
                    ) : reservationAction ? (
                      <Field label="Active reservation">
                        <select
                          required
                          value={operation.reservation_id || ""}
                          onChange={(e) =>
                            change(
                              setOperation,
                              operation,
                              "reservation_id",
                              e.target.value,
                            )
                          }
                        >
                          <option value="">Choose reservation</option>
                          {reservations
                            .filter(
                              (r) =>
                                r.status === "active" &&
                                !tasks.some((t) => t.reservation_id === r.id),
                            )
                            .map((r) => (
                              <option key={r.id} value={r.id}>
                                {r.sku} · {r.quantity} · {r.reference}
                              </option>
                            ))}
                        </select>
                      </Field>
                    ) : (
                      <Field label="Stock position">
                        <select
                          required
                          value={operation.position_id || ""}
                          onChange={(e) => {
                            const p = inventory.find(
                              (p) => p.id === e.target.value,
                            );
                            setOperation({
                              ...operation,
                              position_id: e.target.value,
                              sku: p?.sku,
                              expected_revision: p?.revision,
                            });
                          }}
                        >
                          <option value="">Choose stock</option>
                          {inventory.map((p) => (
                            <option key={p.id} value={p.id}>
                              {p.sku} · {p.location_id} · lot {p.lot || "—"} ·{" "}
                              {p.available} available
                            </option>
                          ))}
                        </select>
                      </Field>
                    )}
                    {!reservationAction && !wholeContainer && !redirect && (
                      <>
                        {field(
                          "Quantity",
                          operation,
                          setOperation,
                          "quantity",
                          {
                            required: true,
                            hint:
                              operation.kind === "adjustment"
                                ? "Enter the difference, e.g. -2 to remove two units."
                                : "Up to six decimal places.",
                          },
                        )}
                        <Field label="Quantity UOM">
                          <select
                            value={operation.uom || ""}
                            onChange={(e) =>
                              change(
                                setOperation,
                                operation,
                                "uom",
                                e.target.value,
                              )
                            }
                          >
                            <option value="">SKU base unit</option>
                            {(
                              skus.find((s) => s.sku === operation.sku)?.uoms ||
                              []
                            ).map((u: Row) => (
                              <option key={u.uom}>{u.uom}</option>
                            ))}
                          </select>
                        </Field>
                      </>
                    )}
                    {receipt && (
                      <>
                        {field(
                          "Location",
                          operation,
                          setOperation,
                          "location_id",
                          { required: true, list: "management-locations" },
                        )}
                        {field(
                          "Container / pallet code",
                          operation,
                          setOperation,
                          "container",
                        )}
                        {field("Lot / batch", operation, setOperation, "lot")}
                        {field(
                          "Expiry date",
                          operation,
                          setOperation,
                          "expiry",
                          { type: "date" },
                        )}
                      </>
                    )}
                    {needsDestination &&
                      field(
                        "Destination location",
                        operation,
                        setOperation,
                        "destination",
                        { required: true, list: "management-locations" },
                      )}
                    {(receipt || operation.kind === "status") && (
                      <Field label="Stock status">
                        <select
                          value={operation.status}
                          onChange={(e) =>
                            change(
                              setOperation,
                              operation,
                              "status",
                              e.target.value,
                            )
                          }
                        >
                          {stockStatuses.map((s) => (
                            <option key={s}>{s}</option>
                          ))}
                        </select>
                      </Field>
                    )}
                  </div>
                  <datalist id="management-skus">
                    {skus
                      .filter((s) => s.sku.includes(operation.sku || ""))
                      .slice(0, 60)
                      .map((s) => (
                        <option key={s.sku} value={s.sku}>
                          {s.name}
                        </option>
                      ))}
                  </datalist>
                  <datalist id="management-locations">
                    {locations
                      .filter(
                        (l) =>
                          l.kind !== "transit" &&
                          (l.id.includes(operation.location_id || "") ||
                            l.id.includes(operation.destination || "")),
                      )
                      .slice(0, 100)
                      .map((l) => (
                        <option key={l.id} value={l.id}>
                          {l.name}
                        </option>
                      ))}
                  </datalist>
                  <button
                    disabled={!registered}
                    type="submit"
                    className="button primary"
                  >
                    Confirm operation
                  </button>
                </form>
              </Panel>
              <Panel
                title="Import opening stock"
                subtitle="CSV columns: sku,quantity,location_id. Optional: uom,lot,expiry,status,container. All rows are validated together; an identical batch is not posted twice."
              >
                <UploadButton
                  label="Import opening-stock CSV"
                  accept=".csv"
                  disabled={!registered}
                  onFile={(file) =>
                    void run("Import opening stock", async () => {
                      const raw = await file.text();
                      const r = await api(
                        `/api/manage/warehouses/${workspaceId}/opening-stock-csv`,
                        {
                          method: "POST",
                          body: JSON.stringify({
                            csv: raw,
                            actor: operation.actor,
                            reason: `Opening stock: ${file.name}`,
                          }),
                        },
                      );
                      setNotice(
                        `${r.imported} rows imported${r.duplicate ? " (already recorded)" : ""}.`,
                      );
                    })
                  }
                />
              </Panel>
            </>
          )}
          {tab === "Tasks & activity" && (
            <>
              <Panel
                title="Delivery, replenishment & relocation tasks"
                subtitle="Stock remains at its source while reserved. Pickup moves it into transit; delivery moves it into the destination. Failed tasks retain stock in transit until recovered."
              >
                <Table
                  label="Movement tasks"
                  rows={tasks}
                  searchable
                  sortable
                  columns={[
                    {
                      key: "id",
                      label: "Task ID",
                      render: (v: string) => (
                        <code className="task-id">{v}</code>
                      ),
                    },
                    {
                      key: "task_type",
                      label: "Purpose",
                      render: (v) => (
                        <Badge
                          tone={
                            v === "replenishment"
                              ? "green"
                              : v === "delivery"
                                ? "blue"
                                : ""
                          }
                        >
                          {taskTypes[v as keyof typeof taskTypes]?.label ??
                            "Relocation"}
                        </Badge>
                      ),
                    },
                    { key: "sku", label: "SKU" },
                    { key: "source", label: "From" },
                    { key: "destination", label: "To" },
                    {
                      key: "quantity",
                      label: "Quantity",
                      render: (_: unknown, r: Row) =>
                        exactQuantity(r.quantity_micro),
                    },
                    { key: "status", label: "Status" },
                    { key: "reference", label: "Reference" },
                    {
                      key: "_actions",
                      sortable: false,
                      label: "Confirm action",
                      render: (_: string, r: Row) => (
                        <div className="management-actions">
                          {(r.status === "reserved"
                            ? [
                                ["task_pickup", "Confirm pickup"],
                                ["task_cancel", "Cancel"],
                              ]
                            : r.status === "in_transit"
                              ? [
                                  [
                                    "task_complete",
                                    r.task_type === "replenishment"
                                      ? "Confirm replenishment"
                                      : "Confirm delivery",
                                  ],
                                  ["task_fail", "Report interruption"],
                                ]
                              : r.status === "failed"
                                ? [
                                    [
                                      "task_complete",
                                      r.task_type === "replenishment"
                                        ? "Confirm recovered replenishment"
                                        : "Confirm recovered delivery",
                                    ],
                                  ]
                                : []
                          ).map(([kind, label]) => (
                            <button
                              className="button button-small"
                              key={kind}
                              onClick={() =>
                                void run(label, () =>
                                  post(`${warehousePath}/inventory`, {
                                    kind,
                                    task_id: r.id,
                                    request_id: crypto.randomUUID(),
                                    actor: operation.actor,
                                    reason: `${label}: ${r.reference}`,
                                  }),
                                )
                              }
                            >
                              {label}
                            </button>
                          ))}
                        </div>
                      ),
                    },
                  ]}
                />
              </Panel>
              <Panel title="Outbound reservations">
                <Table
                  label="Outbound reservations"
                  rows={reservations}
                  searchable
                  sortable
                  columns={[
                    { key: "sku", label: "SKU" },
                    { key: "location_id", label: "Location" },
                    {
                      key: "quantity",
                      label: "Quantity",
                      render: (_: unknown, r: Row) =>
                        exactQuantity(r.quantity_micro),
                    },
                    { key: "reference", label: "Order/reference" },
                    { key: "status", label: "Status" },
                  ]}
                />
              </Panel>
              <Panel
                title="Inventory transaction history"
                subtitle="Immutable events, including the operator and reason. Corrections are posted as new transactions."
              >
                <Table
                  label="Inventory events"
                  rows={events}
                  searchable
                  sortable
                  columns={[
                    { key: "sequence", label: "Sequence" },
                    { key: "created_at", label: "Time (UTC)" },
                    { key: "kind", label: "Operation" },
                    {
                      key: "task_id",
                      label: "Task ID",
                      render: (v) =>
                        v ? <code className="task-id">{v}</code> : "—",
                    },
                    { key: "actor", label: "Operator" },
                    { key: "reason", label: "Reason" },
                    {
                      key: "lines",
                      label: "Stock changes",
                      render: (lines: Row[]) =>
                        lines
                          .map(
                            (l) =>
                              `${l.position_id.slice(0, 8)}: ${l.quantity_delta > 0 ? "+" : ""}${l.quantity_delta} (${l.reserved_delta > 0 ? "+" : ""}${l.reserved_delta} reserved)`,
                          )
                          .join("; "),
                    },
                  ]}
                />
                <button
                  className="button button-small"
                  disabled={eventsExhausted || !events.length}
                  onClick={() =>
                    void run(
                      "Load earlier transactions",
                      async () => {
                        const rows = await api(
                          `/api/manage/warehouses/${workspaceId}/events?before=${events.at(-1)?.sequence || 0}`,
                        );
                        setEvents((previous) => [...previous, ...rows]);
                        setEventsExhausted(rows.length < 200);
                      },
                      false,
                    )
                  }
                >
                  Load earlier transactions
                </button>
                <button
                  className="button button-small"
                  disabled={!events.length}
                  onClick={() => download("inventory-events.json", events)}
                >
                  Export displayed events
                </button>
              </Panel>
            </>
          )}
          {tab === "Publish" && (
            <>
              <Panel
                title="Publish a warehouse release"
                subtitle="A release freezes warehouse geometry, equipment, SKU data, stock targets, slotting and the current inventory snapshot. Publication validates the saved layout again."
              >
                <ol className="management-sequence">
                  <li>Finish warehouse, demand and stock setup.</li>
                  <li>Generate and review the slotting layout.</li>
                  <li>Register or synchronize the warehouse structure.</li>
                  <li>Publish a named release for external systems.</li>
                </ol>
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    void run("Publish warehouse release", async () => {
                      const revision = await save();
                      await post(`${warehousePath}/publish`, {
                        name: releaseName,
                        expected_revision: revision,
                      });
                      setReleaseName("");
                    });
                  }}
                >
                  <div className="management-fields">
                    <Field label="Release name">
                      <input
                        required
                        value={releaseName}
                        placeholder="For example: AMR operating configuration v1"
                        onChange={(e) => setReleaseName(e.target.value)}
                      />
                    </Field>
                  </div>
                  <button
                    className="button primary"
                    disabled={!registered}
                    type="submit"
                  >
                    <ShieldCheck size={16} />
                    Validate and publish
                  </button>
                </form>
                <p className="management-hint">
                  Publishing preserves operational stock. Consumers should pin
                  the returned release ID to retrieve matching versions of all
                  configuration data.
                </p>
              </Panel>
              <Panel title="Published releases">
                <Table
                  label="Published releases"
                  rows={releases}
                  sortable
                  columns={[
                    { key: "version", label: "Version" },
                    { key: "name", label: "Name" },
                    { key: "published_at", label: "Published (UTC)" },
                    {
                      key: "checksum",
                      label: "SHA-256",
                      render: (v: string) => (
                        <code title={v}>{v.slice(0, 16)}…</code>
                      ),
                    },
                    {
                      key: "_actions",
                      sortable: false,
                      label: "Download",
                      render: (id: string, r: Row) => (
                        <button
                          className="button button-small"
                          onClick={() =>
                            void run("Download release", async () =>
                              download(
                                `warehouse-release-v${r.version}.json`,
                                await api(`/api/manage/releases/${r.id}`),
                              ),
                            )
                          }
                        >
                          <Download size={14} />
                          JSON bundle
                        </button>
                      ),
                    },
                  ]}
                />
              </Panel>
            </>
          )}
          {tab === "Integration" && (
            <>
              <Panel
                title="Connect an external system"
                subtitle="Integration endpoints require a Bearer API key. Published releases provide fixed configuration; live inventory endpoints provide current stock and operational events."
              >
                <div className="management-actions">
                  <a
                    className="button"
                    href="/api/openapi.json"
                    target="_blank"
                    rel="noreferrer"
                  >
                    Open API specification
                  </a>
                  <button
                    className="button"
                    onClick={() =>
                      void run("Copy API specification", async () => {
                        await navigator.clipboard.writeText(
                          location.origin + "/api/openapi.json",
                        );
                      })
                    }
                  >
                    <Copy size={14} />
                    Copy specification URL
                  </button>
                </div>
                <pre className="integration-example">{`GET /api/v1/warehouses\nGET /api/v1/warehouses/${workspaceId}/published\nGET /api/v1/releases/{releaseId}/warehouse\nGET /api/v1/releases/{releaseId}/slotting\nGET /api/v1/warehouses/${workspaceId}/inventory\nGET /api/v1/warehouses/${workspaceId}/events?after=0\n\nAuthorization: Bearer YOUR_API_KEY`}</pre>
                <p className="management-hint">
                  Read keys retrieve configuration and stock. Operate keys can
                  additionally post inventory transactions and confirm
                  relocation tasks. Use the local endpoint or a trusted SSH
                  tunnel.
                </p>
              </Panel>
              <Panel
                title="Integration API keys"
                subtitle="Secrets are shown once and stored as hashes. Revoking a key immediately prevents subsequent requests."
              >
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    void run("Create integration key", async () => {
                      const r = await post("keys", keyDraft);
                      setNewToken(r.token);
                      setKeyDraft({ name: "", scope: "read" });
                    });
                  }}
                >
                  <div className="management-fields">
                    <Field label="Integration name">
                      <input
                        required
                        value={keyDraft.name}
                        onChange={(e) =>
                          setKeyDraft({ ...keyDraft, name: e.target.value })
                        }
                      />
                    </Field>
                    <Field label="Access">
                      <select
                        value={keyDraft.scope}
                        onChange={(e) =>
                          setKeyDraft({ ...keyDraft, scope: e.target.value })
                        }
                      >
                        <option value="read">
                          Read configuration and inventory
                        </option>
                        <option value="operate">
                          Read and post inventory operations
                        </option>
                      </select>
                    </Field>
                  </div>
                  <button type="submit" className="button primary">
                    Create API key
                  </button>
                </form>
                {newToken && (
                  <div className="token-reveal" role="status">
                    <strong>
                      Copy this key now. It will not be shown again.
                    </strong>
                    <code>{newToken}</code>
                    <button
                      className="button button-small"
                      onClick={() =>
                        void run("Copy API key", () =>
                          navigator.clipboard.writeText(newToken),
                        )
                      }
                    >
                      <Copy size={14} />
                      Copy key
                    </button>
                    <button
                      className="button button-small"
                      onClick={() => setNewToken("")}
                    >
                      Hide key
                    </button>
                  </div>
                )}
                <Table
                  label="Integration keys"
                  rows={keys}
                  sortable
                  columns={[
                    { key: "name", label: "Integration" },
                    { key: "scope", label: "Access" },
                    { key: "created_at", label: "Created" },
                    {
                      key: "revoked_at",
                      label: "Revoked",
                      render: (v) => v || "Active",
                    },
                    {
                      key: "_actions",
                      sortable: false,
                      label: "Action",
                      render: (id: string, r: Row) => (
                        <button
                          className="button button-small"
                          disabled={!!r.revoked_at}
                          onClick={() =>
                            void run("Revoke API key", () =>
                              api(`/api/manage/keys/${r.id}`, {
                                method: "DELETE",
                              }),
                            )
                          }
                        >
                          Revoke
                        </button>
                      ),
                    },
                  ]}
                />
              </Panel>
            </>
          )}
        </div>
      </fieldset>
    </div>
  );
}

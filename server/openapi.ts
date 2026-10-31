const response = {
  description: "Successful JSON response",
  content: { "application/json": { schema: { type: "object" } } },
};
const errorResponses = Object.fromEntries(
  [400, 401, 403, 404, 409].map((code) => [
    code,
    {
      description: (
        {
          400: "Invalid input",
          401: "API key required",
          403: "Insufficient scope",
          404: "Record not found",
          409: "Stock, revision or idempotency conflict",
        } as any
      )[code],
      content: {
        "application/json": {
          schema: {
            type: "object",
            required: ["error"],
            properties: { error: { type: "string" } },
          },
        },
      },
    },
  ]),
);
const pathId = (name: string) => ({
  name,
  in: "path",
  required: true,
  schema: { type: "string" },
});
const read = (summary: string, parameters: any[] = []) => ({
  summary,
  security: [{ bearerAuth: [] }],
  parameters,
  responses: { "200": response, ...errorResponses },
});
export const openapi: Record<string, any> = {
  openapi: "3.1.0",
  info: {
    title: "Warehouse Configuration and Inventory API",
    version: "1.0.0",
    description:
      "Published releases are immutable snapshots with metre, kilogram and SKU base-UOM units. Live inventory is separate. Integration keys are read or operate scoped. Administer keys through the local web UI. Use an SSH tunnel for remote access. ASRS replay uses a separate simulation ledger; this API does not execute machine control.",
  },
  servers: [{ url: "/" }],
  components: {
    securitySchemes: { bearerAuth: { type: "http", scheme: "bearer" } },
    schemas: {
      InventoryOperation: {
        type: "object",
        required: ["request_id", "kind", "reason"],
        properties: {
          request_id: {
            type: "string",
            maxLength: 120,
            description:
              "Unique idempotency key; retry the identical body with this key",
          },
          kind: {
            type: "string",
            enum: [
              "opening",
              "receipt",
              "return",
              "adjustment",
              "transfer",
              "status",
              "reserve",
              "reserve_sku",
              "dispatch",
              "cancel_reservation",
              "task_create",
              "task_pickup",
              "task_complete",
              "task_cancel",
              "task_fail",
              "task_redirect",
              "container_transfer",
            ],
          },
          reason: { type: "string", maxLength: 500 },
          sku: { type: "string" },
          quantity: {
            type: "string",
            pattern: "^-?[0-9]+(\\.[0-9]{1,6})?$",
            description: "Exact quantity in uom; adjustment may be negative",
          },
          uom: { type: "string" },
          location_id: { type: "string" },
          destination: { type: "string" },
          position_id: { type: "string" },
          reservation_id: { type: "string" },
          task_id: { type: "string" },
          task_type: {
            enum: ["delivery", "replenishment", "relocation"],
            description:
              "Explicit task_create purpose generates DEL-, RPL- or MOV- IDs. Omit to retain legacy relocation UUIDs.",
          },
          lot: { type: "string" },
          expiry: { type: "string", format: "date" },
          container: { type: "string" },
          status: { enum: ["available", "quarantine", "damaged", "blocked"] },
          expected_revision: { type: "integer" },
        },
      },
    },
  },
  paths: {
    "/api/v1/warehouses": { get: read("List configured warehouses") },
    "/api/v1/skus": {
      get: read("Read SKU master and UOM conversions", [
        { name: "q", in: "query", schema: { type: "string" } },
      ]),
    },
    "/api/v1/partners": { get: read("Read stores and suppliers") },
    "/api/v1/warehouses/{warehouseId}/published": {
      get: read(
        "Get the current published release; pin its release ID for consistent subsequent requests",
        [pathId("warehouseId")],
      ),
    },
    "/api/v1/releases/{releaseId}": {
      get: read(
        "Get a complete immutable release and SHA-256 checksum of recursive key-sorted compact UTF-8 JSON (excluding the checksum property)",
        [pathId("releaseId")],
      ),
    },
    ...Object.fromEntries(
      ["warehouse", "slotting", "inventory", "skus"].map((part) => [
        `/api/v1/releases/{releaseId}/${part}`,
        {
          get: read(`Get ${part} from the pinned release`, [
            pathId("releaseId"),
          ]),
        },
      ]),
    ),
    ...Object.fromEntries(
      [
        "locations",
        "equipment",
        "containers",
        "tasks",
        "reservations",
        "releases",
      ].map((part) => [
        `/api/v1/warehouses/{warehouseId}/${part}`,
        { get: read(`List ${part}`, [pathId("warehouseId")]) },
      ]),
    ),
    "/api/v1/warehouses/{warehouseId}/inventory": {
      get: read(
        "Read current operational inventory; quantities are distinct from planning targets",
        [
          pathId("warehouseId"),
          { name: "q", in: "query", schema: { type: "string" } },
        ],
      ),
      post: {
        summary:
          "Post an atomic inventory operation (operate key required); actor is recorded from the API key",
        parameters: [pathId("warehouseId")],
        security: [{ bearerAuth: [] }],
        requestBody: {
          required: true,
          content: {
            "application/json": {
              schema: { $ref: "#/components/schemas/InventoryOperation" },
            },
          },
        },
        responses: { "201": response, ...errorResponses },
      },
    },
    "/api/v1/warehouses/{warehouseId}/events": {
      get: read(
        "Read immutable stock events in ascending sequence; pass last sequence as after",
        [
          pathId("warehouseId"),
          {
            name: "after",
            in: "query",
            schema: { type: "integer", minimum: 0 },
          },
          {
            name: "limit",
            in: "query",
            schema: { type: "integer", minimum: 1, maximum: 1000 },
          },
        ],
      ),
    },
  },
};

const string = { type: "string" },
  number = { type: "number" },
  integer = { type: "integer" },
  object = { type: "object", additionalProperties: true };
const reference = (name: string) => ({ $ref: `#/components/schemas/${name}` });
const array = (items: any) => ({ type: "array", items });
const record = (properties: Record<string, any>, required: string[] = []) => ({
  type: "object",
  properties,
  required,
  additionalProperties: true,
});
Object.assign(openapi.components.schemas, {
  WarehouseSummary: record(
    {
      id: string,
      name: string,
      version_hash: string,
      updated_at: { type: "string", format: "date-time" },
    },
    ["id", "name", "version_hash"],
  ),
  SKU: record(
    {
      sku: string,
      name: string,
      base_uom: string,
      active: integer,
      revision: integer,
      attributes: object,
      uoms: array(record({ uom: string, factor: number })),
    },
    ["sku", "base_uom", "attributes", "uoms"],
  ),
  InventoryPosition: record(
    {
      id: string,
      warehouse_id: string,
      sku: string,
      base_uom: string,
      location_id: string,
      lot: string,
      expiry: string,
      status: {
        type: "string",
        enum: ["available", "quarantine", "damaged", "blocked"],
      },
      container: string,
      quantity_micro: integer,
      reserved_micro: integer,
      quantity_exact: string,
      reserved_exact: string,
      available_exact: string,
      quantity: number,
      reserved: number,
      available: number,
      revision: integer,
      physical_check: {
        type: "string",
        enum: ["UNVERIFIED", "ITEM_CHECKED", "FAILED"],
      },
      physical_reason: string,
    },
    [
      "id",
      "sku",
      "base_uom",
      "location_id",
      "quantity_micro",
      "reserved_micro",
      "quantity_exact",
      "available_exact",
    ],
  ),
  Location: record(
    {
      warehouse_id: string,
      id: string,
      kind: string,
      rack_id: { type: ["string", "null"] },
      level: { type: ["integer", "null"] },
      slot: { type: ["integer", "null"] },
      details: object,
    },
    ["warehouse_id", "id", "kind", "details"],
  ),
  Warehouse: record(
    {
      id: string,
      name: string,
      version_hash: string,
      time_zone: string,
      project: object,
      geometry: object,
      locations: array(reference("Location")),
      equipment: array(object),
      containers: array(object),
    },
    ["id", "project", "geometry", "locations"],
  ),
  Slotting: record(
    {
      schema: string,
      strategy: string,
      project: object,
      settings: object,
      assignments: array(object),
      validation: object,
      summary: object,
    },
    ["schema", "assignments", "validation"],
  ),
  Release: record(
    {
      schema: { const: "layout_generator_release/v1" },
      api_version: { const: "v1" },
      id: string,
      warehouse_id: string,
      version: integer,
      name: string,
      published_at: { type: "string", format: "date-time" },
      checksum: string,
      units: object,
      warehouse: reference("Warehouse"),
      skus: array(reference("SKU")),
      partners: array(object),
      slotting: reference("Slotting"),
      inventory: array(reference("InventoryPosition")),
      inventory_event_sequence: integer,
      planning_inputs: object,
      validation: object,
    },
    [
      "id",
      "warehouse_id",
      "version",
      "checksum",
      "warehouse",
      "slotting",
      "inventory",
      "inventory_event_sequence",
    ],
  ),
  StockEvent: record(
    {
      sequence: integer,
      id: string,
      request_id: string,
      kind: string,
      actor: string,
      reason: string,
      created_at: { type: "string", format: "date-time" },
      result: object,
      lines: array(
        record(
          {
            position_id: string,
            sku: string,
            location_id: string,
            base_uom: string,
            lot: string,
            expiry: string,
            status: string,
            container: string,
            quantity_delta_micro: integer,
            reserved_delta_micro: integer,
            quantity_delta: number,
            reserved_delta: number,
          },
          [
            "position_id",
            "sku",
            "location_id",
            "quantity_delta_micro",
            "reserved_delta_micro",
          ],
        ),
      ),
    },
    ["sequence", "kind", "actor", "lines"],
  ),
  InventorySnapshot: record(
    {
      warehouse_id: string,
      inventory_event_sequence: integer,
      inventory: array(reference("InventoryPosition")),
    },
    ["warehouse_id", "inventory_event_sequence", "inventory"],
  ),
  Task: record(
    {
      id: string,
      warehouse_id: string,
      source_position_id: string,
      destination: string,
      container: string,
      status: {
        type: "string",
        enum: ["reserved", "in_transit", "failed", "cancelled", "completed"],
      },
      reference: string,
      items: array(
        record({
          position_id: string,
          sku: string,
          quantity_micro: integer,
          reservation_id: string,
          transit_position_id: { type: ["string", "null"] },
        }),
      ),
    },
    ["id", "status", "items"],
  ),
  OrderLine: record(
    {
      dataset_id: string,
      source_row: integer,
      date: { type: "string", format: "date" },
      source_time: string,
      sku: string,
      store: string,
      quantity_ea: number,
      conversion: number,
      uom: string,
      order_reference: string,
    },
    ["dataset_id", "source_row", "date", "sku", "quantity_ea"],
  ),
});
const resultSchema = (schema: any) => ({
  description: "Successful JSON response",
  content: { "application/json": { schema } },
});
const setRead = (path: string, schema: any) => {
  openapi.paths[path].get.responses["200"] = resultSchema(schema);
};
setRead("/api/v1/warehouses", array(reference("WarehouseSummary")));
setRead("/api/v1/skus", array(reference("SKU")));
setRead("/api/v1/partners", array(object));
setRead("/api/v1/warehouses/{warehouseId}/published", reference("Release"));
setRead("/api/v1/releases/{releaseId}", reference("Release"));
setRead(
  "/api/v1/warehouses/{warehouseId}/inventory",
  array(reference("InventoryPosition")),
);
setRead(
  "/api/v1/warehouses/{warehouseId}/events",
  array(reference("StockEvent")),
);
setRead("/api/v1/warehouses/{warehouseId}/tasks", array(reference("Task")));
for (const [part, schema] of Object.entries({
  warehouse: reference("Warehouse"),
  slotting: reference("Slotting"),
  inventory: array(reference("InventoryPosition")),
  skus: array(reference("SKU")),
}))
  setRead(
    `/api/v1/releases/{releaseId}/${part}`,
    record(
      {
        release_id: string,
        version: integer,
        checksum: string,
        units: object,
        data: schema,
      },
      ["release_id", "version", "data"],
    ),
  );
for (const part of [
  "locations",
  "equipment",
  "containers",
  "reservations",
  "releases",
])
  setRead(
    `/api/v1/warehouses/{warehouseId}/${part}`,
    array(part === "locations" ? reference("Location") : object),
  );
openapi.paths["/api/v1/warehouses/{warehouseId}/inventory-snapshot"] = {
  get: {
    ...read("Get a consistent inventory snapshot and exact event-feed cursor", [
      pathId("warehouseId"),
    ]),
    responses: {
      "200": resultSchema(reference("InventorySnapshot")),
      ...errorResponses,
    },
  },
};
openapi.paths["/api/v1/datasets"] = {
  get: {
    ...read("List historical order datasets and retained-line availability"),
    responses: { "200": resultSchema(array(object)), ...errorResponses },
  },
};
openapi.paths["/api/v1/datasets/{datasetId}/calendar"] = {
  get: {
    ...read("Read observed input dates and months only", [pathId("datasetId")]),
    responses: {
      "200": resultSchema(
        record(
          {
            dataset_id: string,
            raw_count: integer,
            dates: array(string),
            months: array(string),
          },
          ["dataset_id", "raw_count", "dates", "months"],
        ),
      ),
      ...errorResponses,
    },
  },
};
openapi.paths["/api/v1/datasets/{datasetId}/line-feed"] = {
  get: {
    ...read(
      "Read retained order lines using a stable source-row cursor; no offset scans",
      [
        pathId("datasetId"),
        ...["after", "limit", "start", "end"].map((name) => ({
          name,
          in: "query",
          schema: ["after", "limit"].includes(name)
            ? {
                type: "integer",
                minimum: name === "after" ? 0 : 1,
                ...(name === "limit"
                  ? { maximum: 5000, default: 5000 }
                  : { default: 0 }),
              }
            : { type: "string", format: "date" },
        })),
      ],
    ),
    responses: {
      "200": resultSchema(
        record(
          {
            rows: array(reference("OrderLine")),
            next_after: integer,
            done: { type: "boolean" },
          },
          ["rows", "next_after", "done"],
        ),
      ),
      ...errorResponses,
    },
  },
};
for (const kind of ["lines", "daily", "baskets"]) {
  const parameters = [
    pathId("datasetId"),
    ...["sku", "start", "end", "limit", "offset"].map((name) => ({
      name,
      in: "query",
      schema: ["limit", "offset"].includes(name)
        ? {
            type: "integer",
            minimum: name === "limit" ? 1 : 0,
            ...(name === "limit" ? { maximum: 500, default: 100 } : {}),
          }
        : { type: "string" },
    })),
  ];
  openapi.paths[`/api/v1/datasets/{datasetId}/${kind}`] = {
    get: {
      ...read(
        `Read paginated ${kind}; original clock time is source local without an inferred timezone`,
        parameters,
      ),
      responses: {
        "200": resultSchema(
          record(
            {
              rows: array(kind === "lines" ? reference("OrderLine") : object),
              total: integer,
              limit: integer,
              offset: integer,
            },
            ["rows", "total", "limit", "offset"],
          ),
        ),
        ...errorResponses,
      },
    },
  };
}
openapi.components.schemas.InventoryOperation.oneOf = [
  {
    properties: { kind: { enum: ["opening", "receipt", "return"] } },
    required: ["sku", "quantity", "location_id"],
  },
  {
    properties: { kind: { enum: ["adjustment", "reserve"] } },
    required: ["position_id", "quantity"],
  },
  {
    properties: { kind: { const: "reserve_sku" } },
    required: ["sku", "quantity"],
  },
  {
    properties: { kind: { enum: ["transfer", "task_create"] } },
    required: ["position_id", "quantity", "destination"],
  },
  {
    properties: { kind: { const: "container_transfer" } },
    required: ["container", "destination"],
  },
  {
    properties: { kind: { const: "status" } },
    required: ["position_id", "quantity", "status"],
  },
  {
    properties: { kind: { enum: ["dispatch", "cancel_reservation"] } },
    required: ["reservation_id"],
  },
  {
    properties: {
      kind: {
        enum: ["task_pickup", "task_complete", "task_cancel", "task_fail"],
      },
    },
    required: ["task_id"],
  },
  {
    properties: { kind: { const: "task_redirect" } },
    required: ["task_id", "destination"],
  },
];

import test from "node:test";
import assert from "node:assert/strict";
import { WarehouseDatabase } from "../server/database.js";
import {
  incompleteSkus,
  applySkuAttributes,
  preparationKey,
} from "../shared/preparation.js";
import { defaultSettings, mergeRows } from "../shared/model.js";
import { native, physical, project } from "./fixtures.js";

test("completeness uses effective requirements; corrections replace imported duplicates and invalidate review", () => {
  const rows = [
    {
      sku: "A",
      pick_frequency: 2,
      velocity_class: "A",
      sku_requirements: { ...physical, max_item_weight: 0 },
      req_weight: 0,
    },
  ];
  assert.deepEqual(incompleteSkus(rows, {})[0].missing, ["max_item_weight"]);
  const attrs = { A: { ...physical, max_item_weight: 2 } };
  const corrected = applySkuAttributes(rows, attrs);
  assert.equal(incompleteSkus(corrected, attrs).length, 0);
  assert.equal(
    mergeRows(corrected, [], attrs)[0].sku_requirements.max_item_weight,
    2,
  );
  assert.equal(corrected[0].req_weight, 2);
  assert.notEqual(
    preparationKey(rows, {}, defaultSettings),
    preparationKey(corrected, attrs, defaultSettings),
  );
  assert.notEqual(
    preparationKey(rows, {}, defaultSettings),
    preparationKey(rows, {}, { ...defaultSettings, start_date: "2023-01-01" }),
  );
});

test("saving SKU preparation is atomic, preserves master UOMs and guards concurrent changes", () => {
  const db = new WarehouseDatabase(":memory:");
  try {
    const id = db.createWorkspace().id;
    db.saveWorkspace(id, {
      name: "Preparation",
      project: project(),
      velocity: [
        {
          sku: "A",
          pick_frequency: 1,
          velocity_class: "A",
          sku_requirements: { ...physical, max_item_weight: 0 },
          req_max_item_weight: 0,
        },
      ],
      stock: [],
      attributes: {},
      settings: defaultSettings,
      result: { old: true },
      _save_revision: 1,
    });
    const master = db.saveSku({
      sku: "A",
      name: "Keep name",
      base_uom: "EA",
      uoms: [{ uom: "BOX", factor: "12" }],
      attributes: { chilled: true },
    });
    assert.ok(master);
    const request = {
      sku: "A",
      values: { ...physical, max_item_weight: 2 },
      master_revision: master.revision,
      workspace_revision: 1,
    };
    const updated = db.completePlanningSku(id, request);
    assert.equal(updated._save_revision, 2);
    assert.equal(updated.stockStale, true);
    assert.equal(updated.result, null);
    assert.equal(
      mergeRows(updated.velocity, [], updated.attributes)[0].sku_requirements
        .max_item_weight,
      2,
    );
    const saved = db.listSkus("A")[0];
    assert.equal(saved.name, "Keep name");
    assert.equal(saved.attributes.chilled, true);
    assert.equal(saved.uoms.find((u: any) => u.uom === "BOX").factor, 12);
    assert.throws(
      () => db.completePlanningSku(id, request),
      /Workspace changed/,
    );
    assert.throws(
      () => db.completePlanningSku(id, { ...request, workspace_revision: 2 }),
      /Record changed/,
    );
    assert.equal(db.getWorkspace(id)._save_revision, 2);
    assert.equal(db.listSkus("A")[0].revision, saved.revision);
    assert.throws(
      () =>
        db.completePlanningSku(id, {
          ...request,
          workspace_revision: 2,
          master_revision: saved.revision,
          values: { max_item_weight: -1 },
        }),
      /positive/,
    );
    assert.equal(db.listSkus("A")[0].attributes.max_item_weight, 2);
    db.db.exec(
      "CREATE TRIGGER reject_workspace_save BEFORE UPDATE ON workspaces BEGIN SELECT RAISE(ABORT, 'Workspace write failed'); END;",
    );
    assert.throws(
      () =>
        db.completePlanningSku(id, {
          ...request,
          workspace_revision: 2,
          master_revision: saved.revision,
          values: { max_item_weight: 3 },
        }),
      /Workspace write failed/,
    );
    assert.equal(db.listSkus("A")[0].attributes.max_item_weight, 2);
    assert.equal(db.listSkus("A")[0].revision, saved.revision);
    assert.equal(db.getWorkspace(id)._save_revision, 2);
    db.db.exec("DROP TRIGGER reject_workspace_save");
    const state = db.getWorkspace(id);
    state.velocity.push({
      sku: "NEW",
      velocity_class: "C",
      pick_frequency: 1,
      req_chilled: "no",
      req_tablet: "yes",
    });
    db.saveWorkspace(id, { ...state, _save_revision: 3 });
    const created = db.completePlanningSku(id, {
      sku: "NEW",
      values: physical,
      workspace_revision: 3,
      master_revision: null,
    });
    assert.equal(created._save_revision, 4);
    assert.equal(db.listSkus("NEW")[0].attributes.chilled, false);
    assert.equal(db.listSkus("NEW")[0].attributes.tablet, true);
    assert.equal(
      incompleteSkus(created.velocity, created.attributes).length,
      0,
    );
  } finally {
    db.close();
  }
});

test("affinity focuses on pairs outside the global top 100, deduplicates baskets and honors dates/thresholds", () => {
  const codes = Array.from(
    { length: 17 },
    (_, i) => `A${String(i).padStart(2, "0")}`,
  );
  const daily = codes.concat("Z", "ZZ").map((sku) => ({
    sku,
    date: "2023-01-01",
    quantity: 1,
    frequency: 1,
    conversion: 1,
    requirements: physical,
  }));
  const orders = {
    daily: [...daily, { ...daily[0], sku: "Z", date: "2023-01-02" }],
    baskets: [
      { date: "2023-01-01", store: "S", skus: [...codes, ...codes] },
      { date: "2023-01-01", store: "T", skus: ["Z", "ZZ", "Z"] },
      { date: "2023-01-02", store: "T", skus: ["Z"] },
    ],
  };
  const result = native({
    command: "analyze",
    orders,
    settings: { affinity_focus_sku: "Z" },
  });
  assert.equal(result.relationships.length, 100);
  assert.equal(
    result.relationships.some((r: any) => r.sku_a === "Z"),
    false,
  );
  assert.equal(result.affinity_graph.relationships.length, 1);
  assert.equal(result.affinity_graph.relationships[0].shared_store_days, 1);
  assert.ok(
    Math.abs(result.affinity_graph.relationships[0].cosine - 1 / Math.sqrt(2)) <
      1e-12,
  );
  const selected = native({
    command: "analyze",
    orders,
    settings: {
      start_date: "2023-01-01",
      end_date: "2023-01-01",
      affinity_focus_sku: "Z",
    },
  });
  assert.equal(selected.affinity_graph.relationships[0].cosine, 1);
  assert.equal(
    native({
      command: "analyze",
      orders,
      settings: { affinity_focus_sku: "Z", affinity_minimum_shared: 2 },
    }).affinity_graph.relationships.length,
    0,
  );
  assert.match(
    native(
      {
        command: "analyze",
        orders,
        settings: { affinity_focus_sku: "MISSING" },
      },
      true,
    ).error,
    /absent/,
  );
  assert.match(
    native(
      { command: "analyze", orders, settings: { affinity_neighbor_limit: 25 } },
      true,
    ).error,
    /neighbors/,
  );
  assert.match(
    native(
      {
        command: "analyze",
        orders,
        settings: { affinity_minimum_shared: 1.5 },
      },
      true,
    ).error,
    /shared/,
  );
  assert.equal(result.affinity_graph.nodes.length, 2);
});

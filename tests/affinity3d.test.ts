import test from "node:test";
import assert from "node:assert/strict";
import { affinityScene } from "../shared/affinity3d.js";
import { native, physical } from "./fixtures.js";

test("3D network includes actual neighbor-to-neighbor links and applies date/threshold filters", () => {
  const baskets = [
    { date: "2023-01-01", store: "S", skus: ["A", "B", "C"] },
    { date: "2023-01-02", store: "S", skus: ["A", "B"] },
    { date: "2023-01-03", store: "S", skus: ["A", "C"] },
  ];
  const orders = {
    baskets,
    daily: baskets.flatMap((b) =>
      b.skus.map((sku) => ({
        date: b.date,
        sku,
        quantity: 1,
        frequency: 1,
        conversion: 1,
        requirements: physical,
      })),
    ),
  };
  const full = native({
    command: "analyze",
    orders,
    settings: { affinity_focus_sku: "A" },
  }).affinity_graph;
  assert.equal(full.relationships.length, 2);
  assert.equal(full.network_relationship_count, 3);
  const bc = full.network_relationships.find(
    (e: any) => e.sku_a === "B" && e.sku_b === "C",
  );
  assert.equal(bc.shared_store_days, 1);
  assert.equal(bc.cosine, 0.5);
  const filtered = native({
    command: "analyze",
    orders,
    settings: { affinity_focus_sku: "A", affinity_minimum_shared: 2 },
  }).affinity_graph;
  assert.equal(filtered.network_relationship_count, 2);
  assert.equal(
    filtered.network_relationships.some(
      (e: any) => e.sku_a === "B" && e.sku_b === "C",
    ),
    false,
  );
  const oneDay = native({
    command: "analyze",
    orders,
    settings: {
      start_date: "2023-01-01",
      end_date: "2023-01-01",
      affinity_focus_sku: "A",
    },
  }).affinity_graph;
  assert.equal(oneDay.network_relationships.length, 3);
  assert.ok(oneDay.network_relationships.every((e: any) => e.cosine === 1));
});

test("network cap retains every focus link even when other pairs rank more strongly", () => {
  const codes = Array.from({ length: 40 }, (_, i) => `A${i}`);
  const baskets = [
    { date: "2023-01-01", store: "S", skus: [...codes, "Z"] },
    { date: "2023-01-02", store: "S", skus: codes },
  ];
  const orders = {
    baskets,
    daily: baskets.flatMap((b) =>
      b.skus.map((sku) => ({
        date: b.date,
        sku,
        quantity: 1,
        frequency: 1,
        conversion: 1,
        requirements: physical,
      })),
    ),
  };
  const graph = native({
    command: "analyze",
    orders,
    settings: { affinity_focus_sku: "Z", affinity_neighbor_limit: 96 },
  }).affinity_graph;
  assert.equal(graph.nodes.length, 41);
  assert.equal(graph.network_relationship_count, 820);
  assert.equal(graph.network_relationships.length, 600);
  for (const edge of graph.relationships)
    assert.ok(
      graph.network_relationships.some(
        (r: any) => r.sku_a === edge.sku_a && r.sku_b === edge.sku_b,
      ),
    );
});

test("3D force layout is deterministic, finite and volumetric; legacy focus graphs still work", () => {
  const graph = {
    focus_sku: "A",
    relationships: [
      { sku_a: "A", sku_b: "B", cosine: 0.8, shared_store_days: 4 },
      { sku_a: "A", sku_b: "C", cosine: 0.3, shared_store_days: 1 },
    ],
    network_relationships: [
      { sku_a: "A", sku_b: "B", cosine: 0.8, shared_store_days: 4 },
      { sku_a: "A", sku_b: "C", cosine: 0.3, shared_store_days: 1 },
      { sku_a: "B", sku_b: "C", cosine: 0.5, shared_store_days: 2 },
    ],
    nodes: [
      { sku: "A", velocity_class: "A", pick_frequency: 100 },
      { sku: "B", velocity_class: "B", pick_frequency: 25 },
      { sku: "C", velocity_class: "C", pick_frequency: 1 },
    ],
  };
  const scene = affinityScene(graph, []);
  assert.deepEqual(scene, affinityScene(graph, []));
  assert.equal(scene.links.length, 3);
  const a = scene.nodes.find((n) => n.sku === "A")!,
    b = scene.nodes.find((n) => n.sku === "B")!,
    c = scene.nodes.find((n) => n.sku === "C")!;
  assert.deepEqual([a.x, a.y, a.z], [0, 0, 0]);
  assert.ok(b.radius > c.radius);
  const refocused = affinityScene({ ...graph, focus_sku: "B" }, []);
  assert.ok(
    refocused.nodes.find((n) => n.sku === "A")!.radius >
      refocused.nodes.find((n) => n.sku === "B")!.radius,
  );
  assert.ok(scene.nodes.some((n) => Math.abs(n.z) > 1));
  assert.ok(
    scene.nodes.every((n) => [n.x, n.y, n.z, n.radius].every(Number.isFinite)),
  );
  assert.ok(Math.hypot(b.x, b.y, b.z) < Math.hypot(c.x, c.y, c.z));
  const legacy = affinityScene(
    { ...graph, network_relationships: undefined },
    [],
  );
  assert.equal(legacy.links.length, 2);
  assert.equal(
    affinityScene({ focus_sku: "ALONE", relationships: [] }, []).nodes.length,
    1,
  );
  assert.equal(
    affinityScene({ focus_sku: "", relationships: [] }, []).nodes.length,
    0,
  );
});

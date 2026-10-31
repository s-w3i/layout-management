import type { Row } from "./model.js";
export const affinityColors: Record<string, string> = {
  A: "#60a5fa",
  B: "#c4b5fd",
  C: "#fbbf24",
};
export type AffinityNode = {
  sku: string;
  velocity_class: string;
  pick_frequency: number;
  x: number;
  y: number;
  z: number;
  radius: number;
};
export type AffinityLink = {
  sku_a: string;
  sku_b: string;
  cosine: number;
  shared_store_days: number;
};
export type AffinityScene = {
  nodes: AffinityNode[];
  links: AffinityLink[];
  span: number;
};
// A deterministic, bounded force layout keeps stronger measured relationships closer.
export function affinityScene(graph: Row, velocity: Row[]): AffinityScene {
  const source: Row[] =
    graph.network_relationships || graph.relationships || [];
  const ids = new Set<string>([String(graph.focus_sku || "")]);
  for (const edge of graph.relationships || source) {
    ids.add(edge.sku_a);
    ids.add(edge.sku_b);
  }
  ids.delete("");
  const names = [...ids].slice(0, 97).sort();
  const data = new Map<string, Row>(
    [...(graph.nodes || []), ...velocity].map((r: Row) => [r.sku, r]),
  );
  const maximum = Math.max(
    1,
    ...names.map((sku) => Number(data.get(sku)?.pick_frequency) || 0),
  );
  const nodes: AffinityNode[] = names.map((sku, i) => {
    let hash = 2166136261;
    for (const char of sku)
      hash = Math.imul(hash ^ char.charCodeAt(0), 16777619);
    const angle = i * 2.399963229728653,
      height = 1 - (2 * (i + 0.5)) / Math.max(names.length, 1),
      ring = Math.sqrt(1 - height * height),
      r = 45 + ((hash >>> 0) % 30);
    const row = data.get(sku),
      frequency = Math.max(0, Number(row?.pick_frequency) || 0);
    return {
      sku,
      velocity_class: row?.velocity_class || "?",
      pick_frequency: frequency,
      x: sku === graph.focus_sku ? 0 : Math.cos(angle) * ring * r,
      y: sku === graph.focus_sku ? 0 : height * r,
      z: sku === graph.focus_sku ? 0 : Math.sin(angle) * ring * r,
      radius: 1.8 + 2.2 * Math.sqrt(frequency / maximum),
    };
  });
  const indices = new Map(nodes.map((n, i) => [n.sku, i])),
    seen = new Set<string>();
  const links: AffinityLink[] = [];
  for (const row of source) {
    if (
      !indices.has(row.sku_a) ||
      !indices.has(row.sku_b) ||
      row.sku_a === row.sku_b
    )
      continue;
    const key = [row.sku_a, row.sku_b].sort().join("\0");
    if (
      seen.has(key) ||
      !Number.isFinite(row.cosine) ||
      row.cosine <= 0 ||
      row.cosine > 1
    )
      continue;
    seen.add(key);
    links.push({
      sku_a: row.sku_a,
      sku_b: row.sku_b,
      cosine: row.cosine,
      shared_store_days: row.shared_store_days,
    });
    if (links.length === 600) break;
  }
  const velocities = new Float64Array(nodes.length * 3),
    force = new Float64Array(nodes.length * 3);
  const springs = links.map((link) => ({
    a: indices.get(link.sku_a)!,
    b: indices.get(link.sku_b)!,
    rest: 24 + 38 * (1 - link.cosine),
    stiffness: 0.025 + 0.07 * link.cosine,
  }));
  for (let iteration = 0; iteration < 220; iteration++) {
    for (let i = 0; i < nodes.length; i++) {
      force[i * 3] = -nodes[i].x * 0.025;
      force[i * 3 + 1] = -nodes[i].y * 0.025;
      force[i * 3 + 2] = -nodes[i].z * 0.025;
    }
    for (let a = 0; a < nodes.length; a++)
      for (let b = a + 1; b < nodes.length; b++) {
        const x = nodes[a].x - nodes[b].x,
          y = nodes[a].y - nodes[b].y,
          z = nodes[a].z - nodes[b].z;
        const square = Math.max(4, x * x + y * y + z * z),
          strength = 1800 / (square * Math.sqrt(square)),
          i = a * 3,
          j = b * 3;
        force[i] += x * strength;
        force[i + 1] += y * strength;
        force[i + 2] += z * strength;
        force[j] -= x * strength;
        force[j + 1] -= y * strength;
        force[j + 2] -= z * strength;
      }
    for (const spring of springs) {
      const a = nodes[spring.a],
        b = nodes[spring.b],
        x = b.x - a.x,
        y = b.y - a.y,
        z = b.z - a.z;
      const distance = Math.max(0.1, Math.sqrt(x * x + y * y + z * z)),
        strength = ((distance - spring.rest) * spring.stiffness) / distance,
        i = spring.a * 3,
        j = spring.b * 3;
      force[i] += x * strength;
      force[i + 1] += y * strength;
      force[i + 2] += z * strength;
      force[j] -= x * strength;
      force[j + 1] -= y * strength;
      force[j + 2] -= z * strength;
    }
    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i];
      if (node.sku === graph.focus_sku) continue;
      for (let axis = 0; axis < 3; axis++) {
        const j = i * 3 + axis;
        velocities[j] =
          (velocities[j] + Math.max(-3, Math.min(3, force[j]))) * 0.76;
      }
      node.x += velocities[i * 3];
      node.y += velocities[i * 3 + 1];
      node.z += velocities[i * 3 + 2];
    }
  }
  return {
    nodes,
    links,
    span: Math.max(
      35,
      ...nodes.map((n) => Math.hypot(n.x, n.y, n.z) + n.radius),
    ),
  };
}

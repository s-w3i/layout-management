import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import {
  ArrowDown,
  ArrowLeft,
  ArrowRight,
  ArrowUp,
  Download,
  Focus,
  RotateCcw,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { affinityColors, affinityScene } from "../../shared/affinity3d";
import type { Row } from "../../shared/model";
import { download } from "./api";
import { Badge, Field, count } from "./components";
type Command =
  | "reset"
  | "focus"
  | "left"
  | "right"
  | "up"
  | "down"
  | "in"
  | "out";
export default function AffinityNetwork({
  graph,
  velocity,
  selected,
  onSelect,
  onExplore,
  busy,
}: {
  graph: Row;
  velocity: Row[];
  selected: string;
  onSelect: (sku: string) => void;
  onExplore: (sku: string) => void;
  busy: boolean;
}) {
  const model = useMemo(
    () => affinityScene(graph, velocity),
    [graph, velocity],
  );
  const [mode, setMode] = useState("3d"),
    [labels, setLabels] = useState(false),
    [failure, setFailure] = useState(""),
    [hover, setHover] = useState("");
  const host = useRef<HTMLDivElement>(null),
    svg = useRef<SVGSVGElement>(null);
  const actions = useRef<{
    command: (type: Command) => void;
    update: () => void;
    png: () => void;
  } | null>(null);
  const latest = useRef({ selected, labels, onSelect });
  latest.current = { selected, labels, onSelect };
  const selectedNode =
    model.nodes.find((n) => n.sku === selected) ||
    model.nodes.find((n) => n.sku === graph.focus_sku);
  const connected = model.links
    .filter(
      (e) => e.sku_a === selectedNode?.sku || e.sku_b === selectedNode?.sku,
    )
    .sort((a, b) => b.cosine - a.cosine);
  useEffect(() => {
    actions.current?.update();
  }, [selected, labels]);
  useEffect(() => {
    if (mode !== "3d" || !host.current) return;
    const mount = host.current;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    } catch {
      setFailure(
        "WebGL is unavailable. The 2D network and connection table remain available.",
      );
      setMode("2d");
      return;
    }
    setFailure("");
    renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
    renderer.setClearColor("#071126");
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    mount.appendChild(renderer.domElement);
    const canvas = renderer.domElement;
    canvas.tabIndex = 0;
    canvas.setAttribute("role", "img");
    canvas.setAttribute(
      "aria-label",
      "Interactive 3D SKU affinity network. Drag to orbit, scroll to zoom, right-drag to pan. Arrow keys rotate, plus and minus zoom, Home resets. Use Inspect SKU to select nodes with the keyboard.",
    );
    const scene = new THREE.Scene(),
      camera = new THREE.PerspectiveCamera(43, 1, 0.1, 2000),
      controls = new OrbitControls(camera, canvas);
    controls.enableDamping = false;
    controls.minDistance = 15;
    controls.maxDistance = model.span * 12;
    controls.screenSpacePanning = true;
    scene.add(new THREE.AmbientLight(0xffffff, 1.2));
    const light = new THREE.DirectionalLight(0xffffff, 2);
    light.position.set(30, 70, 100);
    scene.add(light);
    const resources: (THREE.BufferGeometry | THREE.Material | THREE.Texture)[] =
      [];
    const geometry = new THREE.SphereGeometry(1, 20, 16);
    resources.push(geometry);
    const haloCanvas = document.createElement("canvas");
    haloCanvas.width = 128;
    haloCanvas.height = 128;
    const ctx = haloCanvas.getContext("2d")!;
    const gradient = ctx.createRadialGradient(64, 64, 2, 64, 64, 64);
    gradient.addColorStop(0, "rgba(255,255,255,.95)");
    gradient.addColorStop(0.2, "rgba(255,255,255,.35)");
    gradient.addColorStop(1, "rgba(255,255,255,0)");
    ctx.fillStyle = gradient;
    ctx.fillRect(0, 0, 128, 128);
    const glowTexture = new THREE.CanvasTexture(haloCanvas);
    resources.push(glowTexture);
    const nodeObjects = new Map<
      string,
      { mesh: THREE.Mesh; halo: THREE.Sprite; label: THREE.Sprite }
    >();
    for (const node of model.nodes) {
      const color = affinityColors[node.velocity_class] || "#cbd5e1";
      const material = new THREE.MeshStandardMaterial({
        color,
        emissive: color,
        emissiveIntensity: 0.7,
        roughness: 0.3,
      });
      const mesh = new THREE.Mesh(geometry, material);
      mesh.position.set(node.x, node.y, node.z);
      mesh.scale.setScalar(node.radius);
      scene.add(mesh);
      resources.push(material);
      const glowMaterial = new THREE.SpriteMaterial({
        map: glowTexture,
        color,
        transparent: true,
        opacity: 0.5,
        blending: THREE.AdditiveBlending,
        depthWrite: false,
      });
      resources.push(glowMaterial);
      const halo = new THREE.Sprite(glowMaterial);
      halo.position.copy(mesh.position);
      halo.scale.setScalar(node.radius * 7);
      scene.add(halo);
      const labelCanvas = document.createElement("canvas");
      labelCanvas.width = 512;
      labelCanvas.height = 100;
      const context = labelCanvas.getContext("2d")!;
      context.fillStyle = "rgba(7,17,38,.85)";
      context.fillRect(0, 0, 512, 100);
      context.fillStyle = "#f8fafc";
      context.font = "600 52px sans-serif";
      context.textAlign = "center";
      context.textBaseline = "middle";
      context.fillText(`${node.sku} · ${node.velocity_class}`, 256, 50, 490);
      const texture = new THREE.CanvasTexture(labelCanvas);
      resources.push(texture);
      const labelMaterial = new THREE.SpriteMaterial({
        map: texture,
        transparent: true,
        depthTest: false,
        depthWrite: false,
      });
      resources.push(labelMaterial);
      const label = new THREE.Sprite(labelMaterial);
      label.position
        .copy(mesh.position)
        .add(new THREE.Vector3(0, node.radius + 4, 0));
      label.scale.set(23, 4.5, 1);
      scene.add(label);
      nodeObjects.set(node.sku, { mesh, halo, label });
    }
    const nodes = new Map(model.nodes.map((n) => [n.sku, n]));
    const paths = model.links.map((edge) => {
      const a = nodes.get(edge.sku_a)!,
        b = nodes.get(edge.sku_b)!;
      const start = new THREE.Vector3(a.x, a.y, a.z),
        end = new THREE.Vector3(b.x, b.y, b.z),
        middle = start.clone().add(end).multiplyScalar(0.5);
      const bend = end
        .clone()
        .sub(start)
        .cross(new THREE.Vector3(0.3, 1, 0.2))
        .normalize()
        .multiplyScalar(start.distanceTo(end) * 0.09);
      middle.add(bend);
      const curve = new THREE.QuadraticBezierCurve3(start, middle, end),
        points = curve.getPoints(10),
        vertices: number[] = [];
      for (let i = 0; i < points.length - 1; i++)
        vertices.push(...points[i].toArray(), ...points[i + 1].toArray());
      return vertices;
    });
    const positions = paths.flat(),
      colors: number[] = [];
    model.links.forEach((edge, i) => {
      const color = new THREE.Color("#7aaef8")
        .lerp(new THREE.Color("#c4b5fd"), edge.cosine)
        .multiplyScalar(0.3 + 0.7 * edge.cosine);
      for (let j = 0; j < paths[i].length / 3; j++)
        colors.push(color.r, color.g, color.b);
    });
    const lineGeometry = new THREE.BufferGeometry();
    lineGeometry.setAttribute(
      "position",
      new THREE.Float32BufferAttribute(positions, 3),
    );
    lineGeometry.setAttribute(
      "color",
      new THREE.Float32BufferAttribute(colors, 3),
    );
    resources.push(lineGeometry);
    const lineMaterial = new THREE.LineBasicMaterial({
      vertexColors: true,
      transparent: true,
      opacity: 0.46,
      depthWrite: false,
    });
    resources.push(lineMaterial);
    scene.add(new THREE.LineSegments(lineGeometry, lineMaterial));
    const highlightGeometry = new THREE.BufferGeometry(),
      highlightMaterial = new THREE.LineBasicMaterial({
        color: "#bcecff",
        transparent: true,
        opacity: 0.95,
        depthWrite: false,
      });
    highlightGeometry.setAttribute(
      "position",
      new THREE.Float32BufferAttribute(new Float32Array(positions.length), 3),
    );
    resources.push(highlightGeometry, highlightMaterial);
    scene.add(new THREE.LineSegments(highlightGeometry, highlightMaterial));
    let frame = 0,
      disposed = false,
      hovered = "",
      down: { x: number; y: number } | null = null;
    const render = () => {
      frame = 0;
      if (!disposed) {
        camera.updateMatrixWorld();
        const up = new THREE.Vector3(0, 1, 0).applyQuaternion(
          camera.quaternion,
        );
        for (const object of nodeObjects.values())
          if (object.label.visible) {
            const depth = -object.mesh.position
              .clone()
              .applyMatrix4(camera.matrixWorldInverse).z;
            const perPixel =
              (2 *
                Math.max(0.1, depth) *
                Math.tan(THREE.MathUtils.degToRad(camera.fov / 2))) /
              Math.max(1, mount.clientHeight);
            object.label.scale.set(133 * perPixel, 26 * perPixel, 1);
            object.label.position
              .copy(object.mesh.position)
              .addScaledVector(up, Number(object.mesh.scale.x) + 18 * perPixel);
          }
        renderer.render(scene, camera);
      }
    };
    const invalidate = () => {
      if (!frame && !disposed) frame = requestAnimationFrame(render);
    };
    const update = () => {
      const id = hovered || latest.current.selected;
      const points: number[] = [];
      model.links.forEach((edge, i) => {
        if (edge.sku_a === id || edge.sku_b === id) points.push(...paths[i]);
      });
      const highlightPositions = highlightGeometry.getAttribute(
        "position",
      ) as THREE.BufferAttribute;
      (highlightPositions.array as Float32Array).set(points);
      highlightPositions.needsUpdate = true;
      highlightGeometry.setDrawRange(0, points.length / 3);
      highlightGeometry.computeBoundingSphere();
      for (const node of model.nodes) {
        const object = nodeObjects.get(node.sku)!;
        const active = node.sku === id,
          related = model.links.some(
            (e) =>
              (e.sku_a === id && e.sku_b === node.sku) ||
              (e.sku_b === id && e.sku_a === node.sku),
          );
        object.mesh.scale.setScalar(node.radius * (active ? 1.28 : 1));
        object.halo.material.opacity = active ? 0.95 : related ? 0.6 : 0.27;
        object.halo.scale.setScalar(node.radius * (active ? 10 : 7));
        object.label.visible =
          latest.current.labels || active || node.sku === graph.focus_sku;
      }
      invalidate();
    };
    const fit = () => {
      controls.target.set(0, 0, 0);
      const distance =
        (model.span * 1.3) /
        Math.sin(THREE.MathUtils.degToRad(camera.fov / 2)) /
        Math.min(1, camera.aspect);
      camera.position.set(distance * 0.26, distance * 0.16, distance);
      camera.far = distance * 15;
      camera.updateProjectionMatrix();
      controls.maxDistance = Math.max(model.span * 12, distance * 2);
      controls.update();
      invalidate();
    };
    const command = (kind: Command) => {
      if (kind === "reset") {
        fit();
        return;
      }
      if (kind === "focus") {
        const node = nodes.get(latest.current.selected);
        if (node) {
          const target = new THREE.Vector3(node.x, node.y, node.z),
            delta = camera.position
              .clone()
              .sub(controls.target)
              .normalize()
              .multiplyScalar(Math.max(35, model.span * 0.75));
          controls.target.copy(target);
          camera.position.copy(target).add(delta);
        }
      } else {
        const delta = camera.position.clone().sub(controls.target);
        if (kind === "left" || kind === "right")
          delta.applyAxisAngle(
            new THREE.Vector3(0, 1, 0),
            kind === "left" ? 0.22 : -0.22,
          );
        else if (kind === "up" || kind === "down")
          delta.applyAxisAngle(
            new THREE.Vector3(1, 0, 0).applyQuaternion(camera.quaternion),
            kind === "up" ? 0.18 : -0.18,
          );
        else delta.multiplyScalar(kind === "in" ? 0.8 : 1.25);
        delta.clampLength(controls.minDistance, controls.maxDistance);
        camera.position.copy(controls.target).add(delta);
      }
      controls.update();
      invalidate();
    };
    const pick = (event: PointerEvent) => {
      const rect = canvas.getBoundingClientRect(),
        x = event.clientX - rect.left,
        y = event.clientY - rect.top;
      let chosen = "",
        distance = 22;
      for (const node of model.nodes) {
        const point = new THREE.Vector3(node.x, node.y, node.z).project(camera);
        if (point.z < -1 || point.z > 1) continue;
        const d = Math.hypot(
          ((point.x + 1) * rect.width) / 2 - x,
          ((-point.y + 1) * rect.height) / 2 - y,
        );
        if (d < distance) {
          chosen = node.sku;
          distance = d;
        }
      }
      return chosen;
    };
    const move = (event: PointerEvent) => {
      if (down) return;
      const id = pick(event);
      canvas.style.cursor = id ? "pointer" : "grab";
      if (id !== hovered) {
        hovered = id;
        setHover(id);
        update();
      }
    };
    const pointerDown = (event: PointerEvent) => {
      down = { x: event.clientX, y: event.clientY };
    };
    const pointerUp = (event: PointerEvent) => {
      if (
        down &&
        event.button === 0 &&
        Math.hypot(event.clientX - down.x, event.clientY - down.y) < 6
      ) {
        const id = pick(event);
        if (id) latest.current.onSelect(id);
      }
      down = null;
    };
    const leave = () => {
      down = null;
      hovered = "";
      setHover("");
      update();
    };
    const keyboard = (event: KeyboardEvent) => {
      const key: Record<string, Command> = {
        ArrowLeft: "left",
        ArrowRight: "right",
        ArrowUp: "up",
        ArrowDown: "down",
        "+": "in",
        "=": "in",
        "-": "out",
        Home: "reset",
        f: "focus",
        F: "focus",
      };
      if (key[event.key]) {
        event.preventDefault();
        command(key[event.key]);
      }
    };
    const resize = () => {
      if (!mount.clientWidth || !mount.clientHeight) return;
      renderer.setSize(mount.clientWidth, mount.clientHeight);
      const aspect = mount.clientWidth / mount.clientHeight;
      camera.position
        .sub(controls.target)
        .multiplyScalar(Math.min(1, camera.aspect) / Math.min(1, aspect))
        .add(controls.target);
      camera.aspect = aspect;
      camera.updateProjectionMatrix();
      invalidate();
    };
    const lost = (event: Event) => {
      event.preventDefault();
      setFailure(
        "The graphics context was lost. Use the 2D network, or reopen 3D to retry.",
      );
      setMode("2d");
    };
    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(mount);
    controls.addEventListener("change", invalidate);
    canvas.addEventListener("pointermove", move);
    canvas.addEventListener("pointerdown", pointerDown);
    canvas.addEventListener("pointerup", pointerUp);
    canvas.addEventListener("pointerleave", leave);
    canvas.addEventListener("pointercancel", leave);
    canvas.addEventListener("keydown", keyboard);
    canvas.addEventListener("webglcontextlost", lost);
    actions.current = {
      command,
      update: () => {
        hovered = "";
        setHover("");
        update();
      },
      png: () => {
        renderer.render(scene, camera);
        canvas.toBlob((blob) => {
          if (!blob) return;
          const url = URL.createObjectURL(blob),
            a = document.createElement("a");
          a.href = url;
          a.download = `affinity-${graph.focus_sku}-3d.png`;
          a.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        }, "image/png");
      },
    };
    resize();
    fit();
    update();
    return () => {
      disposed = true;
      cancelAnimationFrame(frame);
      actions.current = null;
      resizeObserver.disconnect();
      controls.dispose();
      canvas.removeEventListener("pointermove", move);
      canvas.removeEventListener("pointerdown", pointerDown);
      canvas.removeEventListener("pointerup", pointerUp);
      canvas.removeEventListener("pointerleave", leave);
      canvas.removeEventListener("pointercancel", leave);
      canvas.removeEventListener("keydown", keyboard);
      canvas.removeEventListener("webglcontextlost", lost);
      resources.forEach((resource) => resource.dispose());
      renderer.dispose();
      renderer.forceContextLoss();
      canvas.remove();
    };
  }, [model, mode, graph.focus_sku]);
  const span = model.span * 1.25,
    bySku = new Map(model.nodes.map((n) => [n.sku, n]));
  return (
    <div className="affinity-network">
      <div className="network-toolbar">
        <div className="inline-actions" role="group" aria-label="Affinity view">
          <button
            className={`button button-small ${mode === "3d" ? "primary" : ""}`}
            aria-pressed={mode === "3d"}
            onClick={() => setMode("3d")}
          >
            3D network
          </button>
          <button
            className={`button button-small ${mode === "2d" ? "primary" : ""}`}
            aria-pressed={mode === "2d"}
            onClick={() => setMode("2d")}
          >
            2D projection
          </button>
        </div>
        <label className="network-label-toggle">
          <input
            type="checkbox"
            checked={labels}
            onChange={(e) => setLabels(e.target.checked)}
          />{" "}
          Show SKU labels
        </label>
        <div className="inline-actions">
          <button
            className="button button-small"
            disabled={mode !== "3d"}
            onClick={() => actions.current?.png()}
          >
            <Download size={14} /> Save PNG
          </button>
          <button
            className="button button-small"
            onClick={() =>
              svg.current &&
              download(
                `affinity-${graph.focus_sku}.svg`,
                svg.current.outerHTML,
                "image/svg+xml",
              )
            }
          >
            <Download size={14} /> Export 2D SVG
          </button>
        </div>
      </div>
      {failure && (
        <div className="notice warning" role="status">
          {failure}
        </div>
      )}
      <div className="network-layout">
        <div className="network-stage">
          {mode === "3d" && (
            <>
              <div className="network-webgl" ref={host} />
              <div
                className="network-camera"
                role="group"
                aria-label="3D camera controls"
              >
                {(
                  [
                    ["left", "Rotate left", ArrowLeft],
                    ["right", "Rotate right", ArrowRight],
                    ["up", "Rotate up", ArrowUp],
                    ["down", "Rotate down", ArrowDown],
                    ["in", "Zoom in", ZoomIn],
                    ["out", "Zoom out", ZoomOut],
                    ["focus", "Center selected SKU", Focus],
                    ["reset", "Reset 3D view", RotateCcw],
                  ] as const
                ).map(([command, label, Icon]) => (
                  <button
                    key={command}
                    className="network-camera-button"
                    aria-label={label}
                    title={label}
                    onClick={() => actions.current?.command(command)}
                  >
                    <Icon size={17} />
                  </button>
                ))}
              </div>
            </>
          )}
          <div hidden={mode !== "2d"} className="network-projection">
            <svg
              ref={svg}
              xmlns="http://www.w3.org/2000/svg"
              width="920"
              height="600"
              viewBox={`${-span} ${-span} ${span * 2} ${span * 2}`}
              role="group"
              aria-label={`2D affinity network for SKU ${graph.focus_sku}`}
            >
              <rect
                x={-span}
                y={-span}
                width={span * 2}
                height={span * 2}
                fill="#071126"
              />
              {model.links.map((edge) => {
                const a = bySku.get(edge.sku_a)!,
                  b = bySku.get(edge.sku_b)!,
                  active =
                    edge.sku_a === selectedNode?.sku ||
                    edge.sku_b === selectedNode?.sku;
                return (
                  <line
                    key={`${edge.sku_a}/${edge.sku_b}`}
                    x1={a.x}
                    y1={a.y}
                    x2={b.x}
                    y2={b.y}
                    stroke={active ? "#bcecff" : "#7aaef8"}
                    strokeWidth={active ? 0.5 : 0.25}
                    opacity={active ? 0.9 : 0.2 + 0.3 * edge.cosine}
                  />
                );
              })}
              {model.nodes.map((node) => (
                <g
                  key={node.sku}
                  role="button"
                  tabIndex={0}
                  aria-label={`Inspect SKU ${node.sku}, class ${node.velocity_class}`}
                  onClick={() => onSelect(node.sku)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" || e.key === " ") {
                      e.preventDefault();
                      onSelect(node.sku);
                    }
                  }}
                >
                  <title>
                    {node.sku} · Class {node.velocity_class} ·{" "}
                    {count(node.pick_frequency)} picks
                  </title>
                  <circle
                    cx={node.x}
                    cy={node.y}
                    r={node.radius}
                    fill={affinityColors[node.velocity_class] || "#cbd5e1"}
                    stroke={node.sku === selectedNode?.sku ? "white" : "none"}
                    strokeWidth="1"
                  />
                  {(labels ||
                    node.sku === selectedNode?.sku ||
                    node.sku === graph.focus_sku) && (
                    <text
                      x={node.x}
                      y={node.y - node.radius - 2}
                      textAnchor="middle"
                      fill="#f8fafc"
                      fontSize="3"
                      fontFamily="sans-serif"
                    >
                      {node.sku} · {node.velocity_class}
                    </text>
                  )}
                </g>
              ))}
            </svg>
          </div>
          <div className="network-stage-caption">
            <strong>
              {model.nodes.length} SKUs · {model.links.length} connections
            </strong>
            <span>
              {hover
                ? `Hover: ${hover}`
                : "Select a glowing node to inspect its connections"}
            </span>
          </div>
        </div>
        <aside className="network-inspector">
          <Field label="Inspect SKU in network">
            <select
              value={selectedNode?.sku || ""}
              onChange={(e) => onSelect(e.target.value)}
            >
              {model.nodes.map((n) => (
                <option key={n.sku} value={n.sku}>
                  {n.sku} · Class {n.velocity_class}
                </option>
              ))}
            </select>
          </Field>
          {selectedNode && (
            <>
              <div className="network-selected-title">
                <strong>{selectedNode.sku}</strong>
                <Badge>Class {selectedNode.velocity_class}</Badge>
              </div>
              <p className="muted">
                {selectedNode.sku === graph.focus_sku
                  ? "Focus SKU"
                  : "Related SKU"}{" "}
                · {count(selectedNode.pick_frequency)} picks
              </p>
              <div className="network-degree">
                <strong>{connected.length}</strong>
                <span>connections in this view</span>
              </div>
              <button
                className="button primary"
                disabled={busy || selectedNode.sku === graph.focus_sku}
                onClick={() => onExplore(selectedNode.sku)}
              >
                Explore this SKU <ArrowRight size={15} />
              </button>
              <h3>Strongest shown connections</h3>
              <ul className="network-connections">
                {connected.slice(0, 5).map((edge) => {
                  const other =
                    edge.sku_a === selectedNode.sku ? edge.sku_b : edge.sku_a;
                  return (
                    <li key={other}>
                      <button onClick={() => onSelect(other)}>{other}</button>
                      <span>{edge.cosine.toFixed(3)}</span>
                    </li>
                  );
                })}
              </ul>
              {!connected.length && (
                <p className="muted">
                  No connections meet the current filters.
                </p>
              )}
              {connected.length > 5 && (
                <p className="muted">
                  Top 5 shown. All links are in the connection table below.
                </p>
              )}
            </>
          )}
          <div className="network-legend">
            <span>
              <i style={{ background: affinityColors.A }} /> A · High velocity
            </span>
            <span>
              <i style={{ background: affinityColors.B }} /> B · Medium velocity
            </span>
            <span>
              <i style={{ background: affinityColors.C }} /> C · Low velocity
            </span>
          </div>
        </aside>
      </div>
      <p className="network-instructions">
        Drag to orbit · scroll/pinch to zoom · right-drag/two fingers to pan.
        Arrow keys rotate, +/− zoom, Home resets. Base node size shows pick
        frequency; selection is enlarged and its links are highlighted. Other
        link brightness shows cosine affinity. Spacing illustrates
        relationships, rather than warehouse distance.
      </p>
    </div>
  );
}

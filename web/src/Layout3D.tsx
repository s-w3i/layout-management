import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import {
  Crosshair,
  Focus,
  RotateCcw,
  RotateCw,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import type { Row } from "../../shared/model";
import { layoutScene, visibleScene, type Box3D } from "../../shared/layout3d";
import { phasePosition } from "../../shared/asrs";

const zones = [
  "#799bd2",
  "#a790c5",
  "#70ab98",
  "#c5a16a",
  "#bd839c",
  "#6da5b5",
  "#a5aa6a",
  "#8f9eb4",
];
const classes: Record<string, string> = {
  A: "#e6a04c",
  B: "#719fc7",
  C: "#abbac8",
};
type Command = "overview" | "focus" | "top" | "left" | "right" | "in" | "out";
type Pose = { scope: string; position: THREE.Vector3; target: THREE.Vector3 };
const worldBox = (box: Box3D) =>
  new THREE.Box3(
    new THREE.Vector3(
      box.x - box.width / 2,
      box.z - box.height / 2,
      -box.y - box.depth / 2,
    ),
    new THREE.Vector3(
      box.x + box.width / 2,
      box.z + box.height / 2,
      -box.y + box.depth / 2,
    ),
  );

export default function Layout3D({
  project,
  geometry,
  result,
  selected,
  onSelect,
  simulation,
}: {
  project: Row;
  geometry: Row | null;
  result: Row;
  selected: string;
  onSelect: (rack: string) => void;
  simulation?: Row;
}) {
  const host = useRef<HTMLDivElement>(null),
    actions = useRef<{
      command: (kind: Command) => void;
      select: (id: string) => void;
      live?: () => void;
    } | null>(null),
    pose = useRef<Pose | null>(null);
  const selectedRef = useRef(selected);
  const simulationRef = useRef(simulation);
  simulationRef.current = simulation;
  selectedRef.current = selected;
  const [colour, setColour] = useState("abc"),
    [level, setLevel] = useState(0),
    [isolate, setIsolate] = useState(false),
    [failure, setFailure] = useState("");
  const model = useMemo(
    () => (geometry ? layoutScene(project, geometry, result) : null),
    [project, geometry, result],
  );
  const scope =
    isolate && model?.racks.some((r: Row) => r.id === selected) ? selected : "";
  const view = useMemo(
    () =>
      model
        ? visibleScene(
            model,
            Math.min(level || model.levels, model.levels),
            scope,
          )
        : null,
    [model, level, scope],
  );
  useEffect(() => {
    pose.current = null;
  }, [model]);
  useEffect(() => {
    if (!host.current || !geometry || !model || !view) return;
    const mount = host.current;
    let renderer: THREE.WebGLRenderer;
    try {
      renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
    } catch {
      setFailure(
        "3D rendering is unavailable. Enable WebGL 2 / hardware acceleration, or use the 2D plan.",
      );
      return;
    }
    setFailure("");
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor("#f5f7fa");
    renderer.outputColorSpace = THREE.SRGBColorSpace;
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    mount.appendChild(renderer.domElement);
    const canvas = renderer.domElement;
    canvas.tabIndex = 0;
    canvas.setAttribute("role", "img");
    canvas.setAttribute(
      "aria-label",
      "Interactive 3D warehouse layout. Drag to orbit, scroll to zoom, right-drag to pan. Arrow keys rotate, plus and minus zoom, Home resets. Select a rack with the labelled selector to inspect inventory.",
    );
    const scene = new THREE.Scene();
    scene.add(new THREE.HemisphereLight(0xffffff, 0x8191a5, 2.4));
    const light = new THREE.DirectionalLight(0xffffff, 2.8);
    light.position.set(30, 70, 50);
    scene.add(light);
    const camera = new THREE.PerspectiveCamera(42, 1, 0.01, 10000),
      controls = new OrbitControls(camera, canvas);
    controls.enableDamping = false;
    controls.maxPolarAngle = Math.PI / 2 - 0.025;
    controls.minPolarAngle = 0.015;
    controls.screenSpacePanning = true;
    const cube = new THREE.BoxGeometry(1, 1, 1),
      materials: THREE.Material[] = [],
      meshes: THREE.InstancedMesh[] = [];
    const picks: THREE.Object3D[] = [];
    const frameMaterial = new THREE.MeshStandardMaterial({
      color: "#76879a",
      roughness: 0.8,
    });
    materials.push(frameMaterial);
    const matrix = new THREE.Object3D();
    const boxes = (
      items: { box: Box3D; rack: string; color?: string }[],
      material: THREE.Material,
    ) => {
      if (!items.length) return;
      const mesh = new THREE.InstancedMesh(cube, material, items.length);
      mesh.userData.racks = items.map((i) => i.rack);
      items.forEach((item, index) => {
        const b = item.box;
        matrix.position.set(b.x, b.z, -b.y);
        matrix.scale.set(b.width, b.height, b.depth);
        matrix.updateMatrix();
        mesh.setMatrixAt(index, matrix.matrix);
        if (item.color) mesh.setColorAt(index, new THREE.Color(item.color));
      });
      mesh.instanceMatrix.needsUpdate = true;
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
      mesh.computeBoundingSphere();
      scene.add(mesh);
      meshes.push(mesh);
      picks.push(mesh);
      return mesh;
    };
    const frames: { box: Box3D; rack: string }[] = [];
    for (const rack of view.racks) {
      const b = rack.box as Box3D;
      const top = b.z + b.height / 2;
      const post = Math.min(0.07, b.width / 8, b.depth / 8);
      for (const x of [-1, 1])
        for (const y of [-1, 1])
          frames.push({
            rack: rack.id,
            box: {
              x: b.x + (x * (b.width - post)) / 2,
              y: b.y + (y * (b.depth - post)) / 2,
              z: top / 2,
              width: post,
              depth: post,
              height: top,
            },
          });
    }
    boxes(frames, frameMaterial);
    const shelves: { box: Box3D; rack: string }[] = [];
    const shelfCells = new Map<string, typeof view.cells>();
    for (const cell of view.cells) {
      const key = `${cell.rack}/${cell.level}`;
      if (!shelfCells.has(key)) shelfCells.set(key, []);
      shelfCells.get(key)!.push(cell);
    }
    for (const cells of shelfCells.values()) {
      const bounds = new THREE.Box3();
      for (const cell of cells) bounds.union(worldBox(cell));
      const min = bounds.min,
        max = bounds.max;
      shelves.push({
        rack: cells[0].rack,
        box: {
          x: (min.x + max.x) / 2,
          y: -(min.z + max.z) / 2,
          z: min.y + 0.025,
          width: max.x - min.x,
          depth: max.z - min.z,
          height: 0.05,
        },
      });
    }
    boxes(shelves, frameMaterial);
    const zoneIds = [...new Set(model.cells.map((c) => c.zone))].sort();
    const loadMaterial = new THREE.MeshStandardMaterial({
      roughness: 0.7,
      metalness: 0.02,
    });
    materials.push(loadMaterial);
    const loadMesh = boxes(
      view.loads.map((load) => ({
        rack: load.rack,
        box: {
          ...load,
          width: Math.max(0.01, load.width - 0.065),
          depth: Math.max(0.01, load.depth - 0.08),
          height: Math.max(0.01, load.height - 0.11),
          z: load.z + 0.02,
        },
        color: load.unverified
          ? "#c45f54"
          : colour === "zones"
            ? zones[zoneIds.indexOf(load.zone) % zones.length]
            : (classes[load.velocity] ?? classes.C),
      })),
      loadMaterial,
    );
    const allBounds = new THREE.Box3();
    for (const rack of view.racks) allBounds.union(worldBox(rack.box));
    if (!scope)
      allBounds.union(
        worldBox({
          x: project.grid.width_m / 2,
          y: project.grid.length_m / 2,
          z: 0,
          width: project.grid.width_m,
          depth: project.grid.length_m,
          height: 0.1,
        }),
      );
    if (allBounds.isEmpty())
      allBounds.set(new THREE.Vector3(0, 0, 0), new THREE.Vector3(2, 2, 2));
    const centre = allBounds.getCenter(new THREE.Vector3()),
      size = allBounds.getSize(new THREE.Vector3());
    const span = Math.max(size.x, size.y, size.z, 2);
    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(size.x + 4, size.z + 4),
      new THREE.MeshStandardMaterial({ color: "#e8edf2", roughness: 1 }),
    );
    floor.rotation.x = -Math.PI / 2;
    floor.position.set(centre.x, -0.035, centre.z);
    scene.add(floor);
    const gridPoints: number[] = [];
    const min = allBounds.min,
      max = allBounds.max;
    const stepX = Math.max(Number(project.grid.spacing_m) || 2, size.x / 100),
      stepY = Math.max(
        Number(project.grid.spacing_y_m ?? project.grid.spacing_m) || 2,
        size.z / 100,
      );
    for (let x = Math.ceil(min.x / stepX) * stepX; x <= max.x; x += stepX)
      gridPoints.push(x, -0.025, min.z, x, -0.025, max.z);
    for (let z = Math.ceil(min.z / stepY) * stepY; z <= max.z; z += stepY)
      gridPoints.push(min.x, -0.025, z, max.x, -0.025, z);
    const grid = new THREE.LineSegments(
      new THREE.BufferGeometry().setAttribute(
        "position",
        new THREE.Float32BufferAttribute(gridPoints, 3),
      ),
      new THREE.LineBasicMaterial({ color: 0xc3ceda }),
    );
    scene.add(grid);
    const stationGeometry = new THREE.CylinderGeometry(0.4, 0.4, 0.25, 16),
      stationMaterial = new THREE.MeshStandardMaterial({ color: "#287557" });
    materials.push(stationMaterial);
    if (!scope)
      for (const node of geometry.nodes ?? [])
        if (node.role === "workstation") {
          const station = new THREE.Mesh(stationGeometry, stationMaterial);
          station.position.set(node.x, 0.125, -node.y);
          scene.add(station);
        }
    const highlight = new THREE.Box3Helper(new THREE.Box3(), 0xcb682c);
    highlight.visible = false;
    scene.add(highlight);
    const craneMeshes = new Map<
      string,
      { mast: THREE.Mesh; carriage: THREE.Mesh; tote: THREE.Mesh }
    >();
    const mastMaterial = new THREE.MeshStandardMaterial({ color: 0x2873ba }),
      carriageMaterial = new THREE.MeshStandardMaterial({ color: 0x277452 }),
      carriedMaterial = new THREE.MeshStandardMaterial({ color: 0xd48b32 });
    materials.push(mastMaterial, carriageMaterial, carriedMaterial);
    const maxHeight = Math.max(
        2,
        ...model.racks.map((r: Row) => r.box.z + r.box.height / 2),
      ),
      reduced = window.matchMedia("(prefers-reduced-motion: reduce)");
    let lastLive: Row | undefined;
    const updateSimulation = () => {
      const live = simulationRef.current;
      if (!live) return;
      const carried = new Set<string>();
      for (const c of live.cranes) {
        let meshes = craneMeshes.get(c.id);
        if (!meshes) {
          meshes = {
            mast: new THREE.Mesh(cube, mastMaterial),
            carriage: new THREE.Mesh(cube, carriageMaterial),
            tote: new THREE.Mesh(cube, carriedMaterial),
          };
          for (const mesh of Object.values(meshes)) scene.add(mesh);
          craneMeshes.set(c.id, meshes);
        }
        const interpolate =
          live.running &&
          live.speed !== "max" &&
          live.speed <= 100 &&
          !reduced.matches;
        const at =
          c.phase && interpolate
            ? Math.min(
                c.phase.end,
                live.now +
                  ((performance.now() - live.received_at) / 1000) * live.speed,
              )
            : live.now;
        const pos = c.phase
          ? phasePosition(c.phase, at, live.axes)
          : c.position;
        meshes.mast.position.set(pos[0], maxHeight / 2, -c.home[1]);
        meshes.mast.scale.set(0.08, maxHeight, 0.08);
        meshes.carriage.position.set(pos[0], pos[2], -pos[1]);
        meshes.carriage.scale.set(0.85, 0.16, 0.55);
        meshes.tote.position.set(pos[0], pos[2] + 0.25, -pos[1]);
        meshes.tote.scale.set(0.5, 0.4, 0.65);
        meshes.tote.visible = c.loaded;
        if (c.loaded && c.load_id) carried.add(c.load_id);
      }
      if (loadMesh && live !== lastLive) {
        view.loads.forEach((load, index) => {
          matrix.position.set(load.x, load.z + 0.02, -load.y);
          matrix.scale.set(
            Math.max(0.01, load.width - 0.065),
            Math.max(0.01, load.height - 0.11),
            Math.max(0.01, load.depth - 0.08),
          );
          if (carried.has(load.id)) matrix.scale.set(0, 0, 0);
          matrix.updateMatrix();
          loadMesh.setMatrixAt(index, matrix.matrix);
          loadMesh.setColorAt(
            index,
            new THREE.Color(
              live.quantities[load.id] > 0
                ? load.unverified
                  ? "#c45f54"
                  : colour === "zones"
                    ? zones[zoneIds.indexOf(load.zone) % zones.length]
                    : (classes[load.velocity] ?? classes.C)
                : "#d8dee6",
            ),
          );
        });
        loadMesh.instanceMatrix.needsUpdate = true;
        if (loadMesh.instanceColor) loadMesh.instanceColor.needsUpdate = true;
      }
      lastLive = live;
    };
    let disposed = false,
      frame = 0;
    const render = () => {
      frame = 0;
      if (!disposed) {
        updateSimulation();
        renderer.render(scene, camera);
        const live = simulationRef.current;
        if (
          live?.running &&
          live.speed !== "max" &&
          live.speed <= 100 &&
          !reduced.matches &&
          !document.hidden
        )
          invalidate();
      }
    };
    const invalidate = () => {
      if (!frame && !disposed) frame = requestAnimationFrame(render);
    };
    const fit = (bounds: THREE.Box3, top = false) => {
      const c = bounds.getCenter(new THREE.Vector3()),
        s = bounds.getSize(new THREE.Vector3());
      const vertical = Math.max(s.x / camera.aspect, s.z, s.y * 1.7, 2);
      const distance =
        (vertical / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)))) *
        1.45;
      controls.target.copy(c);
      camera.position
        .copy(c)
        .add(
          (top
            ? new THREE.Vector3(0, 1, 0.001)
            : new THREE.Vector3(0.65, 0.8, 1)
          )
            .normalize()
            .multiplyScalar(distance),
        );
      camera.near = Math.max(0.01, distance / 2000);
      camera.far = Math.max(1000, distance * 20);
      camera.updateProjectionMatrix();
      controls.update();
      invalidate();
    };
    const select = (id: string) => {
      const rack = view.racks.find((r: Row) => r.id === id);
      highlight.visible = Boolean(rack);
      if (rack) {
        highlight.box.copy(worldBox(rack.box)).expandByScalar(0.035);
        highlight.updateMatrixWorld(true);
      }
      invalidate();
    };
    const command = (kind: Command) => {
      if (kind === "overview") fit(allBounds);
      else if (kind === "top") fit(allBounds, true);
      else if (kind === "focus") {
        const rack = view.racks.find((r: Row) => r.id === selectedRef.current);
        if (rack) fit(worldBox(rack.box).expandByScalar(0.35));
      } else {
        const delta = camera.position.clone().sub(controls.target);
        if (kind === "left" || kind === "right")
          delta.applyAxisAngle(
            new THREE.Vector3(0, 1, 0),
            kind === "left" ? 0.2 : -0.2,
          );
        else delta.multiplyScalar(kind === "in" ? 0.8 : 1.25);
        delta.clampLength(0.6, span * 12);
        camera.position.copy(controls.target).add(delta);
        controls.update();
        invalidate();
      }
    };
    const resize = () => {
      const width = mount.clientWidth,
        height = mount.clientHeight;
      if (!width || !height) return;
      renderer.setSize(width, height);
      camera.aspect = width / height;
      camera.updateProjectionMatrix();
      invalidate();
    };
    resize();
    controls.minDistance = 0.6;
    controls.maxDistance = span * 12;
    if (pose.current?.scope === scope) {
      camera.position.copy(pose.current.position);
      controls.target.copy(pose.current.target);
      controls.update();
    } else fit(allBounds);
    select(selectedRef.current);
    actions.current = { command, select, live: invalidate };
    controls.addEventListener("change", invalidate);
    const observer = new ResizeObserver(resize);
    observer.observe(mount);
    let down = { x: 0, y: 0 };
    const pointerDown = (event: PointerEvent) => {
      down = { x: event.clientX, y: event.clientY };
    };
    const pointerUp = (event: PointerEvent) => {
      if (
        event.button !== 0 ||
        Math.hypot(event.clientX - down.x, event.clientY - down.y) > 5
      )
        return;
      const rect = canvas.getBoundingClientRect(),
        ray = new THREE.Raycaster();
      ray.setFromCamera(
        new THREE.Vector2(
          ((event.clientX - rect.left) / rect.width) * 2 - 1,
          (-(event.clientY - rect.top) / rect.height) * 2 + 1,
        ),
        camera,
      );
      const hit = ray.intersectObjects(picks, false)[0];
      if (hit && hit.instanceId != null)
        onSelect(hit.object.userData.racks[hit.instanceId]);
    };
    const key = (event: KeyboardEvent) => {
      if (event.ctrlKey || event.metaKey || event.altKey) return;
      const keys: Record<string, Command> = {
        ArrowLeft: "left",
        ArrowRight: "right",
        "+": "in",
        "=": "in",
        "-": "out",
        Home: "overview",
      };
      if (keys[event.key]) {
        event.preventDefault();
        command(keys[event.key]);
      } else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
        event.preventDefault();
        const spherical = new THREE.Spherical().setFromVector3(
          camera.position.clone().sub(controls.target),
        );
        spherical.phi = THREE.MathUtils.clamp(
          spherical.phi + (event.key === "ArrowUp" ? -0.12 : 0.12),
          0.02,
          Math.PI / 2 - 0.025,
        );
        camera.position
          .copy(controls.target)
          .add(new THREE.Vector3().setFromSpherical(spherical));
        controls.update();
        invalidate();
      }
    };
    const lost = (event: Event) => {
      event.preventDefault();
      setFailure(
        "The 3D graphics context was lost. Switch to the 2D plan, then reopen 3D to retry.",
      );
    };
    canvas.addEventListener("pointerdown", pointerDown);
    canvas.addEventListener("pointerup", pointerUp);
    canvas.addEventListener("keydown", key);
    canvas.addEventListener("webglcontextlost", lost);
    invalidate();
    return () => {
      pose.current = {
        scope,
        position: camera.position.clone(),
        target: controls.target.clone(),
      };
      disposed = true;
      cancelAnimationFrame(frame);
      observer.disconnect();
      controls.dispose();
      actions.current = null;
      canvas.removeEventListener("pointerdown", pointerDown);
      canvas.removeEventListener("pointerup", pointerUp);
      canvas.removeEventListener("keydown", key);
      canvas.removeEventListener("webglcontextlost", lost);
      for (const mesh of meshes) mesh.dispose();
      cube.dispose();
      stationGeometry.dispose();
      floor.geometry.dispose();
      (floor.material as THREE.Material).dispose();
      grid.geometry.dispose();
      (grid.material as THREE.Material).dispose();
      highlight.geometry.dispose();
      (highlight.material as THREE.Material).dispose();
      for (const material of materials) material.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
      canvas.remove();
    };
  }, [model, view, geometry, project, colour, scope, onSelect]);
  useEffect(() => {
    actions.current?.select(selected);
  }, [selected]);
  useEffect(() => {
    actions.current?.live?.();
  }, [simulation]);
  if (!model || !view)
    return (
      <div className="three-loading" role="status">
        Preparing warehouse geometry…
      </div>
    );
  const validSelection = view.racks.some((r: Row) => r.id === selected);
  return (
    <div className="three-view">
      <div className="three-toolbar">
        <label>
          Colour by
          <select value={colour} onChange={(e) => setColour(e.target.value)}>
            <option value="abc">ABC class</option>
            <option value="zones">Storage zone</option>
          </select>
        </label>
        <label>
          Visible levels
          <select
            value={level}
            onChange={(e) => setLevel(Number(e.target.value))}
          >
            <option value={0}>All {model.levels} levels</option>
            {Array.from({ length: model.levels }, (_, i) => (
              <option key={i} value={i + 1}>
                {i === 0 ? "Level 1" : `Levels 1–${i + 1}`}
              </option>
            ))}
          </select>
        </label>
        <fieldset
          disabled={!!failure}
          className="three-camera"
          role="group"
          aria-label="3D camera controls"
        >
          <button
            className="icon-button"
            aria-label="Orbit left"
            onClick={() => actions.current?.command("left")}
          >
            <RotateCcw size={17} />
          </button>
          <button
            className="icon-button"
            aria-label="Orbit right"
            onClick={() => actions.current?.command("right")}
          >
            <RotateCw size={17} />
          </button>
          <button
            className="icon-button"
            aria-label="Zoom 3D in"
            onClick={() => actions.current?.command("in")}
          >
            <ZoomIn size={17} />
          </button>
          <button
            className="icon-button"
            aria-label="Zoom 3D out"
            onClick={() => actions.current?.command("out")}
          >
            <ZoomOut size={17} />
          </button>
          <button
            className="button button-small"
            onClick={() => actions.current?.command("top")}
          >
            Top
          </button>
          <button
            className="icon-button"
            aria-label="Fit 3D warehouse"
            onClick={() => actions.current?.command("overview")}
          >
            <Crosshair size={17} />
          </button>
        </fieldset>
      </div>
      <div className="three-stage" ref={host}>
        {failure && (
          <div className="three-unavailable" role="alert">
            {failure}
          </div>
        )}
      </div>
      <div className="three-inspect">
        <label>
          Inspect rack
          <select
            value={
              model.racks.some((r: Row) => r.id === selected) ? selected : ""
            }
            onChange={(e) => {
              if (!e.target.value) setIsolate(false);
              onSelect(e.target.value);
            }}
          >
            <option value="">Choose a rack</option>
            {[...model.racks]
              .sort((a: Row, b: Row) => a.id.localeCompare(b.id))
              .map((r: Row) => (
                <option key={r.id} value={r.id}>
                  {r.id} · {r.zone}
                </option>
              ))}
          </select>
        </label>
        <button
          className="button button-small"
          disabled={!validSelection || !!failure}
          onClick={() => actions.current?.command("focus")}
        >
          <Focus size={16} />
          Focus rack
        </button>
        <label className="three-isolate">
          <input
            type="checkbox"
            checked={isolate}
            disabled={!isolate && !validSelection}
            onChange={(e) => setIsolate(e.target.checked)}
          />
          Isolate rack
        </label>
      </div>
      <div className="three-legend">
        {(colour === "abc"
          ? Object.entries(classes)
          : [...new Set(model.cells.map((c) => c.zone))]
              .sort()
              .map((zone, i) => [zone, zones[i % zones.length]])
        ).map(([label, color]) => (
          <span key={label}>
            <i style={{ background: color }} />
            {label}
          </span>
        ))}
        <span>
          <i style={{ background: "#c45f54" }} />
          Unverified
        </span>
        <span>
          <i style={{ background: "#287557" }} />
          Workstation
        </span>
        <span>
          {view.cells.length.toLocaleString()} cells ·{" "}
          {view.loads.length.toLocaleString()} allocations
        </span>
      </div>
      <p className="three-help">
        Drag to orbit · scroll to zoom · right-drag to pan. X/Y ground plane, Z
        height. Blocks show allocated space, not individual item shapes. The
        rack inspector includes loads on hidden levels.
        {model.estimatedCells > 0 &&
          " Shelf dimensions without explicit geometry are estimated from grid spacing and rack settings."}
      </p>
      {model.unmappedUnits > 0 && (
        <p className="inline-warning">
          {model.unmappedUnits} occupied cells could not be mapped to 3D
          geometry.
        </p>
      )}
    </div>
  );
}

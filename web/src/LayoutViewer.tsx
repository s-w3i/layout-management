import { Component, lazy, Suspense, useState, type ReactNode } from "react";
import { Box, Map, ChartNoAxesCombined } from "lucide-react";
import type { Row } from "../../shared/model";
import { MapCanvas } from "./MapCanvas";
import MovementPanel from "./MovementPanel";
const Layout3D = lazy(() => import("./Layout3D"));
class RenderBoundary extends Component<
  { children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  render() {
    return this.state.failed ? (
      <div className="three-loading" role="alert">
        The 3D view could not load. Use the 2D plan, or reload the app to retry.
      </div>
    ) : (
      this.props.children
    );
  }
}
export default function LayoutViewer(props: {
  project: Row;
  geometry: Row | null;
  result: Row;
  selected: string;
  onSelect: (rack: string) => void;
  onCalculateMovement: (range: Row) => void;
  orders: Row | null;
  settings: Row;
}) {
  const [view, setView] = useState("2d");
  return (
    <section className="layout-viewer" aria-label="Generated layout views">
      <div className="layout-view-switch">
        <div>
          <strong>Layout view</strong>
          <span>
            Inspect the allocation in 2D, 3D or by estimated rack movement.
          </span>
        </div>
        <div className="view-options" role="group" aria-label="Layout view">
          <button
            className="button button-small"
            aria-pressed={view === "2d"}
            onClick={() => setView("2d")}
          >
            <Map size={16} />
            2D plan
          </button>
          <button
            className="button button-small"
            aria-pressed={view === "3d"}
            onClick={() => setView("3d")}
          >
            <Box size={16} />
            3D view
          </button>
          <button
            className="button button-small"
            aria-pressed={view === "movement"}
            onClick={() => setView("movement")}
          >
            <ChartNoAxesCombined size={16} aria-hidden="true" />
            Rack movement
          </button>
        </div>
      </div>
      {view === "movement" ? (
        <MovementPanel
          key={props.orders?.id ?? "no-orders"}
          {...props}
          onCalculate={props.onCalculateMovement}
        />
      ) : view === "2d" ? (
        <MapCanvas {...props} />
      ) : (
        <RenderBoundary>
          <Suspense
            fallback={
              <div className="three-loading" role="status">
                Loading 3D renderer…
              </div>
            }
          >
            <Layout3D {...props} />
          </Suspense>
        </RenderBoundary>
      )}
    </section>
  );
}

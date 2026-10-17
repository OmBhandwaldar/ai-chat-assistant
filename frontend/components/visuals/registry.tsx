"use client";

import { Component, type ErrorInfo, type ReactNode } from "react";
import dynamic from "next/dynamic";
import type { UIVisual } from "@/lib/types";
import StatusCard from "./StatusCard";
import WellTable from "./WellTable";
import LineChart from "./LineChart";
import BarChart from "./BarChart";
import Heatmap from "./Heatmap";
import { NoData } from "./shared";

// Leaflet needs `window`; load it only in the browser.
const MapView = dynamic(() => import("./MapView"), {
  ssr: false,
  loading: () => <div className="h-80 animate-pulse rounded-xl border border-neutral-200 bg-neutral-50" />,
});

/** Keeps a bad visual payload from taking the whole chat page down. */
class VisualErrorBoundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Visual failed to render", error, info.componentStack);
  }
  render() {
    return this.state.failed ? <NoData message="Unsupported visual" /> : this.props.children;
  }
}

function pick(visual: UIVisual) {
  const data: unknown = visual.data;
  switch (visual.type) {
    case "status_card":
      return <StatusCard data={data} />;
    case "table":
      return <WellTable data={data} />;
    case "line":
      return <LineChart data={data} />;
    case "bar":
      return <BarChart data={data} />;
    case "heatmap":
      return <Heatmap data={data} />;
    case "map":
      return <MapView data={data} />;
    case "none":
      return null;
    default:
      return <NoData message="Unsupported visual" />;
  }
}

export function VisualRenderer({ visual }: { visual?: UIVisual | null }) {
  if (!visual || typeof visual !== "object" || visual.type === "none") return null;
  // Re-key on type so switching visual types resets a previously failed boundary.
  return <VisualErrorBoundary key={visual.type}>{pick(visual)}</VisualErrorBoundary>;
}

export function renderVisual(visual: UIVisual) {
  return <VisualRenderer visual={visual} />;
}

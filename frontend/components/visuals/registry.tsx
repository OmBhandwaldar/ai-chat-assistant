"use client";

import type { UIVisual } from "@/lib/types";

// TODO(Phase 5.2-5.3, Sonnet/Ollama): dispatch to StatusCard, WellTable,
// LineChart, BarChart, MapView, Heatmap per PLAN.md. Stub below renders a
// placeholder card so the chat shell is usable before those land.
export function renderVisual(visual: UIVisual) {
  return (
    <div className="rounded-xl border border-neutral-200 bg-white p-4 text-sm text-neutral-500">
      [{visual.type}] visual placeholder — component not yet implemented
    </div>
  );
}

"use client";

import type { ReactNode } from "react";
import { Legend, ResponsiveContainer, Tooltip } from "recharts";

// Explicit hex colours: no dependence on Tailwind class generation.
export const SERIES_COLORS = ["#2563eb", "#737373", "#06b6d4", "#d97706"];
export const AXIS_TICK = { fontSize: 12, fill: "#525252" };
export const AXIS_LINE = { stroke: "#d4d4d4" };
export const GRID_STROKE = "#e5e5e5";

export function ChartFrame({ children }: { children: ReactNode }) {
  return (
    <div className="h-72 w-full">
      <ResponsiveContainer width="100%" height="100%">
        {/* ResponsiveContainer clones its single child, so it must be an element, not a fragment. */}
        {children as React.ReactElement}
      </ResponsiveContainer>
    </div>
  );
}

export const chartTooltip = (format: (v: number) => string) => (
  <Tooltip
    formatter={(v) => format(Number(v))}
    contentStyle={{ borderRadius: 8, border: "1px solid #e5e5e5", fontSize: 12, color: "#171717" }}
    labelStyle={{ color: "#171717", fontWeight: 600 }}
    cursor={{ fill: "rgba(0,0,0,0.04)", stroke: "#d4d4d4" }}
  />
);

export const chartLegend = (
  <Legend
    iconType="circle"
    iconSize={8}
    wrapperStyle={{ fontSize: 12, color: "#404040", paddingTop: 8 }}
  />
);

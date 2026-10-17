"use client";

import { useMemo } from "react";
import { CartesianGrid, Line, LineChart as RLineChart, XAxis, YAxis } from "recharts";
import { normalizeSeries } from "@/lib/visual-data";
import { NoData, VisualCard } from "./shared";
import {
  AXIS_LINE,
  AXIS_TICK,
  ChartFrame,
  GRID_STROKE,
  SERIES_COLORS,
  chartLegend,
  chartTooltip,
} from "./chart-shared";

const fmt = (v: number) => (Number.isInteger(v) ? String(v) : v.toFixed(1));

export default function LineChart({ data }: { data?: unknown }) {
  const s = useMemo(() => normalizeSeries(data), [data]);
  if (s.rows.length === 0 || s.categories.length === 0) return <NoData />;

  const title = typeof (data as { title?: unknown })?.title === "string" ? (data as { title: string }).title : undefined;
  return (
    <VisualCard title={title}>
      <ChartFrame>
        <RLineChart data={s.rows} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={GRID_STROKE} vertical={false} />
          <XAxis dataKey={s.index} tick={AXIS_TICK} tickLine={false} axisLine={AXIS_LINE} minTickGap={24} />
          <YAxis tick={AXIS_TICK} tickLine={false} axisLine={false} width={40} tickFormatter={fmt} />
          {chartTooltip(fmt)}
          {chartLegend}
          {s.categories.map((c, i) => (
            <Line
              key={c}
              type="monotone"
              dataKey={c}
              stroke={SERIES_COLORS[i % SERIES_COLORS.length]}
              strokeWidth={2}
              dot={{ r: 3 }}
              activeDot={{ r: 5 }}
              connectNulls
              isAnimationActive={false}
            />
          ))}
        </RLineChart>
      </ChartFrame>
    </VisualCard>
  );
}

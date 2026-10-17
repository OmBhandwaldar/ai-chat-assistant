"use client";

import { useMemo } from "react";
import { Bar, BarChart as RBarChart, CartesianGrid, XAxis, YAxis } from "recharts";
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

export default function BarChart({ data }: { data?: unknown }) {
  const s = useMemo(() => normalizeSeries(data), [data]);
  if (s.rows.length === 0 || s.categories.length === 0) return <NoData />;

  const isCost = s.rawCategories.some((c) => /cost|usd|spend/i.test(c));
  const fmt = (v: number) =>
    isCost ? `$${v.toLocaleString(undefined, { maximumFractionDigits: 0 })}` : v.toLocaleString();
  const title = typeof (data as { title?: unknown })?.title === "string" ? (data as { title: string }).title : undefined;

  return (
    <VisualCard title={title}>
      <ChartFrame>
        <RBarChart data={s.rows} margin={{ top: 8, right: 12, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={GRID_STROKE} vertical={false} />
          <XAxis dataKey={s.index} tick={AXIS_TICK} tickLine={false} axisLine={AXIS_LINE} interval={0} />
          <YAxis tick={AXIS_TICK} tickLine={false} axisLine={false} width={isCost ? 64 : 40} tickFormatter={fmt} />
          {chartTooltip(fmt)}
          {s.categories.length > 1 && chartLegend}
          {s.categories.map((c, i) => (
            <Bar
              key={c}
              dataKey={c}
              fill={SERIES_COLORS[i % SERIES_COLORS.length]}
              radius={[4, 4, 0, 0]}
              maxBarSize={56}
              isAnimationActive={false}
            />
          ))}
        </RBarChart>
      </ChartFrame>
    </VisualCard>
  );
}

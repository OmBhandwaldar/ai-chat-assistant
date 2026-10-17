"use client";

import { useMemo } from "react";
import { ResponsiveHeatMap } from "@nivo/heatmap";
import { normalizeHeatmap } from "@/lib/visual-data";
import { NoData, VisualCard } from "./shared";

export default function Heatmap({ data }: { data?: unknown }) {
  const series = useMemo(() => normalizeHeatmap(data), [data]);
  const hasCells = series.some((r) => r.data.length > 0);
  if (series.length === 0 || !hasCells) return <NoData />;

  const max = Math.max(1, ...series.flatMap((r) => r.data.map((c) => c.y)));
  const title = typeof (data as { title?: unknown })?.title === "string" ? (data as { title: string }).title : undefined;
  const height = Math.max(200, series.length * 40 + 70);

  return (
    <VisualCard title={title}>
      <div style={{ height }} role="img" aria-label="Heatmap of counts by area and month">
        <ResponsiveHeatMap
          data={series}
          margin={{ top: 30, right: 10, bottom: 10, left: 110 }}
          valueFormat=">-.0f"
          axisTop={{ tickSize: 0, tickPadding: 6 }}
          axisLeft={{ tickSize: 0, tickPadding: 8 }}
          axisBottom={null}
          colors={{ type: "sequential", scheme: "blues", minValue: 0, maxValue: max }}
          emptyColor="#f5f5f5"
          borderRadius={3}
          borderWidth={2}
          borderColor="#ffffff"
          inactiveOpacity={0.6}
          labelTextColor={(cell) => ((cell.value ?? 0) / max > 0.45 ? "#ffffff" : "#171717")}
          theme={{ text: { fontSize: 12, fill: "#404040" } }}
          animate={false}
          hoverTarget="cell"
        />
      </div>
    </VisualCard>
  );
}

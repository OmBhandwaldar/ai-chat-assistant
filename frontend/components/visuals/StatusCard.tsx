"use client";

import { Badge, Metric, Text } from "@tremor/react";
import { AlertTriangle, CheckCircle2, CircleHelp, XCircle } from "lucide-react";
import { normalizeStatus } from "@/lib/visual-data";
import { NoData, VisualCard } from "./shared";

const STATE_UI = {
  producing: { label: "Producing", color: "emerald", Icon: CheckCircle2 },
  not_producing: { label: "Not producing", color: "red", Icon: XCircle },
  unknown: { label: "Unknown", color: "gray", Icon: CircleHelp },
} as const;

export default function StatusCard({ data }: { data?: unknown }) {
  const s = normalizeStatus(data);
  if (!s) return <NoData />;
  const ui = STATE_UI[s.state];

  return (
    <VisualCard>
      <div className="flex items-start justify-between gap-3">
        <div>
          <Text className="text-neutral-500">Well status</Text>
          <Metric className="mt-1 text-neutral-900">{s.title}</Metric>
        </div>
        <Badge color={ui.color} icon={ui.Icon}>
          {ui.label}
        </Badge>
      </div>

      {s.metrics.length > 0 && (
        <dl className="mt-4 flex flex-wrap gap-x-8 gap-y-2">
          {s.metrics.map((m) => (
            <div key={m.label}>
              <dt className="text-xs text-neutral-500">{m.label}</dt>
              <dd className="text-sm font-medium text-neutral-900">{m.value}</dd>
            </div>
          ))}
        </dl>
      )}

      {s.reasons.length > 0 && (
        <div className="mt-4">
          <div className="mb-1.5 text-xs font-medium uppercase tracking-wide text-neutral-500">
            Why
          </div>
          <ul className="list-disc space-y-1 pl-5 text-sm text-neutral-800 marker:text-neutral-400">
            {s.reasons.map((r, i) => (
              <li key={i}>{r}</li>
            ))}
          </ul>
        </div>
      )}

      {s.conflicts.length > 0 && (
        <div className="mt-4 flex items-start gap-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-xs text-amber-900">
          <AlertTriangle size={14} className="mt-0.5 shrink-0" aria-hidden />
          <ul className="space-y-1">
            {s.conflicts.map((c, i) => (
              <li key={i}>
                <span className="font-medium">{c.left}</span> vs.{" "}
                <span className="font-medium">{c.right}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
    </VisualCard>
  );
}

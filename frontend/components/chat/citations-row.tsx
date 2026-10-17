"use client";

import { Badge } from "@/components/ui/badge";
import type { Evidence, Conflict } from "@/lib/types";

export function CitationsRow({ citations }: { citations?: Evidence[] }) {
  if (!citations || citations.length === 0) return null;
  return (
    <div className="mt-3 flex flex-wrap gap-1.5">
      {citations.map((c) => (
        <Badge
          key={c.source_id}
          variant="secondary"
          className="rounded-full bg-neutral-100 text-neutral-600 font-normal"
        >
          {c.source_id}
        </Badge>
      ))}
    </div>
  );
}

export function ConflictsPanel({ conflicts }: { conflicts?: Conflict[] }) {
  if (!conflicts || conflicts.length === 0) return null;
  return (
    <div className="mt-3 rounded-lg border border-amber-200 bg-amber-50 p-3">
      <div className="text-xs font-semibold text-amber-800 mb-1.5">
        Conflicting information
      </div>
      <ul className="space-y-1">
        {conflicts.map((c, i) => (
          <li key={i} className="text-xs text-amber-900">
            <span className="font-medium">{c.left}</span>
            {" vs. "}
            <span className="font-medium">{c.right}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}

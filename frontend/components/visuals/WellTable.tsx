"use client";

import { useMemo, useState } from "react";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeaderCell,
  TableRow,
} from "@tremor/react";
import { ArrowDown, ArrowUp, ChevronsUpDown } from "lucide-react";
import { formatCell, normalizeTable, toNumber } from "@/lib/visual-data";
import { NoData, VisualCard } from "./shared";

type SortDir = "asc" | "desc";

function compare(a: unknown, b: unknown): number {
  const na = toNumber(a);
  const nb = toNumber(b);
  if (na !== null && nb !== null) return na - nb;
  return String(a ?? "").localeCompare(String(b ?? ""), undefined, { numeric: true });
}

export default function WellTable({ data }: { data?: unknown }) {
  const { columns, rows } = useMemo(() => normalizeTable(data), [data]);
  const [sort, setSort] = useState<{ key: string; dir: SortDir } | null>(null);

  const sorted = useMemo(() => {
    if (!sort) return rows;
    const factor = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((a, b) => factor * compare(a[sort.key], b[sort.key]));
  }, [rows, sort]);

  if (rows.length === 0 || columns.length === 0) return <NoData />;

  function toggle(key: string) {
    setSort((s) =>
      !s || s.key !== key ? { key, dir: "asc" } : s.dir === "asc" ? { key, dir: "desc" } : null
    );
  }

  return (
    <VisualCard className="p-0">
      <div className="max-h-96 overflow-auto rounded-xl">
        <Table>
          <TableHead>
            <TableRow>
              {columns.map((c) => {
                const active = sort?.key === c.key;
                const Icon = !active ? ChevronsUpDown : sort.dir === "asc" ? ArrowUp : ArrowDown;
                return (
                  <TableHeaderCell
                    key={c.key}
                    aria-sort={active ? (sort.dir === "asc" ? "ascending" : "descending") : "none"}
                    className="whitespace-nowrap"
                  >
                    <button
                      type="button"
                      onClick={() => toggle(c.key)}
                      className="inline-flex cursor-pointer items-center gap-1 rounded text-neutral-700 hover:text-neutral-900 focus-visible:outline-2 focus-visible:outline-neutral-400"
                      aria-label={`Sort by ${c.label}`}
                    >
                      {c.label}
                      <Icon size={12} className={active ? "text-neutral-900" : "text-neutral-400"} aria-hidden />
                    </button>
                  </TableHeaderCell>
                );
              })}
            </TableRow>
          </TableHead>
          <TableBody>
            {sorted.map((row, i) => (
              <TableRow key={i}>
                {columns.map((c) => (
                  <TableCell key={c.key} className="text-neutral-800">
                    {formatCell(row[c.key])}
                  </TableCell>
                ))}
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </div>
    </VisualCard>
  );
}

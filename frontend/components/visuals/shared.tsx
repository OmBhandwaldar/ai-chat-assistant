import type { ReactNode } from "react";
import { cn } from "@/lib/utils";

export const CARD_CLASS = "rounded-xl border border-neutral-200 p-4 bg-white text-neutral-900";

export function VisualCard({
  title,
  children,
  className,
}: {
  title?: string;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn(CARD_CLASS, className)}>
      {title && <h3 className="mb-3 text-sm font-medium text-neutral-700">{title}</h3>}
      {children}
    </div>
  );
}

/** Muted placeholder for missing / empty visual data. */
export function NoData({ message = "No data" }: { message?: string }) {
  return (
    <div className={cn(CARD_CLASS, "text-sm text-neutral-500")} role="status">
      {message}
    </div>
  );
}

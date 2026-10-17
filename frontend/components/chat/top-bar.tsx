"use client";

import { MoreHorizontal, Share, SquarePen } from "lucide-react";

export function TopBar() {
  return (
    <div className="flex items-center justify-end gap-2 border-b border-neutral-200 bg-white px-4 py-3">
      <button
        className="rounded-lg p-2 text-neutral-400 hover:bg-neutral-100 hover:text-neutral-600"
        aria-label="More"
      >
        <MoreHorizontal size={18} />
      </button>
      <button className="flex items-center gap-1.5 rounded-full border border-neutral-200 px-3 py-1.5 text-sm font-medium text-neutral-700 hover:bg-neutral-50">
        <Share size={14} />
        Share
      </button>
      <button
        className="flex h-8 w-8 items-center justify-center rounded-full bg-neutral-100 text-neutral-600 hover:bg-neutral-200"
        aria-label="New chat"
      >
        <SquarePen size={15} />
      </button>
    </div>
  );
}

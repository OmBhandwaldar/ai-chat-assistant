"use client";

import { Lightbulb } from "lucide-react";
import type { ChatMessage } from "@/lib/types";
import { ThinkingPlaceholder } from "./thinking-placeholder";
import { CitationsRow, ConflictsPanel } from "./citations-row";
import { renderVisual } from "@/components/visuals/registry";

export function MessageList({ messages }: { messages: ChatMessage[] }) {
  if (messages.length === 0) {
    return (
      <div className="flex flex-1 items-center justify-center">
        <p className="text-sm text-neutral-400">
          Ask about a well, an area, or a trend to get started.
        </p>
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-6 overflow-y-auto px-4 py-6">
      {messages.map((m) =>
        m.role === "user" ? (
          <div key={m.id} className="flex justify-end">
            <div className="max-w-[80%] rounded-2xl bg-neutral-100 px-4 py-2.5 text-sm text-neutral-900">
              {m.text}
            </div>
          </div>
        ) : (
          <div key={m.id} className="flex flex-col gap-2">
            {m.thoughtSeconds !== undefined && (
              <div className="flex items-center gap-1.5 text-xs text-neutral-400">
                <Lightbulb size={13} />
                Thought for {m.thoughtSeconds}s
              </div>
            )}

            {m.streaming && !m.visual ? (
              <ThinkingPlaceholder />
            ) : (
              m.visual && m.visual.type !== "none" && renderVisual(m.visual)
            )}

            {m.text && (
              <p className="text-sm leading-relaxed text-neutral-800 whitespace-pre-wrap">
                {m.text}
              </p>
            )}

            <CitationsRow citations={m.citations} />
            <ConflictsPanel conflicts={m.conflicts} />
          </div>
        )
      )}
    </div>
  );
}

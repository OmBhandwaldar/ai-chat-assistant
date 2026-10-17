"use client";

import { useEffect, useRef } from "react";
import { Lightbulb } from "lucide-react";
import type { AppUser, ChatMessage } from "@/lib/types";
import { useAppStore } from "@/stores/app-store";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { ThinkingPlaceholder } from "./thinking-placeholder";
import { CitationsRow, ConflictsPanel } from "./citations-row";
import { AnswerMarkdown } from "./markdown";
import { VisualRenderer } from "@/components/visuals/registry";

function initials(name: string) {
  return name
    .split(" ")
    .filter(Boolean)
    .map((p) => p[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();
}

function UserBubble({ message, user }: { message: ChatMessage; user: AppUser | null }) {
  return (
    <div className="flex items-end justify-end gap-2.5">
      <div className="max-w-[80%] whitespace-pre-wrap rounded-2xl bg-neutral-100 px-4 py-2.5 text-sm text-neutral-900 [overflow-wrap:anywhere]">
        {message.text}
      </div>
      <Avatar size="sm" aria-label={user?.name ?? "You"} title={user?.name}>
        <AvatarFallback className="bg-neutral-200 text-[10px] font-medium text-neutral-700">
          {user ? initials(user.name) : "U"}
        </AvatarFallback>
      </Avatar>
    </div>
  );
}

function AgentBlock({ message }: { message: ChatMessage }) {
  const hasVisual = !!message.visual && message.visual.type !== "none";
  // Dotted placeholder only before any answer text or visual arrives.
  const waitingForVisual = message.streaming && !hasVisual && !message.text;

  return (
    <div className="flex flex-col gap-3">
      {message.thoughtSeconds !== undefined && (
        <div className="flex items-center gap-1.5 text-xs text-neutral-500">
          <Lightbulb size={13} aria-hidden />
          Thought for {message.thoughtSeconds}s
        </div>
      )}

      {waitingForVisual ? <ThinkingPlaceholder /> : hasVisual && <VisualRenderer visual={message.visual} />}

      {message.text && <AnswerMarkdown text={message.text} />}

      <CitationsRow citations={message.citations} />
      <ConflictsPanel conflicts={message.conflicts} />
    </div>
  );
}

export function MessageList({ messages }: { messages: ChatMessage[] }) {
  const currentUser = useAppStore((s) => s.currentUser);
  const bottomRef = useRef<HTMLDivElement>(null);

  // Keep the newest message (and streaming text) in view.
  const last = messages[messages.length - 1];
  const scrollKey = `${messages.length}:${last?.text.length ?? 0}:${last?.visual ? 1 : 0}:${last?.streaming ? 1 : 0}`;
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [scrollKey]);

  if (messages.length === 0) {
    return (
      <div className="flex min-h-0 flex-1 items-center justify-center">
        <p className="text-sm text-neutral-500">
          Ask about a well, an area, or a trend to get started.
        </p>
      </div>
    );
  }

  return (
    // The list itself is the scroll container, so the input bar below stays pinned.
    <div className="min-h-0 flex-1 overflow-y-auto" role="log" aria-live="polite" aria-label="Conversation">
      <div className="mx-auto flex w-full max-w-3xl flex-col gap-8 px-4 py-8">
        {messages.map((m) =>
          m.role === "user" ? (
            <UserBubble key={m.id} message={m} user={currentUser} />
          ) : (
            <AgentBlock key={m.id} message={m} />
          )
        )}
        <div ref={bottomRef} />
      </div>
    </div>
  );
}

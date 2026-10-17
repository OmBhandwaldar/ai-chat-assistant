"use client";

import { useState, type KeyboardEvent } from "react";
import { Paperclip, Mic, ArrowUp, ChevronDown, Square } from "lucide-react";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

interface ChatInputProps {
  onSend: (text: string) => void;
  isStreaming?: boolean;
  onStop?: () => void;
}

const MODELS = ["Expert", "Fast"];

export function ChatInput({ onSend, isStreaming, onStop }: ChatInputProps) {
  const [value, setValue] = useState("");
  const [model, setModel] = useState("Expert");

  function handleSend() {
    const text = value.trim();
    if (!text || isStreaming) return;
    onSend(text);
    setValue("");
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  return (
    <div className="mx-auto w-full max-w-3xl px-4 pb-6">
      <div className="flex items-end gap-2 rounded-2xl border border-neutral-200 bg-white px-3 py-2.5 shadow-sm">
        <button
          className="shrink-0 rounded-lg p-2 text-neutral-400 hover:bg-neutral-100 hover:text-neutral-600"
          aria-label="Attach"
          type="button"
        >
          <Paperclip size={18} />
        </button>

        <textarea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          rows={1}
          placeholder="Ask anything"
          className="max-h-40 flex-1 resize-none bg-transparent py-1.5 text-sm text-neutral-900 placeholder:text-neutral-400 focus:outline-none"
        />

        <DropdownMenu>
          <DropdownMenuTrigger className="shrink-0 flex items-center gap-1 rounded-lg px-2 py-1.5 text-sm text-neutral-600 hover:bg-neutral-100">
            {model}
            <ChevronDown size={14} className="text-neutral-400" />
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            {MODELS.map((m) => (
              <DropdownMenuItem key={m} onClick={() => setModel(m)}>
                {m}
              </DropdownMenuItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>

        <button
          className="shrink-0 rounded-lg p-2 text-neutral-400 hover:bg-neutral-100 hover:text-neutral-600"
          aria-label="Voice input"
          type="button"
        >
          <Mic size={18} />
        </button>

        {isStreaming ? (
          <button
            onClick={onStop}
            type="button"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-neutral-900 text-white hover:bg-neutral-700"
            aria-label="Stop"
          >
            <Square size={14} fill="white" />
          </button>
        ) : (
          <button
            onClick={handleSend}
            disabled={!value.trim()}
            type="button"
            className="flex h-9 w-9 shrink-0 items-center justify-center rounded-full bg-neutral-900 text-white hover:bg-neutral-700 disabled:bg-neutral-200 disabled:text-neutral-400"
            aria-label="Send"
          >
            <ArrowUp size={16} />
          </button>
        )}
      </div>
    </div>
  );
}

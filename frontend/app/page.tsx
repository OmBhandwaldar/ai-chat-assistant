"use client";

import { useEffect } from "react";
import { Sidebar } from "@/components/chat/sidebar";
import { TopBar } from "@/components/chat/top-bar";
import { MessageList } from "@/components/chat/message-list";
import { ChatInput } from "@/components/chat/chat-input";
import { useAppStore } from "@/stores/app-store";
import type { AppUser } from "@/lib/types";

// TODO(Phase 5.1, Sonnet): fetch from GET /users instead of this stub list.
const STUB_USERS: AppUser[] = [
  { user_id: "U-201", name: "Michael Turner", role: "production_supervisor", team_id: "TEAM-NE" },
  { user_id: "U-204", name: "Tyler Hughes", role: "field_operator", team_id: "TEAM-NE" },
  { user_id: "U-202", name: "Jessica Reed", role: "field_inspector", team_id: "TEAM-NE" },
  { user_id: "U-102", name: "Jake Morales", role: "field_operator", team_id: "TEAM-SW" },
  { user_id: "U-401", name: "Laura Simmons", role: "hse_officer", team_id: "TEAM-HSE" },
];

export default function ChatPage() {
  const messages = useAppStore((s) => s.messages);
  const appendMessage = useAppStore((s) => s.appendMessage);
  const setUsers = useAppStore((s) => s.setUsers);
  const setCurrentUser = useAppStore((s) => s.setCurrentUser);
  const currentUser = useAppStore((s) => s.currentUser);

  useEffect(() => {
    setUsers(STUB_USERS);
    if (!currentUser) setCurrentUser(STUB_USERS[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // TODO(Phase 4.3, Sonnet): replace with AG-UI streaming via CopilotKit client.
  function handleSend(text: string) {
    appendMessage({
      id: crypto.randomUUID(),
      role: "user",
      text,
      ts: new Date().toISOString(),
    });
    appendMessage({
      id: crypto.randomUUID(),
      role: "assistant",
      text: "Agent wiring not connected yet — this is a placeholder response.",
      ts: new Date().toISOString(),
    });
  }

  return (
    <div className="flex h-dvh w-full bg-white">
      <Sidebar />
      <div className="flex flex-1 flex-col">
        <TopBar />
        <MessageList messages={messages} />
        <ChatInput onSend={handleSend} />
      </div>
    </div>
  );
}

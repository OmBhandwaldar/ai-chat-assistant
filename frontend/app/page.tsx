"use client";

import { useEffect } from "react";
import { Sidebar } from "@/components/chat/sidebar";
import { TopBar } from "@/components/chat/top-bar";
import { MessageList } from "@/components/chat/message-list";
import { ChatInput } from "@/components/chat/chat-input";
import { useAppStore } from "@/stores/app-store";
import { fetchUsers, fetchConversations, streamAgentRun } from "@/lib/api";

export default function ChatPage() {
  const messages = useAppStore((s) => s.messages);
  const appendMessage = useAppStore((s) => s.appendMessage);
  const updateMessage = useAppStore((s) => s.updateMessage);
  const setUsers = useAppStore((s) => s.setUsers);
  const setCurrentUser = useAppStore((s) => s.setCurrentUser);
  const currentUser = useAppStore((s) => s.currentUser);
  const setConversations = useAppStore((s) => s.setConversations);
  const activeConversationId = useAppStore((s) => s.activeConversationId);
  const setActiveConversationId = useAppStore((s) => s.setActiveConversationId);
  const isStreaming = useAppStore((s) => s.isStreaming);
  const beginRun = useAppStore((s) => s.beginRun);
  const endRun = useAppStore((s) => s.endRun);
  const stopRun = useAppStore((s) => s.stopRun);

  useEffect(() => {
    fetchUsers()
      .then((users) => {
        setUsers(users);
        if (!useAppStore.getState().currentUser && users.length > 0) {
          setCurrentUser(users[0]);
        }
      })
      .catch((err) => console.error("Failed to load users", err));
  }, [setUsers, setCurrentUser]);

  const currentUserId = currentUser?.user_id;
  useEffect(() => {
    if (!currentUserId) return;
    let cancelled = false;
    fetchConversations(currentUserId)
      .then((c) => {
        if (!cancelled) setConversations(c);
      })
      .catch((err) => console.error("Failed to load conversations", err));
    return () => {
      cancelled = true;
    };
  }, [currentUserId, setConversations]);

  async function handleSend(text: string) {
    if (!currentUser || isStreaming) return;
    const userId = currentUser.user_id;

    // A new chat has no thread yet — mint one now so every later turn shares it.
    const threadId = activeConversationId ?? crypto.randomUUID();
    if (threadId !== activeConversationId) setActiveConversationId(threadId);

    const userMessageId = crypto.randomUUID();
    appendMessage({
      id: userMessageId,
      role: "user",
      text,
      ts: new Date().toISOString(),
    });

    const assistantMessageId = crypto.randomUUID();
    appendMessage({
      id: assistantMessageId,
      role: "assistant",
      text: "",
      streaming: true,
      ts: new Date().toISOString(),
    });

    const wireMessages = [...messages, { id: userMessageId, role: "user" as const, text }].map(
      (m) => ({ id: m.id, role: m.role, content: m.text })
    );

    const controller = beginRun();
    const startedAt = Date.now();
    let streamedText = "";
    try {
      await streamAgentRun(
        {
          userId,
          threadId,
          runId: crypto.randomUUID(),
          messages: wireMessages,
          signal: controller.signal,
        },
        {
          onTextDelta: (delta) => {
            streamedText += delta;
            // Structured output streams the answer prose, then the JSON tail
            // (`"citations"`, `"visual"`). Show only the prose until agent_answer.
            const cut = streamedText.search(/"\s*,\s*"(?:citations|visual|conflicts)"\s*:/);
            updateMessage(assistantMessageId, {
              text: cut === -1 ? streamedText : streamedText.slice(0, cut),
            });
          },
          onAnswer: (answer) => {
            updateMessage(assistantMessageId, {
              text: answer.text,
              visual: answer.visual,
              citations: answer.citations,
              conflicts: answer.conflicts,
              streaming: false,
              thoughtSeconds: Math.round((Date.now() - startedAt) / 1000),
            });
          },
          onError: (message) => {
            updateMessage(assistantMessageId, { text: message, streaming: false });
          },
        }
      );
    } finally {
      endRun(controller);
      const stopped = controller.signal.aborted;
      const placeholder = useAppStore
        .getState()
        .messages.find((m) => m.id === assistantMessageId);
      updateMessage(assistantMessageId, {
        streaming: false,
        ...(stopped && placeholder && !placeholder.text ? { text: "Stopped." } : {}),
      });
    }

    // Refresh History so the new/updated conversation shows up — but only if the same user
    // is still on screen (they may have switched while the run was in flight).
    if (useAppStore.getState().currentUser?.user_id === userId) {
      fetchConversations(userId)
        .then((c) => {
          if (useAppStore.getState().currentUser?.user_id === userId) setConversations(c);
        })
        .catch((err) => console.error("Failed to refresh conversations", err));
    }
  }

  return (
    <div className="flex h-screen bg-white text-neutral-900">
      <Sidebar />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <TopBar />
        <MessageList messages={messages} />
        <ChatInput onSend={handleSend} isStreaming={isStreaming} onStop={stopRun} />
      </div>
    </div>
  );
}

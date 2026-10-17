import { create } from "zustand";
import type { AppUser, ChatMessage, ConversationSummary } from "@/lib/types";

interface AppState {
  sidebarCollapsed: boolean;
  toggleSidebar: () => void;

  users: AppUser[];
  currentUser: AppUser | null;
  setUsers: (users: AppUser[]) => void;
  setCurrentUser: (user: AppUser) => void;

  conversations: ConversationSummary[];
  setConversations: (c: ConversationSummary[]) => void;

  activeConversationId: string | null;
  setActiveConversationId: (id: string | null) => void;

  messages: ChatMessage[];
  setMessages: (messages: ChatMessage[]) => void;
  appendMessage: (message: ChatMessage) => void;
  updateMessage: (id: string, patch: Partial<ChatMessage>) => void;
}

export const useAppStore = create<AppState>((set) => ({
  sidebarCollapsed: false,
  toggleSidebar: () =>
    set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),

  users: [],
  currentUser: null,
  setUsers: (users) => set({ users }),
  setCurrentUser: (currentUser) => set({ currentUser }),

  conversations: [],
  setConversations: (conversations) => set({ conversations }),

  activeConversationId: null,
  setActiveConversationId: (activeConversationId) =>
    set({ activeConversationId }),

  messages: [],
  setMessages: (messages) => set({ messages }),
  appendMessage: (message) =>
    set((s) => ({ messages: [...s.messages, message] })),
  updateMessage: (id, patch) =>
    set((s) => ({
      messages: s.messages.map((m) => (m.id === id ? { ...m, ...patch } : m)),
    })),
}));

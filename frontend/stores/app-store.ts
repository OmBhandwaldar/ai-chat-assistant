import { create } from "zustand";
import type { AppUser, ChatMessage, ConversationSummary } from "@/lib/types";
import { fetchConversationMessages } from "@/lib/api";

interface AppState {
  sidebarCollapsed: boolean;
  toggleSidebar: () => void;

  users: AppUser[];
  currentUser: AppUser | null;
  setUsers: (users: AppUser[]) => void;
  /** Switching to a different user wipes the on-screen chat: history is private per user. */
  setCurrentUser: (user: AppUser) => void;

  conversations: ConversationSummary[];
  setConversations: (c: ConversationSummary[]) => void;

  /** null = a brand-new chat; the thread id is minted on the first send. */
  activeConversationId: string | null;
  setActiveConversationId: (id: string | null) => void;
  /** Start a fresh, empty chat (cancels any in-flight run). */
  startNewConversation: () => void;
  /** Load a past conversation of the current user into the chat pane. */
  openConversation: (conversationId: string) => Promise<void>;

  messages: ChatMessage[];
  setMessages: (messages: ChatMessage[]) => void;
  appendMessage: (message: ChatMessage) => void;
  updateMessage: (id: string, patch: Partial<ChatMessage>) => void;

  isStreaming: boolean;
  /** Abort any previous run and return a controller for the new one. */
  beginRun: () => AbortController;
  /** Mark a run finished; a no-op if a newer run has already replaced it. */
  endRun: (controller: AbortController) => void;
  /** Stop the in-flight run, if any (Stop button, new chat, user switch). */
  stopRun: () => void;
}

// Held outside the zustand state: controllers aren't render data.
let activeRun: AbortController | null = null;

export const useAppStore = create<AppState>((set, get) => ({
  sidebarCollapsed: false,
  toggleSidebar: () =>
    set((s) => ({ sidebarCollapsed: !s.sidebarCollapsed })),

  users: [],
  currentUser: null,
  setUsers: (users) => set({ users }),
  setCurrentUser: (user) => {
    if (get().currentUser?.user_id === user.user_id) return;
    get().stopRun();
    set({
      currentUser: user,
      conversations: [],
      activeConversationId: null,
      messages: [],
    });
  },

  conversations: [],
  setConversations: (conversations) => set({ conversations }),

  activeConversationId: null,
  setActiveConversationId: (activeConversationId) =>
    set({ activeConversationId }),

  startNewConversation: () => {
    get().stopRun();
    set({ activeConversationId: null, messages: [] });
  },

  openConversation: async (conversationId) => {
    const user = get().currentUser;
    if (!user) return;
    get().stopRun();
    set({ activeConversationId: conversationId, messages: [] });
    try {
      const stored = await fetchConversationMessages(conversationId, user.user_id);
      // Ignore a late response if the user clicked elsewhere / switched user meanwhile.
      if (
        get().activeConversationId !== conversationId ||
        get().currentUser?.user_id !== user.user_id
      ) {
        return;
      }
      set({
        messages: stored.map((m) => ({ id: m.id, role: m.role, text: m.text, ts: m.ts })),
      });
    } catch (err) {
      console.error("Failed to load conversation", err);
    }
  },

  messages: [],
  setMessages: (messages) => set({ messages }),
  appendMessage: (message) =>
    set((s) => ({ messages: [...s.messages, message] })),
  updateMessage: (id, patch) =>
    set((s) => ({
      messages: s.messages.map((m) => (m.id === id ? { ...m, ...patch } : m)),
    })),

  isStreaming: false,
  beginRun: () => {
    activeRun?.abort();
    const controller = new AbortController();
    activeRun = controller;
    set({ isStreaming: true });
    return controller;
  },
  endRun: (controller) => {
    if (activeRun !== controller) return;
    activeRun = null;
    set({ isStreaming: false });
  },
  stopRun: () => {
    activeRun?.abort();
    activeRun = null;
    set({ isStreaming: false });
  },
}));
